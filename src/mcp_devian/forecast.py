"""Прогноз продаж по нишам: в каких нишах аукционы получают ставку, с каким стартом и насколько этому верить.

Основа — завершённые аукционы DeviantArt (лот + ставки из таблицы auctions):
- «получил ставку» = набрал хотя бы одну ставку (это не то же самое, что «купили»);
- итог считаем известным, только если аукцион точно закончился: автор написал «закрыто/продано»,
  или название стало закрытым и ставки перечитаны после этого, или ставки читали, когда лоту было
  ≥ MATURE_DAYS дней, и название уже не «открыт». Иначе лоты без ставок (о них никто не пишет «sold to»)
  выпадали бы из выборки, и доля «со ставкой» выходила бы завышенной;
- если итог известен меньше чем у MIN_COVERAGE выборки — прогноз не строим;
- рынок — чужие лоты из отслеживаемых папок и галерей; лоты, найденные только поиском, и пачки «много
  лотов в одном посте» в доли не входят; один продавец даёт не больше SELLER_CAP лотов за SELLER_WINDOW_DAYS;
- уровни продавцов сравниваем только по числу вотчеров (author_stats). Нет данных — сравниваем со всем рынком
  и говорим об этом;
- сезон: только лоты с явной праздничной темой. Поправка «после сезона» — только по ≥ evidence.MIN_SIDE лотам
  этого времени; без данных честно пишем «после сезона данных нет».

Числа модели (predict) называются «по модели». Наблюдения — «k из n» с 80%-интервалом. Каждое сравнение
«лучше остальных» — с P(лучше) и поправкой на число проверенных ниш (evidence.family_threshold).
Все выводы детерминированы: повторный вопрос на тех же данных даёт тот же ответ.
"""

import json
import math
import re
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from . import evidence, taxonomy
from .taxonomy import Tags

try:  # W1 — модуль выборки рынка; до него префикс тот же
    from .da_market import SEARCH_PLACE_PREFIX
except ImportError:  # pragma: no cover
    SEARCH_PLACE_PREFIX = evidence.SEARCH_PLACE_PREFIX

DAY = 86400
AUCTION_TYPES = ("auction", "bta")
TRADING_TYPES = ("auction", "bta", "flatsale", "ota", "adopt", "raffle")
UNITS = ("usd", "points")

HISTORY_DAYS = 180           # сколько истории берём
MATURE_DAYS = 7              # ставки прочитаны в возрасте ≥ этого — аукцион точно закончился
# Прочитан в 14+ дней — итог известен, даже если в названии всё ещё [OPEN]: так висят как раз лоты без
# ставок, и без этого правила они навсегда оставались бы «неизвестными», а доля со ставкой — завышенной.
FINAL_DAYS = 14
MIN_COVERAGE = 0.7           # итог должен быть известен хотя бы у такой доли выборки
RECENCY_HALF_LIFE_DAYS = 14  # в модели вес лота падает вдвое за столько дней
SUPPLY_DAYS = 14             # «сколько похожих сейчас на рынке»
PRIOR_STRENGTH = 6.0         # модель: сколько «виртуальных лотов» общего уровня добавляем к нише
BUCKET_PRIOR = 4.0           # то же для ценовых корзин
MIN_TIER_POOL = 8            # меньше лотов её уровня — сравниваем со всеми и говорим об этом
MIN_PLAN_POOL = 12           # меньше — цену по прогнозу не предлагаем
MIN_START_LOTS = evidence.MIN_SIDE   # старт — кандидат, только если по этой цене столько лотов
MIN_TRAIN = 8                # меньше прошлых лотов — прогноз для проверки не строим
MIN_CALIBRATION = 20         # столько проверенных прогнозов нужно, чтобы вообще показать точность
MIN_SKILL_N = 30             # меньше — модель считаем «не доказанной»: уверенность не выше «низкой»
CALIBRATION_MAX = 400
SELLER_CAP = 3               # лотов одного продавца за окно
SELLER_WINDOW_DAYS = 30
POST_SEASON_WEEKS = 4
EASTER_HALF_WINDOW = 14      # пасха ± столько дней

# Уровень продавца — по вотчерам из API. Границы условные (общая практика, не из данных).
WATCHER_TIERS = (("newcomer", "новичок", 0), ("growing", "растущий продавец", 500),
                 ("established", "известный продавец", 5000))
TIER_LABEL = {key: label for key, label, _ in WATCHER_TIERS}
TIER_NOTE = evidence.heuristic("уровень по вотчерам: до 500 — новичок, до 5000 — растущий, дальше — известный",
                               "граница условная")
NO_TIERS = "уровни продавцов не сравнимы — сравниваю со всем рынком"
NUMBER_RE = re.compile(r"(?:#|№|\bno\.\s?)\s?(\d{1,3})\b", re.I)

SEASON_THEMES = ("halloween", "christmas", "valentine", "easter")
FIXED_SEASONS: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "halloween": ((10, 1), (10, 31)),
    "christmas": ((12, 1), (12, 26)),
    "valentine": ((2, 1), (2, 14)),
}
SEASONS = (*FIXED_SEASONS, "easter")
SEASON_LABEL = {"halloween": "хэллоуин", "christmas": "рождество", "valentine": "валентинки", "easter": "пасха"}

USD_EDGES = (0, 10, 15, 20, 30, 45, 60, 100)
POINTS_EDGES = (0, 100, 250, 500, 1000, 2000, 5000)

AB_HEURISTIC = evidence.heuristic("AB обычно 3–5× SB", evidence.GUIDE_2021)
# Полные правила пересказа — в get_settings (один раз за разговор); здесь короткая пометка.
GENTLE = "Цифры, n и уверенность — как есть; «Вывода нет» — так и скажи. Оценка, не гарантия."


def round_price(v: float, currency: str = "usd") -> float:
    if currency == "points":
        return float(max(10, int(round(v / 10.0)) * 10))
    if v >= 20:
        return float(int(round(v / 5.0)) * 5)
    return float(max(1, int(round(v))))


def _money(v: float | None, unit: str) -> str:
    if v is None:
        return "?"
    if unit == "points":
        return f"{v:g} pts"
    return f"${v:g}" if float(v).is_integer() else f"${v:.2f}"


def _pct(p: float) -> str:
    return f"{round(p * 100)}%"


