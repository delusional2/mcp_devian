"""LF-треды Toyhouse → какой её перс подходит, что мешает и где шанс продать выше.

Из названия и первого поста треда собираем профиль запроса (LFProfile): какие виды и тип тела
ищут, что просят НЕ предлагать, бюджет (в валюте автора, без пересчёта), чем платят, закрыт ли
тред. Потом для каждого её перса считаем Fit: баллы, причины и «что мешает». Перс с хоть одной
помехой в отклик не предлагается — так не тратятся её 8 откликов в день.

Всё здесь — разбор чужого текста правилами, без догадок: чего не нашли, того не утверждаем.
"""

import json
import re
import time
from dataclasses import asdict, dataclass, field

from . import taxonomy
from .classify import _prices

DAY = 86400

# --- Статус треда ---

# «Закрыт» смотрим в названии и только как статус: «[closed]», «(found)», «Closed!» в конце,
# «thread closed», «Found, thank you!». «Closed species», «found family» — это не статус.
_NOT_STATUS = r"(?!\s*(?:species|specie|spec|cs|trades?|groups?|adopts?|ota|bta|family)\b)"
CLOSED_TITLE = re.compile(
    rf"[\[(]\s*(?:closed|found|done)\b{_NOT_STATUS}[^\])]*[\])]"
    rf"|^\W*closed\b{_NOT_STATUS}"
    r"|(?<!not\s)(?<!eyes\s)(?<!eye\s)\bclosed\W*$|\bthread\s+closed\b|\bnot\s+looking\b(?!\s+for\b)"
    r"|\bfound\W*$|\bfound\s*,?\s*thank",
    re.I)
PAUSED_TITLE = re.compile(
    r"[\[(]\s*(?:on\s*-?\s*hold|paused?|hiatus)\b[^\])]*[\])]|\bon\s*-?\s*hold\W*$|\bpaused\W*$", re.I)
# Первый пост, который автор поправил на «FOUND» / «CLOSED» / «EDIT: found».
SNIPPET_CLOSED = re.compile(
    r"^\W*(?:closed|found)\b|\b(?:edit|update|upd)\W{0,3}(?:closed|found)\b|\bthread\s+(?:is\s+)?closed\b", re.I)
SNIPPET_PAUSED = re.compile(r"^\W*(?:on\s*-?\s*hold|paused)\b|\b(?:edit|update|upd)\W{0,3}(?:on\s*-?\s*hold|paused)\b",
                            re.I)
# «Found a couple but still looking!», «found some, open for more», «Closed species are fine» — тред живой.
SNIPPET_STILL_OPEN = re.compile(
    r"\bstill\s+(?:lf|looking|open|searching|accepting|buying|interested|taking|want)"
    r"|\b(?:open|looking)\s+(?:for|to)\s+more\b|\bmore\s+(?:welcome|wanted)\b"
    r"|\b(?:closed|found)\s+(?:species|specie|cs|family)\b", re.I)

# Часть названия после «LF:» до «O:» / «|» — там то, что ищут (а не то, чем платят).
LF_PART = re.compile(r"\b(?:lf|looking\s+for|seeking|iso)\b\s*[:\-]?\s*(.+?)(?=\||\bo(?:f|ffering)?\s*:|$)", re.I)

# --- Виды ---

LF_HYPERNYMS: dict[str, set[str]] = {
    "canine": {"dog", "wolf", "fox", "kitsune"},
    "canines": {"dog", "wolf", "fox", "kitsune"},
    "feline": {"cat", "tiger", "lion"},
    "felines": {"cat", "tiger", "lion"},
    "reptile": {"dragon", "snake"},
    "reptiles": {"dragon", "snake"},
    "avian": {"bird", "griffin", "harpy"},
    "avians": {"bird", "griffin", "harpy"},
    "insect": {"insect", "moth", "butterfly"},
    "insects": {"insect", "moth", "butterfly"},
    "bug": {"insect", "moth", "butterfly"},
    "bugs": {"insect", "moth", "butterfly"},
    "winged": {"dragon", "bird", "moth", "butterfly", "angel", "bat", "fairy", "griffin", "harpy"},
    "wings": {"dragon", "bird", "moth", "butterfly", "angel", "bat", "fairy", "griffin", "harpy"},
    # taxonomy знает «werewolf» как волка, а «werewolves» её шаблон не ловит.
    "werewolves": {"wolf"},
    "werewolf": {"wolf"},
}
# Сами по себе ничего не говорят о персе: совпадение только по ним — не совпадение.
GENERIC = {"oc", "ocs", "cute", "male", "female", "character", "characters", "design", "designs", "adopt",
           "adoptable", "adoptables", "adopts", "sona", "sonas", "girl", "boy", "girls", "boys", "kid", "kids"}
OPEN_CALL = re.compile(
    r"\b(?:any(?:thing|one)?|not\s+picky|open\s+to\s+(?:all|anything|any)|all\s+species|whatever|surprise\s+me)\b",
    re.I)
