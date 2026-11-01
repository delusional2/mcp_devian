"""Один проход мониторинга: DeviantArt + Toyhouse → SQLite."""

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field

from . import da_access, da_api, da_feeds, db, th_match
from . import deviantart as da
from . import toyhouse as th
from .classify import classify_lot, classify_thread, keyword_hits, match_lot, within_budget
from .config import Config

log = logging.getLogger(__name__)

# Сколько страниц ленты (60 работ RSS / 24 API) читать за раз: обычно одна, следующая — только если на ней
# всё новое; глубже (лоты, закрывшиеся за 3 дня) — раз в da_feeds.DEEP_EVERY_S и если хватает бюджета часа.
FEED_PAGES = 3

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
    # DeviantArt просит притормозить или кончился бюджет запросов часа: одна строка для отчёта.
    limited: str | None = None


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


def _api_client(cfg: Config):
    """Клиент API, если она подключила DeviantArt; иначе None (ленты читаем через RSS)."""
    from . import posting

    try:
        client = posting.da_client(cfg)
        return client if client.is_connected() else None
    except Exception:  # noqa: BLE001 — нет client_id, битый файл токена и т.п.
        return None


def check_deviantart(conn, cfg: Config, now: float | None = None, http=None, client=None,
                     max_feeds: int | None = None, sleep=time.sleep) -> SourceResult:
    """Ленты DeviantArt → база. Через API, если DA подключён (client), иначе RSS; max_feeds — сколько лент
    за проход (по кругу, da_feeds.pick), None — все. Все запросы — в общем бюджете da_access.

    Если передан http, а client нет, — только RSS (тесты и явный вызов): настоящий API сам не берём."""
    res = SourceResult("deviantart")
    try:
        feeds = da.feeds_from_config(cfg.deviantart.groups, cfg.deviantart.artists, cfg.deviantart.my_username)
    except da.DAError as e:
        res.error = str(e)
        db.record_run(conn, "deviantart", res.error, now)
        return res
    if not feeds:
        res.skipped = "в настройках нет групп и художников"
        return res
    if client is None and http is None:
        client = _api_client(cfg)
    with da_access.session(conn), da_access.task("ленты"):
        return _read_feeds(conn, cfg, feeds, res, now, http, client, max_feeds, sleep)


