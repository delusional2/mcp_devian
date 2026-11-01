"""Аукционы на Toyhouse: ставки в постах треда форума.

На Toyhouse нет встроенных аукционов: автор открывает тред (или пост в своём треде продаж),
в первом посте пишет SB/MI/AB и правила, ставки идут ответами, в конце автор пишет
«closed, sold to X». Разбор ставок — тот же, что для комментариев DeviantArt (bids.py):
SB, «+5», AB, отзывы, упоминания не считаются суммами, посты автора — не ставки.

Своё у Toyhouse:
- условия лота берём из её первого поста (SB, MI, AB, валюта, «24h after last bid»);
- в одном треде бывает несколько лотов («#1 SB $10, #2 SB $15») — можно разобрать один лот;
- предложения не деньгами (арт, обмен персами, «credits»/premium/инвайты) записываем отдельно:
  это не ставка, но ей важно их видеть;
- CAD/AUD/NZD/рубли — отдельная валюта, с долларами США не смешиваем.

Посты читаются в её Chrome по одной странице за заход (read_page_js — только DOM открытой
страницы, никаких фоновых запросов), между страницами — человеческая пауза; прочитанное
кэшируется (th_posts), и при следующей проверке дочитываются только новые страницы (read_thread).
Здесь скрипт только собирается строкой. Это ориентир — проверять глазами.
"""

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from . import bids
from .timeparse import relative_to_ts

SOURCE = "toyhouse"
# Ставка при первом чтении треда считается новой, только если она свежая (как у DA).
RECENT_BID_S = 2 * 3600
# Сколько страниц треда читать за одну проверку (по заходу на страницу, с паузой между ними):
# первая (условия) + последние (свежие ставки). Дальше — только новые страницы из кэша.
MAX_PAGES = 5
THREAD_URL = re.compile(r"^https://toyhou\.se(/~forums/[^/?#]+/(\d+)\.[^/?#]*)")
AUCTION_TITLE = re.compile(r"\b(auction|auctions|auc|bta|bid\s*to\s*adopt|bidding|bids?\s+open|аукцион\w*)\b", re.I)

# --- Условия из первого поста ---

_NUMTXT = r"(\d{1,3}(?:,\d{3})+|\d+(?:[.,]\d{1,2})?)\s?(k\b)?"
LABELS = {
    "sb": re.compile(r"\b(?:sb|s\.b\.|starting\s*(?:bid|price)|start(?:ing|s)?\s*at|start\s*bid|стартов\w*(?:\s+ставк\w*)?|"
                     r"старт)\b", re.I),
    "mi": re.compile(r"\b(?:mi|min(?:imum)?\.?\s*(?:bid\s*)?(?:inc(?:rement)?s?|raise|increase)|min\s*inc|"
                     r"шаг(?:\s+ставки)?|минимальный\s+шаг)\b", re.I),
    "ab": re.compile(r"\b(?:ab\d?|auto[\s-]?buy|autobuy|instant\s*buy|buy\s*(?:it\s*)?now|bin|автобай|выкуп)\b", re.I),
}
# «$5 inc», «5 pts increments», «$1 min raise» — шаг, где сумма стоит перед словом.
MI_BEFORE = re.compile(r"(?<![\w.,$€£])((?:[$€£]\s?)?\d+(?:[.,]\d{1,2})?\s?(?:k\b)?\s?(?:\$|usd\b|pts\b|points?\b|€|£)?)"
                       r"\s*(?:min(?:imum)?\.?\s*)?(?:bid\s*)?(?:inc(?:rement)?s?|raise|increase)\b", re.I)
NO_LABEL_VALUE = re.compile(r"^\s*[:=\-–]?\s*(?:none|no|n/?a|nope|tba|tbd|ask|\?+|нет|—|-)\b", re.I)
NO_BEFORE = re.compile(r"\b(?:no|нет|without|без)\s*[:\-]?\s*$", re.I)
ENDS_AFTER = re.compile(
    r"(\d+)\s*(h|hrs?|hours?|d|days?|ч|час\w*|дн\w*|сут\w*)\s*(?:after|from|since|past|после|с\s+момента)\s*"
    r"(?:the\s+)?(?:last|final|latest|последн\w*)", re.I)
UNIT_S = {"h": 3600, "d": 86400, "ч": 3600, "д": 86400, "с": 86400}
ONLY = [
    ("usd", re.compile(r"\b(?:usd|\$|paypal|cash|money|ko-?fi)\s*only\b|\bonly\s+(?:usd|\$|paypal|cash|money)\b|"
                       r"\ball\s+(?:prices|bids)\s+(?:are\s+)?in\s+usd\b|только\s+(?:доллар\w*|usd|деньги)", re.I)),
    ("points", re.compile(r"\b(?:pts|points|da\s*points)\s*only\b|\bonly\s+(?:pts|points)\b|"
                          r"\ball\s+(?:prices|bids)\s+(?:are\s+)?in\s+(?:pts|points)\b|только\s+(?:поинт\w*|pts)", re.I)),
    ("eur", re.compile(r"\b(?:eur|€)\s*only\b|\ball\s+(?:prices|bids)\s+(?:are\s+)?in\s+eur", re.I)),
    ("gbp", re.compile(r"\b(?:gbp|£)\s*only\b|\ball\s+(?:prices|bids)\s+(?:are\s+)?in\s+gbp", re.I)),
]
ACCEPTS = [
    ("art", re.compile(r"\b(?:art|artwork|sketch(?:es)?|commissions?)\s+(?:offers?\s+)?(?:accepted|ok|welcome|allowed)\b|"
                       r"\b(?:accept\w*|taking|open\s+to)\s+(?:art|artwork)\b|принима\w+\s+арт", re.I)),
    ("trade", re.compile(r"\btrades?\s+(?:offers?\s+)?(?:accepted|ok|welcome|allowed)\b|\b(?:accept\w*|taking|open\s+to)\s+"
                         r"trades?\b|принима\w+\s+(?:обмен|трейд)", re.I)),
]
# «USD only» — это про валюту (не поинты), а не запрет арта; запрет — «no art/trades», «money only».
NO_NONCASH = re.compile(r"\bno\s+(?:art|trades?|ocs?|characters?)(?:\s*(?:/|,|or|and)\s*(?:art|trades?|ocs?|characters?))*\b|"
                        r"\b(?:money|cash)\s+only\b|\bonly\s+(?:money|cash)\b|только\s+деньги|без\s+(?:арта|обмен\w*)", re.I)

# Валюты, которые bids.py не знает: «$20 CAD» — не доллары США.
OTHER_CUR = re.compile(r"\b(cad|aud|nzd|sgd|mxn|rub|руб\w*)\b|(?<![\w])(c\$|ca\$|a\$|au\$|nz\$)|₽", re.I)

# --- Предложения не деньгами ---

NONCASH = [
    ("credits", re.compile(r"\b(?:th\s*)?credits?\b|\bpremium\b|\binvite\s*codes?\b|\bинвайт\w*|\bпремиум\w*", re.I)),
    ("trade", re.compile(r"\btrades?\b|\bswap\w*\b|\b(?:my\s+)?(?:ocs?|characters?|chars?|designs?|customs?|myos?)\b|"
                         r"toyhou\.se/\d+|обмен\w*|трейд\w*|перс\w*", re.I)),
    ("art", re.compile(r"\b(?:art|artwork|arts|sketch(?:es)?|drawings?|commissions?|comms?|headshots?|"
                       r"full\s*body|ych)\b|\bарт\w*|рисун\w*", re.I)),
]
OFFER_VERB = re.compile(r"\b(?:offer\w*|can\s+(?:i\s+)?(?:do|give|pay\s+in)|could\s+(?:do|give)|would\s+you\s+(?:take|accept)|"
                        r"accept\w*|trade\s+for|in\s+exchange|плюс|предлага\w*|могу\s+(?:дать|нарисовать|отдать))\b|"
                        r"\+\s*(?:art|арт|sketch|oc|trade)", re.I)