# Слова в «LF: …», которые не описывают перса: если кроме них ничего нет — запрос открытый («LF: OCs!»).
_LF_FILLER = {"lf", "looking", "for", "seeking", "iso", "my", "your", "our", "a", "an", "the", "to", "buy", "adopt",
              "new", "some", "more", "cheap", "please", "pls", "and", "or", "with", "in", "of", "on", "story",
              "stories", "world", "home", "homes", "love", "gift", "friend", "friends", "usd", "pts", "points",
              "paypal", "budget", "offers", "offer", "chars", "char", "babies", "bbs", "maybe", "again", "still",
              "open", "now", "too", "also", "etc", "thread", "need", "want", "wanted", "buying", "help"}

# --- «Не предлагать» ---

_NEG_TRIGGER = re.compile(
    r"\b(?:not\s+(?:looking\s+for|interested\s+in|accepting|wanting|taking|after|into)"
    r"|(?:i\s+)?(?:don['’]?t|do\s+not|doesn['’]?t|won['’]?t)\s+(?:want|need|like|accept|take)"
    r"|anything\s+(?:but|except)|except(?:\s+for)?|besides|without|avoid(?:ing)?|nothing|no|not)\b\s*(?:any\s+|more\s+)?",
    re.I)
_CLAUSE_END = re.compile(r"[.!?;\n|()\[\]:]")
# Слово, на котором перечисление «чего не надо» кончается.
_ITEM_STOP = re.compile(
    r"\b(?:but|only|please|pls|plz|thanks?|thank|thx|unless|if|when|because|since|though|tho|lol|jk|as|so|"
    r"i|i'?m|my|we|you|it|this|that|preferred|preferably|ty|sorry|either|too|also|anymore|atm|rn|right\s+now|"
    r"at\s+all|for\s+now|currently|really|honestly|personally)\b", re.I)
_ITEM_SPLIT = re.compile(r"\s*(?:,|/|&|\+|\bor\b|\band\b|\bnor\b)\s*", re.I)
_ITEM_FILLER = {"ones", "one", "characters", "character", "chars", "char", "ocs", "oc", "designs", "design",
                "adopts", "adopt", "the", "a", "an", "any", "more", "type", "types", "kind", "kinds", "of",
                "please", "pls", "species"}
# «no pressure», «no worries», «no offers» — вежливость или условие, а не «каких персов не надо».
NEG_STOP = {"pressure", "worries", "worry", "rush", "problem", "problems", "need", "needs", "thanks", "hurry",
            "idea", "clue", "ping", "pings", "dms", "dm", "longer", "more", "one", "matter", "offer", "offers",
            "picky", "sure", "looking", "interested", "accepting", "ota", "budget", "limit", "lowballing",
            "lowballs", "lowball", "hate", "rude", "ghosting", "spam", "bumping", "bumps", "bump", "pressuring",
            "stealing", "art", "trades", "trade", "money", "points", "pts", "usd", "paypal", "one's", "rp",
            "nsfw-only", "hurt", "harm", "drama", "reselling", "resell", "tracing", "ai"}

# --- Деньги ---

_CUR = r"(?:\$|usd|€|eur|£|gbp|pts|points?|da\s?points|dap)"
_NUM = r"\d+(?:[.,]\d+)?\s?k?"
_AMT = rf"(?:(?:\$|€|£)\s?{_NUM}|(?:usd|eur|gbp)\s?{_NUM}|{_NUM}\s?{_CUR})"
_TAIL_NUM = rf"(?:{_AMT}|\d+(?:[.,]\d+)?k?(?!\s?%))"
_RANGE = re.compile(
    rf"(?:budget|spend|pay(?:ing)?|price\s+range|range)\b[^.\n\d$€£]{{0,25}}?({_AMT})\s*(?:-|–|—|to)\s*({_TAIL_NUM})"
    rf"|({_AMT})\s*(?:-|–|—|to)\s*({_TAIL_NUM})\s*(?:budget|range)\b"
    rf"|(?:budget|range)\b[^.\n\d$€£]{{0,25}}?(\d+(?:[.,]\d+)?k?)\s*(?:-|–|—|to)\s*({_AMT})"
    rf"|\bprices?\s*:\s*({_AMT})\s*(?:-|–|—|to)\s*({_TAIL_NUM})", re.I)
