"""История рынка с первого дня: после подключения DA дочитываем закрытые аукционы за 60–90 дней назад.

Зачем: прогнозу нужны итоги аукционов её ниши, а мониторинг видит только то, что выложено после
подключения. Поэтому по очереди листаем (официальный API, только чтение) самые живые группы её ниши
(da_market.top_niche_groups) и её собственную галерею — от новых к старым, пока не дойдём до
now − days. По каждому закрытому аукциону читаем комментарии и записываем итог тем же разбором,
что и обычные ставки (auctions.analyse/save).

Бережно: всё идёт с низким приоритетом в общем бюджете da_access (живые проверки важнее), не больше
max_api запросов за проход и daily_cap в сутки. Пауза DeviantArt, свой бюджет, нет прав —
проход сразу останавливается, курсор остаётся: следующий проход продолжит с той же страницы.

Счёт запросов — настоящий: сколько записал da_access за вызов (с ответвлениями комментариев, листанием
папок, делением пачки метаданных). Подставной клиент без da_access — по оценке. Каждый разобранный лот
помнится в backfill_seen: повторное чтение страницы не перечитывает его комментарии и не считает дважды.
Порядок выдачи /gallery не документирован; считаем «сначала новые». Цель закончена, когда вся страница
старше окна или две страницы подряд в основном старше (один старый лот, заново выложенный в группу, —
не край).
"""

import contextlib
import json
import logging
import sqlite3
import time
from datetime import UTC, datetime

from . import auctions, da_access, da_api, da_feeds, db
from . import deviantart as da
from .classify import classify_lot
from .forecast import outcome_known
from .selling import DEFAULT_TZ, parse_end

log = logging.getLogger(__name__)

__all__ = ["ensure", "enqueue", "step", "progress", "DEFAULT_DAYS", "DAILY_CAP"]

DAY = 86400
DEFAULT_DAYS = 75
MIN_DAYS, MAX_DAYS = 60, 90
DAILY_CAP = 300
MAX_API = 40
CHAT_MAX_API = 15             # из чата: ~15 × 1,5 с пауз — ответ укладывается в ~30 с; остальное — в фоне
PAGE_LIMIT = 24
PAUSE_S = 1.5
CLOSED_AGE_DAYS = 14          # старше — итог считаем окончательным, даже если в названии ещё [OPEN]
END_GRACE_S = DAY             # срок из описания прошёл больше суток назад — аукцион закончился
ZERO_BY_STATS_DAYS = 7        # 0 комментариев в этом возрасте — ставок не было, комментарии не читаем
OPEN_RECHECK_S = DAY          # ещё открытый лот на недочитанной странице снова смотрим не раньше
MIN_ROOM = 3                  # страница + метаданные + комментарии одного лота: меньше — проход не начинаем
STALE_PAGES = 2               # столько страниц подряд «в основном старше окна» — край
REENQUEUE_S = 6 * 3600        # группы её ниши дозаполняем не чаще
MAX_COMMENTS = 200
METADATA_CHUNK = 50
AUCTION_TYPES = ("auction", "bta")
VIA = "backfill"

SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_state (
    key TEXT PRIMARY KEY,          -- group:<группа>/<номер папки или ?> | own:<ник>
    folderid TEXT,                 -- UUID папки в API (у её галереи — NULL: /gallery/all)
    offset INTEGER NOT NULL DEFAULT 0,
    oldest_ts REAL,                -- до какой даты публикации дочитали
    lots INTEGER NOT NULL DEFAULT 0,       -- закрытых аукционов записано (каждый один раз)
    bids_read INTEGER NOT NULL DEFAULT 0,  -- из них с прочитанными комментариями
    done INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_ts REAL,
    days INTEGER,                  -- на сколько дней назад читаем
    name TEXT,                     -- название папки (для поиска её UUID)
    enqueued_ts REAL,
    edge_offset INTEGER,           -- первая страница, где встретились лоты старше окна (для большего окна)
    stale_pages INTEGER NOT NULL DEFAULT 0  -- страниц подряд в основном старше окна
);
CREATE TABLE IF NOT EXISTS backfill_daily (
    day TEXT PRIMARY KEY,          -- дата UTC
    calls REAL NOT NULL DEFAULT 0  -- запросов API за сутки
);
CREATE TABLE IF NOT EXISTS backfill_seen (
    item_id TEXT PRIMARY KEY,      -- da:<номер>
    key TEXT,
    kind TEXT NOT NULL,            -- lot (итог записан) | skip (не аукцион) | open (ещё шёл)
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS backfill_meta (
    k TEXT PRIMARY KEY,
    v REAL
);
"""
_NEW_COLUMNS = (("edge_offset", "INTEGER"), ("stale_pages", "INTEGER NOT NULL DEFAULT 0"))


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(backfill_state)")}
    for col, decl in _NEW_COLUMNS:
        if col not in have:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE backfill_state ADD COLUMN {col} {decl}")


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d.%m")


def _clamp_days(days: int | None) -> int:
    try:
        d = int(days or DEFAULT_DAYS)
    except (TypeError, ValueError):
        d = DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, d))


def _group_name(value: str) -> str:
    """«https://www.deviantart.com/group/gallery/123» или «group» → «group»."""
    v = (value or "").strip().rstrip("/")
    return v.split("deviantart.com/")[-1].split("/")[0].lstrip("@").lower()


def _used_today(conn, now: float) -> float:
    row = conn.execute("SELECT calls FROM backfill_daily WHERE day = ?", (_day(now),)).fetchone()
    return float(row["calls"]) if row else 0.0


def _spend(conn, now: float, cost: float) -> None:
    conn.execute("INSERT INTO backfill_daily (day, calls) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET "
                 "calls = backfill_daily.calls + excluded.calls", (_day(now), cost))


# --- очередь ---

def enqueue(conn, groups: list[str] | None, own_username: str, days: int = DEFAULT_DAYS, now: float | None = None,
            cfg=None, limit: int | None = None) -> list[str]:
    """Поставить в очередь группы (None — сами: живые группы её ниши) и её галерею. Повтор не сбрасывает
    курсоры; больший days снова открывает цель с края прежнего окна. → ключи, которые добавились."""
    from . import da_market
    from .config import Config

    now = now or time.time()
    ensure(conn)
    days = _clamp_days(days)
    cfg = cfg or Config()
    limit = int(limit if limit is not None else getattr(cfg.deviantart, "backfill_groups", 6) or 6)
    limit = max(1, min(10, limit))
    ranked = da_market.top_niche_groups(conn, cfg, now, limit=10 if groups is not None else limit)
    if groups is None:
        targets = ranked
    else:
        by_grp = {r["grp"]: r for r in ranked}
        names = list(dict.fromkeys(n for n in (_group_name(g) for g in groups) if n))[:limit]
        targets = [by_grp.get(n) or {"grp": n, "folder": None, "uuid": None, "name": None} for n in names]
    existing = {r["key"]: r for r in conn.execute("SELECT key, days, done FROM backfill_state")}
    added: list[str] = []
    rows: list[tuple] = []
    for t in targets:
        if any(k.startswith(f"group:{t['grp']}/") for k in existing):
            continue
        key = f"group:{t['grp']}/{t['folder'] or '?'}"
        rows.append((key, t.get("uuid"), t.get("name")))
    me = (own_username or "").strip().lstrip("@").lower()
    if me:
        rows.append((f"own:{me}", None, None))
    for key, folderid, name in rows:
        if key in existing:
            continue
        conn.execute("INSERT OR IGNORE INTO backfill_state (key, folderid, name, days, enqueued_ts, updated_ts) "
                     "VALUES (?, ?, ?, ?, ?, NULL)", (key, folderid, name, days, now))
        added.append(key)
    # Большее окно: дочитываем с первой страницы, где прежнее окно кончилось (лоты между старым и новым
    # краем на ней ещё не разобраны); уже разобранные лоты backfill_seen не перечитывает.
    conn.execute('UPDATE backfill_state SET "offset" = COALESCE(edge_offset, "offset"), edge_offset = NULL, '
                 "stale_pages = 0, done = CASE WHEN last_error IS NULL THEN 0 ELSE done END "
                 "WHERE COALESCE(days, 0) < ?", (days,))
    conn.execute("UPDATE backfill_state SET days = ? WHERE COALESCE(days, 0) < ?", (days, days))
    conn.execute("INSERT INTO backfill_meta (k, v) VALUES ('enqueue_ts', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                 (now,))
    conn.commit()
    return added


def _maybe_enqueue(conn, cfg, now: float, days: int, my_username: str) -> None:
    """При подключении DA карточек групп ещё нет — в очереди одна её галерея. Дозаполняем группами ниши,
    когда они появятся (не чаще раза в REENQUEUE_S; запросов к DA это не стоит)."""
    want = max(1, min(10, int(getattr(cfg.deviantart, "backfill_groups", 6) or 6)))
    have = conn.execute("SELECT COUNT(*) FROM backfill_state WHERE key LIKE 'group:%'").fetchone()[0]
    row = conn.execute("SELECT v FROM backfill_meta WHERE k = 'enqueue_ts'").fetchone()
    if have >= want or (row and row["v"] is not None and now - float(row["v"]) < REENQUEUE_S):
        return
    enqueue(conn, None, my_username, days, now, cfg=cfg)


# --- проход ---

class _MetaMissing(Exception):
    """Метаданные (описания) страницы не пришли: классифицировать лоты по пустому описанию нельзя —
    решение «не аукцион» стало бы вечным. Страницу повторяем в следующий проход."""


class _Stop(Exception):
    """Проход закончен: бюджет прохода/суток."""


class _Run:
    def __init__(self, conn, client, now: float, days: int, room: float, sleep, my_username: str, tz: str):
        self.conn, self.client, self.now, self.days = conn, client, now, days
        self.room, self.sleep, self.me, self.tz = room, sleep, my_username, tz
        self.spent = 0.0
        self.lots = 0
        self.bids = 0
        self.free: set[str] = set()
        self.key = ""

    def need(self, est: float, once: str = "") -> None:
        """Перед запросом: хватит ли бюджета прохода. Не хватает — стоп, курсор остаётся. Первые за проход
        метаданные и первое чтение комментариев (once) пускаем, даже если оценка больше остатка, — иначе
        дорогой лот застрял бы навсегда; перерасход — не больше одного лота."""
        if self.spent + est <= self.room:
            return
        if once and once not in self.free:
            self.free.add(once)
            return
        raise _Stop()

    def call(self, est: float, fn, *args, pause: bool = True, once: str = "", **kw):
        """Вызов клиента с настоящим счётом: сколько записал da_access (подставной клиент — оценка est)."""
        self.need(est, once)
        if once:
            self.free.add(once)
        # Записи этого лота (_store, _mark) — до запроса: блокировку базы во время запроса не держим.
        self.conn.commit()
        guard = da_access.current()
        before = sum(guard.spent.values()) if guard is not None else 0.0
        ok = False
        try:
            out = fn(*args, **kw)
            ok = True
            return out
        finally:
            real = (sum(guard.spent.values()) - before) if guard is not None else 0.0
            cost = real if real > 0 else (est if ok else 0.0)
            self.spent += cost
            _spend(self.conn, self.now, cost)
            # Запись не держим открытой во время паузы и следующего запроса: иначе чат (черновики, «го»,
            # продажи) ждёт блокировку базы и падает с «database is locked».
            self.conn.commit()
            if pause:
                self.sleep(PAUSE_S)


def _page(run: _Run, path: str, params: dict, place: str) -> tuple[list[dict], bool, int]:
    """→ (лоты после фильтра, есть ли ещё, следующий offset из ответа)."""
    body = run.call(1, run.client._request, "GET", path, params=params) or {}
    raw = [d for d in body.get("results") or [] if isinstance(d, dict)]
    entries = [e for e in (da_feeds.api_entry(d, place) for d in raw) if e]
    offset = int(params.get("offset") or 0)
    nxt = body.get("next_offset")
    next_offset = int(nxt) if isinstance(nxt, int) and not isinstance(nxt, bool) and nxt > offset else offset + len(raw)
    return entries, bool(body.get("has_more")), next_offset


def _known_outcome(conn, item_id: str) -> bool:
    r = conn.execute("SELECT a.ended, a.extra AS a_extra, a.updated_ts AS a_updated, i.status, i.closed_ts, "
                     "i.published_ts, i.first_seen_ts, i.updated_ts FROM auctions a JOIN items i "
                     "ON i.id = a.deviation_id WHERE a.deviation_id = ?", (item_id,)).fetchone()
    if not r:
        return False
    try:
        a_extra = json.loads(r["a_extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        a_extra = {}
    return outcome_known(ended=r["ended"], status=r["status"], closed_ts=r["closed_ts"],
                         a_extra=a_extra if isinstance(a_extra, dict) else {}, a_updated=r["a_updated"],
                         published=r["published_ts"] or r["first_seen_ts"], item_updated=r["updated_ts"])


def _seen(conn, ids: list[str], now: float) -> set[str]:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {r["item_id"] for r in conn.execute(
        f"SELECT item_id FROM backfill_seen WHERE item_id IN ({marks}) AND (kind != 'open' OR ts > ?)",
        [*ids, now - OPEN_RECHECK_S])}


def _mark(conn, item_id: str, key: str, kind: str, now: float) -> bool:
    """Запомнить решение по лоту. → True, если итог лота записан впервые (для счёта)."""
    old = conn.execute("SELECT kind FROM backfill_seen WHERE item_id = ?", (item_id,)).fetchone()
    conn.execute("INSERT INTO backfill_seen (item_id, key, kind, ts) VALUES (?, ?, ?, ?) ON CONFLICT(item_id) "
                 "DO UPDATE SET key = excluded.key, kind = excluded.kind, ts = excluded.ts", (item_id, key, kind, now))
    return kind == "lot" and (old is None or old["kind"] != "lot")


def _closed(lot, entry: dict, now: float, tz: str = DEFAULT_TZ) -> tuple[bool, float | None]:
    """Аукцион закончился: закрыт по названию, старше 14 дней или срок из описания прошёл больше суток назад.
    → (закончился, срок из описания, если именно по нему)."""
    if lot.status == "closed":
        return True, None
    ts = entry["published_ts"]
    if ts is not None and now - ts >= CLOSED_AGE_DAYS * DAY:
        return True, None
    end = parse_end(f"{entry['title']}\n{entry['text']}", ts, None, tz=tz or DEFAULT_TZ, now=now)
    if end.end_ts is not None and end.end_ts + END_GRACE_S < now:
        return True, float(end.end_ts)
    return False, None


def _store(run: _Run, entry: dict, lot, own: bool, place: str):
    item = da.entry_to_item(entry)
    item.place = place
    item.lot_type, item.status, item.price_text = lot.lot_type, lot.status, lot.price_text
    item.price_usd, item.price_points = lot.usd, lot.points
    item.own = own
    item.extra["multi"] = bool(lot.multi)
    item.extra["via"] = [VIA]
    old = db.get_item(run.conn, item.id)
    if old:  # что о лоте решила лента (находка, причина), не сбрасываем
        item.matched, item.match_kind, item.match_reason = bool(old["matched"]), old["match_kind"], old["match_reason"]
    if own:
        item.matched, item.match_kind = False, None
    db.upsert_item(run.conn, item, run.now)
    if entry.get("comments") is not None or entry.get("favourites") is not None:
        run.conn.execute("INSERT OR IGNORE INTO deviation_stats (deviation_id, ts, views, favourites, comments) "
                         "VALUES (?, ?, NULL, ?, ?)", (item.id, run.now, entry.get("favourites"), entry.get("comments")))
    return db.get_item(run.conn, item.id)


def _comments_cost(n: int | None) -> float:
    """Оценка до чтения (настоящий счёт — после): 1 + по одному на каждые 50 комментариев сверх первых."""
    n = max(0, int(n or 0))
    return 1 + max(0, min(n, MAX_COMMENTS * 2) - 1) // 50


def _process_page(run: _Run, entries: list[dict], own: bool, place: str, cutoff: float
                  ) -> tuple[int, int, float | None]:
    """Лоты страницы → итоги аукционов. → (лотов с датой, из них старше окна, самая старая дата в окне)."""
    dated = old = 0
    fresh: list[dict] = []
    for e in entries:
        ts = e["published_ts"]
        if ts is None:
            continue
        dated += 1
        if ts < cutoff:
            old += 1
            continue
        fresh.append(e)
    oldest = min((e["published_ts"] for e in fresh), default=None)
    seen = _seen(run.conn, [f"da:{e['deviation_id']}" for e in fresh], run.now)
    todo = [e for e in fresh if f"da:{e['deviation_id']}" not in seen
            and not _known_outcome(run.conn, f"da:{e['deviation_id']}")]
    # Описание нужно для цены и срока: полное (с desc_ts) — из базы, остальное — запросом метаданных.
    need = []
    for e in todo:
        row = db.get_item(run.conn, f"da:{e['deviation_id']}")
        extra = da_feeds._old_extra(row) if row else {}
        if row and extra.get("desc_ts") and (extra.get("desc") or row["snippet"]):
            e["text"] = str(extra.get("desc") or row["snippet"] or "")
            e["tags"] = e["tags"] or [str(t) for t in extra.get("tags") or []]
        else:
            need.append(e)
    if need:
        run.call(-(-len(need) // METADATA_CHUNK), da_feeds.fill_text, run.conn, run.client, need, run.now,
                 sleep=run.sleep, pause=False, once="meta")
        if not any(e.get("desc_ts") for e in need):
            raise _MetaMissing()   # сеть/5xx/странный ответ: ничего не помечаем, курсор стоит
    for e in todo:
        iid = f"da:{e['deviation_id']}"
        lot = classify_lot(e["title"], e["text"], e["tags"])
        if lot.lot_type not in AUCTION_TYPES:
            _mark(run.conn, iid, run.key, "skip", run.now)
            continue
        closed, end_ts = _closed(lot, e, run.now, run.tz)
        if not closed:
            _mark(run.conn, iid, run.key, "open", run.now)
            continue
        is_own = own or bool(run.me and (e["author"] or "").lower() == run.me)
        row = _store(run, e, lot, is_own, place)
        age = run.now - e["published_ts"]
        if e.get("comments") == 0 and age >= ZERO_BY_STATS_DAYS * DAY:
            auctions.save(run.conn, row, auctions.analyse(row, []), 0, run.now, zero_by_stats=True)
            read = 0
        else:
            comments = run.call(_comments_cost(e.get("comments")), run.client.deviation_comments, e["uuid"],
                                max_comments=MAX_COMMENTS, once="comments")
            auctions.save(run.conn, row, auctions.analyse(row, comments), len(comments), run.now)
            read = 1
        if end_ts is not None:
            # Закончился по сроку из описания (+сутки): итог окончательный и для прогноза (outcome_known).
            run.conn.execute("UPDATE auctions SET ended = 1, extra = json_set(COALESCE(extra, '{}'), '$.end_ts', ?, "
                             "'$.ended_by', 'date') WHERE deviation_id = ?", (end_ts, iid))
        if _mark(run.conn, iid, run.key, "lot", run.now):
            run.lots += 1
            run.bids += read
            run.conn.execute("UPDATE backfill_state SET lots = lots + 1, bids_read = bids_read + ?, "
                             "oldest_ts = MIN(COALESCE(oldest_ts, ?), ?) WHERE key = ?",
                             (read, e["published_ts"], e["published_ts"], run.key))
        run.conn.commit()
    run.conn.commit()
    return dated, old, oldest


def _resolve_folder(run: _Run, t) -> str | None:
    """UUID папки группы: из da_folders (страница группы) или по списку папок через API."""
    from .da_market import LOT_FOLDER, NOT_LOT_FOLDER

    grp, _, number = t["key"].removeprefix("group:").partition("/")
    if number and number != "?":
        row = run.conn.execute("SELECT uuid, name FROM da_folders WHERE grp = ? AND folder_id = ?",
                               (grp, number)).fetchone()
        if row and row["uuid"]:
            return str(row["uuid"])
    want = (t["name"] or "").strip().lower()
    folders = run.call(1, run.client.gallery_folders, grp, pause=False)
    pick = [f for f in folders if want and (f.get("name") or "").strip().lower() == want]
    pick = pick or [f for f in folders if LOT_FOLDER.search(f.get("name") or "")
                    and not NOT_LOT_FOLDER.search(f.get("name") or "")]
    if not pick or not pick[0].get("folder_id"):
        return None
    return str(pick[0]["folder_id"])


def _targets(conn) -> list:
    return conn.execute("SELECT * FROM backfill_state WHERE done = 0 "
                        "ORDER BY key LIKE 'own:%' DESC, COALESCE(updated_ts, 0), key").fetchall()


def _save_cursor(conn, key: str, now: float, *, offset: int | None = None, oldest: float | None = None,
                 done: bool | None = None, error: str | None = None, folderid: str | None = None,
                 stale: int | None = None, edge: int | None = None) -> None:
    conn.execute(
        'UPDATE backfill_state SET "offset" = COALESCE(?, "offset"), oldest_ts = CASE WHEN ? IS NULL THEN oldest_ts '
        "WHEN oldest_ts IS NULL OR ? < oldest_ts THEN ? ELSE oldest_ts END, done = COALESCE(?, done), "
        "last_error = ?, folderid = COALESCE(?, folderid), stale_pages = COALESCE(?, stale_pages), "
        "edge_offset = COALESCE(edge_offset, ?), updated_ts = ? WHERE key = ?",
        (offset, oldest, oldest, oldest, None if done is None else int(done), error, folderid, stale, edge, now, key))
    conn.commit()


def _read_target(run: _Run, t) -> None:
    key = t["key"]
    run.key = key
    days = _clamp_days(t["days"] or run.days)
    cutoff = run.now - days * DAY
    own = key.startswith("own:")
    if own:
        user = key.removeprefix("own:")
        path, params_base, place = "/gallery/all", {"username": user}, da.MY_GALLERY
    else:
        grp = key.removeprefix("group:").split("/")[0]
        folderid = t["folderid"] or _resolve_folder(run, t)
        if not folderid:
            _save_cursor(run.conn, key, run.now, done=True, error="не нашла папку с лотами")
            return
        if not t["folderid"]:
            _save_cursor(run.conn, key, run.now, folderid=folderid)
        path = f"/gallery/{folderid}"
        params_base = {"username": grp, "mature_content": "false"}
        place = f"{grp}: {t['name'] or 'папка лотов'}"
    offset = int(t["offset"] or 0)
    stale = int(t["stale_pages"] or 0)
    while True:
        entries, has_more, next_offset = _page(run, path, {**params_base, "offset": offset,
                                                                  "limit": PAGE_LIMIT}, place)
        dated, old, oldest = _process_page(run, entries, own, place, cutoff)
        edge = offset if old else None
        if dated and old == dated:
            done = True                                   # вся страница старше окна
        else:
            stale = stale + 1 if dated and old * 2 > dated else 0
            done = stale >= STALE_PAGES
        # Конец галереи — по ответу API (has_more, next_offset), а не по лотам после фильтра: DA отдаёт
        # меньше limit, когда отсеял mature, и пустая страница с has_more — ещё не конец.
        done = done or not has_more or next_offset <= offset
        offset = next_offset
        _save_cursor(run.conn, key, run.now, offset=offset, oldest=oldest if oldest is not None else
                     (cutoff if done and old else None), done=done, stale=stale, edge=edge)
        if done:
            return


def step(conn, client, now: float | None = None, days: int = DEFAULT_DAYS, max_api: int = MAX_API,
         daily_cap: int = DAILY_CAP, sleep=time.sleep, my_username: str = "", tz: str | None = None,
         cfg=None, quiet: bool = False) -> list[str]:
    """Один проход истории рынка. Только чтение; стоп на первой паузе/бюджете/отсутствии прав.
    cfg — дозаполнить очередь группами ниши; quiet (фон) — строки только когда что-то прочитано."""
    now = now or time.time()
    ensure(conn)
    if cfg is not None:
        my_username = my_username or getattr(cfg.deviantart, "my_username", "") or ""
        tz = tz or getattr(getattr(cfg, "posting", None), "timezone", None)
        try:
            _maybe_enqueue(conn, cfg, now, days, my_username)
        except Exception as e:  # noqa: BLE001 — очередь не валит проход
            log.warning("backfill enqueue failed: %s", e)
    targets = _targets(conn)
    if not targets:
        return []
    room = min(float(max(0, max_api)), float(daily_cap) - _used_today(conn, now))
    if room < MIN_ROOM:
        return []
    if not my_username:
        own = [t["key"] for t in conn.execute("SELECT key FROM backfill_state WHERE key LIKE 'own:%'")]
        my_username = own[0].removeprefix("own:") if own else ""
    run = _Run(conn, client, now, _clamp_days(days), room, sleep, my_username.lower(), tz or DEFAULT_TZ)
    stop_line = ""
    with da_access.session(conn), da_access.task("backfill", low=True):
        block = da_access.blocked(conn, da_access.API, low=True, now=now)
        if block is not None:
            return [] if quiet else [f"история рынка: {block.message}"]
        for t in targets:
            try:
                _read_target(run, t)
            except _Stop:
                stop_line = "история рынка: бюджет прохода исчерпан — продолжу в следующий раз"
                break
            except _MetaMissing:
                _save_cursor(conn, t["key"], now, error="описания лотов не пришли — повторю эту страницу")
                stop_line = "история рынка: DeviantArt не отдал описания лотов — повторю в следующий раз"
                break
            except da_api.Throttled as e:
                stop_line = f"история рынка: {e}"
                break
            except da_api.RateLimited:
                _save_cursor(conn, t["key"], now, error="429: DeviantArt просит притормозить")
                stop_line = "история рынка: DeviantArt просит притормозить — продолжу позже"
                break
            except da_api.NeedsReconnect:
                stop_line = "история рынка: нужно ещё раз подключить DeviantArt (не хватает прав) — жду"
                break
            except da_api.NotConnected:
                stop_line = "история рынка: DeviantArt не подключён — жду подключения"
                break
            except da_api.DAApiError as e:
                if str(e.code) in ("network", "network_unsent"):
                    # DeviantArt не отвечает (таймаут): остальные цели ждали бы так же — стоп до следующего раза.
                    _save_cursor(conn, t["key"], now, error=str(e)[:200])
                    stop_line = "история рынка: DeviantArt не отвечает — продолжу в следующий раз"
                    break
                gone = str(e.code) in ("folder_not_found", "group_not_found", "invalid_request") or e.status in (400, 404)
                _save_cursor(conn, t["key"], now, done=gone or None, error=str(e)[:200])
                log.info("backfill %s failed: %s", t["key"], e)
            except Exception as e:  # noqa: BLE001 — одна цель не валит проход
                _save_cursor(conn, t["key"], now, error=f"{type(e).__name__}: {e}"[:200])
                log.warning("backfill %s failed: %s", t["key"], e)
        conn.commit()
    if quiet and not run.lots:
        return []
    lines = []
    if run.lots or run.spent:
        lines.append(f"история рынка: +{run.lots} закрытых аукционов (ставки прочитаны у {run.bids}), "
                     f"запросов API {run.spent:g}")
    if stop_line and not quiet:
        lines.append(stop_line)
    line = progress(conn, now, daily_cap=daily_cap)
    if line:
        lines.append(line)
    return lines


# --- сколько прочитано ---

def progress(conn, now: float | None = None, daily_cap: int = DAILY_CAP) -> str:
    """Одна строка: сколько групп, лотов, до какой даты назад, процент и сколько осталось. '' — очереди нет."""
    now = now or time.time()
    try:
        ensure(conn)
        rows = conn.execute("SELECT * FROM backfill_state").fetchall()
    except Exception:  # noqa: BLE001 — сломанная/занятая база: просто молчим
        return ""
    if not rows:
        return ""
    groups = [r for r in rows if r["key"].startswith("group:")]
    own = [r for r in rows if r["key"].startswith("own:")]
    lots = sum(int(r["lots"] or 0) for r in rows)
    oldest = min((r["oldest_ts"] for r in rows if r["oldest_ts"]), default=None)

    def frac(r) -> float:
        if r["done"]:
            return 1.0
        if not r["oldest_ts"]:
            return 0.0
        days = _clamp_days(r["days"])
        return max(0.0, min(1.0, (now - r["oldest_ts"]) / (days * DAY)))

    pct = sum(frac(r) for r in rows) / len(rows)
    bits = [f"{sum(1 for r in groups if r['done'])}/{len(groups)} групп"]
    if own:
        bits.append("её галерея " + ("прочитана" if own[0]["done"] else "в работе"))
    bits.append(f"{lots} закрытых аукционов")
    if oldest:
        bits.append(f"назад до {_date(oldest)}")
    if all(r["done"] for r in rows):
        errs = [r for r in rows if r["last_error"]]
        tail = f"; пропущено {len(errs)} (не нашла папку или группу)" if errs else ""
        return "история рынка прочитана: " + ", ".join(bits) + tail
    bits[-1] += f" (≈{round(pct * 100)}%)"
    spent = conn.execute("SELECT COALESCE(SUM(calls), 0) AS s, COUNT(*) AS d FROM backfill_daily").fetchone()
    calls, active_days = float(spent["s"] or 0), int(spent["d"] or 0)
    if pct > 0 and calls > 0:
        per_day = min(float(daily_cap), calls / max(1, active_days))
        left = calls * (1 - pct) / pct / max(per_day, 1.0)
        bits.append(f"осталось ≈{max(1, round(left))} дн при текущем бюджете")
    else:
        bits.append("сколько осталось — пока не посчитать")
    waiting = [r["last_error"] for r in rows if r["last_error"] and not r["done"]]
    tail = f"; последняя заминка: {waiting[0][:60]}" if waiting else ""
    return "история рынка: " + ", ".join(bits) + tail
