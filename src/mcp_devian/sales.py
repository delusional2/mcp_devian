"""Её продажи: кто купил, за сколько, оплатил ли, передан ли перс. Отсюда доход и советы.

Продажа идёт по шагам won → paid → transferred → done (или cancelled на любом шаге).
С аукционов DeviantArt продажа заводится сама, когда разбор ставок нашёл победителя
у закрытого лота; ей остаётся отметить «оплачено» и «передан».
"""

import json
import math
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
CURRENCIES = ("usd", "points", "eur", "gbp", "rub")
PAY_METHODS = ("boosty", "points", "other")
FOREIGN = ("usd", "eur", "gbp")
# Boosty: комиссия и источник (официальный FAQ). Курс не храним и не угадываем: сумму в ₽ вписывает она.
BOOSTY_FAQ = "https://boosty.to/boosty/posts/de1e6f32-3a80-4824-95bd-a264ff1b00aa"
BOOSTY_FEE_NOTE = "Boosty берёт 11.7% (FAQ Boosty, обновлено 12.2025) — точную сумму в ₽ впиши сама"
# Правила DA Points (https://www.deviantart.com/about/policy/points/).
POINTS_TOS = "https://www.deviantart.com/about/policy/points/"
POINTS_PER_TRANSFER = 3500
POINTS_PER_YEAR = 20000
POINTS_NOT_CASH = "поинты не выводятся в деньги (правила DA Points)"
# Перс до оплаты: платёж картой (в том числе через Boosty) покупатель может оспорить через свой банк.
CHARGEBACK_NOTE = "оплату картой можно оспорить через банк"


class SaleError(ValueError):
    pass


_COMM_PART = re.compile(r"^comm:([^:\s]+):")


def deal_key(row) -> str:
    """Одна сделка: предоплата и остаток одного слота YCH/коммишена (comm:<слот>:deposit|final) — одна сделка,
    остальные продажи — каждая своя."""
    m = _COMM_PART.match(str(row["item_id"] or ""))
    return f"comm:{m.group(1)}" if m else str(row["id"])


def is_commission_part(row) -> bool:
    """Часть оплаты заказа (предоплата или остаток): в «обычную цену адопта» не входит."""
    return bool(_COMM_PART.match(str(row["item_id"] or "")))


def _checked_net(net_amount: float | None, net_currency: str | None, pay_method: str | None,
                 currency: str | None) -> tuple[float | None, str | None, str | None]:
    """(net, валюта net, способ оплаты). Net — её цифра «сколько дошло»: без валюты считаем рублями."""
    if net_amount is not None:
        if not math.isfinite(net_amount) or net_amount < 0:
            raise SaleError("Сколько дошло — обычное число не меньше нуля")
        net_currency = net_currency or "rub"
        if net_currency not in CURRENCIES or net_currency == "points":
            raise SaleError(f"Валюта «сколько дошло»: {', '.join(c for c in CURRENCIES if c != 'points')}")
    else:
        net_currency = None
    if pay_method is not None and pay_method not in PAY_METHODS:
        raise SaleError(f"Способ оплаты: {', '.join(PAY_METHODS)}")
    if currency == "points":
        if pay_method not in (None, "points"):
            raise SaleError("Поинты — это оплата поинтами, не Boosty")
        pay_method = "points"
        if net_amount is not None:
            raise SaleError("У поинтов нет «сколько дошло в ₽»: поинты не выводятся в деньги")
    return net_amount, net_currency, pay_method


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
    if amount is not None and (not math.isfinite(amount) or amount < 0):
        raise SaleError("Сумма — обычное число не меньше нуля")
    return format if format in FORMATS else "other"


