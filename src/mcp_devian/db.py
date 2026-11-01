import contextlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,          -- deviantart | toyhouse
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    author TEXT,
    place TEXT,                    -- группа DA или доска Toyhouse
    lot_type TEXT,                 -- auction, ota, flatsale, ... (DA)
    status TEXT,                   -- open, closed, unknown
    price_text TEXT,
    price_usd REAL,
    price_points REAL,
    wants_characters INTEGER DEFAULT 0,
    sells_characters INTEGER DEFAULT 0,
    pays TEXT,                     -- usd,points,art,trades
    posts INTEGER,
    activity_ts REAL,              -- последняя активность (unix)
    published_ts REAL,
    snippet TEXT,
    matched INTEGER DEFAULT 0,
    match_kind TEXT,               -- lot (DA), lf (ищут персов), sale (продают)
    match_reason TEXT,
    own INTEGER DEFAULT 0,         -- её собственная работа
    closed_ts REAL,                -- когда впервые увидели лот закрытым
    first_seen_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    notified INTEGER DEFAULT 0,
    dismissed INTEGER DEFAULT 0,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS items_source_activity ON items(source, activity_ts);

CREATE TABLE IF NOT EXISTS runs (
    source TEXT PRIMARY KEY,
    last_run_ts REAL,
    last_ok_ts REAL,
    last_error TEXT,
    fail_count INTEGER DEFAULT 0
);

-- Всё, что может уйти на сайт, сначала черновик: она видит его целиком и говорит «го».
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- da_groups | th_reply
    status TEXT NOT NULL,          -- pending | sending | sent | failed | cancelled
    created_ts REAL NOT NULL,
    payload TEXT NOT NULL,         -- JSON: что именно отправить
    preview TEXT NOT NULL,         -- что ей показали
    result TEXT,
    sent_ts REAL
);

-- Журнал отправок: по нему считаются лимиты.
CREATE TABLE IF NOT EXISTS sent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- da_group | th_reply
    target TEXT NOT NULL,          -- группа DA или id треда
    ref TEXT,                      -- работа DA или персонаж
    ts REAL NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS sent_log_kind_ts ON sent_log(kind, ts);

-- Состояние аукционов по комментариям (ставки). Для её лотов и для рыночной статистики.
CREATE TABLE IF NOT EXISTS auctions (
    deviation_id TEXT PRIMARY KEY, -- как в items: da:<id>
    owner TEXT,
    top_amount REAL,               -- текущая/финальная максимальная ставка
    currency TEXT,                 -- usd | points | eur | gbp
    n_bids INTEGER DEFAULT 0,
    bidders INTEGER DEFAULT 0,
    last_bid_ts REAL,
    ab_hit INTEGER DEFAULT 0,      -- кто-то взял автобай
    ended INTEGER DEFAULT 0,       -- автор написал, что закрыто/продано
    winner TEXT,
    comments_seen INTEGER DEFAULT 0,
    updated_ts REAL NOT NULL,
    extra TEXT
);

-- Её продажи: от «выиграл/купил» до «оплачено» и «перс передан». Основа для дохода и советов.
CREATE TABLE IF NOT EXISTS sales (
    id TEXT PRIMARY KEY,
    item_id TEXT,                  -- da:<id> или th:<id>, если продажа связана с лотом/тредом
    platform TEXT NOT NULL,        -- deviantart | toyhouse | other
    title TEXT NOT NULL,
    character_url TEXT,
    buyer TEXT,
    amount REAL,                   -- сколько заплатил покупатель (gross)
    currency TEXT,                 -- usd | points | eur | gbp | rub
    format TEXT,                   -- auction | flatsale | ota | bta | raffle | other
    status TEXT NOT NULL,          -- won | paid | transferred | done | cancelled
    created_ts REAL NOT NULL,
    paid_ts REAL,
    done_ts REAL,
    source TEXT,                   -- auto (из ставок) | manual
    note TEXT,
    net_amount REAL,               -- сколько дошло до неё (её цифра, например ₽ после Boosty)
    net_currency TEXT,             -- обычно rub
    pay_method TEXT                -- boosty | points | other
);
CREATE INDEX IF NOT EXISTS sales_status ON sales(status, created_ts);

-- Сигналы спроса: просмотры, фавы, комментарии (из API DeviantArt), снимки во времени.
CREATE TABLE IF NOT EXISTS deviation_stats (
    deviation_id TEXT NOT NULL,    -- da:<id>
    ts REAL NOT NULL,
    views INTEGER,
    favourites INTEGER,
    comments INTEGER,
    PRIMARY KEY (deviation_id, ts)
);

