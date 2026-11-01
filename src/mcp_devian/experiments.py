"""Журнал её экспериментов: «старт $5 против $10», «ссылка в посте против ссылки в ответе», «время» и т.п.

Она сама решает, какой лот в какую ветку. Помощник только считает: в каждой ветке n, долю «получили ставку
или продан» (Уилсон), медиану ставки (бутстрэп) и дни до закрытия. «Вероятно лучше» — только через
evidence.compare (≥ MIN_SIDE завершённых лотов в каждой ветке и P выше порога); иначе «пока не различить».
Всегда перечисляем, что ещё различается между ветками (день недели, группы, виды): это не чистый опыт.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db, evidence

DAY = 86400
DEFAULT_TZ = "Europe/Moscow"
PAID = ("paid", "transferred", "done")
AUCTION_TYPES = ("auction", "bta")
UNSOLD_AFTER_DAYS = 14     # лот не-аукцион без продажи столько дней — итог «не продан» (иначе висел бы вечно)
UNRESOLVED_MAX = 0.3       # в ветке итог неизвестен у большей доли лотов — вывода нет: ранние продажи завышают долю
VARIABLES = {"sb": "старт (SB)", "ab": "автобай (AB)", "time": "время публикации", "group": "группы",
             "thumbnail": "превью", "palette": "палитра", "title": "название", "link": "где ссылка (пост / ответ)",
             "format": "формат лота", "price": "цена", "other": "другое"}
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    variable TEXT NOT NULL,        -- sb | ab | time | group | thumbnail | palette | title | link | …
    arms TEXT NOT NULL,            -- JSON ["A", "B"]
    hypothesis TEXT,
    created_ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | closed
    note TEXT
);
CREATE TABLE IF NOT EXISTS experiment_lots (
    exp_id INTEGER NOT NULL,
    arm TEXT NOT NULL,
    item_id TEXT NOT NULL,         -- da:<id> | th:<id>
    added_ts REAL NOT NULL,
    PRIMARY KEY (exp_id, item_id)
);
"""

DA_URL = re.compile(r"^https?://(?:www\.)?deviantart\.com/[^/]+/art/[^/?#]*?-(\d+)/?(?:[?#].*)?$", re.I)
TH_URL = re.compile(r"^https?://(?:www\.)?toyhou\.se/~forums/[^/?#]+/(\d+)(?:\.[^/?#]*)?/?(?:[?#].*)?$", re.I)
REF = re.compile(r"^(da|th):(\d+)$", re.I)


class ExperimentError(ValueError):
    pass


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def resolve_ref(conn, ref: str) -> str | None:
    """Ссылка на работу DA, тред Toyhouse, da:<id>, th:<id> или просто номер работы DA → id лота."""
    ref = (ref or "").strip()
    m = REF.match(ref)
    if m:
        return f"{m.group(1).lower()}:{m.group(2)}"
    if ref.isdigit():
        return f"da:{ref}"
    m = DA_URL.match(ref)
    if m:
        return f"da:{m.group(1)}"
    m = TH_URL.match(ref)
    if m:
        return f"th:{m.group(1)}"
    return None


def _exp_id(ref) -> int:
    m = re.fullmatch(r"\s*(?:e|#)?(\d+)\s*", str(ref or ""), re.I)
    if not m:
        raise ExperimentError(f"Номер эксперимента — например e3, а не «{ref}».")
    return int(m.group(1))


def get(conn, exp_id) -> dict:
    ensure(conn)
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (_exp_id(exp_id),)).fetchone()
    if not row:
        raise ExperimentError(f"Эксперимента e{_exp_id(exp_id)} нет.")
    d = dict(row)
    d["arms"] = json.loads(d["arms"])
    return d


def create(conn, name: str, arms=("A", "B"), variable: str = "sb", hypothesis: str = "",
           now: float | None = None) -> int:
    """Новый эксперимент. Возвращает номер (e<номер>)."""
    ensure(conn)
    name = (name or "").strip()[:80]
    variable = (variable or "other").strip().lower()[:40]
    arm_list = [str(a).strip().upper()[:12] for a in arms if str(a).strip()]
    if not name:
        raise ExperimentError("Нужно название: например «старт 5 против 10».")
    if len(arm_list) < 2 or len(set(arm_list)) != len(arm_list):
        raise ExperimentError("Нужно хотя бы две разные ветки, например A и B.")
    cur = conn.execute("INSERT INTO experiments (name, variable, arms, hypothesis, created_ts) VALUES (?, ?, ?, ?, ?)",
                       (name, variable, json.dumps(arm_list), (hypothesis or "").strip()[:300], now or time.time()))
    conn.commit()
    return int(cur.lastrowid or 0)


