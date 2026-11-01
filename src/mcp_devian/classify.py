"""Разбор лотов и тредов обычными регулярками, без нейросетей."""

import re
import unicodedata
from dataclasses import dataclass

from .config import Filters

# Порядок важен: первое совпадение в названии побеждает.
LOT_TYPES = [
    ("ych", r"\bychs?\b"),
    ("commission", r"\bcommissions?\b|\bcomms?\b|\bcustoms?\b(?!(?:\s+[\w-]+){0,2}\s+adopt)"),
    ("bta", r"\bbta\b|\bbid to adopt"),
    ("auction", r"\bauction|\bsb\s*[:=\-]|\bab\s*[:=\-]|\bstart(?:ing)?\s*bid|\bauto[\s-]?buy|\bmin(?:imum)?\.?\s*inc"),
    ("ota", r"\bota\b|\boffer to adopt|\bpwyw\b|pay what you want"),
    ("raffle", r"\braffle|\bgacha\b"),
    ("flatsale", r"\bflat\s?sale|\bflat price|\bfixed price|\bfs\b|(?<!for\s)\bsale\b|\bclear(?:out|ance)\b"
                 r"|\beach\b|\bcutiemarks?\b"),
    ("adopt", r"\badopt(?:s|able|ables|ion)?\b|\bresale\b|\bpurge\b|\bfor\s+sale\b|\bselling\b"),
]
GENERIC = {"adopt", "other"}

# В описании слово «auction» чаще реклама («check my other auctions»), поэтому
# аукцион там узнаём только по жёстким меткам SB/AB/min inc.
DESC_TYPES = [
    ("bta", r"\bbta\b|\bbid to adopt"),
    ("auction", r"\bsb\s*[:=\-|]|\bsb\s+\$?\d|\bab\s*[:=\-|]|\bstart(?:ing)?\s*bid|\bauto[\s-]?buy"
                r"|\bmin(?:imum)?\.?\s*(?:bid\s*)?inc"),
    ("ota", r"\bota\b|\boffer to adopt|\bpwyw\b|pay what you want|\bmin(?:imum)?\.?\s*offer"),
    ("raffle", r"\braffles?\b|\bgacha\b"),
    ("flatsale", r"\bflat\s?sale|\bflat price|\bfixed price|\bset price|(?:^|\s)\d{1,2}\.\s*\$\d"),
]
# «Price:» — фиксированная цена, только если других меток в описании нет: «Price: SB $10 AB $50» — аукцион.
DESC_PRICE_LABEL = re.compile(r"\bprice\s*[:=\-–]", re.I)
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
# «[CLOSE]» — статус, а «close 3/11», «close at 8pm», «Close Date: Nov 5» — когда закроется.
CLOSED = re.compile(r"\b(?:closed|close(?!\s*(?:date|at|on|in|time|:|\d))|sold|ended|taken|adopted|gone)\b", re.I)
# «[0/3 OPEN]» — открыто 0 из 3, то есть закрыт; «OPEN 2/3» — ещё открыт.
OPEN_FRAC = re.compile(r"\b(\d+)\s*[/\\]\s*(\d+)\s*(?:open|left|available|avail)\b"
                       r"|\b(?:open|left|available)\s*(\d+)\s*[/\\]\s*(\d+)", re.I)
OPEN = re.compile(r"\b(?:open(?:ed)?|available|still up)\b", re.I)
BRACKETS = re.compile(r"[\[({]([^\])}]{1,40})[\])}]")

# --- Цены ---

