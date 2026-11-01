"""Рынок Toyhouse по тому, что уже накопил мониторинг: кто что ищет и сколько готов платить.

Источник — строки items с source='toyhouse': название треда и (для LF-тредов) первый пост.
Что из этого можно честно вытащить:
- LF-треды (ищут персов): заявленные бюджеты — «budget $30», «up to 50 USD», «can pay 2k pts»,
  «$20–40», а ещё «trades only», «art only», «no budget». Это намерения покупателей, не сделки;
- треды продаж: запрошенные цены (обычно только из названия: первый пост у них мы не читаем);
- реальные продажи на Toyhouse мы почти не видим: чужие сделки нигде не публикуются. Есть только
  её собственные продажи из таблицы sales (platform='toyhouse').

Валюты не смешиваем: usd, eur, gbp, points (DA points), cad/aud/nzd/rub — каждая отдельно.
Бюджеты «за набор» (for the set) в статистику «за перса» не идут. Будущее не подсматриваем:
берём только треды, которые были видны к моменту now.
"""

import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .taxonomy import tag_text

DAY = 86400
SOURCE_LABEL = "треды Toyhouse, что видел мониторинг"

# ---------- суммы ----------

_PRE = r"(?P<pre{i}>us\$|ca?\$|au?\$|nz\$|\$|€|£|₽)"
# «1 500 pts» (тысячи через пробел) — только если сразу дальше явная валюта, иначе «2 300» — два числа.
_NUM = (r"(?P<num{i}>\d{{1,3}}(?:[ \u00a0\u202f]\d{{3}})+(?=\s?(?:pts|points?|usd|eur|gbp|cad|aud|nzd|rub|руб|поинт|"
        r"\$|€|£|₽))|\d{{1,3}}(?:,\d{{3}})+(?!\d)|\d+(?:[.,]\d{{1,2}})?)")
_K = r"(?:\s?(?P<k{i}>k|к)(?![a-zа-яё]))?"
_POST = (r"(?:\s?(?P<post{i}>usd|us\s?dollars?|dollars?|bucks|eur|euros?|gbp|pounds?|pts|points?|da\s?points|:points:|"
         r"cad|aud|nzd|rub|руб\w*|₽|\$|€|£|долл\w*|бакс\w*|евро|поинт\w*|пойнт\w*)(?![a-zа-яё]))?")


def _one(i: int) -> str:
    return r"(?<![\w.,#/@$€£])" + (_PRE + r"?\s?" + _NUM + _K + _POST).format(i=i)


TOKEN = re.compile(_one(1), re.I)
RANGE = re.compile(_one(1) + r"\s*(?:-|–|—|~|to|до|or\s+so\s+to)\s*" + (r"(?:" + _PRE + r"\s?)?" + _NUM + _K + _POST).format(i=2),
                   re.I)


def _currency(sym: str | None) -> str | None:
    if not sym:
        return None
    s = sym.lower().replace(" ", "")
    if s in ("c$", "ca$", "cad"):
        return "cad"
    if s in ("a$", "au$", "aud"):
        return "aud"
    if s in ("nz$", "nzd"):
        return "nzd"
    if s in ("€", "eur", "euro", "euros", "евро"):
        return "eur"
    if s in ("£", "gbp", "pound", "pounds"):
        return "gbp"
    if s in ("₽", "rub") or s.startswith("руб"):
        return "rub"
    if s in ("pts", "point", "points", "dapoints", ":points:") or s.startswith(("поинт", "пойнт")):
        return "points"
    return "usd"  # $, us$, usd, dollar(s), bucks, долл…, бакс…


OTHER_DOLLARS = ("cad", "aud", "nzd")


def _cur(pre: str | None, post: str | None) -> str | None:
    """«$70 AUD» — австралийские: голый «$» спереди уступает уточнению после числа."""
    cp, cq = _currency(pre), _currency(post)
    if cp == "usd" and (pre or "").strip() == "$" and cq in OTHER_DOLLARS:
        return cq
    return cp or cq


def _value(num: str, k: str | None) -> float:
    num = re.sub(r"[ \u00a0\u202f]", "", num)
    thousands = re.fullmatch(r"\d{1,3}(?:,\d{3})+", num)
    v = float(num.replace(",", "") if thousands else num.replace(",", "."))
    return v * 1000 if k else v


