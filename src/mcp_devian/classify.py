"""Разбор лотов и тредов обычными регулярками, без нейросетей."""

import re
from dataclasses import dataclass

from .config import Filters

# Порядок важен: первое совпадение в названии побеждает.
LOT_TYPES = [
    ("ych", r"\bych\b"),
    ("commission", r"\bcommissions?\b|\bcomms?\b"),
    ("bta", r"\bbta\b|\bbid to adopt"),
    ("auction", r"\bauction|\bsb\s*[:=\-]|\bab\s*[:=\-]|\bstarting bid|\bauto[\s-]?buy|\bmin(?:imum)?\.?\s*inc"),
    ("ota", r"\bota\b|\boffer to adopt"),
    ("raffle", r"\braffle"),
    ("flatsale", r"\bflat\s?sale|\bflat price|\bfixed price|\bfs\b"),
    ("adopt", r"\badopt(?:s|able|ables|ion)?\b|\bresale\b|\bpurge\b|\bfor\s+sale\b|\bselling\b"),
]
GENERIC = {"adopt", "other"}

# В описании слово «auction» чаще реклама («check my other auctions»), поэтому
# аукцион там узнаём только по жёстким меткам SB/AB/min inc.
DESC_TYPES = [
    ("bta", r"\bbta\b|\bbid to adopt"),
    ("auction", r"\bsb\s*[:=\-]|\bab\s*[:=\-]|\bstarting bid|\bauto[\s-]?buy|\bmin(?:imum)?\.?\s*(?:bid\s*)?inc"),
    ("ota", r"\bota\b|\boffer to adopt"),
    ("raffle", r"\braffles?\b"),
    ("flatsale", r"\bflat\s?sale|\bflat price|\bfixed price"),
]
NEGATED = re.compile(r"\b(?:no|not|non|other|more|check|see)\s+(?:\w+\s+){0,2}$", re.I)

# Теги DA бывают слитными: adoptauction, adoptsopen, otaadopt.
TAG_TYPES = [
    ("bta", r"^bta$|^bta(?:adopts?|open)|adopts?bta$|bidtoadopt"),
    ("auction", r"auction"),
    ("ota", r"^ota$|^ota(?:adopts?|open)|adopts?ota$|offertoadopt"),
    ("raffle", r"raffle"),
    ("flatsale", r"flatsale|flatprice"),
    ("adopt", r"^adopt|adopt(?:s|able|ables)?$"),
]

# --- Статус ---

HARD_CLOSED = re.compile(
    r"\b(?:not|no\s+longer)\s+available\b|\bunavailable\b|\bsold\s+out\b|\bon\s*hold\b|\bpending\b|\breserved\b", re.I)
FRACTION = re.compile(r"\b(\d+)\s*/\s*(\d+)\s*(?:sold|taken|adopted|gone|claimed)\b", re.I)
# Частичные «2 sold», «1 taken», «adopted from @x», «gone in 24h» не закрывают лот.
PARTIAL = re.compile(
    r"\b\d+\s*(?:sold|taken|adopted|gone|claimed)\b|\badopted\s+from\b|\bgone\s+in\b|\bnot\s+(?:yet\s+)?(?:sold|taken)\b",
    re.I)
LEFT = re.compile(r"\b([1-9]\d*)\s+left\b", re.I)
CLOSED = re.compile(r"\b(?:closed|sold|ended|taken|adopted|gone)\b", re.I)
OPEN = re.compile(r"\b(?:open(?:ed)?|available|still up)\b", re.I)
BRACKETS = re.compile(r"[\[({]([^\])}]{1,40})[\])}]")

# --- Цены ---

_N = r"(\d{1,3}(?:[.,]\d{3})+(?!\d)|\d+(?:[.,]\d{1,2})?)"
_K = r"(?:\s?(k)\b)?"
NO_NUM_BEFORE = r"(?<![#\w/.,])"
CUR_SYM = re.compile(r"[$€£]|\b(?:usd|eur|euros?|gbp|dollars?)\b", re.I)
NUM_HEAD = re.compile(_N + _K + r"(?!\s*/\s*\d)", re.I)
NUM_TAIL = re.compile(NO_NUM_BEFORE + _N + r"(?:\s?(k))?(\s?)$", re.I)
POINTS = re.compile(NO_NUM_BEFORE + _N + _K + r"\s*(?:da\s*)?(?:pts\b|points?\b|:points:)", re.I)
LABEL_PUNCT = re.compile(r"[:=\-–(]\s*$")