-- Размер чужих продавцов по API (watchers): уровень продавца не по тому, сколько его лотов мы случайно видели.
CREATE TABLE IF NOT EXISTS author_stats (
    author TEXT PRIMARY KEY,       -- ник в нижнем регистре
    watchers INTEGER,
    deviations INTEGER,
    ts REAL
);

-- Карточка группы DA по рынку (не по её продажам): размер папки, лотов в день, доля её ниши, цены.
CREATE TABLE IF NOT EXISTS group_scores (
    grp TEXT,
    folder TEXT,
    size INTEGER,
    lots_per_day REAL,
    niche_share REAL,
    median_sb REAL,
    unit TEXT,
    bid_share REAL,
    n INTEGER,                     -- лотов на странице ленты
    ts REAL,
    known INTEGER,                 -- из них с известным итогом: по ним доля со ставками
    sb_n INTEGER,                  -- по скольким стартам медиана
    PRIMARY KEY (grp, folder)
);

-- Папки групп со страницы группы: номер из ссылки → UUID для API и размер.
CREATE TABLE IF NOT EXISTS da_folders (
    grp TEXT NOT NULL,             -- в нижнем регистре
    folder_id TEXT NOT NULL,       -- номер из ссылки …/gallery/26780440
    uuid TEXT,                     -- gallectionUuid = folderid в API
    name TEXT,
    size INTEGER,
    ts REAL NOT NULL,
    PRIMARY KEY (grp, folder_id)
);
"""

# Бережный доступ к DeviantArt (da_access): отдельной строкой — da_access открывает её и без остальной схемы.
DA_ACCESS_SCHEMA = """
-- Бережный доступ к DeviantArt (da_access): общий бюджет запросов и общая пауза после 403/429.
CREATE TABLE IF NOT EXISTS da_budget (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,            -- rss (ленты и страницы сайта) | api (официальный API)
    cost REAL NOT NULL DEFAULT 1,  -- 304 «не изменилось» дешевле
    purpose TEXT                   -- кто потратил: ленты, ставки, рынок, …
);
CREATE INDEX IF NOT EXISTS da_budget_kind_ts ON da_budget(kind, ts);

CREATE TABLE IF NOT EXISTS da_cooldown (
    kind TEXT PRIMARY KEY,
    until_ts REAL NOT NULL,        -- до какого времени этот класс адресов не трогаем
    level INTEGER NOT NULL DEFAULT 0,  -- сколько раз подряд DeviantArt просил притормозить
    since_ts REAL NOT NULL,        -- когда просил в последний раз
    status INTEGER,                -- 403 | 429
    notified_since REAL            -- про эту паузу уже сказали уведомлением
);