def add(conn, exp_id, arm: str, item_ref: str, now: float | None = None) -> str:
    """Лот в ветку. Лот, который уже в другом открытом эксперименте, не берём: иначе оба опыта врут."""
    exp = get(conn, exp_id)
    if exp["status"] != "open":
        raise ExperimentError(f"Эксперимент e{exp['id']} закрыт — лоты в него больше не добавляю.")
    arm = (arm or "").strip().upper()
    if arm not in exp["arms"]:
        raise ExperimentError(f"Ветки «{arm}» нет; ветки: {', '.join(exp['arms'])}.")
    item_id = resolve_ref(conn, item_ref)
    if not item_id:
        raise ExperimentError(f"Не поняла, что за лот: «{item_ref}». Нужна ссылка на работу DA, da:<номер> или th:<номер>.")
    row = conn.execute("SELECT l.exp_id, l.arm FROM experiment_lots l JOIN experiments e ON e.id = l.exp_id "
                       "WHERE l.item_id = ? AND (e.status = 'open' OR l.exp_id = ?)", (item_id, exp["id"])).fetchone()
    if row and row["exp_id"] == exp["id"]:
        if row["arm"] == arm:
            return item_id
        raise ExperimentError(f"{item_id} уже в ветке {row['arm']} этого эксперимента.")
    if row:
        raise ExperimentError(f"{item_id} уже в открытом эксперименте e{row['exp_id']} — один лот, один опыт.")
    conn.execute("INSERT INTO experiment_lots (exp_id, arm, item_id, added_ts) VALUES (?, ?, ?, ?)",
                 (exp["id"], arm, item_id, now or time.time()))
    conn.commit()
    return item_id


def close(conn, exp_id, note: str = "") -> None:
    exp = get(conn, exp_id)
    conn.execute("UPDATE experiments SET status = 'closed', note = COALESCE(NULLIF(?, ''), note) WHERE id = ?",
                 ((note or "").strip()[:300], exp["id"]))
    conn.commit()


def list_open(conn) -> list[dict]:
    ensure(conn)
    rows = conn.execute("SELECT e.*, (SELECT COUNT(*) FROM experiment_lots l WHERE l.exp_id = e.id) AS lots "
                        "FROM experiments e WHERE e.status = 'open' ORDER BY e.id").fetchall()
    return [dict(r) for r in rows]


# --- Итоги лотов ---