URL = re.compile(r"https?://\S+")
CASH_WORD = re.compile(r"(?<![\w@])(?:sb|ab|autobuy|автобай)(?!\w)|^\W*\+\s?\d", re.I)

# «AB + art», «$20 with a sketch» — довесок не деньгами записываем предложением (_offer_of),
# а для разбора ставки отрезаем: иначе bids.py не узнает в «AB + art» заявку на автобай.
NONCASH_TAIL = re.compile(r"\s*(?:\+|&|\bplus\b|\band\b|\bwith\b|\bw/|плюс|\bи\b)\s*(?:an?\s+|some\s+|my\s+)?"
                          r"(?:art|artwork|sketch(?:es)?|ocs?|trades?|chars?|characters?|арт\w*|обмен\w*|скетч\w*)\b.*$",
                          re.I | re.S)

# --- Победитель в посте автора, которого bids.py не узнает: «Congrats bob!», «bob won» ---
# Ник без @ дописываем «@», если это участник треда и фраза не условная, — дальше закрытие
# и победителя определяет bids.parse_bids (с его проверкой оговорок HEDGE), в нужный момент треда.
CONGRATS_NAME = re.compile(r"\b(congrat\w*|grats|поздравля\w*)\s*(?:to\s+)?(@?)([\w.-]{2,})", re.I)
WON_NAME = re.compile(r"(?<![\w@])(@?)([\w.-]{2,})\s+(?:won|wins|got\s+it|забира\w*)\b", re.I)
CONDITIONAL = re.compile(r"\b(?:if|unless|once|when|whoever|would|could|might|may|maybe|если|когда|кто|может)\b", re.I)


@dataclass
class ThPost:
    post_id: str
    author: str
    text: str
    ts: float
    page: int = 1
    pos: int = 0          # место на странице
    ts_note: str = ""     # «нет времени» | «время позже следующих постов» — время подставлено по порядку


@dataclass
class Terms:
    sb: float | None = None
    mi: float | None = None
    ab: float | None = None
    currency: str | None = None
    ends_after_s: float | None = None
    accepts: list[str] = field(default_factory=list)   # art | trade — она сама разрешила
    cash_only: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class Offer:
    post_id: str
    user: str
    kinds: list[str]            # art | trade | credits
    with_cash: bool             # «$20 + art»: деньги учтены ставкой, это — довесок
    ts: float
    raw: str


@dataclass
class ThAuction:
    thread_id: str | None
    lot: str
    owner: str
    terms: Terms
    state: bids.AuctionState
    offers: list[Offer] = field(default_factory=list)
    lots_found: list[str] = field(default_factory=list)  # номера лотов в первом посте, если их несколько
    due_ts: float | None = None    # по её правилам «N часов после последней ставки» — когда должен закрыться
    posts_seen: int = 0
    incomplete: str = ""           # почему прочитан не весь тред
    info: list[str] = field(default_factory=list)  # пометки не про отброшенные ставки (условия, время, лоты)

    @property
    def currency(self) -> str | None:
        return self.state.currency or self.terms.currency


@dataclass
class ThBidEvent:
    thread_id: str
    lot: str
    title: str
    url: str
    amount: float
    currency: str | None
    user: str | None


@dataclass
class ThreadRead:
    """Что вернул read_thread_js: посты всех прочитанных страниц и честная пометка о неполноте."""
    posts: list[ThPost]
    pages_read: int = 0
    pages_total: int | None = None
    incomplete: bool = False
    reason: str = ""
    state: str = "ready"


# ---------- условия лота ----------

def _amount_after(text: str, start: int, currency: str | None) -> tuple[float, str | None] | None:
    """Сумма сразу после метки (до следующей метки, не дальше 40 символов)."""
    tail = text[start:start + 40]
    nxt = min((m.start() for p in LABELS.values() if (m := p.search(tail, 1))), default=None)
    tail = tail[:nxt] if nxt else tail
    if NO_LABEL_VALUE.match(tail):
        return None
    tail = re.sub(r"^\s*[:=\-–]?\s*(?:is|of|at|will\s+be|будет)\s+", " ", tail, flags=re.I)
    if not re.match(r"\s*[:=\-–]?\s*(?:[$€£]\s*)?\d", tail):
        return None
    if OTHER_CUR.search(tail):
        return None
    found = bids.find_amounts(tail)
    if found:
        return found[0][1], found[0][2]
    bare = re.match(r"\s*[:=\-–]?\s*" + _NUMTXT, tail, re.I)
    if bare:
        return bids._num(bare.group(1), bare.group(2)), currency
    return None


def _labelled(text: str, label: str, currency: str | None) -> tuple[float, str | None] | None:
    for m in LABELS[label].finditer(text):
        if NO_BEFORE.search(text[max(0, m.start() - 12):m.start()]):
            return None  # «No AB»
        got = _amount_after(text, m.end(), currency)
        if got:
            return got
        if NO_LABEL_VALUE.match(text[m.end():m.end() + 12]):
            return None
    return None


def parse_terms(text: str) -> Terms:
    """SB/MI/AB, валюта и правила из первого поста аукциона."""
    text = " ".join(URL.sub(" ", text or "").split())
    t = Terms()
    for cur, pattern in ONLY:
        if pattern.search(text):
            t.currency = cur
            break
    sb = _labelled(text, "sb", t.currency)
    if sb:
        t.sb, sb_cur = sb
        t.currency = t.currency or sb_cur
    ab = _labelled(text, "ab", t.currency)
    if ab:
        if ab[1] and t.currency and ab[1] != t.currency:
            t.notes.append(f"автобай в другой валюте ({bids.money(*ab)}), не сравниваю со ставками")
        else:
            t.ab = ab[0]
            t.currency = t.currency or ab[1]
    mi = _labelled(text, "mi", t.currency)
    if not mi and (m := MI_BEFORE.search(text)):
        mi = _amount_after(m.group(1), 0, t.currency)
    if mi and (not mi[1] or not t.currency or mi[1] == t.currency):
        t.mi = mi[0]
        t.currency = t.currency or mi[1]
    if t.currency is None and (t.sb is not None or t.ab is not None):
        t.notes.append("в условиях нет валюты: числа без $/pts не сравниваю")
    m = ENDS_AFTER.search(text)
    if m:
        t.ends_after_s = int(m.group(1)) * UNIT_S.get(m.group(2)[0].lower(), 3600)
    for kind, pattern in ACCEPTS:
        if pattern.search(text):
            t.accepts.append(kind)
    t.cash_only = bool(NO_NONCASH.search(text)) and not t.accepts
    return t


# ---------- несколько лотов в одном треде ----------

LOT_MARK = re.compile(r"(?:^|[\s(\[|])(?:#|lot\s*|char(?:acter)?\s*#?\s*|лот\s*|№\s*)(\d{1,2})\b|"
                      r"(?:^|\n)\s*(\d{1,2})\s*[).:]\s", re.I)


# «AB on #2», «for lot 2:», «#2 -» — ссылка на лот в ставке вместе с предлогом (его тоже убираем).
LOT_REF = re.compile(r"(?:\b(?:on|for|to|in|для|на)\s+)?(?:#|\blot\s*|\bchar(?:acter)?\s*#?\s*|лот\s*|№\s*)\d{1,2}\b"
                     r"\s*[:\-–)]?", re.I)