_N = r"(\d{1,3}(?:[.,]\d{3})+(?!\d)|\d+(?:[.,]\d{1,2})?)"
# То же плюс тысячи через пробел («1 000 pts», «1 360 Points») — только для поинтов («Tier 2 100$» — не $2100)
# и только после метки или знака (SPACED_BEFORE): «Cat adopt 3 500 pts» — это «3» в названии и 500 pts.
_NS = r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?!\d)|\d{1,3}(?:[.,]\d{3})+(?!\d)|\d+(?:[.,]\d{1,2})?)"
_K = r"(?:\s?(k)\b)?"
NO_NUM_BEFORE = r"(?<![#\w/.,])"
# «C$», «A$», «NZ$» — канадские, австралийские, новозеландские доллары: это не USD.
CUR_SYM = re.compile(r"(?-i:\b(?:CA?|AU?|NZ)\$)|[$€£]|\b(?:usd|eur|euros?|gbp|dollars?|cad|aud|nzd)\b", re.I)
NUM_HEAD = re.compile(_N + r"(?![\d.,]\d)" + _K, re.I)
NUM_TAIL = re.compile(NO_NUM_BEFORE + _N + r"(?:\s?(k))?(\s?)$", re.I)
POINTS = re.compile(r"(?<![#\w.,])" + _NS + _K + r"\s*(?:da\s*)?(?:pts\b|points?\b|:points:)", re.I)
LABEL_PUNCT = re.compile(r"[:=\-–(|]\s*$")
SPACED_BEFORE = re.compile(r"(?:^|[:=\-–—(|/~≈]|\b(?:sb|ab|mi|md|price|prices?|for|at|bid|offer|start(?:ing)?|"
                           r"autobuy|buyout|approx\w*|or))\s*$", re.I)

SB_LABEL = re.compile(
    r"\b(?:sb|start(?:ing)?\s*bid|start(?:ing)?(?:\s*(?:price|at))?|starts\s+at|opening\s+bid)\s*\)?\s*[:=\-–|]?\s*$", re.I)
SKIP_LABEL = re.compile(
    r"(?:\b(?:mi|md|min(?:imum)?\.?(?:\s*(?:bid|inc\w*|increase|increment)\.?)*|inc\w*|increments?|ab\s*\d?|"
    r"auto[\s-]?buy|buyout|bid\s+above|save|discount|off|bid)|\b(?:more|less|higher|lower)\s+than)\s*\)?\s*[:=\-–|]?\s*$",
    re.I,
)
BARE_SB = re.compile(r"\b(?:sb|start(?:ing)?\s*bid)\s*\)?\s*[:=\-–|]?\s*" + _N + _K, re.I)
# «$5 OFF», «(1$ = 100)», «$5 sale off» — скидка или курс, а не цена.
NOT_A_PRICE_AFTER = re.compile(r"\s*(?:sale\s+)?(?:=|off\b|discount)", re.I)
# Курс «$1 = 100 pts», «(1$/100pts)», «100 pts per 1$» — не цена, если в посте есть другие суммы.
_RATE_ONE = r"(?:\$\s?1|1\s?\$|1\s?usd)(?![\d.,])"
_RATE_PTS = r"\d{2,3}\s*(?:da\s*)?(?:pts|points?)\b"
_RATE_SEP = r"\s*(?:=|/|:|\bper\b|\bto\b|→|->)\s*"
RATE = re.compile(rf"(?<![\d.,]){_RATE_ONE}{_RATE_SEP}{_RATE_PTS}|(?<![\d.,]){_RATE_PTS}{_RATE_SEP}{_RATE_ONE}", re.I)
# Явный статус в начале описания: «Status: Available», «[Status: Auction Open]».
DESC_STATUS = re.compile(r"\bstatus\s*:\s*(auction\s+open|open(?:ed)?|available|closed|sold)\b", re.I)
# Несколько лотов в одном посте: пачка, «OPEN 2/3», «$5 each», нумерованный прайс, грид.
MULTI_WORDS = re.compile(r"(?<!sheet\s)(?<!ref\s)(?<!bonus\s)(?<!extra\s)\b(?:packs?|batch(?:es)?|bundles?|set\s+of|sets"
                         r"|grids?)\b", re.I)
# «$5 each», «5$ (each)», «each: $5» — цена за штуку; «Each new bid resets the timer» — нет.
MULTI_EACH = re.compile(r"(?:\$\s?\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s?(?:\$|usd|pts|points?|€|£))\s*\(?\s*each\b"
                        r"|\(\s*each\s*\)|\beach\s*:|\beach\s+(?:adopt|design|character|oc|lot|one)s?\b", re.I)