def _date(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d.%m.%Y") if ts else "—"


def _unit_word(unit: str | None) -> str:
    return {"usd": "$", "points": "поинты"}.get(unit or "", "$ и поинты")


# --- Данные ---

@dataclass
class Outcome:
    id: str
    author: str
    own: bool
    title: str
    ts: float              # публикация
    known_ts: float        # когда итог стал нам известен (последнее чтение ставок)
    unit: str
    start: float
    sold: bool             # получил хотя бы одну ставку
    n_bids: int
    bidders: int
    final: float | None
    ratio: float | None    # итог / старт; у лотов с взятым автобаем — None (это AB/SB, а не торг)
    ab: float | None
    ab_hit: bool
    tags: Tags
    group: str | None      # праздничная группа (SEASON_THEMES) или None
    search: bool = False   # найден только поиском
    multi: bool = False    # несколько лотов в одном посте
    place: str | None = None
    bidder_names: tuple[str, ...] = ()
    read_age: float | None = None


def _extra(row, key: str = "extra") -> dict:
    try:
        v = json.loads(row[key] or "{}")
    except (json.JSONDecodeError, TypeError, KeyError, IndexError):
        return {}
    return v if isinstance(v, dict) else {}


def is_search(place: str | None) -> bool:
    return bool(place) and str(place).startswith(SEARCH_PLACE_PREFIX)


def is_multi(row) -> bool:
    return bool(_extra(row).get("multi"))


def analytics_tags(row) -> Tags:
    """Теги для аналитики: название + теги (и теги из API), без описания — в описаниях реклама и чужие списки."""
    try:
        return taxonomy.tag_item(row, use_snippet=False)  # type: ignore[call-arg]
    except TypeError:
        extra = _extra(row)
        tags = [*(extra.get("tags") or []), *(extra.get("api_tags") or [])]
        return taxonomy.tag_text(row["title"] or "", [t for t in tags if isinstance(t, str)], "")


def read_age_days(a_extra: dict, a_updated: float | None, published: float | None) -> float | None:
    age = a_extra.get("read_age_days")
    if isinstance(age, (int, float)):
        return float(age)
    if a_updated and published:
        return (a_updated - published) / DAY
    return None


def outcome_known(*, ended, status: str | None, closed_ts: float | None, a_extra: dict, a_updated: float | None,
                  published: float | None, item_updated: float | None) -> bool:
    """Итог аукциона известен: закончен по словам автора, или закрыт и перечитан после закрытия, или ставки
    читали в возрасте ≥ MATURE_DAYS и по названию он тогда уже не был открыт."""
    if ended or a_extra.get("zero_by_stats"):   # 0 комментариев в возрасте ≥7 дн по API — ставок не было
        return True
    if status == "closed" and (a_extra.get("seen_status") == "closed"
                               or (closed_ts is not None and (a_updated or 0) >= closed_ts)):
        return True
    age = read_age_days(a_extra, a_updated, published)
    if age is None or age < MATURE_DAYS:
        return False
    if age >= FINAL_DAYS:
        return True
    still_open = status == "open" and item_updated is not None and a_updated is not None \
        and item_updated >= a_updated - DAY
    return not still_open


def season_group(t: Tags) -> str | None:
    """Праздничная группа — только по явной теме (хэллоуин, рождество, валентинки, пасха)."""
    themes = set(t.themes)
    return next((g for g in SEASON_THEMES if g in themes), None)


def easter(year: int) -> date:
    """Католическая пасха (анонимный григорианский алгоритм)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def season_window(group: str, year: int) -> tuple[date, date]:
    if group == "easter":
        e = easter(year)
        return e - timedelta(days=EASTER_HALF_WINDOW), e + timedelta(days=EASTER_HALF_WINDOW)
    (sm, sd), (em, ed) = FIXED_SEASONS[group]
    return date(year, sm, sd), date(year, em, ed)


def season_phase(group: str | None, ts: float) -> str:
    """in — праздник идёт; post — до POST_SEASON_WEEKS недель после; off — в остальное время."""
    if not group or group not in SEASONS:
        return "off"
    d = datetime.fromtimestamp(ts, UTC).date()
    for year in (d.year - 1, d.year):
        start, end = season_window(group, year)
        if start <= d <= end:
            return "in"
        if end < d <= end + timedelta(weeks=POST_SEASON_WEEKS):
            return "post"
    return "off"


_TYPES_SQL = ",".join(f"'{t}'" for t in AUCTION_TYPES)


def _auction_rows(conn, now: float, days: int):
    try:
        return conn.execute(
            f"SELECT i.*, a.top_amount AS a_top, a.currency AS a_currency, a.n_bids AS a_n_bids, a.bidders AS a_bidders, "
            f"a.ab_hit AS a_ab_hit, a.ended AS a_ended, a.updated_ts AS a_updated, a.extra AS a_extra "
            f"FROM items i JOIN auctions a ON a.deviation_id = i.id WHERE i.source = 'deviantart' "
            f"AND i.lot_type IN ({_TYPES_SQL}) AND COALESCE(i.published_ts, i.first_seen_ts) >= ? "
            f"ORDER BY COALESCE(i.published_ts, i.first_seen_ts), i.id", (now - days * DAY,)).fetchall()
    except Exception:  # noqa: BLE001 — старая база без таблицы auctions
        return []


def _row_known(r) -> bool:
    return outcome_known(ended=r["a_ended"], status=r["status"], closed_ts=r["closed_ts"],
                         a_extra=_extra(r, "a_extra"), a_updated=r["a_updated"],
                         published=r["published_ts"] or r["first_seen_ts"], item_updated=r["updated_ts"])


def _bidder_names(a_extra: dict) -> tuple[str, ...]:
    raw = a_extra.get("bidders")
    if not isinstance(raw, list):
        return ()
    names = {str(b.get("user", "")).strip().lstrip("@").lower() for b in raw if isinstance(b, dict)}
    return tuple(sorted(n for n in names if n))


def load_outcomes(conn, now: float | None = None, days: int = HISTORY_DAYS) -> list[Outcome]:
    """Аукционы DA с известным итогом (её и чужие), старые → новые."""
    from .auctions import ab_price

    now = now or time.time()
    out = []
    for r in _auction_rows(conn, now, days):
        if r["price_usd"] is not None:
            unit, start = "usd", float(r["price_usd"])
        elif r["price_points"] is not None:
            unit, start = "points", float(r["price_points"])
        else:
            continue
        if start <= 0 or not _row_known(r):
            continue
        ts = r["published_ts"] or r["first_seen_ts"]
        a_extra = _extra(r, "a_extra")
        updated = r["a_updated"] or 0
        n_bids = int(r["a_n_bids"] or 0)
        top = r["a_top"]
        sold = n_bids > 0 and bool(top)
        ab_hit = bool(r["a_ab_hit"])
        final = float(top) if sold and (r["a_currency"] or unit) == unit else None
        ratio = final / start if final and not ab_hit else None
        t = analytics_tags(r)
        names = _bidder_names(a_extra)
        out.append(Outcome(
            id=r["id"], author=(r["author"] or "").lower(), own=bool(r["own"]), title=r["title"] or "", ts=ts,
            known_ts=updated or ts, unit=unit, start=start, sold=sold, n_bids=n_bids,
            bidders=len(names) or int(r["a_bidders"] or 0) or (1 if n_bids else 0), final=final, ratio=ratio,
            ab=ab_price(r["snippet"], unit), ab_hit=ab_hit, tags=t, group=season_group(t),
            search=is_search(r["place"]), multi=is_multi(r), place=r["place"], bidder_names=names,
            read_age=read_age_days(a_extra, r["a_updated"], ts)))
    return out


def outcome_coverage(conn, now: float | None = None) -> tuple[int, int]:
    """(у скольких итог известен, сколько всего) среди аукционов рынка из папок старше MATURE_DAYS со ставками в базе."""
    now = now or time.time()
    known = sampled = 0
    for r in _auction_rows(conn, now, HISTORY_DAYS):
        if r["own"] or is_search(r["place"]) or (r["published_ts"] or r["first_seen_ts"]) > now - MATURE_DAYS * DAY:
            continue
        if r["price_usd"] is None and r["price_points"] is None:
            continue
        sampled += 1
        known += _row_known(r)
    return known, sampled


def coverage_refusal(known: int, sampled: int) -> str | None:
    if sampled and known / sampled < MIN_COVERAGE:
        return (f"Вывода нет: итог известен только у {_pct(known / sampled)} выборки ({known} из {sampled} "
                f"аукционов старше {MATURE_DAYS} дн) — нужно ≥{_pct(MIN_COVERAGE)}. Остальные ещё не дочитаны после "
                "окончания; без них доля «со ставкой» вышла бы завышенной.")
    return None


def cap_sellers(obs: list[Outcome], ref: float, per: int = SELLER_CAP,
                window_days: int = SELLER_WINDOW_DAYS) -> list[Outcome]:
    """Не больше per самых свежих лотов одного продавца за каждое окно: один плодовитый продавец — не «рынок»."""
    groups: dict[tuple[str, int], list[Outcome]] = defaultdict(list)
    keep: list[Outcome] = []
    for o in obs:
        if not o.author:
            keep.append(o)
            continue
        groups[(o.author, int(max(0.0, ref - o.ts) // (window_days * DAY)))].append(o)
    for items in groups.values():
        keep += sorted(items, key=lambda o: (o.ts, o.id), reverse=True)[:per]
    return sorted(keep, key=lambda o: (o.ts, o.id))


def market_pool(outcomes: list[Outcome], unit: str | None = None) -> list[Outcome]:
    """Чужие аукционы из папок (без поиска и без пачек), не больше SELLER_CAP лотов продавца за окно."""
    base = [o for o in outcomes if not o.own and not o.search and not o.multi and (unit is None or o.unit == unit)]
    return cap_sellers(base, _cutoff(base) or 0.0)


# --- Уровень продавца ---

@dataclass
class Tier:
    key: str
    label: str
    lots: int
    number: int
    watchers: int | None = None

    @property
    def known(self) -> bool:
        return self.key != "unknown"


def tier_of_watchers(watchers: int) -> tuple[str, str]:
    key, label = WATCHER_TIERS[0][0], WATCHER_TIERS[0][1]
    for k, lab, threshold in WATCHER_TIERS:
        if watchers >= threshold:
            key, label = k, lab
    return key, label


def _author_watchers(conn) -> dict[str, int]:
    try:
        rows = conn.execute("SELECT author, watchers FROM author_stats WHERE watchers IS NOT NULL").fetchall()
    except Exception:  # noqa: BLE001 — таблицы может не быть
        return {}
    return {(r["author"] or "").lower(): int(r["watchers"]) for r in rows if r["author"]}


class SellerTiers:
    """Уровень продавцов по вотчерам (author_stats). Нет строки — уровень неизвестен."""

    def __init__(self, conn) -> None:
        self.watchers = _author_watchers(conn)

    def tier(self, author: str) -> str | None:
        w = self.watchers.get((author or "").lower())
        return tier_of_watchers(w)[0] if w is not None else None

    def tier_at(self, author: str, ts: float = 0.0, own: bool = False) -> str | None:  # старое имя
        return self.tier(author)


SellerHistory = SellerTiers  # старое имя


def her_name(conn) -> str | None:
    row = conn.execute("SELECT lower(author) AS a, COUNT(*) AS c FROM items WHERE source = 'deviantart' AND own = 1 "
                       "AND author IS NOT NULL GROUP BY lower(author) ORDER BY c DESC LIMIT 1").fetchone()
    return row["a"] if row else None


def her_tier(conn, now: float | None = None) -> Tier:
    """Её уровень — так же, как у остальных: по вотчерам из author_stats. Нет данных — «неизвестен»."""
    types = ",".join(f"'{t}'" for t in TRADING_TYPES)
    rows = conn.execute(f"SELECT title FROM items WHERE source = 'deviantart' AND own = 1 AND lot_type IN ({types})"
                        ).fetchall()
    number = max((int(m.group(1)) for r in rows if (m := NUMBER_RE.search(r["title"] or ""))), default=0)
    name = her_name(conn)
    watchers = _author_watchers(conn).get(name or "") if name else None
    if watchers is None:
        return Tier("unknown", "неизвестен", len(rows), number, None)
    key, label = tier_of_watchers(watchers)
    return Tier(key, label, len(rows), number, watchers)


def _tier_pool(pool: list[Outcome], tiers: SellerTiers, her: Tier) -> tuple[list[Outcome], bool, str | None]:
    if not her.known:
        return pool, True, NO_TIERS
    sub = [o for o in pool if tiers.tier(o.author) == her.key]
    if len(sub) >= MIN_TIER_POOL:
        return sub, False, None
    return pool, True, (f"лотов продавцов уровня «{her.label}» (по вотчерам) {len(sub)} — меньше {MIN_TIER_POOL}; "
                        "сравниваю со всем рынком")


def _population(unit: str | None, her: Tier, fallback: bool) -> str:
    who = "все продавцы" if fallback or not her.known else f"продавцы уровня «{her.label}»"
    return f"аукционы DA, {_unit_word(unit)}, {who}, без её лотов"


# --- Модель: вероятность, что лот наберёт ставку ---

def _w(ts: float, ref: float) -> float:
    return 0.5 ** (max(0.0, ref - ts) / (RECENCY_HALF_LIFE_DAYS * DAY))


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _expit(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _shrunk(obs: list[Outcome], ref: float, prior: float, strength: float = PRIOR_STRENGTH) -> tuple[float, float]:
    """(взвешенная доля со ставкой, притянутая к prior; эффективное число лотов)."""
    ws = [_w(o.ts, ref) for o in obs]
    total = sum(ws)
    if not total:
        return prior, 0.0
    scale = total / sum(x * x for x in ws)   # Σw·scale = n_eff
    s = sum(x for x, o in zip(ws, obs, strict=True) if o.sold) * scale
    w = total * scale
    return (s + strength * prior) / (w + strength), w


def n_eff(obs: list[Outcome], ref: float) -> float:
    ws = [_w(o.ts, ref) for o in obs]
    sq = sum(x * x for x in ws)
    return (sum(ws) ** 2 / sq) if sq else 0.0


def confidence(n: float) -> str:
    """Уверенность по числу лотов — та же шкала, что и везде (evidence.level)."""
    return evidence.level(evidence.Evidence(n=int(n), n_eff=None, since_ts=0, until_ts=0, data_as_of=0,
                                            source="market_da", population="", unit=None, kind="observed"))


def _base_rate(pool: list[Outcome], ref: float) -> float:
    return _shrunk(pool, ref, 0.5, 1.0)[0]


def _similar(o: Outcome, t: Tags) -> bool:
    if t.species:  # вид главнее стиля: стиль без вида даёт слишком пёструю выборку
        return bool(set(o.tags.species) & set(t.species))
    keys = set(t.styles) | set(t.themes)
    return bool(keys & (set(o.tags.styles) | set(o.tags.themes)))


def _season_split(matches: list[Outcome], phase_now: str) -> list[Outcome]:
    return [o for o in matches if season_phase(o.group, o.ts) == phase_now]


def _niche_rate(pool: list[Outcome], matches: list[Outcome], ref: float, p0: float, phase_now: str,
                group: str | None) -> tuple[float, float, str]:
    """Доля ниши с учётом сезона: (оценка модели, вес данных, что сделали с сезоном). Поправка — только по данным."""
    p_all, w_all = _shrunk(matches, ref, p0)
    if not group or phase_now == "off":
        return p_all, w_all, ""
    same = _season_split(matches, phase_now)
    if len(same) < evidence.MIN_SIDE:
        return p_all, w_all, ("после сезона данных нет" if phase_now == "post" else
                              f"сезон идёт, лотов этого сезона мало ({len(same)})")
    p, _w_same = _shrunk(same, ref, p_all)
    word = "после сезона" if phase_now == "post" else "сезон идёт"
    return p, w_all, f"{word} — по {len(same)} лотам этого времени"


def _edges(unit: str) -> tuple[float, ...]:
    return USD_EDGES if unit == "usd" else POINTS_EDGES


def _bucket(start: float, unit: str) -> int:
    import bisect

    return max(0, bisect.bisect_right(_edges(unit), start) - 1)


def _pava_decreasing(values: list[float], weights: list[float]) -> list[float]:
    """Монотонно невозрастающая подгонка (чем выше старт, тем не выше шанс ставки)."""
    blocks = [[v, w, 1] for v, w in zip(values, weights, strict=True)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] < blocks[i + 1][0]:
            v1, w1, c1 = blocks[i]
            v2, w2, c2 = blocks[i + 1]
            w = w1 + w2
            blocks[i] = [(v1 * w1 + v2 * w2) / w if w else (v1 + v2) / 2, w, c1 + c2]
            del blocks[i + 1]
            i = max(0, i - 1)
        else:
            i += 1
    out: list[float] = []
    for v, _w, c in blocks:
        out += [v] * int(c)
    return out


def bucket_rates(pool: list[Outcome], unit: str, ref: float, p0: float) -> list[dict]:
    """Модель: шанс ставки по корзинам старта (монотонно, притянуто к общему уровню). Пустые корзины — без
    экстраполяции вверх: дороже всего виденного берём уровень последней корзины с данными (n=0 — вывода нет)."""
    edges = _edges(unit)
    by: dict[int, list[Outcome]] = defaultdict(list)
    for o in pool:
        if o.unit == unit:
            by[_bucket(o.start, unit)].append(o)
    filled = sorted(by)
    if not filled:
        return [{"bucket": b, "lo": edges[b], "hi": edges[b + 1] if b + 1 < len(edges) else None, "p": p0, "n": 0}
                for b in range(len(edges))]
    raw, weights = [], []
    for b in filled:
        p, w = _shrunk(by[b], ref, p0, BUCKET_PRIOR)
        raw.append(p)
        weights.append(w + BUCKET_PRIOR)
    fitted = dict(zip(filled, _pava_decreasing(raw, weights), strict=True))
    out = []
    for b in range(len(edges)):
        if b in fitted:
            p = fitted[b]
        elif b < filled[0]:
            p = fitted[filled[0]]
        elif b > filled[-1]:
            p = fitted[filled[-1]]
        else:
            p = fitted[min(x for x in filled if x > b)]
        out.append({"bucket": b, "lo": edges[b], "hi": edges[b + 1] if b + 1 < len(edges) else None, "p": p,
                    "n": len(by.get(b, []))})
    return out


@dataclass
class Prediction:
    p: float
    p_base: float
    p_niche: float
    niche_n: int
    niche_neff: float
    season: str


def predict(pool: list[Outcome], tags: Tags | None, start: float | None, unit: str, ref: float,
            phase_ts: float | None = None) -> Prediction:
    """Модель: шанс, что лот наберёт хотя бы одну ставку. Использует только то, что лежит в pool."""
    p0 = _base_rate(pool, ref)
    group = season_group(tags) if tags else None
    phase_now = season_phase(group, phase_ts or ref)
    matches = [o for o in pool if tags and _similar(o, tags)] if tags and not tags.is_empty() else []
    if matches:
        p_n, _w_n, season = _niche_rate(pool, matches, ref, p0, phase_now, group)
    else:
        p_n, season = p0, ("после сезона данных нет" if group and phase_now == "post" else "")
    p = p_n
    if start is not None:
        rates = bucket_rates(pool, unit, ref, p0)
        p_b = rates[_bucket(start, unit)]["p"]
        p = _expit(_logit(p_b) + _logit(p_n) - _logit(p0))
    return Prediction(min(max(p, 0.02), 0.98), p0, p_n, len(matches), n_eff(matches, ref), season)


def _cutoff(outcomes: list[Outcome]) -> float | None:
    return max((o.known_ts for o in outcomes), default=None)


def _rate_ev(obs: list[Outcome], population: str, unit: str | None, source="market_da", data_as_of=None,
             kind="observed") -> evidence.Evidence:
    k = sum(1 for o in obs if o.sold)
    return evidence.span([o.ts for o in obs], source, population, unit, kind, n=len(obs),
                         interval=evidence.wilson(k, len(obs)), measure="rate", value=k / len(obs) if obs else None,
                         data_as_of=data_as_of)


# --- Ниши ---

def _keys(t: Tags, group: str | None) -> list[str]:
    keys = [*(f"вид:{s}" for s in t.species), *(f"стиль:{s}" for s in t.styles), *(f"тема:{s}" for s in t.themes)]
    keys += [f"вид:{s}+стиль:{st}" for s in t.species for st in t.styles]
    if group:
        keys.append(f"сезон:{group}")
    return keys


@dataclass
class NicheReport:
    tier: Tier
    tier_fallback: bool
    p0: float
    n_lots: int
    neff: float
    cutoff_ts: float | None
    niches: list[dict] = field(default_factory=list)
    phases: dict = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    refusal: str | None = None
    coverage: tuple[int, int] = (0, 0)
    tested: int = 0
    threshold: float = evidence.COMPARE_P
    base_ev: evidence.Evidence | None = None
    sold: int = 0
    skipped_small: int = 0


def fav_snapshots(conn, rows) -> dict[str, dict]:
    """Фавы/просмотры: первый снимок, сделанный, когда лоту было 3–10 дней (иначе возраст сбивает сравнение)."""
    pub = {r["id"]: (r["published_ts"] or r["first_seen_ts"]) for r in rows}
    ids = list(pub)
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        try:
            snaps = conn.execute(f"SELECT * FROM deviation_stats WHERE deviation_id IN ({','.join('?' * len(chunk))}) "
                                 "ORDER BY ts", chunk).fetchall()
        except Exception:  # noqa: BLE001
            return {}
        for s in snaps:
            did = s["deviation_id"]
            age = (s["ts"] - (pub.get(did) or s["ts"])) / DAY
            if did not in out and 3 <= age <= 10:
                out[did] = dict(s)
    return out


def _supply_rows(conn, now: float):
    """Предложение: чужие торговые лоты из папок (не из поиска), которые отслеживаются ≥ SUPPLY_DAYS."""
    types = ",".join(f"'{t}'" for t in TRADING_TYPES)
    market = conn.execute(
        f"SELECT * FROM items WHERE source = 'deviantart' AND own = 0 AND lot_type IN ({types}) "
        f"AND COALESCE(published_ts, first_seen_ts) >= ?", (now - HISTORY_DAYS * DAY,)).fetchall()
    folders = {r["p"]: r["t"] for r in conn.execute(
        "SELECT COALESCE(place, '') AS p, MIN(first_seen_ts) AS t FROM items WHERE source = 'deviantart' "
        "GROUP BY COALESCE(place, '')").fetchall()}
    m_ref = max((r["published_ts"] or r["first_seen_ts"] for r in market), default=now)
    recent = [r for r in market if (r["published_ts"] or r["first_seen_ts"]) >= m_ref - SUPPLY_DAYS * DAY
              and not is_search(r["place"]) and folders.get(r["place"] or "", m_ref) <= m_ref - SUPPLY_DAYS * DAY]
    return recent, m_ref


def niches(conn, now: float | None = None, min_lots: int = evidence.MIN_SIDE) -> NicheReport:
    """Ниши (вид, стиль, тема, вид+стиль, сезон): доля аукционов со ставкой против остального рынка."""
    now = now or time.time()
    outcomes = load_outcomes(conn, now)
    tier = her_tier(conn, now)
    tiers = SellerTiers(conn)
    pool, fallback, note = _tier_pool(market_pool(outcomes), tiers, tier)
    ref = _cutoff(pool) or now
    k0 = sum(1 for o in pool if o.sold)
    report = NicheReport(tier, fallback, k0 / len(pool) if pool else 0.0, len(pool), n_eff(pool, ref), _cutoff(pool))
    report.sold = k0
    report.phases = {g: season_phase(g, now) for g in SEASONS}
    report.coverage = outcome_coverage(conn, now)
    report.refusal = coverage_refusal(*report.coverage)
    if note:
        report.caveats.append(note)
    population = _population(None, tier, fallback)
    report.base_ev = _rate_ev(pool, population, None, data_as_of=report.cutoff_ts) if pool else None
    if report.refusal or len(pool) < MIN_TRAIN:
        return report

    recent, m_ref = _supply_rows(conn, now)
    supply: dict[str, int] = defaultdict(int)
    recent_keys: dict[str, list[str]] = {}
    for r in recent:
        t = analytics_tags(r)
        recent_keys[r["id"]] = _keys(t, season_group(t))
        for sk in recent_keys[r["id"]]:
            supply[sk] += 1
    stats = fav_snapshots(conn, recent)
    supply_med = statistics.median(sorted(supply.values())) if supply else 0
    by_key: dict[str, list[Outcome]] = defaultdict(list)
    for o in pool:
        for bk in _keys(o.tags, o.group):
            by_key[bk].append(o)

    def sample(obs: list[Outcome]) -> tuple[list[Outcome], str, str | None, str]:
        groups = [o.group for o in obs if o.group]
        group = max(set(groups), key=groups.count) if groups and len(groups) * 2 >= len(obs) else None
        phase_now = season_phase(group, now)
        if not group or phase_now == "off":
            return obs, "", group, phase_now
        same = _season_split(obs, phase_now)
        if len(same) >= min_lots:
            word = "после сезона" if phase_now == "post" else "сезон идёт"
            return same, f"{word} — по {len(same)} лотам этого времени", group, phase_now
        return obs, ("после сезона данных нет" if phase_now == "post" else
                     f"сезон идёт, лотов этого сезона мало ({len(same)})"), group, phase_now

    candidates = []
    for key in sorted(by_key):
        obs, season, group, phase = sample(by_key[key])
        ids = {o.id for o in obs}
        rest = [o for o in pool if o.id not in ids]
        if len(obs) < min_lots or len(rest) < min_lots:
            report.skipped_small += 1
            continue
        candidates.append((key, obs, rest, season, group, phase, by_key[key]))
    report.tested = len(candidates)
    report.threshold = evidence.family_threshold(len(candidates))
    for key, obs, rest, season, group, phase, all_obs in candidates:
        k, n = sum(1 for o in obs if o.sold), len(obs)
        kr, nr = sum(1 for o in rest if o.sold), len(rest)
        p_better = evidence.p_greater(k, n, kr, nr)
        verdict = "neutral"
        if p_better >= report.threshold:
            verdict = "sells"
        elif 1 - p_better >= report.threshold and supply.get(key, 0) >= max(1, supply_med):
            verdict = "saturated"
        ev = _rate_ev(obs, population, None, data_as_of=report.cutoff_ts)
        recent_auc = [rid for rid, ks in recent_keys.items() if key in ks]
        favs = views = 0
        for item_id in recent_auc:
            if item_id in stats:
                favs += stats[item_id].get("favourites") or 0
                views += stats[item_id].get("views") or 0
        competition = _competition(conn, recent_auc)
        sold = [o.ratio for o in obs if o.ratio]
        report.niches.append({
            "key": key, "lots": n, "sold": k, "neff": float(n), "confidence": evidence.level(ev), "ev": ev,
            "p_sold": k / n, "p_all": sum(1 for o in all_obs if o.sold) / len(all_obs),
            "interval": ev.interval, "rest": (kr, nr), "p_better": p_better,
            "season": season, "group": group, "phase": phase, "supply": supply.get(key, 0),
            "competition": competition[0], "bidders_recent": competition[1],
            "avg_bids": statistics.mean(o.n_bids for o in obs),
            "ratio": statistics.median(sold) if sold else None, "ratio_n": len(sold),
            "fav_per_view": favs / views if views else None, "verdict": verdict})
    report.niches.sort(key=lambda d: (-d["p_better"], d["supply"], d["key"]))
    return report


def _competition(conn, ids: list[str]) -> tuple[float | None, int]:
    """(лотов на одного участника торгов, сколько разных участников) — по спискам ставивших, если они есть."""
    if not ids:
        return None, 0
    try:
        rows = conn.execute(f"SELECT deviation_id, extra FROM auctions WHERE deviation_id IN ({','.join('?' * len(ids))})",
                            ids).fetchall()
    except Exception:  # noqa: BLE001
        return None, 0
    with_list = [r for r in rows if isinstance(_extra(r).get("bidders"), list)]
    if not with_list:
        return None, 0
    names: set[str] = set()
    for r in with_list:
        names.update(_bidder_names(_extra(r)))
    return (len(with_list) / len(names) if names else None), len(names)


# --- Цена ---

@dataclass
class PricePlan:
    unit: str
    tier: Tier
    tier_fallback: bool
    pool_n: int
    niche_n: int
    p_niche: float
    p_base: float
    candidates: list[dict] = field(default_factory=list)
    best: dict | None = None
    ab: dict = field(default_factory=dict)
    confidence: str = "нет вывода"
    season: str = ""
    caveats: list[str] = field(default_factory=list)
    cutoff_ts: float | None = None
    label: str = ""
    population: str = ""
    refusal: str | None = None
    skill: dict = field(default_factory=dict)
    small: list[dict] = field(default_factory=list)
    niche_pool: bool = False
    niche_ev: evidence.Evidence | None = None
    niche_level: str = "нет вывода"


def _ratio_ci(obs: list[Outcome]) -> tuple[float, float, float | None, int]:
    """(нижняя, верхняя граница, медиана, n) роста цены у лотов со ставкой, без взятых автобаев."""
    ratios = [o.ratio for o in obs if o.ratio]
    if not ratios:
        return 1.0, 1.0, None, 0
    lo, hi = evidence.median_ci(ratios)
    return max(1.0, lo), max(1.0, hi), statistics.median(ratios), len(ratios)


def price_plan(conn, *, species=(), styles=(), themes=(), keywords=(), unit: str = "usd",
               now: float | None = None) -> PricePlan:
    """Какой старт аукциона даёт больше денег с гарантией по нижней границе: шанс ставки × старт × рост."""
    now = now or time.time()
    text = " ".join([*species, *styles, *themes, *keywords])
    tags = taxonomy.tag_text(text) if text.strip() else Tags()
    outcomes = load_outcomes(conn, now)
    tier = her_tier(conn, now)
    pool, fallback, note = _tier_pool(market_pool(outcomes, unit), SellerTiers(conn), tier)
    ref = _cutoff(pool) or now
    base = predict(pool, tags, None, unit, ref, now)
    plan = PricePlan(unit, tier, fallback, len(pool), base.niche_n, base.p_niche, base.p_base,
                     season=base.season, cutoff_ts=_cutoff(pool), label=" ".join(tags.all()) or "любые адопты",
                     population=_population(unit, tier, fallback))
    if note and pool:
        plan.caveats.append(note)
    plan.refusal = coverage_refusal(*outcome_coverage(conn, now))
    if plan.refusal:
        return plan
    if len(pool) < MIN_PLAN_POOL:
        plan.caveats.append(f"завершённых аукционов в {'поинтах' if unit == 'points' else 'долларах'}: "
                            f"{len(pool)}, нужно хотя бы {MIN_PLAN_POOL}")
        return plan
    # Кандидаты считаем по лотам ниши, если их хватает, иначе — по рынку в целом.
    niche = [o for o in pool if _similar(o, tags)] if not tags.is_empty() else []
    data = niche if len(niche) >= MIN_PLAN_POOL else pool
    plan.niche_pool = data is niche
    if tags.all() and not plan.niche_pool:
        plan.caveats.append(f"похожих лотов {len(niche)} — старт по рынку в целом, не по нише")
    cal = calibration(conn, now, outcomes=outcomes)
    plan.skill = {"n": cal.get("n", 0), "skill": cal.get("skill")}
    model_ok = cal.get("skill") is not None and cal["skill"] > 0 and cal.get("n", 0) >= MIN_SKILL_N
    edges = _edges(unit)
    for b, lo in enumerate(edges):
        hi = edges[b + 1] if b + 1 < len(edges) else None
        in_b = [o for o in data if _bucket(o.start, unit) == b]
        if not in_b:
            continue
        start = round_price(statistics.median(o.start for o in in_b), unit)
        k, n = sum(1 for o in in_b if o.sold), len(in_b)
        w_lo, w_hi = evidence.wilson(k, n)
        r_lo, r_hi, r_med, r_n = _ratio_ci(in_b)
        pr = predict(pool, tags, start, unit, ref, now)
        ratio = r_med or 1.0
        ev = evidence.span([o.ts for o in in_b], "market_da", plan.population, unit, "observed", n=n,
                           interval=(w_lo, w_hi), measure="rate", value=k / n, data_as_of=plan.cutoff_ts)
        lvl: str = evidence.level(ev)
        if not model_ok:
            lvl = evidence.cap(lvl, "низкая")
        c = {"start": start, "lo": lo, "hi": hi, "n": n, "k": k, "p_obs": k / n, "ci": (w_lo, w_hi),
             "p_sold": pr.p, "ratio": ratio, "ratio_ci": (r_lo, r_hi), "ratio_n": r_n,
             "expected": pr.p * start * ratio, "ev_lo": w_lo * start * r_lo, "ev_hi": w_hi * start * r_hi,
             "level": lvl, "ev": ev, "usable": n >= MIN_START_LOTS and evidence.level(ev) != "нет вывода"}
        plan.candidates.append(c)
        if n < MIN_START_LOTS:
            plan.small.append(c)
    usable = [c for c in plan.candidates if c["usable"]]
    if usable:
        plan.best = max(usable, key=lambda c: (round(c["ev_lo"], 6), c["p_obs"], -c["start"]))
        plan.confidence = plan.best["level"]
    else:
        plan.caveats.append(f"ни по одной цене старта нет {MIN_START_LOTS} лотов с достаточно узким интервалом")
    if not model_ok:
        plan.caveats.append("модель пока не точнее простого среднего (или проверок меньше "
                            f"{MIN_SKILL_N}: {cal.get('n', 0)}) — уверенность не выше «низкой»")
    if niche:
        k_n = sum(1 for o in niche if o.sold)
        plan.niche_ev = evidence.span([o.ts for o in niche], "model", plan.population, unit, "estimate", n=len(niche),
                                      interval=evidence.wilson(k_n, len(niche)), measure="rate",
                                      value=plan.p_niche, data_as_of=plan.cutoff_ts)
        plan.niche_level = evidence.level(plan.niche_ev)
        if not model_ok:
            plan.niche_level = evidence.cap(plan.niche_level, "низкая")
    with_ab = [o for o in data if o.ab]
    plan.ab = {"n": len(with_ab), "hits": sum(1 for o in with_ab if o.ab_hit), "heuristic": AB_HEURISTIC}
    return plan


# --- Проверка прогноза на прошлом ---

_CAL_CACHE: dict[tuple, dict] = {}


def calibration(conn, now: float | None = None, outcomes: list[Outcome] | None = None) -> dict:
    """Как прогноз «будет ли ставка» сработал бы на прошлых лотах рынка — зная только то, что было до них."""
    now = now or time.time()
    outcomes = market_pool(load_outcomes(conn, now) if outcomes is None else outcomes)
    tiers = SellerTiers(conn)
    key = (hash(tuple((o.id, o.sold, o.ts, o.known_ts, o.start, o.unit, o.author, tuple(o.tags.all()))
                      for o in outcomes)), hash(tuple(sorted(tiers.watchers.items()))))
    if key in _CAL_CACHE:
        return dict(_CAL_CACHE[key])
    targets = outcomes[-CALIBRATION_MAX:]
    preds: list[tuple[float, bool, float]] = []
    for o in targets:
        train = [x for x in outcomes if x.known_ts < o.ts and x.id != o.id and x.unit == o.unit]
        if len(train) < MIN_TRAIN:
            continue
        tier = tiers.tier(o.author)
        same = [x for x in train if tier and tiers.tier(x.author) == tier]
        pool = same if len(same) >= MIN_TIER_POOL else train
        pr = predict(pool, o.tags, o.start, o.unit, o.ts, o.ts)
        preds.append((pr.p, o.sold, pr.p_base))
    result: dict = {"n": len(preds), "lots": len(outcomes), "needed": max(0, MIN_CALIBRATION - len(preds)),
                    "cutoff_ts": _cutoff(outcomes), "buckets": []}
    if preds:
        result["brier"] = statistics.mean((p - y) ** 2 for p, y, _ in preds)
        result["brier_base"] = statistics.mean((b - y) ** 2 for _, y, b in preds)
        result["skill"] = 1 - result["brier"] / result["brier_base"] if result["brier_base"] else 0.0
        result["hit_rate"] = statistics.mean(1.0 if (p >= 0.5) == y else 0.0 for p, y, _ in preds)
        result["sold_rate"] = statistics.mean(1.0 if y else 0.0 for _, y, _ in preds)
        for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
            inside = [(p, y) for p, y, _ in preds if lo <= p < lo + 0.2 or (lo == 0.8 and p >= 0.8)]
            if inside:
                result["buckets"].append({"lo": lo, "hi": lo + 0.2, "n": len(inside),
                                          "predicted": statistics.mean(p for p, _ in inside),
                                          "actual": statistics.mean(1.0 if y else 0.0 for _, y in inside)})
    if len(_CAL_CACHE) > 32:
        _CAL_CACHE.clear()
    _CAL_CACHE[key] = dict(result)
    return result


# --- Тексты ---

def _basis_line(n_lots: int, cutoff: float | None, now: float | None = None) -> str:
    line = f"Опора: {n_lots} завершённых аукционов рынка, данные по {_date(cutoff)}."
    if cutoff and (now or time.time()) - cutoff > evidence.STALE_DAYS * DAY:
        line += f" Внимание, {evidence.stale(evidence.Evidence(0, None, 0, 0, cutoff, 'market_da', '', None, 'observed'), now)}."
    return line


def _niche_name(key: str) -> str:
    kind, _, rest = key.partition(":")
    if kind == "сезон":
        return SEASON_LABEL.get(rest, rest)
    return key.replace("вид:", "").replace("стиль:", "").replace("тема:", "")


def _niche_row(d: dict) -> str:
    lo, hi = d["interval"]
    kr, nr = d["rest"]
    bits = [f"ставку получили {d['sold']} из {d['lots']} ({_pct(d['p_sold'])}, 80%: {evidence.rate_interval(lo, hi)}), "
            f"у остальных {kr} из {nr} ({_pct(kr / nr)}); P(чаще остальных) = {d['p_better']:.2f}"]
    if d["ratio"] and d["ratio_n"] >= 3:
        bits.append(f"со ставками итог/старт ×{d['ratio']:.1f} (медиана по {d['ratio_n']}, без автобаев)")
    bits.append(f"на рынке за {SUPPLY_DAYS} дн: {d['supply']}")
    if d["competition"] is not None:
        bits.append(f"{d['competition']:.1f} лота на одного участника торгов ({d['bidders_recent']} разных)")
    if d["fav_per_view"] is not None:
        bits.append(f"фавы/просмотры на 3–10-й день {d['fav_per_view'] * 100:.1f}%")
    if d["season"]:
        bits.append(d["season"])
    return f"- {_niche_name(d['key'])}: " + ", ".join(bits) + " " + evidence.tag(d["ev"])


def render_niches(rep: NicheReport, limit: int = 8, now: float | None = None) -> str:
    if rep.refusal:
        return rep.refusal + "\n\n" + GENTLE
    if rep.n_lots < MIN_TRAIN:
        return (evidence.refuse("доля аукционов со ставкой по нишам", f"{rep.n_lots} завершённых аукционов рынка",
                                MIN_TRAIN)
                + " Помогает добавить 2–3 крупные группы адоптов «только для рынка» и подключить DeviantArt — "
                  "тогда помощник сам дочитывает ставки.\n\n" + GENTLE)
    if not rep.niches:
        return (f"Вывода нет: сравнить ниши не с чем — ни у одной ниши нет хотя бы {evidence.MIN_SIDE} завершённых "
                f"аукционов внутри и {evidence.MIN_SIDE} вне её (всего завершённых аукционов рынка: {rep.n_lots}). "
                "Помогает добавить 2–3 крупные группы адоптов разных видов «только для рынка».\n\n" + GENTLE)
    who = f"продавцов уровня «{rep.tier.label}»" if not rep.tier_fallback else "всех продавцов"
    lines = [f"Ниши на рынке DA: где аукционы {who} получают ставку чаще остальных",
             f"В среднем ставку получили {rep.sold} из {rep.n_lots} ({_pct(rep.p0)}) "
             + (evidence.tag(rep.base_ev) if rep.base_ev else "")]
    sells = [d for d in rep.niches if d["verdict"] == "sells"][:limit]
    cold = [d for d in rep.niches if d["verdict"] == "saturated"][:limit]
    lines.append("")
    rule = (f"проверено ниш с ≥{evidence.MIN_SIDE} лотами: {rep.tested}, порог P ≥ {rep.threshold:.3f} "
            "(строже при многих проверках)")
    if sells:
        lines.append(f"Чаще остальных получают ставку ({rule}):")
        lines += [_niche_row(d) for d in sells]
    else:
        lines.append(f"Ниш, где ставку получают заметно чаще остальных, по данным не видно ({rule}).")
    if cold:
        lines += ["", "Реже остальных получают ставку при большом предложении:"] + [_niche_row(d) for d in cold]
    neutral = [d for d in rep.niches if d["verdict"] == "neutral"][:3]
    if neutral and not sells:
        lines += ["", "Больше всего данных (разница с рынком не доказана):"] + [_niche_row(d) for d in neutral]
    # Оговорки и «на чём основано» — одним последним абзацем: длинный ответ режется, а этот абзац остаётся.
    lines.append("")
    post = [SEASON_LABEL[g] for g, ph in sorted(rep.phases.items()) if ph == "post"]
    if post:
        lines.append(f"Сейчас «после сезона»: {', '.join(post)} — поправка только по данным этого времени, "
                     "без них ниша оценивается как обычно.")
    lines += [f"Оговорка: {c}." for c in rep.caveats]
    if not rep.tier_fallback:
        lines.append(TIER_NOTE)
    lines += [_basis_line(rep.n_lots, rep.cutoff_ts, now), GENTLE]
    return "\n".join(lines)


def render_price_plan(plan: PricePlan, now: float | None = None) -> str:
    head = (f"Старт аукциона для «{plan.label}» ({'поинты' if plan.unit == 'points' else 'доллары'}). "
            f"Сравниваю: {plan.population or _population(plan.unit, plan.tier, plan.tier_fallback)}.")
    if plan.refusal:
        return "\n".join([head, plan.refusal, GENTLE])
    if not plan.best:
        lines = [head, evidence.refuse("старт по данным", "; ".join(plan.caveats) or "данных нет",
                                       f"{MIN_START_LOTS} лотов на одну цену старта")]
        if plan.small:
            lines.append("Старты, где лотов меньше " + str(MIN_START_LOTS) + " (вывода нет): "
                         + ", ".join(f"{_money(c['start'], plan.unit)} ({c['n']})" for c in plan.small) + ".")
        return "\n".join(lines + [GENTLE])
    b = plan.best
    lo, hi = b["ci"]
    lines = [head,
             f"Решение: старт {_money(b['start'], plan.unit)}. Наблюдалось: ставку получили {b['k']} из {b['n']} "
             f"лотов с таким стартом (80%: {evidence.rate_interval(lo, hi)}); по модели ~{_pct(b['p_sold'])}. "
             f"Ожидаемо с лота {_money(round(b['ev_lo'], 2), plan.unit)}–{_money(round(b['ev_hi'], 2), plan.unit)} "
             f"(по модели ~{_money(round(b['expected'], 2), plan.unit)}). Выбран старт с лучшей нижней границей. "
             + evidence.tag(b["ev"]).replace(f"уверенность {evidence.level(b['ev'])}", f"уверенность {plan.confidence}"),
             f"Старты, по которым есть ≥{MIN_START_LOTS} лотов" + (" ниши" if plan.niche_pool else "") + ":"]
    for c in plan.candidates:
        if c["n"] < MIN_START_LOTS:
            continue
        clo, chi = c["ci"]
        ratio = (f", итог/старт ×{c['ratio']:.1f} по {c['ratio_n']}" if c["ratio_n"] else ", ставок с ростом нет")
        lines.append(f"- {_money(c['start'], plan.unit)}: ставку получили {c['k']} из {c['n']} "
                     f"({evidence.rate_interval(clo, chi)}), по модели ~{_pct(c['p_sold'])}{ratio}; ожидаемо "
                     f"{_money(round(c['ev_lo'], 2), plan.unit)}–{_money(round(c['ev_hi'], 2), plan.unit)}, "
                     f"уверенность {c['level']}")
    if plan.small:
        lines.append(f"Вывода нет (лотов меньше {MIN_START_LOTS}): "
                     + ", ".join(f"{_money(c['start'], plan.unit)} — {c['n']}" for c in plan.small) + ".")
    ab = plan.ab
    if ab.get("n", 0) >= evidence.MIN_SIDE:
        lines.append(f"Автобай: в описаниях был у {ab['n']} лотов, взяли {ab['hits']} раз.")
    elif ab.get("n"):
        lines.append(f"Автобай: лотов с AB {ab['n']} — мало для вывода.")
    lines.append(f"Автобай — {ab.get('heuristic', AB_HEURISTIC)}.")
    if plan.season:
        lines.append(f"Сезон: {plan.season}.")
    lines += [f"Оговорка: {c}." for c in plan.caveats]
    lines += [_basis_line(plan.pool_n, plan.cutoff_ts, now), GENTLE]
    return "\n".join(lines)


def render_calibration(cal: dict) -> str:
    if cal["n"] < MIN_CALIBRATION:
        return (f"Проверить точность прогноза пока не на чем: проверено {cal['n']} прогнозов из нужных "
                f"{MIN_CALIBRATION} (ещё {cal['needed']} завершённых аукционов). Всего лотов с итогом: {cal['lots']}.")
    skill = cal.get("skill", 0.0)
    lines = [f"Проверка на прошлом: {cal['n']} аукционов рынка, для каждого прогноз строился только по тому, что было "
             "до его публикации.",
             f"Угадано, будет ли ставка: {_pct(cal['hit_rate'])} (ставку получили {_pct(cal['sold_rate'])} лотов).",
             f"Ошибка прогноза (Brier, меньше — лучше): {cal['brier']:.3f}; если просто брать средний уровень — "
             f"{cal['brier_base']:.3f}; skill {skill:+.2f} (n={cal['n']}). "
             + ("Прогноз по нишам и цене точнее среднего." if skill > 0 and cal["n"] >= MIN_SKILL_N else
                f"Прогноз точнее среднего, но проверок меньше {MIN_SKILL_N} — уверенность не выше «низкой»."
                if skill > 0 else
                "Пока прогноз не точнее простого среднего — относись к нему как к ориентиру.")]
    for bk in cal["buckets"]:
        lines.append(f"- когда прогноз говорил ~{_pct(bk['predicted'])}, ставку получили {_pct(bk['actual'])} "
                     f"(по {bk['n']} лотам)")
    lines.append(f"Данные по {_date(cal.get('cutoff_ts'))}.")
    return "\n".join(lines)