def add_sale(conn, *, title: str, amount: float | None, currency: str | None, platform: str, format: str,
             buyer: str | None = None, item_id: str | None = None, character_url: str | None = None,
             status: str = "won", source: str = "manual", note: str = "", now: float | None = None,
             if_new: bool = False, net_amount: float | None = None, net_currency: str | None = None,
             pay_method: str | None = None) -> str | None:
    """Новая продажа. if_new=True — если по этому лоту продажа уже есть, ничего не пишем и отдаём None.
    net_amount — сколько дошло до неё (её цифра; валюта по умолчанию rub), pay_method — boosty|points|other."""
    now = now or time.time()
    format = _checked_format(title, amount, currency, platform, format, status)
    net_amount, net_currency, pay_method = _checked_net(net_amount, net_currency, pay_method, currency)
    sale_id = uuid.uuid4().hex[:8]
    cur = conn.execute(
        f"INSERT {'OR IGNORE ' if if_new else ''}INTO sales (id, item_id, platform, title, character_url, buyer, "
        "amount, currency, format, status, created_ts, paid_ts, done_ts, source, note, net_amount, net_currency, "
        "pay_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sale_id, item_id, platform, title.strip(), character_url, buyer, amount, currency, format, status, now,
         now if status in ("paid", "transferred", "done") else None, now if status == "done" else None,
         source, note, net_amount, net_currency, pay_method))
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
           now: float | None = None, lot: str = "", net_amount: float | None = None,
           net_currency: str | None = None, pay_method: str | None = None) -> tuple[str, str]:
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
    net_amount, net_currency, pay_method = _checked_net(net_amount, net_currency, pay_method, currency)
    fields: dict[str, Any] = {"title": title, "amount": amount, "currency": currency, "platform": platform,
                              "format": format, "buyer": buyer, "character_url": character_url, "note": note,
                              "net_amount": net_amount, "net_currency": net_currency, "pay_method": pay_method}
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
        "character_url = COALESCE(?, character_url), format = ?, source = 'manual', note = ?, "
        "net_amount = COALESCE(?, net_amount), net_currency = COALESCE(?, net_currency), "
        "pay_method = COALESCE(?, pay_method) WHERE id = ?",
        (f["title"].strip(), f["amount"], f["currency"], f["buyer"], f["character_url"],
         row["format"] if fmt == "other" else fmt, f["note"], f.get("net_amount"), f.get("net_currency"),
         f.get("pay_method"), row["id"]))
    conn.commit()
    return row["id"], "merged"


def get(conn, sale_id: str):
    return conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id.strip(),)).fetchone()


def set_net(conn, sale_id: str, net_amount: float, net_currency: str = "rub", pay_method: str | None = None) -> None:
    """Сколько до неё дошло по продаже (её цифра после комиссии Boosty и вывода)."""
    row = get(conn, sale_id)
    if not row:
        raise SaleError("Такой продажи нет")
    net_amount_, net_currency_, pay_method_ = _checked_net(net_amount, net_currency, pay_method, row["currency"])
    conn.execute("UPDATE sales SET net_amount = ?, net_currency = ?, pay_method = COALESCE(?, pay_method) "
                 "WHERE id = ?", (net_amount_, net_currency_, pay_method_, row["id"]))
    conn.commit()


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
    seen: dict[str, set[str]] = {}
    for r in rows:
        cur = r["currency"]
        if not cur:   # валюта неизвестна — не доллары: в суммы не входит (см. without_currency)
            continue
        bucket = out.setdefault(cur, {"total": 0.0, "count": 0, "avg": 0.0, "by_platform": {}, "by_format": {}})
        bucket["total"] += r["amount"]
        deals = seen.setdefault(cur, set())
        if deal_key(r) not in deals:   # предоплата и остаток одного заказа — одна продажа
            deals.add(deal_key(r))
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
    """$1,234.50 · $40 · 1,500 pts · 1,500 ₽. Центы показываем, только если они есть."""
    if amount is None:
        return "?"
    if currency == "points":
        return f"{round(amount):,} pts"
    value = round(amount, 2)
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    if currency == "rub":
        return f"{text} ₽"
    return f"{SYMBOL.get(currency or 'usd', '$')}{text}"


def received(conn, since: float, until: float | None = None) -> dict:
    """Что дошло до неё по оплаченным продажам за период — только её цифры, без пересчёта по курсу.

    {"net": {валюта: {"total", "count"}}, "boosty_missing": {валюта gross: count},
     "missing": {валюта gross: count}} — missing: оплачено в $/€/£, а сколько дошло, не вписано."""
    until = until or time.time()
    rows = conn.execute(
        "SELECT * FROM sales WHERE status IN ('paid', 'transferred', 'done') AND paid_ts IS NOT NULL "
        "AND paid_ts >= ? AND paid_ts < ?", (since, until)).fetchall()
    net: dict[str, dict] = {}
    boosty_missing: dict[str, int] = {}
    missing: dict[str, int] = {}
    seen: dict[str, set[str]] = {}
    for r in rows:
        if r["net_amount"] is not None:
            nc = r["net_currency"] or "rub"
            b = net.setdefault(nc, {"total": 0.0, "count": 0})
            b["total"] = round(b["total"] + r["net_amount"], 2)
            if deal_key(r) not in seen.setdefault(nc, set()):
                seen[nc].add(deal_key(r))
                b["count"] += 1
        elif r["currency"] in FOREIGN and r["amount"] is not None:
            missing[r["currency"]] = missing.get(r["currency"], 0) + 1
            if r["pay_method"] == "boosty":
                boosty_missing[r["currency"]] = boosty_missing.get(r["currency"], 0) + 1
    return {"net": net, "boosty_missing": boosty_missing, "missing": missing}