_BUDGET_ONE = [
    re.compile(rf"\bbudget\b(?:\s+is)?(?:\s+(?:around|about|of|roughly|approx\.?|is))?\s*[:\-~=]?\s*"
               rf"(?:up\s+to|under|max(?:imum)?|around|about|~|of)?\s*({_AMT})", re.I),
    re.compile(rf"\bno\s+offers?\s+(?:over|above|higher\s+than|more\s+than)\s*({_AMT})", re.I),
    re.compile(rf"\b(?:won'?t|can'?t|cannot|can\s+not|will\s+not|not\s+able\s+to|unable\s+to)\s+(?:pay|spend|go)\s+"
               rf"(?:over|more\s+than|above|past|higher\s+than|beyond)\s*({_AMT})", re.I),
    re.compile(rf"\b(?:up\s+to|under|below|max(?:imum)?|no\s+more\s+than|less\s+than|at\s+most)\s*[:\-]?\s*({_AMT})",
               re.I),
    re.compile(rf"({_AMT})\s*(?:max(?:imum)?|or\s+less|or\s+under|tops|at\s+most|is\s+my\s+(?:max|limit|budget)"
               rf"|budget)\b", re.I),
    re.compile(rf"\b(?:max(?:imum)?|limit)\s+is\s*({_AMT})", re.I),
    re.compile(rf"\bnothing\s+(?:over|above|more\s+than|higher\s+than)\s*({_AMT})", re.I),
]
_NO_OFFERS = re.compile(r"\bno\s+offers?\b(?!\s+(?:over|above|higher|more|under|below))"
                        r"|\bnot\s+(?:taking|accepting)\s+offers?\b|\boffers?\s+closed\b", re.I)
_NO_OTA = re.compile(r"\bno\s+ota\b|\bnot\s+(?:taking|accepting|doing)\s+ota\b", re.I)

# «paying via paypal invoice only», «ko-fi only», «only points», «(art only)».
_PAY_WORD = r"(paypal|ko-?fi|cash\s?app|venmo|usd|\$+|money|cash|points?|pts|da\s?points|art|trades?)"
_PAY_ONLY = re.compile(
    rf"(?<!\w){_PAY_WORD}(?:\s+(?!but\b|or\b|and\b)[\w-]+){{0,3}}\s*\bonly\b"
    rf"|\bonly\s+(?:accepting\s+|paying\s+(?:via|with|in|through)\s+|via\s+|through\s+|in\s+)?{_PAY_WORD}(?!\w)", re.I)
PAY_GROUP = {"paypal": "usd", "ko-fi": "usd", "cashapp": "usd", "venmo": "usd", "usd": "usd", "money": "usd",
             "points": "points", "art": "art", "trades": "trades"}
PAY_LABEL = {"paypal": "через PayPal", "ko-fi": "через Ko-fi", "cashapp": "через Cash App", "venmo": "через Venmo",
             "usd": "деньгами", "money": "деньгами", "points": "поинтами", "art": "артом", "trades": "обменом"}
CURRENCY_LABEL = {"usd": "$", "points": "pts", "eur": "€", "gbp": "£"}

_PAYS_SNIPPET = {
    "usd": re.compile(r"\$\s?\d|\d\s?\$|\busd\b|paypal|ko-?fi|cash\s?app|venmo|\bmoney\b|€|£", re.I),
    "points": re.compile(r"\bpts\b|\bpoints?\b|\bda\s?points\b", re.I),
    "art": re.compile(r"\b(?:can|will|could)\s+(?:also\s+)?(?:do|offer|draw|pay\s+(?:in|with))\s+art\b"
                      r"|\b(?:offering|paying\s+(?:in|with))\s+art\b|\bart\s+(?:only|offers?)\b", re.I),
    "trades": re.compile(r"\b(?:can|will|could)\s+(?:also\s+)?(?:offer\s+)?trades?\b|\btrades?\s+(?:ok|welcome|only)\b"
                         r"|\boffering\s+trades?\b", re.I),
}


@dataclass
class LFProfile:
    closed: bool = False
    paused: bool = False
    wanted_species: set[str] = field(default_factory=set)
    wanted_body: set[str] = field(default_factory=set)
    # «cat», «anthro», «hybrid» или связка «cat+pastel» («no cats with pastel colors»).
    negated: set[str] = field(default_factory=set)
    open_call: bool = False
    # Бюджет: верхняя граница и валюта автора (usd, points, eur, gbp). Не пересчитываем.
    budget: tuple[float, str] | None = None
    pays: set[str] = field(default_factory=set)
    pays_only: str | None = None
    no_offers: bool = False
    no_ota: bool = False
    # Автор исключил всё, что сам ищет («LF wolves … anything except canines»): что предлагать — непонятно.
    contradictory: bool = False
    # Как автор написал «не надо» — чтобы показать ей его словами.
    negated_text: list[str] = field(default_factory=list)
    # Вид не назван, но в «LF: …» есть приметы («pastel», «sky themed»): подходят персы с такими тегами.
    wanted_other: set[str] = field(default_factory=set)


@dataclass
class Fit:
    score: int
    reasons: list[str]
    blockers: list[str]


# --- Разбор ---