MULTI_LEAD = 300
MULTI_FRAC = re.compile(
    r"\b(?:open|left|available)\s*[:\-]?\s*(\d{1,4})\s*[/\\]\s*(\d{1,4})\b"
    r"|\b(\d{1,4})\s*[/\\]\s*(\d{1,4})\s*(?:open|left|available|avail|sold|taken|adopted)\b", re.I)
PRICE_LIST_ENTRY = re.compile(r"(?:^|\s)\d{1,2}\.\s*(?:\$\d|\d+(?:[.,]\d+)?\s*\$|X\b|SOLD\b|❌)", re.I)


@dataclass
class Lot:
    lot_type: str
    status: str
    price_text: str | None = None
    usd: float | None = None      # только доллары
    points: float | None = None
    # Цена в € или £: (сумма, "eur" | "gbp"). В доллары не пересчитываем — курс плавает,
    # поэтому бюджет в $ к таким лотам не применяется, а в статистику $ они не попадают.
    other_price: tuple[float, str] | None = None
    is_sb: bool = False          # цена — стартовая ставка аукциона
    # Пачка: несколько адоптов в одном посте («OPEN 2/3», «$5 each», «1. $6 2. SOLD»).
    # Цена такого поста — минимум по пачке, а не цена одного лота.
    multi: bool = False


OTHER_CURRENCIES = {"eur": "€", "gbp": "£", "cad": "CA$", "aud": "AU$", "nzd": "NZ$"}


def normalize_text(s: str) -> str:
    """NFKC: «𝑺𝑩 : 150$» → «SB : 150$», полноширинные цифры → обычные."""
    return unicodedata.normalize("NFKC", s or "")


def _currency_of(symbol: str) -> str:
    s = symbol.lower()
    if s in ("€",) or s.startswith("eur"):
        return "eur"
    if s in ("£", "gbp"):
        return "gbp"
    if s in ("c$", "ca$", "cad"):
        return "cad"
    if s in ("a$", "au$", "aud"):
        return "aud"
    if s in ("nz$", "nzd"):
        return "nzd"
    return "usd"


@dataclass
class _Amount:
    value: float
    currency: str  # usd | eur | gbp | points
    anchor: int    # где начинается «$10» или «10$»
    end: int
    label: str | None = None  # sb | skip


def _num(raw: str, k: str | None) -> float:
    raw = re.sub(r"[ \u00a0\u202f]", "", raw)
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        raw = re.sub(r"[.,]", "", raw)
    value = float(raw.replace(",", "."))
    return value * 1000 if k else value


def _amounts(text: str) -> list[_Amount]:
    found: dict[int, _Amount] = {}  # по началу числа: «$15 USD» — одна цена, не две
    for sym in CUR_SYM.finditer(text):
        word = sym.group().isalpha()
        currency = _currency_of(sym.group())
        before = text[:sym.start()]
        tail = NUM_TAIL.search(before)
        # «5$» — символ после числа; для «5 usd» допускаем пробел, для «5 $» нет.
        if tail and (word or not tail.group(3) or not re.match(r"\s?\d", text[sym.end():])):
            start = tail.start(1)
            found.setdefault(start, _Amount(_num(tail.group(1), tail.group(2)), currency, start, sym.end()))
            continue
        pos = sym.end()
        # «$ 60» бывает только после метки («SB : $ 60»), иначе «LF $ 3 left» даст мусор.
        if text[pos:pos + 1] == " " and (word or LABEL_PUNCT.search(before)):
            pos += 1
        head = NUM_HEAD.match(text, pos)
        if head:
            found.setdefault(head.start(1), _Amount(_num(head.group(1), head.group(2)), currency, sym.start(), head.end()))
    for m in POINTS.finditer(text):
        raw, start = m.group(1), m.start(1)
        if re.search(r"\s", raw) and not SPACED_BEFORE.search(text[max(0, start - 30):start]):
            last = re.split(r"\s", raw)[-1]  # «adopt 3 500 pts» — «3» из названия, цена 500
            raw, start = last, m.end(1) - len(last)
        found.setdefault(start, _Amount(_num(raw, m.group(2)), "points", start, m.end()))

    amounts = sorted(found.values(), key=lambda a: a.anchor)
    for a in amounts:
        if NOT_A_PRICE_AFTER.match(text[a.end:a.end + 12]):
            a.label = "skip"
    rates = [m.span() for m in RATE.finditer(text)]
    in_rate = [a for a in amounts if any(lo <= a.anchor < hi for lo, hi in rates)]
    if in_rate and len(in_rate) < len(amounts):
        for a in in_rate:
            a.label = "skip"
    for a in amounts:
        prefix = text[max(0, a.anchor - 40):a.anchor]
        if a.label:
            continue
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


