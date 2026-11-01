"""Один проход мониторинга: DeviantArt + Toyhouse → SQLite."""

import contextlib
import logging
import time
from dataclasses import dataclass, field

from . import db
from . import deviantart as da
from . import toyhouse as th
from .classify import classify_lot, classify_thread, keyword_hits, match_lot, within_budget
from .config import Config

log = logging.getLogger(__name__)

SOURCES = ("deviantart", "toyhouse")

# После ошибок (429, проверка Cloudflare, нет сети) интервал удваивается, но не дольше 6 часов.
MAX_BACKOFF_S = 6 * 3600
# Ручные проверки из Claude не чаще, чем раз в N секунд, чтобы случайно не долбить сайты.
MANUAL_COOLDOWN_S = {"toyhouse": 10 * 60, "deviantart": 2 * 60}
RATE_LIMIT_COOLDOWN_S = 30 * 60
RETRY_AFTER_FAIL_S = 60


@dataclass
class SourceResult:
    source: str
    seen: int = 0
    new: int = 0
    new_matched: int = 0
    skipped: str | None = None
    error: str | None = None
    needs_login: bool = False
    notes: list[str] = field(default_factory=list)


def is_due(conn, source: str, minutes: int, now: float) -> bool:
    row = db.last_run(conn, source)
    if not row or not row["last_run_ts"]:
        return True
    interval = minutes * 60 * 2 ** min(row["fail_count"] or 0, 8)
    return now - row["last_run_ts"] >= min(interval, max(MAX_BACKOFF_S, minutes * 60)) - 60


def cooldown_reason(conn, source: str, now: float) -> str | None:
    row = db.last_run(conn, source)
    if not row or not row["last_run_ts"]:
        return None
    ago = now - row["last_run_ts"]
    err = row["last_error"] or ""
    if source == "deviantart" and ("429" in err or "403" in err) and ago < RATE_LIMIT_COOLDOWN_S:
        return f"DeviantArt недавно просил притормозить, подожду ещё {int((RATE_LIMIT_COOLDOWN_S - ago) // 60) + 1} мин"
    if err:
        # Прошлая попытка не удалась (например, она как раз нажала галочку): повтор можно почти сразу.
        if ago < RETRY_AFTER_FAIL_S:
            return f"прошлая проверка не удалась ({err[:120]}), попробую снова через минуту"
        return None
    if ago < MANUAL_COOLDOWN_S[source]:
        return f"проверяли {int(ago // 60)} мин назад, данные свежие"
    return None


def check_deviantart(conn, cfg: Config, now: float | None = None, http=None) -> SourceResult:
    res = SourceResult("deviantart")
    me = cfg.deviantart.my_username.lower()
    try:
        feeds = da.feeds_from_config(cfg.deviantart.groups, cfg.deviantart.artists, cfg.deviantart.my_username)
    except da.DAError as e:
        res.error = str(e)
        db.record_run(conn, "deviantart", res.error, now)
        return res
    if not feeds:
        res.skipped = "в настройках нет групп и художников"
        return res

    own_http = http is None
    http = http or da.client()
    try:
        for i, feed in enumerate(feeds):
            if i:
                da.polite_pause()
            try:
                entries = da.fetch_feed(http, feed.query, feed.place)
            except da.RateLimited as e:
                res.error = str(e)
                break
            except (da.DAError, OSError) as e:
                res.notes.append(f"{feed.place}: {e}")
                continue
            for entry in entries:
                item = da.entry_to_item(entry)
                lot = classify_lot(entry["title"], entry["text"], entry["tags"])
                item.lot_type, item.status, item.price_text = lot.lot_type, lot.status, lot.price_text
                item.price_usd, item.price_points = lot.usd, lot.points
                item.own = feed.own or bool(me and (entry["author"] or "").lower() == me)
                item.matched, item.match_reason = match_lot(cfg.filters, lot, entry["title"], entry["text"], entry["tags"])
                # Её собственные лоты и папки «только для рынка» в находки не попадают.
                if item.own:
                    item.matched = False
                elif not feed.notify:
                    # Тот же лот мог прийти и из обычной папки: находкой остаётся, пока проходит фильтры.
                    old = db.get_item(conn, item.id)
                    keep = bool(old and old["match_kind"] == "lot")
                    if item.matched and keep and old:
                        item.match_reason = old["match_reason"] or item.match_reason
                    item.matched = item.matched and keep
                item.match_kind = "lot" if item.matched else None
                res.seen += 1
                if db.upsert_item(conn, item, now):
                    res.new += 1
                    res.new_matched += int(item.matched)
            conn.commit()
    finally:
        if own_http:
            http.close()

    if res.notes and not res.error and len(res.notes) == len(feeds):
        res.error = "ни один источник не ответил: " + "; ".join(res.notes[:3])
    db.record_run(conn, "deviantart", res.error, now)
    return res