def _singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ves") and len(w) > 4:
        return w[:-3] + "f"
    if w.endswith(("ches", "shes", "xes", "sses")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


_BIG_CATS = re.compile(r"\bbig\s+cats?\b", re.I)


def _hyper(text: str) -> set[str]:
    """Широкие слова: свои (LF_HYPERNYMS) и общие с аналитикой (taxonomy.HYPERNYMS: rodents, equines, undead)."""
    out: set[str] = set(taxonomy.broader_species(text))
    for w in re.findall(r"[a-z]+", text.lower()):
        out |= LF_HYPERNYMS.get(w, set())
    if _BIG_CATS.search(text):
        out |= {"tiger", "lion"}
    return out


def species_in(text: str) -> set[str]:
    return set(taxonomy.tag_text(text, (), "").species) | _hyper(text)


def _features(text: str) -> tuple[set[str], set[str], set[str]]:
    """(виды, тип тела, стиль) из куска текста."""
    t = taxonomy.tag_text(text, (), "")
    return set(t.species) | _hyper(text), set(t.body), set(t.styles)


def _neg_items(blob: str) -> tuple[set[str], list[str], list[tuple[int, int]]]:
    """(что нельзя, как автор это написал, куски текста с отрицаниями).

    Отрицание действует до конца фразы: «no hybrids or customs» — оба, «not looking for anthro ones» —
    anthro (а не «looking»). Новое «no» внутри списка начинает новое отрицание, а «but», «only»,
    «please» список заканчивают."""
    negated: set[str] = set()
    shown: list[str] = []
    spans: list[tuple[int, int]] = []
    triggers = list(_NEG_TRIGGER.finditer(blob))
    for i, m in enumerate(triggers):
        start = m.end()
        end = len(blob)
        stop = _CLAUSE_END.search(blob, start)
        if stop:
            end = stop.start()
        if i + 1 < len(triggers) and triggers[i + 1].start() < end:
            end = triggers[i + 1].start()
        seg = blob[start:end]
        cut = _ITEM_STOP.search(seg)
        if cut:
            seg = seg[:cut.start()]
        if not seg.strip():
            continue
        spans.append((m.start(), start + len(seg)))
        for raw in _ITEM_SPLIT.split(seg):
            words = [w for w in re.findall(r"[\w'\-]+", raw.lower()) if w not in _ITEM_FILLER][:6]
            if not words:
                continue
            item = " ".join(words)
            sp, body, styles = _features(item)
            feats = sp | body | styles
            if not feats:
                # Без вида и стиля: «hybrids», «customs» — запоминаем само слово.
                if len(words) > 2 or any(w in NEG_STOP or _singular(w) in NEG_STOP for w in words):
                    continue
                negated.add(" ".join([*words[:-1], _singular(words[-1])]))
            elif " with " in f" {raw.lower()} " or (sp and (body or styles)) or (body and styles):
                # «cats with pastel colors», «anthro dragons» — нельзя только такое сочетание.
                negated.add("+".join(sorted(feats)))
            else:
                negated |= feats
            if item not in shown:
                shown.append(item)
    return negated, shown, spans


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    out = list(text)
    for a, b in spans:
        for k in range(a, min(b, len(out))):
            out[k] = " "
    return "".join(out)


def _amount(raw: str) -> tuple[float, str] | None:
    """«$40», «20$», «2k pts», «€20», «60 USD» → (сумма, валюта). Без пересчёта валют."""
    s = raw.lower().replace(" ", "")
    m = re.search(r"(\d+(?:[.,]\d+)?)(k?)", s)
    if not m:
        return None
    num = m.group(1)
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", num):
        num = re.sub(r"[.,]", "", num)
    value = float(num.replace(",", ".")) * (1000 if m.group(2) else 1)
    if "€" in s or "eur" in s:
        return value, "eur"
    if "£" in s or "gbp" in s:
        return value, "gbp"
    if re.search(r"pts|point|dap", s):
        return value, "points"
    if "$" in s or "usd" in s:
        return value, "usd"
    return None


def _budget(blob: str) -> tuple[float, str] | None:
    """Верхняя граница бюджета. Несколько сумм в одной валюте — берём большую (не отсекаем лишнего)."""
    found: list[tuple[int, tuple[float, str]]] = []
    for m in _RANGE.finditer(blob):
        groups = [g for g in m.groups()]
        lo_raw, hi_raw = next((groups[k], groups[k + 1]) for k in (0, 2, 4, 6) if groups[k])
        lo, hi = _amount(lo_raw), _amount(hi_raw)
        if lo and not hi and (num := re.search(r"\d+(?:[.,]\d+)?k?", hi_raw)) and \
                (_amount("$" + num.group()) or (0, ""))[0] < lo[0]:
            continue  # «paying $20 to 3 people» — 3 не верх диапазона
        if lo and not hi:
            num = re.search(r"\d+(?:[.,]\d+)?k?", hi_raw)
            hi = _amount(num.group() + CURRENCY_LABEL.get(lo[1], "")) if num and lo[1] != "usd" else (
                _amount("$" + num.group()) if num else None)
        if hi:
            found.append((m.start(), hi))
    for rx in _BUDGET_ONE:
        for m in rx.finditer(blob):
            got = _amount(m.group(1))
            if got:
                found.append((m.start(), got))
    if not found:
        return None
    found.sort(key=lambda t: t[0])
    currency = found[0][1][1]
    return max(v for _, (v, c) in found if c == currency), currency


def _pay_key(word: str) -> str:
    w = word.lower().replace(" ", "")
    if w.startswith("ko"):
        return "ko-fi"
    if w.startswith("cash") and "app" in w:
        return "cashapp"
    if w.startswith("$") or w == "usd":
        return "usd"
    if w in ("cash", "money"):
        return "money"
    if w.startswith(("point", "pts", "da")):
        return "points"
    if w.startswith("trade"):
        return "trades"
    return w


def pays_only(blob: str) -> str | None:
    m = _PAY_ONLY.search(blob)
    if not m:
        return None
    return _pay_key(m.group(1) or m.group(2))


def payments(title: str, snippet: str | None) -> set[str]:
    """Чем платит автор треда: из названия (O: …) и первого поста."""
    from .classify import classify_thread

    pays = set(classify_thread(title or "").pays)
    for key, rx in _PAYS_SNIPPET.items():
        if snippet and rx.search(snippet):
            pays.add(key)
    only = pays_only(f"{title or ''}\n{snippet or ''}")
    if only:
        pays = {PAY_GROUP.get(only, only)}
    return pays


def thread_status(title: str, snippet: str | None) -> tuple[bool, bool]:
    """(закрыт, на паузе) — по названию и по началу первого поста (его могли поправить на FOUND)."""
    head = (snippet or "")[:300]

    def edited(rx) -> bool:
        m = rx.search(head)
        return bool(m) and not SNIPPET_STILL_OPEN.search(head[m.start():m.start() + 200])
    closed = bool(CLOSED_TITLE.search(title or "")) or edited(SNIPPET_CLOSED)
    paused = bool(PAUSED_TITLE.search(title or "")) or edited(SNIPPET_PAUSED)
    return closed, paused


_OWN_SENTENCE = re.compile(
    r"[^.!?\n]*\b(?:my|mine|i\s+(?:already\s+)?(?:have|own|got)|trad(?:e|es|ing)|offering)\b[^.!?\n]*", re.I)


def lf_profile(title: str, snippet: str | None) -> LFProfile:
    title = title or ""
    blob = f"{title}\n{snippet or ''}"
    closed, paused = thread_status(title, snippet)
    negated, shown, spans = _neg_items(blob)
    positive = _blank(blob, spans)
    lf = LF_PART.search(_blank(title, [(a, b) for a, b in spans if b <= len(title)]))
    title_part = lf.group(1) if lf else positive[:len(title)]
    body_text = _OWN_SENTENCE.sub(" ", positive[len(title):])
    want_text = f"{title_part}\n{body_text}"
    sp, body, _ = _features(title_part)
    if not sp and not body:
        # Вид в названии не назван — тогда смотрим пост, кроме фраз о своих персах и обменах
        # («my other OC is a dragon», «or trade my dragon OCs»).
        sp, body, _ = _features(body_text)
    flat = {n for n in negated if "+" not in n}
    wanted = sp - flat
    contradictory = bool(sp) and not wanted
    wanted_body = body - flat
    other: set[str] = set()
    open_call = False
    if not sp and not wanted_body:
        tags = taxonomy.tag_text(title_part, (), "")
        words = {_singular(w) for w in re.findall(r"[a-z]{3,}", title_part.lower())
                 if w not in GENERIC and w not in _LF_FILLER and _singular(w) not in _LF_FILLER}
        other = (set(tags.styles) | set(tags.themes) | words) - GENERIC
        open_call = bool(OPEN_CALL.search(want_text)) or not other
    return LFProfile(
        closed=closed, paused=paused, wanted_species=wanted, wanted_body=wanted_body, negated=negated,
        open_call=open_call, budget=_budget(blob), pays=payments(title, snippet), pays_only=pays_only(blob),
        no_offers=bool(_NO_OFFERS.search(blob)), no_ota=bool(_NO_OTA.search(blob)),
        contradictory=contradictory, negated_text=shown, wanted_other=set() if open_call else other)


def profile_dict(p: LFProfile) -> dict:
    """Для items.extra['lf']: множества — отсортированными списками (JSON)."""
    out = asdict(p)
    for k, v in out.items():
        if isinstance(v, set):
            out[k] = sorted(v)
        elif isinstance(v, tuple):
            out[k] = list(v)
    return out


# --- Её перс ---

def char_prices(ch: dict) -> list[tuple[float, str]]:
    """Все цены перса: «$20 / 2000 pts» → [(20, usd), (2000, points)]. Валюты не пересчитываем."""
    raw = str(ch.get("price") or "").strip()
    if not raw:
        return []
    p = _prices(raw)
    out: list[tuple[float, str]] = []
    if p:
        if p.usd is not None:
            out.append((p.usd, "usd"))
        if p.points is not None:
            out.append((p.points, "points"))
        if p.other:
            out.append(p.other)
    if not out:
        one = _amount(raw)
        out = [one] if one else []
    return out


def char_price(ch: dict, currency: str | None = None) -> tuple[float, str] | None:
    """Цена перса в валюте currency, если такая есть, иначе первая указанная."""
    prices = char_prices(ch)
    found = [pr for pr in prices if pr[1] == currency] or prices
    return found[0] if found else None


def accepts_of(characters: list[dict]) -> frozenset[str]:
    """В чём она продаёт: валюты цен её персов (по умолчанию доллары)."""
    cur = {pr[1] for ch in characters or [] if isinstance(ch, dict) for pr in char_prices(ch)}
    return frozenset(cur or {"usd"})


# Эти виды — гуманоиды, даже если тип тела у перса не указан («LF: humanoids» → эльф подходит).
HUMANOID_SPECIES = {"elf", "vampire", "angel", "demon", "witch", "fairy", "mermaid", "human", "kemonomimi"}


def _char_feats(ch: dict) -> tuple[set[str], set[str], set[str], set[str]]:
    tags = [str(t) for t in (ch.get("tags") or []) if isinstance(t, str) and t.strip()]
    name = str(ch.get("name") or "")
    # Тип тела — только целыми словами: в слитном разборе тегов «humanoid» содержит «human».
    # Её теги — целыми словами: «dragonfly» не дракон, «wolfsbane» не волк.
    t = taxonomy.tag_text(" ".join([name, *tags]), (), "")
    species = set(t.species) | _hyper(" ".join(tags))
    raw = {_singular(w) for tag in tags for w in re.findall(r"[\w\-]+", tag.lower())} - GENERIC
    raw |= {" ".join([*tag.lower().split()[:-1], _singular(tag.lower().split()[-1])]) for tag in tags if tag.split()}
    body = set(t.body)
    if not body and species & HUMANOID_SPECIES:
        body.add("humanoid")
    return species, body, set(t.styles), raw


def _fmt(amount: tuple[float, str]) -> str:
    v, cur = amount
    num = f"{v:g}"
    return {"usd": f"${num}", "eur": f"€{num}", "gbp": f"£{num}"}.get(cur, f"{num} {CURRENCY_LABEL.get(cur, cur)}")


_NAME_CUE = re.compile(r"\b(?:named|called|name\s+is|names?\s*:)\s*$", re.I)


def _name_hit(name: str, text: str) -> bool:
    """Автор треда назвал её перса по имени. «Sky themed ones» в начале фразы — не имя: имя-слово
    из словаря (вид, стиль, тема) или в начале предложения засчитываем только после «named/called»."""
    if not name or not text.strip():
        return False
    plain = not taxonomy.tag_text(name, (), "").all()
    for m in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text):
        before = text[:m.start()]
        if _NAME_CUE.search(before):
            return True
        sentence_start = not before.strip() or re.search(r"[.!?:\n|]\s*$", before)
        if plain and not sentence_start:
            return True
    return False


