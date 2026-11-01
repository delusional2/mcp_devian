"""Чек-лист поста перед публикацией: всё ли покупателю понятно — как платить, до какого срока, что он получает.

Только проверка текста её поста (listing.listing_facts) и готовые английские строки, которые она может вставить
сама. Ничего не публикуется. Факты про Boosty и DA Points — с источником и датой; способы, которые для неё не
работают (Ko-fi, PayPal, Stripe), никогда не предлагаем, а непроверенные (Hipolink, Tribute, крипта) помечаем
«не проверено».
"""

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import sales
from .listing import buyer_gets, listing_facts, pays_any

BOOSTY_FAQ_DATE = "обновлено 12.2025"
POINTS_CHECKED = "проверено 11.2026"


@dataclass(frozen=True)
class Fact:
    key: str
    text: str
    source: str
    date: str


BOOSTY_FACTS: tuple[Fact, ...] = (
    Fact("cards", "Иностранцы платят банковской картой, методом оплаты «не для карт РФ».",
         sales.BOOSTY_FAQ, BOOSTY_FAQ_DATE),
    Fact("3ds", "Нужен 3-D Secure; иностранная карта может не пройти 3DS или её отклонит платёжный партнёр.",
         sales.BOOSTY_FAQ, BOOSTY_FAQ_DATE),
    Fact("rub", "Ты получаешь рубли; цены задаёшь в рублях, иностранцы могут видеть их в долларах — конвертация "
                "автоматическая по региону, валюту доната поменять нельзя (переключать покупателю нечего).",
         sales.BOOSTY_FAQ, BOOSTY_FAQ_DATE),
    Fact("fee", "Комиссия 11.7% с учётом платёжных систем; комиссия за вывод — отдельно.",
         sales.BOOSTY_FAQ, BOOSTY_FAQ_DATE),
    Fact("refund", "Встроенного возврата нет: вернуть деньги можешь ты сама, а покупатель может оспорить "
                   "платёж через свой банк.", sales.BOOSTY_FAQ, BOOSTY_FAQ_DATE),
    Fact("lanes", "Если оплата не проходит, первым делом проверить, что выбран метод «не для карт РФ».",
         "https://dtf.ru/howto/5207312-kak-pravilno-oplatit-boosty (сторонний гайд)", "09.2026"),
    Fact("no_paypal", "PayPal на оплате Boosty больше нет (старые гайды «выбери PayPal» устарели).",
         "https://www.solcard.cc/blog/pay-boosty-with-crypto (сторонний, продавец карт)", "08.2026"),
    Fact("no_crypto", "Криптовалюту Boosty не принимает.",
         "https://www.solcard.cc/blog/pay-boosty-with-crypto (сторонний, продавец карт)", "08.2026"),
)

POINTS_FACTS: tuple[Fact, ...] = (
    Fact("not_cash", "Поинты не выводятся в деньги и не продаются за деньги — ни на DA, ни вне его.",
         sales.POINTS_TOS, POINTS_CHECKED),
    Fact("per_transfer", f"Одним переводом — не больше {sales.POINTS_PER_TRANSFER:,} поинтов.",
         sales.POINTS_TOS, POINTS_CHECKED),
    Fact("per_year", f"Одному человеку — не больше {sales.POINTS_PER_YEAR:,} поинтов за год.",
         sales.POINTS_TOS, POINTS_CHECKED),
    Fact("final", "Перевод окончательный, споры между участниками DA не разбирает.", sales.POINTS_TOS, POINTS_CHECKED),
)

# Не для неё: почему — с источником. Сами не предлагаем; объясняем, только если она спросит.
NOT_FOR_HER = {
    "paypal": "PayPal не работает в России с марта 2022 (отчёт PayPal 8-K: "
              "https://www.sec.gov/Archives/edgar/data/1633917/000163391722000075/pypl-20220308.htm)",
    "ko-fi": "Ko-fi выплачивает только через PayPal или Stripe "
             "(https://help.ko-fi.com/hc/en-us/articles/24482435253661-What-payment-methods-are-available-on-Ko-fi)",
    "stripe": "России нет в списке стран Stripe (https://stripe.com/global)",
}
UNVERIFIED = {
    "hipolink": "Hipolink — не проверено: им пользуются рядом с Boosty, но приём PayPal в 2026 не подтверждён.",
    "tribute": "Tribute (Telegram) — не проверено: по статье 2024 года звёзды и выплата в USDT.",
    "crypto": "Крипта — не проверено: Boosty её не принимает, надёжного способа для адоптов не нашла.",
}