@dataclass
class _Prices:
    usd: float | None = None
    points: float | None = None
    other: tuple[float, str] | None = None
    is_sb: bool = False


def _prices(text: str) -> _Prices | None:
    amounts = _amounts(text)
    if not amounts:
        return None
    any_sb = any(a.label == "sb" for a in amounts)
    out = _Prices()
    out.usd, usd_sb = _pick(amounts, "usd", any_sb)
    out.points, pts_sb = _pick(amounts, "points", any_sb)
    other_sb = False
    for cur in OTHER_CURRENCIES:
        value, sb = _pick(amounts, cur, any_sb)
        if value is not None and out.other is None:
            out.other, other_sb = (value, cur), sb
    out.is_sb = usd_sb or pts_sb or other_sb
    bare = None if any_sb else BARE_SB.search(text)
    if bare:
        # «SB: 5 / MI: $1», «SB: 25 MI: 5 … Tier 2 (55$)» — у SB нет валюты;
        # если валюта в посте одна, это она, и SB главнее прочих сумм.
        currencies = {a.currency for a in amounts}
        if len(currencies) == 1:
            value = _num(bare.group(1), bare.group(2))
            cur = currencies.pop()
            if cur == "usd":
                out.usd = value
            elif cur == "points":
                out.points = value
            else:
                out.other = (value, cur)
            out.is_sb = True
    return out


def _pairs(text: str) -> list[tuple[float, float]]:
    """(доллары, поинты) записанные рядом: «$9 / 900 points», «$10 (USD) OR 800 points»."""
    amounts = [a for a in _amounts(text) if a.label != "skip"]
    out = []
    for a, b in zip(amounts, amounts[1:], strict=False):
        if {a.currency, b.currency} != {"usd", "points"}:
            continue
        if re.fullmatch(r"\s*(?:\(?\s*usd\s*\)?\s*)?(?:/|\bor\b|\|)\s*", text[a.end:b.anchor], re.I):
            out.append((a.value, b.value) if a.currency == "usd" else (b.value, a.value))
    return out


def _price_label(p: _Prices) -> str | None:
    parts = []
    if p.usd is not None:
        parts.append(f"${p.usd:g}")
    if p.other is not None:
        parts.append(f"{OTHER_CURRENCIES[p.other[1]]}{p.other[0]:g}")
    if p.points is not None:
        parts.append(f"{p.points:g} pts")
    if not parts:
        return None
    return ("SB " if p.is_sb else "") + " / ".join(parts)


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
    if best is None and DESC_PRICE_LABEL.search(text):
        return "flatsale"
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
    of = OPEN_FRAC.search(s)
    if of:
        n = of.group(1) or of.group(3)
        return "open" if int(n) > 0 else "closed"
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


def _desc_status(text: str) -> str | None:
    m = DESC_STATUS.search(text[:400])
    if not m:
        return None
    return "closed" if m.group(1).lower() in ("closed", "sold") else "open"


def _is_fraction(num: str, den: str) -> bool:
    """«2/3» — доля лотов; «2026/11» и «11/11» — даты, а не пачка."""
    n, m = int(num), int(den)
    return len(num) < 4 and 2 <= m <= 50 and n <= m


