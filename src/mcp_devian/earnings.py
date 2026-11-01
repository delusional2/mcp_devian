"""Деньги её времени: сколько она на самом деле зарабатывает в час, цель на месяц и план недели.

Часы она отмечает сама («Dragon adopt — 3.5 ч»): по id продажи, лоту, ссылке, слоту заказа (comm:<слот>)
или просто названию. Заработок в час — сколько дошло до неё в ₽ (net из продаж), делённое на часы:
* непроданное старше SETTLE_DAYS и общая работа формата (база YCH и т.п.) идут с 0 ₽ — так честнее;
* то, что ещё продаётся или ждёт оплаты, в расчёт не входит и показывается отдельно;
* оплаченное без суммы в ₽ ($, €, поинты) с рублями не смешиваем и по курсу не пересчитываем.

Каждое число — с меткой evidence.tag (n, период, источник, уверенность). Меньше MIN_WORKS работ в группе —
«вывода нет» вместо ₽/ч. «Выгоднее» говорим только при P(лучше) ≥ порога с поправкой на число сравнений.
План недели берёт ниши, лучшее время и непроданное из уже готовых модулей и спрос не придумывает.
Ничего не публикует и никому не пишет: только считает и предлагает, решает она.
"""

from __future__ import annotations

import math
import random
import re
import sqlite3
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, evidence, sales, taxonomy

DAY = 86400
HOUR = 3600
DEFAULT_TZ = "Europe/Moscow"
HISTORY_DAYS = 180          # часы за этот период
GOAL_HISTORY_DAYS = 120     # средняя цена продажи «по нынешним ценам»
SETTLE_DAYS = 30            # работа без оплаченной продажи дольше — считаем непроданной (0 ₽)
TITLE_LINK_DAYS = 7         # свободную запись сами привязываем к продаже с тем же названием не старше записи на столько
DEDUP_SECONDS = 120         # та же запись часов ещё раз за это время — повтор вызова, не новая работа
MIN_WORKS = evidence.MIN_SIDE
MAX_HOURS = 200.0
MAX_TARGET = 10_000_000
MAX_CAPACITY = 50
CAPACITY_WEEKS = 8
MIN_ACTIVE_WEEKS = 4
PAID = ("paid", "transferred", "done")
WEAK_LEVELS = ("нет вывода", "низкая")

SCHEMA = """
-- Её часы: сколько рисовала адопт / слот / набор. Отсюда ₽ в час.
CREATE TABLE IF NOT EXISTS work_log (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,              -- когда записала
    hours REAL NOT NULL,
    sale_id TEXT,                  -- продажа (sales.id), если нашлась
    item_id TEXT,                  -- лот da:/th: или слот заказа comm:<слот>
    title TEXT NOT NULL,
    format TEXT,                   -- её уточнение: auction|flatsale|ota|bundle|ych|commission|other
    overhead INTEGER NOT NULL DEFAULT 0,  -- общая работа формата (база YCH): 0 ₽, считается сразу
    note TEXT
);
CREATE INDEX IF NOT EXISTS work_log_ts ON work_log(ts);

-- Её цель на месяц (₽, сколько должно дойти до неё).
CREATE TABLE IF NOT EXISTS earn_goals (
    month TEXT PRIMARY KEY,        -- YYYY-MM по её часовому поясу
    target REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'rub',
    set_ts REAL NOT NULL
);

-- Её ответы для плана: сколько адоптов в неделю успевает.
CREATE TABLE IF NOT EXISTS earn_prefs (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts REAL NOT NULL
);
"""

FORMATS = ("auction", "flatsale", "ota", "bundle", "ych", "commission", "other")
FORMAT_NAMES = {"auction": "аукционы", "flatsale": "фикс-цена", "ota": "OTA", "bundle": "наборы", "ych": "YCH",
                "commission": "коммишены", "other": "другое"}
FORMS = {"auction": ("аукцион", "аукциона", "аукционов"),
         "flatsale": ("продажа по фикс-цене", "продажи по фикс-цене", "продаж по фикс-цене"),
         "ota": ("OTA-продажа", "OTA-продажи", "OTA-продаж"),
         "bundle": ("набор", "набора", "наборов"),
         "ych": ("YCH-слот", "YCH-слота", "YCH-слотов"),
         "commission": ("коммишен", "коммишена", "коммишенов"),
         "other": ("продажа", "продажи", "продаж"),
         "all": ("продажа", "продажи", "продаж")}
WORKS_FORMS = ("работа", "работы", "работ")
# Формат продажи из ledger → формат для денег. raffle и adopt — как у sales.
SALE_FORMAT = {"auction": "auction", "bta": "auction", "flatsale": "flatsale", "adopt": "flatsale", "ota": "ota",
               "raffle": "other", "other": "other", "bundle": "bundle", "ych": "ych", "commission": "commission"}
ALIASES = (("аукц", "auction"), ("auction", "auction"), ("bta", "auction"), ("фикс", "flatsale"),
           ("flat", "flatsale"), ("fs", "flatsale"), ("adopt", "flatsale"), ("ota", "ota"), ("ота", "ota"),
           ("набор", "bundle"), ("bundle", "bundle"), ("batch", "bundle"), ("сет", "bundle"), ("ych", "ych"),
           ("иch", "ych"), ("коммиш", "commission"), ("комиш", "commission"), ("commission", "commission"),
           ("заказ", "commission"), ("друг", "other"), ("other", "other"), ("raffle", "other"),
           ("лотере", "other"))
BUNDLE_RE = re.compile(r"набор|\bbatch\b|\bbundle\b|\bset\s+of\b|\bpack\b|\b\d\s*(?:in|в)\s*1\b", re.I)
YCH_RE = re.compile(r"\bych\b", re.I)
COMMISSION_RE = re.compile(r"коммиш|комиш|\bcommission|\bcomm\b|\bзаказ", re.I)
SALE_ID_RE = re.compile(r"^[0-9a-f]{8}$")
LOT_RE = re.compile(r"^(?:da|th):\S+$")
COMM_RE = re.compile(r"^comm:([^:\s]+)(?::\S*)?$")
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь")
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
DAYPARTS = ("ночью", "утром", "днём", "вечером")
GENTLE = "Это её записи и её продажи, не прогноз: цифры, n и уверенность — как есть."


class EarningsError(ValueError):
    pass


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


# --- Мелочи ---

def _norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def _h(x: float) -> str:
    return f"{round(x, 2):g}"


def _rub(x: float | None) -> str:
    if x is None:
        return "?"
    return sales.money(round(x, -1) if abs(x) >= 100 else round(x), "rub")


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _tz(cfg) -> str:
    return getattr(getattr(cfg, "posting", None), "timezone", "") or DEFAULT_TZ


def normalize_format(text: str | None) -> str | None:
    """«набор», «аукцион», «ych», «коммишен» … → ключ формата; None — не узнала."""
    t = _norm(text)
    if not t:
        return None
    if t in FORMATS:
        return t
    for prefix, fmt in ALIASES:
        if t.startswith(prefix):
            return fmt
    return None


def guess_format(title: str | None, raw: str | None = None, item_id: str | None = None) -> str:
    """Формат по названию (набор/YCH/коммишен важнее) и по формату продажи или лота."""
    t = title or ""
    if BUNDLE_RE.search(t):
        return "bundle"
    if YCH_RE.search(t):
        return "ych"
    if COMMISSION_RE.search(t):
        return "commission"
    fmt = SALE_FORMAT.get((raw or "").lower())
    if fmt and fmt != "other":
        return fmt
    if (item_id or "").startswith("comm:"):
        return "commission"
    return fmt or "other"


_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zа-яё]*)")


def parse_hours(value) -> float:
    """«3.5 ч», «3,5ч», «2 ч 30 мин», «90 мин», «1:30», «3h», 4 → часы (0 < h ≤ MAX_HOURS)."""
    if isinstance(value, bool):
        raise EarningsError("Часы — число, например «3.5 ч»")
    if isinstance(value, (int, float)):
        h = float(value)
    else:
        s = str(value or "").strip().lower().replace(",", ".")
        if not s:
            raise EarningsError("Нужны часы: например «3.5 ч» или «90 мин».")
        clock = re.fullmatch(r"(\d{1,3}):([0-5]\d)", s)
        if clock:
            h = int(clock.group(1)) + int(clock.group(2)) / 60
        else:
            parts = _NUM.findall(s)
            rest = re.sub(r"(?<![\wа-яё])(?:и|and)(?![\wа-яё])|[\s+]", "", _NUM.sub("", s))
            if not parts or rest:
                raise EarningsError(f"Не поняла, сколько часов: «{value}» — напиши, например, «3.5 ч» или «90 мин».")
            h = 0.0
            for num, unit in parts:
                if not unit:
                    if len(parts) > 1:
                        raise EarningsError(f"Не поняла, сколько часов: «{value}» — напиши «2 ч 30 мин».")
                    h += float(num)
                elif unit[0] in "чh":
                    h += float(num)
                elif unit[0] in "мm":
                    h += float(num) / 60
                else:
                    raise EarningsError(f"Не поняла, сколько часов: «{value}» — нужны часы или минуты.")
    if not math.isfinite(h) or h <= 0 or h > MAX_HOURS:
        raise EarningsError(f"Часы — больше нуля и не больше {MAX_HOURS:g} на одну запись.")
    return round(h, 4)


