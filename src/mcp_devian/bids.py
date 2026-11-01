"""Ставки на аукционах DeviantArt: разбор комментариев под работой.

На DA ставки пишут комментариями («SB», «$25», «+5», «AB»), а автор отвечает
«closed», «sold to @X» и т.п. Разбор консервативный: лучше пропустить сомнительную
ставку (и сказать об этом в notes), чем приписать её. Это ориентир — проверять глазами.
"""

import re
import time
from collections import Counter
from dataclasses import dataclass, field

CURRENCIES = ("usd", "points", "eur", "gbp")
SYMBOL = {"usd": "$", "eur": "€", "gbp": "£"}


@dataclass
class Comment:
    comment_id: str
    parent_id: str | None
    user: str
    posted_ts: float
    text: str
    depth: int = 0


@dataclass
class Bid:
    comment_id: str
    user: str
    amount: float
    currency: str
    ts: float
    kind: str  # sb | bid | raise | ab
    raw: str


@dataclass
class AuctionState:
    bids: list[Bid] = field(default_factory=list)
    top: Bid | None = None
    n_bids: int = 0
    bidders: int = 0
    last_bid_ts: float | None = None
    ab_hit: bool = False
    ended: bool = False
    winner: str | None = None
    currency: str | None = None
    notes: list[str] = field(default_factory=list)


# «1 000 pts» — тысячи через пробел (и неразрывный), только если сразу за числом поинты: в долларах так почти
# не пишут, а «#2 100$» — это лот 2 за $100.
_SPACED = (r"(?<![#\w.,])\d{1,3}(?:[ \u00a0\u202f]\d{3})+"
           r"(?=\s?(?:pts\b|points?\b|:points:|поинт|da\s?points))")
_NUM = r"(\d{1,3}(?:,\d{3})+|" + _SPACED + r"|\d+(?:[.,]\d{1,2})?)\s?(k\b)?"
AMOUNT_PATTERNS = [
    ("usd", re.compile(r"\$\s?" + _NUM, re.I)),
    ("usd", re.compile(_NUM + r"\s?(?:\$|usd\b|dollars?\b|d[oó]lar\w*|бакс\w*|долл\w*)", re.I)),
    ("eur", re.compile(r"€\s?" + _NUM, re.I)),
    ("eur", re.compile(_NUM + r"\s?(?:€|eur\b|euros?\b|евро\b)", re.I)),
    ("gbp", re.compile(r"£\s?" + _NUM, re.I)),
    ("gbp", re.compile(_NUM + r"\s?(?:£|gbp\b)", re.I)),
    ("points", re.compile(_NUM + r"\s?(?:pts\b|points?\b|:points:|поинт\w*|da\s?points)", re.I)),
]

RETRACT = re.compile(r"\b(nvm|never\s?mind|retract\w*|withdraw\w*|cancel\s+(?:my\s+)?bid)\b|"
                     r"отменяю|отмена\s+ставки|забираю\s+ставку|снимаю\s+ставку", re.I)
# @ab_cat — ник, а не автобай: слева не буква/цифра/@, справа не буква/цифра/_.
AB_WORD = re.compile(r"(?<![\w@])(ab|autobuy|auto[\s-]?buy|автобай)(?!\w)", re.I)
SB_WORD = re.compile(r"^\W*(sb|starting\s+bid|стартовая)\W*$", re.I)
RAISE = re.compile(r"^\W*\+\s?(?:\$\s?)?(\d+(?:[.,]\d{1,2})?)\s?(\$|usd|pts|points?|€|£)?\W*$", re.I)
BID_WORD = re.compile(r"\b(bid|bidding|ставлю|ставка|puja|pujo|oferta|oferto|ofrezco)\b\s*:?\s*", re.I)
BARE = re.compile(r"^\W*(\d+(?:[.,]\d{1,2})?)\W*$")
# Вежливые и служебные слова, которые не мешают понять ставку: «SB please!», «I bid 25», «20 pls».
FILLER = re.compile(r"\b(please|pls|plz|pliz|por\s+favor|пожалуйста|плиз|i'?ll|i\s+will|i'?d|i|my|me|"
                    r"take|the|it|bid\s+is|is|go)\b", re.I)
