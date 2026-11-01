"""Одно правило для всех цифр аналитики: откуда число, на скольких лотах, за какой период и насколько ему верить.

Каждое число, которое она видит, идёт с меткой tag(ev): «(n=12, 01.09–31.10.2026, рынок DA, оценка,
уверенность низкая)». Если данных мало — refuse(...) вместо числа. Общие советы сообщества, которые не
вытекают из её данных, помечаются heuristic(...) и никогда не становятся строкой «Решение:».

Сравнения («лучше», «лучшее окно», «здесь получают ставку») делаем только при P(лучше) ≥ порога и хотя бы
MIN_SIDE лотах с каждой стороны. Если сравнений много (ниши, слоты времени), порог строже — иначе среди
десятка проверок что-нибудь «победит» случайно.

Все случайные вычисления (бутстрэп, Монте-Карло) идут с фиксированным зерном: один и тот же вопрос
на тех же данных даёт тот же ответ.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

DAY = 86400
MIN_SIDE = 8               # лотов с каждой стороны для любого сравнения
COMPARE_P = 0.9            # P(одно лучше другого), начиная с которой говорим «лучше»
FAMILY_ERROR = 0.1         # на семейство сравнений: ожидаемо не больше 0.1 ложной «победы»
STALE_DAYS = 3

Source = Literal["her_lots", "her_sales", "market_da", "market_da_search", "th_lf", "model", "rule_of_thumb",
                 "palette", "experiment", "market_th_stated", "x_manual", "crm"]
Kind = Literal["observed", "estimate", "heuristic"]
Level = Literal["нет вывода", "низкая", "средняя", "высокая"]
LEVELS: tuple[str, ...] = ("нет вывода", "низкая", "средняя", "высокая")

SOURCE_LABEL = {"her_lots": "её лоты", "her_sales": "её продажи", "market_da": "рынок DA",
                "market_da_search": "рынок DA, из поиска", "th_lf": "LF-треды Toyhouse", "model": "модель",
                "rule_of_thumb": "общая практика", "palette": "палитры лотов",
                "experiment": "её эксперимент", "market_th_stated": "заявленные цены TH",
                "x_manual": "X — ссылки, которые она принесла", "crm": "её покупатели и участники"}
KIND_LABEL = {"observed": "наблюдение", "estimate": "оценка", "heuristic": "общая практика"}


@dataclass(frozen=True)
class Evidence:
    n: int
    n_eff: float | None
    since_ts: float
    until_ts: float
    data_as_of: float
    source: Source
    population: str                 # «аукционы DA, $, все продавцы, без её лотов»
    unit: str | None                # usd | points | None — одна валюта на Evidence, никогда не смешиваем
    kind: Kind
    interval: tuple[float, float] | None = None   # 80%: Wilson для долей, бутстрэп для цен
    measure: Literal["rate", "price", "count"] = "rate"
    value: float | None = None      # точечная оценка (для цен — медиана)


def span(ts_values, source: Source, population: str, unit: str | None, kind: Kind, *,
         n: int | None = None, n_eff: float | None = None, interval=None, measure="rate", value=None,
         data_as_of: float | None = None) -> Evidence:
    """Evidence по списку моментов времени (публикации лотов): период = от первого до последнего."""
    ts = [t for t in ts_values if t]
    lo, hi = (min(ts), max(ts)) if ts else (0.0, 0.0)
    return Evidence(n=len(ts) if n is None else n, n_eff=n_eff, since_ts=lo, until_ts=hi,
                    data_as_of=data_as_of if data_as_of is not None else hi, source=source, population=population,
                    unit=unit, kind=kind, interval=interval, measure=measure, value=value)


# --- Статистика ---

def wilson(k: float, n: float, z: float = 1.28) -> tuple[float, float]:
    """80%-интервал Уилсона для доли k из n. Пустая выборка — (0, 1): ничего не известно."""
    if n <= 0:
        return 0.0, 1.0
    p = min(max(k / n, 0.0), 1.0)
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def median_ci(values, level: float = 0.8, B: int = 500, seed: int = 0) -> tuple[float, float]:
    """Интервал медианы бутстрэпом, детерминированно (random.Random(seed))."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        raise ValueError("median_ci: пустая выборка")
    if len(vals) == 1:
        return vals[0], vals[0]
    rng = random.Random(seed)
    n = len(vals)
    meds = sorted(statistics.median(rng.choice(vals) for _ in range(n)) for _ in range(B))
    tail = (1 - level) / 2
    return meds[int(tail * (B - 1))], meds[int(round((1 - tail) * (B - 1)))]


