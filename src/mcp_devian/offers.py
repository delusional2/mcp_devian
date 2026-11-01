"""Предложения покупателям: набор из её непроданных похожих лотов и оплата частями.

Только расчёт и тексты-черновики: отправляет она сама. Цена набора — от медианы её собственных
оплаченных продаж (метка evidence.tag), а если продаж мало — «вывода нет», без придуманных цифр.
Скидку на набор не придумываем: это её решение (общая практика продавцов — набор дешевле суммы).
"""

import statistics
import time
from collections import defaultdict

from . import evidence, forecast, sales, selling, taxonomy

DAY = 86400
STALE_DAYS = 7
HISTORY_DAYS = 180
MIN_BUNDLE = 2
PLAN_RULE = "перс передаётся после полной оплаты"
# Срок остатка — её правило (holds_policy); своего срока не придумываем: без правила — заглушка <N> days.
PLAN_SNIPPET = ("Payment plan is possible: 50% deposit now ({deposit}), the rest ({rest}) within <N> days. "
                "The character is transferred after full payment.")
PLAN_SNIPPET_POLICY = ("Payment plan is possible: 50% deposit now ({deposit}), the rest ({rest}). {policy} "
                       "The character is transferred after full payment.")


def _key(tags: taxonomy.Tags) -> str | None:
    """Чем лоты похожи: вид, иначе стиль."""
    if tags.species:
        return tags.species[0]
    if tags.styles:
        return tags.styles[0]
    return None


def _stale_lots(conn, now: float) -> list[dict]:
    types = ",".join(f"'{t}'" for t in selling.SELL_TYPES)
    rows = conn.execute(
        "SELECT i.*, a.n_bids AS a_n_bids, a.ended AS a_ended, a.last_bid_ts AS a_last FROM items i "
        "LEFT JOIN auctions a ON a.deviation_id = i.id "
        f"WHERE i.source = 'deviantart' AND i.own = 1 AND i.lot_type IN ({types}) "
        "AND COALESCE(i.published_ts, i.first_seen_ts) BETWEEN ? AND ? "
        "AND NOT EXISTS (SELECT 1 FROM sales s WHERE s.item_id = i.id AND s.status != 'cancelled') "
        "ORDER BY COALESCE(i.published_ts, i.first_seen_ts)",
        (now - selling.LOOKBACK_DAYS * DAY, now - STALE_DAYS * DAY)).fetchall()
    out = []
    for r in rows:
        why = selling._verdict(r, now, STALE_DAYS, [], selling.DEFAULT_TZ)
        if not why:
            continue
        unit, price = selling._unit(r)
        out.append({"item_id": r["id"], "title": r["title"], "url": r["url"], "lot_type": r["lot_type"],
                    "unit": unit, "price": price, "why": why, "key": _key(taxonomy.tag_item(r, use_snippet=False))})
    return out


def her_median(conn, unit: str, now: float | None = None, key: str | None = None,
               days: int = HISTORY_DAYS) -> tuple[float | None, evidence.Evidence]:
    """Медиана её оплаченных продаж в этой валюте за days дней (по виду/стилю key, если задан).
    (None, ev) — если продаж меньше порога evidence (уровень «нет вывода»)."""
    now = now or time.time()
    rows = [r for r in sales.list_sales(conn, ("paid", "transferred", "done"), since=now - days * DAY)
            if r["paid_ts"] and r["amount"] is not None and r["currency"] == unit
            and not sales.is_commission_part(r)]  # части оплаты заказа — не цена адопта
    if key:
        kept = []
        for r in rows:
            item = conn.execute("SELECT * FROM items WHERE id = ?", (r["item_id"],)).fetchone() if r["item_id"] else None
            tags = taxonomy.tag_item(item, use_snippet=False) if item else taxonomy.tag_text(r["title"])
            if key in tags.species or key in tags.styles:
                kept.append(r)
        rows = kept
    values = [float(r["amount"]) for r in rows]
    who = f"её продажи «{key}»" if key else "все её продажи"
    if not values:
        return None, evidence.span([], "her_sales", f"{who}, {unit}", unit, "observed", n=0, measure="price")
    interval = evidence.median_ci(values)
    med = statistics.median(values)
    ev = evidence.span([r["paid_ts"] for r in rows], "her_sales", f"{who}, {unit}", unit, "observed",
                       interval=interval, measure="price", value=med)
    if evidence.level(ev) == "нет вывода":
        return None, ev
    return med, ev