# Рассказы о прошлом и реклама — не ставки, даже если в них есть цена. (TOS/terms сюда не входят:
# многие аукционы просят дописать «I read the TOS» к ставке.)
CHATTY = re.compile(r"\b(paid|last time|commissions?|comms|my prices?|price list|for reference|"
                    r"рисую|заказ\w*|в прошлый раз)\b", re.I)
# «$30 wait no, $45», «sorry I mean $45» — правка суммы в том же комментарии: берём последнюю.
CORRECTION = re.compile(r"\b(wait|actually|i\s+mean|make\s+it|correction|oops|sorry|typo|edit)\b|"
                        r"исправ\w*|то\s+есть|точнее|ой", re.I)
SHORT = 60  # длиннее — считаем болтовнёй, если нет явного слова «bid»/«ставка»

# Сигналы автора: закрыл, продал, победитель, автобай взят.
CLOSE = re.compile(r"\b(closed|sold(?:\s+out)?|ended|finished|закрыт\w*|продан\w*|окончен\w*|завершен\w*)\b|"
                   r"\b(?:auction|аукцион)\s+(?:is\s+)?over\b", re.I)
NOT_A_CLOSE = re.compile(r"\bclosed\s+(?:species|trades?|group|to\s+offers)\b", re.I)
OWNER_AB = re.compile(r"\b(ab|autobuy|auto[\s-]?buy|автобай)\b.{0,20}\b(taken|reached|claimed|hit|взят\w*|выкуплен\w*)\b"
                      r"|\b(taken|reached|claimed)\b.{0,10}\b(ab|autobuy)\b", re.I)
NEG_BEFORE = re.compile(r"\b(not|never|no\s+longer|isn'?t|aren'?t|wasn'?t|won'?t|don'?t)\b|n't\b|\bне\b|\bещё\b|\bеще\b|"
                        r"\bпока\b", re.I)
NEG_AFTER = re.compile(r"^\W*(yet|still|ещё|еще)\b", re.I)
# Закрытие в будущем («Will be closed tomorrow», «закроется через сутки») — аукцион ещё идёт.
FUTURE_CLOSE = re.compile(r"\b(?:will|shall|going\s+to)\s+(?:be\s+)?(?:closed|ended|finished|sold|over)\b|"
                          r"\b(?:closed|ended|finished|sold|closes|ends)\s+(?:in\s+\d|at\s+\d|on\s+\w+day|until|tomorrow|"
                          r"tonight|after|\d+\s*(?:h|hours?|d|days?)\b)|"
                          r"закро\w*ся|законч\w*ся|заверш\w*ся|будет\s+(?:закрыт|продан|окончен)", re.I)
WINNER = [
    re.compile(r"\b(?:sold|goes|went)\s+to\s+(@?[\w.-]{2,})", re.I),
    re.compile(r"\bпродан\w*\s+(@[\w.-]{2,})", re.I),
    re.compile(r"\b(?:winner|победител\w*)\s*(?:is|:|-|—)?\s*(@?[\w.-]{2,})", re.I),
    re.compile(r"\bcongrat\w*\s*(?:to\s+)?(@[\w.-]{2,})", re.I),
    re.compile(r"(@[\w.-]{2,})\s+(?:won|wins|победил\w*)\b", re.I),
    re.compile(r"\bпоздравля\w*\s*(@?[\w.-]{2,})", re.I),
]
# «Currently the winner», «winner gets announced», «congrats on the first bid» — ещё не итог.
HEDGE = re.compile(r"\b(currently|so\s+far|for\s+now|right\s+now|first\s+bid|gets|will|announc\w*|tba|tbd|soon|"
                   r"пока|сейчас|будет|объявл\w*)\b", re.I)
