"""Продажа её лотов: что делать с непроданным, когда кончается аукцион, кому напомнить об оплате.

Модуль только считает и готовит данные. Всё, что уйдёт на сайт (комментарий-напоминание),
становится черновиком и уходит только по её «го». Публичный текст — её шаблон слово в слово
(здесь подставляются только ник, сумма, название, дни). Числа в советах берутся из прогноза
(forecast) и рынка в базе; где данных нет — так и говорим, ничего не досчитываем «на глаз».
"""

import json
import re
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import auctions, db, forecast, sales, taxonomy

DAY = 86400
HOUR = 3600
DEFAULT_TZ = "Europe/Moscow"
AUCTION_TYPES = ("auction", "bta")
FIXED_TYPES = ("flatsale", "adopt")
SELL_TYPES = (*AUCTION_TYPES, *FIXED_TYPES, "ota")

STALE_DAYS = 7            # фикс-цена / аукцион без срока без ставок дольше — «не продаётся»
LOOKBACK_DAYS = 60        # старше — уже не разбираем
OPEN_WINDOW_DAYS = 45     # открытые аукционы, у которых ищем срок
ENDED_MEMORY_DAYS = 14    # «закончился, но не закрыт» — не дольше
MARKET_DAYS = 180         # рынок для сравнения форматов
MARKET_MIN_AGE_DAYS = 3   # свежие рыночные лоты ещё не успели закрыться
MIN_MARKET = 5            # меньше похожих лотов — вывод по формату не делаем
PRICE_GAP = 1.15          # её фикс-цена выше медианы проданных похожих на 15% — предлагаем снизить
REMIND_AFTER_DAYS = 2      # = payment_window_hours по умолчанию (48 ч)
PAYMENT_WINDOW_HOURS = 48
REMIND_COOLDOWN_DAYS = 3  # не чаще одного напоминания на продажу за столько дней
DRAFT_GUARD_S = DAY       # черновики не прочитать — сутки после черновика напоминания второй не готовим
REMIND_MAX_PER_SALE = 1   # одно напоминание покупателю; второе — уже давление, дальше решает она сама
TOO_MANY_REMINDERS = ("напоминание по этой продаже уже было — второе не готовлю: "
                      "дальше реши сама (написать лично, отдать второму по ставке или перевыставить)")
PLAUSIBLE_BEFORE = 2 * DAY
PLAUSIBLE_AFTER = 31 * DAY

SCHEMA = """
CREATE TABLE IF NOT EXISTS selling_templates (
    kind TEXT PRIMARY KEY,         -- см. TEMPLATE_SLOTS
    text TEXT NOT NULL,            -- её текст слово в слово
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_reminders (
    sale_id TEXT NOT NULL,
    ts REAL NOT NULL,
    status TEXT NOT NULL,          -- draft | sent
    PRIMARY KEY (sale_id, ts)
);
CREATE TABLE IF NOT EXISTS lot_alerts (
    deviation_id TEXT NOT NULL,
    kind TEXT NOT NULL,            -- soon | ended
    end_ts REAL NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (deviation_id, kind, end_ts)
);
"""


class SellingError(ValueError):
    pass


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _left(seconds: float) -> str:
    minutes = max(0, int(round(seconds / 60)))
    if minutes < 60:
        return f"{minutes} мин"
    h, m = divmod(minutes, 60)
    if h >= 48:
        return f"{h // 24} дн"
    return f"{h} ч" + (f" {m} мин" if m else "")


# --- Её шаблоны (слово в слово) ---

TEMPLATE_SLOTS = {"payment_reminder": ("buyer", "amount", "title", "days", "url"),
                  "pinglist": ("title", "url", "mentions"),
                  # Как заплатить (Boosty / поинты): её текст или DEFAULT_TEMPLATES["payment_info"].
                  "payment_info": ("buyer", "title", "amount", "boosty_url", "window_hours"),
                  # Предложение тёплому покупателю или ответ на LF-пост (outreach, leads) — её слова.
                  "offer": ("name", "title", "link", "why"),
                  # Пост в X через intent: она жмёт «Post» сама (слоты — как в xpost.TEMPLATE_SLOTS).
                  "x_post": ("title", "url", "price", "species"),
                  # Раффл: правила и итог (слоты — как в raffle.TEMPLATE_SLOTS).
                  "raffle": ("title", "prize", "ends", "method", "rules", "commitment"),
                  "raffle_winner": ("winner", "title", "prize", "seed", "commitment", "list_hash", "count",
                                    "entrants", "verify"),
                  # Статус на DA (user.manage) о новом лоте.
                  "status": ("title", "url", "price")}

# Текст по умолчанию — только для payment_info и только как ПРЕДЛОЖЕНИЕ: в черновик (комментарий, письмо) он
# не попадает никогда — сначала она сохраняет свой текст (set_template kind=payment_info). Факты — FAQ Boosty
# (обновлено 12.2025): метод оплаты «не для карт РФ», конвертация автоматическая (валюту доната не выбрать),
# обязателен 3-D Secure. Строка про DA Points — только если поинты есть в её способах оплаты.
DEFAULT_TEMPLATES = {
    "payment_info": (
        "Hi {buyer}! Thank you for {title}. You can pay {amount} on my Boosty: {boosty_url}\n"
        "- please choose the payment option for cards issued outside Russia (not the one for Russian cards)\n"
        "- the currency is converted automatically, your bank does the rest\n"
        "- your card needs 3-D Secure (the confirmation code from your bank)\n"
        "- PayPal is not available on Boosty, sorry!\n"
        "- if the payment fails, please don't retry many times in a row, just message me\n"
        "- after paying, send me your DA/TH name so I can match the payment\n"
        "{points_line}Please pay within {window_hours}h. Thank you!"),
}
DEFAULT_POINTS_LINE = "If your card doesn't work, DA Points are an option too. "
NO_BOOSTY_LINK = "link in my profile"
SLOT_RE = re.compile(r"\{(\w*)\}")