@dataclass
class Money:
    low: float | None
    high: float | None
    currency: str | None      # None — число без валюты
    per: str = ""             # each | set | ""
    approx: bool = False
    start: int = 0
    end: int = 0
    raw: str = ""

    @property
    def cap(self) -> float | None:
        """Сколько максимум: верх диапазона или единственное число."""
        return self.high if self.high is not None else self.low


def find_money(text: str) -> list[Money]:
    """Все суммы и диапазоны: «$20-40», «20–40 USD», «$1.5k», «2k pts», «€15». Упоминания и ссылки не считаются."""
    text = re.sub(r"@[\w.-]+|https?://\S+", lambda m: " " * len(m.group()), text or "")
    out: list[Money] = []
    taken: list[tuple[int, int]] = []
    for m in RANGE.finditer(text):
        c1 = _cur(m.group("pre1"), m.group("post1"))
        c2 = _cur(m.group("pre2"), m.group("post2"))
        if c1 == "usd" and (m.group("pre1") or "").strip() == "$" and not m.group("post1") and c2 in OTHER_DOLLARS:
            c1 = c2  # «$20 - 40 CAD»
        if not (c1 or c2) or (c1 and c2 and c1 != c2):
            continue
        a = _value(m.group("num1"), m.group("k1") or m.group("k2"))
        b = _value(m.group("num2"), m.group("k2"))
        if b <= a:
            continue
        out.append(Money(a, b, c1 or c2, start=m.start(), end=m.end(), raw=m.group().strip()))
        taken.append((m.start(), m.end()))
    for m in TOKEN.finditer(text):
        if any(s <= m.start() < e for s, e in taken) or not m.group("num1"):
            continue
        cur = _cur(m.group("pre1"), m.group("post1"))
        start = m.start()
        other = OTHER_BEFORE.search(text[max(0, m.start() - 5):m.start()])
        if other and cur == "usd":
            cur = other.group(1).lower()  # «CAD $20» — не доллары США
            start = max(0, m.start() - 5) + other.start()  # «can pay CAD $40»: подсказка «can pay» — перед «CAD»
        v = _value(m.group("num1"), m.group("k1"))
        out.append(Money(v, None, cur, start=start, end=m.end(), raw=m.group().strip()))
    return sorted(out, key=lambda x: x.start)


# ---------- контекст суммы ----------

CUE_MAX = re.compile(r"(?:up\s*to|upto|under|below|max(?:imum)?|budget(?:\s+is)?|no\s+more\s+than|at\s+most|cap|"
                     r"бюджет\w*|не\s+больше|не\s+дороже|максимум|макс)\W{0,4}$|(?<![\w])до\s*$", re.I)
CUE_PAY = re.compile(r"(?:can|could|will|would|willing\s+to|able\s+to)\s+(?:pay|spend|do|offer|give)"
                     r"(?:\s+(?:around|about|up\s*to))?\W{0,4}$|(?:paying|pay|spend|spending|offering|offer|i\s+have|have|got)\W{0,4}$|"
                     r"(?:могу|готов\w*)\s+(?:заплатить|дать|потратить|отдать|предложить)(?:\s+до)?\W{0,4}$|"
                     r"(?:плачу|предлагаю|есть)\W{0,4}$", re.I)
# «$20 max», «$30 tops», «$30 or less» — граница после суммы.
MAX_AFTER = re.compile(r"^\s*(?:max(?:imum)?|tops|or\s+(?:less|under|below|lower)|at\s+most|и\s+меньше|максимум|макс)\b", re.I)
OTHER_BEFORE = re.compile(r"\b(cad|aud|nzd)\s*$", re.I)
BARE_CUE = re.compile(r"(?:up\s*to|upto|max(?:imum)?|budget(?:\s+is)?|no\s+more\s+than|at\s+most|бюджет\w*|не\s+больше|"
                      r"не\s+дороже|максимум|макс)\W{0,4}$|(?<![\w])до\s*$|(?:^|[\s|/(\[,])o\s*:\s*$", re.I)
