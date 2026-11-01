"""Цены из первых постов тредов продаж Toyhouse: заявленные, а не проданные.

Источник — посты, которые уже прочитаны и лежат в th_posts (th_auctions; здесь только чтение).
Из первого поста треда продаж берём медиану цен за перса в главной валюте треда и пишем
в th_stated с меткой «заявлено в первом посте». Не цены персов: шаг ставки (MI), автовыкуп (AB),
комиссии, чаевые, доплаты («+$5 for extra art»). В аукционном треде — только стартовые ставки (SB).
Есть цена «за набор» без явной цены «each» — тред пропускаем: не понять, что за перса. Это просьба продавца, не сделка: в медианы
реальных продаж (th_market.realized_sales) не попадает никогда.

Тредов без прочитанного первого поста — missing(): их можно дочитать в её Chrome по её просьбе
(th_thread read), своего браузерного кода здесь нет.
"""

import re
import statistics
import time

from .th_market import Money, parse_stated

__all__ = ["KINDS", "LABEL", "ensure", "extract", "missing", "per_character", "per_character_kind", "stated_rows"]

DAY = 86400
LABEL = "заявлено в первом посте"

SCHEMA = """
CREATE TABLE IF NOT EXISTS th_stated (
    item_id TEXT PRIMARY KEY,      -- th:<номер треда>
    amount REAL NOT NULL,          -- медиана цен за перса в главной валюте треда
    currency TEXT NOT NULL,
    label TEXT NOT NULL,           -- «заявлено в первом посте»: не цена сделки
    ts REAL NOT NULL,              -- когда тред был активен
    n_prices INTEGER,              -- сколько цен в этой валюте нашлось в посте
    raw TEXT,
    kind TEXT                      -- price (цена продажи) | sb (стартовая ставка аукциона); NULL — старая запись
);
"""
KINDS = ("price", "sb")


# Сумма — не цена перса: перед ней MI/AB/комиссия/чаевые/доплата или «+».
ADDON_BEFORE = re.compile(
    r"(?:\b(?:mi|min(?:imum)?\s*inc\w*|bid\s*inc\w*|increments?|ab|a/b|auto\s*-?\s*buy|autobuy|buy\s*-?\s*out|"
    r"insta\s*-?\s*buy|fees?|tips?|tip\s*jar|extras?|add\s*-?\s*ons?|shipping|deposit|hold)\W{0,4}|\+\s*)$", re.I)
ADDON_AFTER = re.compile(r"^\s*(?:fees?\b|tips?\b|extra\b|add\s*-?\s*ons?\b|shipping\b|"
                         r"for\s+(?:an?\s+)?(?:extra|additional|add)\b|per\s+(?:extra|additional)\b)", re.I)
START_BEFORE = re.compile(r"(?:\bsb|\bstart(?:ing)?(?:\s*(?:bid|price|at))?|\bstarts\s+at|\bmin(?:imum)?\s+bid|"
                          r"\bopening\s+bid)\W{0,4}$", re.I)


def _clause(blob: str, start: int) -> str:
    """Кусок перед суммой до ближайшего разделителя: «SB: $10 / MI: $2» → для $2 — « MI: »."""
    return re.split(r"[\n/|;,(]", blob[max(0, start - 40):start])[-1]


def _value(m: Money) -> float | None:
    return m.low if m.low is not None else m.high


def per_character(text: str) -> tuple[str, list[float]] | None:
    """Цены за перса из первого поста: (валюта, суммы) или None, если цены перса не понять."""
    got = per_character_kind(text)
    return (got[0], got[1]) if got else None