def render_income(conn, days: float, now: float | None = None) -> str:
    """Текст для sales:income: оплачено по валютам, поинты отдельной строкой, сколько дошло (её цифры)."""
    now = now or time.time()
    since = now - days * 86400
    data = income(conn, since, now)
    got = received(conn, since, now)
    unknown = without_amount(conn, since)
    no_currency = without_currency(conn, since, now)
    tail = [
        (f"Ещё продаж без суммы: {unknown} — в доход не вошли. Назови сумму и ссылку на лот — уточню "
         "(sales:record с url)." if unknown else ""),
        f"Ещё {no_currency} продаж без валюты — в суммы не входят." if no_currency else "",
    ]
    if not data:
        return "\n".join(filter(None, [f"За {days:g} дн оплаченных продаж нет.", *tail]))
    lines = [f"Оплачено за {days:g} дн (по её записям; валюты не складываю и по курсу не пересчитываю):"]
    for cur in sorted(data, key=lambda c: (c == "points", c)):
        d = data[cur]
        head = "- поинты: " if cur == "points" else "- "
        lines.append(f"{head}{money(d['total'], cur)} за {d['count']} продаж(и), в среднем {money(d['avg'], cur)}"
                     + (f" — {POINTS_NOT_CASH}" if cur == "points" else ""))
        if d.get("by_format"):
            lines.append("  по форматам: " + ", ".join(f"{k} {money(v, cur)}" for k, v in d["by_format"].items()))
        if d.get("by_platform"):
            lines.append("  по площадкам: " + ", ".join(f"{k} {money(v, cur)}" for k, v in d["by_platform"].items()))
    for cur, b in got["net"].items():
        lines.append(f"Дошло до тебя (как ты вписала): {money(b['total'], cur)} по {b['count']} продаж(ам)")
    if got["boosty_missing"]:
        n = sum(got["boosty_missing"].values())
        lines.append(f"По {n} продаж(ам) через Boosty не вписано, сколько дошло: {BOOSTY_FEE_NOTE} "
                     "(sales:status sale_id=… net=…).")
    other = sum(got["missing"].values()) - sum(got["boosty_missing"].values())
    if other > 0:
        lines.append(f"По {other} продаж(ам) в $/€/£ не вписано, сколько дошло в ₽ — курс не угадываю.")
    lines += [t for t in tail if t]
    return "\n".join(lines)


def points_warnings(conn, buyer: str | None, amount: float | None, now: float | None = None,
                    exclude_sale_id: str | None = None) -> list[str]:
    """Оговорки к продаже за поинты по правилам DA Points: перевод ≤3500, от одного человека ≤20000 за год,
    поинты не выводятся в деньги. Всегда хотя бы последняя.

    exclude_sale_id — эта продажа уже записана: её сумму не считаем дважды (зовём после sales:record).
    Год — скользящие 365 дней (у DA год от первого перевода этому человеку — это приближение)."""
    now = now or time.time()
    out = []
    if amount is not None and amount > POINTS_PER_TRANSFER:
        parts = math.ceil(amount / POINTS_PER_TRANSFER)
        out.append(f"одним переводом можно не больше {POINTS_PER_TRANSFER:,} поинтов — {round(amount):,} pts "
                   f"придётся разбить на {parts} перевода(ов) ({POINTS_TOS})")
    if buyer and amount is not None:
        year = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM sales WHERE currency = 'points' AND status != 'cancelled' "
            "AND lower(ltrim(buyer, '@')) = lower(?) AND created_ts >= ? AND id != ?",
            (buyer.strip().lstrip("@"), now - 365 * 86400, exclude_sale_id or "")).fetchone()[0]
        if year + amount > POINTS_PER_YEAR:
            out.append(f"от одного человека за год можно получить не больше {POINTS_PER_YEAR:,} поинтов: от "
                       f"{buyer} за 12 мес уже {round(year):,}, с этой продажей будет {round(year + amount):,} "
                       f"({POINTS_TOS})")
    out.append(POINTS_NOT_CASH)
    return out


def describe(row) -> str:
    who = f" · {row['buyer']}" if row["buyer"] else ""
    if row["amount"] is None:
        amount = "сумма ?"
    elif not row["currency"]:
        amount = f"{row['amount']:g} (валюта ?)"
    else:
        amount = money(row["amount"], row["currency"])
    keys = row.keys()
    if "net_amount" in keys and row["net_amount"] is not None:
        amount += f" (дошло {money(row['net_amount'], row['net_currency'] or 'rub')})"
    if "pay_method" in keys and row["pay_method"] and row["pay_method"] != "points":
        amount += f" · {row['pay_method']}"
    status = STATUS_NAMES.get(row["status"], row["status"])
    if row["status"] == "transferred" and not row["paid_ts"]:
        status += ", оплаты ещё нет"
    note = f" — {row['note']}" if row["note"] else ""
    return f"[{row['id']}] {row['title']} — {amount}{who} · {status} ({row['platform']}, {row['format']}){note}"
