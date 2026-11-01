"""Отчёт за месяц одной HTML-страницей: доход, форматы и площадки, заработок в час, ниши, воронка, цель.

Страница самодостаточная: графики — встроенный SVG, ни скриптов, ни шрифтов, ни ссылок наружу. Светлая и
тёмная тема по настройке системы, на телефоне читается без горизонтальной прокрутки. Лежит у неё в
Загрузках (~/Downloads/mcp-devian-report-ГГГГ-ММ.html) и никуда не отправляется.

Правила цифр те же, что везде (evidence.py): у каждого графика n и период; валюты не складываем и по
курсу не пересчитываем; сравнение форматов и заработка в час — только при достаточных данных, иначе
«Вывода нет». Ников покупателей в отчёте нет: его могут переслать. Весь текст экранируется.
"""

from __future__ import annotations

import calendar
import html
import importlib
import math
import os
import re
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import evidence
from .sales import CURRENCIES, POINTS_NOT_CASH, deal_key, money

DEFAULT_TZ = "Europe/Moscow"
MARKER = "mcp_devian-report"
PAID = ("paid", "transferred", "done")
MIN_FORMAT_N = evidence.MIN_SIDE     # продаж каждого формата, чтобы сравнивать форматы
MIN_HOURLY_N = evidence.MIN_SIDE     # работ с отмеченными часами в формате (как earnings.MIN_WORKS)
MIN_HOURLY_HOURS = 3.0               # и хотя бы столько часов
NICHES_SHOWN = 8
_NAME_TRIES = 50
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь")
FORMAT_NAMES = {"auction": "аукционы", "flatsale": "фикс-цена", "ota": "OTA", "bta": "BTA", "raffle": "рафлы",
                "adopt": "адопты", "other": "другое",
                "bundle": "наборы", "ych": "YCH", "commission": "коммишены"}
PLATFORM_NAMES = {"deviantart": "DeviantArt", "toyhouse": "Toyhouse", "other": "другое"}
_MONTH_RE = (re.compile(r"^\s*(\d{4})[-/.](\d{1,2})\s*$"), re.compile(r"^\s*(\d{1,2})[-/.](\d{4})\s*$"))