def set_template(conn, kind: str, text: str, now: float | None = None) -> None:
    """Сохранить её шаблон. Проверяем только слоты: сам текст не трогаем."""
    if kind not in TEMPLATE_SLOTS:
        raise SellingError(f"Шаблоны: {', '.join(TEMPLATE_SLOTS)}")
    if not (text or "").strip():
        raise SellingError("Шаблон пустой — нужен её текст слово в слово")
    allowed = TEMPLATE_SLOTS[kind]
    unknown = sorted({s for s in SLOT_RE.findall(text) if s not in allowed})
    if unknown:
        raise SellingError(f"Неизвестные поля: {', '.join('{' + u + '}' for u in unknown)}. "
                           f"Можно: {' '.join('{' + s + '}' for s in allowed)}")
    if SLOT_RE.findall(text).count("mentions") > 1:
        raise SellingError("{mentions} в шаблоне можно только один раз — иначе ники упомянутся дважды")
    ensure(conn)
    conn.execute("INSERT INTO selling_templates (kind, text, updated_ts) VALUES (?, ?, ?) ON CONFLICT(kind) "
                 "DO UPDATE SET text = excluded.text, updated_ts = excluded.updated_ts", (kind, text, now or time.time()))
    conn.commit()


def get_template(conn, kind: str) -> str | None:
    ensure(conn)
    row = conn.execute("SELECT text FROM selling_templates WHERE kind = ?", (kind,)).fetchone()
    return row["text"] if row else None


def template_or_default(conn, kind: str) -> tuple[str | None, bool]:
    """(текст, это текст по умолчанию?). Её шаблон важнее; по умолчанию есть только у payment_info."""
    text = get_template(conn, kind)
    if text:
        return text, False
    default = DEFAULT_TEMPLATES.get(kind)
    return default, default is not None


def payment_info(conn, *, buyer: str = "", title: str = "", amount: str = "", boosty_url: str = "",
                 window_hours: int = PAYMENT_WINDOW_HOURS, currency: str | None = None,
                 methods: list[str] | tuple[str, ...] | None = None) -> dict:
    """Как заплатить: {"text", "default": bool}. Без ссылки Boosty — «link in my profile».

    default=True — это НЕ её текст, а предложение: показать ей, в черновик не класть.
    Продажа за поинты — text=None: Boosty тут ни при чём, поинты переводят на DA."""
    if currency == "points":
        return {"text": None, "default": False}
    text, is_default = template_or_default(conn, "payment_info")
    if is_default and text:
        points = DEFAULT_POINTS_LINE if "points" in (methods or ()) else ""
        text = text.replace("{points_line}", points)
    values = {"buyer": buyer or "there", "title": title or "the adopt", "amount": amount or "",
              "boosty_url": boosty_url or NO_BOOSTY_LINK, "window_hours": window_hours}
    rendered = render_template(text or "", values)
    rendered = re.sub(r" {2,}", " ", rendered)
    return {"text": rendered, "default": is_default}


def render_template(text: str, values: dict) -> str:
    return SLOT_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


# --- Срок аукциона из её описания ---

@dataclass
class EndInfo:
    end_ts: float | None
    kind: str = ""            # absolute | relative | after_last_bid
    precision: str = ""       # minute | day
    tz: str | None = None
    tz_assumed: bool = False
    phrase: str = ""
    reason: str = ""


MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12, "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7,
          "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
# EST/PST и т.п. понимаем как местное время региона: летом «EST» почти всегда значит EDT.
TZ_ZONES = {"est": "America/New_York", "edt": "America/New_York", "cst": "America/Chicago",
            "cdt": "America/Chicago", "mst": "America/Denver", "mdt": "America/Denver",
            "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "hst": "Pacific/Honolulu",
            "gmt": "UTC", "utc": "UTC", "bst": "Europe/London", "cet": "Europe/Paris", "cest": "Europe/Paris",
            "msk": "Europe/Moscow", "мск": "Europe/Moscow", "jst": "Asia/Tokyo", "kst": "Asia/Seoul",
            "aest": "Australia/Sydney", "aedt": "Australia/Sydney"}
TZ_SHORT = {"ET": "America/New_York", "CT": "America/Chicago", "MT": "America/Denver", "PT": "America/Los_Angeles"}

END_KW = re.compile(r"\b(?:ends?|ending|end\s+date|closes?|closing|deadline|until|till)\b|"
                    r"заканчива\w*|законч\w*|закро\w*|окончани\w*|заверш\w*|дедлайн|(?<![\w])до(?![\w])", re.I)
AFTER_BID = re.compile(r"(\d{1,3})\s*(h|hrs?|hours?|ч|час\w*)\s*(?:after|from|since|после)\s+(?:the\s+)?"
                       r"(?:last|latest|final|последней)\s+(?:bid|ставки)", re.I)
PAY_BEFORE = re.compile(r"оплат|(?<![\w])pay|paid|payment|hold|холд|бронь|резерв|reserve", re.I)
_UNIT = r"(h|hrs?|hours?|d|days?|ч|час\w*|д|дн\w*|дня|сут\w*)"
REL_AFTER_KW = re.compile(r"^\W{0,3}(?:in|within|через)\s+(\d{1,3})\s*" + _UNIT + r"(?![\w])", re.I)
REL_STANDALONE = re.compile(r"\b(\d{1,3})\s*[-\s]?(h|hr|hour|day)s?\s+(?:long\s+)?auction\b|"
                            r"\bauction\s+(?:lasts|runs)\s+(?:for\s+)?(\d{1,3})\s*(h|hrs?|hours?|days?)\b|"
                            r"аукцион\s+на\s+(\d{1,3})\s*(ч|час\w*|д\w*|сут\w*)", re.I)
NUM_DATE = re.compile(r"(?<![\d.:/$€£,])(\d{1,2})([./])(\d{1,2})(?:\2(\d{4}|\d{2}))?(?![\d:/]|[.,]\d)")
ISO_DATE = re.compile(r"(?<![\d.])(\d{4})-(\d{2})-(\d{2})(?![\d])")
# День-месяц и месяц-день ищем отдельно: иначе «the 3rd of November» и «Saturday 7 November» съедаются
# ложным «месяцем» (the / Saturday) и настоящая дата не находится.
MONTH_DATE_DM = re.compile(r"(?<![\w])(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-zа-яё]{3,9})\.?(?:,?\s+(\d{4}))?", re.I)
MONTH_DATE_MD = re.compile(r"(?<![\w])([a-zа-яё]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?![\d:])(?:,?\s+(\d{4}))?", re.I)
# Полдень / полночь — той же длины, чтобы позиции в строке не съехали.
_WORD_TIMES = [(re.compile(r"(?<![\w])noon(?![\w])", re.I), "12pm"),
               (re.compile(r"(?<![\w])midnight(?![\w])", re.I), "11:59 pm"),
               (re.compile(r"(?<![\w])полдень(?![\w])", re.I), "12:00  "),
               (re.compile(r"(?<![\w])полночь(?![\w])", re.I), "23:59  ")]