def _read_feeds(conn, cfg: Config, feeds: list, res: SourceResult, now: float | None, http, client,
                max_feeds: int | None, sleep) -> SourceResult:
    from . import da_api

    ts = now or time.time()
    api_ok = client is not None and da_access.blocked(conn, da_access.API) is None
    rss_ok = da_access.blocked(conn, da_access.RSS) is None
    if not api_ok and not rss_ok:
        # Ни одного запроса: DeviantArt просил подождать (или бюджет часа кончился). Проход не записываем
        # ошибкой — пауза da_access сама решает, когда снова можно.
        res.limited = _limit_line(conn, False, None)
        res.skipped = res.limited
        return res
    last = da_feeds.last_reads(conn)
    chosen = da_feeds.pick(feeds, last, da_feeds.priority_keys(conn, feeds, ts), max_feeds)
    me = cfg.deviantart.my_username.lower()
    own_http = http is None
    throttled: Exception | None = None
    read = left = rss_calls = 0
    try:
        for i, feed in enumerate(chosen):
            entries, via, uuid, valid = None, "rss", None, None
            state: dict = {}
            rest = len(chosen) - i - 1
            due = da_feeds.deep_due(conn, feed.query, ts)
            deep = False
            # Новая лента (ни разу не читали): в ней всё «новое», и обычное листание прочитало бы все страницы
            # сразу у всех новых лент. Глубже первой страницы её прочитает глубокое чтение, когда хватит бюджета.
            shallow_pages = FEED_PAGES if feed.query in last else 1
            known = lambda dev_id: bool(db.known_ids(conn, [f"da:{dev_id}"]))  # noqa: E731
            if api_ok and not da_feeds.api_missed(conn, feed.query, ts, "order"):
                if i:
                    sleep(da_feeds.API_PAUSE_S)
                deep = _deep_ok(conn, da_access.API, due, rest)
                try:
                    uuid = da_feeds.folder_uuid(conn, client, feed, ts) if feed.folder_id else None
                    pages = FEED_PAGES if deep else shallow_pages
                    entries = da_feeds.read_api(conn, client, feed, uuid, ts, pages=pages, known=known,
                                                sleep=sleep, deep=deep, state=state)
                    via = "api"
                except da_api.Throttled as e:
                    if e.why != "budget":
                        throttled, api_ok = e, False
                        left += len(chosen) - i
                        break
                    # Свой бюджет API на час кончился (DeviantArt ни о чём не просил): дальше — через RSS.
                    throttled, api_ok = e, False
                except da_api.RateLimited as e:
                    # Пауза включена (da_access); RSS в этом же проходе не дёргаем — остальное в следующий раз.
                    throttled, api_ok = e, False
                    left += len(chosen) - i
                    break
                except da_api.NotConnected:
                    api_ok = False
                    res.notes.append("DeviantArt отключился — ленты читаю через RSS")
                except da_feeds.NoFolderUuid:
                    pass
                except da_api.DAApiError as e:
                    log.info("API feed %s failed, RSS instead: %s", feed.query, e)
            if entries is None:
                if not rss_ok:
                    left += 1
                    continue
                http = http or da.client()
                if rss_calls:
                    da.polite_pause()
                rss_calls += 1
                deep = _deep_ok(conn, da_access.RSS, due, rest)
                # Глубокое чтение — без условного GET: 304 на первой странице скрыл бы закрытия ниже.
                valid = {} if deep else da_feeds.validators(conn, feed.query)
                try:
                    pages = FEED_PAGES if deep else shallow_pages
                    entries = da.fetch_feed(http, feed.query, feed.place, pages=pages, known=known,
                                            validators=valid, deep=deep, state=state)
                except da.RateLimited as e:
                    throttled, rss_ok = e, False
                    if not api_ok:
                        left += len(chosen) - i
                        break
                    left += 1
                    continue
                except (da.DAError, OSError) as e:
                    res.notes.append(f"{feed.place}: {e}")
                    # Сломанная лента не должна вечно стоять первой в очереди.
                    da_feeds.mark_read(conn, feed.query, ts + i * 1e-3, "rss")
                    continue
                for entry in entries:
                    entry.setdefault("desc_ts", ts)  # в RSS полное описание: метаданные API для него не нужны
            # Прочитанное до отказа на 2-й странице (или на метаданных) сохраняем: запросы уже потрачены.
            _save_entries(conn, cfg, feed, entries, me, res, now)
            if via == "api":
                da_feeds.save_stats(conn, entries, ts)
            # +i мс: прочитанные раньше в этом проходе встанут в очередь раньше (иначе при равном времени
            # первой всегда оказывалась бы одна и та же лента).
            da_feeds.mark_read(conn, feed.query, ts + i * 1e-3, via, valid, uuid)
            read += 1
            cut = state.get("cut")
            if deep and cut is None:
                da_feeds.mark_deep(conn, feed.query, ts)
            if isinstance(cut, da_api.RateLimited):
                throttled, api_ok = cut, False
                if not (isinstance(cut, da_api.Throttled) and cut.why == "budget"):
                    left += rest
                    break
            elif isinstance(cut, da.RateLimited):
                throttled, rss_ok = cut, False
                if not api_ok:
                    left += rest
                    break
    finally:
        if own_http and http is not None:
            http.close()

    # Пауза от DeviantArt видна в отчёте всегда («…до 18:40 — читаю через API»), свой бюджет — если из-за
    # него что-то не прочитали.
    res.limited = da_access.status_line(conn, client is not None and api_ok)
    if not res.limited and (throttled is not None or left > 0):
        res.limited = _limit_line(conn, False, throttled)
    if left > 0 and res.limited and "следующий раз" not in res.limited:
        res.limited += f"; лент не прочитано: {left} — остальное в следующий раз"
    errors = [n for n in res.notes if not n.startswith("DeviantArt отключился")]
    if errors and not read and not res.limited and len(errors) == len(chosen):
        res.error = "ни один источник не ответил: " + "; ".join(errors[:3])
    if read or res.error:
        db.record_run(conn, "deviantart", res.error, now)
    return res


def _deep_ok(conn, kind: str, due: bool, rest: int) -> bool:
    """Листать ли ленту глубоко: пора (due) и в часе хватит на все страницы, метаданные, остальные ленты
    прохода и первые страницы следующих проверок (da_feeds.DEEP_KEEP)."""
    if not due:
        return False
    need = FEED_PAGES + (2 if kind == da_access.API else 0) + rest + da_feeds.DEEP_KEEP[kind]
    return da_access.room(conn, kind) >= need


def _limit_line(conn, api_connected: bool, exc: Exception | None) -> str:
    """Одна понятная строка: пауза от DeviantArt (до какого времени) или свой бюджет на этот час."""
    line = da_access.status_line(conn, api_connected)
    if line:
        return line
    block = getattr(exc, "block", None)
    if block is not None:
        return block.message
    for kind in (da_access.RSS, da_access.API):
        b = da_access.blocked(conn, kind)
        if b:
            return b.message
    return "DeviantArt просит притормозить — остальное в следующий раз"


def _save_entries(conn, cfg: Config, feed, entries: list[dict], me: str, res: SourceResult, now) -> None:
    for entry in entries:
        item = da.entry_to_item(entry)
        lot = classify_lot(entry["title"], entry["text"], entry["tags"])
        item.extra["multi"] = bool(getattr(lot, "multi", False))
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
        # Чужие лоты становятся «находками» (дайджест, уведомления), только если она покупает.
        if not cfg.notify.market_lots:
            item.matched = False
        item.match_kind = "lot" if item.matched else None
        res.seen += 1
        if db.upsert_item(conn, item, now):
            res.new += 1
            res.new_matched += int(item.matched)
    conn.commit()