-- Ленты: когда читали (очередь по кругу) и ETag/Last-Modified для условного GET.
CREATE TABLE IF NOT EXISTS da_feeds (
    key TEXT PRIMARY KEY,          -- запрос ленты: gallery:группа/папка или gallery:ник
    etag TEXT,
    last_modified TEXT,
    last_read_ts REAL,
    via TEXT,                      -- rss | api
    uuid TEXT,                     -- папка в API (если читали через API)
    deep_ts REAL,                  -- когда листали глубже первой страницы (закрытия за 3 дня)
    api_miss_ts REAL,              -- UUID папки не нашёлся в API: с тех пор читаем через RSS
    order_miss_ts REAL             -- выдача API не «сначала новые»: с тех пор читаем через RSS
);
"""
SCHEMA += DA_ACCESS_SCHEMA

# Метка места для лотов, найденных только поиском (RSS или теги API), а не в папке/галерее.
SEARCH_PLACE_PREFIX = "поиск DA"

# Колонки, добавленные после первой версии схемы.
MIGRATIONS = [
    ("items", "match_kind", "TEXT"),
    ("runs", "fail_count", "INTEGER DEFAULT 0"),
    ("items", "own", "INTEGER DEFAULT 0"),
    ("items", "closed_ts", "REAL"),
    ("group_scores", "known", "INTEGER"),
    ("group_scores", "sb_n", "INTEGER"),
    ("da_feeds", "deep_ts", "REAL"),
    ("da_feeds", "api_miss_ts", "REAL"),
    ("da_feeds", "order_miss_ts", "REAL"),
    ("sales", "net_amount", "REAL"),
    ("sales", "net_currency", "TEXT"),
    ("sales", "pay_method", "TEXT"),
]


@dataclass
class Item:
    id: str
    source: str
    title: str
    url: str
    author: str | None = None
    place: str | None = None
    lot_type: str | None = None
    status: str | None = None
    price_text: str | None = None
    price_usd: float | None = None
    price_points: float | None = None
    wants_characters: bool = False
    sells_characters: bool = False
    pays: str | None = None
    posts: int | None = None
    activity_ts: float | None = None
    published_ts: float | None = None
    snippet: str | None = None
    matched: bool = False
    match_kind: str | None = None
    match_reason: str | None = None
    own: bool = False
    extra: dict = field(default_factory=dict)


ITEM_COLUMNS = [f for f in Item.__dataclass_fields__ if f != "extra"]
# Поля extra, которые досчитали мы сами (API, поиск): повторное чтение ленты их не стирает.
# desc_ts — когда описание дочитали метаданными API: иначе после чтения через RSS его снова запрашивали бы.
KEEP_EXTRA = ("uuid", "api_tags", "via", "places", "desc_ts")
# Списки, которые копятся из разных источников: при обновлении — объединение.
UNION_EXTRA = ("via", "places", "api_tags", "folder_links")


def _union(old, new) -> list:
    out: list = []
    for v in [*(old if isinstance(old, list) else []), *(new if isinstance(new, list) else [])]:
        if v not in out:
            out.append(v)
    return out


def _merge_extra(old: dict, new: dict) -> dict:
    """Новые данные ленты + то, что мы досчитали раньше. via/places — объединение, как множества."""
    merged = dict(new)
    for key in KEEP_EXTRA:
        if key not in old:
            continue
        if key in ("via", "places"):
            merged[key] = sorted(set(_union(old.get(key), new.get(key))))
        elif key == "api_tags":
            merged[key] = _union(old.get(key), new.get(key))
        elif not merged.get(key):
            merged[key] = old[key]
    return merged


def is_search_place(place: str | None) -> bool:
    return bool(place) and str(place).startswith(SEARCH_PLACE_PREFIX)


def _keep_place(old: str | None, new: str | None) -> str | None:
    """Место лота не перескакивает на последнюю прочитанную ленту: первое остаётся.
    Исключение — место «поиск DA»: настоящая папка/галерея важнее и заменяет его."""
    if not old:
        return new
    if is_search_place(old) and new and not is_search_place(new):
        return new
    return old


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DATA_DIR / "monitor.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    _unique_sale_per_item(conn)
    conn.commit()
    return conn


def _unique_sale_per_item(conn: sqlite3.Connection) -> None:
    """Одна продажа на лот: иначе один аукцион считался бы в доходе дважды.

    В старых базах дубль уже мог появиться (две проверки записали победителя одновременно).
    Из дублей оставляем самую продвинутую запись (не отменённая, с оплатой), при равенстве — раннюю."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'sales_item'").fetchone():
        return
    rows = conn.execute(
        "SELECT rowid, item_id FROM sales WHERE item_id IN "
        "(SELECT item_id FROM sales WHERE item_id IS NOT NULL GROUP BY item_id HAVING COUNT(*) > 1) "
        "ORDER BY item_id, status = 'cancelled', paid_ts IS NULL, created_ts, rowid").fetchall()
    keep: set[str] = set()
    drop = []
    for r in rows:
        if r["item_id"] in keep:
            drop.append((r["rowid"],))
        else:
            keep.add(r["item_id"])
    conn.executemany("DELETE FROM sales WHERE rowid = ?", drop)
    # Если между чисткой и индексом другой процесс успел дописать дубль — попробуем при следующем открытии.
    with contextlib.suppress(sqlite3.IntegrityError):
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS sales_item ON sales(item_id) WHERE item_id IS NOT NULL")