def split_lots(first_text: str) -> dict[str, str]:
    """{«1»: текст условий лота 1, ...}, если в первом посте несколько лотов со своими SB. Иначе {}.

    Метки «#1», «lot 1» — лот, если в куске есть SB или AB. Нумерованный список «1) … 2) …» — лот,
    только если в куске есть SB: иначе «1) SB $10 / 2) MI $1 / 3) AB $60» — это пункты правил."""
    marks = [(m.start(), m.group(1) or m.group(2), bool(m.group(2))) for m in LOT_MARK.finditer(first_text or "")]
    out: dict[str, str] = {}
    for i, (pos, num, listed) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(first_text)
        chunk = first_text[pos:end]
        if LABELS["sb"].search(chunk) or (not listed and LABELS["ab"].search(chunk)):
            out.setdefault(num, chunk)
    return out if len(out) >= 2 else {}


def _post_lot(text: str) -> str | None:
    m = LOT_MARK.search(text)
    return (m.group(1) or m.group(2)) if m else None


def _strip_lot(text: str) -> str:
    if LOT_REF.search(text):
        return LOT_REF.sub(" ", text, count=1)
    return LOT_MARK.sub(" ", text, count=1)


def _lot_chunk(text: str, lot: str) -> str | None:
    """Кусок поста автора про этот лот: «#1 sold to @bob, #2 still open» → для #2 — «#2 still open».
    Без номеров лотов — весь пост (касается всех лотов); номера есть, а этого нет — None."""
    marks = [(m.start(), m.group(1) or m.group(2)) for m in LOT_MARK.finditer(text)]
    if not marks:
        return text
    for i, (pos, num) in enumerate(marks):
        if num == lot:
            return text[pos:marks[i + 1][0] if i + 1 < len(marks) else len(text)]
    return None


# ---------- посты ----------

def _ts_exact(value) -> float | None:
    """Время, не зависящее от часового пояса Mac: число (epoch) или ISO с явным поясом."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) or None
    text = str(value).strip()
    if text.isdigit():
        return float(text) or None
    iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo else None


def _ts_local(value) -> float | None:
    """Дата без пояса («1 Nov 2026, 10:00 am») — читается как местное время Mac, поэтому последний вариант."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    for fmt in ("%d %b %Y, %I:%M:%S %p", "%d %b %Y, %H:%M:%S", "%b %d, %Y, %I:%M:%S %p", "%b %d, %Y %I:%M %p",
                "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def _post_ts(p: dict, now: float) -> float | None:
    """Точное время (epoch/ISO с поясом) → относительное («3 hours ago», не зависит от пояса, как
    toyhouse._abbr_ts) → дата без пояса (может уехать на разницу поясов)."""
    keys = ("ts", "when_title", "when")
    for get in (_ts_exact, lambda v: relative_to_ts(str(v), now) if v else None, _ts_local):
        for k in keys:
            got = get(p.get(k))
            if got:
                return got
    return None


def _fix_times(out: list[ThPost], now: float) -> None:
    """Порядок постов — со страницы; время подгоняем под него.

    - нет времени → время соседнего поста до него (или первого известного после);
    - время позже «сейчас», а после него на странице есть пост не позже «сейчас» — часы/пояс
      соврали: ставим время того следующего поста (иначе все посты после него выпали бы как «будущие»);
    - время меньше, чем у поста выше, → подтягиваем вверх (относительное время грубое).
    Посты в самом конце со временем позже now остаются «будущими»: analyse_thread их не берёт."""
    known = [p.ts for p in out if p.ts]
    prev = known[0] if known else 0.0
    for p in out:
        if not p.ts:
            p.ts, p.ts_note = prev, "нет времени"
        prev = p.ts
    nxt = None
    for p in reversed(out):
        if p.ts <= now:
            nxt = p.ts
        elif nxt is not None:
            p.ts, p.ts_note = nxt, "время позже следующих постов"
    last = 0.0
    for p in out:
        if p.ts < last:
            p.ts = last
        last = p.ts


def to_posts(raw: list, now: float | None = None) -> list[ThPost]:
    """Посты из скрипта/кэша (или уже ThPost) → по порядку на странице, время не убывает.

    Время на Toyhouse часто относительное («3 hours ago»), то есть грубое. Порядок поэтому берём
    из страницы (страница, место на ней), а время подгоняем под порядок (_fix_times)."""
    now = now or time.time()
    out: list[ThPost] = []
    seen: set[str] = set()
    for i, p in enumerate(raw or []):
        if isinstance(p, ThPost):
            post = p
        elif isinstance(p, dict) and p.get("post_id"):
            ts = _post_ts(p, now)
            pos = p.get("pos")
            post = ThPost(post_id=str(p["post_id"]).removeprefix("post-"), author=str(p.get("author") or "").strip(),
                          text=str(p.get("text") or "").strip(), ts=ts or 0.0, page=int(p.get("page") or 1),
                          pos=int(pos) if isinstance(pos, int | float) else i, ts_note=str(p.get("ts_note") or ""))
        else:
            continue
        if post.post_id in seen:
            continue
        seen.add(post.post_id)
        out.append(post)
    out.sort(key=lambda p: (p.page, p.pos))  # внутри страницы — порядок, в котором посты стоят на ней
    _fix_times(out, now)
    return out


def _comment(p: ThPost, seq: int, text: str | None = None) -> bids.Comment:
    # comment_id — порядковый номер: parse_bids сортирует по (время, номер), а время у нас грубое.
    return bids.Comment(comment_id=str(seq), parent_id=None, user=p.author, posted_ts=p.ts,
                        text=p.text if text is None else text)


def _clean_for_bid(text: str) -> str:
    return URL.sub(" ", text)


def _noncash(text: str) -> list[str]:
    return [kind for kind, pattern in NONCASH if pattern.search(text)]


def _offer_of(p: ThPost, owner_l: str) -> Offer | None:
    if p.author.lower() == owner_l:
        return None
    text = " ".join(p.text.split())
    kinds = _noncash(text)
    if not kinds:
        return None
    cash = bool(bids.find_amounts(_clean_for_bid(text)) or CASH_WORD.search(text))
    th_link = bool(re.search(r"toyhou\.se/\d+", text))
    # «Can I offer art?», «AB + art», «trade offer: <ссылка>», короткое «art?» — это предложение.
    # Короткое со ставкой («$20 (love this design!!)») — просто ставка: слово «design» ещё не обмен.
    if not (OFFER_VERB.search(text) or NONCASH_TAIL.search(text) or th_link or (len(text) <= 40 and not cash)):
        return None
    return Offer(p.post_id, p.author, kinds, cash, p.ts, text[:80])


def _clause(text: str, start: int, end: int) -> str:
    """Фраза вокруг совпадения: от начала предложения до ближайшей запятой/точки после него.
    «If no one else bids, bob wins» — условие перед; «bob won, pay when you can» — «when» уже не про победу."""
    a = max(text.rfind(ch, 0, start) for ch in ".!?;\n") + 1
    b = min([i for i in (text.find(ch, end) for ch in ",.!?;\n") if i != -1] or [len(text)])
    return text[a:b]