NOT_MONEY_AFTER = re.compile(
    r"^\s*(?:\+\s*)?(?:-\s*\d+\s+)?(?:ocs?\b|characters?|chars?\b|designs?|adopts?|kids?|pets?|slots?|pieces?|pics?|"
    r"images?|refs?\b|weeks?|days?|hours?|hrs?\b|h\b|d\b|months?|mo\b|years?|yrs?\b|y/?o\b|per\b|people|persons?|"
    r"times?|x\b|%|th\b|st\b|nd\b|rd\b|of\b|more\b|others?\b|other\b|"
    r"перс\w*|дн\w*|недел\w*|час\w*|лет\b|год\w*|штук\w*|месяц\w*|раз\w*)", re.I)
AGE_BEFORE = re.compile(r"(?:under|over|above|below|at\s+least|aged?|ages|младше|старше|от)\s*$", re.I)
CUE_MIN = re.compile(r"(?:at\s+least|min(?:imum)?|from|starting(?:\s+at)?|over|above|от|минимум|не\s+меньше)\W{0,4}$", re.I)
CUE_APPROX = re.compile(r"(?:around|about|approx\w*|roughly|~|около|примерно|где-то)\W{0,4}$", re.I)
PAST = re.compile(r"\b(?:paid|bought|was|were|worth|valued?|value|retail|originally|og|resale\s+of|got\s+(?:her|him|them)\s+for|"
                  r"купил\w*|стоил\w*|ценность|оценен\w*)\b", re.I)
# Метка «O:», «offering», «paying» в названии: всё после неё — то, чем платят.
OFFER_MARK = re.compile(r"(?:^|[\s|/\-(\[,])(?:o|of|offering|offers?|paying|pay)\s*[:\-]", re.I)
PER_EACH = re.compile(r"^\W{0,3}(?:each|ea\b|/\s*(?:each|ea|char|oc)|per\s+(?:character|char|oc|design|piece|one|perso?n?)|"
                      r"apiece|a\s+piece|за\s+(?:каждого|штуку|перса|одного))", re.I)
PER_SET = re.compile(r"^\W{0,3}(?:for\s+(?:the\s+|a\s+)?(?:whole\s+)?(?:set|bundle|pair|duo|trio|all|both|lot|group|batch)|"
                     r"(?:the\s+)?(?:set|bundle)\b|за\s+(?:всех|набор|сет|пару|обоих|всё|все))", re.I)
PER_SET_BEFORE = re.compile(r"(?:set|bundle|all\s+(?:of\s+them|\d+)|both|вс[её]х|набор|сет)\s*(?:for|[:\-=]|за)?\s*$", re.I)

FLAGS = {
    "trades_only": re.compile(r"\btrades?\s*only\b|\bonly\s+(?:for\s+)?trades?\b|только\s+(?:на\s+)?(?:обмен|трейд)\w*", re.I),
    "art_only": re.compile(r"\bart\s*only\b|\bonly\s+(?:for\s+)?art\b|только\s+(?:за\s+)?арт\w*", re.I),
    "usd_only": re.compile(r"\b(?:usd|\$|paypal|cash|money)\s*only\b|\bonly\s+(?:usd|\$|paypal|cash|money)\b|"
                           r"только\s+(?:доллар\w*|деньги|usd)", re.I),
    "points_only": re.compile(r"\b(?:pts|points)\s*only\b|\bonly\s+(?:pts|points)\b|только\s+(?:поинт\w*|pts)", re.I),
    "flexible": re.compile(r"\bflexible\s+budget\b|\bbudget\s*(?:is\s*)?(?:[:\-]\s*)?(?:flexible|negotiable|open)\b|"
                           r"\bno\s+budget\s+(?:limit|cap)\b|\bany\s+budget\b|бюджет\s+(?:гибк\w*|любой)", re.I),
    "no_budget": re.compile(r"\bno\s+(?:set\s+|real\s+)?budget\b(?!\s*(?:limit|cap))|"
                            r"\bbudget\s*[:\-]?\s*(?:none|n/?a|nothing|0)\b|без\s+бюджета|бюджета\s+нет", re.I),
    "obo": re.compile(r"\bobo\b|\bor\s+best\s+offer\b|\bnegotiable\b|\bopen\s+to\s+offers\b|\boffers?\s+(?:welcome|accepted)\b|"
                      r"\bторг\b", re.I),
}
FLAG_NAMES = {"trades_only": "только обмен", "art_only": "только арт", "usd_only": "только $", "points_only": "только pts",
              "flexible": "бюджет гибкий", "no_budget": "бюджет не назван/нет", "obo": "торг (OBO)"}


