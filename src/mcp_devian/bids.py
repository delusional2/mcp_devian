"""Ставки на аукционах DeviantArt: разбор комментариев под работой.

На DA ставки пишут комментариями («SB», «$25», «+5», «AB»), а автор отвечает
«closed», «sold to @X» и т.п. Разбор консервативный: лучше пропустить сомнительную
ставку (и сказать об этом в notes), чем приписать её. Это ориентир — проверять глазами.
"""

import re
import time
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


_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:[.,]\d{1,2})?)\s?(k\b)?"
AMOUNT_PATTERNS = [
    ("usd", re.compile(r"\$\s?" + _NUM, re.I)),
    ("usd", re.compile(_NUM + r"\s?(?:\$|usd\b|dollars?\b|бакс\w*|долл\w*)", re.I)),
    ("eur", re.compile(r"€\s?" + _NUM, re.I)),
    ("eur", re.compile(_NUM + r"\s?(?:€|eur\b|euros?\b|евро\b)", re.I)),
    ("gbp", re.compile(r"£\s?" + _NUM, re.I)),
    ("gbp", re.compile(_NUM + r"\s?(?:£|gbp\b)", re.I)),
    ("points", re.compile(_NUM + r"\s?(?:pts\b|points?\b|:points:|поинт\w*|da\s?points)", re.I)),
]

RETRACT = re.compile(r"\b(nvm|never\s?mind|retract\w*|withdraw\w*|cancel\s+(?:my\s+)?bid)\b|"
                     r"отменяю|отмена\s+ставки|забираю\s+ставку|снимаю\s+ставку", re.I)
AB_WORD = re.compile(r"(?<![a-z])(ab|autobuy|auto[\s-]?buy|автобай)(?![a-z])", re.I)
SB_WORD = re.compile(r"^\W*(sb|starting\s+bid|стартовая)\W*$", re.I)
RAISE = re.compile(r"^\W*\+\s?(?:\$\s?)?(\d+(?:[.,]\d{1,2})?)\s?(\$|usd|pts|points?|€|£)?\W*$", re.I)
BID_WORD = re.compile(r"\b(bid|bidding|ставлю|ставка)\b\s*:?\s*", re.I)
BARE = re.compile(r"^\W*(\d+(?:[.,]\d{1,2})?)\W*$")
# Рассказы о прошлом и реклама — не ставки, даже если в них есть цена.
CHATTY = re.compile(r"\b(paid|last time|commissions?|comms|my prices?|price list|for reference|"
                    r"tos|terms|рисую|заказ\w*|в прошлый раз)\b", re.I)
SHORT = 60  # длиннее — считаем болтовнёй, если нет явного слова «bid»/«ставка»

# Сигналы автора.
OWNER_ENDED = re.compile(r"\b(closed|sold|ended|finished|winner|won|congrat\w*|"
                         r"закрыт\w*|продан\w*|окончен\w*|завершен\w*|победител\w*)\b", re.I)
OWNER_AB = re.compile(r"\b(ab|autobuy|auto[\s-]?buy|автобай)\b.{0,20}\b(taken|reached|claimed|hit|взят\w*|выкуплен\w*)\b"
                      r"|\b(taken|reached|claimed)\b.{0,10}\b(ab|autobuy)\b", re.I)
WINNER = [
    re.compile(r"(?:sold|продан\w*|goes)\s+(?:to\s+)?@?([\w-]{3,})", re.I),
    re.compile(r"(?:winner|победител\w*)\s*(?:is|:|-)?\s*@?([\w-]{3,})", re.I),
    re.compile(r"congrat\w*\s*(?:to\s+)?@([\w-]{3,})", re.I),
    re.compile(r"@([\w-]{3,})\s+(?:won|wins|победил\w*)", re.I),
    re.compile(r"поздравля\w*\s*@?([\w-]{3,})", re.I),
]
# «Will be closed tomorrow», «закроется через 24ч» — это про будущее, аукцион ещё идёт.
FUTURE = re.compile(r"\b(will|until|tomorrow|in\s+\d+\s*(?:h|hours?|d|days?)|after\s+the\s+last|завтра|через|до\s+\d)", re.I)
NOT_NAMES = {"you", "the", "this", "everyone", "all", "auction", "thanks", "thank", "bidding", "bidders", "всех"}


def _num(raw: str, k: str | None) -> float:
    raw = raw.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw) else raw.replace(",", ".")
    value = float(raw)
    return value * 1000 if k else value


MENTION = re.compile(r"@[\w.-]+")


def find_amount(text: str) -> tuple[float, str] | None:
    """Первая сумма с явной валютой в тексте. Упоминания (@kitty123) не считаются: цифры в нике — не ставка."""
    text = MENTION.sub(" ", text)
    found = []
    for currency, pattern in AMOUNT_PATTERNS:
        m = pattern.search(text)
        if m:
            found.append((m.start(), _num(m.group(1), m.group(2)), currency))
    if not found:
        return None
    _, value, currency = min(found)
    return value, currency