def _name_winners(text: str, commenters: dict[str, str]) -> str:
    """«Congrats bob!» → «Congrats @bob!», «bob won» → «@bob won», если bob писал в треде (в этом лоте)
    и фраза не условная («if no one else bids, bob wins»). Дальше победителя и момент закрытия
    определяет bids.parse_bids — с его же проверкой оговорок («so far», «currently»)."""
    def fix(m, name_group: int, at_group: int, make) -> str:
        name = m.group(name_group).rstrip(".-!")
        if m.group(at_group) or name.lower() not in commenters or name.lower() in bids.NOT_NAMES:
            return m.group(0)
        clause = _clause(text, m.start(), m.end())
        if CONDITIONAL.search(clause) or bids.HEDGE.search(clause):
            return m.group(0)
        return make(m, name) + m.group(name_group)[len(name):]

    text = CONGRATS_NAME.sub(lambda m: fix(m, 3, 2, lambda m, n: f"{m.group(1)} @{n}"), text)
    return WON_NAME.sub(lambda m: fix(m, 2, 1, lambda m, n: f"@{n} won"), text)


def analyse_thread(posts: list, owner: str | None = None, lot: str | None = None, terms: Terms | None = None,
                   thread_id: str | None = None, now: float | None = None, incomplete: str = "") -> ThAuction:
    """Разбор ставок в треде. owner — автор аукциона (по умолчанию автор первого поста).

    Первый пост автора — условия (SB/MI/AB), не «закрытие»: его в parse_bids не отдаём.
    Посты позже now не смотрим (без заглядывания в будущее)."""
    now = now or time.time()
    every = to_posts(posts, now)
    items = [p for p in every if p.ts <= now]
    info: list[str] = []
    if len(items) < len(every):
        info.append(f"постов со временем позже момента разбора: {len(every) - len(items)} — не учитываю")
    fixed = Counter(p.ts_note for p in items if p.ts_note)
    if fixed:
        info.append("время подставлено по порядку на странице: " + ", ".join(f"{k} — {n}" for k, n in fixed.items()))
    owner = owner or (items[0].author if items else "")
    owner_l = owner.lower()
    opening = items[0] if items and items[0].author.lower() == owner_l else None
    lots = split_lots(opening.text) if opening else {}
    lot = str(lot) if lot not in (None, "") else ""
    if terms is None:
        base = opening.text if opening else ""
        terms = parse_terms(lots.get(lot, base) if lot else base)
        if lot and lot in lots:
            whole = parse_terms(base)  # «USD only», «24h after last bid» обычно пишут один раз на весь тред
            terms.currency = terms.currency or whole.currency
            terms.ends_after_s = terms.ends_after_s or whole.ends_after_s
            terms.cash_only = terms.cash_only or whole.cash_only
            terms.accepts = terms.accepts or whole.accepts
    info.extend(terms.notes)
    if lot and not lots:
        info.append(f"в первом посте нет лотов с номерами — лот #{lot} разобран как весь тред")
    rest = items[1:] if opening else items

    comments: list[bids.Comment] = []
    extra_notes: list[str] = []
    for seq, p in enumerate(rest, start=1):
        text = _clean_for_bid(p.text)
        if p.author.lower() == owner_l:
            if lot and lots:
                chunk = _lot_chunk(text, lot)
                if chunk is None:
                    continue
                text = chunk
        else:
            if OTHER_CUR.search(text) and bids.find_amounts(text):
                extra_notes.append(f"{p.author}: сумма в другой валюте («{' '.join(p.text.split())[:40]}»), не учитываю")
                continue
            if lots and lot:
                which = _post_lot(text)
                if which != lot:
                    if which is None and re.search(r"\d", text):
                        extra_notes.append(f"{p.author}: не указан номер лота — не знаю, к какому лоту ставка")
                    continue
                text = _strip_lot(text)
            text = NONCASH_TAIL.sub("", text) or text
        comments.append(_comment(p, seq, text))
    commenters = {c.user.lower(): c.user for c in comments if c.user and c.user.lower() != owner_l}
    for c in comments:
        if c.user.lower() == owner_l:
            c.text = _name_winners(c.text, commenters)
    state = bids.parse_bids(comments, owner=owner, sb=terms.sb, ab=terms.ab, currency=terms.currency, min_inc=terms.mi)
    state.notes.extend(extra_notes)
    if lots and not lot:
        # Весь тред с несколькими лотами: «#1 sold to @bob, #2 still open» не закрывает тред целиком,
        # а ставки разных лотов нельзя сравнивать. Итог — только по лотам (analyse_lots).
        state.ended, state.winner, state.ab_hit = False, None, False
        info.append(f"в треде {len(lots)} лотов — ставки и закрытие смотри по каждому лоту")

    offers = [o for p in rest if (o := _offer_of(p, owner_l))
              and (not (lots and lot) or _post_lot(p.text) == lot)]
    if offers and terms.cash_only:
        info.append(f"предложений не деньгами: {len(offers)}, а в условиях — только деньги")
    due = None
    if terms.ends_after_s and state.last_bid_ts and not state.ended:
        due = state.last_bid_ts + terms.ends_after_s
    return ThAuction(thread_id=thread_id, lot=lot, owner=owner, terms=terms, state=state, offers=offers,
                     lots_found=sorted(lots, key=lambda s: int(s)) if lots else [], due_ts=due,
                     posts_seen=len(items), incomplete=incomplete, info=info)


def analyse_lots(posts: list, owner: str | None = None, thread_id: str | None = None, now: float | None = None,
                 incomplete: str = "") -> list[ThAuction]:
    """Тред целиком: один разбор, а если в первом посте несколько лотов — по разбору на каждый лот."""
    now = now or time.time()
    items = to_posts(posts, now)
    whole = analyse_thread(items, owner, thread_id=thread_id, now=now, incomplete=incomplete)
    if not whole.lots_found:
        return [whole]
    return [analyse_thread(items, owner, lot=n, thread_id=thread_id, now=now, incomplete=incomplete)
            for n in whole.lots_found]


# ---------- текст для неё ----------

OFFER_NAMES = {"art": "арт", "trade": "обмен", "credits": "credits/premium/инвайты"}


def summary(a: ThAuction, now: float | None = None) -> str:
    """Та же сводка, что у DA (bids.auction_summary), плюс то, что есть только на Toyhouse."""
    now = now or time.time()
    text = bids.auction_summary(a.state, sb=a.terms.sb, ab=a.terms.ab, currency=a.currency, now=now)
    extra = []
    if a.lot:
        extra.append(f"лот #{a.lot}")
    elif a.lots_found:
        extra.append(f"в треде {len(a.lots_found)} лотов (#{', #'.join(a.lots_found)}) — разбор общий, "
                     "смотри по номеру лота")
    if a.offers:
        pure = [o for o in a.offers if not o.with_cash]
        kinds = sorted({OFFER_NAMES[k] for o in a.offers for k in o.kinds})
        extra.append(f"не деньгами: {len(a.offers)} (без суммы {len(pure)}; {', '.join(kinds)}) — "
                     f"от {', '.join(dict.fromkeys(o.user for o in a.offers))}"[:200])
    if a.due_ts:
        left = a.due_ts - now
        extra.append("по твоим правилам аукцион уже должен был закрыться" if left <= 0
                     else f"по правилам закроется через {max(1, int(left // 3600))} ч, если не будет ставок")
    if a.incomplete:
        extra.append(f"прочитано не всё: {a.incomplete}")
    extra.extend(n for n in a.info if not (a.lots_found and not a.lot and n.startswith("в треде ")))
    text = text.removesuffix(" Это разбор комментариев — проверь глазами.")
    for old, new in (("комментариев", "постов"), ("комментария", "поста"), ("комментарий", "пост")):
        text = text.replace(old, new)
    if extra:
        line = "; ".join(extra)
        text += " " + line[0].upper() + line[1:] + "."
    return text + " Это разбор постов треда — проверь глазами."


def summary_lots(auctions: list[ThAuction], now: float | None = None) -> str:
    """Сводка по треду: один аукцион — как summary; несколько лотов — строка на лот."""
    if len(auctions) == 1:
        return summary(auctions[0], now)
    return "\n".join(f"Лот #{a.lot}: {summary(a, now)}" for a in auctions)