# --- Что за работа ---

def _sale_for_item(conn, item_id: str):
    return conn.execute("SELECT * FROM sales WHERE item_id = ? ORDER BY status = 'cancelled', created_ts DESC LIMIT 1",
                        (item_id,)).fetchone()


def _comm_sales(conn, slot: str) -> list:
    prefix = f"comm:{slot}:"
    return conn.execute("SELECT * FROM sales WHERE substr(item_id, 1, ?) = ? ORDER BY item_id, created_ts",
                        (len(prefix), prefix)).fetchall()


def _comm_slot(item_id: str | None) -> str | None:
    m = COMM_RE.match(item_id or "")
    return m.group(1) if m else None


def _comm_kind(conn, slot: str) -> str | None:
    """ych | commission из очереди заказов (commissions.comm_slots), если она есть; только чтение."""
    try:
        row = conn.execute("SELECT kind FROM comm_slots WHERE id = ?", (slot,)).fetchone()
    except sqlite3.Error:
        return None
    return row["kind"] if row and row["kind"] in ("ych", "commission") else None


def _comm_waiting(conn, slot: str) -> bool:
    """Слот заказа ждёт остаток: строка остатка (comm:<слот>:final) появляется только с оплатой, поэтому
    «все части оплачены» по продажам ещё не значит «оплачен целиком». Смотрим сам слот (commissions):
    не final_paid / delivered / cancelled и остаток больше нуля — сделка ещё не закрыта. Слота нет — не знаем."""
    try:
        row = conn.execute("SELECT status, price, deposit_pct FROM comm_slots WHERE id = ?", (slot,)).fetchone()
    except sqlite3.Error:
        return False
    if row is None or row["status"] in ("final_paid", "delivered", "cancelled"):
        return False
    deposit = round((row["price"] or 0) * (row["deposit_pct"] or 0) / 100, 2)
    return round((row["price"] or 0) - deposit, 2) > 0


def _from_sale(s) -> dict:
    slot = _comm_slot(s["item_id"])
    if slot:
        return {"kind": "commission", "sale_id": None, "item_id": f"comm:{slot}", "title": s["title"]}
    return {"kind": "sale", "sale_id": s["id"], "item_id": s["item_id"], "title": s["title"]}


def _by_title(conn, title: str, now: float) -> tuple[list, list, list]:
    """(свежие продажи, свежие лоты, старые продажи) с этим названием.

    По названию привязываем только к свежему: продажа заведена не раньше SETTLE_DAYS назад и ещё не оплачена
    (или оплачена не раньше TITLE_LINK_DAYS назад), лот выложен не раньше SETTLE_DAYS назад. Иначе «Dragon adopt»
    сегодня приклеится к давно проданному «Dragon adopt» — и новая работа потеряется."""
    want = _norm(title)
    since = now - SETTLE_DAYS * DAY
    rows: list = []
    old: list = []
    for r in conn.execute("SELECT * FROM sales WHERE status != 'cancelled' ORDER BY created_ts DESC"):
        if _norm(r["title"]) != want:
            continue
        fresh = (r["created_ts"] or 0) >= since and (
            not r["paid_ts"] or r["paid_ts"] >= now - TITLE_LINK_DAYS * DAY)
        (rows if fresh else old).append(r)
    items = [r for r in conn.execute("SELECT * FROM items WHERE own = 1 AND COALESCE(published_ts, first_seen_ts) >= ?",
                                     (since,)) if _norm(r["title"]) == want]
    return rows, items, old


def resolve(conn, ref: str, now: float | None = None) -> dict:
    """{kind: sale|item|commission|none, sale_id, item_id, title} по id продажи, лоту, ссылке, comm:<слот>, названию.
    У kind=none может быть old — старые продажи с тем же названием (к ним сами не привязываем)."""
    now = now or time.time()
    ref = str(ref or "").strip()
    if not ref:
        raise EarningsError("Что рисовала? Назови продажу (id), ссылку на лот или название.")
    slot = _comm_slot(ref)
    if slot:
        rows = _comm_sales(conn, slot)
        return {"kind": "commission", "sale_id": None, "item_id": f"comm:{slot}",
                "title": rows[0]["title"] if rows else ref}
    if SALE_ID_RE.match(ref.lower()):
        s = sales.get(conn, ref.lower())
        if s:
            return _from_sale(s)
    if LOT_RE.match(ref):
        return _from_lot(conn, ref, None)
    if ref.lower().startswith("http"):
        s = conn.execute("SELECT * FROM sales WHERE character_url = ? AND status != 'cancelled' "
                         "ORDER BY created_ts DESC", (ref,)).fetchone()
        if s:
            return _from_sale(s)
        item_id = sales.lot_id_for_url(conn, ref)
        if item_id:
            return _from_lot(conn, item_id, ref)
        raise EarningsError("Ссылку не узнала: нужна ссылка на её лот DeviantArt или на перса из её продаж.")
    found, items, old = _by_title(conn, ref, now)
    if len(found) > 1:
        raise EarningsError(f"Продаж «{ref}» несколько — назови id: "
                            + ", ".join(f"{r['id']} ({sales.money(r['amount'], r['currency'])})" for r in found[:6]))
    if found:
        return _from_sale(found[0])
    if len(items) > 1:
        raise EarningsError(f"Лотов «{ref}» несколько — пришли ссылку или id: "
                            + ", ".join(r["id"] for r in items[:6]))
    if items:
        return _from_lot(conn, items[0]["id"], None)
    return {"kind": "none", "sale_id": None, "item_id": None, "title": ref, "old": old}


def _from_lot(conn, item_id: str, url: str | None) -> dict:
    s = _sale_for_item(conn, item_id)
    if s:
        return _from_sale(s)
    item = db.get_item(conn, item_id)
    if item is None and url is None:
        raise EarningsError("Такого лота нет в базе — пришли ссылку или название.")
    return {"kind": "item", "sale_id": None, "item_id": item_id, "title": item["title"] if item else url}


# --- Запись часов ---

def _entry_key(conn, r, cache: dict | None = None) -> tuple[str, list, str | None]:
    """(ключ работы, её продажи, лот) для записи work_log — так записи одной работы складываются."""
    cache = {} if cache is None else cache
    if r["overhead"]:
        return f"overhead:{r['format'] or 'other'}:{_norm(r['title'])}", [], None
    slot = _comm_slot(r["item_id"])
    if slot:
        if ("comm", slot) not in cache:
            cache[("comm", slot)] = _comm_sales(conn, slot)
        return f"comm:{slot}", cache[("comm", slot)], None
    s = sales.get(conn, r["sale_id"]) if r["sale_id"] else None
    if s is None and r["item_id"]:
        s = _sale_for_item(conn, r["item_id"])
        if s is None:
            return f"item:{r['item_id']}", [], r["item_id"]
    if s is None:
        # Свободная запись: продажа с тем же названием, заведённая позже записи, — это она и есть.
        want = _norm(r["title"])
        found = [x for x in conn.execute("SELECT * FROM sales WHERE status != 'cancelled' AND created_ts >= ? "
                                         "AND created_ts <= ?",
                                         (r["ts"] - TITLE_LINK_DAYS * DAY, r["ts"] + SETTLE_DAYS * DAY))
                 if _norm(x["title"]) == want]
        if len(found) == 1:
            s = found[0]
    if s is None:
        return f"title:{_norm(r['title'])}", [], None
    slot = _comm_slot(s["item_id"])
    if slot:
        return f"comm:{slot}", _comm_sales(conn, slot), None
    return f"sale:{s['id']}", [s], s["item_id"]


