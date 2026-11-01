"""YCH и коммишены: второй доход с тех же покупателей.

Предложения (YCH-база со слотами, виды коммишенов с ценами), очередь слотов с предоплатой и сроками,
напоминания ей самой, проверка загрузки против её часов в неделю и запись денег в обычные продажи.

Слот идёт по шагам claimed → deposit → in_progress → wip_sent → final_paid → delivered (или cancelled).
Деньги попадают в sales частями: предоплата и остаток — отдельные записи с item_id comm:<слот>:deposit и
comm:<слот>:final. Уникальный item_id в sales не даёт записать одну часть дважды.

Клиентам и в ленту ничего не уходит: напоминания — для неё, анонс — только факты, текст пишет она сама.
На Toyhouse тексты от ИИ запрещены, включая перевод (правило 4.8), — это сказано в анонсе для Toyhouse.
"""

from __future__ import annotations

import re
import statistics
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import evidence, sales

DAY = 86400
WEEK = 7 * DAY
KINDS = ("ych", "commission")
KIND_NAMES = {"ych": "YCH", "commission": "коммишен"}
PLATFORMS = ("deviantart", "toyhouse", "x", "other")
SLOT_STATUSES = ("claimed", "deposit", "in_progress", "wip_sent", "final_paid", "delivered", "cancelled")
ACTIVE = ("claimed", "deposit", "in_progress", "wip_sent", "final_paid")
RANK = {s: i for i, s in enumerate(SLOT_STATUSES[:-1])}
TRANSITIONS = {
    "claimed": {"deposit", "in_progress", "cancelled"},
    "deposit": {"claimed", "in_progress", "cancelled"},
    "in_progress": {"claimed", "deposit", "wip_sent", "final_paid", "cancelled"},
    "wip_sent": {"in_progress", "final_paid", "cancelled"},
    "final_paid": {"in_progress", "wip_sent", "delivered", "cancelled"},
    "delivered": {"final_paid"},
    "cancelled": {"claimed"},
}
STATUS_NAMES = {"claimed": "занят", "deposit": "предоплата есть", "in_progress": "в работе",
                "wip_sent": "WIP отправлен, ждёт остаток", "final_paid": "оплачено полностью — отдать работу",
                "delivered": "сдано", "cancelled": "отменён"}