def _clause_before(text: str, start: int, width: int = 40, commas: bool = False) -> str:
    before = text[max(0, start - width):start]
    return re.split(r"[,.!?;\n|]" if commas else r"[.!?;\n|]\s", before)[-1]


@dataclass
class Stated:
    """Что написано в треде про деньги."""
    side: str                                   # lf | sale
    amounts: list[Money] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    unclear: int = 0                            # суммы, которые не поняли как бюджет/цену

    def per_char(self, currency: str) -> list[Money]:
        return [m for m in self.amounts if m.currency == currency and m.per != "set"]

    def currencies(self) -> list[str]:
        return sorted({m.currency for m in self.amounts if m.currency and m.per != "set"})

    def cap(self, currency: str) -> float | None:
        vals = [m.cap for m in self.per_char(currency) if m.cap is not None]
        return max(vals) if vals else None

    def floor(self, currency: str) -> float | None:
        vals = [v for m in self.per_char(currency) if (v := m.low if m.low is not None else m.high) is not None]
        return min(vals) if vals else None


def _default_currency(flags: set[str]) -> str | None:
    if "usd_only" in flags:
        return "usd"
    if "points_only" in flags:
        return "points"
    return None


def parse_stated(text: str, side: str = "lf", title: str = "") -> Stated:
    """Бюджеты (side='lf') или цены (side='sale') из названия и первого поста.

    Для LF сумма — бюджет, только если перед ней «budget/up to/can pay/O:/от/до…» или это диапазон;
    «I paid $40 for her» — не бюджет. Число без валюты берём, только если в треде «USD only»/«pts only»."""
    blob = f"{title or ''}\n{text or ''}"
    st = Stated(side=side, flags={k for k, p in FLAGS.items() if p.search(blob)})
    default = _default_currency(st.flags)
    title_len = len(title or "") + 1
    offer_at = None
    if title:
        m = OFFER_MARK.search(title)
        offer_at = m.end() if m else None
    prev: Money | None = None
    for money in find_money(blob):
        before = _clause_before(blob, money.start)
        # «budget $30 or 3000 pts / €25» — альтернативы в других валютах наследуют смысл первой суммы.
        alt = (prev is not None and money.high is None and money.currency and prev.currency != money.currency
               and re.fullmatch(r"\s*(?:or|/|или|,)\s*", blob[prev.end:money.start], re.I))
        after = blob[money.end:money.end + 30]
        near = " ".join(_clause_before(blob, money.start, commas=True).split()[-3:])
        if PAST.search(near):
            st.unclear += 1
            continue
        if money.currency is None:
            # Число без валюты — сумма, только если в треде «USD only»/«pts only», перед ним явная подсказка
            # бюджета и после него не единица («3 ocs», «2 per person», «3 weeks»). «under/over/at least 18» —
            # скорее возраст, голое число после них не берём.
            if (not default or not (BARE_CUE.search(before) or CUE_PAY.search(before))
                    or NOT_MONEY_AFTER.match(after) or AGE_BEFORE.search(before)):
                continue
            money.currency = default
        if PER_EACH.match(after):
            money.per = "each"
        elif PER_SET.match(after) or PER_SET_BEFORE.search(before):
            money.per = "set"
        in_offer = offer_at is not None and money.start >= offer_at and money.start < title_len
        if money.high is None:
            if CUE_APPROX.search(before):
                money.approx = True
                money.high = money.low
            elif CUE_MIN.search(before) or after.startswith("+"):
                pass  # «от $20», «$20+» — нижняя граница
            elif alt and prev is not None and prev.approx:
                money.approx, money.high = True, money.low
            elif alt and prev is not None and prev.high is None:
                pass
            elif alt or CUE_MAX.search(before) or CUE_PAY.search(before) or MAX_AFTER.match(after) or in_offer:
                money.high, money.low = money.low, None
            elif side == "lf":
                st.unclear += 1
                continue
        st.amounts.append(money)
        prev = money
    return st


