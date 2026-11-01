"""Советник по продажам: что сделать, чтобы она заработала больше.

Каждый совет опирается на её продажи, ставки и наблюдаемый рынок, и каждая цифра идёт с меткой
evidence.tag: n, период, откуда данные и уверенность. Сравнения («лучше», «лучшее окно», «здесь чаще
получают ставку») — только при P(лучше) ≥ порога и хотя бы evidence.MIN_SIDE лотах с каждой стороны,
иначе совета нет. Общие практики сообщества помечены «общая практика, не из твоих данных» и никогда не
выдаются за выводы. Ничего не делается само: советник только предлагает, решает она.
"""

import json
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from . import digest, evidence, forecast, sales, taxonomy, th_match
from .config import Config
from .listing import buyer_gets, listing_facts, pays_any, row_facts

DAY = 86400
DEFAULT_TZ = "Europe/Moscow"
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_ON = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье"]
AUCTION_TYPES = ("auction", "bta")
FIXED_TYPES = ("flatsale", "adopt")
STALE_DAYS = 14              # общая практика, если своих данных о сроке продажи мало (см. time_to_close)
OPEN_SEEN_DAYS = 3           # фикс-цена «висит», только если открытой её видели в ленте за эти дни
MIN_PRICING_LOTS = 5         # правила про цену — минимум столько её завершённых лотов в одной валюте
MIN_NORMS = 30               # доли оформления по рынку — от стольких лотов
TAG_OK = re.compile(r"^[A-Za-z0-9_]{2,40}$")
DAYPARTS = (("ночь", "ночью", 0), ("утро", "утром", 6), ("день", "днём", 12), ("вечер", "вечером", 18))

# Полные правила пересказа — в get_settings (один раз за разговор); здесь короткая пометка.
GENTLE_ADVICE = "Цифры и оговорки — как есть; это предложения, решает она."
MI_AB_HEURISTIC = evidence.heuristic("MI $1–2, AB 3–5× SB", evidence.GUIDE_2021)
STALE_OPTIONS = evidence.heuristic("снизить цену, перевести в OTA (пусть предлагают сами), собрать набор «2 за X», "
                                   "предложить в подходящих LF-тредах Toyhouse", "практика продавцов адоптов")
LOW_START = evidence.heuristic("невысокий старт на аукционе помогает получить первую ставку",
                               "практика продавцов адоптов")
BOOSTY_NO_HINT = ("Boosty указан, но не сказано, как платить иностранцам: метод оплаты «не для карт РФ» и обязательный "
                  "3-D Secure (FAQ Boosty, обновлено 12.2025); валюту пересчитывает их банк. "
                  "Готовую строку даст чек-лист поста.")
EXCLUSIVES = ("DA Exclusives — официальный способ DeviantArt продавать адопты: фикс-цена или приём предложений, "
              "нужна подписка Core, лот попадает в Shop, комиссия «от 2.5%», можно включить 10% роялти с перепродаж "
              f"({evidence.DA_SELL}). Данных, что так продаётся лучше, у меня нет — это просто ещё один формат.")


@dataclass
class Advice:
    kind: str
    title: str
    detail: str
    evidence: str = ""
    confidence: str = "низкая"   # нет вывода | низкая | средняя | высокая
    impact: int = 0
    extra: dict = field(default_factory=dict)


def _conf(n: int, mid: int = 5, high: int = 12) -> str:
    """Старое имя: уверенность по числу наблюдений, единая шкала evidence.level."""
    return forecast.confidence(n)


def _money(amount: float | None, currency: str | None = "usd") -> str:
    return sales.money(round(amount, 2) if amount is not None else None, currency)


def _round_price(v: float, currency: str = "usd") -> float:
    return forecast.round_price(v, currency)


# Ниши по прогнозу показываем, когда завершённых аукционов рынка хватает.
FORECAST_MIN_LOTS = 20


def _plan(conn, now: float, unit: str = "usd", **kw):
    try:
        return forecast.price_plan(conn, unit=unit, now=now, **kw)
    except Exception:  # noqa: BLE001 — советник не должен падать из-за прогноза
        return None