def describe_event(e: ThBidEvent) -> str:
    who = f" от {e.user}" if e.user else ""
    lot = f" (лот #{e.lot})" if e.lot else ""
    return f"Toyhouse: новая ставка {bids.money(e.amount, e.currency)}{who} — «{e.title[:60]}»{lot}"


# ---------- таблица и события ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS th_auctions (
    thread_id TEXT NOT NULL,       -- номер треда (как в items: th:<номер>)
    lot TEXT NOT NULL DEFAULT '',  -- номер лота, если в треде несколько
    url TEXT,
    title TEXT,
    owner TEXT,
    own INTEGER DEFAULT 0,
    sb REAL, ab REAL, mi REAL,
    currency TEXT,
    top_amount REAL,
    top_user TEXT,
    n_bids INTEGER DEFAULT 0,
    bidders INTEGER DEFAULT 0,
    last_bid_ts REAL,
    ab_hit INTEGER DEFAULT 0,
    ended INTEGER DEFAULT 0,
    winner TEXT,
    offers INTEGER DEFAULT 0,
    posts_seen INTEGER DEFAULT 0,
    incomplete TEXT,
    notified_top REAL,
    notified_ts REAL,
    first_seen_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    extra TEXT,
    PRIMARY KEY (thread_id, lot)
);
-- Прочитанные посты тредов: при следующей проверке дочитываем только новые страницы.
CREATE TABLE IF NOT EXISTS th_posts (
    thread_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    pos INTEGER NOT NULL,
    author TEXT,
    text TEXT,
    ts REAL,                       -- время поста на момент чтения (NULL — не разобралось)
    seen_ts REAL NOT NULL,
    PRIMARY KEY (thread_id, post_id)
);
CREATE TABLE IF NOT EXISTS th_threads (
    thread_id TEXT PRIMARY KEY,
    pages_total INTEGER,
    last_page INTEGER,             -- до какой страницы прочитано подряд с конца
    skipped TEXT,                  -- страницы, которые пропустили (длинный тред), «3–17»
    updated_ts REAL NOT NULL
);
"""


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def thread_id_of(url: str) -> str | None:
    m = THREAD_URL.match((url or "").strip())
    return m.group(2) if m else None


def item_id(thread_id: str, lot: str = "") -> str:
    """id для sales.item_id: th:<тред> или th:<тред>#<лот>."""
    return f"th:{thread_id}" + (f"#{lot}" if lot else "")


def watch(conn, url: str, title: str = "", lot: str = "", own: bool = True, now: float | None = None) -> str:
    """Начать следить за аукционом-тредом (её или чужим). Возвращает номер треда."""
    tid = thread_id_of(url)
    if not tid:
        raise ValueError("Нужна ссылка на тред форума Toyhouse вида https://toyhou.se/~forums/13.adoption-center/123.name")
    ensure(conn)
    now = now or time.time()
    conn.execute("INSERT INTO th_auctions (thread_id, lot, url, title, own, first_seen_ts, updated_ts) "
                 "VALUES (?,?,?,?,?,?,?) ON CONFLICT(thread_id, lot) DO UPDATE SET url = excluded.url, "
                 "title = COALESCE(NULLIF(excluded.title, ''), th_auctions.title), own = excluded.own",
                 (tid, lot or "", page1_url(url), title, int(own), now, 0))
    conn.commit()
    return tid


def decide_own(owner: str, my_username: str = "", mine: bool | None = None) -> bool | None:
    """Её ли аукцион: по нику из настроек; ника нет — что она сказала (mine); иначе None —
    save оставит как было (а новую строку запишет как чужую: продажу без уверенности не заводим)."""
    if my_username:
        return (owner or "").lstrip("@").lower() == my_username.lstrip("@").lower()
    return mine


def save(conn, a: ThAuction, url: str, title: str = "", own: bool | None = None,
         now: float | None = None) -> ThBidEvent | None:
    """Сохранить разбор. Вернуть событие, если появилась ставка, о которой она ещё не знает.

    own=None — оставить, как было в строке (watch(mine=...)); новой строке — 0.
    Логика как в auctions.save: про сумму говорим один раз; при первом чтении — только свежие ставки."""
    ensure(conn)
    now = now or time.time()
    tid = a.thread_id or thread_id_of(url)
    if not tid:
        raise ValueError("нет номера треда")
    old = conn.execute("SELECT * FROM th_auctions WHERE thread_id = ? AND lot = ?", (tid, a.lot)).fetchone()
    if own is None:
        whole = old or conn.execute("SELECT own FROM th_auctions WHERE thread_id = ? AND lot = ''", (tid,)).fetchone()
        own = bool(whole["own"]) if whole else False
    top = a.state.top
    read_before = bool(old and old["updated_ts"])
    notified = old["notified_top"] if old else None
    notified_ts = old["notified_ts"] if old else None
    if top is None:
        fresh = False
    elif not read_before:
        fresh = bool(top.ts) and top.ts >= now - RECENT_BID_S
    else:
        fresh = (notified is None or top.amount > notified
                 or (bool(top.ts) and notified_ts is not None and top.amount != notified and top.ts > notified_ts))
    if top is not None and (fresh or not read_before):
        notified, notified_ts = top.amount, top.ts
    extra = {"notes": a.state.notes[:10], "info": a.info[:10], "offers": [o.__dict__ for o in a.offers[:10]],
             "lots": a.lots_found, "due_ts": a.due_ts}
    conn.execute(
        "INSERT INTO th_auctions (thread_id, lot, url, title, owner, own, sb, ab, mi, currency, top_amount, top_user, "
        "n_bids, bidders, last_bid_ts, ab_hit, ended, winner, offers, posts_seen, incomplete, notified_top, notified_ts, "
        "first_seen_ts, updated_ts, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(thread_id, lot) DO UPDATE SET url=excluded.url, title=COALESCE(NULLIF(excluded.title, ''), "
        "th_auctions.title), owner=excluded.owner, own=excluded.own, sb=excluded.sb, ab=excluded.ab, mi=excluded.mi, "
        "currency=excluded.currency, top_amount=excluded.top_amount, top_user=excluded.top_user, n_bids=excluded.n_bids, "
        "bidders=excluded.bidders, last_bid_ts=excluded.last_bid_ts, ab_hit=excluded.ab_hit, ended=excluded.ended, "
        "winner=excluded.winner, offers=excluded.offers, posts_seen=excluded.posts_seen, incomplete=excluded.incomplete, "
        "notified_top=excluded.notified_top, notified_ts=excluded.notified_ts, updated_ts=excluded.updated_ts, "
        "extra=excluded.extra",
        (tid, a.lot, page1_url(url), title, a.owner, int(own), a.terms.sb, a.terms.ab, a.terms.mi, a.currency,
         top.amount if top else None, top.user if top else None, a.state.n_bids, a.state.bidders, a.state.last_bid_ts,
         int(a.state.ab_hit), int(a.state.ended), a.state.winner, len(a.offers), a.posts_seen, a.incomplete or None,
         notified, notified_ts, now, now, json.dumps(extra, ensure_ascii=False)))
    conn.commit()
    if top is not None and fresh:
        return ThBidEvent(tid, a.lot, title or (old["title"] if old else "") or f"тред {tid}", page1_url(url),
                          top.amount, a.currency, top.user)
    return None