def per_character_kind(text: str) -> tuple[str, list[float], str] | None:
    """(валюта, суммы, вид): вид sb — аукционный тред (стартовые ставки), price — цены продажи.
    Их не смешиваем: старт аукциона ниже цены продажи и тянул бы общую медиану вниз."""
    st = parse_stated(text, side="sale")
    blob = f"\n{text or ''}"      # как собирает parse_stated без названия: позиции сумм — в этой строке
    if any(m.per == "set" for m in st.amounts):
        if not any(m.per == "each" for m in st.amounts):
            return None             # «€15, the set is €40» — что из этого за перса, не понять
        amounts = [m for m in st.amounts if m.per == "each"]
    else:
        amounts = list(st.amounts)
    keep: list[tuple[Money, bool]] = []
    for m in amounts:
        if not m.currency:
            continue
        before = _clause(blob, m.start)
        if ADDON_BEFORE.search(before) or ADDON_AFTER.match(blob[m.end:m.end + 25]):
            continue
        keep.append((m, bool(START_BEFORE.search(before))))
    kind = "price"
    if any(sb for _, sb in keep):  # аукционный тред: цена — стартовая ставка
        keep = [(m, sb) for m, sb in keep if sb]
        kind = "sb"
    by_cur: dict[str, list[float]] = {}
    for m, _ in keep:
        v = _value(m)
        if v is not None and v > 0 and m.currency:
            by_cur.setdefault(m.currency, []).append(float(v))
    if not by_cur:
        return None
    cur = max(by_cur, key=lambda c: (len(by_cur[c]), c == "usd"))
    return cur, by_cur[cur], kind


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    if "kind" not in {r[1] for r in conn.execute("PRAGMA table_info(th_stated)")}:
        conn.execute("ALTER TABLE th_stated ADD COLUMN kind TEXT")


def _has_posts(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'th_posts'").fetchone() is not None


def _sale_threads(conn, now: float, days: int):
    return conn.execute(
        "SELECT * FROM items WHERE source = 'toyhouse' AND match_kind = 'sale' AND COALESCE(wants_characters, 0) = 0 "
        "AND first_seen_ts <= ? AND COALESCE(activity_ts, published_ts, first_seen_ts) BETWEEN ? AND ? "
        "ORDER BY COALESCE(activity_ts, published_ts, first_seen_ts) DESC", (now, now - days * DAY, now)).fetchall()


def _first_post(conn, item_id: str) -> str | None:
    tid = item_id.removeprefix("th:").split("#")[0]
    row = conn.execute("SELECT text FROM th_posts WHERE thread_id = ? AND page = 1 ORDER BY pos, "
                       "CAST(post_id AS INTEGER) LIMIT 1", (tid,)).fetchone()
    return row["text"] if row and (row["text"] or "").strip() else None


def extract(conn, now: float | None = None, days: int = 30) -> int:
    """Разобрать первые посты тредов продаж за days дней → th_stated. → сколько тредов с ценой."""
    now = now or time.time()
    ensure(conn)
    if not _has_posts(conn):
        return 0
    stored = 0
    for r in _sale_threads(conn, now, days):
        text = _first_post(conn, r["id"])
        if text is None:
            continue
        got = per_character_kind(text)
        if got is None:
            conn.execute("DELETE FROM th_stated WHERE item_id = ?", (r["id"],))
            continue
        cur, values, kind = got
        amount = float(statistics.median(values))
        raw = ", ".join(f"{v:g}" for v in values)[:200]
        conn.execute(
            "INSERT INTO th_stated (item_id, amount, currency, label, ts, n_prices, raw, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET amount = excluded.amount, currency = excluded.currency, "
            "label = excluded.label, ts = excluded.ts, n_prices = excluded.n_prices, raw = excluded.raw, "
            "kind = excluded.kind",
            (r["id"], amount, cur, LABEL, r["activity_ts"] or r["published_ts"] or r["first_seen_ts"],
             len(values), raw, kind))
        stored += 1
    conn.commit()
    return stored


def missing(conn, limit: int = 10, now: float | None = None, days: int = 30) -> list[dict]:
    """Треды продаж без прочитанного первого поста: [{item_id, url, title}] — свежие первыми."""
    now = now or time.time()
    has = _has_posts(conn)
    out = []
    for r in _sale_threads(conn, now, days):
        if has and _first_post(conn, r["id"]) is not None:
            continue
        out.append({"item_id": r["id"], "url": r["url"], "title": r["title"] or ""})
        if len(out) >= limit:
            break
    return out


def stated_rows(conn, since: float, until: float, my_username: str = "") -> list:
    """Строки th_stated за период (без её собственных тредов): для th_market."""
    try:
        return conn.execute(
            "SELECT s.*, lower(COALESCE(i.author, '')) AS author FROM th_stated s JOIN items i ON i.id = s.item_id "
            "WHERE s.ts BETWEEN ? AND ? AND i.first_seen_ts <= ? AND (? = '' OR lower(COALESCE(i.author, '')) != ?)",
            (since, until, until, my_username.lower(), my_username.lower())).fetchall()
    except Exception:  # noqa: BLE001 — таблицы ещё нет
        return []
