"""Прогноз по рынку адоптов на основе того, что накопил мониторинг DeviantArt.

Что есть в данных: стартовые цены (SB) аукционов и фикс-цены, тип лота, теги,
когда лот появился, когда последний раз встречался в ленте (updated_ts) и когда
впервые стал закрытым (closed_ts). Ставки берутся из таблицы auctions (разбор
комментариев, bids.py) — только для лотов, чьи комментарии читали; разбор может
ошибаться. «Закрыт» не всегда значит «продан».
Поэтому прогноз — это ориентир по похожим лотам, а не обещание.
"""

import json
import re
import statistics
import time
from dataclasses import dataclass, field

from .bids import money
from .classify import TAG_TYPES, keyword_hits

DAY = 86400
# Ценовые группы: в каждой цены сравнимы между собой.
GROUPS = [
    ("auction", "аукционы (SB)", {"auction", "bta"}),
    ("fixed", "фикс-цены", {"flatsale", "adopt"}),
    ("raffle", "лотереи (цена билета)", {"raffle"}),
    ("ota", "OTA (суммы в постах — ориентиры, не цены продажи)", {"ota"}),
]
GROUP_OF = {t: key for key, _, types in GROUPS for t in types}
GROUP_LABEL = {key: label for key, label, _ in GROUPS}
MARKET_LABEL = {"auction": "аукционов", "fixed": "фикс-цен", "raffle": "лотерей", "ota": "OTA-лотов"}
# Для совместимости: какие типы считаются аукционом / фикс-ценой.
AUCTION_TYPES = GROUPS[0][2]
FIXED_TYPES = GROUPS[1][2]

MIN_SIMILAR = 5            # меньше похожих — показываем их поштучно и берём весь рынок типа
HORIZON_DAYS = 3           # «закрылся в первые N дней» — только для прослеженных N дней
UNITS = ("usd", "points")

GENTLE = (
    "Как говорить с ней: тепло и бережно, но честно. Не меняй и не округляй цифры, обязательно назови "
    "уверенность и скажи, если выборка расширена или данных мало. Хвали только то, что она сама рассказала "
    "о работе — картинку ты не видишь. Давай диапазоны, а не приговоры, без слов «плохо» и «не продастся». "
    "Если её цена заметно выше диапазона или похожие лоты редко закрываются, скажи это прямо, но мягко, и "
    "предложи варианты (чуть ниже SB, автобай, другой формат). Напомни, что это ориентир по похожим лотам, "
    "а не оценка её как художницы."
)

# Теги, которые есть почти у каждого адопта и ничего не говорят о самой работе.
GENERIC_WORDS = {
    "oc", "ocs", "art", "arts", "artwork", "design", "designs", "character", "characters", "original",
    "originalcharacter", "originalcharacters", "digitalart", "digital", "digitalpainting", "painting",
    "drawing", "illustration", "open", "closed", "sale", "forsale", "cheap", "cute", "anime", "sb", "ab",
    "usd", "pts", "points", "dapoints", "money", "paypal", "deviantart", "da", "new", "available",
}
GENERIC_RE = re.compile(r"adopt|auction|raffle|flatsale|commission|^ych|^ota|ota$|^bta|bta$")
TITLE_STOP = {"with", "from", "this", "that", "your", "for", "and", "the", "open", "closed", "sold"}


def tags(row) -> list[str]:
    try:
        return json.loads(row["extra"] or "{}").get("tags", [])
    except (json.JSONDecodeError, TypeError):
        return []


_tags = tags  # старое имя


def _norm(word: str) -> str:
    return re.sub(r"[\s_-]", "", word.lower())


def is_generic(word: str) -> bool:
    w = _norm(word)
    if not w or w in GENERIC_WORDS or GENERIC_RE.search(w):
        return True
    return any(re.search(pattern, w) for _, pattern in TAG_TYPES)


def keywords_from_tags(tag_list: list[str], title: str = "", limit: int = 6) -> list[str]:
    """Слова для поиска похожих: сначала выкидываем общие теги, потом берём первые."""
    words: list[str] = []
    for t in tag_list:
        if not is_generic(t) and _norm(t) not in {_norm(w) for w in words}:
            words.append(t.strip())
    if len(words) < 2:
        for w in re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", title):
            if w.lower() not in TITLE_STOP and not is_generic(w) and _norm(w) not in {_norm(x) for x in words}:
                words.append(w)
    return words[:limit]