SB_LABEL = re.compile(
    r"\b(?:sb|starting\s*bid|start(?:ing)?(?:\s*(?:price|at))?|starts\s+at|opening\s+bid)\s*\)?\s*[:=\-–]?\s*$", re.I)
SKIP_LABEL = re.compile(
    r"(?:\b(?:mi|md|min(?:imum)?\.?(?:\s*(?:bid|inc\w*|increase|increment)\.?)*|inc\w*|increments?|ab\s*\d?|"
    r"auto[\s-]?buy|buyout|bid\s+above)|\b(?:more|less|higher|lower)\s+than)\s*\)?\s*[:=\-–]?\s*$",
    re.I,
)
BARE_SB = re.compile(r"\b(?:sb|starting\s*bid)\s*\)?\s*[:=\-–]?\s*" + _N + _K, re.I)


@dataclass
class Lot:
    lot_type: str
    status: str
    price_text: str | None = None
    usd: float | None = None
    points: float | None = None


@dataclass
class _Amount:
    value: float
    currency: str  # usd | points
    anchor: int    # где начинается «$10» или «10$»
    end: int
    label: str | None = None  # sb | skip


def _num(raw: str, k: str | None) -> float:
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        raw = re.sub(r"[.,]", "", raw)
    value = float(raw.replace(",", "."))
    return value * 1000 if k else value


def _amounts(text: str) -> list[_Amount]:
    found: dict[int, _Amount] = {}  # по началу числа: «$15 USD» — одна цена, не две
    for sym in CUR_SYM.finditer(text):
        word = sym.group().isalpha()
        before = text[:sym.start()]
        tail = NUM_TAIL.search(before)
        # «5$» — символ после числа; для «5 usd» допускаем пробел, для «5 $» нет.
        if tail and (word or not tail.group(3)):
            start = tail.start(1)
            found.setdefault(start, _Amount(_num(tail.group(1), tail.group(2)), "usd", start, sym.end()))
            continue
        pos = sym.end()
        # «$ 60» бывает только после метки («SB : $ 60»), иначе «LF $ 3 left» даст мусор.
        if text[pos:pos + 1] == " " and (word or LABEL_PUNCT.search(before)):
            pos += 1
        head = NUM_HEAD.match(text, pos)
        if head:
            found.setdefault(head.start(1), _Amount(_num(head.group(1), head.group(2)), "usd", sym.start(), head.end()))
    for m in POINTS.finditer(text):
        found.setdefault(m.start(1), _Amount(_num(m.group(1), m.group(2)), "points", m.start(1), m.end()))

    amounts = sorted(found.values(), key=lambda a: a.anchor)
    for a in amounts:
        prefix = text[max(0, a.anchor - 40):a.anchor]
        if SB_LABEL.search(prefix):
            a.label = "sb"
        elif SKIP_LABEL.search(prefix):
            a.label = "skip"
    # «SB: $10 (USD) OR 800 points» — вторая валюта того же SB.
    for a in amounts:
        if a.label:
            continue
        for s in amounts:
            between = text[s.end:a.anchor]
            if (s.label == "sb" and s.currency != a.currency and s.anchor < a.anchor
                    and len(between) <= 20 and ":" not in between and re.search(r"\bor\b|/", between, re.I)):
                a.label = "sb"
                break
    return amounts


def _pick(amounts: list[_Amount], currency: str, any_sb: bool) -> tuple[float | None, bool]:
    """(цена, это SB?). Если SB назван в другой валюте, эта валюта — скорее AB, не показываем."""
    mine = [a for a in amounts if a.currency == currency]
    sb = [a for a in mine if a.label == "sb"]
    if sb:
        return sb[0].value, True
    if any_sb:
        return None, False
    rest = [a.value for a in mine if a.label != "skip"]
    return (min(rest), False) if rest else (None, False)


