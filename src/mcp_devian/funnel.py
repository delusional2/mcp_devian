"""Воронка её лота: просмотры → фавы → ставки → оплата, и где именно теряются покупатели.

Каждый шаг сравниваем с её же лотами за 90 дней (и, где есть, с похожими лотами рынка): «ниже, чем у 80%
твоих лотов» — только по снимку на 3–10 день и у лота, и у базы; нет такого снимка — «вывода нет». Слабое место — шаг, где лот
в нижней четверти её лотов; для него — что проверить (время и группы / превью и палитра / старт и пост /
оплата). Меньше MIN_SIDE лотов для сравнения — «вывода нет». Группы ранжируем по тому, сколько людей
в её нише там ставит. Никому не пишем: это выбор, куда выкладывать.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict

from . import advisor, db, evidence, forecast, palette

DAY = 86400
WINDOW_DAYS = 90
WEAK_PCT = 0.25          # лот в нижней четверти её лотов по шагу — слабое место
MIN_VIEWS = 20           # долю фавов считаем от 20 просмотров
MIN_FAVS = 3             # ставки на фав — от 3 фавов
PAY_WAIT_DAYS = 2        # «ждёт оплаты» дольше — уже трение
PAID = ("paid", "transferred", "done")
STEPS = ("views", "fav_rate", "bid_rate")
STEP_NAME = {"views": "Просмотры", "fav_rate": "Фавы от просмотров", "bid_rate": "Ставящие на фав"}
STEP_TITLE = {"views": "мало просмотров — лот мало кто видит",
              "fav_rate": "смотрят, но мало фавят — превью не цепляет",
              "bid_rate": "фавят, но не ставят — мешает цена или пост",
              "payment": "ставки были, а оплаты нет"}


def _json(v) -> dict:
    try:
        d = json.loads(v or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _snapshots(conn, item_id: str) -> list:
    try:
        return conn.execute("SELECT * FROM deviation_stats WHERE deviation_id = ? ORDER BY ts", (item_id,)).fetchall()
    except Exception:  # noqa: BLE001
        return []


def _comparable(conn, item_id: str, pub: float | None) -> tuple[dict | None, str]:
    """(снимок на 3–10 день, состояние): "ok"; иначе снимка нет и состояние — "none" (снимков не было),
    "young" (все снимки моложе 3 дн) или "missed" (снимки только старше 10 дн или не на 3–10 день)."""
    snaps = _snapshots(conn, item_id)
    if not snaps or not pub:
        return None, "none"
    ages = []
    for s in snaps:
        age = (s["ts"] - pub) / DAY
        if 3 <= age <= 10:
            return dict(s), "ok"
        ages.append(age)
    return None, "young" if max(ages) < 3 else "missed"


def _sale(conn, item_id: str):
    return conn.execute("SELECT * FROM sales WHERE item_id = ? ORDER BY status = 'cancelled', created_ts DESC LIMIT 1",
                        (item_id,)).fetchone()


def _paid(sale) -> bool:
    return bool(sale) and (sale["status"] in ("paid", "done") or (sale["status"] == "transferred" and sale["paid_ts"]))


def lot_funnel(conn, item_id: str, now: float | None = None) -> dict | None:
    """{views, favs, comments, bids, bidders, sold, amount, …} — последний снимок, ставки и продажа. None — лота нет."""
    row = db.get_item(conn, item_id)
    if not row:
        return None
    snaps = _snapshots(conn, item_id)
    last = snaps[-1] if snaps else None
    try:
        a = conn.execute("SELECT * FROM auctions WHERE deviation_id = ?", (item_id,)).fetchone()
    except Exception:  # noqa: BLE001
        a = None
    sale = _sale(conn, item_id)
    bids = int(a["n_bids"] or 0) if a else 0
    unit = "usd" if row["price_usd"] is not None else "points" if row["price_points"] is not None else None
    return {
        "id": item_id, "title": row["title"], "url": row["url"], "lot_type": row["lot_type"],
        "published_ts": row["published_ts"] or row["first_seen_ts"], "start": row["price_usd"] if unit == "usd"
        else row["price_points"], "unit": unit,
        "views": last["views"] if last else None, "favs": last["favourites"] if last else None,
        "comments": last["comments"] if last else None, "stats_ts": last["ts"] if last else None,
        "bids": bids, "bidders": (int(a["bidders"] or 0) or (1 if bids else 0)) if a else 0,
        "top": a["top_amount"] if a and bids else None, "top_unit": (a["currency"] or unit) if a else unit,
        "sold": _paid(sale), "sale_status": sale["status"] if sale else None,
        "sale_ts": sale["created_ts"] if sale else None,
        "amount": sale["amount"] if sale else None, "currency": sale["currency"] if sale else None,
    }


def _money(v, unit) -> str:
    from .sales import money

    return money(v, unit) if unit else f"{v:g}"


def _lot_ev(f: dict, now: float) -> evidence.Evidence:
    ts = f["stats_ts"] or f["published_ts"] or now
    return evidence.span([ts], "her_lots", "её лот", f["unit"], "observed", n=1, measure="count", data_as_of=ts)


def _funnel_text(f: dict) -> str:
    bits = [f"просмотры {f['views']}" if f["views"] is not None else "просмотры —",
            f"фавы {f['favs']}" if f["favs"] is not None else "фавы —"]
    if f["lot_type"] in advisor.AUCTION_TYPES or f["bids"]:
        bits.append(f"ставящих {f['bidders']}, ставок {f['bids']}"
                    + (f", максимум {_money(f['top'], f['top_unit'])}" if f["top"] else ""))
    if f["sold"]:
        bits.append("оплачено" + (f" {_money(f['amount'], f['currency'])}" if f["amount"] and f["currency"] else ""))
    elif f["sale_status"] == "won":
        bits.append("ждёт оплаты")
    elif f["sale_status"] == "cancelled":
        bits.append("продажа отменена")
    return " → ".join(bits)


def _metrics(f: dict, snap: dict | None, finished: bool) -> dict:
    out: dict = {}
    if snap and snap.get("views") is not None:
        out["views"] = float(snap["views"])
        if snap["views"] >= MIN_VIEWS and snap.get("favourites") is not None:
            out["fav_rate"] = snap["favourites"] / snap["views"]
        if finished and (snap.get("favourites") or 0) >= MIN_FAVS and f["lot_type"] in advisor.AUCTION_TYPES:
            out["bid_rate"] = f["bidders"] / snap["favourites"]
    return out


def _finished(conn, row, now: float) -> bool:
    """Итог аукциона известен (forecast.outcome_known). «Закрыт» без прочитанных ставок — не итог."""
    try:
        a = conn.execute("SELECT * FROM auctions WHERE deviation_id = ?", (row["id"],)).fetchone()
    except Exception:  # noqa: BLE001
        a = None
    return advisor._finished(row, dict(a) if a else None)


def _baseline(conn, item_id: str, now: float) -> tuple[dict[str, list[float]], list[float]]:
    """Её другие лоты DA за 90 дн: значения шагов и моменты публикации."""
    vals: dict[str, list[float]] = defaultdict(list)
    ts: list[float] = []
    for r in advisor._her_lots(conn, now, WINDOW_DAYS):
        if r["id"] == item_id:
            continue
        f = lot_funnel(conn, r["id"], now)
        if not f:
            continue
        snap, state = _comparable(conn, r["id"], f["published_ts"])
        if state != "ok":
            continue
        m = _metrics(f, snap, _finished(conn, r, now))
        for k, v in m.items():
            vals[k].append(v)
        if m:
            ts.append(f["published_ts"])
    return vals, ts


def _market(conn, now: float, niche: set[str]) -> tuple[dict[str, list[float]], list[float]]:
    """Похожие лоты рынка (её ниша) с итогом и снимком на 3–10 день."""
    outcomes = forecast.market_pool(forecast.load_outcomes(conn, now, WINDOW_DAYS))
    if niche:
        outcomes = [o for o in outcomes if niche & (set(o.tags.species) | set(o.tags.styles))]
    snaps = forecast.fav_snapshots(conn, [{"id": o.id, "published_ts": o.ts, "first_seen_ts": o.ts} for o in outcomes])
    vals: dict[str, list[float]] = defaultdict(list)
    ts = []
    for o in outcomes:
        s = snaps.get(o.id)
        if not s or s.get("views") is None:
            continue
        ts.append(o.ts)
        vals["views"].append(float(s["views"]))
        if s["views"] >= MIN_VIEWS and s.get("favourites") is not None:
            vals["fav_rate"].append(s["favourites"] / s["views"])
        if (s.get("favourites") or 0) >= MIN_FAVS:
            vals["bid_rate"].append(o.bidders / s["favourites"])
    return vals, ts


def _pct_below(values: list[float], x: float) -> float:
    """Доля значений ниже x (половина равных)."""
    if not values:
        return 0.5
    return (sum(1 for v in values if v < x) + 0.5 * sum(1 for v in values if v == x)) / len(values)


def _fmt(step: str, v: float) -> str:
    return f"{v:.0f}" if step == "views" else f"{v * 100:.1f}%" if step == "fav_rate" else f"{v:.2f}"


def _ev(values, ts, source, population, unit=None) -> evidence.Evidence:
    return evidence.span(ts, source, population, unit, "observed", n=len(values), measure="count",
                         value=statistics.median(values) if values else None)


def _payment_problem(f: dict, now: float, finished: bool) -> str | None:
    """Трение на оплате. Идущий аукцион со ставками — не проблема: «не записана» только у завершённого."""
    if not f["bids"] and not f["sale_status"]:
        return None
    if f["sold"]:
        return None
    if f["sale_status"] == "transferred":       # передан, а оплата не отмечена — как advisor._rule_unpaid
        return "перс передан, а оплата не отмечена"
    if f["sale_status"] == "won" and f["sale_ts"] and now - f["sale_ts"] > PAY_WAIT_DAYS * DAY:
        return f"ждёт оплаты {((now - f['sale_ts']) / DAY):.0f} дн"
    if f["sale_status"] == "cancelled":
        return "продажа отменена"
    if f["sale_status"] is None and f["bids"] and finished:
        return "продажа не записана (если уже оплатили — запиши её в sales)"
    return None


def _her_unpaid_line(conn, now: float) -> str:
    rows = conn.execute("SELECT status, created_ts, paid_ts FROM sales WHERE created_ts >= ? AND created_ts <= ?",
                        (now - WINDOW_DAYS * DAY, now - PAY_WAIT_DAYS * DAY)).fetchall()
    n = len(rows)
    if n < evidence.MIN_SIDE:
        return evidence.refuse(f"доля неоплаченных у тебя (продаж старше {PAY_WAIT_DAYS} дн за {WINDOW_DAYS} дн)",
                               n, evidence.MIN_SIDE)
    # Передан, а оплата не отмечена — тоже не оплачен (как sales.unpaid и _payment_problem).
    k = sum(1 for r in rows if r["status"] in ("won", "cancelled")
            or (r["status"] == "transferred" and r["paid_ts"] is None))
    ev = evidence.span([r["created_ts"] for r in rows], "her_sales", "её продажи", None, "observed", n=n,
                       interval=evidence.wilson(k, n), measure="rate", value=k / n)
    return f"Не оплачены или отменены у тебя {k} из {n} продаж ({evidence.pct(k / n)}) {evidence.tag(ev)}"


def _payment_rules() -> list[str]:
    from . import checklist, sales

    per = getattr(sales, "POINTS_PER_TRANSFER", 3500)
    year = getattr(sales, "POINTS_PER_YEAR", 20000)
    facts = {f.key: f for f in checklist.BOOSTY_FACTS}
    # Каждое утверждение — со своим источником: FAQ Boosty не говорит ни про PayPal, ни про блокировки.
    faq = [facts[k] for k in ("cards", "3ds") if k in facts]
    out = []
    if faq:
        out.append(evidence.heuristic(" ".join(f.text for f in faq), f"FAQ Boosty, {faq[0].date}: {faq[0].source}"))
    if "no_paypal" in facts:
        f = facts["no_paypal"]
        out.append(evidence.heuristic(f.text, f"{f.source}, {f.date}"))
    return out + [
        evidence.heuristic(f"запасной вариант — DA Points: до {per} за перевод, до {year} в год одному человеку; "
                           "поинты не выводятся в деньги", "правила DA Points"),
        evidence.heuristic("для иностранной карты обычно дают 48 ч на оплату; передать второму по ставке — "
                           "только твоё решение", "условия продавцов адоптов"),
    ]


def _rules(conn, step: str, row, f: dict, now: float, tz: str, niche: set[str]) -> list[str]:
    out: list[str] = []
    if step == "views":
        best = advisor.best_publish_time(conn, now, tz)
        out.append(f"Время: лучше всего {best['when']} — {best['evidence']}" if best else
                   "Время: лучшего окна по твоим лотам и рынку не видно — вывода нет.")
        good = [g for g in group_targets(conn, now, niche=niche) if not g["few"] and g["bidders"]][:3]
        if good:
            out.append("Группы, где в твоей нише больше всего ставящих: " + "; ".join(
                f"{g['group']} — ставящих {g['bidders']} {evidence.tag(g['ev'])}" for g in good))
        else:
            out.append("Группы: вывода нет — по группам твоей ниши мало данных (подробнее — promo:groups).")
    elif step == "fav_rate":
        fam = palette.cached_family(conn, f["id"])
        if fam:
            out.append(f"Палитра превью: {palette.label(fam)}. Такие в нише: "
                       f"{palette.family_line(conn, fam, now, niche=niche)}")
        else:
            out.append("Палитра превью ещё не посчитана — миниатюры докачиваются в фоне понемногу.")
        species = forecast.analytics_tags(row).species
        if species:
            outside = niche and not (set(species) & niche)
            out.append(f"Вид: {', '.join(species)}" + (" — это вне твоей обычной ниши." if outside else "."))
        out.append(evidence.heuristic("на фавы сильнее всего влияет миниатюра: персонаж крупно, читаемый силуэт, "
                                      "без текста поверх", "практика продавцов адоптов"))
    elif step == "bid_rate":
        from .listing import pays_any, row_facts

        facts, full = row_facts(row)
        need = [(k, w) for k, w in (("sb", "старт (SB)"), ("mi", "шаг ставки (MI)"), ("ab", "автобай (AB)"),
                                    ("end_time", "когда конец"), ("tos", "правила (TOS)")) if not facts.get(k)]
        missing = [w for _k, w in need] + ([] if pays_any(facts) else ["как платить"])
        tail = "" if full else " (читала только начало описания)"
        out.append(("В посте не нашла: " + ", ".join(missing) + tail + ".") if missing else
                   "В посте есть старт, шаг, автобай, конец, правила и оплата.")
        out.append(_start_line(conn, f, now))
        out.append(forecast.AB_HEURISTIC)
    return out


def _start_line(conn, f: dict, now: float) -> str:
    if f["start"] is None or not f["unit"]:
        return "Старт в названии или цене не нашла — сравнить не с чем."
    rows = [o for o in advisor._her_auction_outcomes(conn, now)
            if o["unit"] == f["unit"] and o["bids"] and o["row"]["id"] != f["id"]]
    if len(rows) < evidence.MIN_SIDE:
        return evidence.refuse("старт против твоих лотов со ставкой", len(rows), evidence.MIN_SIDE)
    starts = [float(o["sb"]) for o in rows]
    med = statistics.median(starts)
    lo, hi = evidence.median_ci(starts)
    ev = evidence.span([o["ts"] for o in rows], "her_lots", "её аукционы со ставкой", f["unit"], "observed",
                       n=len(rows), interval=(lo, hi), measure="price", value=med)
    word = "выше" if f["start"] > hi else "ниже" if f["start"] < lo else "в пределах"
    return (f"Старт {_money(f['start'], f['unit'])} — {word} медианы старта твоих лотов, получивших ставку "
            f"({_money(med, f['unit'])}, 80%: {_money(lo, f['unit'])}–{_money(hi, f['unit'])}) {evidence.tag(ev)}")


def diagnose(conn, item_id: str, now: float | None = None, tz: str = advisor.DEFAULT_TZ,
             niche: set[str] | None = None) -> list[str]:
    """Слабое место воронки лота против её лотов за 90 дн и рынка ниши, с метками доказательств."""
    now = now or time.time()
    f = lot_funnel(conn, item_id, now)
    if not f:
        return [f"Лота {item_id} нет в базе — проверь ник DA в настройках и дождись проверки."]
    row = db.get_item(conn, item_id)
    niche = palette.her_niche(conn) if niche is None else niche
    lines = [f"Воронка: {_funnel_text(f)} {evidence.tag(_lot_ev(f, now))}"]
    snap, state = _comparable(conn, item_id, f["published_ts"])
    if state == "none":
        lines.append("Просмотров и фавов по лоту ещё нет (снимков не было) — по шагам воронки вывода нет.")
    elif state == "young":
        lines.append("Вывода нет: лот моложе трёх дней, а твои лоты сравниваю по 3–10 дню — "
                     "загляни, когда ему будет 3 дня.")
    elif state == "missed":
        lines.append("Вывода нет: снимка просмотров на 3–10 день у этого лота нет, а сравнивать лоты разного "
                     "возраста нечестно.")
    finished = _finished(conn, row, now)
    mine = _metrics(f, snap, finished)
    base, base_ts = _baseline(conn, item_id, now)
    market, market_ts = _market(conn, now, niche)
    weak: list[tuple[float, str]] = []
    for step in STEPS:
        if step not in mine:
            continue
        x = mine[step]
        vals = base.get(step, [])
        if len(vals) < evidence.MIN_SIDE:
            lines.append(evidence.refuse(f"{STEP_NAME[step].lower()} — твоих лотов для сравнения", len(vals),
                                         evidence.MIN_SIDE))
            continue
        pct = _pct_below(vals, x)
        where = (f"ниже, чем у {evidence.pct(1 - pct)} твоих лотов" if pct < 0.5
                 else f"выше, чем у {evidence.pct(pct)} твоих лотов")
        line = (f"{STEP_NAME[step]}: {_fmt(step, x)} — {where} "
                f"(медиана {_fmt(step, statistics.median(vals))}) "
                + evidence.tag(_ev(vals, base_ts, "her_lots", "её лоты DA за 90 дн")))
        mv = market.get(step, [])
        if len(mv) >= evidence.MIN_SIDE:
            line += (f"; у похожих лотов рынка медиана {_fmt(step, statistics.median(mv))} "
                     + evidence.tag(_ev(mv, market_ts, "market_da", "аукционы её ниши, другие продавцы")))
        else:
            line += f"; по рынку ниши вывода нет ({len(mv)} из нужных {evidence.MIN_SIDE})"
        lines.append(line)
        if pct <= WEAK_PCT:
            weak.append((pct, step))
    pay = _payment_problem(f, now, finished)
    if weak:
        _pct, step = min(weak)
        lines.append(f"Слабое место: {STEP_TITLE[step]}. Что проверить:")
        lines += [f"- {r}" for r in _rules(conn, step, row, f, now, tz, niche)]
    elif not pay:
        lines.append("Слабого места по сравнению с твоими лотами не видно (или сравнивать пока не с чем).")
    if pay:
        lines.append(f"{'Ещё' if weak else 'Слабое место'}: {STEP_TITLE['payment']} — {pay} "
                     f"{evidence.tag(_lot_ev(f, now))}. Что проверить:")
        lines.append(f"- {_her_unpaid_line(conn, now)}")
        lines += [f"- {r}" for r in _payment_rules()]
    return lines


def render(conn, now: float | None = None, ref: str | None = None, tz: str = advisor.DEFAULT_TZ,
           niche: set[str] | None = None) -> str:
    """Один лот подробно (ref) или её последние 10 лотов коротко."""
    from .experiments import resolve_ref

    now = now or time.time()
    if ref:
        item_id = resolve_ref(conn, ref)
        if not item_id:
            return f"Не поняла, что за лот: «{ref}». Нужна ссылка на работу DA или da:<номер>."
        row = db.get_item(conn, item_id)
        head = f"Воронка лота «{row['title']}»" if row else f"Воронка {item_id}"
        return "\n".join([head, *diagnose(conn, item_id, now, tz, niche)])
    lots = advisor._her_lots(conn, now, WINDOW_DAYS)[-10:][::-1]
    if not lots:
        return (f"Твоих лотов на DA за {WINDOW_DAYS} дн в базе нет. Проверь ник DA в настройках — "
                "тогда помощник увидит твою галерею.")
    funnels = [f for f in (lot_funnel(conn, r["id"], now) for r in lots) if f]
    ev = evidence.span([f["published_ts"] for f in funnels], "her_lots", "её последние лоты DA", None, "observed",
                       n=len(funnels), measure="count")
    lines = [f"Твои последние лоты: просмотры → фавы → ставки → оплата (последний снимок) {evidence.tag(ev)}"]
    lines += [f"• {f['title'][:60]} — {_funnel_text(f)}" for f in funnels]
    lines.append("Где теряются покупатели у конкретного лота — promo:funnel ref=<ссылка>.")
    return "\n".join(lines)


# --- Группы ---

def group_key(place: str | None) -> str | None:
    """Место лота → группа DA (в нижнем регистре). Галереи, «художник …» и поиск — не группы."""
    from .deviantart import MY_GALLERY

    if not place or forecast.is_search(place) or place == MY_GALLERY or place.startswith("художник "):
        return None
    key = place.split(":", 1)[0].strip().lower()
    return key or None


def group_targets(conn, now: float | None = None, days: int = 60, niche: set[str] | None = None,
                  my_username: str = "") -> list[dict]:
    """Группы по числу разных ставящих на лоты её ниши (и победителей), с её итогами там и лотами в день."""
    from . import da_market

    now = now or time.time()
    niche = palette.her_niche(conn) if niche is None else niche
    me = (my_username or "").strip().lstrip("@").lower()
    # Рынок группы — без её лотов (они отдельно, «твоих там») и не больше SELLER_CAP лотов одного продавца
    # за окно: один плодовитый продавец не должен раздувать группу.
    market = [o for o in forecast.load_outcomes(conn, now, days)
              if not o.search and not o.own and not (me and o.author == me)
              and (not niche or niche & (set(o.tags.species) | set(o.tags.styles)))]
    outcomes = forecast.cap_sellers(market, forecast._cutoff(market) or now)
    try:
        repeat = {str(b["user"]).lower() for b in da_market.buyers(conn, days, now, my_username) if b["lots"] >= 2}
    except Exception:  # noqa: BLE001 — нет таблиц в старой базе
        repeat = set()
    winners: dict[str, str] = {}
    ids = [o.id for o in outcomes]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(f"SELECT deviation_id, winner FROM auctions WHERE winner IS NOT NULL AND winner != '' "
                              f"AND deviation_id IN ({','.join('?' * len(chunk))})", chunk):
            winners[r["deviation_id"]] = str(r["winner"]).lower()
    by: dict[str, list] = defaultdict(list)
    for o in outcomes:
        key = group_key(o.place)
        if key:
            by[key].append(o)
    her_out = {o["row"]["id"]: bool(o["bids"]) for o in advisor._her_auction_outcomes(conn, now) if o["ts"] >= now - days * DAY}
    her_by: dict[str, set[str]] = defaultdict(set)
    for place, lot_ids in advisor._group_lots(conn).items():
        key = group_key(place)
        if key:
            her_by[key] |= {i for i in lot_ids if i in her_out}
    try:
        sent = {r["target"].lower(): r["c"] for r in conn.execute(
            "SELECT target, COUNT(*) AS c FROM sent_log WHERE kind = 'da_group' AND ok = 1 AND ts >= ? GROUP BY target",
            (now - days * DAY,))}
    except Exception:  # noqa: BLE001
        sent = {}
    try:
        lpd = {r["grp"].lower(): r["l"] for r in conn.execute(
            "SELECT grp, MAX(lots_per_day) AS l FROM group_scores GROUP BY grp") if r["grp"]}
    except Exception:  # noqa: BLE001
        lpd = {}
    rows = []
    for key in set(by) | set(her_by):
        obs = by.get(key, [])
        n, k = len(obs), sum(1 for o in obs if o.sold)
        bidders = {b for o in obs for b in o.bidder_names}
        ev = evidence.span([o.ts for o in obs], "market_da", f"аукционы её ниши в группе {key}", None, "observed",
                           n=n, interval=evidence.wilson(k, n) if n else None, measure="rate",
                           value=k / n if n else None)
        mine = her_by.get(key, set())
        rows.append({"group": key, "n": n, "k": k, "bidders": len(bidders), "repeat": len(bidders & repeat),
                     "winners": len({winners[o.id] for o in obs if o.id in winners}), "her_n": len(mine),
                     "her_k": sum(1 for i in mine if her_out.get(i)), "her_sent": sent.get(key, 0),
                     "lots_per_day": lpd.get(key), "ev": ev, "level": evidence.level(ev),
                     "few": n < evidence.MIN_SIDE, "since_ts": ev.since_ts, "until_ts": ev.until_ts})
    return sorted(rows, key=lambda g: (g["few"], -g["bidders"], -g["winners"], -(g["k"] / g["n"] if g["n"] else 0),
                                       g["group"]))


def render_group_targets(rows: list[dict], days: int = 60, limit: int = 10) -> str:
    if not rows:
        return (f"Групп с аукционами твоей ниши за {days} дн в базе нет — вывода нет. Добавь группы, где продают "
                "похожих персонажей, в настройки DeviantArt — помощник начнёт их читать.")
    lines = [f"Куда выкладывать: группы по числу ставящих на лоты твоей ниши за {days} дн. Только выбор групп — "
             "никому не пишем. Лимиты отправки смотри в правилах каждой группы."]
    for g in rows[:limit]:
        line = f"• {g['group']}: "
        if g["n"]:
            line += (f"ставящих {g['bidders']} (ставят не впервые {g['repeat']}), победителей {g['winners']}; "
                     f"лотов ниши {g['n']}, со ставкой {g['k']} ({evidence.pct(g['k'] / g['n'])}) "
                     + evidence.tag(g["ev"]))
        else:
            line += "чужих лотов ниши с итогом нет"
        if g["lots_per_day"] is not None:
            line += f"; лотов в день {g['lots_per_day']:.1f}"
        if g["her_n"]:
            line += f"; твоих там {g['her_n']}, со ставкой {g['her_k']}"
            if g["her_n"] < evidence.MIN_SIDE:
                line += " (по твоим вывода нет)"
        if g["her_sent"]:
            line += f"; твоих отправок за {days} дн: {g['her_sent']}"
        if g["few"]:
            line += " — мало данных"
        lines.append(line)
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    return "\n".join(lines)