@dataclass
class PriceStats:
    n: int = 0
    low: float | None = None      # 25-й перцентиль
    median: float | None = None
    high: float | None = None     # 75-й перцентиль
    values: list[float] = field(default_factory=list, repr=False)

    def position(self, price: float) -> float | None:
        """Доля лотов этой группы, которые дешевле её цены (0..1)."""
        if not self.values:
            return None
        return sum(v < price for v in self.values) / len(self.values)


def price_stats(values: list[float | None]) -> PriceStats:
    vals = sorted(v for v in values if v is not None and v > 0)
    if not vals:
        return PriceStats()
    if len(vals) == 1:
        return PriceStats(1, vals[0], vals[0], vals[0], vals)
    q = statistics.quantiles(vals, n=4, method="inclusive")
    return PriceStats(len(vals), q[0], statistics.median(vals), q[2], vals)


@dataclass
class Position:
    group: str        # ключ группы
    unit: str         # usd | points
    share: float      # доля лотов дешевле её цены
    n: int


@dataclass
class BidStats:
    """Ставки по аукционам из комментариев: финальные (закрытые) и текущие (открытые)."""
    final: dict[str, PriceStats] = field(default_factory=dict)      # unit -> максимальные ставки закрытых
    final_sb: dict[str, PriceStats] = field(default_factory=dict)   # unit -> их SB
    ratio: dict[str, float | None] = field(default_factory=dict)    # unit -> медиана «ставка / SB»
    ratio_n: dict[str, int] = field(default_factory=dict)
    current: dict[str, PriceStats] = field(default_factory=dict)    # unit -> текущие ставки открытых
    no_bids_closed: int = 0                                          # закрылись без единой ставки

    @property
    def n(self) -> int:
        return sum(s.n for s in self.final.values())


@dataclass
class Forecast:
    keywords: list[str]
    ignored_keywords: list[str]   # слишком общие, не участвовали
    widened: bool                 # похожих мало — статистика по всему рынку типа
    comparables: int              # лотов, по которым считается статистика
    hits: list                    # строки, найденные по словам (даже если их мало)
    market_scope: str             # «аукционов», «фикс-цен», «всех типов»
    stats: dict[str, dict[str, PriceStats]]
    open_now: int
    unknown_status: int
    stale: int
    closed_share: float | None
    closed_share_n: int
    closed_within_horizon: float | None
    horizon_n: int
    other_currency: int
    confidence: str
    positions: list[Position] = field(default_factory=list)
    bids: BidStats = field(default_factory=BidStats)

    # Старые имена полей — для совместимости.
    @property
    def auction_usd(self) -> PriceStats:
        return self.stats["auction"]["usd"]

    @property
    def fixed_usd(self) -> PriceStats:
        return self.stats["fixed"]["usd"]

    def position_for(self, group: str, unit: str = "usd") -> float | None:
        return next((p.share for p in self.positions if p.group == group and p.unit == unit), None)


def _market_rows(conn, days: int, now: float):
    return conn.execute(
        "SELECT * FROM items WHERE source = 'deviantart' AND own = 0 AND lot_type NOT IN ('ych', 'commission', 'other') "
        "AND COALESCE(published_ts, first_seen_ts) >= ?", (now - days * DAY,)).fetchall()


def _start(r) -> float:
    return r["published_ts"] or r["first_seen_ts"]


def _matches(keywords: list[str], r) -> bool:
    # Только название и теги: в описаниях слишком много рекламы и чужих ссылок.
    found = keyword_hits(keywords, r["title"], " ".join(tags(r)))
    need = 2 if len(keywords) >= 3 else 1
    return len(found) >= need


def comparables(conn, keywords: list[str], lot_type: str | None = None, days: int = 60,
                now: float | None = None) -> tuple[list, list, bool]:
    """(лоты для статистики, найденные по словам, расширено ли до всего рынка типа)."""
    now = now or time.time()
    rows = _market_rows(conn, days, now)
    if lot_type:
        group = GROUP_OF.get(lot_type)
        rows = [r for r in rows if GROUP_OF.get(r["lot_type"]) == group] if group else \
            [r for r in rows if r["lot_type"] == lot_type]
    if not keywords:
        return rows, [], False
    hits = [r for r in rows if _matches(keywords, r)]
    if len(hits) >= MIN_SIMILAR:
        return hits, hits, False
    return rows, hits, True