SELL_TYPES = ("auction", "bta", "ota", "flatsale", "adopt", "other")
DEADLINE_TYPES = ("auction", "bta", "raffle")
AUCTION_TYPES = ("auction", "bta")
# «ends 48h after the last bid», «ends in 3 days», «72 hour auction» — срок без пояса понятен.
RELATIVE_END = re.compile(r"\b\d{1,3}\s*[- ]?(?:h|hrs?|hours?|days?)\s+(?:after|from|auction)\b|"
                          r"\bends?\s+in\s+\d{1,3}\s*(?:h|hrs?|hours?|days?)\b", re.I)
PROFILE_LINK = re.compile(r"\b(?:link|payment\s+info|how\s+to\s+pay)\b[^.\n]{0,20}\b(?:in|on)\s+my\s+(?:profile|bio)\b",
                          re.I)


@dataclass
class Item:
    ok: bool
    key: str
    what: str      # по-русски: что проверяли / чего не хватает
    fix: str = ""  # готовая английская строка, которую можно вставить в пост


def _get(settings, key: str, default):
    if settings is None:
        return default
    if isinstance(settings, dict):
        return settings.get(key, default)
    return getattr(settings, key, default)


def _methods(settings) -> list[str]:
    methods = list(_get(settings, "payment_methods", []) or [])
    if _get(settings, "boosty_url", "") and "boosty" not in methods:
        methods.insert(0, "boosty")
    return methods


def tz_label(name: str | None, now: float | None = None) -> str:
    """«MSK (GMT+3)» для Europe/Moscow, иначе «GMT+X» по смещению сейчас."""
    try:
        zone = ZoneInfo(name or "Europe/Moscow")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Europe/Moscow")
    moment = datetime.fromtimestamp(now, zone) if now else datetime.now(zone)
    offset = moment.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    h, m = divmod(abs(minutes), 60)
    gmt = (f"GMT{'+' if minutes > 0 else '-'}{h}" + (f":{m:02d}" if m else "")) if minutes else "GMT"
    return f"MSK ({gmt})" if getattr(zone, "key", "") == "Europe/Moscow" else gmt


def payment_line(settings) -> str:
    """Строка «как платить» для поста — по её способам оплаты. Пусто, если способы не заданы."""
    methods = _methods(settings)
    parts = []
    if "boosty" in methods:
        url = _get(settings, "boosty_url", "") or "link in my profile"
        parts.append(f"Boosty ({url}; choose the option for cards issued outside Russia, 3-D Secure)")
    if "points" in methods:
        parts.append("DA Points")
    if "other" in methods:
        parts.append("other ways - ask me")
    return ("Payment: " + " or ".join(parts)) if parts else ""


FOREIGN_HINT = ("International buyers: on Boosty choose the payment option for cards issued outside Russia (not the "
                "one for Russian cards); the price is converted automatically (you may see it in USD), and your card "
                "needs 3-D Secure. "
                "PayPal isn't available there.")
CURRENCY_SIGN = {"usd": "$", "eur": "€", "gbp": "£", "rub": "₽ ", "points": "pts "}


def _sign(settings, facts: dict) -> str:
    """Знак валюты для заготовки SB/MI/AB: явная currency в настройках, иначе поинты, если она берёт только их."""
    cur = str(_get(settings, "currency", "") or "").lower()
    if cur in CURRENCY_SIGN:
        return CURRENCY_SIGN[cur]
    methods = _methods(settings)
    if methods == ["points"] or (not methods and facts.get("pay_points") and not facts.get("pay_boosty")):
        return CURRENCY_SIGN["points"]
    return "$"