def save_all(conn, auctions: list[ThAuction], url: str, title: str = "", own: bool | None = None,
             now: float | None = None) -> list[ThBidEvent]:
    """Сохранить разбор треда (analyse_lots). Нашлись лоты — строка «весь тред» (lot='') больше не нужна:
    её own/title переходят к лотам, а саму строку убираем, чтобы она не висела «ещё не читались»."""
    ensure(conn)
    events = []
    tid = next((a.thread_id for a in auctions if a.thread_id), None) or thread_id_of(url)
    if any(a.lot for a in auctions) and tid:
        whole = conn.execute("SELECT own, title FROM th_auctions WHERE thread_id = ? AND lot = ''", (tid,)).fetchone()
        if whole:
            title = title or whole["title"] or ""
            if own is None:
                own = bool(whole["own"])
    for a in auctions:
        e = save(conn, a, url, title, own=own, now=now)
        if e:
            events.append(e)
    if any(a.lot for a in auctions) and tid:
        conn.execute("DELETE FROM th_auctions WHERE thread_id = ? AND lot = ''", (tid,))
        conn.commit()
    return events


def to_refresh(conn, my_username: str = "", now: float | None = None, limit: int = 10) -> list[dict]:
    """Какие треды перечитать: её открытые аукционы за 3 недели — из th_auctions (она просила следить)
    и из items (её треды, где в названии auction/BTA). Один тред — одна строка, даже если в нём
    несколько лотов (читаем один раз, разбираем analyse_lots). Сначала её, потом чужие; внутри — те,
    что дольше не читались."""
    ensure(conn)
    now = now or time.time()
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT thread_id, url, title, own, updated_ts FROM th_auctions WHERE ended = 0 AND "
                          "COALESCE(NULLIF(updated_ts, 0), first_seen_ts) >= ? ORDER BY updated_ts", (now - 21 * 86400,)):
        got = out.setdefault(r["thread_id"], {"thread_id": r["thread_id"], "url": r["url"], "title": r["title"],
                                              "own": r["own"], "updated_ts": r["updated_ts"] or 0})
        got["own"] = max(got["own"] or 0, r["own"] or 0)
        got["updated_ts"] = min(got["updated_ts"], r["updated_ts"] or 0)
    if my_username:
        rows = conn.execute(
            "SELECT id, url, title FROM items WHERE source = 'toyhouse' AND lower(author) = lower(?) "
            "AND COALESCE(activity_ts, published_ts, first_seen_ts) >= ? ORDER BY activity_ts DESC",
            (my_username, now - 21 * 86400)).fetchall()
        for r in rows:
            tid = r["id"].removeprefix("th:")
            if AUCTION_TITLE.search(r["title"] or "") and tid not in out:
                known = conn.execute("SELECT 1 FROM th_auctions WHERE thread_id = ?", (tid,)).fetchone()
                if not known:  # есть строки, но все закрыты — не перечитываем
                    out[tid] = {"thread_id": tid, "url": r["url"], "title": r["title"], "own": 1, "updated_ts": 0}
    # Её треды — первыми (чужие, за которыми она следит, — только данные рынка), внутри — кто дольше не читался.
    return sorted(out.values(), key=lambda e: (not e["own"], e["updated_ts"] or 0))[:limit]


def summary_row(row) -> str:
    """Коротко по сохранённой строке th_auctions (для списка её аукционов)."""
    if not row["updated_ts"]:
        return "ставки ещё не читались"
    parts = [f"ставок {row['n_bids']}" + (f" от {row['bidders']} чел." if row["bidders"] else "")]
    if row["top_amount"] is not None:
        parts.append(f"максимум {bids.money(row['top_amount'], row['currency'])}"
                     + (f" у {row['top_user']}" if row["top_user"] else ""))
    if row["offers"]:
        parts.append(f"не деньгами {row['offers']}")
    if row["ab_hit"]:
        parts.append("автобай взят")
    if row["ended"]:
        parts.append("закрыт" + (f", победитель {row['winner']}" if row["winner"] else ""))
    if row["incomplete"]:
        parts.append("прочитан не весь тред")
    return ", ".join(parts)


def my_rows(conn, limit: int = 20) -> list:
    """Её аукционы на Toyhouse из таблицы: сначала открытые, потом свежие."""
    ensure(conn)
    return conn.execute("SELECT * FROM th_auctions WHERE own = 1 ORDER BY ended, updated_ts DESC LIMIT ?",
                        (limit,)).fetchall()


CHECK_AMOUNT = "сумму проверь: победитель не тот, кто ставил больше всех (автобай, отзыв ставки?)"
CHECK_BUYER = "автор не назвала победителя — записала того, кто ставил больше всех; проверь"
NONCASH_WIN = "победитель назван, денежных ставок нет — похоже, оплата артом/обменом; сумму впиши сама"


def sale_candidates(conn) -> list[dict]:
    """Её закрытые аукционы на Toyhouse, по которым ещё нет продажи: готовые аргументы для sales.add_sale."""
    ensure(conn)
    rows = conn.execute(
        "SELECT * FROM th_auctions t WHERE own = 1 AND ended = 1 AND (top_amount IS NOT NULL OR winner IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM sales s WHERE s.item_id = ('th:' || t.thread_id || "
        "CASE WHEN t.lot = '' THEN '' ELSE '#' || t.lot END))").fetchall()
    out = []
    for r in rows:
        if not r["lot"] and (json.loads(r["extra"] or "{}").get("lots") or []):
            continue  # тред с лотами целиком — не продажа; продажи — по лотам
        winner, amount, note = r["winner"], r["top_amount"], ""
        if amount is None:
            if not r["offers"] or not winner:
                continue
            note = NONCASH_WIN
        elif not winner:
            if not r["top_user"]:
                continue
            winner, note = r["top_user"], CHECK_BUYER
        elif r["top_user"] and winner.lstrip("@").lower() != r["top_user"].lstrip("@").lower():
            amount, note = None, CHECK_AMOUNT
        currency = r["currency"] if r["currency"] in ("usd", "points", "eur", "gbp") and amount is not None else None
        title = (r["title"] or f"Toyhouse тред {r['thread_id']}") + (f" — лот #{r['lot']}" if r["lot"] else "")
        out.append({"title": title, "amount": amount, "currency": currency, "platform": "toyhouse",
                    "format": "auction", "buyer": winner, "item_id": item_id(r["thread_id"], r["lot"]),
                    "source": "auto", "note": note, "if_new": True})
    return out


def sale_item_id(conn, url: str, lot: str = "") -> str:
    """item_id для её ручной записи продажи по ссылке на тред Toyhouse (sales.record): тот же,
    что у авто-записи победителя, — тогда ручная запись сольётся с ней, а не задвоится."""
    tid = thread_id_of(url)
    if not tid:
        raise ValueError("Нужна ссылка на тред форума Toyhouse вида https://toyhou.se/~forums/13.adoption-center/123.name")
    lot = str(lot or "").lstrip("#").strip()
    if not lot:
        ensure(conn)
        lots = [r["lot"] for r in conn.execute("SELECT lot FROM th_auctions WHERE thread_id = ? AND lot != '' "
                                               "ORDER BY lot", (tid,))]
        if lots:
            raise ValueError(f"В этом треде несколько лотов (#{', #'.join(lots)}) — укажи номер лота.")
    return item_id(tid, lot)


def sync_sales(conn, now: float | None = None) -> list[str]:
    """Закрытые её аукционы Toyhouse → продажа «ждёт оплаты» (один раз на лот, как sales.sync_from_auctions)."""
    from . import sales

    created = []
    for kw in sale_candidates(conn):
        sale_id = sales.add_sale(conn, now=now, **kw)
        if sale_id:
            created.append(sale_id)
    return created


# ---------- её Chrome: страницы треда по одной ----------