NOT_NAMES = {"you", "the", "this", "everyone", "all", "auction", "thanks", "thank", "bidding", "bidders", "всех",
             "for", "tba", "tbd", "highest", "gets", "will", "yet", "is", "to", "me", "a", "an", "someone",
             "announced", "soon", "the_highest", "bidder", "winner", "who", "you're", "you’re"}


def _num(raw: str, k: str | None) -> float:
    raw = re.sub(r"[ \u00a0\u202f]", "", raw)
    raw = raw.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw) else raw.replace(",", ".")
    value = float(raw)
    return value * 1000 if k else value


MENTION = re.compile(r"@[\w.-]+")


def find_amounts(text: str) -> list[tuple[int, float, str]]:
    """Все суммы с явной валютой: (позиция, сумма, валюта), без пересекающихся совпадений.
    Упоминания (@kitty123) не считаются: цифры в нике — не ставка."""
    text = MENTION.sub(lambda m: " " * len(m.group()), text)
    spans: list[tuple[int, int, float, str]] = []
    for currency, pattern in AMOUNT_PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < e and s < m.end() for s, e, _, _ in spans):
                continue  # «$20» и «20$» — одна и та же сумма
            spans.append((m.start(), m.end(), _num(m.group(1), m.group(2)), currency))
    return [(s, v, c) for s, _, v, c in sorted(spans)]


def find_amount(text: str) -> tuple[float, str] | None:
    """Первая сумма с явной валютой в тексте."""
    found = find_amounts(text)
    return (found[0][1], found[0][2]) if found else None


def _negated(text: str, start: int, end: int) -> bool:
    clause = re.split(r"[.!?;]", text[:start])[-1]
    before = " ".join(clause.split()[-3:])
    after = re.split(r"[.!?;,]", text[end:])[0]
    return bool(NEG_BEFORE.search(before) or NEG_BEFORE.search(text[start:end]) or NEG_AFTER.search(after))


def _owner_winner(text: str, commenters: dict[str, str]) -> str | None:
    """Победитель из слов автора: только явный @ник, ник участника или «winner is X»."""
    for i, pattern in enumerate(WINNER):
        m = pattern.search(text)
        if not m:
            continue
        # Оговорки смотрим только в той же фразе: «Sold to @bob! Will send files» — это итог.
        clause_start = max(text.rfind(ch, 0, m.start()) for ch in ".!?;") + 1
        clause_end = min([i_ for i_ in (text.find(ch, m.end()) for ch in ".!?;") if i_ != -1] or [len(text)])
        if HEDGE.search(text[clause_start:clause_end]):
            continue
        raw = m.group(1).rstrip(".-")
        name = raw.lstrip("@")
        if name.lower() in NOT_NAMES or len(name) < 2:
            continue
        if name.lower() in commenters:
            return commenters[name.lower()]
        if raw.startswith("@") or i in (2, 5):  # «winner is X» / «поздравляю X» — явная форма
            return name
    return None


def _owner_closes(text: str) -> bool:
    if FUTURE_CLOSE.search(text):
        return False
    addressed = text.lstrip().startswith("@")
    for m in CLOSE.finditer(text):
        if NOT_A_CLOSE.match(text, m.start()) or _negated(text, m.start(), m.end()):
            continue
        # Ответ кому-то («@bob ...») или длинный комментарий закрывают, только если закрытие — в начале.
        head = len(text[:m.start()].split()) <= 2
        if head and not addressed:
            return True
        if not addressed and len(text) <= 40:
            return True
    return False


@dataclass
class _Owner:
    ended: bool = False
    ab: bool = False
    winner: str | None = None


def _owner_signal(owner: _Owner, c: Comment, commenters: dict[str, str]) -> None:
    text = " ".join(c.text.split())
    m = OWNER_AB.search(text)
    if m and not _negated(text, m.start(), m.end()):
        owner.ab = owner.ended = True
    winner = _owner_winner(text, commenters)
    if winner:
        owner.winner = winner
        owner.ended = True
    elif _owner_closes(text):
        owner.ended = True