DEFAULT_DEPOSIT_PCT = 50
# После WIP обычно остаётся доделка; без её цифры (hours_left) считаем четверть оценки — это допущение.
AFTER_WIP_SHARE = 0.25
# Слот без срока в проверке загрузки стоит в конце: через 4 недели.
NO_DEADLINE_DAYS = 28
MIN_STATS = 8
TOYHOUSE_RULE = ("Toyhouse, правило 4.8: тексты от ИИ запрещены, включая перевод и описания — "
                 "пиши сама, без перевода (https://toyhou.se/~rules)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS comm_offers (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- ych | commission
    title TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    slots INTEGER,                   -- мест в YCH; NULL — без ограничения
    deposit_pct REAL NOT NULL,
    hours REAL,                      -- её оценка часов на один слот
    turnaround_days REAL,            -- срок по умолчанию от заявки
    platform TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | closed
    created_ts REAL NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS comm_slots (
    id TEXT PRIMARY KEY,
    offer_id TEXT,
    kind TEXT NOT NULL,
    client TEXT NOT NULL,
    what TEXT,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    deposit_pct REAL NOT NULL,
    hours REAL,
    hours_left REAL,                 -- её поправка «осталось часов»
    deadline_ts REAL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    created_ts REAL NOT NULL,
    status_ts REAL NOT NULL,         -- когда слот пришёл в нынешний шаг
    deposit_paid_ts REAL,
    final_paid_ts REAL,
    delivered_ts REAL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS comm_slots_status ON comm_slots(status, deadline_ts);
CREATE TABLE IF NOT EXISTS comm_reminded (
    slot_id TEXT NOT NULL,
    key TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (slot_id, key)
);
"""


class CommissionError(ValueError):
    pass


@dataclass
class Settings:
    weekly_hours: float = 0          # сколько часов в неделю она успевает рисовать; 0 — не проверяем загрузку
    tz: str = "Europe/Moscow"
    deposit_pct: float = DEFAULT_DEPOSIT_PCT
    deposit_days: float = 3          # предоплата не пришла столько дней — напомнить
    deadline_warn_days: float = 3    # до срока осталось столько — напомнить
    final_days: float = 5            # после WIP остаток не пришёл столько дней — напомнить
    deliver_days: float = 2          # оплачено полностью, а работа не отдана столько дней — напомнить


def _cfg_num(p, name: str, default: float, lo: float = 0.0, hi: float = 1e6) -> float:
    """Число из конфига; нет поля, None, мусор — значение по умолчанию; за пределами — к границе."""
    v = getattr(p, name, None)
    if v is None or isinstance(v, bool):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:
        return default
    return min(hi, max(lo, x))


def settings_from(cfg) -> Settings:
    """Настройки из её конфига; полей ещё нет (или там None) — значения по умолчанию."""
    p = getattr(cfg, "posting", None)
    d = Settings()
    return Settings(
        weekly_hours=_cfg_num(p, "commission_weekly_hours", d.weekly_hours, 0, 168),
        tz=getattr(p, "timezone", d.tz) or d.tz,
        deposit_pct=_cfg_num(p, "commission_deposit_pct", d.deposit_pct, 0, 100),
        deposit_days=_cfg_num(p, "commission_deposit_days", d.deposit_days, 0, 365),
        deadline_warn_days=_cfg_num(p, "commission_deadline_warn_days", d.deadline_warn_days, 0, 365),
        final_days=_cfg_num(p, "commission_final_days", d.final_days, 0, 365),
        deliver_days=_cfg_num(p, "commission_deliver_days", d.deliver_days, 0, 365),
    )


def ensure_tables(conn) -> None:
    conn.executescript(SCHEMA)


def _zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "Europe/Moscow")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Moscow")


def _new_id() -> str:
    return uuid.uuid4().hex[:6]


def _client(name: str | None) -> str:
    return (name or "").strip().lstrip("@").strip()


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and x not in (float("inf"),
                                                                                          float("-inf"))


def _currency(value: str | None, default: str = "rub") -> str:
    return (value or "").strip().lower() or default


PLATFORM_ALIASES = {"da": "deviantart", "deviant": "deviantart", "th": "toyhouse", "toyhou.se": "toyhouse",
                    "twitter": "x", "tw": "x"}


def _platform(value: str | None, default: str = "other") -> str:
    """deviantart | toyhouse | x | other — без учёта регистра, с короткими именами (DA, TH, Twitter)."""
    p = (value or "").strip().lower()
    if not p:
        return default
    p = PLATFORM_ALIASES.get(p, p)
    if p not in PLATFORMS:
        raise CommissionError(f"Площадка: {', '.join(PLATFORMS)}")
    return p


def _check_money(price, currency: str | None, deposit_pct) -> None:
    if price is None or not _is_num(price) or price < 0:
        raise CommissionError("Цена — обычное число не меньше нуля")
    if currency not in sales.CURRENCIES:
        raise CommissionError(f"Валюта: {', '.join(sales.CURRENCIES)}")
    if not _is_num(deposit_pct) or not 0 <= deposit_pct <= 100:
        raise CommissionError("Предоплата — от 0 до 100%")


def _check_hours(hours) -> None:
    if hours is not None and (not _is_num(hours) or hours < 0 or hours > 1000):
        raise CommissionError("Часы — число от 0 до 1000")


# --- Сроки ---

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_RU = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$")
_DAYS = re.compile(r"^\+?(\d{1,3})(?:\s*(?:д|дн|дней|дня|days?|d))?$", re.I)


def parse_deadline(text: str | None, *, now: float | None = None, tz: str | None = None) -> float | None:
    """«2027-02-01», «01.02.2027», «01.02» (ближайшее будущее) — конец дня в её поясе; «10» / «+10» — через
    10 дней. Пусто — None."""
    now = now or time.time()
    s = (text or "").strip()
    if not s:
        return None
    m = _DAYS.match(s)
    if m:
        days = int(m.group(1))
        if days < 1:
            raise CommissionError("Срок — хотя бы через 1 день")
        return now + days * DAY
    zone = _zone(tz)
    m = _ISO.match(s)
    r = _RU.match(s)
    when: datetime | None = None
    try:
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            when = datetime(y, mo, d, 23, 59, tzinfo=zone)
        elif r:
            d, mo = int(r.group(1)), int(r.group(2))
            if r.group(3):
                when = datetime(int(r.group(3)), mo, d, 23, 59, tzinfo=zone)
            else:  # ближайшее будущее такое число (29.02 — ближайший високосный год)
                year = datetime.fromtimestamp(now, zone).year
                for y in range(year, year + 9):
                    try:
                        cand = datetime(y, mo, d, 23, 59, tzinfo=zone)
                    except ValueError:
                        continue
                    if cand.timestamp() > now:
                        when = cand
                        break
                if when is None:
                    raise ValueError("no such date")
        else:
            raise CommissionError("Срок — дата вида 2027-02-01 или 01.02, либо число дней («10»)")
    except ValueError as e:
        if isinstance(e, CommissionError):
            raise
        raise CommissionError("Такой даты нет") from e
    assert when is not None
    if when.timestamp() <= now:
        raise CommissionError("Этот срок уже прошёл")
    return when.timestamp()


def _date(ts: float, tz: str | None) -> str:
    return datetime.fromtimestamp(ts, _zone(tz)).strftime("%d.%m")


# --- Предложения ---

def add_offer(conn, *, kind: str, title: str, price: float, currency: str = "rub", slots: int | None = None,
              deposit_pct: float = DEFAULT_DEPOSIT_PCT, hours: float | None = None,
              turnaround_days: float | None = None, platform: str = "other", note: str = "",
              now: float | None = None) -> str:
    """Новое предложение: YCH (обычно со слотами) или вид коммишена с ценой."""
    ensure_tables(conn)
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise CommissionError(f"Вид: {', '.join(KINDS)}")
    if not (title or "").strip():
        raise CommissionError("Нужно название")
    currency = _currency(currency)
    _check_money(price, currency, deposit_pct)
    _check_hours(hours)
    if slots is not None and (isinstance(slots, bool) or not isinstance(slots, int) or slots < 1):
        raise CommissionError("Мест — хотя бы одно (или не указывай, если без ограничения)")
    if turnaround_days is not None and (not _is_num(turnaround_days) or not 0 < turnaround_days <= 365):
        raise CommissionError("Срок выполнения — от 1 до 365 дней")
    platform = _platform(platform)
    oid = _new_id()
    conn.execute("INSERT INTO comm_offers (id, kind, title, price, currency, slots, deposit_pct, hours, "
                 "turnaround_days, platform, status, created_ts, note) VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                 (oid, kind, title.strip(), float(price), currency, slots, float(deposit_pct), hours,
                  turnaround_days, platform, now or time.time(), note))
    conn.commit()
    return oid


def get_offer(conn, offer_id: str):
    ensure_tables(conn)
    return conn.execute("SELECT * FROM comm_offers WHERE id = ?", ((offer_id or "").strip(),)).fetchone()


def list_offers(conn, open_only: bool = True) -> list:
    ensure_tables(conn)
    sql = "SELECT * FROM comm_offers" + (" WHERE status = 'open'" if open_only else "")
    return conn.execute(sql + " ORDER BY created_ts DESC").fetchall()


def set_offer_open(conn, offer_id: str, is_open: bool) -> None:
    if not get_offer(conn, offer_id):
        raise CommissionError("Такого предложения нет")
    conn.execute("UPDATE comm_offers SET status = ? WHERE id = ?", ("open" if is_open else "closed", offer_id.strip()))
    conn.commit()


def close_offer(conn, offer_id: str) -> None:
    set_offer_open(conn, offer_id, False)


def slots_left(conn, offer_id: str) -> int | None:
    """Свободных мест в предложении; None — без ограничения."""
    offer = get_offer(conn, offer_id)
    if not offer:
        raise CommissionError("Такого предложения нет")
    if offer["slots"] is None:
        return None
    taken = conn.execute("SELECT COUNT(*) FROM comm_slots WHERE offer_id = ? AND status != 'cancelled'",
                         (offer["id"],)).fetchone()[0]
    return max(0, offer["slots"] - taken)


# --- Слоты ---

def get_slot(conn, slot_id: str):
    ensure_tables(conn)
    return conn.execute("SELECT * FROM comm_slots WHERE id = ?", ((slot_id or "").strip(),)).fetchone()


def _slot(conn, slot_id: str):
    row = get_slot(conn, slot_id)
    if not row:
        raise CommissionError("Такого слота нет")
    return row


def list_slots(conn, active_only: bool = False) -> list:
    ensure_tables(conn)
    sql = "SELECT * FROM comm_slots"
    if active_only:
        sql += f" WHERE status IN ({','.join('?' * len(ACTIVE))})"
    return conn.execute(sql + " ORDER BY COALESCE(deadline_ts, 1e18), created_ts",
                        ACTIVE if active_only else ()).fetchall()


def claim(conn, *, client: str, what: str = "", offer_id: str | None = None, kind: str | None = None,
          price: float | None = None, currency: str | None = None, deposit_pct: float | None = None,
          deadline: str | None = None, hours: float | None = None, platform: str | None = None, note: str = "",
          now: float | None = None, tz: str | None = None, weekly_hours: float = 0, force: bool = False,
          default_deposit_pct: float = DEFAULT_DEPOSIT_PCT) -> dict:
    """Занять слот. Из предложения берём цену, валюту, предоплату, часы и срок, если не названы.

    weekly_hours > 0 — проверяем, успевает ли она к сроку вместе с уже взятым; не успевает — отказ,
    если она не решила взять всё равно (force). {"slot_id", "warnings", "capacity"}."""
    ensure_tables(conn)
    now = now or time.time()
    client = _client(client)
    if not client:
        raise CommissionError("Нужен ник клиента")
    offer = None
    if offer_id:
        offer = get_offer(conn, offer_id)
        if not offer:
            raise CommissionError("Такого предложения нет")
        if offer["status"] != "open":
            raise CommissionError("Это предложение закрыто — открой его снова, если берёшь ещё")
        if slots_left(conn, offer["id"]) == 0:
            raise CommissionError("Свободных мест в этом YCH нет")
    kind = (kind or "").strip().lower() or (offer["kind"] if offer else "commission")
    if kind not in KINDS:
        raise CommissionError(f"Вид: {', '.join(KINDS)}")
    if price is None and offer:
        price = offer["price"]
    if price is None:
        raise CommissionError("Нужна цена слота")
    currency = _currency(currency, offer["currency"] if offer else "rub")
    if deposit_pct is None:
        deposit_pct = offer["deposit_pct"] if offer else default_deposit_pct
    _check_money(price, currency, deposit_pct)
    if hours is None and offer:
        hours = offer["hours"]
    _check_hours(hours)
    platform = _platform(platform, offer["platform"] if offer else "other")
    deadline_ts = parse_deadline(deadline, now=now, tz=tz)
    if deadline_ts is None and offer and offer["turnaround_days"]:
        deadline_ts = now + offer["turnaround_days"] * DAY

    warnings: list[str] = []
    cap = None
    if weekly_hours and weekly_hours > 0 and hours:
        # Отказ — только если именно этот слот создаёт срыв срока, которого без него нет. Уже просроченные
        # и уже сорванные сроки новый слот не исправит и не должен из-за них блокироваться.
        before = capacity(conn, weekly_hours=weekly_hours, now=now)
        cap = capacity(conn, weekly_hours=weekly_hours, now=now, extra=(deadline_ts, float(hours)))
        old = {c["slot_id"] for c in before["conflicts"]}
        new = [c for c in cap["conflicts"] if c["slot_id"] not in old]
        if new:
            msg = _overbook_text(new[0], weekly_hours, tz)
            if not force:
                raise CommissionError(msg + " Поставь срок позже или возьми всё равно (force).")
            warnings.append(msg)
        elif before["conflicts"]:
            warnings.append("уже взятое не успевается: " + _overbook_text(before["conflicts"][0], weekly_hours, tz))
    sid = _new_id()
    conn.execute(
        "INSERT INTO comm_slots (id, offer_id, kind, client, what, price, currency, deposit_pct, hours, deadline_ts, "
        "platform, status, created_ts, status_ts, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,'claimed',?,?,?)",
        (sid, offer["id"] if offer else None, kind, client, (what or "").strip() or (offer["title"] if offer else ""),
         float(price), currency, float(deposit_pct), hours, deadline_ts, platform, now, now, note))
    conn.commit()
    return {"slot_id": sid, "warnings": warnings, "capacity": cap}


def _overbook_text(c: dict | None, weekly_hours: float, tz: str | None) -> str:
    if not c:
        return "Не успеешь к срокам."
    who = "новому слоту" if c["slot_id"] == "new" else f"слоту [{c['slot_id']}]"
    return (f"Не успеешь к сроку {_date(c['deadline_ts'], tz)} ({who}): к нему нужно ~{c['need']:g} ч работы, а при "
            f"{weekly_hours:g} ч в неделю есть ~{c['have']:g} ч.")


def _amounts(slot) -> tuple[float, float]:
    deposit = round(slot["price"] * slot["deposit_pct"] / 100, 2)
    return deposit, round(slot["price"] - deposit, 2)


def _sale_for(conn, slot_id: str, part: str):
    return conn.execute("SELECT * FROM sales WHERE item_id = ?", (f"comm:{slot_id}:{part}",)).fetchone()


def _link(conn, slot, part: str, now: float, net_amount: float | None = None, net_currency: str | None = None,
          pay_method: str | None = None) -> str | None:
    """Часть оплаты → продажа «оплачено». Уже есть — не дублируем; отменённую возвращаем."""
    deposit, final = _amounts(slot)
    amount = deposit if part == "deposit" else final
    if amount <= 0:
        return None
    try:
        row = _sale_for(conn, slot["id"], part)
        if row is None:
            what = slot["what"] or KIND_NAMES[slot["kind"]]
            label = "предоплата" if part == "deposit" else ("остаток" if deposit > 0 else "оплата")
            sale_id = sales.add_sale(
                conn, title=f"{KIND_NAMES[slot['kind']]} «{what}» — {label}", amount=amount,
                currency=slot["currency"], platform="other" if slot["platform"] == "x" else slot["platform"],
                format=slot["kind"], buyer=slot["client"], item_id=f"comm:{slot['id']}:{part}", status="paid",
                source="commissions", note=f"commissions:{slot['id']}", now=now, if_new=True,
                net_amount=net_amount, net_currency=net_currency, pay_method=pay_method)
            if sale_id:
                return sale_id
            row = _sale_for(conn, slot["id"], part)
        if row["status"] == "cancelled":
            sales.set_status(conn, row["id"], "won", now=now)
            sales.set_status(conn, row["id"], "paid", now=now)
        if net_amount is not None:
            sales.set_net(conn, row["id"], net_amount, net_currency or "rub", pay_method)
        return row["id"]
    except sales.SaleError as e:
        raise CommissionError(str(e)) from e


def _sale_to(conn, slot_id: str, part: str, status: str, now: float) -> None:
    row = _sale_for(conn, slot_id, part)
    if not row or row["status"] == status:
        return
    if row["status"] == "cancelled" and status == "done":
        return  # отменённая (не пришедшая или возвращённая) оплата доходом не становится
    path = {("done", "paid"): ["transferred", "paid"], ("cancelled", "paid"): ["won", "paid"],
            ("done", "cancelled"): ["cancelled"], ("paid", "done"): ["done"], ("paid", "cancelled"): ["cancelled"],
            ("transferred", "done"): ["done"]}
    for step in path.get((row["status"], status), [status]):
        sales.set_status(conn, row["id"], step, now=now)


def _split_net(net: float | None, deposit: float, final: float) -> tuple[float | None, float | None]:
    """Одна сумма «сколько дошло» за всю оплату → доли предоплаты и остатка пропорционально суммам."""
    if net is None:
        return None, None
    total = deposit + final
    dep = round(net * deposit / total, 2) if total > 0 else 0.0
    return dep, round(net - dep, 2)


def mark_paid(conn, slot_id: str, part: str, *, net_amount: float | None = None, net_currency: str | None = None,
              pay_method: str | None = None, now: float | None = None) -> dict:
    """Пришли деньги: part = deposit (предоплата) или final (остаток / вся сумма без предоплаты).
    Пишет часть в продажи один раз. {"sale_id", "status", "warnings", "parts", "amount"} — parts и amount:
    какие части и на какую сумму записаны этим вызовом."""
    now = now or time.time()
    slot = _slot(conn, slot_id)
    if part not in ("deposit", "final"):
        raise CommissionError("Часть оплаты: deposit или final")
    if slot["currency"] == "points":
        pay_method = None  # поинты — не Boosty, даже если Boosty у неё настроен: способ оплаты — поинты
    if slot["status"] not in ACTIVE:
        raise CommissionError(f"Слот {STATUS_NAMES[slot['status']]} — оплату не отмечаю")
    deposit, final = _amounts(slot)
    warnings: list[str] = []
    if part == "deposit":
        if slot["deposit_pct"] <= 0:
            raise CommissionError("У этого слота нет предоплаты — отметь оплату как final")
        plan = [("deposit", net_amount)]
    elif final <= 0:
        plan = [("deposit", net_amount)]   # 100% предоплата: вся сумма — это одна оплата
    elif deposit > 0 and not slot["deposit_paid_ts"]:
        dep_net, fin_net = _split_net(net_amount, deposit, final)
        plan = [("deposit", dep_net), ("final", fin_net)]
        warnings.append("предоплата не была отмечена — записала и её, клиент заплатил всё сразу"
                        + ("; «сколько дошло» разделила между предоплатой и остатком пропорционально суммам"
                           if net_amount is not None else ""))
    else:
        plan = [("final", net_amount)]
    amounts = {"deposit": deposit, "final": final}
    if slot["currency"] == "points":
        fresh = 0.0
        for p, _ in plan:
            row = _sale_for(conn, slot["id"], p)
            if amounts[p] > 0 and (row is None or row["status"] == "cancelled"):
                fresh += amounts[p]
        if fresh > 0:
            warnings += sales.points_warnings(conn, slot["client"], fresh, now=now)
    sale_id = None
    for p, net in plan:
        sale_id = _link(conn, slot, p, now, net, net_currency, pay_method) or sale_id
    if part == "deposit":
        new_status = "deposit" if slot["status"] == "claimed" else slot["status"]
        conn.execute("UPDATE comm_slots SET deposit_paid_ts = COALESCE(deposit_paid_ts, ?), status = ?, "
                     "status_ts = CASE WHEN status = ? THEN status_ts ELSE ? END WHERE id = ?",
                     (now, new_status, new_status, now, slot["id"]))
    else:
        if deposit > 0:
            conn.execute("UPDATE comm_slots SET deposit_paid_ts = COALESCE(deposit_paid_ts, ?) WHERE id = ?",
                         (now, slot["id"]))
        conn.execute("UPDATE comm_slots SET final_paid_ts = COALESCE(final_paid_ts, ?), status = 'final_paid', "
                     "status_ts = CASE WHEN status = 'final_paid' THEN status_ts ELSE ? END WHERE id = ?",
                     (now, now, slot["id"]))
        new_status = "final_paid"
    conn.commit()
    parts = [p for p, _ in plan if amounts[p] > 0]
    return {"sale_id": sale_id, "status": new_status, "warnings": warnings, "parts": parts,
            "amount": round(sum(amounts[p] for p in parts), 2)}


def set_status(conn, slot_id: str, status: str, now: float | None = None) -> list[str]:
    """Перевести слот на шаг. Оплата через статус тоже попадает в продажи (без «сколько дошло»).
    Возвращает предупреждения."""
    now = now or time.time()
    slot = _slot(conn, slot_id)
    if status not in SLOT_STATUSES:
        raise CommissionError(f"Шаг: {', '.join(SLOT_STATUSES)}")
    if status == slot["status"]:
        return []
    if status == "cancelled":
        cancel(conn, slot_id, now=now)
        return []
    if status not in TRANSITIONS[slot["status"]]:
        raise CommissionError(f"Из «{STATUS_NAMES[slot['status']]}» нельзя сразу в «{STATUS_NAMES[status]}»")
    warnings: list[str] = []
    if slot["status"] == "cancelled":
        return _reopen(conn, slot, now)
    if status == "deposit":
        if slot["deposit_pct"] <= 0:
            raise CommissionError("У этого слота нет предоплаты")
        if not slot["deposit_paid_ts"]:
            return mark_paid(conn, slot_id, "deposit", now=now)["warnings"]
    if status == "final_paid" and not slot["final_paid_ts"]:
        return mark_paid(conn, slot_id, "final", now=now)["warnings"]
    if status == "in_progress" and slot["deposit_pct"] > 0 and not slot["deposit_paid_ts"]:
        warnings.append("работа начата без предоплаты — если клиент пропадёт, часы не окупятся")
    if RANK[status] < RANK["deposit"] and slot["deposit_paid_ts"]:
        _sale_to(conn, slot["id"], "deposit", "cancelled", now)
        conn.execute("UPDATE comm_slots SET deposit_paid_ts = NULL WHERE id = ?", (slot["id"],))
    if RANK[status] < RANK["final_paid"] and slot["final_paid_ts"]:
        _sale_to(conn, slot["id"], "final", "cancelled", now)
        conn.execute("UPDATE comm_slots SET final_paid_ts = NULL WHERE id = ?", (slot["id"],))
    if status == "delivered":
        for part in ("deposit", "final"):
            _sale_to(conn, slot["id"], part, "done", now)
    elif slot["status"] == "delivered":
        for part in ("deposit", "final"):
            _sale_to(conn, slot["id"], part, "paid", now)
    conn.execute("UPDATE comm_slots SET status = ?, status_ts = ?, delivered_ts = ? WHERE id = ?",
                 (status, now, now if status == "delivered" else None, slot["id"]))
    conn.commit()
    if slot["deadline_ts"] and status in ACTIVE and slot["deadline_ts"] < now:
        warnings.append("срок уже прошёл — договорись с клиентом о новом (sales:ych_slot date=…)")
    return warnings


def _reopen(conn, slot, now: float) -> list[str]:
    """Отменённый слот снова в очереди. Места в YCH и закрытое предложение проверяем, как при новой заявке.
    Оставленные при отмене деньги снова засчитываются за этот слот (шаг — по тому, что оплачено)."""
    if slot["offer_id"]:
        offer = get_offer(conn, slot["offer_id"])
        if offer and offer["status"] != "open":
            raise CommissionError("Это предложение закрыто — открой его снова, если берёшь слот обратно")
        if offer and slots_left(conn, offer["id"]) == 0:
            raise CommissionError("Свободных мест в этом YCH нет — слот вернуть некуда")
    warnings: list[str] = []
    status = "final_paid" if slot["final_paid_ts"] else "deposit" if slot["deposit_paid_ts"] else "claimed"
    for part, ts in (("deposit", slot["deposit_paid_ts"]), ("final", slot["final_paid_ts"])):
        if ts:
            _sale_to(conn, slot["id"], part, "paid", now)
    if status != "claimed":
        warnings.append("оставленная при отмене оплата снова засчитана за этот слот — шаг: "
                        + STATUS_NAMES[status])
    conn.execute("UPDATE comm_slots SET status = ?, status_ts = ?, delivered_ts = NULL WHERE id = ?",
                 (status, now, slot["id"]))
    conn.commit()
    if slot["deadline_ts"] and slot["deadline_ts"] < now:
        warnings.append("срок уже прошёл — договорись с клиентом о новом (sales:ych_slot date=…)")
    return warnings


def cancel(conn, slot_id: str, *, refund_deposit: bool = False, now: float | None = None) -> None:
    """Отменить слот. refund_deposit — вернула деньги: продажи отменяются; иначе оставленные деньги
    остаются доходом (продажа закрывается как «готово»). Трогаем только реально отмеченные оплаты:
    отменённая раньше (ошиблась, денег не было) доходом не становится."""
    now = now or time.time()
    slot = _slot(conn, slot_id)
    if slot["status"] not in ACTIVE:
        raise CommissionError(f"Слот уже {STATUS_NAMES[slot['status']]}")
    for part, paid_ts in (("deposit", slot["deposit_paid_ts"]), ("final", slot["final_paid_ts"])):
        _sale_to(conn, slot["id"], part, "done" if paid_ts and not refund_deposit else "cancelled", now)
    if refund_deposit:  # деньги вернула — слот больше не оплачен (важно, если его откроют снова)
        conn.execute("UPDATE comm_slots SET deposit_paid_ts = NULL, final_paid_ts = NULL WHERE id = ?", (slot["id"],))
    conn.execute("UPDATE comm_slots SET status = 'cancelled', status_ts = ? WHERE id = ?", (now, slot["id"]))
    conn.commit()


def set_deadline(conn, slot_id: str, deadline: str, *, now: float | None = None, tz: str | None = None) -> float | None:
    slot = _slot(conn, slot_id)
    ts = parse_deadline(deadline, now=now, tz=tz)
    conn.execute("UPDATE comm_slots SET deadline_ts = ? WHERE id = ?", (ts, slot["id"]))
    conn.commit()
    return ts


def set_hours_left(conn, slot_id: str, hours_left: float | None) -> None:
    """Её поправка: сколько часов осталось по слоту (None — снова по оценке)."""
    slot = _slot(conn, slot_id)
    _check_hours(hours_left)
    conn.execute("UPDATE comm_slots SET hours_left = ? WHERE id = ?", (hours_left, slot["id"]))
    conn.commit()


# --- Напоминания ---

def reminders(conn, now: float | None = None, *, deposit_days: float = 3, deadline_warn_days: float = 3,
              final_days: float = 5, deliver_days: float = 2, tz: str | None = None) -> list[dict]:
    """Что пора сделать ей самой. Клиентам ничего не уходит.
    [{"slot_id", "kind", "key", "text"}], kind: overdue | deadline_soon | deposit_unpaid | final_unpaid | deliver."""
    now = now or time.time()
    out = []
    for s in list_slots(conn, active_only=True):
        who = f"[{s['id']}] {s['client']}"
        deposit, final = _amounts(s)
        if s["status"] == "claimed" and s["deposit_pct"] > 0 and not s["deposit_paid_ts"] \
                and now - s["created_ts"] >= deposit_days * DAY:
            days = int((now - s["created_ts"]) // DAY)
            out.append({"slot_id": s["id"], "kind": "deposit_unpaid", "key": "deposit_unpaid",
                        "text": f"{who}: предоплата {sales.money(deposit, s['currency'])} не пришла {days} дн — "
                                "напомни или освободи слот"})
        if s["deadline_ts"]:
            key_ts = int(s["deadline_ts"])
            if s["deadline_ts"] < now:
                days = int((now - s["deadline_ts"]) // DAY)
                out.append({"slot_id": s["id"], "kind": "overdue", "key": f"overdue:{key_ts}",
                            "text": f"{who}: срок {_date(s['deadline_ts'], tz)} прошёл"
                                    + (f" {days} дн назад" if days else "") + " — напиши клиенту о новом сроке"})
            elif s["deadline_ts"] - now <= deadline_warn_days * DAY:
                days = (s["deadline_ts"] - now) / DAY
                out.append({"slot_id": s["id"], "kind": "deadline_soon", "key": f"deadline_soon:{key_ts}",
                            "text": f"{who}: срок {_date(s['deadline_ts'], tz)}, осталось {days:.0f} дн"
                                    if days >= 1 else f"{who}: срок {_date(s['deadline_ts'], tz)} — сегодня-завтра"})
        if s["status"] == "wip_sent" and not s["final_paid_ts"] and final > 0 \
                and now - s["status_ts"] >= final_days * DAY:
            days = int((now - s["status_ts"]) // DAY)
            out.append({"slot_id": s["id"], "kind": "final_unpaid", "key": "final_unpaid",
                        "text": f"{who}: WIP отправлен {days} дн назад, остаток {sales.money(final, s['currency'])} "
                                "не пришёл — напомни"})
        if s["status"] == "final_paid" and now - (s["final_paid_ts"] or s["status_ts"]) >= deliver_days * DAY:
            out.append({"slot_id": s["id"], "kind": "deliver", "key": "deliver",
                        "text": f"{who}: оплачено полностью — пора отдать работу"})
    order = {"overdue": 0, "deliver": 1, "deadline_soon": 2, "final_unpaid": 3, "deposit_unpaid": 4}
    return sorted(out, key=lambda r: order[r["kind"]])


def mark_reminded(conn, items: list[dict], now: float | None = None) -> None:
    """Эти напоминания она уже видела (фон показал) — второй раз не показываем."""
    if items:
        conn.executemany("INSERT OR IGNORE INTO comm_reminded (slot_id, key, ts) VALUES (?,?,?)",
                         [(r["slot_id"], r["key"], now or time.time()) for r in items])
        conn.commit()


def pending_reminders(conn, now: float | None = None, *, mark: bool = True, **days) -> list[dict]:
    """Для фоновой проверки: напоминания, которых она ещё не видела (каждое — один раз)."""
    now = now or time.time()
    fresh = [r for r in reminders(conn, now, **days)
             if not conn.execute("SELECT 1 FROM comm_reminded WHERE slot_id = ? AND key = ?",
                                 (r["slot_id"], r["key"])).fetchone()]
    if mark and fresh:
        conn.executemany("INSERT OR IGNORE INTO comm_reminded (slot_id, key, ts) VALUES (?,?,?)",
                         [(r["slot_id"], r["key"], now) for r in fresh])
        conn.commit()
    return fresh


# --- Загрузка ---

def _hours_left(s) -> float | None:
    if s["hours_left"] is not None:
        return float(s["hours_left"])
    if s["hours"] is None:
        return None
    return float(s["hours"]) * (AFTER_WIP_SHARE if s["status"] in ("wip_sent", "final_paid") else 1.0)


def capacity(conn, *, weekly_hours: float, now: float | None = None,
             extra: tuple[float | None, float] | None = None) -> dict:
    """Успевает ли она ко всем срокам при weekly_hours часов в неделю (раньше срок — раньше делается).
    extra — (срок, часы) слота, который она думает взять.

    {"ok": True/False/None (не знаю её часов), "hours_left", "must_this_week", "free_this_week",
     "conflicts": [{"slot_id", "deadline_ts", "need", "have"}] — будущие сроки, к которым не успеть,
     "overdue": [{"slot_id", "deadline_ts", "hours"}] — сроки уже прошли (их часы всё равно идут первыми),
     "unknown_hours", "active"}"""
    now = now or time.time()
    items, unknown, rows = _work_items(conn, now, extra)
    total = round(sum(h for _, h, _ in items), 2)
    out = {"ok": None, "hours_left": total, "must_this_week": None, "free_this_week": None, "conflicts": [],
           "overdue": [{"slot_id": sid, "deadline_ts": d, "hours": round(h, 1)} for d, h, sid in items if d < now],
           "unknown_hours": unknown, "active": len(rows), "weekly_hours": weekly_hours}
    if not weekly_hours or weekly_hours <= 0:
        return out
    cum = 0.0
    for d, h, sid in items:
        cum += h
        if d < now:
            continue  # уже просрочен — это не «не успеешь», а «уже не успела»: отдельно, в overdue
        have = weekly_hours * (d - now) / WEEK
        if cum > have + 1e-9:
            out["conflicts"].append({"slot_id": sid, "deadline_ts": d, "need": round(cum, 1), "have": round(have, 1)})
    must = _must_before(items, weekly_hours, now + WEEK)
    out["ok"] = not out["conflicts"]
    out["must_this_week"] = round(must, 2)
    out["free_this_week"] = round(max(0.0, weekly_hours - must), 2)
    return out


def _work_items(conn, now: float, extra: tuple[float | None, float] | None = None):
    """[(срок, часы осталось, id)] раньше срок — раньше; слоты без оценки часов — счётчиком unknown."""
    items, unknown = [], 0
    rows = list_slots(conn, active_only=True)
    for s in rows:
        h = _hours_left(s)
        if h is None:
            unknown += 1
            continue
        items.append((s["deadline_ts"] or now + NO_DEADLINE_DAYS * DAY, h, s["id"]))
    if extra is not None:
        items.append((extra[0] or now + NO_DEADLINE_DAYS * DAY, float(extra[1]), "new"))
    items.sort(key=lambda t: t[0])
    return items, unknown, rows


def _must_before(items, weekly_hours: float, until: float) -> float:
    """Сколько часов обязательно сделать до until, чтобы к каждому сроку успеть: после until до срока d
    можно сделать не больше weekly_hours·(d − until)/неделю."""
    cum, must = 0.0, 0.0
    for d, h, _ in items:
        cum += h
        must = max(must, cum - weekly_hours * max(0.0, (d - until) / WEEK))
    return must


def committed_hours(conn, since: float, until: float, *, weekly_hours: float = 0,
                    now: float | None = None) -> float:
    """Часы работы по слотам, которые обязательно лягут на [since, until) — для плана на неделю.

    weekly_hours > 0 — тот же расчёт, что must_this_week в capacity: до until нужно сделать всё со сроком
    до until и ту часть поздних слотов, которую после until уже не успеть; если since в будущем, вычитаем
    то, что она успеет до since. weekly_hours = 0 — просто часы слотов со сроком до until (и просроченных),
    без поздних слотов, — это нижняя граница. Слот без срока стоит через 4 недели, как в capacity.
    Слоты без оценки часов не входят (см. capacity unknown_hours).
    Для текущей недели проще взять capacity(...)["free_this_week"]."""
    now = now or time.time()
    items, _, _ = _work_items(conn, now)
    if weekly_hours and weekly_hours > 0:
        must = _must_before(items, weekly_hours, until)
        before = weekly_hours * max(0.0, since - now) / WEEK
        return round(max(0.0, must - before), 2)
    return round(sum(h for d, h, _ in items if d <= until), 2)


# --- Доход и статистика ---

def income(conn, since: float, until: float | None = None) -> dict:
    """Оплачено по YCH и коммишенам за период, по валютам: {валюта: {"total", "count", "net": {...}}}."""
    until = until or time.time()
    rows = conn.execute("SELECT * FROM sales WHERE source = 'commissions' AND status IN ('paid','transferred','done') "
                        "AND paid_ts IS NOT NULL AND paid_ts >= ? AND paid_ts < ?", (since, until)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        b = out.setdefault(r["currency"], {"total": 0.0, "count": 0, "net": {}})
        b["total"] = round(b["total"] + (r["amount"] or 0), 2)
        b["count"] += 1
        if r["net_amount"] is not None:
            nc = r["net_currency"] or "rub"
            b["net"][nc] = round(b["net"].get(nc, 0.0) + r["net_amount"], 2)
    return out


def stats(conn, now: float | None = None) -> dict:
    """Сданные слоты: сколько вовремя и сколько дней от заявки до сдачи — с опорой evidence."""
    now = now or time.time()
    ensure_tables(conn)
    rows = conn.execute("SELECT * FROM comm_slots WHERE status = 'delivered' AND delivered_ts IS NOT NULL").fetchall()
    with_deadline = [r for r in rows if r["deadline_ts"]]
    on_time = sum(1 for r in with_deadline if r["delivered_ts"] <= r["deadline_ts"])
    days = [(r["delivered_ts"] - r["created_ts"]) / DAY for r in rows]
    out = {"n": len(rows), "n_deadline": len(with_deadline), "on_time": on_time,
           "median_days": round(statistics.median(days), 1) if days else None, "evidence": None}
    if len(with_deadline) >= MIN_STATS:
        lo, hi = evidence.wilson(on_time, len(with_deadline))
        out["evidence"] = evidence.span([r["delivered_ts"] for r in with_deadline], "her_sales",
                                        "её сданные YCH и коммишены", None, "observed", interval=(lo, hi),
                                        measure="rate", value=on_time / len(with_deadline), data_as_of=now)
    return out


def render_stats(conn, now: float | None = None, days: float = 30) -> str:
    now = now or time.time()
    st = stats(conn, now)
    lines = ["YCH и коммишены"]
    inc = income(conn, now - days * DAY, now)
    if inc:
        bits = [f"{sales.money(b['total'], cur)} ({b['count']} оплат)" for cur, b in inc.items()]
        lines.append(f"Оплачено за {days:g} дн (её записи, предоплаты и остатки отдельно): " + ", ".join(bits))
    ev = st["evidence"]
    if ev is None:
        lines.append(evidence.refuse("вовремя ли ты сдаёшь", f"{st['n_deadline']} сданных со сроком", MIN_STATS))
    else:
        lo, hi = ev.interval
        lines.append(f"Вовремя сдано {st['on_time']} из {st['n_deadline']} ({evidence.pct(ev.value)}, "
                     f"80%-интервал {evidence.rate_interval(lo, hi)}) {evidence.tag(ev)}")
        lines.append(f"От заявки до сдачи — медиана {st['median_days']:g} дн (n={st['n']}).")
    return "\n".join(lines)


# --- Анонс: только факты ---

def announcement_facts(conn, offer_id: str, platform: str | None = None) -> dict:
    """Факты для анонса «YCH открыт». Текст она пишет сама; публикация — через обычные черновики и её «го».

    platform не назван — площадка самого предложения. Перевод предлагаем только для DA и X и никогда —
    если анонс или само предложение на Toyhouse (правило 4.8: тексты от ИИ, включая перевод, запрещены)."""
    offer = get_offer(conn, offer_id)
    if not offer:
        raise CommissionError("Такого предложения нет")
    platform = _platform(platform, offer["platform"])
    toyhouse = "toyhouse" in (platform, offer["platform"])
    facts = {"offer_id": offer["id"], "kind": offer["kind"], "kind_name": KIND_NAMES[offer["kind"]],
             "title": offer["title"], "price": sales.money(offer["price"], offer["currency"]),
             "price_value": offer["price"], "currency": offer["currency"], "slots_total": offer["slots"],
             "slots_left": slots_left(conn, offer["id"]), "deposit_pct": int(offer["deposit_pct"])
             if float(offer["deposit_pct"]).is_integer() else offer["deposit_pct"],
             "turnaround_days": offer["turnaround_days"], "platform": platform, "open": offer["status"] == "open",
             "translation_allowed": platform in ("deviantart", "x") and not toyhouse}
    if toyhouse:
        facts["toyhouse_rule"] = TOYHOUSE_RULE
    return facts


def render_announcement(f: dict) -> str:
    lines = [f"Факты для поста «{f['title']}» ({f['kind_name']}) — текст пишешь своими словами, я только собрала "
             "цифры:", f"- цена: {f['price']}"]
    if f["slots_total"]:
        lines.append(f"- мест: {f['slots_left']} из {f['slots_total']} свободно")
    lines.append(f"- предоплата: {f['deposit_pct']}%" if f["deposit_pct"] else "- без предоплаты")
    if f["turnaround_days"]:
        lines.append(f"- срок: до {f['turnaround_days']:g} дн")
    if not f["open"]:
        lines.append("- предложение сейчас закрыто")
    lines.append("Что вписать самой: пример работы, что входит, что не рисуешь, способ оплаты, ссылку на TOS.")
    if f.get("toyhouse_rule"):
        lines.append(f["toyhouse_rule"])
    elif f.get("translation_allowed") is True and f.get("platform") in ("deviantart", "x"):
        lines.append("Для DA и X: напишешь по-русски — переведу аккуратно, ты проверишь.")
    lines.append("Публикую только после твоего «го».")
    return "\n".join(lines)


# --- Виды ---

def _slot_status_name(s) -> str:
    if s["status"] == "claimed" and s["deposit_pct"] > 0:
        return "занят, предоплата есть" if s["deposit_paid_ts"] else "ждёт предоплату"
    if s["status"] == "in_progress" and s["deposit_pct"] > 0 and not s["deposit_paid_ts"]:
        return "в работе, предоплаты нет"
    return STATUS_NAMES[s["status"]]


def describe_slot(s, now: float, tz: str | None) -> str:
    what = s["what"] or KIND_NAMES[s["kind"]]
    due = ""
    if s["deadline_ts"]:
        left = (s["deadline_ts"] - now) / DAY
        due = (f" · срок {_date(s['deadline_ts'], tz)}"
               + (f" (просрочен на {-left:.0f} дн)" if left < 0 and s["status"] in ACTIVE
                  else f" (через {left:.0f} дн)" if s["status"] in ACTIVE else ""))
    return (f"[{s['id']}] {s['client']} — {KIND_NAMES[s['kind']]} «{what}», {sales.money(s['price'], s['currency'])}"
            f" · {_slot_status_name(s)}{due}")


def render_queue(conn, now: float | None = None, tz: str | None = None) -> str:
    now = now or time.time()
    rows = list_slots(conn, active_only=True)
    if not rows:
        return "Занятых слотов пока нет."
    lines = [f"Слоты в очереди: {len(rows)} (раньше срок — выше)"]
    lines += [f"- {describe_slot(s, now, tz)}" for s in rows]
    return "\n".join(lines)


def describe_offer(conn, o) -> str:
    left = slots_left(conn, o["id"])
    bits = [f"[{o['id']}] {KIND_NAMES[o['kind']]} «{o['title']}» — {sales.money(o['price'], o['currency'])}"]
    if o["slots"]:
        bits.append(f"мест {left} из {o['slots']}")
    bits.append(f"предоплата {o['deposit_pct']:g}%")
    if o["hours"]:
        bits.append(f"~{o['hours']:g} ч")
    if o["turnaround_days"]:
        bits.append(f"срок {o['turnaround_days']:g} дн")
    bits.append("открыт" if o["status"] == "open" else "закрыт")
    return ", ".join(bits)


def render_offers(conn, open_only: bool = False) -> str:
    rows = list_offers(conn, open_only=open_only)
    if not rows:
        return "Предложений пока нет. YCH или коммишен — sales:ych_offer (format ych|commission, title, amount, slots, hours)."
    return "Твои предложения:\n" + "\n".join(f"- {describe_offer(conn, o)}" for o in rows)


def render_capacity(cap: dict, tz: str | None = None) -> str:
    head = f"Осталось ~{cap['hours_left']:g} часов работы по {cap['active']} слотам"
    if cap["unknown_hours"]:
        head += f" (у {cap['unknown_hours']} не указаны часы — их не считаю)"
    if cap["ok"] is None:
        return head + ". Скажи, сколько часов в неделю ты успеваешь рисовать, — буду следить, чтобы слотов не " \
                      "набралось больше, чем успеваешь."
    lines = [head + ".", f"На этой неделе нужно ~{cap['must_this_week']:g} ч из {cap['weekly_hours']:g} — свободно "
             f"~{cap['free_this_week']:g} ч (по твоим оценкам часов)."]
    for o in cap.get("overdue") or []:
        lines.append(f"Срок {_date(o['deadline_ts'], tz)} у слота [{o['slot_id']}] уже прошёл (~{o['hours']:g} ч "
                     "работы) — договорись с клиентом о новом (sales:ych_slot date=…).")
    if cap["conflicts"]:
        lines.append(_overbook_text(cap["conflicts"][0], cap["weekly_hours"], tz))
    return "\n".join(lines)


def render_reminders(items: list[dict]) -> str:
    if not items:
        return "По слотам всё спокойно: напоминать нечего."
    return "Напоминания (клиентам пишешь сама):\n" + "\n".join(f"- {r['text']}" for r in items)


# --- Один вход для инструмента ---

ACTIONS = ("offers", "offer", "close", "open", "claim", "paid", "status", "cancel", "deadline", "hours", "queue",
           "remind", "capacity", "stats", "announce")
CLEAR_DEADLINE = ("нет", "-", "—", "none", "снять", "без срока")


def _need(**values) -> str:
    missing = [k for k, v in values.items() if v in (None, "", [], {})]
    return f"Нужно: {', '.join(missing)}." if missing else ""


def handle(conn, action: str, *, settings: Settings | None = None, now: float | None = None, id: str = "",
           kind: str = "", title: str = "", what: str = "", client: str = "", price: float | None = None,
           currency: str = "", slots: int | None = None, deposit_pct: float | None = None,
           hours: float | None = None, deadline: str = "", platform: str = "", part: str = "",
           net: float | None = None, pay_method: str = "", status: str = "", refund: bool = False, force: bool = False,
           turnaround_days: float | None = None, note: str = "") -> str:
    """Все действия с YCH и коммишенами одной функцией (для инструмента commissions)."""
    st = settings or Settings()
    now = now or time.time()
    ensure_tables(conn)

    def offer() -> str:
        need = _need(kind=kind, title=title, price=price)
        if need or price is None:
            return need
        oid = add_offer(conn, kind=kind, title=title, price=price, currency=currency or "rub", slots=slots,
                        deposit_pct=st.deposit_pct if deposit_pct is None else deposit_pct, hours=hours,
                        turnaround_days=turnaround_days, platform=platform or "other", note=note, now=now)
        return f"Сохранила: {describe_offer(conn, get_offer(conn, oid))}. Факты для анонса — sales:ych_announce."

    def claim_() -> str:
        need = _need(client=client)
        if need:
            return need
        got = claim(conn, client=client, what=what, offer_id=id or None, kind=kind or None, price=price,
                    currency=currency or None, deposit_pct=deposit_pct, deadline=deadline, hours=hours,
                    platform=platform or None, note=note, now=now, tz=st.tz, weekly_hours=st.weekly_hours,
                    force=force, default_deposit_pct=st.deposit_pct)
        s = get_slot(conn, got["slot_id"])
        lines = [f"Слот занят: {describe_slot(s, now, st.tz)}"]
        deposit, _ = _amounts(s)
        if deposit > 0:
            lines.append(f"Предоплата {sales.money(deposit, s['currency'])} — когда придёт, sales:ych_paid status=deposit.")
        lines += [f"Внимание: {w}" for w in got["warnings"]]
        if got["capacity"] and got["capacity"]["ok"]:
            lines.append(f"Загрузка: на этой неделе свободно ~{got['capacity']['free_this_week']:g} ч.")
        return "\n".join(lines)

    def paid() -> str:
        need = _need(id=id, part=part)
        if need:
            return need
        got = mark_paid(conn, id, part, net_amount=net, now=now, pay_method=pay_method or None)
        s = get_slot(conn, id)
        parts = got["parts"]
        if not parts:
            line = f"Сумма слота 0 — в продажи писать нечего. Слот: {_slot_status_name(s)}."
        else:
            if len(parts) == 2:
                label = "Оплата целиком (предоплата + остаток)"
            elif parts == ["final"]:
                label = "Остаток" if s["deposit_pct"] > 0 else "Оплата"
            else:
                label = "Оплата" if s["deposit_pct"] >= 100 else "Предоплата"
            line = (f"{label} {sales.money(got['amount'], s['currency'])} записана в продажи"
                    + (f" [{got['sale_id']}]" if got["sale_id"] else "") + f". Слот: {_slot_status_name(s)}.")
        return "\n".join([line, *[f"Внимание: {w}" for w in got["warnings"]]])

    def status_() -> str:
        need = _need(id=id, status=status)
        if need:
            return need
        warnings = set_status(conn, id, status, now=now)
        s = get_slot(conn, id)
        return "\n".join([f"Слот [{s['id']}]: {_slot_status_name(s)}.", *[f"Внимание: {w}" for w in warnings]])

    def cancel_() -> str:
        need = _need(id=id)
        if need:
            return need
        cancel(conn, id, refund_deposit=refund, now=now)
        return f"Слот [{id.strip()}] отменён" + (" — деньги вернула, из дохода убрала." if refund
                                                  else "; полученная предоплата осталась в доходе.")

    def deadline_() -> str:
        need = _need(id=id, deadline=deadline)
        if need:
            return need + " Снять срок — deadline=нет."
        clear = deadline.strip().lower() in CLEAR_DEADLINE
        ts = set_deadline(conn, id, "" if clear else deadline, now=now, tz=st.tz)
        return f"Срок слота [{id.strip()}]: {_date(ts, st.tz)}." if ts else f"Срок слота [{id.strip()}] снят."

    def hours_() -> str:
        need = _need(id=id)
        if need:
            return need
        set_hours_left(conn, id, hours)
        return f"Осталось по слоту [{id.strip()}]: " + (f"~{hours:g} ч." if hours is not None else "по оценке.")

    def open_close(is_open: bool) -> str:
        need = _need(id=id)
        if need:
            return need
        set_offer_open(conn, id, is_open)
        return f"Предложение [{id.strip()}] " + ("снова открыто." if is_open else "закрыто — новых слотов не беру.")

    def announce() -> str:
        need = _need(id=id)
        return need or render_announcement(announcement_facts(conn, id, platform or None))

    routes = {
        "offers": lambda: render_offers(conn),
        "offer": offer,
        "close": lambda: open_close(False),
        "open": lambda: open_close(True),
        "claim": claim_,
        "paid": paid,
        "status": status_,
        "cancel": cancel_,
        "deadline": deadline_,
        "hours": hours_,
        "queue": lambda: render_queue(conn, now, st.tz),
        "remind": lambda: render_reminders(reminders(
            conn, now, deposit_days=st.deposit_days, deadline_warn_days=st.deadline_warn_days,
            final_days=st.final_days, deliver_days=st.deliver_days, tz=st.tz)),
        "capacity": lambda: render_capacity(capacity(conn, weekly_hours=st.weekly_hours, now=now), st.tz),
        "stats": lambda: render_stats(conn, now),
        "announce": announce,
    }
    fn = routes.get((action or "").strip())
    if fn is None:
        return f"Действия: {', '.join(ACTIONS)}."
    try:
        return fn()
    except CommissionError as e:
        return f"Не вышло: {e}"