def log_hours(conn, ref: str, hours, *, format: str = "", overhead: bool = False, note: str = "",
              now: float | None = None) -> dict:
    """Записать её часы. {id, linked, sale_id, item_id, title, format, hours, total_hours, text}."""
    ensure(conn)
    now = now or time.time()
    h = parse_hours(hours)
    fmt = None
    if (format or "").strip():
        fmt = normalize_format(format)
        if not fmt:
            raise EarningsError("Формат: аукцион, фикс-цена, OTA, набор, YCH, коммишен или другое.")
    if overhead:
        if not fmt:
            raise EarningsError("Общая работа (база YCH, подготовка набора) — назови формат, к которому она относится.")
        target: dict[str, Any] = {"kind": "overhead", "sale_id": None, "item_id": None,
                                  "title": str(ref or "").strip() or FORMAT_NAMES[fmt]}
    else:
        target = resolve(conn, ref, now)
    # Повтор того же вызова (клиент переспросил) — не удваиваем её часы.
    dup = conn.execute("SELECT * FROM work_log WHERE ts >= ? AND ts <= ? AND hours = ? AND title = ? "
                       "AND sale_id IS ? AND item_id IS ? AND format IS ? AND overhead = ? ORDER BY ts DESC LIMIT 1",
                       (now - DEDUP_SECONDS, now + DEDUP_SECONDS, h, target["title"], target["sale_id"],
                        target["item_id"], fmt, int(bool(overhead)))).fetchone()
    if dup is not None:
        return {"id": dup["id"], "linked": target["kind"], "sale_id": target["sale_id"], "item_id": target["item_id"],
                "title": target["title"], "format": fmt or "", "hours": h, "total_hours": None, "duplicate": True,
                "text": (f"Это уже записано только что: {_h(h)} ч — «{target['title']}» (запись {dup['id']}). "
                         "Второй раз не добавляю. Если правда было ещё столько же — скажи ещё раз через пару минут.")}
    entry_id = uuid.uuid4().hex[:8]
    conn.execute("INSERT INTO work_log (id, ts, hours, sale_id, item_id, title, format, overhead, note) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (entry_id, now, h, target["sale_id"], target["item_id"], target["title"], fmt, int(bool(overhead)),
                  note or ""))
    conn.commit()
    row = conn.execute("SELECT * FROM work_log WHERE id = ?", (entry_id,)).fetchone()
    cache: dict = {}
    key, sale_rows, item_id = _entry_key(conn, row, cache)
    total = sum(r["hours"] for r in conn.execute("SELECT * FROM work_log").fetchall()
                if _entry_key(conn, r, cache)[0] == key)
    shown = fmt or guess_format(target["title"], sale_rows[0]["format"] if sale_rows else None, target["item_id"])
    if not fmt and item_id and not sale_rows:
        item = db.get_item(conn, item_id)
        shown = guess_format(target["title"], item["lot_type"] if item else None, item_id)
    kind = target["kind"]
    where = {"sale": f" (продажа {target['sale_id']})",
             "item": " (лот; продажи пока нет — посчитаю, когда продашь)",
             "commission": " (слот заказа)", "overhead": " (общая работа формата)", "none": ""}[kind]
    text = (f"Записала: {_h(h)} ч — «{target['title']}»{where}, формат: {FORMAT_NAMES[shown]}. "
            f"Всего на эту работу: {_h(total)} ч.")
    if kind == "none":
        text += (" Такой продажи или лота не нашла — записала отдельно. Продашь — скажи id продажи, привяжу "
                 f"(запись {entry_id}). Не продастся за {SETTLE_DAYS} дн — посчитаю как 0 ₽.")
        old = target.get("old") or []
        if old:
            text += (" Есть давняя продажа с таким названием — к ней сама не привязываю, это может быть другая "
                     "работа. Если часы про неё — назови id: "
                     + ", ".join(f"{s['id']} ({sales.money(s['amount'], s['currency'])})" for s in old[:3]) + ".")
    if kind == "overhead":
        text += " В ₽/ч эти часы идут с 0 ₽ — так честнее."
    return {"id": entry_id, "linked": kind, "sale_id": target["sale_id"], "item_id": target["item_id"],
            "title": target["title"], "format": shown, "hours": h, "total_hours": round(total, 4), "text": text}


def link(conn, what: str, sale_id: str) -> str:
    """Привязать свободные записи (id записи или название) к продаже."""
    ensure(conn)
    s = sales.get(conn, (sale_id or "").strip().lower()) if (sale_id or "").strip() else None
    if s is None:
        raise EarningsError("Такой продажи нет — нужен id продажи (sales list).")
    what = str(what or "").strip()
    rows = conn.execute("SELECT * FROM work_log WHERE id = ?", (what.lower(),)).fetchall() if what else []
    if not rows:
        want = _norm(what)
        rows = [r for r in conn.execute("SELECT * FROM work_log WHERE sale_id IS NULL AND item_id IS NULL "
                                        "AND overhead = 0") if _norm(r["title"]) == want]
    if not rows:
        raise EarningsError("Не нашла такой записи часов — назови id записи или название, как записывала.")
    t = _from_sale(s)
    conn.executemany("UPDATE work_log SET sale_id = ?, item_id = ? WHERE id = ?",
                     [(t["sale_id"], t["item_id"], r["id"]) for r in rows])
    conn.commit()
    return f"Привязала {len(rows)} запис(ь/и) «{rows[0]['title']}» к продаже {s['id']} («{s['title']}»)."