def p_greater(k1: int, n1: int, k0: int, n0: int, draws: int = 4000, seed: int = 0) -> float:
    """P(p1 > p0) при апостериорных Beta(1+k, 1+n−k), Монте-Карло с фиксированным зерном."""
    rng = random.Random(seed)
    a1, b1 = 1 + k1, 1 + max(0, n1 - k1)
    a0, b0 = 1 + k0, 1 + max(0, n0 - k0)
    wins = sum(1 for _ in range(draws) if rng.betavariate(a1, b1) > rng.betavariate(a0, b0))
    return wins / draws


def family_threshold(m: int, base: float = COMPARE_P) -> float:
    """Порог P(лучше), когда проверяем m сравнений сразу: ожидаемо ≤ FAMILY_ERROR ложных «побед»."""
    return max(base, 1 - FAMILY_ERROR / max(1, m))


def compare(k1: int, n1: int, k0: int, n0: int, threshold: float = COMPARE_P) -> str | None:
    """'better' / 'worse' / None. Сравнение возможно только при MIN_SIDE лотах с каждой стороны."""
    if n1 < MIN_SIDE or n0 < MIN_SIDE:
        return None
    p = p_greater(k1, n1, k0, n0)
    if p >= threshold:
        return "better"
    if 1 - p >= threshold:
        return "worse"
    return None


def level(ev: Evidence) -> Level:
    """нет вывода (<8) / низкая (<15) / средняя (<40) / высокая; на шаг ниже, если интервал широкий
    (шире 0.3 для доли или шире половины медианы для цены)."""
    base = ev.n_eff if ev.n_eff is not None else ev.n
    idx = 0 if base < 8 else 1 if base < 15 else 2 if base < 40 else 3
    if ev.interval and idx > 0:
        lo, hi = ev.interval
        width = hi - lo
        if ev.measure == "rate":
            wide = width > 0.3
        elif ev.measure == "price":
            center = ev.value if ev.value is not None else (lo + hi) / 2
            wide = center > 0 and width > 0.5 * center
        else:
            wide = False
        if wide:
            idx -= 1
    return LEVELS[idx]  # type: ignore[return-value]


def cap(lvl: str, ceiling: str) -> str:
    """Уверенность не выше ceiling."""
    return LEVELS[min(LEVELS.index(lvl), LEVELS.index(ceiling))]


def weakest(levels) -> str:
    items = [lv for lv in levels if lv in LEVELS]
    return min(items, key=LEVELS.index) if items else "нет вывода"


# --- Тексты ---

def _d(ts: float, year: bool) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d.%m.%Y" if year else "%d.%m")


def period(ev: Evidence) -> str:
    if not ev.since_ts and not ev.until_ts:
        return "период неизвестен"
    lo, hi = ev.since_ts or ev.until_ts, ev.until_ts or ev.since_ts
    same_year = datetime.fromtimestamp(lo, UTC).year == datetime.fromtimestamp(hi, UTC).year
    if _d(lo, True) == _d(hi, True):
        return _d(hi, True)
    return f"{_d(lo, not same_year)}–{_d(hi, True)}"


def tag(ev: Evidence) -> str:
    """«(n=12, 01.09–31.10.2026, рынок DA, оценка, уверенность низкая)»."""
    n = f"n={ev.n}"
    if ev.n_eff is not None and abs(ev.n_eff - ev.n) >= 1:
        n += f", эфф. {ev.n_eff:.0f}"
    bits = [n, period(ev), SOURCE_LABEL.get(ev.source, str(ev.source)), KIND_LABEL.get(ev.kind, str(ev.kind))]
    if ev.kind != "heuristic":
        bits.append(f"уверенность {level(ev)}")
    return "(" + ", ".join(bits) + ")"