# Время с точкой (20.00) — только после «at / в / @ / by» или перед am/pm/поясом и если это не может быть датой.
_TIME_LEAD = re.compile(r"(?:\bat|\bby|(?<![\w])в|@)\s*$", re.I)
TIME = re.compile(r"(?<![\d$€£])(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?![\d%])", re.I)
TZ_AFTER = re.compile(r"^\s*(?:по\s+|\(\s*)?(?:(?P<off>(?:gmt|utc)\s*[+\-−]\s*\d{1,2}(?::?\d{2})?)|"
                      r"(?P<msk>москв\w*|moscow\s+time)|(?P<name>[A-Za-zА-Яа-яЁё]{2,5}))(?![\w])", re.I)


def _unit_seconds(unit: str) -> int:
    u = unit.lower()
    return HOUR if u[0] in "hч" else DAY


def _month(word: str) -> int | None:
    w = word.lower()
    return MONTHS.get(w[:3]) or MONTHS.get(w)


def _tz_after(text: str) -> tuple[tzinfo | None, str | None, str]:
    """(tzinfo|None, имя, ошибка). Ошибка — пояс назван, но непонятно какой."""
    m = TZ_AFTER.match(text)
    if not m:
        return None, None, ""
    if m.group("off"):
        raw = re.sub(r"\s", "", m.group("off")).replace("−", "-")
        sign = -1 if "-" in raw else 1
        num = re.split(r"[+-]", raw)[1]
        hh, mm = (num.split(":") + ["0"])[:2] if ":" in num else ((num[:-2], num[-2:]) if len(num) > 2 else (num, "0"))
        return timezone(sign * timedelta(hours=int(hh), minutes=int(mm))), raw.upper(), ""
    if m.group("msk"):
        return ZoneInfo("Europe/Moscow"), "MSK", ""
    name = m.group("name")
    if name.lower() in TZ_ZONES:
        return ZoneInfo(TZ_ZONES[name.lower()]), name.upper(), ""
    if name in TZ_SHORT:
        return ZoneInfo(TZ_SHORT[name]), name, ""
    if name.isupper() and name.isalpha() and len(name) >= 3:
        return None, None, f"пояс {name} непонятен (несколько стран так пишут) — время не считаю"
    return None, None, ""


def _plausible_dates(day_month: list[tuple[int, int]], year: int | None, ref: float) -> list[tuple[int, int, int]]:
    ref_year = datetime.fromtimestamp(ref, UTC).year
    years = [year] if year else [ref_year - 1, ref_year, ref_year + 1]
    out = set()
    for d, m in day_month:
        for y in years:
            try:
                noon = datetime(y, m, d, 12, tzinfo=UTC).timestamp()
            except ValueError:
                continue
            if ref - PLAUSIBLE_BEFORE <= noon <= ref + PLAUSIBLE_AFTER:
                out.add((y, m, d))
    return sorted(out)


def _absolute(window: str, ref: float, zone_default: ZoneInfo, tz_name: str) -> tuple[EndInfo | None, str]:
    """Дата (+время, +пояс) в куске текста сразу после «ends». (EndInfo | None, причина отказа)."""
    found: list[tuple[re.Match[str], list[tuple[int, int]], int | None]] = []
    for m in ISO_DATE.finditer(window):
        found.append((m, [(int(m.group(3)), int(m.group(2)))], int(m.group(1))))
    window_num = ISO_DATE.sub(lambda m: " " * len(m.group(0)), window)
    for m in NUM_DATE.finditer(window_num):
        a, b = int(m.group(1)), int(m.group(3))
        year = m.group(4)
        year = (2000 + int(year) if len(year) == 2 else int(year)) if year else None
        # С точкой пишут день.месяц (03.11 = 3 ноября); месяц.день пробуем, только если день.месяц невозможен.
        dotted = m.group(2) == "."
        found.append((m, [(a, b)] if dotted or a == b else [(a, b), (b, a)], year))
        if dotted and a != b and not _plausible_dates([(a, b)], year, ref):
            found[-1] = (m, [(b, a)], year)
    for m in MONTH_DATE_DM.finditer(window):
        month = _month(m.group(2))
        if month:
            found.append((m, [(int(m.group(1)), month)], int(m.group(3)) if m.group(3) else None))
    for m in MONTH_DATE_MD.finditer(window):
        month = _month(m.group(1))
        if month:
            found.append((m, [(int(m.group(2)), month)], int(m.group(3)) if m.group(3) else None))
    if not found:
        tm = TIME.search(window)
        if tm and (tm.group(2) or tm.group(3)):
            return None, "указано только время, без даты"
        return None, ""
    m, options, year = min(found, key=lambda f: f[0].start())
    others = {tuple(pd[0]) for f in found if f[0] is not m and len(pd := _plausible_dates(f[1], f[2], ref)) == 1}
    first = _plausible_dates(options, year, ref)
    if len(first) == 1 and others - {first[0]}:
        return None, f"в сроке несколько дат («{m.group(0).strip()}» и ещё) — не угадываю, какая верная"
    dates = first
    if not dates:
        return None, f"дата «{m.group(0).strip()}» не похожа на срок этого лота"
    if len(dates) > 1:
        return None, f"дата «{m.group(0).strip()}» неоднозначна (день и месяц можно прочитать по-разному)"
    y, mo, d = dates[0]
    rest = window[:m.start()] + " " * (m.end() - m.start()) + window[m.end():]
    for pat, same_len in _WORD_TIMES:
        rest = pat.sub(same_len, rest)
    tm = None
    for t in TIME.finditer(rest):
        if not (t.group(2) or t.group(3)):
            continue
        if re.match(r"\d{1,2}\.\d{2}", t.group(0)) and not t.group(3):
            h, mi = int(t.group(1)), int(t.group(2))
            could_be_date = 1 <= h <= 31 and 1 <= mi <= 12
            lead = bool(_TIME_LEAD.search(rest[:t.start()]))
            zoned = _tz_after(rest[t.end():])[0] is not None
            if could_be_date or not (lead or zoned):
                continue  # «05.11» — вторая дата, «1.50 usd» — цена
        tm = t
        break
    after = rest[tm.end():] if tm else rest[m.end():]
    zone, zname, err = _tz_after(after)
    if err:
        return None, err
    assumed = zone is None
    zone = zone or zone_default
    zname = zname or tz_name
    if tm:
        h, mi = int(tm.group(1)), int(tm.group(2) or 0)
        ampm = (tm.group(3) or "").lower().replace(".", "")
        if ampm:
            if not 1 <= h <= 12:
                return None, f"время «{tm.group(0).strip()}» непонятно"
            h = h % 12 + (12 if ampm == "pm" else 0)
        if h > 23 or mi > 59:
            return None, f"время «{tm.group(0).strip()}» непонятно"
        end = datetime(y, mo, d, h, mi, tzinfo=zone).timestamp()
        precision = "minute"
    else:
        end = datetime(y, mo, d, 23, 59, tzinfo=zone).timestamp()
        precision = "day"
    return EndInfo(end, "absolute", precision, zname, assumed, m.group(0).strip()), ""