# «I read the TOS», «TOS read», «terms ok» — многие аукционы просят дописать это к ставке.
TOS_NOTE = re.compile(r"\b(?:i\s+)?(?:have\s+)?(?:read|accept\w*|agree\w*)\s+(?:to\s+)?(?:the\s+|your\s+)?(?:tos|terms)\b|"
                      r"\b(?:tos|terms)\s*(?:read|accepted|agreed|ok|okay)?\b", re.I)


def _strip_fillers(text: str) -> str:
    return " ".join(FILLER.sub(" ", TOS_NOTE.sub(" ", text)).split())


AMOUNT_TOKEN = re.compile(r"[$€£]?\s?\d[\d.,]*\s?(?:k\b)?\s?(?:\$|usd\b|pts\b|points?\b|€|£|dollars?\b)?", re.I)


def _is_ab_claim(text: str) -> bool:
    """«AB!», «AB please», «I'll AB», «AB $80» — заявка на автобай. «Wish I could AB» — нет."""
    rest = AB_WORD.sub(" ", MENTION.sub(" ", text))
    rest = AMOUNT_TOKEN.sub(" ", rest)
    return not re.search(r"[^\W\d_]", _strip_fillers(rest))


def _starts_like_bid(text: str) -> bool:
    lead = re.sub(r"^[^\w$€£+]+", "", MENTION.sub(" ", text)).lstrip()
    if not lead:
        return False
    if RAISE.match(lead) or re.match(r"(sb|ab|autobuy|автобай)\b", lead, re.I):
        return True
    amounts = find_amounts(lead)
    return bool(amounts) and amounts[0][0] == 0


def _pick_amount(text: str, top: Bid | None) -> tuple[float, str] | str | None:
    """Одна сумма → она. Несколько → цитата текущей ставки или явная правка, иначе «проверь глазами»."""
    found = find_amounts(text)
    values = {(v, c) for _, v, c in found}
    if not found:
        return None
    if len(values) == 1:
        return found[0][1], found[0][2]
    if top is not None:
        rest = {(v, c) for v, c in values if v != top.amount}
        if len(rest) == 1:
            return next(iter(rest))  # «@anna bid $20 so I bid $25»
    if CORRECTION.search(text):
        return found[-1][1], found[-1][2]  # «$30 wait no, $45»
    listed = ", ".join(money(v, c) for _, v, c in found)
    return f"несколько сумм в одном комментарии ({listed}): «{text[:60]}» — проверь глазами"


def _interpret(c: Comment, top: Bid | None, sb: float | None, ab: float | None,
               currency: str | None, text: str | None = None) -> tuple[float, str | None, str] | str:
    """(сумма, валюта или None — «как у лота», вид) или строка-причина, почему это не ставка."""
    text = " ".join((c.text if text is None else text).split())
    if not text:
        return "пустой комментарий"
    short = len(text) <= SHORT
    explicit = bool(BID_WORD.search(text))
    if "?" in text and not explicit:
        return "вопрос, не ставка"
    if CHATTY.search(text) and not explicit and not _starts_like_bid(text):
        return "разговор о ценах, не ставка"
    if not short and not explicit:
        return "длинный комментарий без слова «bid»"

    if AB_WORD.search(text):
        if _is_ab_claim(text):
            picked = _pick_amount(text, top)
            if isinstance(picked, str):
                return picked
            if picked:
                return picked[0], picked[1], "ab"
            if ab is not None:
                return ab, None, "ab"
            return "«AB» без известной суммы автобая"
        # Упомянули AB, но это не заявка («wish I could AB») — дальше разбираем как обычный комментарий.
        reason_ab = "упоминает AB — проверь глазами"
    else:
        reason_ab = ""

    cleaned = _strip_fillers(BID_WORD.sub(" ", text))
    if SB_WORD.match(cleaned):
        if sb is None:
            return "«SB» без известной стартовой цены"
        return sb, None, "sb"

    m = RAISE.match(text)
    if m:
        base = top.amount if top else sb
        if base is None:
            return "«+N» без текущей ставки"
        unit = {"$": "usd", "usd": "usd", "pts": "points", "point": "points", "points": "points",
                "€": "eur", "£": "gbp"}.get((m.group(2) or "").lower())
        return base + _num(m.group(1), None), unit, "raise"

    picked = _pick_amount(text, top)
    if isinstance(picked, str):
        return picked
    if picked:
        return picked[0], picked[1], "bid"

    m = BARE.match(cleaned)
    if m and currency:
        return _num(m.group(1), None), None, "bid"
    return reason_ab or "не похоже на ставку"