def _prices(text: str) -> tuple[float | None, float | None, bool] | None:
    amounts = _amounts(text)
    if not amounts:
        return None
    any_sb = any(a.label == "sb" for a in amounts)
    usd, usd_sb = _pick(amounts, "usd", any_sb)
    points, pts_sb = _pick(amounts, "points", any_sb)
    is_sb = usd_sb or pts_sb
    if not any_sb and usd is None and points is None:
        # «SB: 5 / MI: $1» — у SB нет валюты; если валюта в посте одна, это она.
        bare = BARE_SB.search(text)
        currencies = {a.currency for a in amounts}
        if bare and len(currencies) == 1:
            value = _num(bare.group(1), bare.group(2))
            usd, points = (value, None) if "usd" in currencies else (None, value)
            is_sb = True
    return usd, points, is_sb


def _price_label(usd: float | None, points: float | None, is_sb: bool) -> str | None:
    parts = []
    if usd is not None:
        parts.append(f"${usd:g}")
    if points is not None:
        parts.append(f"{points:g} pts")
    if not parts:
        return None
    return ("SB " if is_sb else "") + " / ".join(parts)


def _tag_type(tags: list[str] | tuple) -> str | None:
    norm = [re.sub(r"[\s_-]", "", t.lower()) for t in tags]
    for name, pattern in TAG_TYPES:
        if name != "adopt" and any(re.search(pattern, t) for t in norm):
            return name
    return None


def _desc_type(text: str) -> str | None:
    best = None
    for name, pattern in DESC_TYPES:
        for m in re.finditer(pattern, text, re.I):
            if NEGATED.search(text[max(0, m.start() - 30):m.start()]):
                continue
            if best is None or m.start() < best[0]:
                best = (m.start(), name)
            break
    return best[1] if best else None


def lot_type_of(title: str, text: str = "", tags: list[str] | tuple = ()) -> str:
    found = next((name for name, pattern in LOT_TYPES if re.search(pattern, title, re.I)), "other")
    if found not in GENERIC:
        return found
    # «Adoptable [OPEN]» в названии, а «SB: $10» в описании — это аукцион.
    specific = _tag_type(tags) or _desc_type(text)
    if specific:
        return specific
    if found == "adopt":
        return found
    norm = [re.sub(r"[\s_-]", "", t.lower()) for t in tags]
    if any(re.search(TAG_TYPES[-1][1], t) for t in norm) or re.search(LOT_TYPES[-1][1], text, re.I):
        return "adopt"
    return "other"


def _status_words(s: str) -> str | None:
    if HARD_CLOSED.search(s):
        return "closed"
    frac = FRACTION.search(s)
    if frac:
        return "open" if int(frac.group(1)) < int(frac.group(2)) else "closed"
    s = PARTIAL.sub(" ", s)
    if OPEN.search(s) or LEFT.search(s):
        return "open"
    if CLOSED.search(s):
        return "closed"
    return None


def status_of(title: str) -> str:
    # Явная метка в скобках ([OPEN], (closed)) главнее слов вокруг.
    for m in BRACKETS.finditer(title):
        status = _status_words(m.group(1))
        if status:
            return status
    return _status_words(title) or "unknown"


def classify_lot(title: str, text: str = "", tags: list[str] | tuple = ()) -> Lot:
    lot = Lot(lot_type=lot_type_of(title, text, tags), status=status_of(title))
    # Цену ищем сначала в названии, потом в описании.
    for source in (title, text):
        prices = _prices(source)
        if prices:
            lot.usd, lot.points, is_sb = prices
            lot.price_text = _price_label(lot.usd, lot.points, is_sb)
            break
    # Лента — папка группы адоптов: лот с ценой без явного типа — это адопт.
    if lot.lot_type == "other" and (lot.usd is not None or lot.points is not None):
        lot.lot_type = "adopt"
    return lot