def parse_end(text: str | None, published_ts: float | None = None, last_bid_ts: float | None = None,
              tz: str = DEFAULT_TZ, now: float | None = None) -> EndInfo:
    """Явный срок аукциона из названия/описания: «ends 03.11 20:00 EST», «ends in 48h», «72 hour auction»,
    «24h after last bid», «closes Nov 5», «заканчивается 5 ноября в 20:00 МСК».

    Только явные и однозначные сроки. Пояс не указан — её пояс (tz), с пометкой tz_assumed. Дата без
    времени — конец дня (precision="day"). Несколько разных сроков, непонятный пояс (IST), дата, которую
    можно прочитать двумя способами, — end_ts=None и причина в reason."""
    text = text or ""
    ref = published_ts or now or time.time()
    zone_default = _zone(tz)
    tz_name = tz if tz else DEFAULT_TZ
    found: list[EndInfo] = []
    reasons: list[str] = []

    for m in AFTER_BID.finditer(text):
        span = int(m.group(1)) * _unit_seconds(m.group(2))
        if last_bid_ts:
            found.append(EndInfo(last_bid_ts + span, "after_last_bid", "minute", phrase=m.group(0)))
        else:
            reasons.append(f"«{m.group(0)}»: ставок нет — таймер ещё не начался")
    bid_spans = [(m.start(), m.end()) for m in AFTER_BID.finditer(text)]

    for m in REL_STANDALONE.finditer(text):
        n = next(g for g in (m.group(1), m.group(3), m.group(5)) if g)
        unit = next(g for g in (m.group(2), m.group(4), m.group(6)) if g)
        if published_ts:
            found.append(EndInfo(published_ts + int(n) * _unit_seconds(unit), "relative", "minute", phrase=m.group(0)))
        else:
            reasons.append("неизвестно, когда лот выложен")

    for kw in END_KW.finditer(text):
        if any(a <= kw.start() < b for a, b in bid_spans):
            continue
        if any(kw.end() <= a <= kw.end() + 12 for a, _ in bid_spans):
            continue  # «ends in 24h after the last bid» — это таймер от ставки, он уже учтён
        if PAY_BEFORE.search(text[max(0, kw.start() - 15):kw.start()]):
            continue  # «оплата до 10.11», «pay until …» — срок оплаты, а не конец аукциона
        window = re.split(r"[\n|;]", text[kw.end():kw.end() + 60], maxsplit=1)[0]
        if AFTER_BID.match(window.strip()):
            continue
        rel = REL_AFTER_KW.match(window)
        if rel:
            if published_ts:
                found.append(EndInfo(published_ts + int(rel.group(1)) * _unit_seconds(rel.group(2)), "relative",
                                     "minute", phrase=(kw.group(0) + rel.group(0)).strip()))
            else:
                reasons.append("неизвестно, когда лот выложен")
            continue
        # Окно до следующего «ends»: иначе «03.11 … Edit: ends 05.11» склеится в одну фразу.
        nxt = END_KW.search(window, 1)
        info, why = _absolute(window[:nxt.start()] if nxt else window, ref, zone_default, tz_name)
        if info:
            info.phrase = (kw.group(0) + " " + info.phrase).strip()
            found.append(info)
        elif why:
            reasons.append(why)

    distinct: list[EndInfo] = []
    for f in found:
        if not any(abs((f.end_ts or 0) - (d.end_ts or 0)) < 60 for d in distinct):
            distinct.append(f)
    if len(distinct) > 1:
        return EndInfo(None, reason="в описании несколько разных сроков: "
                                    + ", ".join(f"«{d.phrase}»" for d in distinct[:3]))
    if distinct:
        info = distinct[0]
        if info.tz_assumed:
            info.reason = f"пояс не указан — считаю по твоему ({tz_name})"
        if info.precision == "day":
            info.reason = (info.reason + "; " if info.reason else "") + "время не указано — считаю до конца дня"
        return info
    if reasons:
        return EndInfo(None, reason=reasons[0])
    return EndInfo(None, reason="срок не указан явно")


# --- Её открытые аукционы: скоро конец / уже конец ---

def _open_auctions(conn, now: float) -> list:
    types = ",".join(f"'{t}'" for t in AUCTION_TYPES)
    return conn.execute(
        "SELECT i.*, a.n_bids AS a_n_bids, a.top_amount AS a_top, a.currency AS a_currency, a.last_bid_ts AS a_last, "
        "a.ended AS a_ended, a.extra AS a_extra FROM items i LEFT JOIN auctions a ON a.deviation_id = i.id "
        f"WHERE i.source = 'deviantart' AND i.own = 1 AND i.lot_type IN ({types}) "
        "AND COALESCE(i.status, '') != 'closed' AND COALESCE(a.ended, 0) = 0 "
        "AND COALESCE(i.published_ts, i.first_seen_ts) >= ? ORDER BY COALESCE(i.published_ts, i.first_seen_ts)",
        (now - OPEN_WINDOW_DAYS * DAY,)).fetchall()


def _top_user(row) -> str | None:
    try:
        return json.loads(row["a_extra"] or "{}").get("top_user")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None


def lot_end(row, tz: str = DEFAULT_TZ) -> EndInfo:
    text = f"{row['title'] or ''}\n{row['snippet'] or ''}"
    return parse_end(text, row["published_ts"] or row["first_seen_ts"], row["a_last"], tz)


def item_end(conn, item_id: str | None, tz: str = DEFAULT_TZ) -> EndInfo | None:
    """Срок её лота по описанию (как lot_end) — по id в базе; None, если лота нет."""
    if not item_id:
        return None
    row = conn.execute("SELECT i.*, a.last_bid_ts AS a_last FROM items i LEFT JOIN auctions a "
                       "ON a.deviation_id = i.id WHERE i.id = ?", (item_id,)).fetchone()
    return lot_end(row, tz) if row else None


def end_text(info: EndInfo, tz: str = DEFAULT_TZ) -> str:
    """«04.11 18:00» или «конца 04.11» (если в описании только дата)."""
    local = datetime.fromtimestamp(info.end_ts or 0, _zone(tz))
    return f"конца {local:%d.%m}" if info.precision == "day" else f"{local:%d.%m %H:%M}"