def refuse(what: str, have, need) -> str:
    return f"Вывода нет: {what} — {have} из нужных {need}."


def stale(ev: Evidence, now: float | None = None, max_age_days: float = STALE_DAYS) -> str | None:
    now = now or time.time()
    if ev.data_as_of and now - ev.data_as_of > max_age_days * DAY:
        return f"данные старые: по {_d(ev.data_as_of, False)}"
    return None


def heuristic(text: str, source: str) -> str:
    return f"общая практика, не из твоих данных: {text} ({source})"


def pct(p: float) -> str:
    return f"{round(p * 100)}%"


def rate_interval(lo: float, hi: float) -> str:
    return f"{round(lo * 100)}–{round(hi * 100)}%"


# Источники, на которые ссылаемся в общих советах.
GUIDE_2021 = "гайд сообщества zeryuo, 2021: https://www.deviantart.com/zeryuo/journal/A-guide-to-adoptables-594803527"
DA_HELP_SUBMIT = "справка DeviantArt: https://www.deviantartsupport.com/kb/en/article/how-do-i-submit-art-4011138"
DA_SELL = "DeviantArt Sell: https://www.deviantart.com/team/journal/DeviantArt-Sell-1276020491"


# --- На чём стоит аналитика ---

HEALTH_SOURCES = (("deviantart", "лента DeviantArt"), ("toyhouse", "Toyhouse"), ("da_auctions", "ставки DA"),
                  ("enrich", "просмотры/фавы DA"), ("da_sampler", "поиск по рынку DA"))
try:  # контракт C1: место «поиск DA: …» у лотов, найденных только поиском
    from .db import SEARCH_PLACE_PREFIX
except ImportError:  # pragma: no cover
    SEARCH_PLACE_PREFIX = "поиск DA"


