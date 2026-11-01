"""Её продажи: кто купил, за сколько, оплатил ли, передан ли перс. Отсюда доход и советы.

Продажа идёт по шагам won → paid → transferred → done (или cancelled на любом шаге).
С аукционов DeviantArt продажа заводится сама, когда разбор ставок нашёл победителя
у закрытого лота; ей остаётся отметить «оплачено» и «передан».
"""

import json
import re
import sqlite3
import time
import uuid
from typing import Any

STATUSES = ("won", "paid", "transferred", "done", "cancelled")
# Куда можно перейти из каждого шага. Назад («ошиблась, ещё не оплатил») тоже можно.
TRANSITIONS = {
    "won": {"paid", "transferred", "done", "cancelled"},
    "paid": {"won", "transferred", "done", "cancelled"},
    "transferred": {"paid", "done", "cancelled"},
    "done": {"transferred", "cancelled"},
    "cancelled": {"won"},
}
STATUS_NAMES = {"won": "ждёт оплаты", "paid": "оплачено", "transferred": "перс передан", "done": "готово",
                "cancelled": "отменена"}
PLATFORMS = ("deviantart", "toyhouse", "other")
FORMATS = ("auction", "flatsale", "ota", "bta", "raffle", "adopt", "other")
CURRENCIES = ("usd", "points", "eur", "gbp")


class SaleError(ValueError):
    pass


def _checked_format(title: str, amount: float | None, currency: str | None, platform: str, format: str,
                   status: str) -> str:
    if not (title or "").strip():
        raise SaleError("Нужно название (что продано)")
    if platform not in PLATFORMS:
        raise SaleError(f"Площадка: {', '.join(PLATFORMS)}")
    if status not in STATUSES:
        raise SaleError(f"Статус: {', '.join(STATUSES)}")
    if currency is not None and currency not in CURRENCIES:
        raise SaleError(f"Валюта: {', '.join(CURRENCIES)}")
    if amount is not None and amount < 0:
        raise SaleError("Сумма не может быть отрицательной")
    return format if format in FORMATS else "other"


def add_sale(conn, *, title: str, amount: float | None, currency: str | None, platform: str, format: str,
             buyer: str | None = None, item_id: str | None = None, character_url: str | None = None,
             status: str = "won", source: str = "manual", note: str = "", now: float | None = None,
             if_new: bool = False) -> str | None:
    """Новая продажа. if_new=True — если по этому лоту продажа уже есть, ничего не пишем и отдаём None."""
    now = now or time.time()
    format = _checked_format(title, amount, currency, platform, format, status)
    sale_id = uuid.uuid4().hex[:8]
    cur = conn.execute(
        f"INSERT {'OR IGNORE ' if if_new else ''}INTO sales (id, item_id, platform, title, character_url, buyer, "
        "amount, currency, format, status, created_ts, paid_ts, done_ts, source, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sale_id, item_id, platform, title.strip(), character_url, buyer, amount, currency, format, status, now,
         now if status in ("paid", "transferred", "done") else None, now if status == "done" else None,
         source, note))
    conn.commit()
    return sale_id if cur.rowcount else None


DA_ART_URL = re.compile(r"https?://(?:www\.)?deviantart\.com/[^/]+/art/[^/?#]*?-(\d+)/?(?:[?#].*)?$", re.I)


def lot_id_for_url(conn, url: str) -> str | None:
    """da:<номер> по ссылке на работу DeviantArt — даже если лота ещё нет в базе."""
    from . import auctions

    url = (url or "").strip()
    m = DA_ART_URL.match(url)
    if not m:
        return None
    item = auctions.item_for_url(conn, url)
    return item["id"] if item else f"da:{m.group(1)}"