def _looks_like_attempt(text: str) -> bool:
    text = " ".join(text.split())
    return len(text) <= 40 and bool(re.search(r"\d|\b(sb|ab|bid|ставк\w*|puja)\b", text, re.I))


def parse_bids(comments: list[Comment], owner: str, sb: float | None = None, ab: float | None = None,
               currency: str | None = None, min_inc: float | None = None) -> AuctionState:
    state = AuctionState(currency=currency)
    owner_l = (owner or "").lower()
    ordered = sorted(comments, key=lambda c: (c.posted_ts, int(c.comment_id) if c.comment_id.isdigit() else 0))
    commenters = {c.user.lower(): c.user for c in ordered if c.user and c.user.lower() != owner_l}
    sig = _Owner()
    active: list[Bid] = []
    ab_bid: Bid | None = None  # ставка, которой взяли автобай (её могут отозвать)

    def ended() -> bool:
        return sig.ended or ab_bid is not None

    def accept(c: Comment, res: tuple[float, str | None, str]) -> None:
        nonlocal ab_bid
        amount, unit, kind = res
        unit = unit or state.currency
        top = active[-1] if active else None
        if unit is None:
            state.notes.append(f"{c.user}: сумма без валюты, а валюта лота неизвестна")
            return
        if state.currency is None:
            state.currency = unit
        elif unit != state.currency:
            # Другую валюту не смешиваем: пусть решает она.
            state.notes.append(f"{c.user}: ставка в другой валюте ({money(amount, unit)}), не учитываю")
            return
        if top and amount <= top.amount:
            state.notes.append(f"{c.user}: {money(amount, unit)} не выше текущей {money(top.amount, unit)}, не учитываю")
            return
        if not top and sb is not None and amount < sb:
            state.notes.append(f"{c.user}: {money(amount, unit)} ниже SB {money(sb, unit)}, не учитываю")
            return
        if top and min_inc and amount - top.amount < min_inc:
            # Шаг меньше MI: ставку оставляем (авторы часто принимают), но отмечаем.
            state.notes.append(f"{c.user}: шаг {money(amount - top.amount, unit)} меньше MI {money(min_inc, unit)}")
        if kind == "ab" and ab is not None and amount < ab:
            state.notes.append(f"{c.user}: просит AB, но {money(amount, unit)} ниже автобая {money(ab, unit)} — "
                               "считаю обычной ставкой")
            kind = "bid"
        bid = Bid(c.comment_id, c.user, amount, unit, c.posted_ts, kind, " ".join(c.text.split())[:80])
        active.append(bid)
        if kind == "ab" or (ab is not None and amount >= ab):
            ab_bid = bid

    for c in ordered:
        if c.user.lower() == owner_l:
            _owner_signal(sig, c, commenters)
            continue
        if RETRACT.search(c.text):
            # Отзыв того самого автобая — единственное, что «открывает» аукцион обратно.
            own_ab = ab_bid is not None and not sig.ended and c.user.lower() == ab_bid.user.lower()
            if ended() and not own_ab:
                state.notes.append(f"{c.user}: отзыв ставки после закрытия, не учитываю")
                continue
            mine = [b for b in active if b.user.lower() == c.user.lower()]
            if mine:
                active.remove(mine[-1])
                if mine[-1] is ab_bid:
                    ab_bid = None
                    state.notes.append(f"{c.user} отозвал(а) автобай — аукцион снова открыт, проверь")
                else:
                    state.notes.append(f"{c.user} отозвал(а) ставку {money(mine[-1].amount, mine[-1].currency)}")
            # «nvm make it $30» — отзыв и новая ставка в одном комментарии.
            rest = RETRACT.sub(" ", c.text)
            res = _interpret(c, active[-1] if active else None, sb, ab, state.currency, text=rest)
            if not isinstance(res, str):
                accept(c, res)
            continue
        if ended():
            state.notes.append(f"{c.user}: комментарий после закрытия, не считаю")
            continue
        res = _interpret(c, active[-1] if active else None, sb, ab, state.currency)
        if isinstance(res, str):
            if res == "не похоже на ставку":
                if _looks_like_attempt(c.text):
                    state.notes.append(f"{c.user}: не поняла «{' '.join(c.text.split())[:40]}»")
            elif res != "пустой комментарий":
                state.notes.append(f"{c.user}: {res}")
            continue
        accept(c, res)

    state.ab_hit = sig.ab or ab_bid is not None
    state.ended = sig.ended or ab_bid is not None
    state.winner = sig.winner or (ab_bid.user if ab_bid is not None else None)
    state.bids = active
    state.top = max(active, key=lambda b: b.amount) if active else None
    state.n_bids = len(active)
    state.bidders = len({b.user.lower() for b in active})
    state.last_bid_ts = max((b.ts for b in active), default=None)
    return state