def match_thread(cfg: Config, kind, title: str, snippet: str | None, activity_ts: float | None,
                 now: float, author: str | None = None) -> tuple[str | None, str]:
    """(lf | sale | None, причина). Закрытые, на паузе, «нашла» и её собственные треды — не находки."""
    if not activity_ts or now - activity_ts > cfg.toyhouse.active_days * 86400:
        return None, "давно без активности"
    closed, paused = th_match.thread_status(title, snippet)
    if closed or paused:
        return None, "закрыт"
    me = (cfg.posting.th_username or "").strip().lower()
    if me and (author or "").strip().lower() == me:
        return None, "её тред"
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


_PAY_ORDER = ("usd", "points", "art", "trades")


def _thread_item(cfg: Config, row: th.ThreadRow, snippet: str | None, now: float,
                 snippet_ts: float | None = None) -> db.Item:
    item = th.row_to_item(row)
    kind = classify_thread(row.title)
    item.wants_characters, item.sells_characters = kind.wants_characters, kind.sells_characters
    if kind.wants_characters:
        # Чем платят — и из названия, и из первого поста («paying via paypal», «budget 2k pts»).
        pays = th_match.payments(row.title, snippet)
        kind.pays = [p for p in _PAY_ORDER if p in pays] + sorted(pays - set(_PAY_ORDER))
        prof = th_match.lf_profile(row.title, snippet)
        item.extra["lf"] = th_match.profile_dict(prof)
    item.pays = ",".join(kind.pays) or None
    item.snippet = snippet
    item.extra["thread_key"] = th_match.thread_key(row.author, row.title)
    if snippet_ts:
        item.extra["snippet_ts"] = snippet_ts
    lot = classify_lot(row.title)
    item.price_text, item.price_usd, item.price_points = lot.price_text, lot.usd, lot.points
    item.match_kind, item.match_reason = match_thread(cfg, kind, row.title, snippet, row.activity_ts, now,
                                                      author=row.author)
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


# Чуть раньше прошлой удачной проверки: страница могла сдвинуться, пока её читали.
SINCE_OVERLAP_S = 15 * 60


def board_since(conn, cfg: Config, now: float) -> float:
    """С какого момента активность на досках нам ещё не видна: с прошлой удачной проверки
    (но не дальше active_days)."""
    floor = now - cfg.toyhouse.active_days * 86400
    last = db.last_run(conn, "toyhouse")
    last_ok = last["last_ok_ts"] if last else None
    return max(last_ok - SINCE_OVERLAP_S, floor) if last_ok else floor


def _page_tail_ts(rows: list[th.ThreadRow]) -> float | None:
    """Время активности последней строки страницы. Закреплённые треды стоят сверху и бывают старыми —
    поэтому смотрим на конец страницы, а не на минимум."""
    for row in reversed(rows):
        if row.activity_ts:
            return row.activity_ts
    return None


def _read_boards(reader, cfg: Config, now: float, res: SourceResult,
                 since_ts: float | None = None) -> list[th.ThreadRow]:
    """Доски по страницам. Минимум pages_per_board; дальше листаем, пока последняя строка страницы
    новее since_ts (на доске много нового с прошлой проверки), но не больше max_pages_per_board.
    Между страницами — те же человеческие паузы; проверку Cloudflare не обходим."""
    rows: dict[str, th.ThreadRow] = {}
    first = True
    low = max(1, cfg.toyhouse.pages_per_board)
    high = max(low, cfg.toyhouse.max_pages_per_board)
    for board in cfg.toyhouse.boards:
        for page in range(1, high + 1):
            if not first:
                th.human_pause()
            first = False
            try:
                got = reader.board(board, page, now)
            except th.ChallengeError as e:
                _challenged(res, e)
                return list(rows.values())
            except th.ToyhouseError as e:
                res.notes.append(str(e))
                break
            new = [r for r in got if r.thread_id not in rows]
            # Тред мог переехать на другую страницу между загрузками: берём первое появление.
            for row in got:
                rows.setdefault(row.thread_id, row)
            if not got or not new:
                break  # пустая страница или та же самая (номер страницы не сработал)
            if page < low:
                continue
            tail = _page_tail_ts(got)
            if since_ts is None or tail is None or tail < since_ts:
                break
    return list(rows.values())


SNIPPET_REREAD_S = 24 * 3600