class ReportError(ValueError):
    """Понятная ей причина, почему отчёта не будет."""


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def plural(n: int, one: str, few: str, many: str) -> str:
    """1 продажа, 2 продажи, 5 продаж."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _sales_word(n: int) -> str:
    return f"{n} {plural(n, 'продажа', 'продажи', 'продаж')}"


def _zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def previous_month(tz: str = DEFAULT_TZ, now: float | None = None) -> str:
    """«ГГГГ-ММ» прошлого месяца в её часовом поясе: «отчёт за месяц» — обычно за закончившийся."""
    today = datetime.fromtimestamp(now or time.time(), _zone(tz))
    year, mon = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{year:04d}-{mon:02d}"


def month_bounds(month: str | None, tz: str = DEFAULT_TZ, now: float | None = None) -> tuple[float, float, str]:
    """(начало, конец — начало следующего месяца, «ГГГГ-ММ») в её часовом поясе. Пусто — текущий месяц."""
    zone = _zone(tz)
    today = datetime.fromtimestamp(now or time.time(), zone)
    month = (month or "").strip()
    if not month:
        year, mon = today.year, today.month
    else:
        m = _MONTH_RE[0].match(month)
        if m:
            year, mon = int(m.group(1)), int(m.group(2))
        else:
            m = _MONTH_RE[1].match(month)
            if not m:
                raise ReportError("Месяц — как «2026-10» или «10.2026».")
            year, mon = int(m.group(2)), int(m.group(1))
    if not 1 <= mon <= 12 or year < 2000:
        raise ReportError("Месяц — как «2026-10» или «10.2026».")
    if (year, mon) > (today.year, today.month):
        raise ReportError("Этот месяц ещё не начался.")
    start = datetime(year, mon, 1, tzinfo=zone)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=zone)
    return start.timestamp(), end.timestamp(), f"{year:04d}-{mon:02d}"


def _d(ts: float, zone: ZoneInfo, year: bool = False) -> str:
    return datetime.fromtimestamp(ts, zone).strftime("%d.%m.%Y" if year else "%d.%m")


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# --- Сбор данных ---

def _sales_rows(conn, since: float, until: float) -> list:
    return conn.execute(
        "SELECT * FROM sales WHERE status IN ('paid', 'transferred', 'done') AND paid_ts IS NOT NULL "
        "AND paid_ts >= ? AND paid_ts < ?", (since, until)).fetchall()


def _bump(b: dict, seen: dict, key: tuple, r) -> None:
    """+1 к числу продаж, если это новая сделка: предоплата и остаток одного заказа — одна продажа."""
    deals = seen.setdefault(key, set())
    if deal_key(r) not in deals:
        deals.add(deal_key(r))
        b["count"] += 1


def _income(rows, zone: ZoneInfo, days: int) -> dict:
    gross: dict[str, dict] = {}
    net: dict[str, dict] = {}
    seen: dict[tuple, set] = {}
    daily = [0.0] * days
    missing_net: set[str] = set()
    no_currency: set[str] = set()
    for r in rows:
        cur = r["currency"]
        if r["amount"] is not None and cur in CURRENCIES:
            g = gross.setdefault(cur, {"total": 0.0, "count": 0})
            g["total"] = round(g["total"] + r["amount"], 2)
            _bump(g, seen, ("gross", cur), r)
        elif r["amount"] is not None:
            no_currency.add(deal_key(r))
        if r["net_amount"] is not None:
            nc = r["net_currency"] or "rub"
            b = net.setdefault(nc, {"total": 0.0, "count": 0})
            b["total"] = round(b["total"] + r["net_amount"], 2)
            _bump(b, seen, ("net", nc), r)
            if nc == "rub":
                day = datetime.fromtimestamp(r["paid_ts"], zone).day - 1
                if 0 <= day < days:
                    daily[day] += r["net_amount"]
        elif cur != "points" and r["amount"] is not None:
            missing_net.add(deal_key(r))
    return {"gross": gross, "net": net, "daily_rub": daily, "missing_net": len(missing_net),
            "no_currency": len(no_currency)}


def _breakdown(rows, key: str) -> list[dict]:
    """По формату или площадке: число оплаченных продаж (сделок), суммы по валютам, сколько дошло в ₽."""
    out: dict[str, dict] = {}
    seen: dict[tuple, set] = {}
    for r in rows:
        k = r[key] or "other"
        b = out.setdefault(k, {"key": k, "count": 0, "gross": defaultdict(float), "net_rub": 0.0, "net_n": 0})
        _bump(b, seen, ("all", k), r)
        if r["amount"] is not None and r["currency"]:
            b["gross"][r["currency"]] += r["amount"]
        if r["net_amount"] is not None and (r["net_currency"] or "rub") == "rub":
            b["net_rub"] += r["net_amount"]
            if deal_key(r) not in seen.setdefault(("net", k), set()):
                seen[("net", k)].add(deal_key(r))
                b["net_n"] += 1
    return sorted(out.values(), key=lambda b: (-b["count"], b["key"]))


def _level_by_n(n: int) -> str:
    """Уверенность по одному n (интервала нет): те же пороги, что evidence.level."""
    ev = evidence.Evidence(n=n, n_eff=None, since_ts=0.0, until_ts=0.0, data_as_of=0.0, source="her_sales",
                           population="её работы", unit="rub", kind="observed")
    return evidence.level(ev)


def _hourly(entries) -> list[dict]:
    """Нормализуем то, что дал учёт часов: {label, hours, net_rub | rate, n, level?} → rate и уверенность только
    при достаточных данных. Есть level (от earnings) — его решение главнее; нет — те же пороги evidence.py."""
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        label = " ".join(str(e.get("label") or e.get("format") or "").split())[:80] or "без названия"
        hours = _num(e.get("hours"))
        n = int(_num(e.get("n")) or 0)
        net = _num(e.get("net_rub", e.get("income")))
        rate = _num(e.get("rate"))
        if rate is None and net is not None and hours:
            rate = net / hours
        level = str(e.get("level") or "")
        from_earnings = bool(level)
        if from_earnings:  # учёт часов уже решил, хватает ли данных (evidence.level) — его решение главнее
            enough = rate is not None and level != "нет вывода"
        else:
            enough = bool(hours and hours >= MIN_HOURLY_HOURS and n >= MIN_HOURLY_N and rate is not None)
            level = _level_by_n(n) if enough else "нет вывода"
        if not enough:
            level = "нет вывода"
        out.append({"label": label, "hours": hours, "n": n, "net_rub": net, "level": level,
                    "from_earnings": from_earnings,
                    "rate": round(rate) if enough and rate is not None else None})
    return sorted(out, key=lambda r: (r["rate"] is None, -(r["rate"] or 0), r["label"]))


def _need_text(need) -> str:
    bits = []
    for d in need or []:
        if isinstance(d, dict) and _num(d.get("count")):
            if d.get("level") == "нет вывода":
                continue
            fmt = str(d.get("format") or "")
            name = "все форматы" if fmt == "all" else FORMAT_NAMES.get(fmt, fmt)
            sure = f" (уверенность {d['level']}, n={int(_num(d.get('n')) or 0)})" if d.get("level") else ""
            bits.append(f"ещё ~{int(_num(d.get('count')) or 0)} × {name}{sure}")
    return ("Чтобы добрать по нынешним ценам: " + " или ".join(bits) + ".") if bits else ""


def earnings_inputs(conn, label: str, now: float, tz: str = DEFAULT_TZ) -> tuple[dict | None, dict | None]:
    """Часы и цель из учёта часов (earnings.py), если он есть: (hourly, goal) в формате collect().
    Любая ошибка там — (None, None) для этой части: отчёт важнее одной секции."""
    try:
        earnings = importlib.import_module(f"{__package__}.earnings")
    except ImportError:
        return None, None
    hourly = goal = None
    zone = _zone(tz)
    try:
        rep = earnings.hourly(conn, now)
        rows = []
        for g in getattr(rep, "formats", None) or []:
            key = str(getattr(g, "key", "") or "")
            label_ = getattr(g, "label", None) or getattr(g, "name", None) or FORMAT_NAMES.get(key, key)
            n = getattr(g, "n", None)
            if n is None:  # earnings.Group: работы в расчёте ₽/ч
                n = len(getattr(g, "rub", None) or []) or getattr(g, "paid_n", 0)
            net = getattr(g, "net", None)
            rows.append({"label": label_, "rate": getattr(g, "rate", None), "level": getattr(g, "level", None),
                         "hours": getattr(g, "hours", None), "n": n or 0,
                         "net_rub": net if net is not None else getattr(g, "income", None)})
        if rows:
            days = getattr(rep, "days", None)
            span_ = f"за {days} дн" if isinstance(days, int) else "все отмеченные часы"
            hourly = {"rows": rows, "period": f"{span_} по {_d(now, zone, True)}",
                      "source": "учёт часов: дошло в ₽ ÷ часы"}
    except Exception:  # noqa: BLE001
        hourly = None
    try:
        if month_bounds(None, tz, now)[2] == label:
            st = earnings.goal_status(conn, now, tz)
            if isinstance(st, dict) and _num(st.get("target")):
                goal = {"target": st["target"], "currency": "rub", "done": st.get("got"),
                        "plan": _need_text(st.get("need"))}
        else:
            row = conn.execute("SELECT target FROM earn_goals WHERE month = ?", (label,)).fetchone()
            if row and _num(row[0]):
                goal = {"target": row[0], "currency": "rub"}
    except Exception:  # noqa: BLE001
        goal = None
    return hourly, goal


def _funnel(conn, since: float, until: float, now: float) -> dict:
    from . import funnel

    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM items WHERE source = 'deviantart' AND own = 1 "
        "AND COALESCE(published_ts, first_seen_ts) >= ? AND COALESCE(published_ts, first_seen_ts) < ?",
        (since, until)).fetchall()]
    out: dict[str, Any] = {"lots": 0, "views": 0, "favs": 0, "stats_n": 0, "with_bids": 0, "sold": 0, "ts": []}
    for iid in ids:
        f = funnel.lot_funnel(conn, iid, now)
        if not f:
            continue
        out["lots"] += 1
        out["ts"].append(f["published_ts"])
        if f["views"] is not None:
            out["views"] += int(f["views"] or 0)
            out["favs"] += int(f["favs"] or 0)
            out["stats_n"] += 1
        out["with_bids"] += 1 if f["bids"] else 0
        out["sold"] += 1 if f["sold"] else 0
    return out


def _niches(conn, now: float, niche_report) -> dict:
    from . import forecast

    rep = niche_report
    if rep is None:
        try:
            rep = forecast.niches(conn, now)
        except Exception as e:  # noqa: BLE001 — отчёт важнее одной секции
            return {"refusal": f"Вывода нет: прогноз по нишам сейчас не посчитать ({type(e).__name__})."}
    refusal = getattr(rep, "refusal", None)
    n_lots = int(getattr(rep, "n_lots", 0) or 0)
    if refusal:
        return {"refusal": refusal}
    if n_lots < forecast.MIN_TRAIN:
        return {"refusal": evidence.refuse("доля аукционов со ставкой по нишам",
                                           f"{n_lots} завершённых аукционов рынка", forecast.MIN_TRAIN)}
    rows = list(getattr(rep, "niches", []) or [])
    if not rows:
        return {"refusal": f"Вывода нет: ни у одной ниши нет хотя бы {evidence.MIN_SIDE} завершённых аукционов "
                           f"(всего аукционов рынка: {n_lots})."}
    rows = sorted(rows, key=lambda d: ({"sells": 0, "neutral": 1, "saturated": 2}.get(d.get("verdict"), 1),
                                       -(d.get("lots") or 0)))[:NICHES_SHOWN]
    out = []
    for d in rows:
        key = str(d.get("key") or "")
        try:
            name = forecast._niche_name(key)
        except Exception:  # noqa: BLE001
            name = key
        ev = d.get("ev")
        out.append({"name": name, "lots": int(d.get("lots") or 0), "sold": int(d.get("sold") or 0),
                    "p": float(d.get("p_sold") or 0.0), "verdict": d.get("verdict") or "neutral",
                    "confidence": d.get("confidence") or (evidence.level(ev) if ev else ""),
                    "interval": d.get("interval")})
    cutoff = getattr(rep, "cutoff_ts", None)
    return {"refusal": None, "n_lots": n_lots, "rows": out, "cutoff": cutoff,
            "tested": getattr(rep, "tested", len(rows))}


def _goal(goal, net: dict) -> dict | None:
    if not isinstance(goal, dict):
        return None
    target = _num(goal.get("target"))
    cur = str(goal.get("currency") or "rub").lower()
    if not target or target <= 0 or cur not in CURRENCIES or cur == "points":
        return None
    done = _num(goal.get("done"))
    if done is None:
        done = (net.get(cur) or {}).get("total", 0.0)
    pct = round(100 * done / target)
    return {"target": target, "currency": cur, "done": done, "pct": pct,
            "left": max(0.0, target - done), "plan": " ".join(str(goal.get("plan") or "").split())[:400]}


def collect(conn, month: str | None = None, *, tz: str = DEFAULT_TZ, now: float | None = None, hourly=None,
            goal: dict | None = None, niche_report=None, use_earnings: bool = True) -> dict:
    """Все цифры отчёта. hourly — [{label, hours, net_rub|rate, n, level?}] или {"rows", "period", "source"};
    goal — {target, currency, done?, plan?}; niche_report — forecast.NicheReport (None — посчитать здесь).
    use_earnings — чего не передали, взять из учёта часов (earnings.py), если он есть."""
    now = now or time.time()
    zone = _zone(tz)
    since, until, label = month_bounds(month, tz, now)
    if use_earnings and (hourly is None or goal is None):
        e_hourly, e_goal = earnings_inputs(conn, label, now, tz)
        hourly = e_hourly if hourly is None else hourly
        goal = e_goal if goal is None else goal
    hourly_period = hourly_source = None
    if isinstance(hourly, dict):
        hourly_period, hourly_source = hourly.get("period"), hourly.get("source")
        hourly = hourly.get("rows")
    days = calendar.monthrange(int(label[:4]), int(label[5:]))[1]
    rows = _sales_rows(conn, since, until)
    inc = _income(rows, zone, days)
    last = min(until, now) - 1
    partial = now < until
    return {
        "label": label, "since": since, "until": until, "last": last, "partial": partial, "tz": zone.key,
        "now": now, "days": days,
        "month_name": f"{MONTHS[int(label[5:]) - 1]} {label[:4]}",
        "period": f"{_d(since, zone)}–{_d(last, zone, True)}",
        "paid_n": len({deal_key(r) for r in rows}), "paid_ts": [r["paid_ts"] for r in rows],
        **inc,
        "formats": _breakdown(rows, "format"), "platforms": _breakdown(rows, "platform"),
        "hourly": _hourly(hourly), "hourly_given": hourly is not None,
        "hourly_period": str(hourly_period or "")[:80], "hourly_source": str(hourly_source or "")[:80],
        "funnel": _funnel(conn, since, until, now),
        "niches": _niches(conn, now, niche_report),
        "goal": _goal(goal, inc["net"]),
    }


# --- Отрисовка ---

CSS = """
:root{color-scheme:light;--bg:#f6f5f2;--surface:#fcfcfb;--text:#0b0b0b;--text-2:#52514e;--muted:#7a7974;
--grid:#e4e3df;--s1:#2a78d6;--s2:#eb6834;--track:#e9e8e4;--good:#0ca30c;--warn:#b87d00}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#121211;
--surface:#1a1a19;--text:#f0efec;--text-2:#c3c2b7;--muted:#9a9990;--grid:#2e2e2c;--s1:#3987e5;--s2:#d95926;
--track:#2a2a28;--good:#3fc23f;--warn:#fab219}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121211;--surface:#1a1a19;--text:#f0efec;--text-2:#c3c2b7;
--muted:#9a9990;--grid:#2e2e2c;--s1:#3987e5;--s2:#d95926;--track:#2a2a28;--good:#3fc23f;--warn:#fab219}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%}
main{max-width:860px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.15rem;margin:0 0 8px}
.sub,.cap,.note{color:var(--text-2)}.sub{margin:0 0 20px}.cap{font-size:.85rem;margin:6px 0 0}
.note{font-size:.9rem;margin:8px 0 0}
section{background:var(--surface);border:1px solid var(--grid);border-radius:12px;padding:16px;margin:0 0 16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 16px}
.tile{background:var(--surface);border:1px solid var(--grid);border-radius:12px;padding:12px 14px}
.tile b{display:block;font-size:1.5rem;font-variant-numeric:tabular-nums}.tile span{color:var(--text-2);font-size:.85rem}
svg{display:block;width:100%;height:auto;margin-top:8px}
svg text{fill:var(--text-2);font-size:13px;font-family:inherit}svg .v{fill:var(--text)}
.bar{fill:var(--s1)}.bar2{fill:var(--s2)}.track{fill:var(--track)}.axis{stroke:var(--grid)}
table{width:100%;border-collapse:collapse;font-size:.9rem;margin-top:8px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid)}td.n,th.n{text-align:right}
details{margin-top:8px}summary{cursor:pointer;color:var(--text-2);font-size:.9rem}
.refuse{color:var(--text-2);border-left:3px solid var(--warn);padding:4px 10px;margin:8px 0 0}
.progress{height:14px;background:var(--track);border-radius:7px;overflow:hidden;margin-top:10px}
.progress i{display:block;height:100%;background:var(--good);border-radius:7px}
.tablewrap{overflow-x:auto}
"""


def _cap(n: int, what: str, period: str, source: str) -> str:
    return f'<p class="cap">n={n} {_e(what)} · {_e(period)} · {_e(source)}</p>'


def _hbars(rows: list[tuple[str, float, str]], css: str = "bar", value_w: int = 120) -> str:
    """Горизонтальные полосы: (подпись, значение, текст значения). Подписи и значения — текстом рядом."""
    if not rows:
        return ""
    width, label_w, row_h = 600, 170, 30
    plot = width - label_w - value_w
    top = max((v for _, v, _ in rows), default=0) or 1
    parts = [f'<svg viewBox="0 0 {width} {row_h * len(rows) + 4}" role="img" '
             f'aria-label="{_e(", ".join(f"{lbl}: {txt}" for lbl, _, txt in rows))}">']
    for i, (label, value, text) in enumerate(rows):
        y = i * row_h + 4
        short = label if len(label) <= 22 else label[:21] + "…"
        w = max(0.0, value) / top * plot
        parts.append(f'<g><title>{_e(label)}: {_e(text)}</title>'
                     f'<text x="0" y="{y + 17}">{_e(short)}</text>'
                     f'<rect class="track" x="{label_w}" y="{y + 6}" width="{plot}" height="14" rx="4"/>'
                     + (f'<rect class="{css}" x="{label_w}" y="{y + 6}" width="{w:.1f}" height="14" rx="4"/>'
                        if w > 0 else "")
                     + f'<text class="v" x="{label_w + plot + 8}" y="{y + 17}">{_e(text)}</text></g>')
    parts.append("</svg>")
    return "".join(parts)


def _day_bars(values: list[float]) -> str:
    """Дневной доход в ₽: столбцы по дням месяца, подписи через 5 дней и у максимума."""
    width, height, base, left = 600, 170, 140, 8
    n = len(values)
    step = (width - left * 2) / n
    top = max(values) or 1
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Сколько дошло в рублях по дням">',
             f'<line class="axis" x1="{left}" y1="{base}" x2="{width - left}" y2="{base}"/>']
    peak = values.index(max(values)) if max(values) > 0 else -1
    for i, v in enumerate(values):
        x = left + i * step
        h = v / top * (base - 24)
        if v > 0:
            parts.append(f'<g><title>{i + 1}-е: {_e(money(v, "rub"))}</title>'
                         f'<rect class="bar" x="{x + 1:.1f}" y="{base - h:.1f}" width="{max(1.0, step - 2):.1f}" '
                         f'height="{h:.1f}" rx="2"/></g>')
        if i == peak:
            parts.append(f'<text class="v" x="{x + step / 2:.1f}" y="{base - h - 6:.1f}" text-anchor="middle">'
                         f'{_e(money(v, "rub"))}</text>')
        if i == 0 or (i + 1) % 5 == 0:
            parts.append(f'<text x="{x + step / 2:.1f}" y="{base + 18}" text-anchor="middle">{i + 1}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _table(head: list[str], rows: list[list[str]], numeric_from: int = 1) -> str:
    th = "".join(f'<th{" class=n" if i >= numeric_from else ""}>{_e(h)}</th>' for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td{" class=n" if i >= numeric_from else ""}>{_e(c)}</td>'
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _details(head, rows, numeric_from: int = 1) -> str:
    return f"<details><summary>Таблица</summary>{_table(head, rows, numeric_from)}</details>"


def _refuse(text: str) -> str:
    return f'<p class="refuse">{_e(text)}</p>'


def _gross_text(gross: dict) -> str:
    return ", ".join(money(v, c) for c, v in sorted(gross.items(), key=lambda kv: (kv[0] == "points", kv[0])) if v)


def _income_section(d: dict) -> str:
    out = ['<section id="income"><h2>Доход</h2>']
    if not d["paid_n"]:
        out.append(f'<p class="note">Оплаченных продаж нет за {_e(d["period"])}.</p></section>')
        return "".join(out)
    rub = d["net"].get("rub")
    if rub:
        out.append(f'<p>Дошло до тебя: <b>{_e(money(rub["total"], "rub"))}</b> ({_e(_sales_word(rub["count"]))}).</p>')
        out.append(_day_bars(d["daily_rub"]))
        out.append(_cap(rub["count"], "оплаченных продаж с суммой «дошло» в ₽", d["period"],
                        "по дню оплаты, её записи"))
        out.append(_details(["День", "Дошло"], [[str(i + 1), money(v, "rub")] for i, v in enumerate(d["daily_rub"])
                                                if v]))
    else:
        out.append(_refuse("Сколько дошло в ₽, не вписано ни по одной продаже — дневной график не из чего строить "
                           "(sales:status sale_id=… net=…)."))
    lines = []
    for cur, g in sorted(d["gross"].items(), key=lambda kv: (kv[0] == "points", kv[0])):
        tail = f" — {POINTS_NOT_CASH}" if cur == "points" else ""
        lines.append(f"<li>{_e(money(g['total'], cur))} — {_e(_sales_word(g['count']))}{_e(tail)}</li>")
    for cur, b in d["net"].items():
        if cur != "rub":
            lines.append(f"<li>дошло {_e(money(b['total'], cur))} — {_e(_sales_word(b['count']))}</li>")
    out.append("<p class=note>Оплатили покупатели (по валютам; валюты не складываю и по курсу не пересчитываю):</p>"
               f"<ul>{''.join(lines)}</ul>")
    if d["missing_net"]:
        out.append(f'<p class="note">Продаж без суммы «дошло» в ₽: {d["missing_net"]} — в рублёвый итог '
                   "они не вошли.</p>")
    if d["no_currency"]:
        out.append(f'<p class="note">Ещё {d["no_currency"]} продаж без валюты — в суммы не входят.</p>')
    out.append("</section>")
    return "".join(out)


def _breakdown_rows(items: list[dict], names: dict) -> list[list[str]]:
    return [[names.get(b["key"], b["key"]), str(b["count"]), _gross_text(b["gross"]) or "—",
             money(b["net_rub"], "rub") if b["net_n"] else "—"] for b in items]


def _formats_section(d: dict) -> str:
    out = ['<section id="formats"><h2>Продажи по форматам и площадкам</h2>']
    if not d["paid_n"]:
        out.append('<p class="note">Оплаченных продаж нет — разбивать нечего.</p></section>')
        return "".join(out)
    for title, one, items, names in (("Форматы", "Формат", d["formats"], FORMAT_NAMES),
                                     ("Площадки", "Площадка", d["platforms"], PLATFORM_NAMES)):
        out.append(f"<h3>{_e(title)}</h3>")
        out.append(_hbars([(names.get(b["key"], b["key"]), b["count"], _sales_word(b["count"])) for b in items]))
        out.append(_cap(d["paid_n"], "оплаченных продаж", d["period"], "её записи продаж"))
        out.append(_details([one, "Продаж", "Оплатили", "Дошло ₽"],
                            _breakdown_rows(items, names)))
    small = [b for b in d["formats"] if b["count"] < MIN_FORMAT_N]
    if len(d["formats"]) > 1 and small:
        out.append(_refuse(evidence.refuse("сравнивать форматы, какой выгоднее",
                                           f"у формата «{FORMAT_NAMES.get(small[0]['key'], small[0]['key'])}» "
                                           f"{small[0]['count']} продаж", f"{MIN_FORMAT_N} у каждого")))
    out.append("</section>")
    return "".join(out)


def _hourly_section(d: dict) -> str:
    out = ['<section id="hourly"><h2>Заработок в час</h2>']
    rows = d["hourly"]
    if not rows:
        out.append('<p class="note">Часы на адопты за этот период не отмечены — посчитать заработок в час не из '
                   "чего. Отмечай, сколько рисовала каждый адопт, и здесь появится «₽ в час» по форматам.</p>")
        out.append("</section>")
        return "".join(out)
    good = [r for r in rows if r["rate"] is not None]
    if good:
        out.append(_hbars([(r["label"], r["rate"], f"{r['rate']:,} ₽/ч · уверенность {r['level']}") for r in good],
                          "bar2", value_w=230))
        hours = sum(r["hours"] or 0 for r in good)
        out.append(_cap(sum(r["n"] for r in good), f"работ с отмеченными часами ({hours:g} ч)",
                        d["hourly_period"] or d["period"], d["hourly_source"] or "дошло в ₽ ÷ её часы"))
        if any(r["level"] == "низкая" for r in good):
            out.append('<p class="note">Уверенность «низкая» — работ пока мало: это ориентир, не вывод.</p>')
    for r in rows:
        if r["rate"] is None:
            works = f"{r['n']} {plural(r['n'], 'работа', 'работы', 'работ')}"
            if r["from_earnings"]:  # порог учёта часов (earnings.MIN_WORKS), не свой
                have, need = works, f"{MIN_HOURLY_N} {plural(MIN_HOURLY_N, 'работы', 'работ', 'работ')}"
            else:
                have = f"{works}, {r['hours'] or 0:g} ч"
                need = f"{MIN_HOURLY_N} {plural(MIN_HOURLY_N, 'работы', 'работ', 'работ')} и {MIN_HOURLY_HOURS:g} ч"
            out.append(_refuse(evidence.refuse(f"₽ в час для «{r['label']}»", have, need)))
    out.append(_details(["Формат", "Работ", "Часов", "Дошло", "₽/ч", "Уверенность"],
                        [[r["label"], str(r["n"]), f"{r['hours'] or 0:g}",
                          money(r["net_rub"], "rub") if r["net_rub"] is not None else "—",
                          f"{r['rate']:,}" if r["rate"] is not None else "вывода нет", r["level"]]
                         for r in rows]))
    out.append("</section>")
    return "".join(out)


def _niches_section(d: dict) -> str:
    n = d["niches"]
    out = ['<section id="niches"><h2>Спрос по нишам (рынок DA)</h2>']
    if n.get("refusal"):
        out.append(_refuse(n["refusal"]) + "</section>")
        return "".join(out)
    verdict = {"sells": "чаще остальных", "saturated": "реже, много предложения", "neutral": "разница не доказана"}
    out.append(_hbars([(r["name"], r["p"], f"{round(r['p'] * 100)}% ({r['sold']} из {r['lots']})")
                       for r in n["rows"]]))
    zone = _zone(d["tz"])
    upto = _d(n["cutoff"] or d["now"], zone, True)
    out.append(_cap(n["n_lots"], "завершённых аукционов рынка DA, доля получивших ставку", f"по {upto}",
                    "рынок DA, оценка"))
    out.append(_details(["Ниша", "Лотов", "Со ставкой", "Доля", "Вывод", "Уверенность"],
                        [[r["name"], str(r["lots"]), str(r["sold"]), f"{round(r['p'] * 100)}%",
                          verdict.get(r["verdict"], r["verdict"]), r["confidence"]] for r in n["rows"]]))
    out.append('<p class="note">Это рынок, не её лоты; оценка, не гарантия.</p></section>')
    return "".join(out)


def _funnel_section(d: dict) -> str:
    f = d["funnel"]
    out = ['<section id="funnel"><h2>Воронка её лотов DA</h2>']
    if not f["lots"]:
        out.append('<p class="note">В этом месяце её лотов DA в базе нет.</p></section>')
        return "".join(out)
    steps = [("Просмотры", f["views"]), ("Фавы", f["favs"]), ("Лоты со ставкой", f["with_bids"]),
             ("Оплачено", f["sold"])]
    out.append(_hbars([(name, v, f"{v:,}") for name, v in steps]))
    out.append(_cap(f["lots"], "её лотов DA, выложенных в месяце", d["period"],
                    f"просмотры и фавы известны у {f['stats_n']} (последний снимок)"))
    bits = []
    if f["views"]:
        bits.append(f"фавы от просмотров {f['favs'] / f['views'] * 100:.1f}%")
    bits.append(f"со ставкой {f['with_bids']} из {f['lots']}")
    bits.append(f"оплачено {f['sold']} из {f['lots']}")
    out.append(f'<p class="note">{_e("; ".join(bits))}.</p>')
    if f["lots"] < evidence.MIN_SIDE:
        lots = f"{f['lots']} {plural(f['lots'], 'лот', 'лота', 'лотов')} за месяц"
        out.append(_refuse(evidence.refuse("где теряются покупатели (сравнение с её прошлыми лотами)", lots,
                                           evidence.MIN_SIDE)))
    out.append("</section>")
    return "".join(out)


def _goal_section(d: dict) -> str:
    g = d["goal"]
    out = ['<section id="goal"><h2>Цель на месяц</h2>']
    if not g:
        out.append('<p class="note">Цель на месяц не задана. Скажи, например, «хочу 30 000 ₽», и здесь будет '
                   "видно, сколько уже есть.</p></section>")
        return "".join(out)
    pct = g["pct"]
    out.append(f'<p>{_e(money(g["done"], g["currency"]))} из {_e(money(g["target"], g["currency"]))} — '
               f"<b>{pct}%</b>" + (f", осталось {_e(money(g['left'], g['currency']))}" if g["left"] else
                                   ", цель взята") + ".</p>")
    out.append(f'<div class="progress" role="img" aria-label="{pct}%"><i style="width:{min(100, max(0, pct))}%">'
               "</i></div>")
    n = (d["net"].get(g["currency"]) or {}).get("count", 0)
    sign = {"rub": "₽", "usd": "$", "eur": "€", "gbp": "£"}.get(g["currency"], g["currency"].upper())
    out.append(_cap(n, f"продаж с суммой «дошло» в {sign}", d["period"], "её записи"))
    if g["plan"]:
        out.append(f'<p class="note">{_e(g["plan"])}</p>')
    out.append("</section>")
    return "".join(out)


def render_html(d: dict) -> str:
    zone = _zone(d["tz"])
    title = f"Отчёт за {d['month_name']}"
    rub = d["net"].get("rub")
    tiles = [(money(rub["total"], "rub") if rub else "—", "дошло до тебя"),
             (str(d["paid_n"]), "оплаченных продаж"),
             (str(d["funnel"]["lots"]), "лотов DA выложено")]
    if d["goal"]:
        tiles.append((f"{d['goal']['pct']}%", "цели месяца"))
    made = datetime.fromtimestamp(d["now"], zone).strftime("%d.%m.%Y %H:%M")
    sub = (f"{d['period']}" + (" (месяц ещё идёт)" if d["partial"] else "") + f" · собрано {made} ({zone.key}) · "
           "цифры из её записей и рынка DA, валюты не складываю")
    body = [
        f'<header><h1>{_e(title)}</h1><p class="sub">{_e(sub)}</p></header>',
        '<div class="tiles">' + "".join(f'<div class="tile"><b>{_e(v)}</b><span>{_e(k)}</span></div>'
                                        for v, k in tiles) + "</div>",
        _goal_section(d), _income_section(d), _formats_section(d), _hourly_section(d), _funnel_section(d),
        _niches_section(d),
        '<footer><p class="note">Файл лежит только у тебя на компьютере; ников покупателей здесь нет. '
        "Рынок — оценка, не гарантия.</p></footer>",
    ]
    return ("<!doctype html>\n<html lang=\"ru\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="generator" content="{MARKER}"><title>{_e(title)}</title>'
            f"<style>{CSS}</style></head><body><main>{''.join(body)}</main></body></html>\n")


# --- Файл ---

def _short(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s


def _is_ours(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f'content="{MARKER}"' in f.read(4096)
    except OSError:
        return False


def _out_path(folder: Path, label: str) -> Path:
    for i in range(1, _NAME_TRIES + 1):
        out = folder / f"mcp-devian-report-{label}{'' if i == 1 else f'-{i}'}.html"
        if not out.exists() or (out.is_file() and _is_ours(out)):
            return out
    raise ReportError(f"В {_short(folder)} уже {_NAME_TRIES} файлов отчёта за {label} — разбери папку.")


def build(conn, month: str | None = None, *, out_dir: str | Path | None = None, tz: str = DEFAULT_TZ,
          now: float | None = None, hourly=None, goal: dict | None = None, niche_report=None,
          use_earnings: bool = True) -> dict:
    """Собрать и записать отчёт. {"path", "summary"}. Свой прошлый отчёт за месяц перезаписываем, чужой файл — нет."""
    data = collect(conn, month, tz=tz, now=now, hourly=hourly, goal=goal, niche_report=niche_report,
                   use_earnings=use_earnings)
    page = render_html(data)
    folder = Path(out_dir).expanduser() if out_dir else Path.home() / "Downloads"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        out = _out_path(folder, data["label"])
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".report-", suffix=".html")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(page)
            os.replace(tmp, out)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise ReportError(f"Не получилось записать отчёт в {_short(folder)} — нет доступа или места.") from e
    rub = data["net"].get("rub")
    bits = [f"Отчёт за {data['month_name']}: {_short(out)} — открой в браузере (двойной клик)."]
    if data["paid_n"]:
        bits.append(f"Оплаченных продаж: {data['paid_n']}" + (f", дошло {money(rub['total'], 'rub')}" if rub else "")
                    + ".")
    else:
        bits.append("Оплаченных продаж за месяц нет.")
    if data["goal"]:
        bits.append(f"Цель: {data['goal']['pct']}%.")
    bits.append("Файл только у неё на компьютере, никуда не отправлен.")
    return {"path": str(out), "summary": " ".join(bits)}