MAX_BIDDER_RECORDS = 50


def bidder_records(state: AuctionState, limit: int = MAX_BIDDER_RECORDS) -> list[dict]:
    """Кто ставил: [{user, amount, currency, ts, n}] — по одной записи на человека (его максимальная ставка,
    n — сколько его ставок на лоте), сначала крупные. Только для аналитики рынка, никому не пишем."""
    best: dict[str, Bid] = {}
    count: Counter = Counter()
    for b in state.bids:
        if not b.user:
            continue
        key = b.user.lower()
        count[key] += 1
        if key not in best or b.amount > best[key].amount:
            best[key] = b
    rows = sorted(best.values(), key=lambda b: (-b.amount, b.ts))
    return [{"user": b.user, "amount": b.amount, "currency": b.currency, "ts": b.ts, "n": count[b.user.lower()]}
            for b in rows[:limit]]


def money(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "?"
    if currency == "points":
        return f"{amount:g} pts"
    return f"{SYMBOL.get(currency or 'usd', '$')}{amount:g}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _ago(ts: float, now: float) -> str:
    s = max(0, int(now - ts))
    if s < 3600:
        return f"{s // 60} мин назад"
    if s < 86400:
        return f"{s // 3600} ч назад"
    return f"{s // 86400} дн назад"


def auction_summary(state: AuctionState, sb: float | None = None, ab: float | None = None,
                    currency: str | None = None, now: float | None = None) -> str:
    now = now or time.time()
    cur = state.currency or currency
    parts = []
    if state.top:
        n, b = state.n_bids, state.bidders
        parts.append(f"Ставок: {n} от {b} {_plural(b, 'человека', 'человек', 'человек')}, "
                     f"сейчас {money(state.top.amount, cur)} у {state.top.user}"
                     + (f" (SB {money(sb, cur)})" if sb is not None else "")
                     + f", последняя {_ago(state.last_bid_ts or now, now)}")
    else:
        parts.append("Ставок пока нет" + (f" (SB {money(sb, cur)})" if sb is not None else ""))
    if ab is not None:
        parts.append(f"автобай {money(ab, cur)} {'взят' if state.ab_hit else 'не взят'}")
    elif state.ab_hit:
        parts.append("автобай взят")
    text = "; ".join(parts) + "."
    if state.ended:
        text += " Автор закрыл аукцион" + (f", победитель — {state.winner}." if state.winner else ".")
    if state.notes:
        n = len(state.notes)
        text += f" Не посчитала как ставки: {n} {_plural(n, 'комментарий', 'комментария', 'комментариев')}."
    return text + " Это разбор комментариев — проверь глазами."