def _auction_rows(conn, rows) -> dict[str, dict]:
    """Строки таблицы auctions для этих лотов (если таблицы нет или пусто — {})."""
    ids = [r["id"] for r in rows if GROUP_OF.get(r["lot_type"]) == "auction"]
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        try:
            found = conn.execute(f"SELECT * FROM auctions WHERE deviation_id IN ({','.join('?' * len(chunk))})",
                                 chunk).fetchall()
        except Exception:  # noqa: BLE001 — старая база без таблицы auctions
            return {}
        out.update({a["deviation_id"]: dict(a) for a in found})
    return out


def bid_stats(conn, rows) -> BidStats:
    auctions = _auction_rows(conn, rows)
    result = BidStats()
    if not auctions:
        return result
    by_id = {r["id"]: r for r in rows}
    finals: dict[str, list[float]] = {u: [] for u in UNITS}
    sbs: dict[str, list[float]] = {u: [] for u in UNITS}
    ratios: dict[str, list[float]] = {u: [] for u in UNITS}
    current: dict[str, list[float]] = {u: [] for u in UNITS}
    for dev_id, a in auctions.items():
        r = by_id.get(dev_id)
        unit = a.get("currency")
        if r is None or unit not in UNITS:
            continue
        ended = bool(a.get("ended")) or r["status"] == "closed"
        if not a.get("n_bids") or not a.get("top_amount"):
            result.no_bids_closed += ended
            continue
        top = float(a["top_amount"])
        if ended:
            finals[unit].append(top)
            sb = r[f"price_{unit}"]
            if sb:
                sbs[unit].append(sb)
                ratios[unit].append(top / sb)
        else:
            current[unit].append(top)
    for u in UNITS:
        result.final[u] = price_stats(list(finals[u]))
        result.final_sb[u] = price_stats(list(sbs[u]))
        result.ratio[u] = statistics.median(ratios[u]) if ratios[u] else None
        result.ratio_n[u] = len(ratios[u])
        result.current[u] = price_stats(list(current[u]))
    return result


def _confidence(n: int) -> str:
    return "низкая" if n < 5 else "средняя" if n < 15 else "нормальная"


def _bucket_size(stats: dict, lot_type: str | None, unit: str) -> int:
    if lot_type and GROUP_OF.get(lot_type):
        return stats[GROUP_OF[lot_type]][unit].n
    return max((s[unit].n for s in stats.values()), default=0)


def forecast(conn, keywords: list[str], lot_type: str | None = None, my_usd: float | None = None,
             my_points: float | None = None, days: int = 60, now: float | None = None) -> Forecast:
    now = now or time.time()
    given = [k for k in (keywords or []) if k and k.strip()]
    useful = [k for k in given if not is_generic(k)]
    ignored = [k for k in given if is_generic(k)]
    rows, hits, widened = comparables(conn, useful, lot_type, days, now)
    # Слова были, но все общие — это тоже «весь рынок», а не похожие.
    if given and not useful:
        widened = True

    stats = {key: {u: price_stats([r[f"price_{u}"] for r in rows if GROUP_OF.get(r["lot_type"]) == key])
                   for u in UNITS} for key, _, _ in GROUPS}

    # Закрылись ли: только лоты с понятным статусом. «Открытый» лот считаем,
    # только если видели его в ленте и после 3-го дня — иначе он просто выпал из RSS.
    matured = [r for r in rows if _start(r) <= now - HORIZON_DAYS * DAY]
    closed = [r for r in matured if r["status"] == "closed"]
    still_open = [r for r in matured if r["status"] == "open" and r["updated_ts"] >= _start(r) + HORIZON_DAYS * DAY]
    stale = [r for r in matured if r["status"] == "open" and r["updated_ts"] < _start(r) + HORIZON_DAYS * DAY]
    decided = len(closed) + len(still_open)

    # Скорость: доля закрывшихся в первые N дней — только среди лотов, которые
    # застали открытыми и проследили N дней (или до закрытия). Медиану не считаем:
    # медленные лоты ещё открыты, и она получалась бы слишком оптимистичной.
    followed = closed_fast = 0
    for r in matured:
        seen_open = r["first_seen_ts"] < (r["closed_ts"] or float("inf"))
        if not seen_open:
            continue
        if r["status"] == "closed" and r["closed_ts"]:
            followed += 1
            closed_fast += r["closed_ts"] - _start(r) <= HORIZON_DAYS * DAY
        elif r["status"] == "open" and r["updated_ts"] >= _start(r) + HORIZON_DAYS * DAY:
            followed += 1

    positions = []
    for unit, price in (("usd", my_usd), ("points", my_points)):
        if not price:
            continue
        groups = [GROUP_OF[lot_type]] if lot_type in GROUP_OF else [k for k, _, _ in GROUPS]
        for g in groups:
            s = stats[g][unit]
            share = s.position(price)
            if share is not None:
                positions.append(Position(g, unit, share, s.n))

    unit = "points" if my_points and not my_usd else "usd"
    level = _confidence(min(len(rows), _bucket_size(stats, lot_type, unit) or len(rows)))
    if widened:
        level = "низкая"

    return Forecast(
        keywords=useful,
        ignored_keywords=ignored,
        widened=widened,
        comparables=len(rows),
        hits=hits,
        market_scope=MARKET_LABEL.get(GROUP_OF.get(lot_type or "", ""), "всех типов"),
        stats=stats,
        open_now=sum(1 for r in rows if r["status"] == "open" and _start(r) >= now - 7 * DAY),
        unknown_status=sum(1 for r in rows if r["status"] not in ("open", "closed")),
        stale=len(stale),
        closed_share=len(closed) / decided if decided >= 3 else None,
        closed_share_n=decided,
        closed_within_horizon=closed_fast / followed if followed >= 3 else None,
        horizon_n=followed,
        other_currency=sum(1 for r in rows if r["price_usd"] is None and r["price_text"]
                           and ("€" in r["price_text"] or "£" in r["price_text"])),
        confidence=level,
        positions=positions,
        bids=bid_stats(conn, rows),
    )