def _json(v) -> dict:
    try:
        d = json.loads(v or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def lot_outcome(conn, item_id: str, now: float) -> dict:
    """{found, finished, sold, top, unit, days, ts, place, species}. Завершён: продажа записана или итог известен —
    у аукциона DA по forecast.outcome_known, у аукциона Toyhouse по дочитанному th_auctions, у прочих лотов
    по статусу «закрыт» или возрасту ≥ UNSOLD_AFTER_DAYS без продажи (не продан). Закрытый аукцион без
    прочитанных ставок — не итог. «Продан» — ставка или продажа."""
    from . import forecast

    row = db.get_item(conn, item_id)
    out: dict = {"id": item_id, "found": bool(row), "finished": False, "sold": False, "top": None, "unit": None,
                 "days": None, "ts": None, "place": None, "species": ()}
    sale = conn.execute("SELECT * FROM sales WHERE item_id = ? AND status != 'cancelled' "
                        "ORDER BY created_ts DESC LIMIT 1", (item_id,)).fetchone()
    try:
        a = conn.execute("SELECT * FROM auctions WHERE deviation_id = ?", (item_id,)).fetchone()
    except Exception:  # noqa: BLE001
        a = None
    if row:
        start = row["published_ts"] or row["first_seen_ts"]
        out.update(ts=start, place=row["place"], species=tuple(forecast.analytics_tags(row).species),
                   unit="usd" if row["price_usd"] is not None else "points" if row["price_points"] is not None else None)
        if row["lot_type"] not in AUCTION_TYPES:
            # Непроданный flat sale / OTA не закрывают: без срока он вечно «открыт» и выпадает из счёта,
            # а проданные считаются сразу — доля продаж завышена.
            known = row["status"] == "closed" or bool(start and now - start >= UNSOLD_AFTER_DAYS * DAY)
        elif a:
            known = forecast.outcome_known(
                ended=a["ended"], status=row["status"], closed_ts=row["closed_ts"], a_extra=_json(a["extra"]),
                a_updated=a["updated_ts"], published=start, item_updated=row["updated_ts"])
        else:
            th = _th_outcome(conn, item_id)
            known = th is not None
            if th and th["top"] is not None:
                out.update(sold=True, top=th["top"], unit=th["unit"] or out["unit"])
        out["finished"] = bool(sale) or known
    elif sale:
        out.update(finished=True, ts=sale["created_ts"])
    if a and (a["n_bids"] or 0) > 0 and a["top_amount"]:
        out.update(sold=True, top=float(a["top_amount"]), unit=a["currency"] or out["unit"])
    if sale:
        out["sold"] = True
        if out["top"] is None and sale["amount"] is not None:
            out.update(top=float(sale["amount"]), unit=sale["currency"] or out["unit"])
    # closed_ts годится, только если лот сначала видели открытым: иначе это момент первого чтения, не закрытия.
    closed = row["closed_ts"] if row and row["closed_ts"] and row["closed_ts"] > row["first_seen_ts"] else None
    end = (sale["created_ts"] if sale else None) or closed
    if end and out["ts"] and out["finished"]:
        out["days"] = max(0.0, (end - out["ts"]) / DAY)
    return out


def _th_outcome(conn, item_id: str) -> dict | None:
    """Аукцион Toyhouse: {"top", "unit"} — закончен (top=None — без ставок); None — итог неизвестен
    (не закончен, тред дочитан не весь или таблицы нет)."""
    if not item_id.startswith("th:"):
        return None
    try:
        rows = conn.execute("SELECT * FROM th_auctions WHERE thread_id = ?", (item_id[3:],)).fetchall()
    except Exception:  # noqa: BLE001 — старая база без таблицы
        return None
    if not rows or not all(r["ended"] and not r["incomplete"] for r in rows):
        return None
    bid = [r for r in rows if (r["n_bids"] or 0) > 0 and r["top_amount"]]
    if not bid:
        return {"top": None, "unit": None}
    best = max(bid, key=lambda r: r["top_amount"])
    return {"top": float(best["top_amount"]), "unit": best["currency"]}


def _weekday(ts: float, tz: str) -> int:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).weekday()


def _confounders(arms: dict[str, list[dict]], tz: str) -> list[str]:
    lines = []
    days = {a: Counter(_weekday(o["ts"], tz) for o in obs if o["ts"]) for a, obs in arms.items()}
    if all(days.values()):
        top = {a: c.most_common(1)[0][0] for a, c in days.items()}
        weekend = {a: sum(n for d, n in c.items() if d >= 5) / sum(c.values()) for a, c in days.items()}
        if len(set(top.values())) > 1 or max(weekend.values()) - min(weekend.values()) >= 0.3:
            lines.append("дни недели разные: " + "; ".join(f"{a} — чаще {WEEKDAYS[d]}" for a, d in top.items()))
        else:
            lines.append("дни недели похожи")
    groups = {a: Counter(o["place"] for o in obs if o["place"]) for a, obs in arms.items()}
    if any(groups.values()):
        sets = {a: set(g) for a, g in groups.items()}
        if len({frozenset(s) for s in sets.values()}) > 1:
            lines.append("группы/места разные: " + "; ".join(
                f"{a} — {', '.join(p for p, _ in g.most_common(2)) or '—'}" for a, g in groups.items()))
        else:
            lines.append("группы/места одни и те же")
    species = {a: Counter(s for o in obs for s in o["species"]) for a, obs in arms.items()}
    if any(species.values()):
        tops = {a: tuple(s for s, _ in c.most_common(2)) for a, c in species.items()}
        if len(set(tops.values())) > 1:
            lines.append("виды разные: " + "; ".join(f"{a} — {', '.join(t) or '—'}" for a, t in tops.items()))
        else:
            lines.append("виды похожи")
    lines.append("не учтены: сезон, превью, сколько похожих лотов было на рынке в те же дни")
    return lines


def _money(v: float, unit: str | None) -> str:
    from .sales import money

    return money(v, unit)