def check(text: str, lot_type: str, settings=None, now: float | None = None) -> list[Item]:
    """Что есть и чего нет в её посте. settings — cfg.posting (или dict с теми же ключами)."""
    text = text or ""
    facts = listing_facts(text)
    lot_type = (lot_type or "other").lower()
    methods = _methods(settings)
    boosty_url = _get(settings, "boosty_url", "") or ""
    items: list[Item] = []
    selling_lot = lot_type in SELL_TYPES

    if selling_lot:
        line = payment_line(settings)
        if "boosty" in methods:
            ok = bool(facts["pay_boosty"] or (boosty_url and boosty_url in text) or PROFILE_LINK.search(text))
            what = "как платить: Boosty" if ok else "не видно, как платить (у тебя Boosty)"
        else:
            ok = pays_any(facts)
            what = "способ оплаты" if ok else "не видно способа оплаты"
        items.append(Item(ok, "payment", what, line or "Payment: <how to pay>"))
        uses_boosty = "boosty" in methods or facts["pay_boosty"]
        if uses_boosty:
            ok = bool(facts["foreign_pay_hint"])
            items.append(Item(ok, "foreign_hint",
                              "подсказка иностранцам, как платить на Boosty" if ok else
                              "нет подсказки иностранцам: метод оплаты «не для карт РФ», 3-D Secure "
                              f"(FAQ Boosty, {BOOSTY_FAQ_DATE})", FOREIGN_HINT))
        window = int(_get(settings, "payment_window_hours", 48) or 48)
        ok = bool(facts["payment_window"])
        items.append(Item(ok, "payment_window", "срок оплаты" if ok else "не сказано, сколько ждёшь оплату",
                          f"Please pay within {window}h, otherwise the adopt goes to the next bidder or back up "
                          "for sale."))
        holds, plans = _get(settings, "holds_policy", "") or "", bool(_get(settings, "payment_plans", False))
        if holds or plans:
            ok = bool(facts["hold"])
            # Срок остатка — только её правило; своего не придумываем (заглушка <N> days).
            fix = holds or ("Payment plans are possible: <deposit>% now, the rest within <N> days; the character is "
                            "transferred after full payment.")
            items.append(Item(ok, "holds", "правило про холды / оплату частями" if ok else
                              "твоё правило про холды или оплату частями в посте не видно", fix))

    tos_url = _get(settings, "tos_url", "") or ""
    ok = bool(facts["tos"] or (tos_url and tos_url in text))
    items.append(Item(ok, "tos", "TOS" if ok else "нет ссылки на TOS", f"TOS: {tos_url or '<link to my TOS>'}"))

    if lot_type in DEADLINE_TYPES:
        relative = bool(RELATIVE_END.search(text))
        ok = bool(facts["end_time"] or relative)
        tz = tz_label(_get(settings, "timezone", "Europe/Moscow"), now)
        items.append(Item(ok, "deadline", "срок окончания" if ok else "не видно, когда закончится",
                          f"Ends: DD.MM at HH:MM {tz}"))
        if ok and not relative:
            tz_ok = bool(facts["timezone"])
            items.append(Item(tz_ok, "timezone", "часовой пояс у срока" if tz_ok else
                              "у срока нет часового пояса — покупатели из других стран ошибутся",
                              f"(time zone: {tz})"))

    if lot_type in AUCTION_TYPES:
        ok = bool(facts["sb"] and facts["mi"] and facts["ab"])
        missing = [k.upper() for k in ("sb", "mi", "ab") if not facts[k]]
        sign = _sign(settings, facts)
        items.append(Item(ok, "sb_mi_ab", "SB/MI/AB" if ok else f"не видно {'/'.join(missing)}",
                          f"SB: {sign}__ | MI: {sign}__ | AB: {sign}__ (or \"no AB\")"))

    if selling_lot:
        ok = buyer_gets(facts)
        items.append(Item(ok, "buyer_gets", "что получает покупатель" if ok else "не сказано, что получит покупатель",
                          "You get: full-size PNG without watermark + Toyhouse transfer."))
        if _get(settings, "th_username", "") or facts["toyhouse"]:
            ok = bool(facts["toyhouse"])
            items.append(Item(ok, "toyhouse", "перенос на Toyhouse" if ok else "не сказано про перенос на Toyhouse",
                              "The character will be transferred on Toyhouse after full payment."))
    return items


def render(items: list[Item]) -> str:
    """Коротко по-русски: чего не хватает и готовая английская строка; что на месте — одной строкой."""
    missing = [i for i in items if not i.ok]
    have = [i.what for i in items if i.ok]
    if not items:
        return "Чек-лист: проверять нечего."
    lines = [f"Чек-лист поста: на месте {len(have)} из {len(items)}."]
    for i in missing:
        lines.append(f"- {i.what}" + (f"\n  можно вставить: {i.fix}" if i.fix else ""))
    if have:
        lines.append("Есть: " + ", ".join(have) + ".")
    if not missing:
        lines.append("Всё на месте.")
    return "\n".join(lines)


def facts_text(points: bool = True) -> str:
    """Факты для неё (по-русски), каждый с источником и датой; DA Points — как отдельная альтернатива."""
    lines = ["Boosty для иностранных покупателей:"]
    lines += [f"- {f.text} ({f.source}, {f.date})" for f in BOOSTY_FACTS]
    if points:
        lines.append("Альтернатива — DA Points (не деньги):")
        lines += [f"- {f.text} ({f.source}, {f.date})" for f in POINTS_FACTS]
    lines.append("Другие способы: " + " ".join(UNVERIFIED.values()))
    return "\n".join(lines)