def _old_snippet(conn, thread_id: str) -> tuple[str | None, float | None]:
    old = db.get_item(conn, f"th:{thread_id}")
    if not old:
        return None, None
    try:
        extra = json.loads(old["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        extra = {}
    ts = extra.get("snippet_ts") if isinstance(extra, dict) else None
    if old["snippet"] is not None and not ts:
        ts = old["first_seen_ts"]  # прочитано до того, как стали запоминать время
    return old["snippet"], ts


def _title_fit(cfg: Config, row: th.ThreadRow) -> int:
    """Лучший балл её персов по одному названию (до чтения поста): -1 — никто не подходит."""
    chars = [c for c in cfg.posting.characters or [] if isinstance(c, dict)]
    if not chars:
        return 0
    prof = th_match.lf_profile(row.title, None)
    accepts = th_match.accepts_of(chars)
    best = -1
    for ch in chars:
        fit = th_match.character_fit(prof, ch, accepts, row.title, None)
        if not fit.blockers:
            best = max(best, fit.score)
    return best


def _fetch_snippets(conn, reader, cfg: Config, rows: list[th.ThreadRow], now: float,
                    res: SourceResult) -> dict[str, tuple[str | None, float | None]]:
    """Первые посты живых LF-тредов: там написано, что именно ищут, бюджет и «не предлагать».

    Бюджет чтений на проход тот же (read_first_posts). Тратим его по порядку: где её перс подходит
    лучше всего по названию, где меньше откликов, что новее. Уже прочитанный пост старше суток
    перечитываем только у тредов, куда подходит её перс (автор мог поправить его на FOUND).
    Только чтение страниц, в базу здесь не пишем, чтобы не держать её заблокированной
    во время пауз и загрузок.
    """
    snippets: dict[str, tuple[str | None, float | None]] = {}
    queue = []
    for row in rows:
        snippet, ts = _old_snippet(conn, row.thread_id)
        snippets[row.thread_id] = (snippet, ts)
        fresh = row.activity_ts and now - row.activity_ts <= cfg.toyhouse.active_days * 86400
        if not (fresh and classify_thread(row.title).wants_characters):
            continue
        closed, paused = th_match.thread_status(row.title, None)
        if closed or paused:
            continue
        fit = _title_fit(cfg, row)
        unread = snippet is None
        if not unread and not (fit > 0 and (not ts or now - ts >= SNIPPET_REREAD_S)):
            continue
        queue.append((-fit, not unread, row.posts if row.posts is not None else 10 ** 6,
                      -(row.created_ts or 0), row))
    budget = 0 if res.needs_login else cfg.toyhouse.read_first_posts
    queue.sort(key=lambda q: q[:4])
    for *_, row in queue:
        if budget <= 0:
            break
        budget -= 1
        th.human_pause()
        try:
            # Пустая строка = «читали, текста нет», чтобы не перечитывать каждый раз.
            snippets[row.thread_id] = (reader.first_post(row.url) or "", now)
        except th.ChallengeError as e:
            _challenged(res, e)
            break
        except th.ToyhouseError as e:
            res.notes.append(str(e))
    return snippets


def _save_threads(conn, cfg: Config, rows: list[th.ThreadRow], snippets: dict, now: float,
                  res: SourceResult) -> None:
    for row in rows:
        snippet, snippet_ts = snippets.get(row.thread_id, (None, None))
        item = _thread_item(cfg, row, snippet, now, snippet_ts)
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
        since_ts = board_since(conn, cfg, now)
        with session as reader:
            rows = _read_boards(reader, cfg, now, res, since_ts)
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


def refresh_auctions(conn, cfg: Config, now: float | None = None, market_sample: int | None = None,
                     own_limit: int = 20, time_budget_s: float | None = None) -> tuple[list[str], list]:
    """Ставки на аукционах через API DeviantArt, если она его подключила. (строки отчёта, новые ставки).

    Не чаще раза в auctions.MIN_INTERVAL_S, после 429 — пауза, проход укладывается в time_budget_s.
    Если DA был подключён и отвалился — говорим об этом, а не молчим."""
    from . import auctions, posting

    ts = now or time.time()
    reason = auctions.gate(conn, ts)
    if reason:
        return [reason], []
    try:
        client = posting.da_client(cfg)
        connected = client.is_connected()
    except Exception:  # noqa: BLE001 — не подключён или нет client_id
        connected = False
    if not connected:
        return auctions.note_disconnected(conn, cfg, ts), []
    paused = da_access.cooldowns(conn).get(da_access.API)
    if paused:
        return [f"ставки: DeviantArt притормозил API до {da_access.until_text(paused['until_ts'])} — "
                "обновлю после, ничего делать не нужно"], []
    budget = auctions.TIME_BUDGET_S if time_budget_s is None else time_budget_s
    stats: dict = {}
    try:
        sample = auctions.MARKET_SAMPLE_PER_RUN if market_sample is None else market_sample
        with da_access.session(conn), da_access.task("ставки"):
            done, events = auctions.refresh_via_api(conn, cfg, client, now, market_sample=sample,
                                                    own_limit=own_limit, deadline=time.monotonic() + budget,
                                                    stats=stats)
    except Exception as e:  # noqa: BLE001
        log.warning("auction refresh failed: %s", e)
        db.record_run(conn, auctions.RUN_SOURCE, f"{type(e).__name__}: {e}"[:200], ts)
        return [f"ставки: не получилось обновить ({type(e).__name__})"], []
    lines = [f"ставки: обновлено аукционов {done}"] if done else []
    stopped = stats.get("stopped")
    if stopped == "not_connected":
        lines += auctions.note_disconnected(conn, cfg, ts, lost_now=True)
    elif stopped == "rate_limited":
        db.record_run(conn, auctions.RUN_SOURCE, "429: DeviantArt просит притормозить", ts)
        lines.append("ставки: " + (da_access.status_line(conn, True)
                                   or "DeviantArt просит притормозить — остальное обновлю позже"))
    elif stopped == "throttled":
        # Свой бюджет запросов на час кончился: не ошибка, просто продолжим на следующей проверке.
        db.record_run(conn, auctions.RUN_SOURCE, None, ts)
        lines.append(f"ставки: {stats.get('limit_note') or 'лимит запросов на этот час'}")
    elif stopped == "errors":
        db.record_run(conn, auctions.RUN_SOURCE, "API DeviantArt не отвечает", ts)
        lines.append("ставки: API DeviantArt не отвечает — попробую в следующий раз")
    else:
        db.record_run(conn, auctions.RUN_SOURCE, None, ts)
        if stopped == "time":
            lines.append("ставки: не все лоты успела перечитать — остальные в следующий раз")
    lines += [auctions.describe_event(e) for e in events]
    return lines, events


TH_AUCTIONS_SOURCE = "th_auctions"
TH_AUCTIONS_LIMIT = 5                  # тредов за проход
TH_AUCTIONS_PAGES = 3                  # страниц на тред за проход: первая (условия) и свежие
TH_AUCTIONS_INTERVAL_S = 30 * 60       # в фоне не чаще раза в полчаса
TH_AUCTIONS_MANUAL_INTERVAL_S = 10 * 60
TH_AUCTIONS_BUDGET_S = 4 * 60          # launchd запускает проверку раз в 15 минут — проход заметно короче
TH_CHROME_LOCK_WAIT_S = 30


def refresh_th_auctions(conn, cfg: Config, now: float | None = None, interactive: bool = False, browser=None,
                        min_interval_s: float | None = None, time_budget_s: float | None = None,
                        clock=time.monotonic) -> tuple[list[str], list]:
    """Перечитать её аукционы-треды Toyhouse (ta.to_refresh). (строки отчёта, новые ставки на ЕЁ аукционах).

    Режим chrome — только когда она попросила проверку в чате (interactive): её Chrome в фоне не трогаем.
    Режим bot — своё окно бота, открывается, только если есть что перечитать. Не чаще min_interval_s,
    не дольше time_budget_s; на проверке Cloudflare — стоп, ошибка одного треда — следующий. Не бросает."""
    from . import th_auctions as ta

    ts = now or time.time()
    chrome = cfg.toyhouse.mode == "chrome"
    if chrome and not interactive:
        return [], []
    interval = (TH_AUCTIONS_MANUAL_INTERVAL_S if interactive else TH_AUCTIONS_INTERVAL_S) \
        if min_interval_s is None else min_interval_s
    last = db.last_run(conn, TH_AUCTIONS_SOURCE)
    if last and last["last_run_ts"] and ts - last["last_run_ts"] < interval:
        return [], []
    try:
        entries = ta.to_refresh(conn, cfg.posting.th_username, ts, limit=TH_AUCTIONS_LIMIT)
    except Exception as e:  # noqa: BLE001
        log.warning("th auctions queue failed: %s", e)
        return [], []
    if not entries:
        return [], []
    deadline = clock() + (TH_AUCTIONS_BUDGET_S if time_budget_s is None else time_budget_s)
    lines: list[str] = []
    events: list = []
    error: str | None = None
    done = 0
    stop_on: tuple[type[BaseException], ...] = (th.ChallengeError,)

    def run(read_one) -> None:
        nonlocal done, error
        for i, entry in enumerate(entries):
            if clock() >= deadline:
                lines.append("аукционы Toyhouse: не все треды успела перечитать — остальные в следующий раз")
                break
            if i:
                th.human_pause()
            try:
                read = read_one(entry["url"])
            except stop_on as e:
                error = str(e)[:200]
                lines.append(f"аукционы Toyhouse: {e} — остальные треды в следующий раз")
                break
            except th.BrowserBusy:
                raise  # Chrome занят — проход не считается состоявшимся (ниже: время не записываем)
            except (th.ToyhouseError, ValueError) as e:
                lines.append(f"аукционы Toyhouse: «{(entry.get('title') or entry['thread_id'])[:60]}» "
                             f"не прочитался ({e})")
                continue
            if not read.posts:
                continue
            # Разбор — на момент чтения, не на время слота: посты датированы настоящим временем,
            # и свежие ставки с меткой позже слота иначе считались бы «из будущего».
            _, evs = ta.refresh(conn, read, entry, now=max(ts, time.time()))
            done += 1
            if entry.get("own"):  # чужие треды, за которыми она следит, — только данные, без уведомлений
                events.extend(evs)

    try:
        if chrome:
            from .her_chrome import ChromeSetupError

            stop_on = (th.ChallengeError, ChromeSetupError)
            run(lambda url: ta.run_th_thread(url, conn, max_pages=TH_AUCTIONS_PAGES, interactive=interactive,
                                             lock_wait_s=TH_CHROME_LOCK_WAIT_S))
        else:
            with _bot_reader(cfg, interactive, browser or th.open_browser) as reader:
                run(lambda url: ta.read_thread_bot(reader, url, conn, max_pages=TH_AUCTIONS_PAGES))
    except th.BrowserBusy as e:
        lines.append(f"аукционы Toyhouse: {e} — перечитаю в следующий раз")
        lines += [ta.describe_event(ev) for ev in events]
        return lines, events  # время прохода не записываем: попробуем на следующей проверке
    except th.BrowserClosed:
        error = "окно Chrome закрыли"
        lines.append("аукционы Toyhouse: окно Chrome закрыли во время чтения")
    except Exception as e:  # noqa: BLE001 — Playwright и osascript бросают разное
        log.warning("th auctions refresh failed: %s: %s", type(e).__name__, e)
        error = f"{type(e).__name__}: {e}"[:200]
        lines.append(f"аукционы Toyhouse: не получилось обновить ({type(e).__name__})")
    with contextlib.suppress(Exception):
        db.record_run(conn, TH_AUCTIONS_SOURCE, error, ts)
    if done:
        lines.insert(0, f"аукционы Toyhouse: перечитала тредов {done}")
    lines += [ta.describe_event(e) for e in events]
    return lines, events


ENRICH_SOURCE = "enrich"
ENRICH_INTERVAL_S = 3600
UNPAID_NUDGE_SOURCE = "unpaid_nudge"
UNPAID_NUDGE_INTERVAL_S = 86400
NOTIFY_TITLE = "mcp_devian"


def _th_sales(conn, ts: float) -> list[str]:
    from . import th_auctions as ta

    new = ta.sync_sales(conn, ts)
    return [f"продажи: записала победителей аукционов Toyhouse — {len(new)}"] if new else []


def _deadline_notices(conn, cfg: Config, ts: float) -> None:
    """Её аукционы DA: «закончится через …» и «закончился, но не закрыт» — каждый один раз."""
    from . import selling

    tz = cfg.posting.timezone
    soon = selling.ending_soon(conn, ts, within_h=3, tz=tz, unseen_only=True, limit=3)
    ended = selling.ended_unclosed(conn, ts, tz=tz, unseen_only=True, limit=3)
    from .digest import notify_macos

    for e in [*soon, *ended]:
        notify_macos(NOTIFY_TITLE, e["text"][:220])


def _unpaid_nudge(conn, cfg: Config, ts: float) -> None:
    """Раз в сутки — одно уведомление, сколько продаж ждут оплаты. Черновиков сама не готовит."""
    from . import sales, selling
    from .digest import notify_macos

    last = db.last_run(conn, UNPAID_NUDGE_SOURCE)
    if last and last["last_run_ts"] and ts - last["last_run_ts"] < UNPAID_NUDGE_INTERVAL_S:
        return
    # Все неоплаченные, а не только те, кому можно напомнить прямо сейчас: Toyhouse, непроверенные суммы
    # и недавно напомненные тоже не оплачены — число не занижаем.
    n = len(sales.unpaid(conn, selling.REMIND_AFTER_DAYS, ts))
    db.record_run(conn, UNPAID_NUDGE_SOURCE, None, ts)
    if n:
        notify_macos(NOTIFY_TITLE, f"Не оплачено: {n} — спроси Claude")


def _update_notice() -> None:
    from . import updates
    from .digest import notify_macos

    note = updates.notice_to_announce(short=True)
    if note:
        notify_macos(NOTIFY_TITLE, note[:220])


def seller_pass(conn, cfg: Config, now: float | None = None, enrich: bool = True) -> list[str]:
    """После ставок: записать победителей её аукционов (DA и Toyhouse) в продажи; уведомления macOS —
    аукцион скоро закончится / закончился и не закрыт, раз в сутки «не оплачено: N», новая версия;
    раз в час — просмотры/фавы лотов через API (если DA подключён). Ничего не бросает."""
    from . import enrich as enrich_mod
    from . import posting, sales

    ts = now or time.time()
    lines = []
    try:
        new = sales.sync_from_auctions(conn, ts)
        if new:
            lines.append(f"продажи: записала победителей аукционов — {len(new)}")
    except Exception as e:  # noqa: BLE001
        log.warning("sales sync failed: %s", e)
    # Каждое дополнение — отдельно: сбой одного не мешает остальным и самой проверке.
    try:
        lines += _th_sales(conn, ts)
    except Exception as e:  # noqa: BLE001
        log.warning("toyhouse sales sync failed: %s", e)
    if cfg.notify.macos:
        for hook, args in ((_deadline_notices, (conn, cfg, ts)), (_unpaid_nudge, (conn, cfg, ts)),
                           (_update_notice, ())):
            try:
                hook(*args)
            except Exception as e:  # noqa: BLE001
                log.warning("%s failed: %s", hook.__name__, e)
    if not enrich:
        return lines
    last = db.last_run(conn, ENRICH_SOURCE)
    if last and last["last_run_ts"] and ts - last["last_run_ts"] < ENRICH_INTERVAL_S:
        return lines
    try:
        client = posting.da_client(cfg)
        if not client.is_connected():
            return lines
        # Статистика — необязательная работа: низкий приоритет в общем бюджете запросов.
        with da_access.session(conn), da_access.task("статистика", low=True):
            done = enrich_mod.enrich_stats(conn, client, ts)
    except Exception as e:  # noqa: BLE001 — нет client_id, сеть и т.п.
        log.warning("enrich failed: %s", e)
        db.record_run(conn, ENRICH_SOURCE, f"{type(e).__name__}: {e}"[:200], ts)
        return lines
    db.record_run(conn, ENRICH_SOURCE, done["stopped"], ts)
    if done["snapshots"]:
        lines.append(f"статистика: просмотры и фавы для {done['snapshots']} лотов")
    if done["stopped"]:
        lines.append(f"статистика: {done['stopped']}")
    return lines


BUYERS_BG_SOURCE = "buyers_bg"
BUYERS_BG_INTERVAL_S = 3600            # фавнувшие, LF-теги DA, ответы «stop» — не чаще раза в час
TH_LF_ALERT_SOURCE = "th_lf_alerts"
GOAL_NUDGE_SOURCE = "goal_nudge"
GOAL_NUDGE_INTERVAL_S = 7 * 86400      # цель месяца — не чаще раза в неделю
COMMISSION_NOTIFY_MAX = 3              # напоминаний по слотам за один проход фона
FAVER_CALLS = 3
LF_CALLS = 2
OFFER_WATCH_WORKS = 2


def _bg(name: str, fn, default=None):
    """Каждая фоновая добавка — отдельно: её сбой только в лог, остальное и сама проверка идут дальше."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.warning("%s failed: %s: %s", name, type(e).__name__, e)
        return default


class _NetBreaker:
    """Клиент DA для фонового прохода: после первой сетевой ошибки (DeviantArt завис, таймаут) остальные
    запросы прохода не идут в сеть, а сразу получают ту же ошибку. Иначе каждая часть ждала бы свои 60 с,
    и проход вылезал бы за 15-минутный слот, задерживая её запланированные посты."""
    NET = ("network", "network_unsent")

    def __init__(self, client) -> None:
        self._client = client
        self.down: Exception | None = None

    def __getattr__(self, name: str):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args, **kw):
            if self.down is not None:
                raise self.down
            try:
                return attr(*args, **kw)
            except da_api.DAApiError as e:
                if getattr(e, "code", None) in self.NET:
                    self.down = e
                raise

        return call


def growth_pass(conn, cfg: Config, now: float | None = None, client=None, palette_fetch=None,
                palette_sleep=None) -> list[str]:
    """Фон 0.5 после ставок и рынка: тёплые покупатели (только база), история рынка, похожие продавцы,
    кто фавнул её лоты, LF-посты DA, ответы «stop» под её предложениями, палитры превью, LF Toyhouse.

    Только чтение и уведомления: ни черновиков, ни сообщений. DeviantArt — в общем бюджете (низкий
    приоритет), каждая часть в своих пределах; при паузе DeviantArt палитры не качаем. Не бросает."""
    from . import backfill, buyers, da_market, digest, leads, outreach, palette, similar

    ts = now or time.time()
    lines: list[str] = []
    d, me = cfg.deviantart, cfg.deviantart.my_username
    _bg("buyers.sync", lambda: buyers.sync(conn, ts, me, cfg.posting.th_username))
    if client is None:
        client = _api_client(cfg)
    breaker = _NetBreaker(client) if client is not None else None
    client = breaker
    if client is not None:
        with da_access.session(conn):
            lines += _bg("backfill", lambda: backfill.step(conn, client, ts, days=d.backfill_days,
                                                           max_api=d.backfill_api_per_run, cfg=cfg, quiet=True), [])
            if d.similar_watch:
                lines += _bg("similar", lambda: similar.step(conn, client, ts, manual=d.similar_artists, cfg=cfg),
                             [])
            last = db.last_run(conn, BUYERS_BG_SOURCE)
            if me and not (last and last["last_run_ts"] and ts - last["last_run_ts"] < BUYERS_BG_INTERVAL_S):
                _bg("favers", lambda: buyers.refresh_favers(conn, client, ts, FAVER_CALLS, me))
                found = _bg("da_lf", lambda: leads.da_lf_scan(conn, client, ts, max_calls=LF_CALLS,
                                                              characters=cfg.posting.characters,
                                                              my_username=me), {})
                if found and found.get("new"):
                    lines.append(f"покупатели: новых LF-постов на DA под её персов — {found['new']} (buyers:leads)")
                watched = _bg("offer_replies", lambda: outreach.watch_offer_replies(
                    conn, client, ts, owner=me, max_works=OFFER_WATCH_WORKS), {})
                if watched and watched.get("opted_out"):
                    lines.append(f"покупатели: попросили больше не отмечать — {watched['opted_out']}, запомнила")
                _bg("buyers_bg run", lambda: db.record_run(conn, BUYERS_BG_SOURCE, None, ts))
    if breaker is not None and breaker.down is not None:
        lines.append("фон: DeviantArt не отвечает — остальное чтение DA и палитры отложила до следующей проверки")
    elif d.palette_downloads_per_hour > 0 and not _bg("cooldowns", lambda: da_access.cooldowns(conn, ts), {"?": 1}):
        def colors() -> None:
            niche = da_market.niche_terms(conn, cfg)
            if palette_fetch is not None:
                palette.step(conn, palette_fetch, ts, per_hour=d.palette_downloads_per_hour, niche=niche,
                             sleep=palette_sleep)
                return
            with palette.client() as http:
                palette.step(conn, palette.make_fetch(http), ts, per_hour=d.palette_downloads_per_hour,
                             niche=niche, sleep=palette_sleep)

        _bg("palette", colors)
    if cfg.toyhouse.background_lf:  # по умолчанию выключено: включает она (toyhouse_background_lf)
        def th_lf() -> None:
            last = db.last_run(conn, TH_LF_ALERT_SOURCE)
            since = last["last_run_ts"] if last and last["last_run_ts"] else None
            found = leads.th_alerts(conn, cfg, ts, since_ts=since)
            db.record_run(conn, TH_LF_ALERT_SOURCE, None, ts)
            if found:
                lines.extend(found)
                digest.notify_lines(cfg, "Нашла покупателя на Toyhouse", found)

        _bg("th_lf_alerts", th_lf)
    lines += _bg("money", lambda: money_pass(conn, cfg, ts), [])
    return lines


def money_pass(conn, cfg: Config, now: float | None = None) -> list[str]:
    """Её деньги в фоне — только ей, клиентам ничего не уходит, черновиков нет:
    - напоминания по её слотам YCH/коммишенов (commission_reminders, по умолчанию да): каждое — один раз,
      не больше COMMISSION_NOTIFY_MAX за проход, остальные — в следующий раз;
    - цель месяца (goal_nudges, по умолчанию нет): раз в неделю, только если цель задана."""
    from . import commissions, digest, earnings, evidence

    ts = now or time.time()
    p = cfg.posting
    lines: list[str] = []
    if p.commission_reminders:
        commissions.ensure_tables(conn)
        st = commissions.settings_from(cfg)
        fresh = commissions.pending_reminders(conn, ts, mark=False, deposit_days=st.deposit_days,
                                              deadline_warn_days=st.deadline_warn_days, final_days=st.final_days,
                                              deliver_days=st.deliver_days, tz=st.tz)[:COMMISSION_NOTIFY_MAX]
        if fresh:
            texts = [f"слоты: {r['text']}" for r in fresh]
            shown = digest.notify_lines(cfg, "YCH и коммишены", [r["text"] for r in fresh])
            # Уведомление не дошло (macOS/Telegram сломались) — не отмечаем: напомню в следующий раз.
            # Уведомления выключены совсем — она видит напоминания в чате (sales:ych), отмечаем как раньше.
            if shown or not (cfg.notify.macos or cfg.notify.telegram):
                commissions.mark_reminded(conn, fresh, ts)
            lines += texts
    if p.goal_nudges:
        last = db.last_run(conn, GOAL_NUDGE_SOURCE)
        if not (last and last["last_run_ts"] and ts - last["last_run_ts"] < GOAL_NUDGE_INTERVAL_S):
            g = earnings.goal_status(conn, ts, p.timezone or earnings.DEFAULT_TZ)
            db.record_run(conn, GOAL_NUDGE_SOURCE, None, ts)
            if g["target"]:
                left = (f"осталось {earnings._rub(g['remaining'])} за {g['days_left']} дн" if g["remaining"] > 0
                        else "цель достигнута")
                text = (f"Цель месяца: дошло {earnings._rub(g['got'])} из {earnings._rub(g['target'])} "
                        f"({evidence.pct(g['got'] / g['target'])}, {g['got_n']} оплач. с суммой в ₽); {left}. "
                        "Подробнее — спроси в чате про цель месяца.")
                digest.notify_lines(cfg, "Цель месяца", [text])
                lines.append(text)
    return lines


def format_results(results: list[SourceResult]) -> list[str]:
    lines = []
    for r in results:
        if r.skipped and r.limited and r.skipped == r.limited:
            lines.append(f"{r.source}: {r.limited}")
        elif r.skipped:
            lines.append(f"{r.source}: пропуск — {r.skipped}")
        elif r.error:
            lines.append(f"{r.source}: ошибка — {r.error}")
        else:
            lines.append(f"{r.source}: просмотрено {r.seen}, новых {r.new}, подходит из новых {r.new_matched}")
        if r.limited and r.skipped != r.limited:
            lines.append(f"  ! {r.limited}")
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
        # Плановая проверка — несколько лент по кругу; проверка по её просьбе — больше, но в том же бюджете.
        "deviantart": lambda: _guarded(check_deviantart, "deviantart", conn, cfg, now,
                                       max_feeds=da_feeds.MANUAL_FEEDS_PER_RUN if interactive
                                       else da_feeds.FEEDS_PER_RUN),
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