def report(conn, exp_id, now: float | None = None, tz: str = DEFAULT_TZ) -> str:
    """Итоги по веткам и честный вывод: «вероятно лучше (P≈…)», «пока не различить» или «вывода нет»."""
    now = now or time.time()
    exp = get(conn, exp_id)
    rows = conn.execute("SELECT arm, item_id FROM experiment_lots WHERE exp_id = ? ORDER BY added_ts, item_id",
                        (exp["id"],)).fetchall()
    arms: dict[str, list[dict]] = {a: [] for a in exp["arms"]}
    for r in rows:
        arms.setdefault(r["arm"], []).append(lot_outcome(conn, r["item_id"], now))
    var = VARIABLES.get(exp["variable"], exp["variable"])
    lines = [f"Эксперимент e{exp['id']} «{exp['name']}» — меняем: {var}; "
             f"{'открыт' if exp['status'] == 'open' else 'закрыт'}."]
    if exp["hypothesis"]:
        lines.append(f"Гипотеза: {exp['hypothesis']}")
    done = {a: [o for o in obs if o["finished"]] for a, obs in arms.items()}
    units = Counter(o["unit"] for obs in done.values() for o in obs if o["top"] is not None and o["unit"])
    unit = units.most_common(1)[0][0] if units else None
    stats: dict[str, tuple[int, int, list[float]]] = {}
    for a, obs in arms.items():
        fin = done[a]
        n, k = len(fin), sum(1 for o in fin if o["sold"])
        lost = sum(1 for o in obs if not o["found"])
        head = f"Ветка {a}: лотов {len(obs)}, завершено {n}" + (f", нет в базе {lost}" if lost else "")
        if not n:
            lines.append(head + " — итогов пока нет.")
            stats[a] = (0, 0, [])
            continue
        lo, hi = evidence.wilson(k, n)
        ev = evidence.span([o["ts"] for o in fin], "experiment", f"её эксперимент «{exp['name']}», ветка {a}", None,
                           "observed", n=n, interval=(lo, hi), measure="rate", value=k / n, data_as_of=now)
        line = (f"{head}. Со ставкой или продан: {k} из {n} ({evidence.pct(k / n)}, 80%: "
                f"{evidence.rate_interval(lo, hi)})")
        tops = [o["top"] for o in fin if o["top"] is not None and o["unit"] == unit]
        if tops:
            mlo, mhi = evidence.median_ci(tops)
            line += (f"; медиана ставки {_money(statistics.median(tops), unit)} (80%: {_money(mlo, unit)}–"
                     f"{_money(mhi, unit)}, по {len(tops)})")
        days = [o["days"] for o in fin if o["days"] is not None]
        if days:
            line += f"; до закрытия медиана {statistics.median(days):.1f} дн (по {len(days)})"
        lines.append(line + " " + evidence.tag(ev))
        stats[a] = (k, n, tops)
    pending = {a: (sum(1 for o in obs if o["found"] and not o["finished"]), len(obs)) for a, obs in arms.items()}
    open_arms = {a: (m, t) for a, (m, t) in pending.items() if t and m / t > UNRESOLVED_MAX}
    if open_arms:
        lines.append("Вывод: вывода нет — итог ещё неизвестен у " + ", ".join(
            f"{a}: {m} из {t}" for a, (m, t) in open_arms.items()) + " лотов; пока проданные видны раньше "
            "непроданных, доля завышена.")
    else:
        lines.append("Вывод: " + _verdict(stats))
    lines.append("Что ещё различается между ветками (могло повлиять): " + "; ".join(_confounders(done, tz)) + ".")
    return "\n".join(lines)