def record(conn, *, url: str = "", title: str, amount: float | None, currency: str | None, platform: str,
           format: str, buyer: str | None = None, character_url: str | None = None, note: str = "",
           now: float | None = None, lot: str = "") -> tuple[str, str]:
    """Её продажа вручную. Со ссылкой на лот DA или тред-аукцион Toyhouse (lot — номер лота в треде)
    привязываем к лоту, чтобы авто-запись победителя не посчитала ту же продажу второй раз.

    Возвращает (id, что вышло): new — новая запись; merged — уточнила запись, которую помощник
    завёл сам по ставкам; exists — она эту продажу уже записывала, ничего не меняла."""
    from . import th_auctions as ta

    if not (platform or "").strip():  # не сказала, где продала: ссылка на перса Toyhouse — значит Toyhouse
        platform = "toyhouse" if (character_url or "").strip().startswith("https://toyhou.se/") else "deviantart"
    item_id = None
    if (url or "").strip() and ta.THREAD_URL.match(url.strip()):
        try:
            item_id = ta.sale_item_id(conn, url.strip(), lot)
        except ValueError as e:
            raise SaleError(str(e)) from e
        platform = "toyhouse"
    elif (url or "").strip():
        item_id = lot_id_for_url(conn, url)
        if not item_id:
            raise SaleError("url — ссылка на лот DeviantArt вида https://www.deviantart.com/ник/art/название-12345 "
                            "или на тред Toyhouse https://toyhou.se/~forums/13.adoption-center/123.name "
                            "(ссылку на перса передай в character_url)")
        platform = "deviantart"
    fields: dict[str, Any] = {"title": title, "amount": amount, "currency": currency, "platform": platform, "format": format,
              "buyer": buyer, "character_url": character_url, "note": note}
    if item_id:
        existing = conn.execute("SELECT * FROM sales WHERE item_id = ?", (item_id,)).fetchone()
        if existing:
            return _merge(conn, existing, fields)
    try:
        sale_id = add_sale(conn, item_id=item_id, now=now, **fields)
    except sqlite3.IntegrityError:
        # Фоновая проверка успела записать победителя этого лота между нашим SELECT и INSERT.
        row = conn.execute("SELECT * FROM sales WHERE item_id = ?", (item_id,)).fetchone() if item_id else None
        if row is None:
            raise
        return _merge(conn, row, fields)
    if sale_id is None:  # без if_new add_sale всегда пишет или падает
        raise SaleError("Продажу не удалось записать")
    return sale_id, "new"


def _merge(conn, row, f: dict) -> tuple[str, str]:
    if row["source"] != "auto":
        return row["id"], "exists"
    fmt = _checked_format(f["title"], f["amount"], f["currency"], f["platform"], f["format"], row["status"])
    # Её данные точнее разбора комментариев: сумма, валюта, покупатель — её. Пометку «проверь» снимаем.
    conn.execute(
        "UPDATE sales SET title = ?, amount = ?, currency = ?, buyer = COALESCE(?, buyer), "
        "character_url = COALESCE(?, character_url), format = ?, source = 'manual', note = ? WHERE id = ?",
        (f["title"].strip(), f["amount"], f["currency"], f["buyer"], f["character_url"],
         row["format"] if fmt == "other" else fmt, f["note"], row["id"]))
    conn.commit()
    return row["id"], "merged"


def get(conn, sale_id: str):
    return conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id.strip(),)).fetchone()


def set_status(conn, sale_id: str, status: str, now: float | None = None) -> None:
    now = now or time.time()
    row = get(conn, sale_id)
    if not row:
        raise SaleError("Такой продажи нет")
    if status not in STATUSES:
        raise SaleError(f"Статус: {', '.join(STATUSES)}")
    if status == row["status"]:
        return
    if status not in TRANSITIONS[row["status"]]:
        raise SaleError(f"Из «{STATUS_NAMES[row['status']]}» нельзя сразу в «{STATUS_NAMES[status]}»")
    paid_ts = row["paid_ts"]
    # «Перс передан» не значит «оплачено»: передать можно и до оплаты (например, по доверию).
    # Оплату отмечает paid, а done — сделка закрыта целиком.
    if status in ("paid", "done") and not paid_ts:
        paid_ts = now
    if status == "won":
        paid_ts = None
    done_ts = now if status == "done" else (None if status != "cancelled" else row["done_ts"])
    conn.execute("UPDATE sales SET status = ?, paid_ts = ?, done_ts = ? WHERE id = ?",
                 (status, paid_ts, done_ts, row["id"]))
    conn.commit()


def list_sales(conn, statuses=None, since: float | None = None) -> list:
    sql, params = "SELECT * FROM sales WHERE 1=1", []
    if statuses:
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    if since is not None:
        sql += " AND created_ts >= ?"
        params.append(since)
    return conn.execute(sql + " ORDER BY created_ts DESC", params).fetchall()


CHECK_AMOUNT = ("сумму проверь: победитель не тот, кто ставил больше всех "
                "(может, автобай или ставку отозвали) — пришли сумму со ссылкой на лот")
CHECK_BUYER = "автор не назвал победителя — записала того, кто ставил больше всех; проверь"


def _same_user(a: str, b: str) -> bool:
    return a.strip().lstrip("@").lower() == b.strip().lstrip("@").lower()