def match_thread(cfg: Config, kind, title: str, snippet: str | None, activity_ts: float | None,
                 now: float) -> tuple[str | None, str]:
    """(lf | sale | None, причина)."""
    if not activity_ts or now - activity_ts > cfg.toyhouse.active_days * 86400:
        return None, "давно без активности"
    if kind.wants_characters:
        hits = keyword_hits(cfg.toyhouse.lf_keywords, title, snippet or "")
        if not cfg.toyhouse.lf_keywords or hits:
            reason = ["ищут персов"]
            if kind.pays:
                reason.append("платят: " + "/".join(kind.pays))
            if hits:
                reason.append("слова: " + ", ".join(hits))
            return "lf", " · ".join(reason)
    # Треды продаж показываем, только если задано, что она хочет купить: иначе это поток на сотни строк.
    if kind.sells_characters and cfg.filters.keywords:
        if keyword_hits(cfg.filters.exclude, title):
            return None, "исключено"
        lot = classify_lot(title, snippet or "")
        if not within_budget(cfg.filters, lot.usd, lot.points):
            return None, "дороже бюджета"
        hits = keyword_hits(cfg.filters.keywords, title, snippet or "")
        if hits:
            reason = ["продают", "слова: " + ", ".join(hits)]
            if lot.price_text:
                reason.insert(1, lot.price_text)
            return "sale", " · ".join(reason)
    return None, "не подходит"


def _thread_item(cfg: Config, row: th.ThreadRow, snippet: str | None, now: float) -> db.Item:
    item = th.row_to_item(row)
    kind = classify_thread(row.title)
    item.wants_characters, item.sells_characters = kind.wants_characters, kind.sells_characters
    item.pays = ",".join(kind.pays) or None
    item.snippet = snippet
    lot = classify_lot(row.title)
    item.price_text, item.price_usd, item.price_points = lot.price_text, lot.usd, lot.points
    item.match_kind, item.match_reason = match_thread(cfg, kind, row.title, snippet, row.activity_ts, now)
    item.matched = item.match_kind is not None
    return item


def ask_to_pass_check() -> None:
    from .digest import notify_macos

    notify_macos("Toyhouse", "Просит подтвердить, что ты человек: нажми галочку в окне Chrome")


def ask_in_her_chrome() -> None:
    from .digest import notify_macos

    notify_macos("Toyhouse", "Просит подтвердить, что ты человек: вкладка в твоём Chrome открыта, нажми галочку")


def _challenged(res: SourceResult, e: th.ChallengeError) -> None:
    res.error, res.needs_login = str(e), True


def _read_boards(reader, cfg: Config, now: float, res: SourceResult) -> list[th.ThreadRow]:
    rows: dict[str, th.ThreadRow] = {}
    first = True
    for board in cfg.toyhouse.boards:
        for page in range(1, cfg.toyhouse.pages_per_board + 1):
            if not first:
                th.human_pause()
            first = False
            try:
                # Тред мог переехать на другую страницу между загрузками: берём первое появление.
                for row in reader.board(board, page, now):
                    rows.setdefault(row.thread_id, row)
            except th.ChallengeError as e:
                _challenged(res, e)
                return list(rows.values())
            except th.ToyhouseError as e:
                res.notes.append(str(e))
    return list(rows.values())


def _fetch_snippets(conn, reader, cfg: Config, rows: list[th.ThreadRow], now: float,
                    res: SourceResult) -> dict[str, str | None]:
    """Первые посты новых живых LF-тредов: там написано, что именно ищут.

    Только чтение страниц, в базу здесь не пишем, чтобы не держать её заблокированной
    во время пауз и загрузок.
    """
    snippets: dict[str, str | None] = {}
    budget = 0 if res.needs_login else cfg.toyhouse.read_first_posts
    for row in rows:
        old = db.get_item(conn, f"th:{row.thread_id}")
        snippets[row.thread_id] = old["snippet"] if old else None
        if snippets[row.thread_id] is not None or budget <= 0:
            continue
        fresh = row.activity_ts and now - row.activity_ts <= cfg.toyhouse.active_days * 86400
        if not (fresh and classify_thread(row.title).wants_characters):
            continue
        budget -= 1
        th.human_pause()
        try:
            # Пустая строка = «читали, текста нет», чтобы не перечитывать каждый раз.
            snippets[row.thread_id] = reader.first_post(row.url) or ""
        except th.ChallengeError as e:
            _challenged(res, e)
            budget = 0
        except th.ToyhouseError as e:
            res.notes.append(str(e))
    return snippets