def is_multi(title: str, text: str = "") -> bool:
    """Несколько адоптов в одном посте. Такой пост не равен одному лоту в статистике цен."""
    blob = f"{title} {text}"
    lead = re.sub(r"https?://\S+", " ", f"{title} {text[:MULTI_LEAD]}")
    if MULTI_WORDS.search(lead) or MULTI_EACH.search(lead):
        return True
    for m in MULTI_FRAC.finditer(blob):
        num, den = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if _is_fraction(num, den):
            return True
    return len(PRICE_LIST_ENTRY.findall(blob)) >= 2


def classify_lot(title: str, text: str = "", tags: list[str] | tuple = ()) -> Lot:
    title, text = normalize_text(title), normalize_text(text)
    status = status_of(title)
    if status == "unknown":
        status = _desc_status(text) or status
    lot = Lot(lot_type=lot_type_of(title, text, tags), status=status, multi=is_multi(title, text))
    # Цену ищем сначала в названии, потом в описании. Описание дополняет название:
    # «$9» в названии + «$9 / 900 points» в описании — это 9 долларов и 900 поинтов.
    first = _prices(title)
    if first is None:
        first = _prices(text)
    elif text:
        extra = _prices(text)
        # Вторую валюту берём только из пары «$9 / 900 points» (_pairs ниже): «1500 pts … PNG +$5»
        # не значит, что лот стоит $5.
        if extra and ((first.usd is not None and extra.usd == first.usd)
                      or (first.points is not None and extra.points == first.points)):
            first.is_sb = first.is_sb or extra.is_sb
        for usd, pts in _pairs(text):
            if first.points is None and first.usd == usd:
                first.points = pts
            elif first.usd is None and first.points == pts:
                first.usd = usd
    if first:
        lot.usd, lot.points, lot.other_price = first.usd, first.points, first.other
        lot.is_sb = first.is_sb and (first.usd is not None or first.points is not None or first.other is not None)
        lot.price_text = _price_label(first)
    # Лента — папка группы адоптов: лот с ценой без явного типа — это адопт.
    if lot.lot_type == "other" and (lot.usd is not None or lot.points is not None or lot.other_price):
        lot.lot_type = "adopt"
    return lot


def keyword_hits(keywords: list[str], *texts: str) -> list[str]:
    blob = " ".join(t for t in texts if t)
    # Целое слово, можно во множественном числе: «base» ловит «bases», но не «based».
    return [kw for kw in keywords
            if kw and re.search(r"(?<!\w)" + re.escape(kw) + r"(?:e?s)?(?!\w)", blob, re.I)]


def within_budget(filters: Filters, usd: float | None, points: float | None) -> bool:
    # Цены в €/£ сюда не приходят (usd=None): бюджет в $ к ним не применяем, курс не угадываем.
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
# «LF: anything! O: OTA» — покупатель открыт к любым персам и пишет, чем платит.
OPEN_CALL = re.compile(
    r"\b(?:anything|anyone|whatever|not\s+picky|open\s+to\s+(?:anything|all|everything))\b", re.I)
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
    title = normalize_text(title)
    lf, after_lf = _segment(title, LF_START)
    offer, _ = _segment(title, OFFER_START)
    sell_text = title
    lf_m, offer_m = LF_START.search(title), OFFER_START.search(title)
    if lf and offer and lf_m and offer_m and offer_m.start() > lf_m.start():
        # После «LF: … O:» перечислено, чем платит покупатель («O: OTA», «O: art»),
        # это не признак распродажи.
        _, after_offer = _segment(title[offer_m.start():], OFFER_START)
        sell_text = title[:offer_m.start()] + " " + after_offer

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
    elif (not lf_wants_chars and offer and OPEN_CALL.search(lf) and not _has(CHARS, offer)
          and not SELL_HINTS.search(sell_text) and not re.search(r"\boffers?\b", lf, re.I)):
        lf_wants_chars = True
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