def _bids_text(row) -> str:
    n = int(row["a_n_bids"] or 0)
    if row["a_n_bids"] is None:
        return "ставки ещё не читались"
    if not n:
        return "ставок пока нет"
    user = _top_user(row)
    return f"ставок {n}, максимум {sales.money(row['a_top'], row['a_currency'])}" + (f" у {user}" if user else "")


def _lot_dict(row, info: EndInfo) -> dict:
    return {"item_id": row["id"], "title": row["title"], "url": row["url"], "uuid": auctions.known_uuid(row),
            "end_ts": info.end_ts, "precision": info.precision, "n_bids": int(row["a_n_bids"] or 0),
            "top": row["a_top"], "currency": row["a_currency"], "top_user": _top_user(row)}


def _unseen(conn, kind: str, rows: list[dict], now: float, limit: int | None = None) -> list[dict]:
    """Отдаёт ещё не показанные (не больше limit) и помечает показанными только их — остальные придут в следующий раз."""
    ensure(conn)
    out: list[dict] = []
    for r in rows:
        if limit is not None and len(out) >= limit:
            break
        if conn.execute("SELECT 1 FROM lot_alerts WHERE deviation_id = ? AND kind = ? AND end_ts = ?",
                        (r["item_id"], kind, r["end_ts"])).fetchone():
            continue
        cur = conn.execute("INSERT OR IGNORE INTO lot_alerts (deviation_id, kind, end_ts, ts) VALUES (?, ?, ?, ?)",
                           (r["item_id"], kind, r["end_ts"], now))
        if cur.rowcount:
            out.append(r)
    conn.commit()
    return out


def ending_soon(conn, now: float | None = None, within_h: float = 3, tz: str = DEFAULT_TZ,
                unseen_only: bool = False, limit: int | None = None) -> list[dict]:
    """Её аукционы, которые закончатся в ближайшие within_h часов (по явному сроку в описании).

    unseen_only=True — для фоновых уведомлений: каждый лот (с этим сроком) отдаётся один раз;
    limit — сколько показать за раз (остальные не помечаются и придут в следующий запуск)."""
    now = now or time.time()
    out = []
    for row in _open_auctions(conn, now):
        info = lot_end(row, tz)
        if info.end_ts is None or not now < info.end_ts <= now + within_h * HOUR:
            continue
        d = _lot_dict(row, info)
        d["left_s"] = info.end_ts - now
        when = "сегодня последний день (время не указано)" if info.precision == "day" else \
            f"закончится через {_left(d['left_s'])}"
        d["text"] = f"«{row['title'][:60]}» {when}: {_bids_text(row)}"
        out.append(d)
    out.sort(key=lambda d: d["end_ts"])
    return _unseen(conn, "soon", out, now, limit) if unseen_only else out[:limit]


def ended_unclosed(conn, now: float | None = None, tz: str = DEFAULT_TZ, unseen_only: bool = False,
                   limit: int | None = None) -> list[dict]:
    """Её аукционы, срок которых прошёл, а лот не закрыт (нет «sold to», статус не closed).
    Для вопроса «закрыть одной командой?». winner_guess — кто ставил больше всех (проверить глазами)."""
    now = now or time.time()
    out = []
    for row in _open_auctions(conn, now):
        info = lot_end(row, tz)
        if info.end_ts is None or not now - ENDED_MEMORY_DAYS * DAY <= info.end_ts <= now:
            continue
        d = _lot_dict(row, info)
        d["ended_ago_s"] = now - info.end_ts
        d["winner_guess"] = d["top_user"] if d["n_bids"] else None
        head = f"«{row['title'][:60]}» закончился {_left(d['ended_ago_s'])} назад"
        if d["n_bids"]:
            d["text"] = f"{head}: {_bids_text(row)}. Закрыть одной командой?"
        else:
            d["text"] = f"{head} без ставок — посмотри варианты, что с ним делать."
        out.append(d)
    out.sort(key=lambda d: d["end_ts"])
    return _unseen(conn, "ended", out, now, limit) if unseen_only else out[:limit]


def auction_ends(conn, now: float | None = None, tz: str = DEFAULT_TZ) -> list[dict]:
    """Все её открытые аукционы: срок или причина, почему его нет."""
    now = now or time.time()
    out = []
    for row in _open_auctions(conn, now):
        info = lot_end(row, tz)
        d = _lot_dict(row, info)
        d.update(reason=info.reason, bids=_bids_text(row))
        out.append(d)
    return out


def render_ends(rows: list[dict], tz: str = DEFAULT_TZ, now: float | None = None) -> str:
    if not rows:
        return "Открытых аукционов нет."
    now = now or time.time()
    zone = _zone(tz)
    lines = [f"Открытые аукционы ({tz}):"]
    for r in rows:
        if r["end_ts"]:
            local = datetime.fromtimestamp(r["end_ts"], zone)
            when = local.strftime("%d.%m") + ("" if r["precision"] == "day" else local.strftime(" %H:%M"))
            when += " (прошёл)" if r["end_ts"] <= now else ""
        else:
            when = f"срок ? — {r['reason']}"
        note = f" · {r['reason']}" if r["end_ts"] and r["reason"] else ""
        lines.append(f"- «{r['title'][:50]}» — {when}; {r['bids']}{note}")
    return "\n".join(lines)


# --- Непроданное: варианты из данных ---

def _unit(row) -> tuple[str | None, float | None]:
    if row["price_usd"] is not None:
        return "usd", float(row["price_usd"])
    if row["price_points"] is not None:
        return "points", float(row["price_points"])
    return None, None


def _similar(t: taxonomy.Tags, mine: taxonomy.Tags) -> bool:
    if mine.is_empty():
        return True
    if mine.species:
        return bool(set(t.species) & set(mine.species))
    keys = set(mine.styles) | set(mine.themes)
    return bool(keys & (set(t.styles) | set(t.themes)))


def _market(conn, lot_type: str, tags: taxonomy.Tags, now: float) -> list:
    rows = conn.execute(
        "SELECT * FROM items WHERE source = 'deviantart' AND own = 0 AND lot_type = ? "
        "AND COALESCE(published_ts, first_seen_ts) BETWEEN ? AND ?",
        (lot_type, now - MARKET_DAYS * DAY, now - MARKET_MIN_AGE_DAYS * DAY)).fetchall()
    # Как в прогнозе (forecast, demand_by_tag): без лотов, найденных только поиском (их редко перечитывают —
    # «закрыт» у них занижен), и теги по названию и тегам, без описания.
    return [r for r in rows if not forecast.is_search(r["place"]) and _similar(forecast.analytics_tags(r), tags)]