def _save_threads(conn, cfg: Config, rows: list[th.ThreadRow], snippets: dict, now: float,
                  res: SourceResult) -> None:
    for row in rows:
        item = _thread_item(cfg, row, snippets.get(row.thread_id), now)
        res.seen += 1
        if db.upsert_item(conn, item, now):
            res.new += 1
            res.new_matched += int(item.matched)
    conn.commit()


@contextlib.contextmanager
def _bot_reader(cfg: Config, interactive: bool, browser):
    with browser(headless=cfg.toyhouse.headless) as context:
        yield th.Reader(context, interactive=interactive and not cfg.toyhouse.headless,
                        on_challenge=ask_to_pass_check)


def check_toyhouse(conn, cfg: Config, now: float | None = None, browser=None,
                   interactive: bool = False, her_chrome_session=None) -> SourceResult:
    res = SourceResult("toyhouse")
    now = now or time.time()
    if not cfg.toyhouse.boards:
        res.skipped = "в настройках не выбраны доски"
        return res
    if cfg.toyhouse.mode == "chrome":
        from . import her_chrome

        opener = her_chrome_session or her_chrome.open_her_chrome
        session = opener(interactive=interactive, on_challenge=ask_in_her_chrome)
    else:
        session = _bot_reader(cfg, interactive, browser or th.open_browser)
    try:
        with session as reader:
            rows = _read_boards(reader, cfg, now, res)
            if not rows and res.notes and not res.error:
                raise th.ToyhouseError("; ".join(res.notes[:3]))
            snippets = _fetch_snippets(conn, reader, cfg, rows, now, res)
        # Что успели прочитать до проверки Cloudflare, сохраняем всё равно.
        _save_threads(conn, cfg, rows, snippets, now, res)
    except th.BrowserBusy as e:
        res.skipped = str(e)
        return res
    except th.BrowserClosed:
        res.error = "окно Chrome закрыли во время проверки"
    except Exception as e:  # Playwright бросает свои ошибки, их много разных
        log.warning("toyhouse check failed: %s: %s", type(e).__name__, e)
        res.error = f"{type(e).__name__}: {e}"
    db.record_run(conn, "toyhouse", res.error, now)
    return res


def format_results(results: list[SourceResult]) -> list[str]:
    lines = []
    for r in results:
        if r.skipped:
            lines.append(f"{r.source}: пропуск — {r.skipped}")
        elif r.error:
            lines.append(f"{r.source}: ошибка — {r.error}")
        else:
            lines.append(f"{r.source}: просмотрено {r.seen}, новых {r.new}, подходит из новых {r.new_matched}")
        lines.extend(f"  ! {n}" for n in r.notes[:5])
    return lines


CHROME_MODE_SKIP ="режим chrome: Toyhouse читается по просьбе в чате, в её Chrome"


def _guarded(fn, source: str, conn, *args, **kwargs) -> SourceResult:
    """Ошибка одного источника не должна мешать другому и дайджесту."""
    try:
        return fn(conn, *args, **kwargs)
    except Exception as e:
        log.exception("%s check crashed", source)
        res = SourceResult(source, error=f"{type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            db.record_run(conn, source, res.error)
        return res


def run_check(conn, cfg: Config, sources=SOURCES, force: bool = False, now: float | None = None,
              interactive: bool = False, manual_cooldown: bool = False) -> list[SourceResult]:
    now = now or time.time()
    polls = {"deviantart": cfg.deviantart.poll_minutes, "toyhouse": cfg.toyhouse.poll_minutes}
    checks = {
        "deviantart": lambda: _guarded(check_deviantart, "deviantart", conn, cfg, now),
        "toyhouse": lambda: _guarded(check_toyhouse, "toyhouse", conn, cfg, now, interactive=interactive),
    }
    results = []
    for source in SOURCES:
        if source not in sources:
            continue
        reason = cooldown_reason(conn, source, now) if manual_cooldown else None
        if source == "toyhouse" and cfg.toyhouse.mode == "chrome" and not interactive:
            results.append(SourceResult(source, skipped=CHROME_MODE_SKIP))
        elif reason:
            results.append(SourceResult(source, skipped=reason))
        elif force or is_due(conn, source, polls[source], now):
            results.append(checks[source]())
        else:
            results.append(SourceResult(source, skipped="ещё рано"))
    return results