def _fmt(v: float, unit: str) -> str:
    return f"${v:g}" if unit == "usd" else f"{v:g} pts"


def _row_price(r) -> str:
    if r["price_text"]:
        return r["price_text"]
    parts = [_fmt(r[f"price_{u}"], u) for u in UNITS if r[f"price_{u}"]]
    return " / ".join(parts) or "без цены"


def _range(s: PriceStats, unit: str) -> str | None:
    if not s.n:
        return None
    if s.n < 5:
        # На паре значений квартили врут: показываем сами цены.
        return f"{', '.join(_fmt(v, unit) for v in s.values)} (всего {s.n})"
    assert s.low is not None and s.high is not None and s.median is not None
    return f"обычно {_fmt(s.low, unit)}–{_fmt(s.high, unit)}, середина {_fmt(s.median, unit)} (n={s.n})"


def render(fc: Forecast, lot_type: str | None = None) -> str:
    lines = []
    if fc.ignored_keywords:
        lines.append(f"Слишком общие слова, не искала по ним: {', '.join(fc.ignored_keywords)}.")
    if fc.keywords and not fc.widened:
        lines.append(f"Похожих лотов за 60 дней (слова: {', '.join(fc.keywords)}): {fc.comparables}. "
                     f"Уверенность: {fc.confidence}.")
    else:
        if fc.keywords:
            hit_bits = [f"{r['title'][:40]} — {_row_price(r)}" for r in fc.hits[:5]]
            lines.append(f"По словам {', '.join(fc.keywords)} нашлось всего {len(fc.hits)} — для статистики мало"
                         + (": " + "; ".join(hit_bits) if hit_bits else "") + ".")
        lines.append(f"Ниже весь рынок {fc.market_scope} за 60 дней: {fc.comparables} лотов на рынке "
                     f"(это не «похожие»). Уверенность: {fc.confidence}.")

    for key, label, _ in GROUPS:
        if lot_type and GROUP_OF.get(lot_type) and GROUP_OF[lot_type] != key:
            continue
        for unit in UNITS:
            text = _range(fc.stats[key][unit], unit)
            if text:
                lines.append(f"{label[:1].upper() + label[1:]}, {'$' if unit == 'usd' else 'pts'}: {text}")
    if lot_type == "ota" and not any(fc.stats["ota"][u].n for u in UNITS):
        lines.append("Для OTA цены почти не пишут — это предложения, сравнить её цену не с чем.")

    where = "лотов на рынке" if fc.widened or not fc.keywords else "похожих"
    for p in fc.positions:
        lines.append(f"Её цена выше, чем у {p.share:.0%} {where} — {GROUP_LABEL[p.group]}, "
                     f"{'$' if p.unit == 'usd' else 'pts'}, по {p.n} ценам"
                     + ("; цен очень мало, это скорее ориентир" if p.n < 5 else ""))

    lines.append(f"Сейчас открыто ({where}, за неделю): {fc.open_now}")
    if fc.closed_share is not None:
        lines.append(f"Закрылись из тех, что висят 3+ дня: {fc.closed_share:.0%} (из {fc.closed_share_n})")
    if fc.closed_within_horizon is not None:
        lines.append(f"Закрылись в первые {HORIZON_DAYS} дня: {fc.closed_within_horizon:.0%} "
                     f"(из {fc.horizon_n}, которых проследили)")
    skipped = []
    if fc.unknown_status:
        skipped.append(f"{fc.unknown_status} без статуса в названии")
    if fc.stale:
        skipped.append(f"{fc.stale} выпали из ленты")
    if skipped:
        lines.append("Не учтены в «закрылись»: " + ", ".join(skipped) + ".")
    if fc.other_currency:
        lines.append(f"{fc.other_currency} лотов с ценой в €/£ — в статистику $ не включены.")
    lines += _bid_lines(fc.bids)
    bids_note = ("ставки взяты из комментариев только тех лотов, которые читали, и разбор может ошибаться"
                 if fc.bids.n or any(s.n for s in fc.bids.current.values())
                 else "финальных ставок пока не видно (комментарии ещё не читали)")
    lines += [
        "",
        f"Ограничения: SB — стартовая цена; {bids_note}; «закрыт» не всегда «продан»; "
        "данные только из папок и галерей, которые отслеживаются, и копятся с момента установки.",
        "",
        GENTLE,
    ]
    return "\n".join(lines)