# ---------- статистика ----------

@dataclass
class Stats:
    n: int = 0
    people: int = 0
    low: float | None = None      # p25 (или минимум, если n<8)
    high: float | None = None     # p75 (или максимум)
    median: float | None = None
    min: float | None = None
    max: float | None = None


def stats(values: list[float], people: int | None = None) -> Stats:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return Stats(people=people or 0)
    if len(vals) >= 8:  # на малой выборке квартили обманчивы — показываем весь разброс
        q = statistics.quantiles(vals, n=4, method="inclusive")
        low, high = q[0], q[2]
    else:
        low, high = vals[0], vals[-1]
    return Stats(n=len(vals), people=people if people is not None else len(vals), low=low, high=high,
                 median=statistics.median(vals), min=vals[0], max=vals[-1])


def confidence(n: int) -> str:
    """Та же шкала, что и везде (evidence.level): меньше 8 — «нет вывода»."""
    from .forecast import confidence as shared

    return shared(n)


SYMBOL = {"usd": "$", "eur": "€", "gbp": "£", "cad": "C$", "aud": "A$", "nzd": "NZ$", "rub": "₽"}


def money(v: float | None, cur: str) -> str:
    if v is None:
        return "?"
    if cur == "points":
        return f"{round(v):,} pts"
    v = round(v, 2)
    text = f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    return f"{text} ₽" if cur == "rub" else f"{SYMBOL.get(cur, cur + ' ')}{text}"


def _span(s: Stats, cur: str) -> str:
    if s.n == 0:
        return ""
    if s.low == s.high:
        return money(s.low, cur)
    return f"{money(s.low, cur)}–{money(s.high, cur)}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _people(n: int) -> str:
    return f"{n} {_plural(n, 'человек', 'человека', 'человек')}"


def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m")


# ---------- данные ----------

@dataclass
class Thread:
    id: str
    side: str
    author: str
    title: str
    url: str
    ts: float
    has_text: bool
    tags: list[str]
    species: list[str]
    stated: Stated
    snippet: str = ""
    groups: dict[str, list[str]] = field(default_factory=dict)


def _side(r) -> str | None:
    if r["match_kind"] == "lf" or r["wants_characters"]:
        return "lf"
    if r["match_kind"] == "sale" or r["sells_characters"]:
        return "sale"
    return None


def load_threads(conn, days: int = 30, now: float | None = None, my_username: str = "") -> list[Thread]:
    """LF-треды и треды продаж за days дней до now (без её собственных и без того, что появилось позже now)."""
    now = now or time.time()
    rows = conn.execute(
        "SELECT * FROM items WHERE source = 'toyhouse' AND first_seen_ts <= ? "
        "AND COALESCE(activity_ts, published_ts, first_seen_ts) BETWEEN ? AND ?",
        (now, now - days * DAY, now)).fetchall()
    out = []
    for r in rows:
        side = _side(r)
        if not side or (my_username and (r["author"] or "").lower() == my_username.lower()):
            continue
        snippet = r["snippet"] or ""
        stated = parse_stated(snippet, side, r["title"] or "")
        if side == "sale" and not any(m.currency for m in stated.amounts):
            # Первый пост продаж не читаем: берём цену, которую мониторинг уже вынул из названия.
            for cur, col in (("usd", "price_usd"), ("points", "price_points")):
                if r[col] is not None:
                    stated.amounts.append(Money(r[col], None, cur, raw=f"{col}"))
        pays = set((r["pays"] or "").split(","))
        if side == "lf" and pays & {"trades", "art"} and not pays & {"usd", "points"}:
            stated.flags.add("trades_only" if "trades" in pays and "art" not in pays else
                             "art_only" if "art" in pays and "trades" not in pays else "noncash")
        t = tag_text(r["title"] or "", (), snippet)
        out.append(Thread(id=r["id"], side=side, author=(r["author"] or "").lower(), title=r["title"] or "",
                          url=r["url"], ts=r["activity_ts"] or r["published_ts"] or r["first_seen_ts"],
                          has_text=bool(snippet.strip()), tags=[*t.species, *t.body, *t.styles, *t.themes],
                          species=list(t.species), stated=stated, snippet=snippet,
                          groups={"species": t.species, "body": t.body, "styles": t.styles, "themes": t.themes}))
    return out