def character_fit(p: LFProfile, ch: dict, accepts: frozenset[str] = frozenset({"usd"}),
                  title: str = "", snippet: str | None = None) -> Fit:
    """Насколько её перс подходит под запрос. Помехи (blockers) — причины НЕ предлагать."""
    species, body, styles, raw = _char_feats(ch)
    feats = species | body | styles | raw
    score, reasons, blockers = 0, [], []
    if p.closed:
        blockers.append("тред закрыт или автор уже нашёл")
    if p.paused:
        blockers.append("тред на паузе")
    for n in sorted(p.negated):
        parts = n.split("+")
        if all(part in feats for part in parts):
            blockers.append("автор просит не предлагать: " + " ".join(parts))
    name_hit = _name_hit(str(ch.get("name") or "").strip(), f"{title}\n{snippet or ''}")
    same = species & p.wanted_species
    if same:
        score += 40
        reasons.append("вид: " + ", ".join(sorted(same)))
        if p.wanted_body:
            if body & p.wanted_body:
                score += 10
                reasons.append("тип тела: " + ", ".join(sorted(body & p.wanted_body)))
            elif body:
                blockers.append(f"нужен {'/'.join(sorted(p.wanted_body))}, у перса {'/'.join(sorted(body))}")
    elif p.contradictory:
        blockers.append("автор исключил всё, что сам ищет — непонятно, что предлагать")
    elif p.wanted_species:
        if name_hit:
            score += 20
            reasons.append("автор назвал перса по имени")
        else:
            blockers.append("ищут другой вид: " + ", ".join(sorted(p.wanted_species)))
    elif p.wanted_body:
        if body & p.wanted_body:
            score += 20
            reasons.append("тип тела: " + ", ".join(sorted(body & p.wanted_body)))
        elif body:
            blockers.append(f"ищут {'/'.join(sorted(p.wanted_body))}, у перса {'/'.join(sorted(body))}")
        else:
            # Нет данных — не повод отказать: пусть она посмотрит сама.
            reasons.append(f"ищут {'/'.join(sorted(p.wanted_body))}, у перса тип тела не указан — проверь")
    elif p.open_call:
        score += 10
        reasons.append("открытый запрос: вид не важен")
    elif p.wanted_other:
        hits = sorted((raw | styles) & p.wanted_other)
        if hits:
            score += 20
            reasons.append("совпало с запросом: " + ", ".join(hits[:3]))
        else:
            reasons.append("в запросе: " + ", ".join(sorted(p.wanted_other)[:4]) + " — у перса таких тегов нет")
    if score and (title or snippet):
        # Уточнения из поста («moth dragons please») — бонус, но не фразы о своих персах и обменах.
        text = f"{title}\n{_OWN_SENTENCE.sub(' ', snippet or '')}".lower()
        extra = sorted(w for w in (raw - same - body) | (styles - {"kawaii"})
                       if len(w) > 2 and re.search(r"(?<![\w-])" + re.escape(w) + r"(?:e?s)?(?![\w-])", text))
        if extra:
            score += min(15, 5 * len(extra))
            reasons.append("совпало: " + ", ".join(extra[:3]))
    price = char_price(ch, p.budget[1] if p.budget else None)
    if p.budget and price:
        if p.budget[1] == price[1]:
            if price[0] <= p.budget[0]:
                score += 10 if score else 0  # в бюджете, но не подходит по запросу — не повод предлагать
                reasons.append(f"в бюджете ({_fmt(price)} ≤ {_fmt(p.budget)})")
            else:
                blockers.append(f"цена перса {_fmt(price)} выше бюджета треда {_fmt(p.budget)}")
        else:
            reasons.append(f"бюджет в другой валюте ({_fmt(p.budget)}), сравнить нельзя")
    if p.pays_only:
        group = PAY_GROUP.get(p.pays_only, p.pays_only)
        if group not in accepts:
            blockers.append(f"платят только {PAY_LABEL.get(p.pays_only, p.pays_only)}")
    elif p.pays and not (p.pays & accepts) and p.pays <= {"art", "trades"}:
        blockers.append("предлагают только " + " и ".join(PAY_LABEL[x] for x in sorted(p.pays)))
    return Fit(score=score, reasons=reasons, blockers=blockers)