def page1_url(url: str) -> str:
    m = THREAD_URL.match((url or "").strip())
    return f"https://toyhou.se{m.group(1)}" if m else (url or "").split("#")[0].split("?")[0]


def page_url(url: str, n: int) -> str:
    base = page1_url(url)
    return base if n <= 1 else f"{base}?page={n}"


READY_SELECTOR = ".forum-post"


def read_page_js(thread_url: str) -> str:
    """Скрипт для run_in_chrome(page_url(url, n), js, READY_SELECTOR): посты ОТКРЫТОЙ страницы треда.

    - работает только на https://toyhou.se/~forums/<доска>/<этот тред>.*; иначе {state: 'wrong_page'};
    - читает только DOM открытой страницы: никаких XHR/fetch — каждая страница открывается обычным
      заходом, а между страницами Python делает человеческую паузу (read_thread);
    - проверка Cloudflare → {state: 'challenge'}; постов нет → {state: 'no_posts'};
    - цитаты (blockquote) из текста поста убирает: процитированная чужая ставка — не ставка;
    - на странице ничего не меняет и ничего не отправляет.
    Возвращает {state, thread_id, page, pages_total, posts: [{post_id, author, text, when, when_title}]}."""
    m = THREAD_URL.match((thread_url or "").strip())
    if not m:
        raise ValueError("Нужна ссылка на тред форума Toyhouse.")
    tid = m.group(2)
    return r"""(function () {
  var TID = """ + json.dumps(tid) + r""";
  var pm = /^\/~forums\/[^\/]+\/(\d+)\./.exec(location.pathname);
  if (location.origin !== 'https://toyhou.se' || !pm || pm[1] !== TID) {
    return JSON.stringify({state: 'wrong_page'});
  }
  if (/_cf_chl_opt/.test(document.documentElement.innerHTML.slice(0, 200000)) ||
      document.getElementById('challenge-form') || document.getElementById('challenge-error-text') ||
      document.querySelector('.cf-turnstile')) {
    return JSON.stringify({state: 'challenge'});
  }
  var cur = Number((/[?&]page=(\d+)/.exec(location.search) || [])[1] || 1) || 1;
  function clean(s) { return (s || '').replace(/[ \t ]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim(); }
  function outside(list) {
    return Array.prototype.slice.call(list).filter(function (x) { return !x.closest('.user-content'); })[0] || null;
  }
  function bodyText(body) {
    if (!body) return '';
    var c = body.cloneNode(true);
    Array.prototype.slice.call(c.querySelectorAll('blockquote, .fr-quote, .forum-quote, .quote, script, style'))
      .forEach(function (q) { q.parentNode.removeChild(q); });
    Array.prototype.slice.call(c.querySelectorAll('br')).forEach(function (b) {
      b.parentNode.replaceChild(document.createTextNode('\n'), b);
    });
    Array.prototype.slice.call(c.querySelectorAll('p, div, li')).forEach(function (b) {
      b.appendChild(document.createTextNode('\n'));
    });
    return clean(c.textContent);
  }
  var posts = Array.prototype.slice.call(document.querySelectorAll('.forum-post')).map(function (p) {
    var a = p.querySelector('a[id^="post-"]');
    var cm = (p.className || '').match(/\bpost-(\d+)\b/);
    var pid = a ? a.id.replace(/^post-/, '') : (cm ? cm[1] : null);
    var au = outside(p.querySelectorAll('.forum-post-user-badge a, .display-user a'));
    var t = outside(p.querySelectorAll('time[datetime], abbr.datetime'));
    return {post_id: pid, author: au ? au.textContent.replace(/\s+/g, ' ').trim() : '',
            text: bodyText(p.querySelector('.user-content')),
            when: t ? t.textContent.replace(/\s+/g, ' ').trim() : '',
            when_title: t ? (t.getAttribute('datetime') || t.getAttribute('title') || '') : ''};
  }).filter(function (x) { return x.post_id; });
  if (!posts.length) return JSON.stringify({state: 'no_posts'});
  var total = cur;
  Array.prototype.slice.call(document.querySelectorAll('.pagination a[href]')).forEach(function (a) {
    if (a.closest('.forum-post, .user-content')) return;
    var u; try { u = new URL(a.getAttribute('href'), location.href); } catch (e) { return; }
    var m = /^\/~forums\/[^\/]+\/(\d+)\./.exec(u.pathname);
    if (u.origin !== 'https://toyhou.se' || !m || m[1] !== TID) return;
    var n = Number(u.searchParams.get('page') || 0);
    if (n > total) total = n;
  });
  return JSON.stringify({state: 'ready', thread_id: TID, page: cur, pages_total: total, posts: posts});
})()"""


STATE_REASONS = {
    "wrong_page": "вкладка открылась не на этом треде",
    "no_posts": "на странице нет постов (тред удалён/закрыт, или Toyhouse поменял разметку)",
    "challenge": "Toyhouse попросил проверку Cloudflare — нажми галочку в Chrome и проверь ещё раз",
    "error": "скрипт вернул непонятный ответ",
}


@dataclass
class PageRead:
    state: str
    page: int = 1
    pages_total: int = 1
    posts: list[dict] = field(default_factory=list)
    reason: str = ""


def page_from_extracted(raw) -> PageRead:
    """Ответ read_page_js (строка JSON, dict или None от run_in_chrome) → PageRead. Мусор — не исключение."""
    if raw is None or raw == "":
        return PageRead("no_posts", reason=STATE_REASONS["no_posts"])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return PageRead("error", reason=STATE_REASONS["error"])
    if not isinstance(raw, dict):
        return PageRead("error", reason=STATE_REASONS["error"])
    state = str(raw.get("state") or "error")
    if state != "ready":
        return PageRead(state, reason=STATE_REASONS.get(state, STATE_REASONS["error"]))
    page = raw.get("page")
    page = int(page) if isinstance(page, int | float) and page >= 1 else 1
    total = raw.get("pages_total")
    total = max(page, int(total)) if isinstance(total, int | float) else page
    posts = [dict(p, page=page, pos=i) for i, p in enumerate(raw.get("posts") or [])
             if isinstance(p, dict) and p.get("post_id")]
    if not posts:
        return PageRead("no_posts", page, total, reason=STATE_REASONS["no_posts"])
    return PageRead("ready", page, total, posts)


def thread_from_extracted(raw, now: float | None = None) -> ThreadRead:
    """Одна прочитанная страница → ThreadRead (без кэша)."""
    got = page_from_extracted(raw)
    if got.state != "ready":
        return ThreadRead([], incomplete=True, reason=got.reason, state=got.state)
    return ThreadRead(to_posts(got.posts, now), pages_read=1, pages_total=got.pages_total,
                      incomplete=got.pages_total > 1, reason=f"прочитана страница {got.page} из {got.pages_total}"
                      if got.pages_total > 1 else "")


def _pages_span(pages: list[int]) -> str:
    if not pages:
        return ""
    return str(pages[0]) if len(pages) == 1 else f"{pages[0]}–{pages[-1]}"


def _cached(conn, tid: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT post_id, author, text, ts, page, pos FROM th_posts WHERE thread_id = ? "
        "ORDER BY page, pos, CAST(post_id AS INTEGER)", (tid,))]


def _must_stop(e: Exception) -> bool:
    """Cloudflare, закрытое окно, занятый или не настроенный Chrome — это не «одна страница не прочиталась»:
    проход по тредам должен остановиться, даже если у треда есть кэш."""
    from . import her_chrome
    from . import toyhouse as th

    return isinstance(e, (th.ChallengeError, th.BrowserClosed, th.BrowserBusy, her_chrome.ChromeSetupError))