@dataclass
class Segment:
    key: str
    lf: int = 0
    lf_people: int = 0
    sale: int = 0
    budgets: dict[str, Stats] = field(default_factory=dict)
    asks: dict[str, Stats] = field(default_factory=dict)
    flags: Counter = field(default_factory=Counter)

    @property
    def ratio(self) -> float | None:
        return round(self.lf / self.sale, 2) if self.sale else None


def _per_person(threads: list[Thread], cur: str, cap: bool = True) -> dict[str, float]:
    """Один человек — одно число: у покупателя с тремя LF-тредами берём самый высокий бюджет."""
    out: dict[str, float] = {}
    for t in threads:
        v = t.stated.cap(cur) if cap else t.stated.floor(cur)
        if v is not None:
            out[t.author] = max(out[t.author], v) if (cap and t.author in out) else (
                min(out[t.author], v) if t.author in out else v)
    return out


def _money_stats(threads: list[Thread], cap: bool = True) -> dict[str, Stats]:
    curs = sorted({c for t in threads for c in t.stated.currencies()})
    return {cur: stats(list(vals.values())) for cur in curs if (vals := _per_person(threads, cur, cap))}


def _segment(key: str, threads: list[Thread]) -> Segment:
    lf = [t for t in threads if t.side == "lf"]
    sale = [t for t in threads if t.side == "sale"]
    seg = Segment(key=key, lf=len(lf), lf_people=len({t.author for t in lf}), sale=len(sale),
                  budgets=_money_stats(lf, cap=True), asks=_money_stats(sale, cap=False))
    for t in lf:
        seg.flags.update(f for f in t.stated.flags if f in ("trades_only", "art_only", "no_budget", "flexible", "noncash"))
    return seg


@dataclass
class Report:
    days: int
    since: float
    until: float
    n_lf: int
    n_lf_text: int
    n_sale: int
    overall: Segment
    segments: list[Segment]
    realized: dict[str, Stats]
    realized_no_cur: int = 0


def realized_sales(conn, since: float, until: float) -> tuple[dict[str, Stats], int]:
    """Её оплаченные продажи на Toyhouse за период — единственные настоящие цены, что у нас есть.
    → (по валютам, сколько оплаченных без валюты: их в суммы не берём — 4000 чего, непонятно)."""
    try:
        rows = conn.execute("SELECT amount, currency, buyer FROM sales WHERE platform = 'toyhouse' AND amount IS NOT NULL "
                            "AND status IN ('paid', 'transferred', 'done') AND (status != 'transferred' OR paid_ts "
                            "IS NOT NULL) AND COALESCE(paid_ts, created_ts) BETWEEN ? AND ?",
                            (since, until)).fetchall()
    except Exception:  # noqa: BLE001 — нет таблицы sales в старой базе
        return {}, 0
    by: dict[str, list[float]] = {}
    for r in rows:
        if r["currency"]:
            by.setdefault(r["currency"], []).append(r["amount"])
    return {cur: stats(vals) for cur, vals in by.items()}, sum(1 for r in rows if not r["currency"])


def demand_supply(conn, days: int = 30, now: float | None = None, my_username: str = "", min_threads: int = 2,
                  group: str = "all") -> Report:
    """Спрос (LF) против предложения (продажи) по видам/стилям/темам. group: species | styles | themes | all."""
    now = now or time.time()
    threads = load_threads(conn, days, now, my_username)
    by_key: dict[str, list[Thread]] = {}
    for t in threads:
        tags = t.tags if group == "all" else t.groups.get(group, [])
        for key in dict.fromkeys(tags):
            by_key.setdefault(key, []).append(t)
    segments = [_segment(k, v) for k, v in by_key.items() if len(v) >= min_threads]
    segments.sort(key=lambda s: (s.lf_people, s.lf, -s.sale), reverse=True)
    lf = [t for t in threads if t.side == "lf"]
    realized = realized_sales(conn, now - days * DAY, now)
    return Report(days=days, since=now - days * DAY, until=now, n_lf=len(lf), n_lf_text=sum(t.has_text for t in lf),
                  n_sale=sum(t.side == "sale" for t in threads), overall=_segment("всё", threads), segments=segments,
                  realized=realized[0], realized_no_cur=realized[1])