def undo(conn, entry_id: str = "") -> str:
    """Убрать её запись часов (ошиблась): по id записи или по названию работы (последнюю с ним). Пусто — последнюю."""
    ensure(conn)
    what = str(entry_id or "").strip()
    if not what:
        row = conn.execute("SELECT * FROM work_log ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
        if row is None:
            raise EarningsError("Записей часов пока нет.")
    else:
        row = conn.execute("SELECT * FROM work_log WHERE id = ?", (what.lower(),)).fetchone()
        if row is None:
            want = _norm(what)
            rows = [r for r in conn.execute("SELECT * FROM work_log ORDER BY ts DESC, rowid DESC")
                    if _norm(r["title"]) == want or r["sale_id"] == what.lower() or r["item_id"] == what]
            row = rows[0] if rows else None
        if row is None:
            raise EarningsError("Такой записи часов нет — назови id записи или название, как записывала.")
    conn.execute("DELETE FROM work_log WHERE id = ?", (row["id"],))
    conn.commit()
    return f"Убрала запись: {_h(row['hours'])} ч — «{row['title']}»."


# --- Работы ---

@dataclass
class Work:
    key: str
    title: str
    hours: float
    format: str
    status: str                  # paid | pending | unsold | overhead
    income_rub: float | None     # ₽ дошло; 0 у непроданных и общей работы; None — не в ₽ или ещё не продано
    other: dict = field(default_factory=dict)   # оплачено, но не в ₽: {валюта: сумма}
    ts: float = 0.0
    tags: taxonomy.Tags = field(default_factory=taxonomy.Tags)
    sale_ids: list = field(default_factory=list)
    item_id: str | None = None
    entries: int = 0


def works(conn, now: float | None = None, days: int = HISTORY_DAYS) -> list[Work]:
    """Её работы за days дней: работа входит целиком (все её часы, даже записанные раньше окна), если последняя
    запись часов или оплата попали в окно. Иначе окно резало бы работу пополам: доход весь, часов — часть."""
    ensure(conn)
    now = now or time.time()
    since = now - days * DAY
    rows = conn.execute("SELECT * FROM work_log WHERE ts <= ? ORDER BY ts, rowid", (now,)).fetchall()
    cache: dict = {}
    groups: dict[str, dict] = {}
    for r in rows:
        key, sale_rows, item_id = _entry_key(conn, r, cache)
        g = groups.setdefault(key, {"entries": [], "sales": {}, "item_id": None, "overhead": False})
        g["entries"].append(r)
        g["sales"].update({s["id"]: s for s in sale_rows})
        g["item_id"] = g["item_id"] or item_id
        g["overhead"] = g["overhead"] or bool(r["overhead"])
    def recent(g: dict) -> bool:
        return (max(e["ts"] for e in g["entries"]) >= since
                or any((s["paid_ts"] or 0) >= since for s in g["sales"].values() if s["status"] != "cancelled"))

    return [_work(conn, key, g, now) for key, g in groups.items() if recent(g)]


def _work(conn, key: str, g: dict, now: float) -> Work:
    entries, sale_rows = g["entries"], list(g["sales"].values())
    item = db.get_item(conn, g["item_id"]) if g["item_id"] else None
    title = sale_rows[0]["title"] if sale_rows and not key.startswith("comm:") else (
        item["title"] if item else entries[0]["title"])
    explicit = [e["format"] for e in entries if e["format"]]
    raw = sale_rows[0]["format"] if sale_rows else (item["lot_type"] if item else None)
    fmt = explicit[-1] if explicit else (_comm_kind(conn, key[5:]) if key.startswith("comm:") else None) \
        or guess_format(title, raw, entries[0]["item_id"])
    tags = taxonomy.tag_item(item, use_snippet=False) if item else taxonomy.tag_text(title)
    w = Work(key=key, title=title, hours=sum(e["hours"] for e in entries), format=fmt, status="pending",
             income_rub=None, ts=max(e["ts"] for e in entries), tags=tags, sale_ids=[s["id"] for s in sale_rows],
             item_id=g["item_id"], entries=len(entries))
    live = [s for s in sale_rows if s["status"] != "cancelled"]
    paid_rows = [s for s in live if s["status"] in PAID and s["paid_ts"]]
    if g["overhead"]:
        w.status, w.income_rub = "overhead", 0.0
    elif key.startswith("comm:") and _comm_waiting(conn, key[5:]):
        pass  # предоплата есть, остаток ещё не пришёл — работа не оплачена целиком: ждёт
    elif paid_rows and len(paid_rows) == len(live):
        w.status, w.ts = "paid", max(s["paid_ts"] for s in paid_rows)
        rub, complete = 0.0, True
        for s in paid_rows:
            if s["net_amount"] is not None and (s["net_currency"] or "rub") == "rub":
                rub += s["net_amount"]
                continue
            complete = False
            if s["net_amount"] is not None:
                w.other[s["net_currency"]] = w.other.get(s["net_currency"], 0.0) + s["net_amount"]
            elif s["amount"] is not None:
                cur = s["currency"] or "?"
                w.other[cur] = w.other.get(cur, 0.0) + s["amount"]
        w.income_rub = rub if complete else None
    elif not live:
        published = (item["published_ts"] or item["first_seen_ts"]) if item else 0
        ref = max([w.ts, published or 0, *(s["created_ts"] for s in sale_rows)])
        if now - ref >= SETTLE_DAYS * DAY:
            w.status, w.income_rub = "unsold", 0.0
    return w


# --- ₽ в час ---

def _ratio(sample) -> float:
    hours = sum(w.hours for w in sample)
    return sum(w.income_rub for w in sample) / hours if hours else 0.0


def ratio_ci(ws: list[Work], level: float = 0.8, B: int = 1000, seed: int = 0) -> tuple[float, float]:
    """80%-интервал ₽/ч бутстрэпом по работам, детерминированно."""
    if len(ws) == 1:
        r = _ratio(ws)
        return r, r
    rng = random.Random(seed)
    n = len(ws)
    boots = sorted(_ratio([ws[rng.randrange(n)] for _ in range(n)]) for _ in range(B))
    tail = (1 - level) / 2
    return boots[int(tail * (B - 1))], boots[int(round((1 - tail) * (B - 1)))]


def p_rate_greater(a: list[Work], b: list[Work], B: int = 2000, seed: int = 0) -> float:
    """P(₽/ч у a больше, чем у b) — бутстрэп обеих выборок, ничьи пополам."""
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    wins = 0.0
    for _ in range(B):
        ra = _ratio([a[rng.randrange(na)] for _ in range(na)])
        rb = _ratio([b[rng.randrange(nb)] for _ in range(nb)])
        wins += 1.0 if ra > rb else 0.5 if ra == rb else 0.0
    return wins / B


@dataclass
class Group:
    key: str
    label: str
    works: list[Work]
    rub: list[Work] = field(default_factory=list)   # в расчёте ₽/ч
    rate: float | None = None
    interval: tuple[float, float] | None = None
    ev: evidence.Evidence | None = None
    level: str = "нет вывода"
    avg_check: float | None = None
    hours_per_work: float | None = None
    income: float = 0.0
    hours: float = 0.0
    paid_n: int = 0
    unsold_n: int = 0
    overhead_n: int = 0
    missing: list[Work] = field(default_factory=list)   # оплачено, но не в ₽ (кроме поинтов)

    @property
    def missing_n(self) -> int:
        return len(self.missing)


def _group(key: str, label: str, ws: list[Work]) -> Group:
    g = Group(key, label, ws)
    g.rub = [w for w in ws if w.status in ("unsold", "overhead") or (w.status == "paid" and w.income_rub is not None)]
    paid_rub = [w for w in g.rub if w.status == "paid"]
    g.paid_n = len(paid_rub)
    g.unsold_n = sum(1 for w in g.rub if w.status == "unsold")
    g.overhead_n = sum(1 for w in g.rub if w.status == "overhead")
    g.missing = [w for w in ws if w.status == "paid" and w.income_rub is None
                 and (not w.other or any(c != "points" for c in w.other))]
    g.income = sum(w.income_rub or 0.0 for w in g.rub)
    g.hours = sum(w.hours for w in g.rub)
    if paid_rub:
        g.avg_check = statistics.mean(w.income_rub or 0.0 for w in paid_rub)
        g.hours_per_work = statistics.median(w.hours for w in paid_rub)
    if paid_rub and g.hours > 0:
        g.rate = g.income / g.hours
        g.interval = ratio_ci(g.rub)
        g.ev = evidence.span([w.ts for w in g.rub], "her_sales", f"её работы: {label}, ₽ дошло за час", "rub",
                             "observed", n=len(g.rub), interval=g.interval, measure="price", value=g.rate)
        g.level = evidence.level(g.ev)
    return g


def _usable(g: Group) -> bool:
    return g.rate is not None and g.level != "нет вывода"


def compare(groups: list[Group]) -> dict | None:
    """Лидер по ₽/ч и против кого его перевес доказан (P ≥ порога на все пары). None — сравнивать не с чем."""
    ok = [g for g in groups if _usable(g)]
    if len(ok) < 2:
        return None
    thr = evidence.family_threshold(len(ok) * (len(ok) - 1) // 2)
    leader = max(ok, key=lambda g: (g.rate, g.key))
    beats: list[tuple[Group, float]] = []
    ties: list[tuple[Group, float]] = []
    for g in ok:
        if g is leader:
            continue
        p = p_rate_greater(leader.rub, g.rub)
        (beats if p >= thr else ties).append((g, p))
    with_check = [g for g in ok if g.avg_check is not None]
    seems = max(with_check, key=lambda g: (g.avg_check, g.key)) if with_check else None
    return {"leader": leader, "beats": beats, "ties": ties, "threshold": thr, "seems": seems}


@dataclass
class HourlyReport:
    days: int
    works: list[Work]
    formats: list[Group]
    niches: list[Group]
    overall: Group | None
    compare_formats: dict | None
    compare_niches: dict | None
    pending: list[Work]
    points: list[Work]


def _niche_label(key: str) -> str:
    kind, _, rest = key.partition(":")
    return f"{'вид' if kind == 'вид' else 'стиль'} {rest}"


def hourly(conn, now: float | None = None, days: int = HISTORY_DAYS) -> HourlyReport:
    now = now or time.time()
    ws = works(conn, now, days)
    settled = [w for w in ws if w.status != "pending"]
    by_fmt: dict[str, list[Work]] = defaultdict(list)
    by_niche: dict[str, list[Work]] = defaultdict(list)
    for w in settled:
        by_fmt[w.format].append(w)
        for s in w.tags.species:
            by_niche[f"вид:{s}"].append(w)
        for s in w.tags.styles:
            by_niche[f"стиль:{s}"].append(w)
    formats = [_group(f, FORMAT_NAMES.get(f, f), by_fmt[f]) for f in FORMATS if by_fmt.get(f)]
    formats.sort(key=lambda g: (-((g.rate or 0.0) if _usable(g) else -1), -len(g.rub), g.key))
    niches = [_group(k, _niche_label(k), v) for k, v in sorted(by_niche.items())]
    niches = [g for g in niches if g.rub]
    niches.sort(key=lambda g: (-((g.rate or 0.0) if _usable(g) else -1), -len(g.rub), g.key))
    return HourlyReport(days=days, works=ws, formats=formats, niches=niches,
                        overall=_group("all", "все форматы", settled) if settled else None,
                        compare_formats=compare(formats), compare_niches=compare(niches),
                        pending=[w for w in ws if w.status == "pending"],
                        points=[w for w in settled if w.status == "paid" and "points" in w.other])


def _other_text(ws: list[Work], points: bool = False, rub: bool = False) -> str:
    """Оплаченное без «сколько дошло» в ₽: points=True — поинты; rub=True — цена в ₽ без net; иначе — $, € и т.п."""
    totals: dict[str, float] = defaultdict(float)
    hours: dict[str, float] = defaultdict(float)
    for w in ws:
        for cur, amount in w.other.items():
            if (cur == "points") == points and (points or (cur == "rub") == rub):
                totals[cur] += amount
                hours[cur] += w.hours
    return ", ".join(f"{sales.money(v, None if c == '?' else c)} за {_h(hours[c])} ч" for c, v in totals.items())


def _group_line(g: Group) -> str:
    head = f"- {g.label}: "
    tail = []
    if g.unsold_n:
        tail.append(f"из них непроданных {g.unsold_n} (0 ₽)")
    if g.overhead_n:
        tail.append(f"общая работа {g.overhead_n} (0 ₽)")
    extra = ("; " + ", ".join(tail)) if tail else ""
    n = len(g.rub)
    if _usable(g):
        lo, hi = g.interval or (g.rate, g.rate)
        line = (f"{head}~{_rub(g.rate)}/ч (80%: {_rub(lo)}–{_rub(hi)}), {n} {_plural(n, WORKS_FORMS)}, "
                f"{_h(g.hours)} ч, дошло {_rub(g.income)}{extra} {evidence.tag(g.ev) if g.ev else ''}")
    elif g.rub and g.paid_n:
        line = (f"{head}вывода нет — {n} {_plural(n, WORKS_FORMS)}, {_h(g.hours)} ч, дошло {_rub(g.income)}{extra}; "
                f"нужно ≥{MIN_WORKS} работ")
    elif g.rub:
        line = (f"{head}вывода нет — {n} {_plural(n, WORKS_FORMS)}, {_h(g.hours)} ч, оплаченных с суммой в ₽ "
                f"пока нет{extra}")
    else:
        line = f"{head}в ₽ посчитать не из чего"
    gross = _other_text(g.missing, rub=True)
    if gross:
        line += (f"\n  оплачено, но не вписано, сколько дошло до тебя (цена {gross}) — не считала; "
                 "впиши — sales:status sale_id=… net=…")
    other = _other_text(g.missing)
    if other:
        line += f"\n  оплачено не в ₽ (с ₽ не смешиваю; впиши, сколько дошло — sales:status sale_id=… net=…): {other}"
    return line


def _compare_lines(cmp_: dict | None, what: str, seems_line: bool = True) -> list[str]:
    if not cmp_:
        return []
    leader = cmp_["leader"]
    out = []
    if cmp_["beats"]:
        names = ", ".join(g.label for g, _ in cmp_["beats"])
        out.append(f"Что выгоднее на самом деле ({what}): в час выгоднее всего {leader.label} — больше, чем {names} "
                   f"(P(больше) ≥ {cmp_['threshold']:.2f} по каждой паре).")
    if cmp_["ties"]:
        names = ", ".join(g.label for g, _ in cmp_["ties"])
        out.append(f"{leader.label.capitalize()} против {names}: разница в ₽/ч не доказана — считай их равными.")
    seems = cmp_["seems"]
    if seems_line and seems is not None and seems is not leader and any(g is seems for g, _ in cmp_["beats"]):
        out.append(f"Кажется, что выгоднее {seems.label}: средний чек выше ({_rub(seems.avg_check)} против "
                   f"{_rub(leader.avg_check)}), но на работу уходит больше времени: ~{_h(seems.hours_per_work)} ч "
                   f"против ~{_h(leader.hours_per_work)} ч (медианы).")
    return out


def render_hourly(rep: HourlyReport) -> str:
    if not rep.works:
        return ("Часов пока не записано. Скажи после адопта: «рисовала Dragon adopt 3.5 ч» — посчитаю, сколько ты "
                "на самом деле зарабатываешь в час по форматам.")
    lines = [f"Заработок в час за {rep.days} дн — сколько дошло до тебя в ₽ ÷ часы. Непроданное дольше "
             f"{SETTLE_DAYS} дн и общая работа идут с 0 ₽ — так честнее.", "По форматам:"]
    lines += [_group_line(g) for g in rep.formats] or ["- оплаченных работ с часами пока нет"]
    usable = [g for g in rep.formats if _usable(g)]
    if rep.compare_formats:
        lines += _compare_lines(rep.compare_formats, "форматы")
    elif len(rep.formats) > 1 or usable:
        lines.append(f"Сравнить форматы пока нельзя: нужно ≥{MIN_WORKS} работ хотя бы в двух форматах.")
    shown = [g for g in rep.niches if _usable(g)]
    if shown:
        lines.append("По видам и стилям:")
        lines += [_group_line(g) for g in shown[:6]]
        lines += _compare_lines(rep.compare_niches, "виды и стили", seems_line=False)
        lines.append("  Виды и форматы пересекаются: дракон-аукцион и дракон-набор — в одном виде.")
    small = len(rep.niches) - len(shown)
    if small:
        lines.append(f"Ещё {small} видов/стилей — меньше {MIN_WORKS} работ, вывода нет.")
    if rep.pending:
        hours = sum(w.hours for w in rep.pending)
        lines.append(f"Ещё продаются или ждут оплаты (не считала): {len(rep.pending)} "
                     f"{_plural(len(rep.pending), WORKS_FORMS)}, {_h(hours)} ч.")
    if rep.points:
        lines.append(f"Поинты отдельно: {_other_text(rep.points, points=True)} — {sales.POINTS_NOT_CASH}.")
    lines.append(GENTLE)
    return "\n".join(lines)


# --- Цель на месяц ---

def month_bounds(now: float, tz: str = DEFAULT_TZ, key: str | None = None) -> tuple[float, float, str]:
    """(начало, конец, "YYYY-MM") месяца по её часовому поясу; key — другой месяц."""
    zone = _zone(tz)
    if key:
        year, month = (int(x) for x in key.split("-"))
    else:
        local = datetime.fromtimestamp(now, zone)
        year, month = local.year, local.month
    start = datetime(year, month, 1, tzinfo=zone)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
    return start.timestamp(), end.timestamp(), f"{year:04d}-{month:02d}"


def _parse_month(month: str, now: float, tz: str) -> str:
    text = str(month or "").strip()
    _, _, cur = month_bounds(now, tz)
    if not text:
        return cur
    cy, cm = (int(x) for x in cur.split("-"))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", text) or None
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
    elif (m := re.fullmatch(r"(\d{1,2})[./](\d{4})", text)):
        mo, y = int(m.group(1)), int(m.group(2))
    elif (m := re.fullmatch(r"\d{1,2}", text)):
        mo = int(text)
        y = cy + (1 if mo < cm else 0)
    else:
        found = [i + 1 for i, name in enumerate(MONTHS) if _norm(text).startswith(name[:3])]
        if not found:
            raise EarningsError("Месяц — например «2026-12», «12» или «декабрь».")
        mo = found[0]
        y = cy + (1 if mo < cm else 0)
    if not 1 <= mo <= 12:
        raise EarningsError("Месяц — от 1 до 12.")
    return f"{y:04d}-{mo:02d}"


def set_goal(conn, target, month: str = "", now: float | None = None, tz: str = DEFAULT_TZ) -> str:
    """Её цель на месяц в ₽ (сколько должно дойти). Тот же месяц — перезапись."""
    ensure(conn)
    now = now or time.time()
    try:
        value = float(target)
    except (TypeError, ValueError):
        raise EarningsError("Цель — сумма в ₽, например 30000.") from None
    if not math.isfinite(value) or value <= 0 or value > MAX_TARGET:
        raise EarningsError(f"Цель — сумма в ₽ больше нуля и не больше {MAX_TARGET:,}.")
    key = _parse_month(month, now, tz)
    conn.execute("INSERT OR REPLACE INTO earn_goals (month, target, currency, set_ts) VALUES (?, ?, 'rub', ?)",
                 (key, value, now))
    conn.commit()
    name = MONTHS[int(key[5:]) - 1]
    head = f"Записала цель на {name} {key[:4]}: {_rub(value)}."
    if key == month_bounds(now, tz)[2]:
        return head + "\n" + render_goal(goal_status(conn, now, tz))
    return head


def _explicit_formats(conn) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in conn.execute("SELECT sale_id, item_id, format FROM work_log WHERE format IS NOT NULL ORDER BY ts"):
        for k in (r["sale_id"], r["item_id"]):
            if k:
                out[k] = r["format"]
    return out


def _deals(conn, rows, since: float | None = None, until: float | None = None) -> list[dict]:
    """Оплаченные продажи → сделки: предоплата и остаток одного слота заказа — одна сделка.

    Слот заказа — сделка, только когда оплачены все его части (не отменённые): одна предоплата — ещё не сделка,
    иначе средний чек занижен. Части берём из базы целиком, а в окно [since, until) смотрим по последней оплате."""
    ensure(conn)
    explicit = _explicit_formats(conn)
    deals: dict[str, dict] = {}
    seen_slots: set[str] = set()
    for s in rows:
        slot = _comm_slot(s["item_id"])
        key = f"comm:{slot}" if slot else s["id"]
        parts = [s]
        if slot:
            if slot in seen_slots:
                continue
            seen_slots.add(slot)
            parts = [p for p in _comm_sales(conn, slot) if p["status"] != "cancelled"]
            if not parts or any(p["status"] not in PAID or not p["paid_ts"] for p in parts) \
                    or _comm_waiting(conn, slot):
                continue
        fmt = (explicit.get(key) or explicit.get(s["id"]) or (explicit.get(s["item_id"]) if s["item_id"] else None)
               or (_comm_kind(conn, slot) if slot else None))
        d: dict[str, Any] = {"format": fmt or guess_format(s["title"], s["format"], s["item_id"]), "net": 0.0,
                             "ts": 0.0, "rub": True}
        for p in parts:
            d["ts"] = max(d["ts"], p["paid_ts"] or 0)
            if p["net_amount"] is not None and (p["net_currency"] or "rub") == "rub":
                d["net"] += p["net_amount"]
            else:
                d["rub"] = False
        if (since is not None and d["ts"] < since) or (until is not None and d["ts"] >= until):
            continue
        deals[key] = d
    return [d for d in deals.values() if d["rub"]]


def _mean_ci(values: list[float], level: float = 0.8, B: int = 1000, seed: int = 0) -> tuple[float, float]:
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(B))
    tail = (1 - level) / 2
    return means[int(tail * (B - 1))], means[int(round((1 - tail) * (B - 1)))]


def _avg(fmt: str, deals: list[dict], remaining: float) -> dict:
    vals = [d["net"] for d in deals]
    mean = statistics.mean(vals)
    ci = _mean_ci(vals)
    label = "все форматы" if fmt == "all" else FORMAT_NAMES.get(fmt, fmt)
    ev = evidence.span([d["ts"] for d in deals], "her_sales", f"её продажи: {label}, ₽ дошло", "rub", "observed",
                       n=len(vals), interval=ci, measure="price", value=mean)
    lvl = evidence.level(ev)
    count = math.ceil(remaining / mean) if mean > 0 and lvl != "нет вывода" and remaining > 0 else None
    return {"format": fmt, "mean": mean, "n": len(vals), "ev": ev, "level": lvl, "count": count}


def _counts(a: dict | None) -> bool:
    """По этому среднему можно сказать «ещё ~N»: вывод есть и в среднем доходят деньги (не 0 ₽)."""
    return a is not None and a["level"] != "нет вывода" and a["mean"] > 0 and a["count"] is not None


def goal_status(conn, now: float | None = None, tz: str = DEFAULT_TZ, rep: HourlyReport | None = None) -> dict:
    """Прогресс цели месяца. rep — уже посчитанный hourly(conn, now), чтобы не считать второй раз."""
    ensure(conn)
    now = now or time.time()
    start, end, key = month_bounds(now, tz)
    goal = conn.execute("SELECT * FROM earn_goals WHERE month = ?", (key,)).fetchone()
    month_rows = conn.execute(
        "SELECT * FROM sales WHERE status IN ('paid','transferred','done') AND paid_ts >= ? AND paid_ts < ?",
        (start, end)).fetchall()
    got, got_n, missing, points = 0.0, 0, [], 0.0
    for s in month_rows:
        if s["net_amount"] is not None and (s["net_currency"] or "rub") == "rub":
            got += s["net_amount"]
            got_n += 1
        elif s["currency"] == "points":
            points += s["amount"] or 0
        else:
            missing.append(s)
    waiting = conn.execute("SELECT COUNT(*) FROM sales WHERE (status = 'won' OR (status = 'transferred' AND "
                           "paid_ts IS NULL)) AND created_ts >= ?", (now - 30 * DAY,)).fetchone()[0]
    target = goal["target"] if goal else None
    remaining = max(0.0, target - got) if target is not None else None
    hist = conn.execute(
        "SELECT * FROM sales WHERE status IN ('paid','transferred','done') AND paid_ts >= ? AND paid_ts < ?",
        (now - GOAL_HISTORY_DAYS * DAY, now)).fetchall()
    deals = _deals(conn, hist, now - GOAL_HISTORY_DAYS * DAY, now)
    by_fmt: dict[str, list[dict]] = defaultdict(list)
    for d in deals:
        by_fmt[d["format"]].append(d)
    avgs = [_avg(f, v, remaining or 0) for f, v in sorted(by_fmt.items())]
    need = sorted([a for a in avgs if _counts(a)], key=lambda a: (a["count"], a["format"]))
    small = [a for a in avgs if a["level"] == "нет вывода"]
    # Вывод есть, но в среднем 0 ₽ (подарки, розыгрыши): цель так не набрать — «ещё ~N» не считаем.
    zero = [a for a in avgs if a["level"] != "нет вывода" and a["mean"] <= 0]
    overall = _avg("all", deals, remaining or 0) if deals else None
    rep = rep if rep is not None else hourly(conn, now)
    rate = rep.overall if rep.overall is not None and _usable(rep.overall) else None
    days_left = max(1, math.ceil((end - now) / DAY))
    elapsed = max(1.0, (now - start) / DAY)
    return {"month": key, "target": target, "got": got, "got_n": got_n, "remaining": remaining,
            "days_left": days_left, "elapsed": elapsed, "need": need, "small": small, "zero": zero,
            "overall": overall,
            "deals_n": len(deals), "missing": missing, "points": points, "waiting": waiting, "rate": rate}


def _month_name(key: str) -> str:
    return MONTHS[int(key[5:]) - 1]


def _need_line(a: dict) -> str:
    forms = FORMS.get(a["format"], FORMS["other"])
    label = " (все форматы)" if a["format"] == "all" else ""
    return (f"- ещё ~{a['count']} {_plural(a['count'], forms)}{label} — в среднем {_rub(a['mean'])} "
            f"{evidence.tag(a['ev'])}")


def render_goal(st: dict) -> str:
    name = _month_name(st["month"])
    lines: list[str] = []
    if st["target"] is None:
        lines.append(f"Цель на {name} не задана. Скажи сумму — например, «хочу 30 000 ₽ в этом месяце» — и я покажу, "
                     f"сколько уже есть и что для этого нужно. Сейчас за {name} дошло: {_rub(st['got'])} "
                     f"({st['got_n']} {_plural(st['got_n'], FORMS['all'])} с суммой в ₽).")
    else:
        share = st["got"] / st["target"] if st["target"] else 0
        lines.append(f"Цель на {name}: {_rub(st['target'])}. Уже дошло: {_rub(st['got'])} ({evidence.pct(share)}) — "
                     f"{st['got_n']} {_plural(st['got_n'], ('оплаченная продажа', 'оплаченные продажи', 'оплаченных продаж'))} "
                     "с суммой в ₽.")
        if st["remaining"] <= 0:
            lines.append("Цель достигнута — дальше всё сверху.")
        else:
            lines.append(f"Осталось {_rub(st['remaining'])} за {st['days_left']} дн: ~{_rub(st['remaining'] / st['days_left'])}"
                         f" в день (пока в этом месяце выходило ~{_rub(st['got'] / st['elapsed'])} в день).")
            overall = st["overall"]
            if st["need"]:
                lines.append(f"Что для этого нужно по твоим средним за {GOAL_HISTORY_DAYS} дн (сколько дошло в ₽), "
                             "любой из вариантов:")
                lines += [_need_line(a) for a in st["need"]]
            elif _counts(overall):
                lines.append(_need_line(overall))
            elif overall and overall["level"] != "нет вывода":
                lines.append(f"Вывода нет: по твоим продажам за {GOAL_HISTORY_DAYS} дн в среднем дошло "
                             f"{_rub(overall['mean'])} — сколько ещё продаж нужно, отсюда не посчитать.")
            else:
                have = overall["n"] if overall else 0
                lines.append(evidence.refuse("сколько ещё продаж нужно",
                                             f"{have} продаж с суммой в ₽ за {GOAL_HISTORY_DAYS} дн", MIN_WORKS)
                             + " Впиши, сколько дошло, по прошлым продажам (sales:status sale_id=… net=…) — посчитаю.")
            if st.get("zero"):
                lines.append("В среднем 0 ₽ дошло: " + ", ".join(FORMAT_NAMES.get(a["format"], a["format"])
                                                                 for a in st["zero"])
                             + " — к цели они не приближают, в варианты не включила.")
            if st["small"] and (st["need"] or _counts(overall)):
                lines.append("По этим форматам продаж мало — вывода нет: "
                             + ", ".join(f"{FORMAT_NAMES.get(a['format'], a['format'])} ({a['n']} из {MIN_WORKS})"
                                         for a in st["small"]) + ".")
            g = st["rate"]
            if g is not None and g.rate:
                lines.append(f"По времени: при твоих ~{_rub(g.rate)}/ч это ещё ~{_h(round(st['remaining'] / g.rate))} ч "
                             f"работы {evidence.tag(g.ev)}")
    if st["missing"]:
        n = len(st["missing"])
        gross = ", ".join(sales.money(s["amount"], s["currency"]) for s in st["missing"][:5])
        lines.append(f"Не вошли: {n} {_plural(n, ('оплаченная продажа', 'оплаченные продажи', 'оплаченных продаж'))}, "
                     f"где не вписано, сколько дошло до тебя в ₽ (цена: {gross}) — впиши (sales:status sale_id=… net=…); "
                     "курс не угадываю.")
    if st["points"]:
        lines.append(f"Поинты в цель не входят: {sales.money(st['points'], 'points')} — {sales.POINTS_NOT_CASH}.")
    if st["waiting"]:
        lines.append(f"Ждут оплаты: {st['waiting']} — войдут, когда оплатят и ты впишешь, сколько дошло.")
    return "\n".join(lines)


# --- Сколько она успевает ---

def set_capacity(conn, per_week, now: float | None = None) -> str:
    ensure(conn)
    now = now or time.time()
    try:
        n = float(per_week)
    except (TypeError, ValueError):
        raise EarningsError("Сколько адоптов в неделю — целое число, например 3.") from None
    if not math.isfinite(n) or n != int(n) or not 0 <= n <= MAX_CAPACITY:
        raise EarningsError(f"Сколько адоптов в неделю — целое от 0 до {MAX_CAPACITY}.")
    if n == 0:
        conn.execute("DELETE FROM earn_prefs WHERE key = 'adopts_per_week'")
        conn.commit()
        return "Забыла — буду прикидывать по твоим публикациям."
    conn.execute("INSERT OR REPLACE INTO earn_prefs (key, value, ts) VALUES ('adopts_per_week', ?, ?)",
                 (str(int(n)), now))
    conn.commit()
    return f"Запомнила: {int(n)} адопт(а/ов) в неделю — так и буду планировать."


def capacity(conn, now: float | None = None) -> dict:
    """{"value": int|None, "source": "her"|"history"|None, "text"} — её число или по её публикациям."""
    ensure(conn)
    now = now or time.time()
    row = conn.execute("SELECT value FROM earn_prefs WHERE key = 'adopts_per_week'").fetchone()
    if row:
        v = int(row["value"])
        return {"value": v, "source": "her", "text": f"{v} в неделю (как ты сказала)"}
    from .selling import SELL_TYPES

    since = now - CAPACITY_WEEKS * 7 * DAY
    counts = [0] * CAPACITY_WEEKS
    # Только её адопты на DA (аукцион/фикс/OTA): скетчи, примеры YCH и лоты в тредах Toyhouse — не адопты недели.
    types = ",".join("?" * len(SELL_TYPES))
    for r in conn.execute("SELECT COALESCE(published_ts, first_seen_ts) AS t FROM items WHERE own = 1 "
                          f"AND source = 'deviantart' AND lot_type IN ({types}) "
                          "AND COALESCE(published_ts, first_seen_ts) >= ? AND COALESCE(published_ts, first_seen_ts) < ?",
                          (*SELL_TYPES, since, now)):
        counts[min(CAPACITY_WEEKS - 1, int((now - r["t"]) // (7 * DAY)))] += 1
    active = [c for c in counts if c]
    if len(active) >= MIN_ACTIVE_WEEKS:
        v = max(1, round(statistics.median(active)))
        return {"value": v, "source": "history",
                "text": f"~{v} в неделю — по твоим публикациям (активных недель {len(active)} из {CAPACITY_WEEKS})"}
    return {"value": None, "source": None, "text": ""}


# --- План недели ---

def _default_niches(conn, now):
    from . import forecast

    return forecast.niches(conn, now)


def _default_best(conn, now, tz):
    from . import advisor

    return advisor.best_publish_time(conn, now, tz)


def _default_unsold(conn, now, tz):
    from . import selling

    return selling.unsold(conn, now, tz=tz, limit=3)


def _default_ending(conn, now, tz):
    from . import selling

    return selling.ending_soon(conn, now, within_h=7 * 24, tz=tz)


def _niche_name(key: str) -> str:
    from .forecast import SEASON_LABEL

    kind, _, rest = key.partition(":")
    if kind == "сезон":
        return SEASON_LABEL.get(rest, rest)
    return key.replace("вид:", "").replace("стиль:", "").replace("тема:", "").replace("+", " + ")


def slot_times(best: dict, now: float, tz: str, count: int) -> list[float]:
    """count моментов на ближайшие 7 дней в лучшем окне (будни/выходные × часть суток), по одному в день;
    если дней в окне меньше — второй лот в тот же день на 2 ч позже, третий — на 4 ч (окно — 6 ч).
    Больше 3 в день в окно не помещается: моментов будет меньше count, без повторов."""
    if not count or not best or not best.get("slot"):
        return []
    kind, part = best["slot"][0], int(best["slot"][1])
    zone = _zone(tz)
    today = datetime.fromtimestamp(now, zone).date()
    days = []
    for d in range(8):
        day = today + timedelta(days=d)
        at = datetime(day.year, day.month, day.day, part * 6, tzinfo=zone)
        if at.timestamp() <= now or at.timestamp() > now + 7 * DAY:
            continue
        if ("weekend" if at.weekday() >= 5 else "weekday") == kind:
            days.append(at.timestamp())
    if not days:
        return []
    out = []
    for i in range(min(count, 3 * len(days))):
        shift = 2 * (i // len(days)) * HOUR
        out.append(days[i % len(days)] + shift)
    return sorted(out)


def _ending_line(d: dict, zone: ZoneInfo) -> str:
    """Строка «закончится» из selling.ending_soon. У срока «только дата» готовый text говорит «сегодня последний
    день» (он писался для окна в 3 ч), а в плане на неделю это неправда — пишем дату сами."""
    title = str(d.get("title") or "")[:60]
    end_ts = d.get("end_ts")
    if end_ts and d.get("precision") == "day":
        return f"«{title}» — до конца {datetime.fromtimestamp(end_ts, zone):%d.%m} (время в описании не указано)"
    if d.get("text"):
        return str(d["text"])
    if end_ts:
        local = datetime.fromtimestamp(end_ts, zone)
        return f"«{title}» — закончится {WEEKDAYS[local.weekday()]} {local:%d.%m %H:%M}"
    return f"«{title}»"


def week_plan(conn, cfg=None, now: float | None = None, *, tz: str | None = None, niches_fn=None, best_time_fn=None,
              unsold_fn=None, ending_fn=None) -> dict:
    """План недели: что рисовать (ниши рынка с доказанным перевесом), какой формат (её ₽/ч), когда выкладывать
    (лучшее окно по данным), что подтолкнуть (непроданное) и что закрывается. Ничего не придумывает и не делает.

    {"capacity", "slots": [ts], "draw": [названия ниш], "ask": вопрос или None, "text"}."""
    ensure(conn)
    now = now or time.time()
    tz = tz or _tz(cfg)
    zone = _zone(tz)
    notes: list[str] = []

    def safe(what: str, fn, *args):
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001 — план не должен падать из-за одного источника
            notes.append(f"{what} не посчитались — план без них")
            return None

    cap = capacity(conn, now)
    end = datetime.fromtimestamp(now + 7 * DAY, zone)
    lines = [f"План на неделю (до {end:%d.%m}, время — {tz}). Только из данных; спрос не придумываю."]
    lines.append(f"Успеваешь: {cap['text']}." if cap["value"] else "Успеваешь: пока не знаю.")

    # Что рисовать.
    rep = safe("Ниши рынка", niches_fn or _default_niches, conn, now)
    draw: list[str] = []
    if rep is not None and getattr(rep, "refusal", None):
        refusal = str(rep.refusal).split("\n")[0]
        lines.append("Что рисовать: " + (refusal if refusal.startswith("Вывода нет") else f"вывода нет — {refusal}"))
    elif rep is not None:
        hot = [d for d in getattr(rep, "niches", []) if d.get("verdict") == "sells"][:3]
        cold = [d for d in getattr(rep, "niches", []) if d.get("verdict") == "saturated"][:2]
        if hot:
            lines.append("Что рисовать — ниши, где аукционы чаще получают ставку:")
            for d in hot:
                kr, nr = d.get("rest", (0, 0))
                rest = f", у остальных {evidence.pct(kr / nr)}" if nr else ""
                lines.append(f"- {_niche_name(d['key'])}: ставку получили {d['sold']} из {d['lots']} "
                             f"({evidence.pct(d['p_sold'])}){rest} {evidence.tag(d['ev'])}")
                draw.append(_niche_name(d["key"]))
        else:
            lines.append("Что рисовать: ниш, где ставку получают заметно чаще, по данным не видно — вывода нет.")
        if cold:
            lines.append("Пореже: " + ", ".join(_niche_name(d["key"]) for d in cold)
                         + " — похожих на рынке много, а ставку получают реже.")
    money = hourly(conn, now)
    nc = money.compare_niches
    if nc and nc["beats"]:
        g = nc["leader"]
        lines.append(f"По твоим ₽/ч выгоднее всего: {g.label} (~{_rub(g.rate)}/ч) {evidence.tag(g.ev)}")
    if not draw and not (nc and nc["beats"]):
        lines.append("Рисуй то, что хочется: подтверждённого спроса в данных нет, выдумывать не буду.")

    # Формат.
    fc = money.compare_formats
    if fc and fc["beats"]:
        g = fc["leader"]
        others = ", ".join(f"{o.label} ~{_rub(o.rate)}/ч" for o, _ in fc["beats"])
        tie = (f"; с {', '.join(o.label for o, _ in fc['ties'])} разница не доказана" if fc["ties"] else "")
        lines.append(f"Формат: {g.label} — у тебя ~{_rub(g.rate)}/ч; меньше: {others}{tie} {evidence.tag(g.ev)}")
    else:
        lines.append(f"Формат: на твой выбор — разница в ₽/ч по твоим записям не доказана (нужно ≥{MIN_WORKS} "
                     "работ с часами хотя бы в двух форматах).")

    # Когда.
    best = safe("Лучшее время", best_time_fn or _default_best, conn, now, tz)
    good = bool(best) and best.get("level") not in WEAK_LEVELS and bool(best.get("slot"))
    slots = slot_times(best, now, tz, cap["value"] or 0) if good else []
    if good:
        lines.append(f"Когда выкладывать (по данным): {best.get('evidence') or best.get('when', '')}")
        for i, ts in enumerate(slots):
            local = datetime.fromtimestamp(ts, zone)
            what = f" — {draw[i % len(draw)]}" if draw else ""
            lines.append(f"- {WEEKDAYS[local.weekday()]} {local:%d.%m} с {local:%H:%M}: адопт {i + 1}{what}")
        extra = (cap["value"] or 0) - len(slots)
        if slots and extra > 0:
            lines.append(f"- ещё {extra} — когда удобно: в лучшее окно больше 3 в день не ставлю, "
                         "чтобы адопты не перебивали друг друга.")
    else:
        lines.append("Когда: лучшего окна по данным не видно — выкладывай, когда тебе удобно.")

    # Что подтолкнуть и что закрывается.
    unsold = safe("Непроданные лоты", unsold_fn or _default_unsold, conn, now, tz) or {}
    lots = list(unsold.get("lots") or [])[:3]
    if lots:
        lines.append("Подтолкнуть (второй заход, без перезалива):")
        lines += [f"- «{lot['title']}» — {lot.get('why', '')}".rstrip(" —") for lot in lots]
        lines.append("  План по каждому — promo repromote.")
    ending = safe("Сроки аукционов", ending_fn or _default_ending, conn, now, tz) or []
    if ending:
        lines.append("Закончатся на этой неделе:")
        lines += [f"- {_ending_line(d, zone)}" for d in list(ending)[:5]]

    # Цель.
    st = goal_status(conn, now, tz, rep=money)
    if st["target"] is not None and st["remaining"] and st["remaining"] > 0:
        line = f"Цель на {_month_name(st['month'])}: осталось {_rub(st['remaining'])} за {st['days_left']} дн"
        if st["need"]:
            a = st["need"][0]
            line += f" — по твоим средним это ~{a['count']} {_plural(a['count'], FORMS.get(a['format'], FORMS['other']))}"
        lines.append(line + ".")
    elif st["target"] is not None:
        lines.append(f"Цель на {_month_name(st['month'])} уже достигнута.")

    ask = None
    if not cap["value"]:
        ask = ("Сколько адоптов в неделю ты успеваешь нарисовать? Скажи число — запомню и разложу по дням "
               "(sales plan amount=N).")
        lines.append(ask)
    lines += notes
    return {"capacity": cap["value"], "slots": slots, "draw": draw, "ask": ask, "text": "\n".join(lines)}


# --- Вход для mcp_server ---

ACTIONS = ("hours", "hours_link", "hours_undo", "hourly", "goal", "capacity", "week")


def handle(conn, cfg, action: str, *, ref: str = "", hours="", format: str = "", overhead: bool = False,
           note: str = "", sale_id: str = "", target=None, month: str = "", per_week=None,
           days: int | None = None, now: float | None = None, **fns) -> str:
    """Одна точка входа для инструментов: hours, hours_link, hours_undo, hourly, goal, capacity, week."""
    tz = _tz(cfg)
    try:
        if action == "hours":
            if hours in ("", None):
                return "Нужны часы: например «3.5 ч» или «90 мин» — и что рисовала (название, ссылка или id продажи)."
            return log_hours(conn, ref, hours, format=format, overhead=overhead, note=note, now=now)["text"]
        if action == "hours_link":
            return link(conn, ref, sale_id)
        if action == "hours_undo":
            return undo(conn, ref)
        if action == "hourly":
            return render_hourly(hourly(conn, now, int(days or HISTORY_DAYS)))
        if action == "goal":
            if target is not None:
                return set_goal(conn, target, month, now, tz)
            return render_goal(goal_status(conn, now, tz))
        if action == "capacity":
            if per_week is not None:
                return set_capacity(conn, per_week, now)
            cap = capacity(conn, now)
            return (f"Успеваешь: {cap['text']}." if cap["value"] else
                    "Пока не знаю, сколько адоптов в неделю ты успеваешь — скажи число.")
        if action == "week":
            return week_plan(conn, cfg, now, tz=tz, **fns)["text"]
    except EarningsError as e:
        return str(e)
    return f"action: {', '.join(ACTIONS)}."


SALES_ACTIONS = ("hours", "hourly", "goal", "plan")


def sales_action(conn, cfg, action: str, *, title: str = "", sale_id: str = "", url: str = "",
                 amount: float | None = None, format: str = "", status: str = "", note: str = "",
                 days: int | None = None, now: float | None = None, **fns) -> str:
    """Те же действия под параметры инструмента sales (без новых полей в схеме):
    hours — amount = часы, что рисовала — sale_id | url | title (comm:<слот> тоже), format, status="overhead" —
            общая работа формата; amount=0 — убрать запись (title = id записи или название работы — последнюю
            с ним; пусто — последнюю вообще);
            без amount, с sale_id и title — привязать записи «title» к продаже;
    hourly — ₽/ч за days; goal — amount = цель в ₽ на этот месяц, без amount — прогресс;
    plan — план недели, amount = сколько адоптов в неделю она успевает (запомнить)."""
    fmt = "" if (format or "").strip().lower() == "other" and action == "hours" else format
    if action == "hours":
        if amount is None:
            if (sale_id or "").strip() and (title or "").strip():
                try:
                    return link(conn, title, sale_id)
                except EarningsError as e:
                    return str(e)
            return handle(conn, cfg, "hours", ref=sale_id or url or title, hours="", now=now)
        if amount == 0:
            return handle(conn, cfg, "hours_undo", ref=title, now=now)
        return handle(conn, cfg, "hours", ref=sale_id or url or title, hours=amount, format=fmt,
                      overhead=(status or "").strip().lower() == "overhead", note=note, now=now)
    if action == "hourly":
        return handle(conn, cfg, "hourly", days=days, now=now)
    if action == "goal":
        return handle(conn, cfg, "goal", target=amount, now=now)
    if action == "plan":
        head = ""
        if amount is not None:
            try:
                head = set_capacity(conn, amount, now) + "\n"
            except EarningsError as e:
                return str(e)
        return head + handle(conn, cfg, "week", now=now, **fns)
    return f"action: {', '.join(SALES_ACTIONS)}."