def _owner_signal(state: AuctionState, c: Comment) -> None:
    text = c.text
    if OWNER_AB.search(text):
        state.ab_hit = True
        state.ended = True
    if OWNER_ENDED.search(text) and not FUTURE.search(text):
        state.ended = True
        for pattern in WINNER:
            m = pattern.search(text)
            if m and m.group(1).lower() not in NOT_NAMES:
                state.winner = m.group(1)
                break


def _interpret(c: Comment, top: Bid | None, sb: float | None, ab: float | None,
               currency: str | None) -> tuple[float, str | None, str] | str:
    """(сумма, валюта или None — «как у лота», вид) или строка-причина, почему это не ставка."""
    text = " ".join(c.text.split())
    if not text:
        return "пустой комментарий"
    short = len(text) <= SHORT
    explicit = bool(BID_WORD.search(text))
    if "?" in text and not explicit:
        return "вопрос, не ставка"
    if CHATTY.search(text) and not explicit:
        return "разговор о ценах, не ставка"
    if not short and not explicit:
        return "длинный комментарий без слова «bid»"

    if AB_WORD.search(text) and (short or explicit):
        amount = find_amount(text)
        if amount:
            return amount[0], amount[1], "ab"
        if ab is not None:
            return ab, None, "ab"
        return "«AB» без известной суммы автобая"

    if SB_WORD.match(text):
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

    amount = find_amount(text)
    if amount:
        return amount[0], amount[1], "bid"

    rest = BID_WORD.sub("", text) if explicit else text
    m = BARE.match(rest)
    if m and currency:
        return _num(m.group(1), None), None, "bid"
    return "не похоже на ставку"


def parse_bids(comments: list[Comment], owner: str, sb: float | None = None, ab: float | None = None,
               currency: str | None = None, min_inc: float | None = None) -> AuctionState:
    state = AuctionState(currency=currency)
    owner_l = (owner or "").lower()
    ordered = sorted(comments, key=lambda c: (c.posted_ts, int(c.comment_id) if c.comment_id.isdigit() else 0))
    active: list[Bid] = []
    for c in ordered:
        if c.user.lower() == owner_l:
            _owner_signal(state, c)
            continue
        if RETRACT.search(c.text):
            mine = [b for b in active if b.user.lower() == c.user.lower()]
            if mine:
                active.remove(mine[-1])
                state.notes.append(f"{c.user} отозвал(а) ставку {money(mine[-1].amount, mine[-1].currency)}")
            continue
        if state.ended:
            state.notes.append(f"{c.user}: комментарий после закрытия, не считаю")
            continue
        top = active[-1] if active else None
        res = _interpret(c, top, sb, ab, state.currency)
        if isinstance(res, str):
            if res != "не похоже на ставку" and res != "пустой комментарий":
                state.notes.append(f"{c.user}: {res}")
            continue
        amount, unit, kind = res
        unit = unit or state.currency
        if unit is None:
            state.notes.append(f"{c.user}: сумма без валюты, а валюта лота неизвестна")
            continue
        if state.currency is None:
            state.currency = unit
        elif unit != state.currency:
            # Другую валюту не смешиваем: пусть решает она.
            state.notes.append(f"{c.user}: ставка в другой валюте ({money(amount, unit)}), не учитываю")
            continue
        if top and amount <= top.amount:
            state.notes.append(f"{c.user}: {money(amount, unit)} не выше текущей {money(top.amount, unit)}, не учитываю")
            continue
        if not top and sb is not None and amount < sb:
            state.notes.append(f"{c.user}: {money(amount, unit)} ниже SB {money(sb, unit)}, не учитываю")
            continue
        if top and min_inc and amount - top.amount < min_inc:
            # Шаг меньше MI: ставку оставляем (авторы часто принимают), но отмечаем.
            state.notes.append(f"{c.user}: шаг {money(amount - top.amount, unit)} меньше MI {money(min_inc, unit)}")
        bid = Bid(c.comment_id, c.user, amount, unit, c.posted_ts, kind, " ".join(c.text.split())[:80])
        active.append(bid)
        if kind == "ab" or (ab is not None and amount >= ab):
            state.ab_hit = True
            state.ended = True
    state.bids = active
    state.top = max(active, key=lambda b: b.amount) if active else None
    state.n_bids = len(active)
    state.bidders = len({b.user.lower() for b in active})
    state.last_bid_ts = max((b.ts for b in active), default=None)
    if state.ended and not state.winner and state.ab_hit and state.top:
        state.winner = state.top.user
    return state


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