def keyword_hits(keywords: list[str], *texts: str) -> list[str]:
    blob = " ".join(t for t in texts if t)
    # Целое слово, можно во множественном числе: «base» ловит «bases», но не «based».
    return [kw for kw in keywords
            if kw and re.search(r"(?<!\w)" + re.escape(kw) + r"(?:e?s)?(?!\w)", blob, re.I)]


def within_budget(filters: Filters, usd: float | None, points: float | None) -> bool:
    if filters.max_usd and usd is not None and usd > filters.max_usd:
        return False
    if filters.max_points and points is not None and points > filters.max_points:
        return False
    return True


def match_lot(filters: Filters, lot: Lot, title: str, text: str = "", tags: list[str] | tuple = ()) -> tuple[bool, str]:
    tag_text = " ".join(tags)
    if lot.lot_type not in filters.lot_types:
        return False, f"тип {lot.lot_type}"
    if lot.status == "closed":
        return False, "закрыт"
    excluded = keyword_hits(filters.exclude, title, tag_text)
    if excluded:
        return False, f"исключено: {', '.join(excluded)}"
    if not within_budget(filters, lot.usd, lot.points):
        return False, "дороже бюджета"
    hits = keyword_hits(filters.keywords, title, tag_text, text)
    if filters.keywords and not hits:
        return False, "нет ключевых слов"
    reason = [lot.lot_type]
    if lot.price_text:
        reason.append(lot.price_text)
    if hits:
        reason.append("слова: " + ", ".join(hits))
    return True, " · ".join(reason)


# --- Треды Toyhouse ---

LF_START = re.compile(
    r"\b(?:lf|looking\s+to\s+buy|looking\s+for|seeking|iso|in\s+search\s+of|searching\s+for|buying|wtb|want(?:ing)?)\b\s*[:\-]?",
    re.I)
OFFER_START = re.compile(r"(?:^|[\s|/\-(\[,])(?:o|of|offering|offers?)\s*:", re.I)
SEGMENT_END = re.compile(r"[|\]\)]|\s[-–—]\s|\b(?:o|of|offering|offers?)\s*:", re.I)
# «Paying $ for customs», «Will pay usd for dragons» — покупатель без слова LF.
PAY_FOR = re.compile(r"\b(?:paying|offering|will\s+pay|can\s+pay)\s+(?P<pay>[^|]*?)\s+for\s+(?P<what>[^|\])]*)", re.I)
# После «LF: cats - paying usd» хвост за тире — это то, чем платят.
DASH_PAY = re.compile(r"^\s*[-–—]\s*(?P<pay>(?:paying|pay|offering|o\b|\$|usd|eur|€|£|pts|points|art)[^|]*)", re.I)

MONEY = r"\$+|usd|eur|€|£|gbp|aud|cad|money|cash|paypal|pts|points?|da\s?points|ko-?fi"
ART = r"art|arts|artwork|commissions?|comms?|sketch(?:es)?|drawings?"
TRADES = r"trades?|swaps?|offers?"
CHARS = (r"ocs?|characters?|chars?|designs?|adopts?|adoptables?|customs?|myos|sonas?|species"
         r"|men|boys|girls|guys|humans?|furr(?:y|ies)|ferals?|anthros?")
FILLER = r"new\s+home|homes?|good\s+home|anything|stuff|etc|and|or|&|\+|/|,|\.|!|\?|\d+|nothing|offers?|any"

# «LF: $$ for my ocs», «LF: offers on my characters» — это продавец ищет покупателя.
SELLER_LF = re.compile(
    r"\b(?:for|on|to\s+(?:buy|adopt|take))\s+(?:my|our|these|this|some\s+of\s+my)\s+(?:\w+\s+)?"
    r"(?:ocs?|characters?|chars?|babies|bbs|designs?|kids|sonas?|adopts?)\b",
    re.I,
)
HOMES = re.compile(r"\blf\b.*\b(?:new\s+|good\s+)?homes?\b", re.I)
# Ищут рисунок, а не персонажа.
ART_REQ = re.compile(r"\b(?:art\s+(?:of|for)|draw\w*|artists?|commission(?:er)?s?|someone\s+to)\b", re.I)
NON_CHAR_REQ = re.compile(r"\b(?:invites?|codes?|rp|role-?play|partners?|friends?|feedback|help|advice)\b", re.I)