def _budget_line(stats_by_cur: dict[str, Stats]) -> str:
    parts = []
    for cur, s in sorted(stats_by_cur.items(), key=lambda kv: -kv[1].n):
        parts.append(f"{_span(s, cur)}, медиана {money(s.median, cur)} (n={s.n})")
    return "; ".join(parts)


def _flags_line(flags: Counter) -> str:
    names = {**FLAG_NAMES, "noncash": "арт/обмен"}
    return ", ".join(f"{names[f]} {n}" for f, n in flags.most_common() if f in names)


def render_market(r: Report, top: int = 8) -> str:
    head = (f"Toyhouse за {r.days} дн ({_date(r.since)}–{_date(r.until)}), источник: {SOURCE_LABEL}. "
            f"LF n={r.n_lf} (первый пост прочитан у {r.n_lf_text}), продажи n={r.n_sale} (цены — из названий).")
    if not r.n_lf and not r.n_sale:
        return head + " Данных нет: мониторинг Toyhouse ещё ничего не собрал за этот период."
    lines = [head]
    if r.overall.budgets:
        lines.append(f"Бюджеты всех LF: {_budget_line(r.overall.budgets)}. Уверенность: "
                     f"{confidence(max(s.n for s in r.overall.budgets.values()))}.")
    for s in r.segments[:top]:
        parts = [f"• {s.key}: ищут {_people(s.lf_people)} (LF {s.lf}) / продают {s.sale}"]
        if s.ratio is not None:
            parts.append(f"спрос/предл. {s.ratio:g}")
        if s.budgets:
            parts.append(f"бюджеты {_budget_line(s.budgets)}")
        if s.asks:
            parts.append(f"просят {_budget_line(s.asks)}")
        fl = _flags_line(s.flags)
        if fl:
            parts.append(fl)
        n = max((x.n for x in s.budgets.values()), default=0)
        lines.append("; ".join(parts) + f". Уверенность: {confidence(n)}.")
    real = "; ".join(f"{_span(s, c)}, медиана {money(s.median, c)} (n={s.n})" for c, s in r.realized.items())
    no_cur = (f" Ещё {r.realized_no_cur} оплаченных без валюты — в цены не вошли; укажи валюту (sales:list)."
              if r.realized_no_cur else "")
    lines.append("Это заявленные бюджеты и запрошенные цены (намерения), а не сделки. "
                 + (f"Твои оплаченные продажи на TH за период: {real}." if real else
                    "Реальных цен продаж на TH у нас нет: чужие сделки не видны, своих оплаченных за период"
                    + (" с валютой" if no_cur else "") + " не записано.") + no_cur)
    return "\n".join(lines)


# ---------- её персы против бюджетов ----------

NEGATED = re.compile(r"\b(?:no|not|without|except|anything\s+but|besides)\s+(?:any\s+|more\s+)?([a-zа-яё][\w\-]{2,})", re.I)


def parse_price(text: str) -> Money | None:
    """Её цена у перса: «$40», «40 USD», «1500 pts», «$30-40», «$40 OBO». Число без валюты — None."""
    found = [m for m in find_money(text or "") if m.currency]
    return found[0] if found else None


@dataclass
class CharDemand:
    name: str
    price_raw: str
    price: Money | None
    threads: int = 0
    people: int = 0
    budgets: dict[str, Stats] = field(default_factory=dict)
    numeric: int = 0          # сколько людей назвали бюджет в валюте её цены
    fits: int = 0             # из них бюджет >= её цены
    flags: Counter = field(default_factory=Counter)
    examples: list[tuple[str, str]] = field(default_factory=list)
    days: int = 30

    @property
    def confidence(self) -> str:
        return confidence(self.numeric or self.people)