def sync_from_auctions(conn, now: float | None = None) -> list[str]:
    """Закрытые её аукционы со ставками → продажа «ждёт оплаты» (один раз на лот).

    Победителя называет автор, сумму мы берём из максимальной ставки. Если это разные люди,
    сумма неизвестна — пишем без неё и просим проверить. Если победителя не назвали, но ставки
    были — записываем того, кто ставил больше всех, тоже с просьбой проверить."""
    rows = conn.execute(
        "SELECT i.id, i.title, i.lot_type, a.winner, a.top_amount, a.currency, a.extra FROM items i "
        "JOIN auctions a ON a.deviation_id = i.id "
        "WHERE i.source = 'deviantart' AND i.own = 1 AND a.ended = 1 AND a.top_amount IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM sales s WHERE s.item_id = i.id)").fetchall()
    created = []
    for r in rows:
        try:
            top_user = json.loads(r["extra"] or "{}").get("top_user")
        except (json.JSONDecodeError, TypeError, AttributeError):
            top_user = None
        winner, amount, note = r["winner"], r["top_amount"], ""
        if not winner:
            if not top_user:
                continue
            winner, note = top_user, CHECK_BUYER
        elif top_user and not _same_user(winner, top_user):
            amount, note = None, CHECK_AMOUNT
        currency = r["currency"] if r["currency"] in CURRENCIES else None
        # INSERT OR IGNORE: две проверки сразу (фон и чат) не заведут одну продажу дважды.
        sale_id = add_sale(conn, title=r["title"], amount=amount, currency=currency, platform="deviantart",
                           format=r["lot_type"] if r["lot_type"] in FORMATS else "auction", buyer=winner,
                           item_id=r["id"], source="auto", note=note, now=now, if_new=True)
        if sale_id:
            created.append(sale_id)
    return created


def income(conn, since: float, until: float | None = None) -> dict:
    """Доход по оплаченным продажам за период, отдельно по валютам. Без суммы, без валюты или без оплаты — не
    доход (продажи без валюты считает without_currency).

    {"usd": {"total", "count", "avg", "by_platform": {...}, "by_format": {...}}, ...}"""
    until = until or time.time()
    rows = conn.execute(
        "SELECT * FROM sales WHERE status IN ('paid', 'transferred', 'done') AND amount IS NOT NULL "
        "AND paid_ts IS NOT NULL AND paid_ts >= ? AND paid_ts < ?", (since, until)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        cur = r["currency"]
        if not cur:   # валюта неизвестна — не доллары: в суммы не входит (см. without_currency)
            continue
        bucket = out.setdefault(cur, {"total": 0.0, "count": 0, "avg": 0.0, "by_platform": {}, "by_format": {}})
        bucket["total"] += r["amount"]
        bucket["count"] += 1
        bucket["by_platform"][r["platform"]] = bucket["by_platform"].get(r["platform"], 0.0) + r["amount"]
        bucket["by_format"][r["format"]] = bucket["by_format"].get(r["format"], 0.0) + r["amount"]
    for bucket in out.values():
        bucket["avg"] = round(bucket["total"] / bucket["count"], 2) if bucket["count"] else 0.0
        bucket["total"] = round(bucket["total"], 2)
    return out


def without_currency(conn, since: float, until: float | None = None) -> int:
    """Сколько оплаченных продаж за период с суммой, но без валюты: в доход не входят, не считаем их долларами."""
    until = until or time.time()
    return conn.execute(
        "SELECT COUNT(*) FROM sales WHERE status IN ('paid', 'transferred', 'done') AND amount IS NOT NULL "
        "AND (currency IS NULL OR currency = '') AND paid_ts IS NOT NULL AND paid_ts >= ? AND paid_ts < ?",
        (since, until)).fetchone()[0]


def without_amount(conn, since: float) -> int:
    """Сколько живых продаж за период без суммы (победитель не тот, кто ставил больше): в доход не входят."""
    return conn.execute("SELECT COUNT(*) FROM sales WHERE amount IS NULL AND status != 'cancelled' "
                        "AND created_ts >= ?", (since,)).fetchone()[0]


def unpaid(conn, older_than_days: float = 3, now: float | None = None) -> list:
    """Не оплачены: «ждёт оплаты» и «перс передан» без отметки оплаты (передала по доверию)."""
    now = now or time.time()
    return conn.execute("SELECT * FROM sales WHERE (status = 'won' OR (status = 'transferred' AND paid_ts IS NULL)) "
                        "AND created_ts <= ? ORDER BY created_ts", (now - older_than_days * 86400,)).fetchall()


SYMBOL = {"usd": "$", "eur": "€", "gbp": "£"}


def money(amount: float | None, currency: str | None) -> str:
    """$1,234.50 · $40 · 1,500 pts. Центы показываем, только если они есть."""
    if amount is None:
        return "?"
    if currency == "points":
        return f"{round(amount):,} pts"
    value = round(amount, 2)
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return f"{SYMBOL.get(currency or 'usd', '$')}{text}"


def describe(row) -> str:
    who = f" · {row['buyer']}" if row["buyer"] else ""
    if row["amount"] is None:
        amount = "сумма ?"
    elif not row["currency"]:
        amount = f"{row['amount']:g} (валюта ?)"
    else:
        amount = money(row["amount"], row["currency"])
    status = STATUS_NAMES.get(row["status"], row["status"])
    if row["status"] == "transferred" and not row["paid_ts"]:
        status += ", оплаты ещё нет"
    note = f" — {row['note']}" if row["note"] else ""
    return f"[{row['id']}] {row['title']} — {amount}{who} · {status} ({row['platform']}, {row['format']}){note}"