def _bid_lines(b: BidStats) -> list[str]:
    lines = []
    for unit in UNITS:
        final, sb, ratio = b.final.get(unit), b.final_sb.get(unit), b.ratio.get(unit)
        if final and final.n:
            assert final.median is not None
            if sb and sb.n and ratio:
                assert sb.median is not None
                line = (f"По ставкам: аукционы с SB около {_fmt(sb.median, unit)} уходят примерно за "
                        f"{_fmt(final.median, unit)} (×{ratio:.1f}), по {final.n} {'лоту' if final.n == 1 else 'лотам'}")
            else:
                line = f"По ставкам: закрытые аукционы ушли примерно за {_fmt(final.median, unit)}, по {final.n} лотам"
            if final.n < 5:
                line += f" — цены: {', '.join(_fmt(v, unit) for v in final.values)}; лотов мало, это скорее ориентир"
            lines.append(line)
        current = b.current.get(unit)
        if current and current.n:
            assert current.median is not None
            lines.append(f"Сейчас на открытых аукционах ставки около {_fmt(current.median, unit)} "
                         f"(по {current.n}, ещё могут вырасти)")
    if b.no_bids_closed:
        lines.append(f"Закрылись без единой ставки: {b.no_bids_closed}")
    return lines


def _her_bids(conn, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    try:
        rows = conn.execute(f"SELECT * FROM auctions WHERE deviation_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    except Exception:  # noqa: BLE001 — старая база без таблицы auctions
        return {}
    return {a["deviation_id"]: dict(a) for a in rows}


def my_works(conn, days: int = 90, now: float | None = None) -> str:
    now = now or time.time()
    rows = conn.execute(
        "SELECT * FROM items WHERE source = 'deviantart' AND own = 1 AND lot_type != 'other' "
        "AND COALESCE(published_ts, first_seen_ts) >= ? "
        "ORDER BY COALESCE(published_ts, first_seen_ts) DESC", (now - days * DAY,)).fetchall()
    if not rows:
        return ("Её работ в базе пока нет. Нужен её ник на DeviantArt (update_settings my_da_username), "
                "после ближайшей проверки галерея подтянется.")
    lines = [f"Её работ за {days} дн: {len(rows)}"]
    her_bids = _her_bids(conn, [r["id"] for r in rows[:15]])
    for r in rows[:15]:
        status = {"closed": "закрыт", "open": "открыт"}.get(r["status"], "статус не указан")
        bits = [r["lot_type"] or "", r["price_text"] or "", status]
        if r["status"] == "closed" and r["closed_ts"] and r["closed_ts"] > r["first_seen_ts"]:
            bits.append(f"закрылся за {(r['closed_ts'] - _start(r)) / DAY:.1f} дн")
        a = her_bids.get(r["id"])
        if a and a.get("n_bids") and a.get("top_amount"):
            bits.append(f"ставок {a['n_bids']}, максимум {money(float(a['top_amount']), a['currency'] or 'usd')}"
                        + (f" у {a['winner']}" if a.get("winner") else ""))
        elif a:
            bits.append("ставок пока нет")
        lines.append(f"- {r['title']} — {' · '.join(b for b in bits if b)}")
    lines += ["", GENTLE]
    return "\n".join(lines)