# --- Шанс продать ---

def _extra(row) -> dict:
    try:
        raw = row["extra"]
    except (IndexError, KeyError):
        return {}
    try:
        val = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def _get(row, key):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def opportunity_score(row, now: float) -> int:
    """Свежий тред, мало откликов, автор сам здесь — шанс, что её заметят, выше."""
    score = 0
    created = _get(row, "published_ts")
    if created:
        age = now - created
        if age < DAY:
            score += 15
        elif age < 3 * DAY:
            score += 8
    posts = _get(row, "posts")
    if posts is not None:
        if posts <= 3:
            score += 10
        elif posts <= 10:
            score += 5
        elif posts > 40:
            score -= 10
    author = (_get(row, "author") or "").lower()
    last = str(_extra(row).get("last_poster") or "").lower()
    if author and last == author:
        score += 5
    return score


def opportunity_notes(row, now: float) -> list[str]:
    out = []
    created = _get(row, "published_ts")
    if created:
        hours = max(0, (now - created) / 3600)
        out.append(f"создан {hours:.0f} ч назад" if hours < 48 else f"создан {hours / 24:.0f} дн назад")
    posts = _get(row, "posts")
    if posts is not None:
        out.append(f"постов {posts}")
    author = (_get(row, "author") or "").lower()
    if author and str(_extra(row).get("last_poster") or "").lower() == author:
        out.append("последним писал сам автор")
    return out