def _json(v) -> dict:
    try:
        d = json.loads(v or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "ни разу"
    days = (now - ts) / DAY
    if days < 1:
        return f"{max(0, int((now - ts) // 3600))} ч назад"
    return f"{days:.0f} дн назад ({_d(ts, False)})"


def render_health(conn, now: float | None = None) -> str:
    """Свежесть источников, итоги аукционов, точность прогноза, продажи по валютам, поиск против папок."""
    from . import forecast

    now = now or time.time()
    lines = ["На чём стоит аналитика"]
    lines.append("Свежесть данных (последний удачный проход):")
    for source, label in HEALTH_SOURCES:
        try:
            row = conn.execute("SELECT * FROM runs WHERE source = ?", (source,)).fetchone()
        except Exception:  # noqa: BLE001
            row = None
        ok = row["last_ok_ts"] if row else None
        err = row["last_error"] if row and row["last_error"] else None
        flag = " — данные старые" if ok and now - ok > STALE_DAYS * DAY else ""
        lines.append(f"- {label}: {_ago(ok, now)}{flag}" + (f"; последняя ошибка: {err[:80]}" if err else ""))
    from . import da_access

    lines += [f"- {line}" for line in da_access.health_lines(conn)]

    known, sampled = forecast.outcome_coverage(conn, now)
    ages = []
    try:
        rows = conn.execute(
            "SELECT a.extra AS a_extra, a.updated_ts AS a_updated, COALESCE(i.published_ts, i.first_seen_ts) AS t "
            "FROM auctions a JOIN items i ON i.id = a.deviation_id WHERE i.source = 'deviantart' AND i.own = 0 "
            "AND i.lot_type IN ('auction','bta') AND COALESCE(i.published_ts, i.first_seen_ts) <= ? "
            "AND COALESCE(i.place, '') NOT LIKE ?",
            (now - forecast.MATURE_DAYS * DAY, SEARCH_PLACE_PREFIX + "%")).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        age = _json(r["a_extra"]).get("read_age_days")
        if not isinstance(age, (int, float)):
            age = ((r["a_updated"] or 0) - (r["t"] or 0)) / DAY if r["a_updated"] and r["t"] else None
        if age is not None:
            ages.append(float(age))
    mature = sum(1 for a in ages if a >= forecast.MATURE_DAYS)
    lines.append("")
    lines.append(f"Аукционы рынка старше {forecast.MATURE_DAYS} дн, чьи ставки читали: {sampled}; итог известен у "
                 f"{known}" + (f" ({pct(known / sampled)})" if sampled else "")
                 + (f" — для прогнозов нужно ≥{pct(forecast.MIN_COVERAGE)}" if sampled and known / sampled
                    < forecast.MIN_COVERAGE else "") + ".")
    try:
        market = [o for o in forecast.load_outcomes(conn, now) if not o.own and not o.search]
    except Exception:  # noqa: BLE001
        market = []
    if market:
        back = sum(1 for o in market if o.backfill)
        lines.append(f"Из них с известным итогом {len(market)}, задним числом (история рынка) {back} "
                     f"({pct(back / len(market))})" + (f" — {forecast.BACKFILL_CAVEAT}." if back else "."))
    if ages:
        lines.append(f"Дочитаны в возрасте ≥{forecast.MATURE_DAYS} дн: {mature} из {len(ages)} "
                     f"({pct(mature / len(ages))}), медианный возраст чтения {statistics.median(ages):.1f} дн.")

    try:
        cal = forecast.calibration(conn, now)
    except Exception:  # noqa: BLE001
        cal = {"n": 0}
    if cal.get("n", 0) and "skill" in cal:
        verdict = ("точнее простого среднего" if cal["skill"] > 0 and cal["n"] >= forecast.MIN_SKILL_N
                   else "пока не точнее простого среднего" if cal["skill"] <= 0
                   else f"проверок мало (нужно ≥{forecast.MIN_SKILL_N})")
        lines.append(f"Точность прогноза «будет ли ставка»: Brier skill {cal['skill']:+.2f} (n={cal['n']}) — {verdict}.")
    else:
        lines.append(f"Точность прогноза: проверить пока не на чем (проверено {cal.get('n', 0)}).")

    try:
        srows = conn.execute("SELECT currency, COUNT(*) AS c, COUNT(amount) AS ca, SUM(amount) AS s FROM sales "
                             "WHERE status != 'cancelled' GROUP BY currency").fetchall()
    except Exception:  # noqa: BLE001
        srows = []
    if srows:
        from .sales import money

        bits = [f"{money(r['s'] or 0, r['currency'])} ({r['ca']})" for r in srows if r["currency"] and r["ca"]]
        no_cur = sum(r["c"] for r in srows if not r["currency"])
        no_amount = sum(r["c"] - r["ca"] for r in srows if r["currency"])
        # Не доход: здесь все записанные продажи за всё время, включая неоплаченные (доход — sales:income).
        lines.append("Её записанные продажи по валютам (все, включая неоплаченные, за всё время; доход — "
                     "sales:income): " + (", ".join(bits) or "—")
                     + (f"; без суммы: {no_amount}" if no_amount else "")
                     + (f"; без валюты: {no_cur} (в суммы не входят)" if no_cur else "") + ".")
    else:
        lines.append("Её продаж в базе пока нет.")

    try:
        prow = conn.execute(
            "SELECT SUM(CASE WHEN place LIKE ? THEN 1 ELSE 0 END) AS s, COUNT(*) AS c FROM items "
            "WHERE source = 'deviantart' AND own = 0", (SEARCH_PLACE_PREFIX + "%",)).fetchone()
        searched, total = int(prow["s"] or 0), int(prow["c"] or 0)
    except Exception:  # noqa: BLE001
        searched, total = 0, 0
    lines.append(f"Лоты рынка: {total}, из них найдены только поиском: {searched}, из папок и галерей: "
                 f"{total - searched}. Лоты из поиска идут в диапазоны цен, но не в доли «получили ставку».")
    try:
        th = conn.execute("SELECT COUNT(*) FROM items WHERE source = 'toyhouse' AND match_kind = 'lf' "
                          "AND COALESCE(activity_ts, published_ts, first_seen_ts) >= ?", (now - 14 * DAY,)).fetchone()[0]
        lines.append(f"LF-тредов Toyhouse за 14 дн: {th}.")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)