def read_thread(fetch, url: str, conn=None, max_pages: int = MAX_PAGES, pause=None, now: float | None = None,
                full: bool = False) -> ThreadRead:
    """Прочитать тред по страницам: fetch(page_url) → ответ read_page_js (или None), pause() — между заходами.

    Первый раз: страница 1 (условия) и последние страницы, всего не больше max_pages заходов; середина
    длинного треда пропускается с честной пометкой. С кэшем (conn): заходим на последнюю прочитанную
    страницу и дочитываем только новые. full=True — забыть кэш и читать заново.
    Ошибка на первой странице без кэша — исключение fetch как есть; на следующих — стоп, incomplete."""
    tid = thread_id_of(url)
    if not tid:
        raise ValueError("Нужна ссылка на тред форума Toyhouse вида https://toyhou.se/~forums/13.adoption-center/123.name")
    now = now or time.time()
    budget = max(2, int(max_pages))
    meta = None
    if conn is not None:
        ensure(conn)
        if full:
            conn.execute("DELETE FROM th_posts WHERE thread_id = ?", (tid,))
            conn.execute("DELETE FROM th_threads WHERE thread_id = ?", (tid,))
            conn.commit()
        meta = conn.execute("SELECT * FROM th_threads WHERE thread_id = ?", (tid,)).fetchone()
        if meta and not conn.execute("SELECT 1 FROM th_posts WHERE thread_id = ? LIMIT 1", (tid,)).fetchone():
            meta = None
    start = max(1, int(meta["last_page"] or 1)) if meta else 1
    reasons: list[str] = []
    skipped: list[int] = []
    reads: list[PageRead] = []
    stop: Exception | None = None
    try:
        first = page_from_extracted(fetch(page_url(url, start)))
    except Exception as e:  # noqa: BLE001 — с кэшем отдаём прочитанное раньше, без кэша — ошибка как есть
        if not meta or _must_stop(e):
            raise
        first = PageRead("error", reason=str(e))
    total = int(meta["pages_total"] or start) if meta else 1
    if first.state != "ready":
        if not meta:
            return ThreadRead([], incomplete=True, reason=first.reason, state=first.state)
        reasons.append(f"страница {start}: {first.reason}")
        if first.state == "no_posts" and start > 1:
            # Посты удалили, страниц стало меньше — в следующий раз начнём с первой.
            conn.execute("UPDATE th_threads SET last_page = 1 WHERE thread_id = ?", (tid,))
            conn.commit()
    else:
        reads.append(first)
        total = max(first.pages_total, first.page)
        want = list(range(first.page + 1, total + 1))
        if len(want) > budget - 1:
            skipped, want = want[:-(budget - 1)], want[-(budget - 1):]
        for n in want:
            if pause:
                pause()
            try:
                got = page_from_extracted(fetch(page_url(url, n)))
            except Exception as e:  # noqa: BLE001 — прочитанное не выбрасываем, честно помечаем
                reasons.append(f"страница {n}: {e}")
                stop = e if _must_stop(e) else None  # прочитанное сохраним, потом — стоп всему проходу
                break
            if got.state != "ready":
                reasons.append(f"страница {n}: {got.reason}")
                break
            reads.append(got)
    raw = [p for r in reads for p in r.posts]
    for p in raw:
        p["ts"] = _post_ts(p, now)
    gap = _pages_span(skipped)
    if conn is not None and reads:
        conn.executemany(
            "INSERT INTO th_posts (thread_id, post_id, page, pos, author, text, ts, seen_ts) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(thread_id, post_id) DO UPDATE SET page = excluded.page, pos = excluded.pos, "
            "author = excluded.author, text = excluded.text, ts = COALESCE(th_posts.ts, excluded.ts)",
            [(tid, str(p["post_id"]).removeprefix("post-"), p["page"], p["pos"], str(p.get("author") or ""),
              str(p.get("text") or ""), p["ts"], now) for p in raw])
        old_gap = (meta["skipped"] if meta else "") or ""
        conn.execute(
            "INSERT INTO th_threads (thread_id, pages_total, last_page, skipped, updated_ts) VALUES (?,?,?,?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET pages_total = excluded.pages_total, last_page = excluded.last_page, "
            "skipped = excluded.skipped, updated_ts = excluded.updated_ts",
            (tid, total, max(r.page for r in reads), "; ".join(x for x in (old_gap, gap) if x) or None, now))
        conn.commit()
        meta = conn.execute("SELECT * FROM th_threads WHERE thread_id = ?", (tid,)).fetchone()
    if stop is not None:
        raise stop
    if conn is not None and meta is not None:
        posts = _cached(conn, tid)
        gap = meta["skipped"] or ""
    else:
        posts = raw
    if gap:
        reasons.append(f"пропущены страницы {gap} (длинный тред; full=True — прочитать всё)")
    return ThreadRead(to_posts(posts, now), pages_read=len(reads), pages_total=total, incomplete=bool(reasons),
                      reason="; ".join(reasons))


def refresh(conn, read: ThreadRead, entry: dict, owner: str | None = None,
            now: float | None = None) -> tuple[list[ThAuction], list[ThBidEvent]]:
    """Прочитанный тред (read_thread) → разбор по лотам → сохранить. entry — строка из to_refresh
    ({thread_id, url, title, own}). Возвращает (разборы, новые ставки для уведомлений)."""
    now = now or time.time()
    auctions = analyse_lots(read.posts, owner, thread_id=entry["thread_id"], now=now,
                            incomplete=read.reason if read.incomplete else "")
    own = None if entry.get("own") is None else bool(entry["own"])
    return auctions, save_all(conn, auctions, entry["url"], entry.get("title") or "", own=own, now=now)


# ---------- чтение треда: её Chrome или окно бота ----------

def run_th_thread(url: str, conn=None, max_pages: int | None = None, full: bool = False, interactive: bool = False,
                  runner=None, challenge_wait_s: int = 120, lock_wait_s: float | None = None,
                  pause=None) -> ThreadRead:
    """Прочитать тред в её Chrome: страница за заходом, с паузой, под общим замком Chrome.
    Ошибки Chrome (ChromeSetupError, ChallengeError, BrowserBusy, ToyhouseError) — наружу."""
    from . import her_chrome
    from . import toyhouse as th

    url = (url or "").strip()
    if not THREAD_URL.match(url):
        raise th.ToyhouseError("Нужна ссылка на тред форума Toyhouse.")
    js = read_page_js(url)
    wait = challenge_wait_s if interactive else 0

    def fetch(u: str):
        return her_chrome.run_in_chrome(u, js, READY_SELECTOR, challenge_wait_s=wait, keep_on_challenge=True,
                                        runner=runner)

    with her_chrome.chrome_turn(her_chrome.LOCK_WAIT_S if lock_wait_s is None else lock_wait_s):
        return read_thread(fetch, url, conn=conn, max_pages=max_pages or MAX_PAGES,
                           pause=th.human_pause if pause is None else pause, full=full)


def run_th_thread_posts(url: str, **kwargs) -> ThreadRead:
    """То же, что run_th_thread (имя из спецификации подключения)."""
    return run_th_thread(url, **kwargs)


def read_thread_bot(reader, url: str, conn=None, max_pages: int | None = None, full: bool = False,
                    pause=None) -> ThreadRead:
    """Режим bot: reader — toyhouse.Reader (своё окно Chrome); страница — page.goto + тот же скрипт."""
    from . import toyhouse as th

    js = read_page_js(url)

    def fetch(u: str):
        reader.get(u, READY_SELECTOR)
        return reader.page.evaluate(js)

    return read_thread(fetch, url, conn=conn, max_pages=max_pages or MAX_PAGES,
                       pause=th.human_pause if pause is None else pause, full=full)