@contextmanager
def session(path: Path | None = None):
    """Соединение, которое закрывается после использования (sqlite3 сам не закрывает в with)."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_item(conn: sqlite3.Connection, item: Item, now: float | None = None) -> bool:
    """Сохраняет элемент. Возвращает True, если он новый."""
    now = now or time.time()
    data = asdict(item)
    extra = json.dumps(data.pop("extra"), ensure_ascii=False)
    closed = now if item.status == "closed" else None
    old = conn.execute("SELECT status, closed_ts, extra, place FROM items WHERE id = ?", (item.id,)).fetchone()
    if old:
        # Служебные поля, которые мы досчитали сами (UUID, теги API, откуда видели), не теряем
        # при повторном чтении ленты.
        with contextlib.suppress(json.JSONDecodeError, TypeError, AttributeError):
            old_extra = json.loads(old["extra"] or "{}")
            if isinstance(old_extra, dict) and old_extra:
                extra = json.dumps(_merge_extra(old_extra, json.loads(extra)), ensure_ascii=False)
        data["place"] = _keep_place(old["place"], data["place"])
        # Момент закрытия нужен для прогноза «как быстро уходят похожие». Если лот снова открыли
        # (ON HOLD → OPEN), сбрасываем: следующее закрытие запишется заново.
        closed = (old["closed_ts"] or now) if item.status == "closed" else None
        sets = ", ".join(f"{c} = :{c}" for c in ITEM_COLUMNS if c != "id")
        conn.execute(f"UPDATE items SET {sets}, extra = :extra, closed_ts = :closed, updated_ts = :now WHERE id = :id",
                     {**data, "extra": extra, "closed": closed, "now": now})
        return False
    cols = ITEM_COLUMNS + ["extra", "closed_ts", "first_seen_ts", "updated_ts"]
    conn.execute(f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
                 {**data, "extra": extra, "closed_ts": closed, "first_seen_ts": now, "updated_ts": now})
    return True


def merge_extra(conn: sqlite3.Connection, item_id: str, patch: dict) -> bool:
    """Дописать в extra лота: списки из UNION_EXTRA объединяются, остальное заменяется.
    False — такого лота нет."""
    row = conn.execute("SELECT extra FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return False
    try:
        extra = json.loads(row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    for key, value in patch.items():
        if key in UNION_EXTRA:
            merged = _union(extra.get(key), value)
            extra[key] = sorted(set(merged)) if key in ("via", "places") else merged
        else:
            extra[key] = value
    conn.execute("UPDATE items SET extra = ? WHERE id = ?", (json.dumps(extra, ensure_ascii=False), item_id))
    return True


def save_folders(conn: sqlite3.Connection, group: str, folders, now: float | None = None) -> None:
    """Запомнить папки группы (номер → UUID и размер) со страницы группы."""
    now = now or time.time()
    conn.executemany(
        "INSERT INTO da_folders (grp, folder_id, uuid, name, size, ts) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(grp, folder_id) DO UPDATE SET uuid = COALESCE(excluded.uuid, da_folders.uuid), "
        "name = excluded.name, size = COALESCE(excluded.size, da_folders.size), ts = excluded.ts",
        [(group.lower(), str(f.folder_id), getattr(f, "uuid", None), f.name, getattr(f, "size", None), now)
         for f in folders])
    conn.commit()


def relabel_own(conn: sqlite3.Connection, my_username: str) -> None:
    """После смены ника: «её» только работы этого автора, остальные — обычный рынок."""
    if my_username:
        conn.execute("UPDATE items SET own = (lower(author) = lower(?)) WHERE source = 'deviantart'", (my_username,))
        conn.execute("UPDATE items SET matched = 0, match_kind = NULL WHERE own = 1")
    else:
        conn.execute("UPDATE items SET own = 0 WHERE source = 'deviantart'")
    conn.commit()


def known_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {r["id"] for r in conn.execute(f"SELECT id FROM items WHERE id IN ({marks})", ids)}


def get_item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def pending_digest(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM items WHERE matched = 1 AND notified = 0 AND dismissed = 0 "
        "ORDER BY source, COALESCE(activity_ts, published_ts, first_seen_ts) DESC"
    ).fetchall()


def mark_notified(conn: sqlite3.Connection, ids: list[str]) -> None:
    conn.executemany("UPDATE items SET notified = 1 WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def set_dismissed(conn: sqlite3.Connection, ids: list[str], value: bool = True) -> int:
    cur = conn.executemany("UPDATE items SET dismissed = ? WHERE id = ?", [(int(value), i) for i in ids])
    conn.commit()
    return cur.rowcount


def query_items(conn: sqlite3.Connection, *, source: str | None = None, since_ts: float | None = None,
                matched_only: bool = True, include_dismissed: bool = False, where: str = "",
                limit: int = 30) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if since_ts:
        clauses.append("COALESCE(activity_ts, published_ts, first_seen_ts) >= ?")
        params.append(since_ts)
    if matched_only:
        clauses.append("matched = 1")
    if not include_dismissed:
        clauses.append("dismissed = 0")
    if where:
        clauses.append(where)
    sql = "SELECT * FROM items"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY COALESCE(activity_ts, published_ts, first_seen_ts) DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def record_run(conn: sqlite3.Connection, source: str, error: str | None = None, now: float | None = None) -> None:
    now = now or time.time()
    conn.execute(
        "INSERT INTO runs (source, last_run_ts, last_ok_ts, last_error, fail_count) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source) DO UPDATE SET last_run_ts = excluded.last_run_ts, "
        "last_ok_ts = COALESCE(excluded.last_ok_ts, runs.last_ok_ts), last_error = excluded.last_error, "
        "fail_count = CASE WHEN excluded.last_error IS NULL THEN 0 ELSE COALESCE(runs.fail_count, 0) + 1 END",
        (source, now, None if error else now, error, 1 if error else 0),
    )
    conn.commit()


def last_run(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE source = ?", (source,)).fetchone()
