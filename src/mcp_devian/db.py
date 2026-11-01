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
"""

# Колонки, добавленные после первой версии схемы.
MIGRATIONS = [
    ("items", "match_kind", "TEXT"),
    ("runs", "fail_count", "INTEGER DEFAULT 0"),
    ("items", "own", "INTEGER DEFAULT 0"),
    ("items", "closed_ts", "REAL"),
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
KEEP_EXTRA = ("uuid",)


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
    conn.commit()
    return conn


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
    old = conn.execute("SELECT status, closed_ts, extra FROM items WHERE id = ?", (item.id,)).fetchone()
    if old:
        # Служебные поля, которые мы досчитали сами (UUID работы), не теряем при повторном чтении ленты.
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            kept = {k: v for k, v in json.loads(old["extra"] or "{}").items() if k in KEEP_EXTRA}
            if kept:
                extra = json.dumps({**json.loads(extra), **kept}, ensure_ascii=False)
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