_KEY_PREFIX = re.compile(r"^\W*(?:lf|looking\s+for|iso|seeking|in\s+search\s+of)\b\W*", re.I)


def thread_key(author: str | None, title: str | None) -> str:
    """Один и тот же запрос одного автора на разных досках (13 и 5866) — один ключ."""
    t = re.sub(r"[\[(][^\])]*[\])]", " ", (title or "").lower())
    t = _KEY_PREFIX.sub("", t)
    t = " ".join(re.sub(r"[^\w\s]", " ", t).split())
    return f"{(author or '').strip().lower()}|{t}"


def _th_id(value: str) -> str:
    v = str(value or "")
    return v if v.startswith("th:") else f"th:{v}"


def replied_threads(conn, my_name: str, since_ts: float) -> set[str]:
    """Треды (id вида th:123), куда она уже отвечала: журнал откликов и «последний пост — её»."""
    out = {_th_id(r[0]) for r in conn.execute(
        "SELECT DISTINCT target FROM sent_log WHERE kind = 'th_reply' AND ok = 1 AND ts >= ?", (since_ts,))}
    name = (my_name or "").strip().lower()
    if name:
        for r in conn.execute("SELECT id, extra FROM items WHERE source = 'toyhouse' AND extra LIKE ?",
                              (f"%{name}%",)):
            if str(_extra(r).get("last_poster") or "").lower() == name:
                out.add(r["id"])
    return out