def _candidate_at(plan: forecast.PricePlan, price: float) -> dict | None:
    for c in plan.candidates:
        if c["lo"] <= price and (c["hi"] is None or price < c["hi"]):
            return c
    return None


def _pct(p: float) -> str:
    return f"{round(p * 100)}%"


def _verdict(row, now: float, stale_days: int, unread: list, tz: str) -> str | None:
    """Почему лот считается непроданным, или None."""
    age_days = int((now - (row["published_ts"] or row["first_seen_ts"])) // DAY)
    if row["lot_type"] in AUCTION_TYPES:
        if row["a_n_bids"] is None:
            unread.append(row["id"])
            return None
        if int(row["a_n_bids"]):
            return None
        if row["status"] == "closed" or row["a_ended"]:
            return "закрылся без ставок"
        info = parse_end(f"{row['title']}\n{row['snippet'] or ''}", row["published_ts"] or row["first_seen_ts"],
                         row["a_last"], tz, now)
        if info.end_ts is not None:
            return "аукцион закончился без ставок" if info.end_ts <= now else None
        return f"{age_days} дн без ставок" if age_days >= stale_days else None
    if row["status"] == "closed":
        return None
    return f"висит {age_days} дн" if age_days >= stale_days else None


def unsold(conn, now: float | None = None, tz: str = DEFAULT_TZ, stale_days: int = STALE_DAYS,
           limit: int = 5) -> dict:
    """Её непроданные лоты (аукцион без ставок, давно висящая фикс-цена/OTA) и варианты с числами из данных.

    {"lots": [{"item_id","title","url","lot_type","unit","price","why","options":[{kind,text,...}],"notes":[...]}],
     "total": сколько всего, "unread": сколько аукционов без прочитанных ставок, "best_time": dict|None}
    Варианты: lower_start / to_auction (прогноз старта), lower_price (медиана проданных похожих фикс-цен),
    to_ota (доля закрытых похожих OTA), bundle (с похожими непроданными), repost (лучшее время)."""
    now = now or time.time()
    types = ",".join(f"'{t}'" for t in SELL_TYPES)
    rows = conn.execute(
        "SELECT i.*, a.n_bids AS a_n_bids, a.ended AS a_ended, a.last_bid_ts AS a_last FROM items i "
        "LEFT JOIN auctions a ON a.deviation_id = i.id "
        f"WHERE i.source = 'deviantart' AND i.own = 1 AND i.lot_type IN ({types}) "
        "AND COALESCE(i.published_ts, i.first_seen_ts) BETWEEN ? AND ? "
        "AND NOT EXISTS (SELECT 1 FROM sales s WHERE s.item_id = i.id AND s.status != 'cancelled') "
        "ORDER BY COALESCE(i.published_ts, i.first_seen_ts) DESC",
        (now - LOOKBACK_DAYS * DAY, now)).fetchall()
    unread: list[str] = []
    picked = [(r, why) for r in rows if (why := _verdict(r, now, stale_days, unread, tz))]
    best_time = _best_time(conn, now, tz)
    plans: dict = {}
    lots = []
    for i, (row, why) in enumerate(picked):
        unit, price = _unit(row)
        tags = taxonomy.tag_item(row)
        lot = {"item_id": row["id"], "title": row["title"], "url": row["url"], "lot_type": row["lot_type"],
               "unit": unit, "price": price, "why": why, "tags": tags, "options": [], "notes": []}
        if i < limit:  # прогноз тяжёлый: считаем только для показанных
            _price_options(conn, lot, row, tags, now, plans)
            _format_options(conn, lot, row, tags, now)
            _time_option(lot, row, best_time, tz)
        lots.append(lot)
    _bundle_options(lots[:limit], lots)
    for lot in lots:
        lot.pop("tags")
    return {"lots": lots[:limit], "total": len(lots), "unread": len(unread), "best_time": best_time}


def _price_options(conn, lot: dict, row, tags, now: float, plans: dict) -> None:
    unit, price = lot["unit"], lot["price"]
    if not unit:
        lot["notes"].append("цена не указана — старт по прогнозу не сравнить")
        return
    key = (tuple(tags.species), tuple(tags.styles), tuple(tags.themes), unit)
    if key not in plans:
        plans[key] = forecast.price_plan(conn, species=tags.species, styles=tags.styles, themes=tags.themes,
                                         unit=unit, now=now)
    plan = plans[key]
    if not plan.best:
        lot["notes"].append("прогноз старта: " + ("; ".join(plan.caveats) or "данных нет"))
        return
    best = plan.best
    base = {"start": best["start"], "p_sold": best["p_sold"], "ratio": best["ratio"], "expected": best["expected"],
            "confidence": plan.confidence, "n": plan.pool_n, "unit": unit, "k": best.get("k"),
            "n_start": best.get("n")}
    # Наблюдение — «k из n при этом старте» (с интервалом), модель — отдельно и с пометкой «по модели».
    pool = "похожих аукционов" if plan.niche_pool else "аукционов рынка в целом"
    seen = (f"ставку получили {best['k']} из {best['n']} {pool}"
            + (f" (80%: {_pct(best['ci'][0])}–{_pct(best['ci'][1])})" if best.get("ci") else "")
            if best.get("n") else f"по {plan.pool_n} аукционам")
    model = f"по модели ~{_pct(best['p_sold'])}"
    if row["lot_type"] in AUCTION_TYPES:
        cur = _candidate_at(plan, price)
        if best["start"] < price:
            p_now = cur["p_sold"] if cur else None
            now_txt = ""
            if cur and cur.get("n"):
                now_txt = f"; при {sales.money(price, unit)} — {cur['k']} из {cur['n']}"
            elif p_now is not None:
                now_txt = f"; при {sales.money(price, unit)} по модели ~{_pct(p_now)}"
            lot["options"].append({**base, "kind": "lower_start", "p_now": p_now,
                                   "text": f"старт {sales.money(best['start'], unit)}: {seen}, {model}{now_txt}; "
                                           f"уверенность {plan.confidence}"})
        else:
            lot["notes"].append(f"старт не выше прогноза ({sales.money(best['start'], unit)}) — дело не в цене")
    else:
        lot["options"].append({**base, "kind": "to_auction",
                               "text": f"аукцион со стартом {sales.money(best['start'], unit)}: {seen}, {model}; "
                                       f"уверенность {plan.confidence}"})
    for c in plan.caveats:
        lot["notes"].append(c)


def _format_options(conn, lot: dict, row, tags, now: float) -> None:
    if row["lot_type"] != "ota":
        ota = _market(conn, "ota", tags, now)
        if len(ota) >= MIN_MARKET:
            closed = sum(1 for r in ota if r["status"] == "closed")
            lot["options"].append({"kind": "to_ota", "n": len(ota), "closed": closed, "rate": closed / len(ota),
                                   "confidence": forecast.confidence(len(ota)),
                                   "text": f"OTA: похожие OTA за {MARKET_DAYS} дн закрыты {closed} из {len(ota)} "
                                           f"({_pct(closed / len(ota))}; закрыт обычно = ушёл, но не всегда), "
                                           f"уверенность {forecast.confidence(len(ota))}"})
    if row["lot_type"] in FIXED_TYPES and lot["unit"]:
        col = "price_usd" if lot["unit"] == "usd" else "price_points"
        sold = [r[col] for r in _market(conn, row["lot_type"], tags, now) if r["status"] == "closed" and r[col]]
        if len(sold) >= MIN_MARKET:
            med = forecast.round_price(statistics.median(sold), lot["unit"])
            if lot["price"] > med * PRICE_GAP:
                lot["options"].append({"kind": "lower_price", "price": med, "n": len(sold), "unit": lot["unit"],
                                       "confidence": forecast.confidence(len(sold)),
                                       "text": f"цена {sales.money(med, lot['unit'])}: медиана закрытых похожих "
                                               f"({len(sold)} лотов), у тебя {sales.money(lot['price'], lot['unit'])}"})


def _best_time(conn, now: float, tz: str) -> dict | None:
    try:
        from . import advisor

        return advisor.best_publish_time(conn, now, tz)
    except Exception:  # noqa: BLE001 — совет про время необязателен
        return None


def _time_option(lot: dict, row, best: dict | None, tz: str) -> None:
    if not best or not row["published_ts"] or best.get("level") in ("нет вывода", "низкая"):
        return
    from .advisor import _slot

    if _slot(row["published_ts"], _zone(tz)) == tuple(best["slot"]):
        return  # уже выложен в лучшее окно — перевыкладывать незачем
    lot["options"].append({"kind": "repost", "weekday": best["weekday"], "hours": best["hours"],
                           "text": f"перевыложить: {best['weekday_name']} {best['hours']} ({tz}) — {best['evidence']}"})


def _bundle_options(shown: list[dict], everything: list[dict]) -> None:
    for lot in shown:
        t = lot["tags"]
        key = set(t.species) or set(t.styles) | set(t.themes)
        if not key:
            continue
        mates = [o for o in everything if o is not lot and key & (set(o["tags"].species) or
                                                             set(o["tags"].styles) | set(o["tags"].themes))]
        if not mates:
            continue
        same_unit = [o for o in mates if o["unit"] == lot["unit"] and o["price"]]
        total = (lot["price"] or 0) + sum(o["price"] for o in same_unit) if lot["price"] else None
        sums = f", старты вместе {sales.money(total, lot['unit'])}" if total and len(same_unit) == len(mates) else ""
        lot["options"].append({"kind": "bundle", "with": [o["item_id"] for o in mates[:3]],
                               "text": f"набор с {', '.join('«' + o['title'][:40] + '»' for o in mates[:3])}"
                                       f"{sums}; цену набора по данным не посчитать — продаж наборов в базе нет"})


def render_unsold(res: dict) -> str:
    if not res["lots"]:
        tail = f" (у {res['unread']} аукционов ставки ещё не читались)" if res.get("unread") else ""
        return "Непроданных лотов за последние недели нет." + tail
    lines = [f"Не продалось: {res['total']}" + (f", показаны {len(res['lots'])}" if res["total"] > len(res["lots"]) else "")]
    for i, lot in enumerate(res["lots"], 1):
        price = sales.money(lot["price"], lot["unit"]) if lot["unit"] else "без цены"
        lines.append(f"{i}. «{lot['title'][:60]}» — {lot['lot_type']} {price}, {lot['why']}")
        lines += [f"   • {o['text']}" for o in lot["options"]]
        lines += [f"   ~ {n}" for n in lot["notes"][:2]]
    lines.append("")  # оговорки — последним абзацем: при обрезке длинного списка он остаётся
    if res.get("unread"):
        lines.append(f"У {res['unread']} аукционов ставки ещё не читались — их не считаю.")
    lines.append("Числа — из её данных и рынка в базе; пересказывай без округлений и смягчений.")
    return "\n".join(lines)


# --- Напоминания об оплате ---

def _reminder_marks(conn, sale_id: str) -> tuple[float | None, float | None]:
    ensure(conn)
    row = conn.execute("SELECT MAX(CASE WHEN status = 'sent' THEN ts END) AS sent, "
                       "MAX(CASE WHEN status = 'draft' THEN ts END) AS draft FROM payment_reminders WHERE sale_id = ?",
                       (sale_id,)).fetchone()
    return row["sent"], row["draft"]


def _live_draft(conn, ref: str) -> bool | None:
    """Есть ли неотправленный черновик с этим ref (ждёт «го», запланирован, отправляется). None — не узнать."""
    try:
        rows = conn.execute("SELECT payload FROM drafts WHERE status IN ('pending', 'scheduled', 'sending')").fetchall()
    except Exception:  # noqa: BLE001 — таблицы черновиков может не быть
        return None
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(p, dict) and (p.get("ref") == ref or ref in (p.get("refs") or [])):
            return True
    return False


def reminder_block(conn, sale_id: str, now: float | None = None) -> str:
    """Проверка прямо перед отправкой напоминания: "" — можно; иначе причина (уже оплачено, недавно напоминали)."""
    now = now or time.time()
    sale = conn.execute("SELECT status FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if not sale:
        return "продажи нет в журнале"
    if sale["status"] != "won":
        return "продажа уже не ждёт оплаты"
    sent, _draft = _reminder_marks(conn, sale_id)
    if sent and now - sent < REMIND_COOLDOWN_DAYS * DAY:
        return f"напоминание уже было {int((now - sent) // DAY)} дн назад"
    if _reminders_sent(conn, sale_id) >= REMIND_MAX_PER_SALE:
        return TOO_MANY_REMINDERS
    return ""


def _reminders_sent(conn, sale_id: str) -> int:
    ensure(conn)
    return conn.execute("SELECT COUNT(*) FROM payment_reminders WHERE sale_id = ? AND status = 'sent'",
                        (sale_id,)).fetchone()[0]


def unmark_reminded(conn, sale_id: str, ts: float) -> None:
    """Напоминание точно не ушло: снять отметку «sent», поставленную перед запросом."""
    ensure(conn)
    conn.execute("DELETE FROM payment_reminders WHERE sale_id = ? AND ts = ? AND status = 'sent'", (sale_id, ts))
    conn.commit()


def mark_reminded(conn, sale_id: str, status: str = "sent", now: float | None = None) -> None:
    """status="draft" — черновик напоминания создан (сутки не готовим второй); "sent" — ушло (3 дня тишины)."""
    if status not in ("draft", "sent"):
        raise SellingError("status: draft или sent")
    ensure(conn)
    conn.execute("INSERT OR REPLACE INTO payment_reminders (sale_id, ts, status) VALUES (?, ?, ?)",
                 (sale_id, now or time.time(), status))
    conn.commit()


def payment_reminders(conn, now: float | None = None, older_than_days: float | None = None,
                      limit: int = 10, payment_window_hours: float = PAYMENT_WINDOW_HOURS,
                      boosty_url: str = "", payment_methods: list[str] | tuple[str, ...] = ()) -> dict:
    """Продажи «ждёт оплаты» дольше срока оплаты → кому можно напомнить (одно напоминание на продажу).

    Срок — payment_window_hours (её настройка, по умолчанию 48 ч); older_than_days, если задан, важнее.

    {"due": [{"sale_id","title","buyer","amount","days","item_id","deviation_url","deviation_uuid","text","ref"}],
     "skipped": [{"sale_id","title","buyer","reason"}], "template": bool, "more": int, "older_than_days"}
    text — её шаблон payment_reminder со слотами; None, если шаблона нет (тогда черновик не готовим).
    payment_info — её шаблон payment_info со слотами (можно дописать к её напоминанию) или None.
    payment_info_suggestion — текст по умолчанию, если её шаблона нет: только показать ей, в черновик НЕ класть
    (публичный текст — только её слова). Для продаж за поинты оба None.
    Продажи с непроверенным покупателем или суммой (запись по ставкам) — в skipped: сначала проверить."""
    now = now or time.time()
    if older_than_days is None:
        older_than_days = max(0.0, float(payment_window_hours)) / 24
    template = get_template(conn, "payment_reminder")
    due, skipped = [], []
    for s in sales.unpaid(conn, older_than_days, now):
        days = int((now - s["created_ts"]) // DAY)
        base = {"sale_id": s["id"], "title": s["title"], "buyer": s["buyer"],
                "amount": sales.money(s["amount"], s["currency"]) if s["amount"] is not None else "?", "days": days}
        sent, draft = _reminder_marks(conn, s["id"])
        if sent and now - sent < REMIND_COOLDOWN_DAYS * DAY:
            skipped.append({**base, "reason": f"напоминание было {int((now - sent) // DAY)} дн назад"})
            continue
        if _reminders_sent(conn, s["id"]) >= REMIND_MAX_PER_SALE:
            skipped.append({**base, "reason": TOO_MANY_REMINDERS})
            continue
        # Пока черновик жив (ждёт «го», запланирован, отправляется) — второй не готовим. Отменённый или
        # просроченный не мешает; сутки страховки — только если черновики не прочитать.
        if draft and (not sent or draft > sent):
            live = _live_draft(conn, f"payment:{s['id']}")
            if live or (live is None and now - draft < DRAFT_GUARD_S):
                skipped.append({**base, "reason": "черновик напоминания уже есть (отменить — drafts:cancel)"})
                continue
        if s["amount"] is None or s["note"] == sales.CHECK_AMOUNT:
            skipped.append({**base, "reason": "сначала проверь сумму — записана по ставкам и не подтверждена"})
            continue
        if s["note"] == sales.CHECK_BUYER:
            skipped.append({**base, "reason": "сначала проверь покупателя — записан тот, кто ставил больше всех"})
            continue
        if s["platform"] != "deviantart" or not (s["item_id"] or "").startswith("da:"):
            skipped.append({**base, "reason": "не DeviantArt — напомни сама там, где договаривались"})
            continue
        if not s["buyer"]:
            skipped.append({**base, "reason": "покупатель не записан"})
            continue
        item = db.get_item(conn, s["item_id"])
        url = item["url"] if item else None
        values = {**base, "url": url or "", "days": days}
        info = payment_info(conn, buyer=s["buyer"], title=s["title"], amount=base["amount"], boosty_url=boosty_url,
                            window_hours=int(payment_window_hours), currency=s["currency"], methods=payment_methods)
        own = info["text"] if not info["default"] else None
        due.append({**base, "item_id": s["item_id"], "deviation_url": url,
                    "deviation_uuid": auctions.known_uuid(item) if item else None,
                    "text": render_template(template, values) if template else None, "ref": f"payment:{s['id']}",
                    "payment_info": own, "payment_info_default": bool(info["default"] and info["text"]),
                    "payment_info_suggestion": info["text"] if info["default"] else None})
    return {"due": due[:limit], "skipped": skipped, "template": bool(template), "more": max(0, len(due) - limit),
            "older_than_days": older_than_days}


def render_reminders(res: dict) -> str:
    if not res["due"] and not res["skipped"]:
        days = res.get("older_than_days", REMIND_AFTER_DAYS)
        return f"Неоплаченных продаж старше {days:g} дн нет."
    lines = []
    if res["due"]:
        lines.append(f"Можно напомнить об оплате: {len(res['due'])}" + (f" (+{res['more']})" if res["more"] else ""))
        for d in res["due"]:
            lines.append(f"- [{d['sale_id']}] {d['buyer']} — «{d['title'][:50]}», {d['amount']}, {d['days']} дн")
            if d["text"]:
                lines.append(f"  текст: {d['text']}")
        if not res["template"]:
            lines.append("Её шаблона напоминания нет: попроси текст своими словами (слоты {buyer} {amount} {title} "
                         "{days} {url}) — без него черновик не готовлю.")
        hint = next((d["payment_info_suggestion"] for d in res["due"] if d.get("payment_info_suggestion")), None)
        if hint:
            lines.append("Её текста «как платить» нет. Предложение (не отправляю и в черновик не кладу — сохрани "
                         "своими словами: set_template('payment_info', …), слоты {buyer} {title} {amount} "
                         "{boosty_url} {window_hours}):\n" + hint)
    for s in res["skipped"]:
        lines.append(f"- [{s['sale_id']}] {s['buyer'] or '?'} — «{s['title'][:50]}»: {s['reason']}")
    return "\n".join(lines)