def bundle_suggestions(conn, now: float | None = None, limit: int = 3) -> list[dict]:
    """Наборы из её непроданных (дольше STALE_DAYS) похожих лотов: ≥2 одного вида/стиля в одной валюте.

    [{"key", "unit", "lots": [...], "median", "price", "evidence", "text"}]; price=None — «вывода нет»."""
    now = now or time.time()
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for lot in _stale_lots(conn, now):
        if lot["key"] and lot["unit"]:
            groups[(lot["key"], lot["unit"])].append(lot)
    out = []
    for (key, unit), lots in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(lots) < MIN_BUNDLE:
            continue
        median, ev = her_median(conn, unit, now, key)
        if median is None:
            median, ev = her_median(conn, unit, now)
        titles = ", ".join(f"«{lot['title'][:40]}»" for lot in lots[:4])
        if median is None:
            price = None
            what = f"её оплаченных продаж в {unit} за {HISTORY_DAYS} дн"
            why = (evidence.refuse(what, ev.n, 8) if ev.n < 8 else
                   f"Вывода нет: {what} — {ev.n}, но цены слишком разные, медиане верить рано.")
            text = f"Набор «{key}» из {len(lots)}: {titles}. Цена набора — {why} Назначь цену сама."
        else:
            price = forecast.round_price(median * len(lots), unit)
            text = (f"Набор «{key}» из {len(lots)}: {titles}. Медиана твоих продаж {sales.money(median, unit)} "
                    f"{evidence.tag(ev)} × {len(lots)} = {sales.money(price, unit)}; "
                    + evidence.heuristic("набор обычно дешевле суммы — скидку реши сама", "практика продавцов адоптов"))
        out.append({"key": key, "unit": unit, "lots": lots, "median": median, "price": price,
                    "evidence": evidence.tag(ev) if median is not None else None, "text": text})
        if len(out) >= limit:
            break
    return out


def render_bundles(found: list[dict]) -> str:
    if not found:
        return (f"Набора не собрать: нет хотя бы {MIN_BUNDLE} похожих непроданных лотов (один вид или стиль, "
                f"одна валюта, висят дольше {STALE_DAYS} дн).")
    lines = ["Можно предложить наборами (текст покупателям — твой, отправляешь сама):"]
    lines += [f"- {b['text']}" for b in found]
    return "\n".join(lines)


def payment_plan_suggestion(price: float | None, her_median_price: float | None, enabled: bool,
                            unit: str = "usd", holds_policy: str = "") -> dict | None:
    """Оплата 50/50 — только если она включила payment_plans и цена выше её медианы. Иначе None.
    Срок остатка — из её holds_policy; без него в тексте заглушка «<N> days», её надо заполнить."""
    if not enabled or price is None or her_median_price is None or price <= her_median_price:
        return None
    deposit = round(price / 2, 2)
    rest = round(price - deposit, 2)
    return {"deposit": deposit, "rest": rest,
            "text": (PLAN_SNIPPET_POLICY.format(deposit=sales.money(deposit, unit), rest=sales.money(rest, unit),
                                                policy=" ".join(holds_policy.split())) if holds_policy.strip() else
                     PLAN_SNIPPET.format(deposit=sales.money(deposit, unit), rest=sales.money(rest, unit))),
            "note": evidence.heuristic(f"дорогой лот проще продать с оплатой частями; {PLAN_RULE}",
                                       "практика продавцов адоптов")}