# --- Подборка для неё ---

REPLIED_WINDOW_DAYS = 30


def suggestions(conn, cfg, days: float = 5, limit: int = 10, show_blocked: bool = False,
                now: float | None = None) -> list[dict]:
    """Живые LF-треды под её персов, лучшие сверху (подходит перс + шанс заметить)."""
    from .th_reply import op_restrictions

    now = now or time.time()
    chars = [c for c in (cfg.posting.characters or []) if isinstance(c, dict)]
    me = (cfg.posting.th_username or "").strip().lower()
    accepts = accepts_of(chars)
    where = "match_kind = 'lf'"
    if show_blocked:
        where = "(match_kind = 'lf' OR (wants_characters = 1 AND match_reason IN ('закрыт', 'её тред')))"
    rows = conn.execute(
        f"SELECT * FROM items WHERE source = 'toyhouse' AND dismissed = 0 AND {where} "
        "AND COALESCE(activity_ts, published_ts, first_seen_ts) >= ? "
        "ORDER BY COALESCE(activity_ts, published_ts, first_seen_ts) DESC",
        (now - days * DAY,)).fetchall()
    replied = replied_threads(conn, me, now - max(days, cfg.posting.th_thread_cooldown_days,
                                                   REPLIED_WINDOW_DAYS) * DAY)
    seen: set[str] = set()
    out = []
    for r in rows:
        key = thread_key(r["author"], r["title"])
        if key in seen:
            continue  # кросс-пост того же автора: оставляем самый свежий
        seen.add(key)
        prof = lf_profile(r["title"], r["snippet"])
        thread_block = []
        if prof.closed:
            thread_block.append("тред закрыт или автор уже нашёл")
        if prof.paused:
            thread_block.append("тред на паузе")
        if me and (r["author"] or "").lower() == me:
            thread_block.append("это её тред")
        if r["id"] in replied:
            thread_block.append("она уже отвечала в этом треде")
        if thread_block and not show_blocked:
            continue
        fits = [(ch, character_fit(prof, ch, accepts, r["title"], r["snippet"])) for ch in chars]
        keep = [(ch, f) for ch, f in fits if show_blocked or (not f.blockers and f.score > 0)]
        if not keep:
            continue
        keep.sort(key=lambda cf: (not cf[1].blockers, cf[1].score), reverse=True)
        best = max((f.score for _, f in keep if not f.blockers), default=0)
        opp = opportunity_score(r, now)
        out.append({"id": r["id"], "title": r["title"], "url": r["url"], "author": r["author"],
                    "fits": keep, "best": best, "opportunity": opp, "rank": best + opp,
                    "thread_blockers": thread_block, "notes": opportunity_notes(r, now),
                    "rules": op_restrictions(r["title"], r["snippet"])})
    out.sort(key=lambda s: (not s["thread_blockers"], s["rank"]), reverse=True)
    return out[:max(1, limit)]


UNTRUSTED_HEADER = "Названия и тексты ниже писали посторонние люди (чужой текст)."
EMPTY = "Подходящих живых LF-тредов под её персов сейчас нет."


def render_suggestions(conn, cfg, days: float = 5, limit: int = 10, show_blocked: bool = False,
                       now: float | None = None) -> str:
    items = suggestions(conn, cfg, days=days, limit=limit, show_blocked=show_blocked, now=now)
    if not items:
        return EMPTY
    out = [UNTRUSTED_HEADER, "Сортировка: насколько подходит перс + шанс, что отклик заметят (свежесть, мало постов)."]
    for s in items:
        lines = [f"[{s['id']}] {s['title']}"]
        if s["notes"]:
            lines.append("  тред: " + ", ".join(s["notes"]))
        if s["thread_blockers"]:
            lines.append("  мешает: " + "; ".join(s["thread_blockers"]))
        for ch, f in s["fits"][:5]:
            name = ch.get("name") or ch.get("url") or "?"
            lines.append(f"  {name} — {f.score}: " + ("; ".join(f.reasons) or "совпадений нет"))
            if f.blockers:
                lines.append("  мешает: " + "; ".join(f.blockers))
        if s["rules"]:
            lines.append("  автор пишет: " + "; ".join(s["rules"]))
        lines.append(f"  {s['url']}")
        out.append("\n".join(lines))
    return "\n\n".join(out)