def _json(v) -> dict:
    try:
        d = json.loads(v or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


# --- Данные ---

def _her_lots(conn, now: float, days: int = 180) -> list:
    return conn.execute(
        "SELECT * FROM items WHERE source = 'deviantart' AND own = 1 AND lot_type NOT IN ('other', 'ych', 'commission') "
        "AND COALESCE(published_ts, first_seen_ts) >= ? ORDER BY COALESCE(published_ts, first_seen_ts)",
        (now - days * DAY,)).fetchall()


def _auctions(conn, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        try:
            rows = conn.execute(f"SELECT * FROM auctions WHERE deviation_id IN ({','.join('?' * len(chunk))})",
                                chunk).fetchall()
        except Exception:  # noqa: BLE001 — таблицы может не быть в старой базе
            return {}
        out.update({r["deviation_id"]: dict(r) for r in rows})
    return out


def _start(r) -> float:
    return r["published_ts"] or r["first_seen_ts"]


def _unit(r) -> tuple[str, float] | None:
    if r["price_usd"] is not None:
        return "usd", r["price_usd"]
    if r["price_points"] is not None:
        return "points", r["price_points"]
    return None


def _tags_of(row) -> list[str]:
    extra = _json(row["extra"])
    tags = [*(extra.get("tags") or []), *(extra.get("api_tags") or [])]
    seen, out = set(), []
    for t in tags:
        if isinstance(t, str) and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _uuid_of(row) -> str | None:
    return _json(row["extra"]).get("uuid")


def _finished(r, a: dict | None) -> bool:
    """Аукцион точно закончился (то же правило, что и в прогнозе: forecast.outcome_known)."""
    if not a:
        return False
    return forecast.outcome_known(ended=a.get("ended"), status=r["status"], closed_ts=r["closed_ts"],
                                  a_extra=_json(a.get("extra")), a_updated=a.get("updated_ts"),
                                  published=_start(r), item_updated=r["updated_ts"])


def _is_group_place(place: str | None) -> bool:
    from .deviantart import MY_GALLERY

    if not place:
        return False
    return place != MY_GALLERY and not place.startswith("художник ") and not forecast.is_search(place)


def _group_lots(conn) -> dict[str, set[str]]:
    """Группа DA → её лоты (da:<id>), где лот был: по журналу отправок и по тому, в каких лентах групп его видели.

    В sent_log для da_group: target — группа, detail — ссылка на работу или её UUID из API. Отправка через
    помощника сейчас выключена, поэтому главный источник — items.extra["places"] её лотов (ленты групп, где
    помощник сам видел её лот, в том числе выложенный вручную)."""
    out: dict[str, set[str]] = defaultdict(set)
    own = conn.execute("SELECT id, place, extra FROM items WHERE source = 'deviantart' AND own = 1").fetchall()
    by_uuid = {}
    for r in own:
        extra = _json(r["extra"])
        if extra.get("uuid"):
            by_uuid[extra["uuid"]] = r["id"]
        raw_places = extra.get("places")
        places = raw_places if isinstance(raw_places, list) else []
        for p in {*(x for x in places if isinstance(x, str)), r["place"]}:
            if _is_group_place(p):
                out[p].add(r["id"])
    try:
        rows = conn.execute("SELECT target, ref, detail FROM sent_log WHERE kind = 'da_group' AND ok = 1").fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        m = re.search(r"-(\d+)/?(?:[?#].*)?$", r["detail"] or "")
        item_id = f"da:{m.group(1)}" if m else by_uuid.get(r["detail"] or "") or by_uuid.get(r["ref"] or "")
        if item_id:
            out[r["target"]].add(item_id)
    return dict(out)


def _first_group_ts(conn) -> dict[str, float]:
    """Её лот → когда его впервые отправили в группу (по журналу)."""
    try:
        rows = conn.execute("SELECT detail, ref, MIN(ts) AS ts FROM sent_log WHERE kind = 'da_group' AND ok = 1 "
                            "GROUP BY detail, ref").fetchall()
    except Exception:  # noqa: BLE001
        return {}
    by_uuid = {_uuid_of(r): r["id"] for r in conn.execute(
        "SELECT id, extra FROM items WHERE source = 'deviantart' AND own = 1").fetchall() if _uuid_of(r)}
    out: dict[str, float] = {}
    for r in rows:
        m = re.search(r"-(\d+)/?(?:[?#].*)?$", r["detail"] or "")
        item_id = f"da:{m.group(1)}" if m else by_uuid.get(r["detail"] or "") or by_uuid.get(r["ref"] or "")
        if item_id:
            out[item_id] = min(out.get(item_id, r["ts"]), r["ts"])
    return out


def _forecast(conn, keywords: list[str], lot_type: str | None, now: float):
    try:
        from . import market

        return market.forecast(conn, keywords, lot_type, now=now)
    except Exception:  # noqa: BLE001 — советник не должен падать из-за прогноза
        return None


def _stat(fc, group: str, unit: str = "usd"):
    try:
        s = fc.stats[group][unit]
        return s if s.n else None
    except (AttributeError, KeyError, TypeError):
        return None


def _tag(s) -> str:
    return evidence.tag(s.ev) if getattr(s, "ev", None) is not None else f"(n={getattr(s, 'n', 0)})"


# --- Время публикации ---

def _slot(ts: float, zone) -> tuple[str, int]:
    local = datetime.fromtimestamp(ts, zone)
    return ("weekend" if local.weekday() >= 5 else "weekday"), local.hour // 6


def _slot_name(slot: tuple[str, int]) -> tuple[str, str, str]:
    kind, part = slot
    name, adverb, hour = DAYPARTS[part]
    days = "в выходные" if kind == "weekend" else "в будни"
    return (f"{days} {adverb} ({hour:02d}–{hour + 6:02d})", "выходные" if kind == "weekend" else "будни",
            f"{hour:02d}–{hour + 6:02d}")


def _testable(obs: list[tuple[tuple[str, int], bool, float]]) -> list[tuple[str, int]]:
    by: Counter = Counter(slot for slot, _got, _ts in obs)
    return [s for s, n in by.items() if n >= evidence.MIN_SIDE and len(obs) - n >= evidence.MIN_SIDE]


def _pick_slot(obs: list[tuple[tuple[str, int], bool, float]], source: str, population: str, unit: str | None,
               tz: str, kind: evidence.Source, n_tests: int | None = None) -> dict | None:
    """n_tests — сколько окон проверяем всего (её и рынка): порог один на все, иначе запасной проход
    по рынку давал бы ложное «лучшее время» вдвое чаще."""
    by: dict[tuple[str, int], list[bool]] = defaultdict(list)
    for slot, got, _ts in obs:
        by[slot].append(got)
    total = len(obs)
    k_all = sum(1 for _, got, _ in obs if got)
    testable = _testable(obs)
    if not testable:
        return None
    thr = evidence.family_threshold(max(n_tests or 0, len(testable)))
    scored = []
    for s in testable:
        k, n = sum(by[s]), len(by[s])
        p = evidence.p_greater(k, n, k_all - k, total - n)
        scored.append((p, n, s, k))
    p, n, best, k = max(scored)
    if p < thr:
        return None
    when, days_name, hours = _slot_name(best)
    times = [ts for s, _g, ts in obs if s == best]
    ev = evidence.span(times, kind, population, unit, "observed", n=n, interval=evidence.wilson(k, n),
                       measure="rate", value=k / n)
    kr, nr = k_all - k, total - n
    return {"slot": best, "weekday": 5 if best[0] == "weekend" else 0, "weekday_name": days_name,
            "hour": DAYPARTS[best[1]][2], "hours": hours, "daypart": DAYPARTS[best[1]][0], "when": when,
            "evidence": (f"{when} ставку получили {k} из {n} ({evidence.pct(k / n)}), в остальное время — {kr} из {nr} "
                         f"({evidence.pct(kr / nr)}); P(лучше) = {p:.2f} при пороге {thr:.2f} ({source}) "
                         + evidence.tag(ev)),
            "n": n, "total": total, "p_better": p, "level": evidence.level(ev), "source": source, "tz": tz}


WEAK_LEVELS = ("нет вывода", "низкая")  # такое окно «лучшим временем» не называем нигде


def best_publish_time(conn, now: float | None = None, tz: str = DEFAULT_TZ) -> dict | None:
    """Окно (будни/выходные × ночь/утро/день/вечер по её поясу), где завершённые аукционы чаще получают ставку.

    Только завершённые лоты; время — первая отправка в группу (если была), иначе публикация. Окно называем,
    только если в нём ≥ evidence.MIN_SIDE лотов и P(чаще остальных) ≥ порога с поправкой на число окон.
    Иначе None — «лучшего времени по данным не видно»."""
    now = now or time.time()
    zone = ZoneInfo(tz)
    sent = _first_group_ts(conn)
    mine = [r for r in _her_lots(conn, now) if r["lot_type"] in AUCTION_TYPES]
    a_mine = _auctions(conn, [r["id"] for r in mine])
    obs = [(_slot(sent.get(r["id"]) or _start(r), zone), bool((a_mine[r["id"]].get("n_bids") or 0) > 0), _start(r))
           for r in mine if _finished(r, a_mine.get(r["id"]))]
    market_rows = [r for r in conn.execute(
        "SELECT * FROM items WHERE source = 'deviantart' AND own = 0 AND lot_type IN ('auction','bta') "
        "AND published_ts >= ?", (now - 60 * DAY,)).fetchall() if not forecast.is_search(r["place"])]
    a_market = _auctions(conn, [r["id"] for r in market_rows])
    m_obs = [(_slot(_start(r), zone), bool((a_market[r["id"]].get("n_bids") or 0) > 0), _start(r))
             for r in market_rows if _finished(r, a_market.get(r["id"]))]
    n_tests = len(_testable(obs)) + len(_testable(m_obs))
    return (_pick_slot(obs, "её лоты", "её аукционы DA", None, tz, "her_lots", n_tests)
            or _pick_slot(m_obs, "рынок", "аукционы DA, все продавцы, без её лотов", None, tz, "market_da", n_tests))


# --- Правила ---

def _rule_unpaid(conn, now: float) -> list[Advice]:
    rows = sales.unpaid(conn, 3, now)
    if not rows:
        return []
    lines = [f"{r['title']} — {sales.money(r['amount'], r['currency']) if r['currency'] else 'сумма без валюты'}"
             + (f", {r['buyer']}" if r["buyer"] else "")
             + f", {int((now - r['created_ts']) // DAY)} дн" for r in rows[:5]]
    trusted = [r for r in rows if r["status"] == "transferred" and not r["paid_ts"]]
    transfer = (f"\nПерс уже передан без оплаты: {len(trusted)}. Лучше передавать после оплаты — "
                f"{sales.CHARGEBACK_NOTE}." if trusted else "")
    return [Advice("unpaid", f"Ждут оплаты: {len(rows)}",
                   "Можно мягко напомнить покупателям. " + evidence.heuristic(
                       "если покупатель молчит неделю, лот часто переходит следующему по ставке", evidence.GUIDE_2021)
                   + transfer + "\n" + "\n".join(lines),
                   evidence=f"{len(rows)} её продаж не оплачены дольше 3 дней", confidence="высокая",
                   impact=90)]


def _her_auction_outcomes(conn, now: float) -> list[dict]:
    """Её завершённые аукционы (forecast.outcome_known) с SB и итогом. Идущие сейчас не попадают."""
    lots = [r for r in _her_lots(conn, now) if r["lot_type"] in AUCTION_TYPES and _unit(r)]
    auctions = _auctions(conn, [r["id"] for r in lots])
    out = []
    for r in lots:
        a = auctions.get(r["id"])
        if not _finished(r, a):
            continue
        assert a is not None
        unit, sb = _unit(r) or ("usd", 0)
        top = a.get("top_amount") if (a.get("currency") or unit) == unit else None
        out.append({"row": r, "unit": unit, "sb": sb, "top": top, "bids": a.get("n_bids") or 0,
                    "ab_hit": bool(a.get("ab_hit")), "ts": _start(r)})
    return out


def _her_ev(rows: list[dict], unit: str | None, k: int | None = None) -> evidence.Evidence:
    n = len(rows)
    k = sum(1 for o in rows if o["bids"]) if k is None else k
    return evidence.span([o["ts"] for o in rows], "her_lots", f"её аукционы DA, {forecast._unit_word(unit)}", unit,
                         "observed", n=n, interval=evidence.wilson(k, n), measure="rate", value=k / n if n else None)


def _pool_words(plan) -> str:
    """«похожих аукционов» — только если старт посчитан по лотам ниши, иначе честно «рынка в целом»."""
    return "похожих аукционов" if getattr(plan, "niche_pool", False) else "аукционов рынка в целом"


def _rule_pricing(conn, now: float) -> list[Advice]:
    """Старт — только из forecast.price_plan; её лоты сравниваем с рынком при том же старте."""
    out = _her_auction_outcomes(conn, now)
    adv: list[Advice] = []
    outcomes = None
    for unit in ("usd", "points"):
        rows = [o for o in out if o["unit"] == unit]
        if len(rows) < MIN_PRICING_LOTS:
            continue
        if outcomes is None:
            outcomes = forecast.market_pool(forecast.load_outcomes(conn, now))
        sb_med = statistics.median(o["sb"] for o in rows)
        b = forecast._bucket(sb_med, unit)
        mine = [o for o in rows if forecast._bucket(o["sb"], unit) == b]
        market = [o for o in outcomes if o.unit == unit and forecast._bucket(o.start, unit) == b]
        k1, n1 = sum(1 for o in mine if o["bids"]), len(mine)
        k0, n0 = sum(1 for o in market if o.sold), len(market)
        verdict = evidence.compare(k1, n1, k0, n0)
        if not verdict:
            continue
        p = evidence.p_greater(k1, n1, k0, n0)
        ev = _her_ev(mine, unit, k1)
        facts = (f"При старте около {_money(sb_med, unit)} твои аукционы получили ставку {k1} из {n1} "
                 f"({evidence.pct(k1 / n1)}), рынок при таком же старте — {k0} из {n0} ({evidence.pct(k0 / n0)}); "
                 f"P(у тебя {'чаще' if verdict == 'better' else 'реже'}) = {max(p, 1 - p):.2f}.")
        plan = _plan(conn, now, unit)
        step = ""
        if plan and plan.best and plan.confidence != "нет вывода":
            pb = plan.best
            if verdict == "worse" and pb["start"] < sb_med:
                step = (f" По рынку лучшая нижняя граница дохода — у старта {_money(pb['start'], unit)}: ставку там "
                        f"получили {pb['k']} из {pb['n']}, по модели ~{evidence.pct(pb['p_sold'])} "
                        f"(уверенность {plan.confidence}).")
            elif verdict == "better" and pb["start"] > sb_med:
                step = (f" По рынку старт {_money(pb['start'], unit)} даёт лучшую нижнюю границу дохода "
                        f"({pb['k']} из {pb['n']} со ставкой, уверенность {plan.confidence}) — можно попробовать.")
        title = "Твои лоты получают ставку реже рынка при том же старте" if verdict == "worse" else \
            "Твои лоты получают ставку чаще рынка при том же старте"
        adv.append(Advice("pricing", title, facts + step, evidence=evidence.tag(ev),
                          confidence=evidence.level(ev), impact=80 if verdict == "worse" else 70))
    for unit in ("usd", "points"):
        if sum(1 for o in out if o["unit"] == unit) >= MIN_PRICING_LOTS:
            continue  # её собственные аукционы говорят больше рынка
        plan = _plan(conn, now, unit)
        if not plan or not plan.best or plan.confidence == "нет вывода":
            continue
        b = plan.best
        lo, hi = b["ci"]
        adv.append(Advice(
            "pricing", f"Старт по рынку: {_money(b['start'], unit)}",
            f"У старта {_money(b['start'], unit)} лучшая нижняя граница дохода с лота: ставку получили {b['k']} из "
            f"{b['n']} {_pool_words(plan)} (80%: {evidence.rate_interval(lo, hi)}), по модели ~{evidence.pct(b['p_sold'])}; "
            f"ожидаемо {_money(round(b['ev_lo'], 2), unit)}–{_money(round(b['ev_hi'], 2), unit)} с лота.",
            evidence=evidence.tag(b["ev"]) + (f"; {plan.caveats[0]}" if plan.caveats else ""),
            confidence=plan.confidence, impact=65))
    return adv


def _rule_autobuy(conn, now: float) -> list[Advice]:
    try:
        from .auctions import ab_price
    except Exception:  # noqa: BLE001
        return []
    adv: list[Advice] = []
    for unit in ("usd", "points"):
        rows = [(o, ab) for o in _her_auction_outcomes(conn, now) if o["unit"] == unit and o["top"]
                if (ab := ab_price(o["row"]["snippet"], unit))]
        if len(rows) < evidence.MIN_SIDE:  # меньше — по общей шкале «нет вывода», совета не даём
            continue
        hit = [o for o, _ in rows if o["ab_hit"]]
        finals = [o["top"] for o, _ in rows if not o["ab_hit"]]
        ab_med = statistics.median(ab for _, ab in rows)
        ev = _her_ev([o for o, _ in rows], unit, len(hit))
        if not hit and finals and statistics.median(finals) <= 0.5 * ab_med:
            adv.append(Advice("autobuy", "Автобай никто не берёт",
                              f"Автобай у тебя около {_money(ab_med, unit)} (медиана), а без автобая лоты ушли примерно "
                              f"за {_money(statistics.median(finals), unit)} (медиана по {len(finals)}). "
                              f"Автобай взяли 0 раз из {len(rows)}. {MI_AB_HEURISTIC}.",
                              evidence=evidence.tag(ev), confidence=evidence.level(ev), impact=40))
        elif len(hit) * 2 >= len(rows):
            adv.append(Advice("autobuy", "Автобай берут часто",
                              f"Автобай около {_money(ab_med, unit)} взяли в {len(hit)} из {len(rows)} аукционов — "
                              "возможно, покупатели готовы платить больше. Насколько выше ставить, по данным не "
                              f"посчитать; {MI_AB_HEURISTIC}.",
                              evidence=evidence.tag(ev), confidence=evidence.level(ev), impact=45))
    return adv


def group_ranking(conn) -> list[dict]:
    """Её группы DA: сколько её завершённых аукционов там было и сколько из них получили ставку."""
    by_group = _group_lots(conn)
    all_ids = sorted({i for ids in by_group.values() for i in ids})
    auctions = _auctions(conn, all_ids)
    rows = {r["id"]: r for r in _rows(conn, all_ids)}
    out = []
    for group, ids in by_group.items():
        done = [i for i in ids if i in rows and _finished(rows[i], auctions.get(i))]
        bids = [auctions[i].get("n_bids") or 0 for i in done]
        if not bids:
            continue
        out.append({"group": group, "lots": len(bids), "avg_bids": statistics.mean(bids),
                    "with_bids": sum(1 for b in bids if b), "share": sum(1 for b in bids if b) / len(bids),
                    "ids": done})
    return sorted(out, key=lambda g: (g["share"], g["lots"]), reverse=True)


def _rows(conn, ids: list[str]) -> list:
    out = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out += conn.execute(f"SELECT * FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    return out


def _rule_groups(conn) -> list[Advice]:
    ranked = group_ranking(conn)
    adv: list[Advice] = []
    if len(ranked) >= 2:
        top = ranked[0]
        rest_ids = {i for g in ranked[1:] for i in g["ids"]} - set(top["ids"])
        auctions = _auctions(conn, sorted(rest_ids))
        k0 = sum(1 for i in rest_ids if (auctions.get(i) or {}).get("n_bids"))
        verdict = evidence.compare(top["with_bids"], top["lots"], k0, len(rest_ids),
                                   evidence.family_threshold(len(ranked)))
        if verdict == "better":
            adv.append(Advice("groups", f"Лучше всего работает {top['group']}",
                              f"В {top['group']} твои лоты получили ставку {top['with_bids']} из {top['lots']}, "
                              f"в остальных группах — {k0} из {len(rest_ids)}. Это совпадение по твоим лотам: лот "
                              "обычно выкладывают сразу в несколько групп, так что причину по этим данным не отделить.",
                              evidence=f"n={top['lots']} против {len(rest_ids)}, её лоты",
                              confidence=forecast.confidence(min(top["lots"], len(rest_ids))), impact=60))
    for g in ranked:
        if g["lots"] >= evidence.MIN_SIDE and not g["with_bids"]:
            adv.append(Advice("groups", f"{g['group']} пока не приносит ставок",
                              f"{g['lots']} твоих завершённых аукционов были в {g['group']}, ни один не получил ставку. "
                              "Это совпадение, не обязательно причина; если лимит отправок туда тратится, можно "
                              "попробовать другие группы.",
                              evidence=f"n={g['lots']}, её лоты", confidence=forecast.confidence(g["lots"]),
                              impact=35))
            break
    return adv


def _rule_timing(conn, now: float, tz: str) -> list[Advice]:
    best = best_publish_time(conn, now, tz)
    if not best or best["level"] in ("нет вывода", "низкая"):
        return []
    return [Advice("timing", f"Выкладывай {best['when']}",
                   f"Лучшее окно по данным — {best['when']} ({tz}).",
                   evidence=best["evidence"], confidence=best["level"], impact=50, extra=best)]


# --- Спрос: DeviantArt и Toyhouse ---

def th_wants(row) -> tuple[set[str], set[str]]:
    """Что ищут в LF-треде: виды из разобранного запроса и стили из текста — без того, что автор просил
    НЕ предлагать («LF ocs, NOT dragons» — драконов не ищут). Нет разбора в extra — разбираем заново."""
    lf = _json(row["extra"]).get("lf")
    if not isinstance(lf, dict) or not isinstance(lf.get("wanted_species"), list):
        lf = th_match.profile_dict(th_match.lf_profile(row["title"] or "", row["snippet"]))
    negated = {n for n in lf.get("negated") or [] if isinstance(n, str)}
    species = {s for s in lf["wanted_species"] if isinstance(s, str)} - negated
    styles = set(taxonomy.tag_text(row["title"] or "", (), row["snippet"] or "").styles) - negated
    return species, styles


def open_lf_threads(conn, days: int, now: float) -> list:
    """Живые LF-треды за days дней: каждый тред один раз (кросс-посты по thread_key), без закрытых и на паузе."""
    rows = conn.execute(
        "SELECT * FROM items WHERE source = 'toyhouse' AND match_kind = 'lf' AND dismissed = 0 "
        "AND COALESCE(activity_ts, published_ts, first_seen_ts) >= ?", (now - days * DAY,)).fetchall()
    seen: set[str] = set()
    out = []
    for r in rows:
        extra = _json(r["extra"])
        key = str(extra.get("thread_key") or r["id"])
        raw_lf = extra.get("lf")
        lf = raw_lf if isinstance(raw_lf, dict) else {}
        if key in seen or lf.get("closed") or lf.get("paused"):
            continue
        seen.add(key)
        out.append(r)
    return out


def th_demand(conn, days: int = 14, now: float | None = None) -> list[dict]:
    """Кого ищут на Toyhouse: живые LF-треды по видам и стилям (каждый тред один раз)."""
    now = now or time.time()
    counts: Counter = Counter()
    times: dict[str, list[float]] = defaultdict(list)
    for r in open_lf_threads(conn, days, now):
        species, styles = th_wants(r)
        ts = r["activity_ts"] or r["published_ts"] or r["first_seen_ts"]
        for label in {*(f"вид:{s}" for s in species), *(f"стиль:{s}" for s in styles)}:
            counts[label] += 1
            times[label].append(ts)
    out = []
    for label, n in counts.most_common():
        kind, _, name = label.partition(":")
        ev = evidence.span(times[label], "th_lf", "LF-треды Toyhouse", None, "observed", n=n, measure="count",
                           data_as_of=now)
        out.append({"tag": label, "species": name if kind == "вид" else None, "styles": name if kind == "стиль" else None,
                    "threads": n, "period": f"{days} дн", "ev": ev})
    return out


TH_DAYS = 14  # LF-треды Toyhouse живут недолго: спрос там считаем за 14 дней, а не за период DA


def demand_by_tag(conn, now: float | None = None, days: int = 30) -> list[dict]:
    """Рынок DA по видам/стилям: сколько лотов (из папок), ставки на лот (только завершённые аукционы), фавы на
    3–10-й день. Отдельная колонка th_threads — сколько LF-тредов Toyhouse ищут такое (не смешивается с DA)."""
    now = now or time.time()
    rows = [r for r in conn.execute(
        "SELECT * FROM items WHERE source = 'deviantart' AND own = 0 AND lot_type NOT IN ('other','ych','commission') "
        "AND COALESCE(published_ts, first_seen_ts) >= ?", (now - days * DAY,)).fetchall()
        if not forecast.is_search(r["place"])]
    auctions = _auctions(conn, [r["id"] for r in rows])
    stats = forecast.fav_snapshots(conn, rows)
    by: dict[str, dict] = {}
    for r in rows:
        t = forecast.analytics_tags(r)
        a = auctions.get(r["id"])
        done = r["lot_type"] in AUCTION_TYPES and _finished(r, a)
        for label in [*(f"вид:{s}" for s in t.species), *(f"стиль:{s}" for s in t.styles)]:
            d = by.setdefault(label, {"tag": label, "lots": 0, "bids": [], "favs": []})
            d["lots"] += 1
            if done and a is not None:
                d["bids"].append(a.get("n_bids") or 0)
            if r["id"] in stats and stats[r["id"]].get("favourites") is not None:
                d["favs"].append(stats[r["id"]]["favourites"])
    th = {d["tag"]: d["threads"] for d in th_demand(conn, TH_DAYS, now)}
    out = []
    for d in by.values():
        out.append({"tag": d["tag"], "lots": d["lots"],
                    "avg_bids": statistics.mean(d["bids"]) if d["bids"] else None, "bids_n": len(d["bids"]),
                    "with_bids": sum(1 for b in d["bids"] if b),
                    "avg_favs": statistics.mean(d["favs"]) if d["favs"] else None, "favs_n": len(d["favs"]),
                    "th_threads": th.get(d["tag"], 0), "period": f"{days} дн", "th_period": f"{TH_DAYS} дн"})
    for tag_, n in th.items():
        if tag_ not in by:
            out.append({"tag": tag_, "lots": 0, "avg_bids": None, "bids_n": 0, "with_bids": 0, "avg_favs": None,
                        "favs_n": 0, "th_threads": n, "period": f"{days} дн", "th_period": f"{TH_DAYS} дн"})
    return sorted(out, key=lambda d: (d["lots"], d["th_threads"]), reverse=True)


def _rule_demand_forecast(conn, now: float) -> list[Advice] | None:
    """Ниши по прогнозу. None — данных мало или прогноз отказался (тогда молчим)."""
    try:
        rep = forecast.niches(conn, now=now)
    except Exception:  # noqa: BLE001
        return None
    if rep.refusal or rep.n_lots < FORECAST_MIN_LOTS or not rep.niches:
        return None
    single = [d for d in rep.niches if "+" not in d["key"] and not d["key"].startswith("сезон:")]
    hot = [d for d in single if d["verdict"] == "sells"][:4]
    cold = [d for d in single if d["verdict"] == "saturated"][:4]
    level = ("у продавцов уровня «" + rep.tier.label + "»") if not rep.tier_fallback else "на рынке (все продавцы)"
    adv = []
    if hot:
        adv.append(Advice("demand", "Где аукционы чаще получают ставку",
                          f"Чаще остальных ставку получают {level}: " + ", ".join(
                              f"{forecast._niche_name(d['key'])} — {d['sold']} из {d['lots']} "
                              f"({forecast._pct(d['p_sold'])}, P = {d['p_better']:.2f})" for d in hot)
                          + f". В среднем — {rep.sold} из {rep.n_lots} ({forecast._pct(rep.p0)}).",
                          evidence=evidence.tag(rep.base_ev) if rep.base_ev else f"n={rep.n_lots}",
                          confidence=evidence.weakest(d["confidence"] for d in hot), impact=55))
    if cold:
        adv.append(Advice("demand", "Где рынок перегрет",
                          "Лотов много, а ставку получают реже остальных: " + ", ".join(
                              f"{forecast._niche_name(d['key'])} ({d['supply']} лотов за {forecast.SUPPLY_DAYS} дн, "
                              f"ставку получили {d['sold']} из {d['lots']})" + (f" — {d['season']}" if d["season"] else "")
                              for d in cold)
                          + ". Если рисуешь такое, выделяйся ценой или форматом.",
                          evidence=evidence.tag(rep.base_ev) if rep.base_ev else f"n={rep.n_lots}",
                          confidence=evidence.weakest(d["confidence"] for d in cold), impact=35))
    return adv


def _rule_th_demand(conn, now: float) -> list[Advice]:
    rows = [d for d in th_demand(conn, TH_DAYS, now) if d["species"] and d["threads"] >= 3][:5]
    if not rows:
        return []
    total = len(open_lf_threads(conn, TH_DAYS, now))  # те же треды, что в разбивке: без дублей и закрытых
    return [Advice("demand", "Кого ищут на Toyhouse",
                   "Живые LF-треды за 14 дней: " + ", ".join(f"{d['species']} — {d['threads']}" for d in rows)
                   + f" (всего тредов {total}). Это запросы покупателей, а не продажи; на DA цифры не переносятся.",
                   evidence=evidence.tag(rows[0]["ev"]), confidence=forecast.confidence(total), impact=50)]


def _rule_demand(conn, now: float) -> list[Advice]:
    return (_rule_demand_forecast(conn, now) or []) + _rule_th_demand(conn, now)


def _live_lf(conn, now: float, days: int = 7) -> list:
    return conn.execute(
        "SELECT * FROM items WHERE source = 'toyhouse' AND match_kind = 'lf' AND dismissed = 0 "
        "AND COALESCE(activity_ts, published_ts, first_seen_ts) >= ? ORDER BY activity_ts DESC",
        (now - days * DAY,)).fetchall()


def _replied(conn, cfg: Config | None, now: float) -> set[str]:
    try:
        from .th_match import replied_threads  # type: ignore[import-not-found]
    except ImportError:
        return set()
    try:
        return set(replied_threads(conn, (cfg.posting.th_username if cfg else "") or "", now - 7 * DAY))
    except Exception:  # noqa: BLE001
        return set()


def _lf_for_characters(conn, chars: list[dict], now: float, cfg: Config | None = None) -> dict[str, list]:
    try:
        from .th_reply import match_characters
    except Exception:  # noqa: BLE001
        return {}
    replied = _replied(conn, cfg, now)
    out: dict[str, list] = defaultdict(list)
    for t in _live_lf(conn, now):
        if t["id"] in replied:
            continue
        for ch, _why in match_characters(t["title"], t["snippet"], chars):
            out[ch.get("url", "")].append(t)
    return out


def time_to_close(conn, lot_type: str, own: bool) -> tuple[float | None, int]:
    """(за сколько дней закрываются 80% лотов этого формата, n) — по closed_ts − публикация, только лоты, которые
    видели открытыми. Точность — интервал проверки ленты."""
    types = AUCTION_TYPES if lot_type in AUCTION_TYPES else FIXED_TYPES if lot_type in FIXED_TYPES else (lot_type,)
    rows = conn.execute(
        f"SELECT COALESCE(published_ts, first_seen_ts) AS t, closed_ts FROM items WHERE source = 'deviantart' "
        f"AND own = ? AND lot_type IN ({','.join('?' * len(types))}) AND closed_ts IS NOT NULL "
        "AND closed_ts > first_seen_ts", (int(own), *types)).fetchall()
    days = sorted((r["closed_ts"] - r["t"]) / DAY for r in rows if r["t"] and r["closed_ts"] >= r["t"])
    if not days:
        return None, 0
    return days[min(len(days) - 1, int(round(0.8 * (len(days) - 1))))], len(days)


def _stale_days(conn, lot_type: str) -> tuple[float, str]:
    p80, n = time_to_close(conn, lot_type, True)
    who = "твоих"
    if n < evidence.MIN_SIDE:
        p80, n = time_to_close(conn, lot_type, False)
        who = "на рынке"
    if p80 is not None and n >= evidence.MIN_SIDE:
        days = max(1.0, round(p80, 1))
        return days, f"80% {who} закрытых лотов этого формата закрылись за {days:g} дн (n={n})"
    return float(STALE_DAYS), evidence.heuristic(f"{STALE_DAYS} дней без продажи — повод что-то поменять",
                                                 "условная граница: данных о сроке продажи мало")


def _rule_stale(conn, cfg: Config, now: float) -> list[Advice]:
    adv: list[Advice] = []
    sold_urls = {r["character_url"] for r in sales.list_sales(conn) if r["character_url"] and r["status"] != "cancelled"}
    chars = [c for c in cfg.posting.characters if c.get("url") and c["url"] not in sold_urls]
    if chars:
        lf = _lf_for_characters(conn, chars, now, cfg)
        with_threads = [(c, lf.get(c["url"], [])) for c in chars if lf.get(c["url"])]
        lines = [f"{c.get('name')}: {len(ts)} тред(ов), например «{ts[0]['title'][:60]}» (чужой текст)"
                 for c, ts in with_threads[:4]]
        if with_threads:
            adv.append(Advice("stale", "Твоих персов ищут прямо сейчас",
                              "Есть живые LF-треды под твоих персов — можно предложить (черновик через "
                              "prepare_toyhouse_reply, отправка только по твоему «го»):\n" + "\n".join(lines),
                              evidence=f"{len(with_threads)} персов совпали с живыми тредами за неделю (n="
                                       f"{sum(len(t) for _, t in with_threads)} тредов, LF-треды Toyhouse)",
                              confidence="средняя", impact=75))
    sold_items = {r["item_id"] for r in sales.list_sales(conn) if r["item_id"] and r["status"] != "cancelled"}
    lots = [r for r in _her_lots(conn, now) if r["id"] not in sold_items and r["closed_ts"] is None
            and r["status"] != "closed"]
    auctions = _auctions(conn, [r["id"] for r in lots])
    stale, notes = [], set()
    for r in lots:
        limit, why = _stale_days(conn, r["lot_type"])
        if _start(r) > now - limit * DAY:
            continue
        if r["lot_type"] in AUCTION_TYPES:
            a = auctions.get(r["id"])
            if not a or (a.get("n_bids") or 0) > 0:
                continue   # ставки не читали — не утверждаем; со ставками — это не «не продаётся»
        elif not (r["status"] == "open" and (r["updated_ts"] or 0) >= now - OPEN_SEEN_DAYS * DAY):
            continue       # фикс-цена/OTA: только если открытой её видели в ленте за последние дни
        stale.append(r)
        notes.add(why)
    if stale:
        fc = _forecast(conn, [], "flatsale", now)
        fixed = _stat(fc, "fixed")
        hint = (f" На рынке фикс-цены обычно {_money(fixed.low)}–{_money(fixed.high)} {_tag(fixed)}."
                if fixed and fixed.n >= 5 else "")
        lines = [f"{r['title'][:60]} — {r['price_text'] or 'цена не указана'}, "
                 f"{int((now - _start(r)) // DAY)} дн" for r in stale[:5]]
        adv.append(Advice("stale", f"Не продаются: {len(stale)}",
                          f"Варианты — {STALE_OPTIONS}.{hint}\n" + "\n".join(lines),
                          evidence="; ".join(sorted(notes)) + f"; лотов {len(stale)}",
                          confidence="средняя", impact=70))
    return adv


def _paid_by_format(conn, now: float) -> tuple[dict[str, dict[str, list[float]]], int]:
    rows = sales.list_sales(conn, ("paid", "transferred", "done"), since=now - 180 * DAY)
    rows = [r for r in rows if r["paid_ts"]]  # «перс передан» без оплаты — ещё не доход
    no_cur = sum(1 for r in rows if r["amount"] is not None and not r["currency"])
    by: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["amount"] is None or not r["currency"]:
            continue
        key = "auction" if r["format"] in AUCTION_TYPES else "flatsale" if r["format"] in FIXED_TYPES else r["format"]
        by[r["currency"]][key].append(r["amount"])
    return by, no_cur


def _format_winner(amounts: dict[str, list[float]]) -> tuple[str, str] | None:
    """(лучше, хуже), если у обоих ≥ MIN_SIDE продаж и 80%-интервалы медиан не пересекаются."""
    good = {k: v for k, v in amounts.items() if len(v) >= evidence.MIN_SIDE}
    if len(good) < 2:
        return None
    ranked = sorted(good, key=lambda k: statistics.median(good[k]), reverse=True)
    best, worst = ranked[0], ranked[-1]
    if evidence.median_ci(good[best])[0] > evidence.median_ci(good[worst])[1]:
        return best, worst
    return None


FORMAT_NAMES = {"auction": "аукционы", "flatsale": "фикс-цена", "ota": "OTA", "raffle": "лотереи"}


def _rule_format(conn, now: float) -> list[Advice]:
    by, no_cur = _paid_by_format(conn, now)
    adv = []
    for cur, amounts in by.items():
        win = _format_winner(amounts)
        if not win:
            continue
        best, worst = win
        bv, wv = amounts[best], amounts[worst]
        n = len(bv) + len(wv)
        adv.append(Advice("format", f"Больше приносят {FORMAT_NAMES.get(best, best)}",
                          f"Медиана: {FORMAT_NAMES.get(best, best)} — {_money(statistics.median(bv), cur)} за продажу "
                          f"({len(bv)}), {FORMAT_NAMES.get(worst, worst)} — {_money(statistics.median(wv), cur)} "
                          f"({len(wv)}); 80%-интервалы медиан не пересекаются."
                          + (f" Ещё {no_cur} продаж без валюты не учтены." if no_cur else ""),
                          evidence=f"n={n}, её продажи за 180 дн, {cur}", confidence=forecast.confidence(n), impact=45))
    return adv


def _rule_beginner(conn, cfg: Config, now: float, tz: str) -> list[Advice]:
    done = [r for r in sales.list_sales(conn) if r["status"] != "cancelled"]
    if len(done) >= 3:
        return []
    fc = _forecast(conn, [], "auction", now)
    auc = _stat(fc, "auction")
    price = ""
    if auc:
        price = (f" На рынке старт аукционов обычно {_money(auc.low)}–{_money(auc.high)} (по {auc.n} лотам) "
                 f"{_tag(auc)}.")
    best = best_publish_time(conn, now, tz)
    # Как в плане, советах и разборе: окно с уверенностью «нет вывода» или «низкая» не называем лучшим.
    when = (f"\n• по данным лучше всего заходят лоты {best['when']} ({best['evidence']})."
            if best and best["level"] not in WEAK_LEVELS else "")
    places: Counter = Counter()
    for r in conn.execute("SELECT place, extra FROM items WHERE source = 'deviantart' AND own = 0 "
                          "AND COALESCE(published_ts, first_seen_ts) >= ?", (now - 30 * DAY,)).fetchall():
        extra = _json(r["extra"])
        ps = {p for p in (extra.get("places") or []) if isinstance(p, str)} | ({r["place"]} if r["place"] else set())
        for p in ps:
            if _is_group_place(p):
                places[p] += 1
    busy = ", ".join(f"{p} ({n})" for p, n in places.most_common(3))
    return [Advice("start", "С чего начать продажи",
                   "Первые продажи — самые трудные, это нормально. Что может помочь:\n"
                   f"• {LOW_START}.{price}\n"
                   + (f"• самые оживлённые группы по рынку: {busy} (лотов за 30 дн, n по группам).\n" if busy else
                      "• выкладывать в крупные живые группы адоптов.\n")
                   + "• отвечать в LF-треды Toyhouse своими подходящими персами (по одному на тред)."
                   + when,
                   evidence=f"её продаж пока {len(done)}", confidence="средняя", impact=70)]


def advice(conn, cfg: Config, now: float | None = None, tz: str = DEFAULT_TZ) -> list[Advice]:
    now = now or time.time()
    out: list[Advice] = []
    for rule in (lambda: _rule_unpaid(conn, now), lambda: _rule_pricing(conn, now),
                 lambda: _rule_autobuy(conn, now), lambda: _rule_groups(conn),
                 lambda: _rule_timing(conn, now, tz), lambda: _rule_demand(conn, now),
                 lambda: _rule_stale(conn, cfg, now), lambda: _rule_format(conn, now),
                 lambda: _rule_beginner(conn, cfg, now, tz)):
        try:
            out.extend(rule())
        except Exception:  # noqa: BLE001 — одно сломанное правило не должно убирать остальные
            continue
    return sorted(out, key=lambda a: a.impact, reverse=True)


def render_advice(items: list[Advice], limit: int = 5) -> str:
    if not items:
        return ("Пока мало данных для советов: нужны её завершённые лоты и немного рынка. "
                "Через неделю-другую мониторинга советы появятся.\n\n" + GENTLE_ADVICE)
    lines = []
    for a in items[:limit]:
        lines.append(f"• {a.title}\n  {a.detail}\n  Основание: {a.evidence}. Уверенность: {a.confidence}.")
    return "\n\n".join(lines) + "\n\n" + GENTLE_ADVICE


# --- Отчёт ---

def th_reply_conversion(conn, days: int = 60, now: float | None = None) -> dict:
    """Отклики в LF-треды Toyhouse → продажи этих персов: {"replies", "characters", "sales", "period", "text"}."""
    now = now or time.time()
    try:
        rows = conn.execute("SELECT ref, ts FROM sent_log WHERE kind = 'th_reply' AND ok = 1 AND ts >= ?",
                            (now - days * DAY,)).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    first: dict[str, float] = {}
    for r in rows:
        if r["ref"]:
            first[r["ref"]] = min(first.get(r["ref"], r["ts"]), r["ts"])
    sold = [s for s in sales.list_sales(conn) if s["status"] != "cancelled" and s["character_url"] in first
            and s["created_ts"] >= first[s["character_url"]]]
    res = {"replies": len(rows), "characters": len(first), "sales": len(sold), "period": f"{days} дн"}
    res["text"] = (f"Отклики на Toyhouse за {days} дн: откликов {len(rows)} → продаж этих персов {len(sold)} "
                   f"(персов в откликах {len(first)}; продажа после отклика — совпадение, не обязательно из-за него).")
    return res


def _income_line(cur: str, b: dict, before: float, since: float, until: float) -> str:
    trend = "" if not before else (f" ({'+' if b['total'] - before >= 0 else '−'}"
                                   f"{_money(abs(b['total'] - before), cur)} к прошлой неделе)")
    ev = evidence.Evidence(n=b["count"], n_eff=None, since_ts=since, until_ts=until, data_as_of=until,
                           source="her_sales", population=f"её оплаченные продажи, {cur}", unit=cur, kind="observed",
                           measure="count")
    return f"Заработано: {_money(b['total'], cur)} за {b['count']} продаж{trend} {evidence.tag(ev)}"


def weekly_report(conn, cfg: Config, now: float | None = None, tz: str = DEFAULT_TZ) -> str:
    now = now or time.time()
    this = sales.income(conn, now - 7 * DAY, now)
    prev = sales.income(conn, now - 14 * DAY, now - 7 * DAY)
    no_cur = sales.without_currency(conn, now - 7 * DAY, now)
    note = digest.freshness_note(conn, now)
    lines = ([note, ""] if note else []) + ["Неделя продаж"]
    if this:
        for cur, b in this.items():
            lines.append(_income_line(cur, b, prev.get(cur, {}).get("total", 0.0), now - 7 * DAY, now))
    else:
        lines.append("Оплаченных продаж на этой неделе нет." + (" На прошлой были." if prev else ""))
    if no_cur:
        lines.append(f"Ещё {no_cur} оплаченных продаж без валюты — в суммы не входят; укажи валюту (sales:list).")
    pending = sales.unpaid(conn, 0, now)
    if pending:
        total: Counter[str] = Counter()
        unknown = 0
        for r in pending:
            if r["amount"] is not None and r["currency"]:
                total[r["currency"]] += r["amount"]
            elif r["amount"] is not None:
                unknown += 1
        lines.append("Ждут оплаты: " + str(len(pending)) + (" на " + ", ".join(_money(v, k) for k, v in total.items())
                                                            if total else "")
                     + (f" (ещё {unknown} без валюты)" if unknown else ""))
    conv = th_reply_conversion(conn, 60, now)
    if conv["replies"] >= 5:
        lines.append(conv["text"])
    tips = advice(conn, cfg, now, tz)
    lines += ["", "Что можно сделать на этой неделе:", render_advice(tips, 3)]
    return "\n".join(lines)


# --- План продажи нового адопта ---

def _market_tags(conn, species: list[str], styles: list[str], now: float, days: int = 60, top: int = 15) -> list[str]:
    """Теги, которые реально ставят на похожих лотах (теги из API; RSS тегов не отдаёт)."""
    rows = conn.execute(
        "SELECT * FROM items WHERE source = 'deviantart' AND own = 0 AND COALESCE(published_ts, first_seen_ts) >= ?",
        (now - days * DAY,)).fetchall()
    counter: Counter = Counter()
    for r in rows:
        t = forecast.analytics_tags(r)
        if species and not set(species) & set(t.species):
            continue
        if styles and not set(styles) & set(t.styles):
            continue
        for tag in _tags_of(r):
            tag = tag.strip().lower().replace(" ", "")
            if TAG_OK.match(tag):
                counter[tag] += 1
    base = ["adopt", "adoptable", "adoptables", *species, *styles]
    tags = [t for t in base if TAG_OK.match(t)]
    for t, _n in counter.most_common(top * 2):
        if t not in tags:
            tags.append(t)
    return tags[:top]


def _cap(word: str) -> str:
    return word[:1].upper() + word[1:]


def _group_scores(conn) -> dict[str, dict]:
    try:
        rows = conn.execute("SELECT * FROM group_scores").fetchall()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        g = (r["grp"] or "").lower()
        if g and (g not in out or (r["known"] or 0) > (out[g].get("known") or 0)):
            out[g] = dict(r)
    return out


def _norm_share(conn, key: str, lot_type: str, now: float) -> tuple[int, int]:
    """(у скольких лотов рынка в описании есть key, у скольких описание вообще видно)."""
    facts = [row_facts(r)[0] for r in _norm_rows(conn, lot_type, 60, now)]
    facts = [f for f in facts if f["desc_len"]]
    return sum(1 for f in facts if f.get(key)), len(facts)


def launch_plan(conn, cfg: Config, *, species: str | None = None, style: str | None = None, fmt: str | None = None,
                keywords=(), now: float | None = None, tz: str = DEFAULT_TZ) -> str:
    """План продажи нового адопта: цена, формат, название, теги, группы, время, треды, что дальше."""
    now = now or time.time()
    t = taxonomy.tag_text(" ".join(x for x in (species or "", style or "", *keywords) if x))
    sp, st = t.species, t.styles
    words = [w for w in [*(sp or ([species] if species else [])), *(st or ([style] if style else [])), *keywords] if w]
    notes: list[str] = []

    fix_fc = _forecast(conn, words, "flatsale", now)
    fix = _stat(fix_fc, "fixed")
    plan = _plan(conn, now, "usd", species=sp, styles=st, themes=t.themes, keywords=tuple(keywords))
    plan_ok = bool(plan and plan.best and plan.confidence != "нет вывода")

    # Формат: её опыт важнее рынка, но сравнение — только при достаточных данных.
    by, _no_cur = _paid_by_format(conn, now)
    win = _format_winner(by.get("usd", {}))
    if fmt in ("auction", "flatsale", "ota", "bta", "raffle"):
        chosen, why = fmt, "она так выбрала"
    elif win and win[0] in ("auction", "flatsale", "ota"):
        chosen, why = win[0], "по её продажам этот формат приносит больше (интервалы медиан не пересекаются)"
    elif plan_ok:
        chosen, why = "auction", f"старт по рынку посчитан по {plan.pool_n if plan else 0} завершённым аукционам"
    elif fix and fix.n >= 5:
        chosen, why = "flatsale", "по рынку есть понятная фикс-цена"
    else:
        chosen, why = "ota", "данных о ценах мало — OTA покажет, сколько готовы предложить"
        notes.append("мало данных о ценах похожих лотов")

    lines = [f"План продажи: {' '.join(words) or 'адопт'}", ""]
    if plan and plan.niche_ev is not None and plan.niche_level != "нет вывода":
        tag = evidence.tag(plan.niche_ev).replace(f"уверенность {evidence.level(plan.niche_ev)}",
                                                  f"уверенность {plan.niche_level}")
        lines.append(f"Ниша: по модели похожие лоты получают ставку в ~{forecast._pct(plan.p_niche)} случаев, "
                     f"в среднем по рынку ~{forecast._pct(plan.p_base)} {tag}."
                     + (f" Сезон: {plan.season}." if plan.season else ""))
    if chosen in ("auction", "bta"):
        if plan_ok and plan and plan.best:
            b = plan.best
            lo, hi = b["ci"]
            lines.append(f"Цена: SB {_money(b['start'])}. С таким стартом ставку получили {b['k']} из {b['n']} "
                         f"{_pool_words(plan)} (80%: {evidence.rate_interval(lo, hi)}), по модели "
                         f"~{forecast._pct(b['p_sold'])} {evidence.tag(b['ev'])}.")
            lines += [f"Оговорка: {c}." for c in plan.caveats if "по рынку в целом" in c]
            if plan.ab.get("n", 0) >= evidence.MIN_SIDE:
                lines.append(f"Автобай на рынке: был у {plan.ab['n']} лотов, взяли {plan.ab['hits']} раз.")
        else:
            reason = (plan.refusal or "; ".join(plan.caveats)) if plan else "прогноз недоступен"
            lines.append("Цена: " + evidence.refuse("старт по данным", reason or "данных нет",
                                                    f"{forecast.MIN_START_LOTS} лотов на одну цену старта")
                         + " Поставь старт, за который тебе точно не обидно.")
            notes.append("старт по данным не посчитан")
        lines.append(f"MI и AB — {MI_AB_HEURISTIC}.")
    elif chosen in ("flatsale", "adopt"):
        if fix and fix.n >= 5:
            price = _round_price(fix.median or fix.low or 10)
            lines.append(f"Цена: около {_money(price)} — середина рынка (обычно {_money(fix.low)}–{_money(fix.high)}) "
                         f"{_tag(fix)}.")
        else:
            lines.append("Цена: " + evidence.refuse("фикс-цена по рынку", f"{fix.n if fix else 0} похожих лотов", 5))
            notes.append("нет цен похожих фикс-лотов")
    else:
        lines.append("Цена: OTA — пусть предлагают сами; можно указать минимум, ниже которого не продаёшь.")
    lines.append(f"Формат: {chosen} — {why}.")
    lines.append(f"Ещё формат: {EXCLUSIVES}")

    name = " ".join(_cap(w) for w in words[:3]) or "Name"
    fmt_label = {"auction": "AUCTION", "bta": "BTA", "flatsale": "FLATSALE", "adopt": "ADOPT", "ota": "OTA",
                 "raffle": "RAFFLE"}.get(chosen, chosen.upper())
    lines.append(f"Название (шаблон, впиши своё): [OPEN] {name} adopt — {fmt_label}")

    must = ["цену (SB/MI/AB или фикс)", "способ оплаты", "что получает покупатель (файл, размер, права)", "TOS"]
    if chosen in AUCTION_TYPES:
        must.insert(1, "когда закончится аукцион (дата/время с поясом или «через 48 ч после последней ставки»)")
        k, n = _norm_share(conn, "end_time", "auction", now)
        if n >= MIN_NORMS:
            must[1] += f" — на рынке срок пишут {k} из {n} аукционов ({evidence.pct(k / n)})"
    lines.append("В описании: " + "; ".join(must) + ".")

    tags = _market_tags(conn, sp, st, now)
    lines.append(f"Теги ({len(tags)}, DeviantArt разрешает до 30 — {evidence.DA_HELP_SUBMIT}): " + ", ".join(tags))

    presets = cfg.posting.group_presets
    scores = _group_scores(conn)
    if presets:
        listed = []
        for pname, groups in presets.items():
            def score(g, scores=scores):
                s = scores.get(g["group"].lower())
                return -(s.get("bid_share") or 0) if s and (s.get("known") or 0) >= evidence.MIN_SIDE else 0

            ordered = sorted(groups, key=score)
            listed.append(f"«{pname}»: " + ", ".join(
                g["group"] + (f" (по рынку, не по твоим продажам: ставку получают "
                              f"{forecast._pct(scores[g['group'].lower()]['bid_share'] or 0)} лотов, "
                              f"n={scores[g['group'].lower()]['known']} с известным итогом)"
                              if g["group"].lower() in scores and (scores[g["group"].lower()].get("known") or 0)
                              >= evidence.MIN_SIDE and scores[g["group"].lower()].get("bid_share") is not None else "")
                for g in ordered))
        lines.append("Группы: " + "; ".join(listed))
    else:
        lines.append("Группы: наборов групп пока нет — добавь свои группы (da_groups:preset_add), чтобы выкладывать "
                     "сразу везде.")

    best = best_publish_time(conn, now, tz)
    lines.append(f"Когда: {best['when']} ({tz}) — {best['evidence']}." if best and best["level"] not in
                 ("нет вывода", "низкая") else
                 "Когда: " + evidence.refuse("лучшее время публикации",
                                             "ни одного окна с доказанной разницей",
                                             f"окно с ≥{evidence.MIN_SIDE} лотами и P(лучше) ≥ порога"))

    want = set(sp) | set(st)
    threads = []
    replied = _replied(conn, cfg, now)
    for th_row in _live_lf(conn, now):
        if th_row["id"] in replied:
            continue
        lf = _json(th_row["extra"]).get("lf") or {}
        if isinstance(lf, dict) and (lf.get("closed") or lf.get("paused")):
            continue
        species_, styles_ = th_wants(th_row)
        if want and (species_ | styles_) & want:
            threads.append(th_row)
    if threads:
        lines.append("Toyhouse — ищут похожих прямо сейчас (чужой текст):")
        lines += [f"  - [{r['id']}] {r['title'][:70]}" for r in threads[:5]]
    else:
        lines.append("Toyhouse: живых LF-тредов под это сейчас не нашла.")

    stale_days, stale_why = _stale_days(conn, chosen)
    lines += ["", "Что дальше:",
              "• через сутки проверь ставки (da_auctions:mine) и ответь всем, кто спрашивал в комментариях;",
              f"• если за {stale_days:g} дн не ушло — посмотри варианты в advice:unsold ({stale_why})."]
    if chosen in ("auction", "bta") and plan_ok and plan:
        conf = plan.confidence
        lines.append(forecast._basis_line(plan.pool_n, plan.cutoff_ts, now, plan.backfilled))
    elif chosen in ("flatsale", "adopt") and fix and fix.ev is not None:
        conf = evidence.level(fix.ev)
    else:
        conf = "нет вывода" if notes else "низкая"
    lines.append(f"\nУверенность: {conf}." + (f" Мало данных: {'; '.join(notes)}." if notes else ""))
    lines.append(GENTLE_ADVICE)
    return "\n".join(lines)


# --- Разбор её поста и нормы рынка ---

STATUS_TITLE = re.compile(r"\b(open|opened|closed?|sold|on\s*hold|открыт\w*|закрыт\w*)\b", re.I)
FORMAT_TITLE = re.compile(r"\b(auction|ota|bta|flat\s?sale|raffle|ych|adopt(?:able)?s?|sale|pwyw)\b", re.I)
PRICE_TITLE = re.compile(r"[$€£]\s?\d|\d\s?[$€£]|\d+\s*(?:usd|pts|points)\b|\bsb\s*[:\-]?\s*\d", re.I)


def _norm_rows(conn, lot_type: str, days: int, now: float) -> list:
    types = AUCTION_TYPES if lot_type in AUCTION_TYPES else FIXED_TYPES if lot_type in FIXED_TYPES else (lot_type,)
    return conn.execute(
        f"SELECT * FROM items WHERE source = 'deviantart' AND own = 0 AND lot_type IN ({','.join('?' * len(types))}) "
        "AND COALESCE(published_ts, first_seen_ts) >= ?", (*types, now - days * DAY)).fetchall()


NORM_LABELS = (("title_status", "статус в названии ([OPEN] и т.п.)"), ("title_price", "цена в названии"),
               ("title_format", "формат в названии"), ("end_time", "срок окончания"), ("timezone", "часовой пояс"),
               ("tos", "TOS/правила"), ("pay", "способ оплаты"), ("buyer_gets", "что получает покупатель"),
               ("unwatermarked", "файл без водяного знака"), ("fullres", "png/полный размер"),
               ("rights", "коммерческие права"), ("toyhouse", "упоминает Toyhouse"), ("sb", "SB"), ("ab", "AB"))


def listing_norms(conn, lot_type: str = "auction", days: int = 60, now: float | None = None) -> str:
    """Как оформляют лоты на рынке DA: доли (и n) по лотам, которые видел помощник. Без выводов о причинах."""
    now = now or time.time()
    rows = _norm_rows(conn, lot_type, days, now)
    if len(rows) < MIN_NORMS:
        return (f"мало данных: лотов типа {lot_type} за {days} дн — {len(rows)}, для долей нужно хотя бы {MIN_NORMS}.")
    counts: Counter = Counter()
    full = with_text = 0
    for r in rows:
        title = r["title"] or ""
        facts, is_full = row_facts(r)
        full += is_full
        flags = {"title_status": bool(STATUS_TITLE.search(title)), "title_price": bool(PRICE_TITLE.search(title)),
                 "title_format": bool(FORMAT_TITLE.search(title))}
        if facts["desc_len"]:
            with_text += 1
            flags.update({"pay": pays_any(facts), "buyer_gets": buyer_gets(facts),
                          **{k: bool(v) for k, v in facts.items() if k != "desc_len"}})
        counts.update(k for k, v in flags.items() if v)
    ev = evidence.span([_start(r) for r in rows], "market_da", f"лоты DA типа {lot_type}, чужие", None, "observed",
                       n=len(rows), measure="count", data_as_of=max(r["updated_ts"] for r in rows))
    lines = [f"Как оформляют {lot_type} на рынке DA за {days} дн {evidence.tag(ev)}:"]
    for key, label in NORM_LABELS:
        total = len(rows) if key.startswith("title_") else with_text
        if total:
            lines.append(f"- {label}: {counts[key]} из {total} ({evidence.pct(counts[key] / total)})")
    lines.append(f"Описание видно у {with_text} из {len(rows)} (полностью — у {full}, у остальных только начало из "
                 "ленты, поэтому доли по описанию могут быть занижены). Это доли, а не причины продаж.")
    return "\n".join(lines)


def listing_review(item_row, conn, cfg: Config, now: float | None = None, tz: str = DEFAULT_TZ) -> list[str]:
    """Конкретные правки к одному её посту на DeviantArt — по фактам из описания, без придуманных цифр."""
    now = now or time.time()
    out = []
    title = item_row["title"] or ""
    facts, full = row_facts(item_row)
    tags = _tags_of(item_row)
    lot_type = item_row["lot_type"] or "other"
    if not STATUS_TITLE.search(title):
        k, n = _title_share(conn, STATUS_TITLE, lot_type, now)
        out.append("В названии нет статуса: добавь [OPEN] — так покупатели сразу видят, что лот доступен."
                   + (f" На рынке статус в названии у {k} из {n} лотов ({evidence.pct(k / n)})." if n >= MIN_NORMS else ""))
    if lot_type in ("other", "adopt") and not re.search(r"\b(auction|ota|bta|flat\s?sale|raffle|ych)\b", title, re.I):
        out.append("В названии не видно формата: допиши AUCTION, FLATSALE или OTA.")
    if not tags:
        out.append("Тегов не вижу: RSS DeviantArt не отдаёт теги — подключи DA, и я проверю теги. "
                   f"Максимум — 30 тегов ({evidence.DA_HELP_SUBMIT}).")
    elif not taxonomy.tag_text(title, tags).species:
        out.append("Нет тега с видом (dragon, cat, moth…).")
    if lot_type in AUCTION_TYPES:
        if not facts["sb"]:
            out.append("В описании не видно SB/MI/AB — без них непонятно, как ставить.")
        if not facts["end_time"]:
            k, n = _norm_share(conn, "end_time", "auction", now)
            out.append("Не видно, когда закончится аукцион (дата/время с поясом или «через 48 ч после последней "
                       "ставки»)." + (f" На рынке срок пишут {k} из {n} аукционов ({evidence.pct(k / n)})."
                                      if n >= MIN_NORMS else ""))
    if lot_type in FIXED_TYPES and not re.search(r"[$€£]|\d+\s*(?:usd|pts|points)", item_text_of(item_row), re.I):
        out.append("В описании не видно цены.")
    if not facts["tos"]:
        out.append("Не видно TOS (условий): ссылка на правила снимает вопросы покупателя.")
    if not pays_any(facts):
        out.append("Не видно способа оплаты: напиши, как платить (Boosty, поинты…) или «payment info in my profile».")
    elif facts.get("pay_boosty") and not facts.get("foreign_pay_hint"):
        out.append(BOOSTY_NO_HINT)
    if lot_type in AUCTION_TYPES + FIXED_TYPES + ("ota",) and not buyer_gets(facts):
        out.append(evidence.heuristic("напиши, что получает покупатель: файл без водяного знака, png/полный размер, "
                                      "права, перенос на Toyhouse", evidence.GUIDE_2021))
    info = []
    groups = sorted(g for g, ids in _group_lots(conn).items() if item_row["id"] in ids)
    if groups:
        info.append("Видела этот пост в группах: " + ", ".join(groups) + ".")
    else:
        out.append("В лентах отслеживаемых групп этот пост не видела. Если ты его туда отправляла — значит, эти группы "
                   "помощник не читает или пост ещё не приняли.")
    best = best_publish_time(conn, now, tz)
    if best and item_row["published_ts"] and best["level"] not in ("нет вывода", "низкая"):
        local = datetime.fromtimestamp(item_row["published_ts"], ZoneInfo(tz))
        if _slot(item_row["published_ts"], ZoneInfo(tz)) != best["slot"]:
            out.append(f"Выложен {WEEKDAYS_ON[local.weekday()]} в {local.hour:02d}:00; по данным чаще получают ставку "
                       f"лоты {best['when']} — {best['evidence']}.")
    if not full and len(item_row["snippet"] or "") >= 290:
        info.append("(Описание я вижу только частично — начало из ленты; часть проверок может ошибаться.)")
    return (out or ["По этим проверкам пост оформлен хорошо."]) + info


def item_text_of(row) -> str:
    from .listing import item_text

    return item_text(row)[0]


def _title_share(conn, pattern: re.Pattern, lot_type: str, now: float) -> tuple[int, int]:
    rows = _norm_rows(conn, lot_type if lot_type not in ("other",) else "auction", 60, now)
    return sum(1 for r in rows if pattern.search(r["title"] or "")), len(rows)


__all__ = ["Advice", "advice", "best_publish_time", "demand_by_tag", "group_ranking", "launch_plan", "listing_facts",
           "listing_norms", "listing_review", "render_advice", "th_demand", "th_reply_conversion", "time_to_close",
           "weekly_report"]