def _verdict(stats: dict[str, tuple[int, int, list]]) -> str:
    small = {a: n for a, (_k, n, _t) in stats.items() if n < evidence.MIN_SIDE}
    if small:
        need = evidence.MIN_SIDE - min(small.values())
        now_n = ", ".join(f"{a}: {n}" for a, (_k, n, _t) in stats.items())
        return (f"вывода нет: нужно ещё {need} лотов в каждой ветке (завершено {now_n}; "
                f"нужно ≥{evidence.MIN_SIDE} в каждой).")
    arms = list(stats)
    out = []
    if len(arms) == 2:
        (a, (ka, na, ta)), (b, (kb, nb, tb)) = stats.items()
        p = evidence.p_greater(ka, na, kb, nb)
        v = evidence.compare(ka, na, kb, nb)
        if v == "better":
            out.append(f"{a} вероятно лучше {b} по доле со ставкой (P≈{min(p, 0.99):.2f})")
        elif v == "worse":
            out.append(f"{b} вероятно лучше {a} по доле со ставкой (P≈{min(1 - p, 0.99):.2f})")
        else:
            out.append(f"по доле со ставкой пока не различить (P({a} лучше)≈{p:.2f}, нужно ≥{evidence.COMPARE_P:.2f})")
        out.append(_price_verdict(a, ta, b, tb))
    else:
        thr = evidence.family_threshold(len(arms))
        total_k = sum(k for k, _n, _t in stats.values())
        total_n = sum(n for _k, n, _t in stats.values())
        for a, (k, n, _t) in stats.items():
            v = evidence.compare(k, n, total_k - k, total_n - n, thr)
            if v:
                p = evidence.p_greater(k, n, total_k - k, total_n - n)
                word = "лучше" if v == "better" else "хуже"
                out.append(f"{a} вероятно {word} остальных (P≈{min(0.99, p if v == 'better' else 1 - p):.2f}, "
                           f"порог {thr:.2f} на {len(arms)} ветки)")
        if not out:
            out.append(f"пока не различить (порог {thr:.2f} на {len(arms)} ветки)")
    return "; ".join(o for o in out if o) + "."


def _price_verdict(a: str, ta: list, b: str, tb: list) -> str:
    if len(ta) < evidence.MIN_SIDE or len(tb) < evidence.MIN_SIDE:
        return "по ставке вывода нет (мало лотов со ставкой)"
    la, ha = evidence.median_ci(ta)
    lb, hb = evidence.median_ci(tb)
    if la > hb:
        return f"по ставке: интервалы не перекрываются — у {a}, вероятно, выше"
    if lb > ha:
        return f"по ставке: интервалы не перекрываются — у {b}, вероятно, выше"
    return "по ставке пока не различить (интервалы перекрываются)"


# --- Один вход для инструмента promo:experiment ---

def handle(conn, *, ref: str = "", text: str = "", items=(), variant: str = "", now: float | None = None,
           tz: str = DEFAULT_TZ) -> str:
    """text='название; переменная[; гипотеза]' без ref — создать; ref + items=['A=<ссылка>', …] — добавить;
    ref + variant='close' — закрыть; только ref — итоги; ничего — список открытых."""
    now = now or time.time()
    try:
        if (variant or "").strip().lower() == "close":
            if not ref:
                return "Какой эксперимент закрыть? Укажи ref=e<номер>."
            close(conn, ref, text)
            return "Закрыла. Итоги:\n" + report(conn, ref, now, tz)
        if isinstance(items, str):     # одна строка «A=<ссылка>», а не список
            items = [items]
        if ref and items:
            done, errors = [], []
            for it in items:
                arm, sep, link = str(it).partition("=")
                if not sep:
                    errors.append(f"«{it}»: нужно в виде A=<ссылка>")
                    continue
                try:
                    done.append(f"{arm.strip().upper()}: {add(conn, ref, arm, link.strip(), now)}")
                except ExperimentError as e:
                    errors.append(str(e))
            return "\n".join(([f"Добавила в e{_exp_id(ref)}: " + ", ".join(done)] if done else [])
                             + [f"Не добавила: {e}" for e in errors])
        if ref:
            return report(conn, ref, now, tz)
        if text:
            parts = [p.strip() for p in text.split(";")]
            name = parts[0]
            variable = parts[1] if len(parts) > 1 and parts[1] else "other"
            hypothesis = parts[2] if len(parts) > 2 else ""
            exp_id = create(conn, name, variable=variable, hypothesis=hypothesis, now=now)
            return (f"Завела эксперимент e{exp_id} «{name}» (меняем: {VARIABLES.get(variable.lower(), variable)}), "
                    f"ветки A и B. Добавляй лоты: ref=e{exp_id} items=['A=<ссылка>', 'B=<ссылка>']. "
                    f"Вывод появится, когда в каждой ветке будет ≥{evidence.MIN_SIDE} завершённых лотов; "
                    "остальное (время, группы) лучше держать одинаковым.")
    except ExperimentError as e:
        return str(e)
    exps = list_open(conn)
    if not exps:
        return "Открытых экспериментов нет. Завести: text='старт 5 против 10; sb'."
    return "Открытые эксперименты:\n" + "\n".join(
        f"• e{e['id']} «{e['name']}» — {VARIABLES.get(e['variable'], e['variable'])}, лотов {e['lots']}" for e in exps)