def _char_species(ch: dict) -> set[str]:
    tags = [t for t in (ch.get("tags") or []) if isinstance(t, str)]
    return set(tag_text(str(ch.get("name") or ""), tags).species)


def _negated_species(t: Thread) -> set[str]:
    words = NEGATED.findall(f"{t.title}\n{t.snippet}")
    return set(tag_text(" ".join(words)).species) if words else set()


def _matches(ch: dict, sp: set[str], t: Thread) -> bool:
    if sp:
        return bool(sp & set(t.species)) and not (sp & _negated_species(t))
    from .th_reply import match_characters  # теги без вида («cute», «pastel») — тот же подбор, что для откликов

    return bool(match_characters(t.title, t.snippet, [ch]))


def character_demand(conn, characters: list[dict], days: int = 30, now: float | None = None,
                     my_username: str = "") -> list[CharDemand]:
    """Для каждого её перса (cfg.posting.characters): сколько людей ищут такого в LF-тредах и с каким бюджетом."""
    now = now or time.time()
    lf = [t for t in load_threads(conn, days, now, my_username) if t.side == "lf"]
    out = []
    for ch in characters or []:
        if not isinstance(ch, dict):
            continue
        sp = _char_species(ch)
        hits = [t for t in lf if _matches(ch, sp, t)]
        price = parse_price(str(ch.get("price") or ""))
        d = CharDemand(name=str(ch.get("name") or ch.get("url") or "?"), price_raw=str(ch.get("price") or ""),
                       price=price, threads=len(hits), people=len({t.author for t in hits}),
                       budgets=_money_stats(hits), days=days)
        for t in hits:
            d.flags.update(f for f in t.stated.flags if f in ("trades_only", "art_only", "no_budget", "flexible", "noncash"))
        if price and price.currency:
            ask = price.low if price.low is not None else price.high
            caps = list(_per_person(hits, price.currency).values())
            d.numeric = len(caps)
            d.fits = sum(c >= ask for c in caps) if ask is not None else 0
        d.examples = [(t.title[:60], t.url) for t in sorted(hits, key=lambda t: -t.ts)[:3]]
        out.append(d)
    return out


def render_character_demand(items: list[CharDemand], examples: bool = False) -> str:
    if not items:
        return "Персов на продажу нет в настройках (posting.characters) — сравнивать не с чем."
    lines = []
    for d in items:
        src = f"LF-треды Toyhouse за {d.days} дн"
        if not d.threads:
            lines.append(f"{d.name}: под этого перса за {d.days} дн LF-тредов не нашлось ({SOURCE_LABEL}).")
            continue
        cur = d.price.currency if d.price else None
        s = d.budgets.get(cur) if cur else None
        if s is None and d.budgets:
            cur, s = max(d.budgets.items(), key=lambda kv: kv[1].n)
        if s and s.n and cur:
            text = (f"{d.name}: под этого перса ищут {_people(d.people)} с бюджетом {_span(s, cur)} "
                    f"(n={s.n} с суммой, LF {d.threads}, {src})")
        else:
            text = f"{d.name}: под этого перса ищут {_people(d.people)} (LF {d.threads}, {src}), бюджет в цифрах не назван"
        if d.price and d.price.currency and d.numeric:
            text += f"; твоя цена {money(d.price.low if d.price.low is not None else d.price.high, d.price.currency)}"
            text += f" — укладываются {d.fits} из {d.numeric}"
        elif d.price_raw and not d.price:
            text += f"; цену «{d.price_raw}» не поняла (нужна валюта: $, pts…)"
        elif d.price and d.price.currency and not d.numeric:
            text += f"; в {money(d.price.low or d.price.high, d.price.currency)} бюджетов нет — сравнить не с чем"
        fl = _flags_line(d.flags)
        if fl:
            text += f"; {fl}"
        text += f". Уверенность: {d.confidence}."
        if examples and d.examples:
            text += " " + " · ".join(f"«{title}» {url}" for title, url in d.examples)
        lines.append(text)
    lines += ["", "Бюджеты — то, что люди написали в LF (намерения), не цены сделок."]  # остаётся при обрезке
    return "\n".join(lines)