SELL_HINTS = re.compile(
    r"\b(?:purge|ufs|uft|for\s+sale|sale|resales?|reselling|selling|sell|adopts?|adoptables?|auctions?|ota|bta|"
    r"flat\s?sale|new\s+home|rehom\w*|free\s+(?:characters?|ocs?|adopts?)|characters?\s+for|ocs?\s+for|designs?\s+for)\b",
    re.I,
)


@dataclass
class ThreadKind:
    wants_characters: bool
    sells_characters: bool
    pays: list[str]


def _segment(title: str, start: re.Pattern) -> tuple[str, str]:
    """(сегмент, что идёт после него)."""
    m = start.search(title)
    if not m:
        return "", ""
    rest = title[m.end():]
    end = SEGMENT_END.search(rest)
    return (rest[: end.start()], rest[end.start():]) if end else (rest, "")


def _currencies(text: str) -> list[str]:
    found = []
    if re.search(r"\$|usd|eur|€|£|gbp|aud|cad|money|cash|paypal|ko-?fi", text, re.I):
        found.append("usd")
    if re.search(r"\bpts\b|\bpoints?\b|\bda\s?points\b", text, re.I):
        found.append("points")
    if re.search(r"\b(?:" + ART + r")\b", text, re.I):
        found.append("art")
    if re.search(r"\b(?:trades?|swaps?)\b|\bocs?\b|\bcharacters?\b|\bdesigns?\b", text, re.I):
        found.append("trades")
    return found


def _has(pattern: str, text: str) -> bool:
    return bool(re.search(r"(?:\b|(?<=\W))(?:" + pattern + r")(?:\b|(?=\W)|$)", text, re.I))


def classify_thread(title: str) -> ThreadKind:
    lf, after_lf = _segment(title, LF_START)
    offer, _ = _segment(title, OFFER_START)
    sell_text = title

    if not lf:
        pay_for = PAY_FOR.search(title)
        rest = title[:pay_for.start()] + title[pay_for.end():] if pay_for else ""
        # «Selling ocs, offering discounts for bulk» — это продавец, а не покупатель.
        if pay_for and not SELL_HINTS.search(rest):
            lf, offer = pay_for.group("what"), offer or pay_for.group("pay")
            sell_text = rest
    if lf and not offer:
        dash = DASH_PAY.match(after_lf)
        if dash:
            offer = dash.group("pay")

    lf_rest = lf
    for pattern in (MONEY, ART, TRADES, FILLER):
        lf_rest = re.sub(r"(?:\b|(?<=\W))(?:" + pattern + r")(?:\b|(?=\W)|$)", " ", lf_rest, flags=re.I)
    lf_has_chars = _has(CHARS, lf)
    lf_wants_chars = lf_has_chars or bool(re.search(r"[a-z]{3,}", lf_rest, re.I))
    seller_lf = bool(lf and SELLER_LF.search(title)) or bool(HOMES.search(title))
    art_request = bool(ART_REQ.search(lf)) or (bool(NON_CHAR_REQ.search(lf)) and not lf_has_chars)
    if seller_lf or art_request:
        lf_wants_chars = False
    # «LF: $$$» без персов — продавец хочет денег. «LF: art» без денег — это заказ рисунка.
    lf_wants_payment = bool(lf) and not lf_wants_chars and not art_request and (_has(MONEY, lf) or _has(TRADES, lf))

    offers_chars = _has(CHARS, offer)
    sells = bool(SELL_HINTS.search(sell_text)) or lf_wants_payment or offers_chars or seller_lf

    # «Чем платят» имеет смысл только там, где ищут персов. Без O: смотрим на «paying usd»
    # внутри LF, но у распродаж LF — это то, что продавец хочет получить, а не платит.
    pays: list[str] = []
    if lf_wants_chars:
        if offer:
            pays = _currencies(offer)
        elif not sells:
            pays = [p for p in _currencies(lf) if p != "trades"]
    return ThreadKind(wants_characters=lf_wants_chars, sells_characters=sells, pays=pays)
