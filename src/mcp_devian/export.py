"""Выгрузка её продаж (и, по желанию, лотов с итогами) в CSV — для таблицы, налогов, учёта.

Файл открывается в русском Excel и Numbers без настройки: разделитель «;», десятичная запятая,
UTF-8 с BOM. Суммы разнесены по колонкам валют, чтобы их можно было складывать формулой.
Даты — в её часовом поясе. Текст, похожий на формулу (=, +, -, @), экранируется апострофом.
"""

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import sales
from .config import DATA_DIR

DEFAULT_TZ = "Europe/Moscow"
CURRENCY_COLUMNS = (("usd", "Сумма USD"), ("eur", "Сумма EUR"), ("gbp", "Сумма GBP"), ("points", "Поинты"))
NO_CURRENCY_COLUMN = "Сумма (валюта ?)"  # сумма записана, а валюта нет — не теряем её молча
LOT_TYPES = ("auction", "bta", "flatsale", "ota", "adopt", "raffle")


def fallback_dir() -> Path:
    return DATA_DIR / "exports"


def default_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else fallback_dir()


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _date(ts: float | None, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, zone).strftime("%Y-%m-%d %H:%M") if ts else ""


def _num(value: float | None) -> str:
    if value is None:
        return ""
    v = round(float(value), 2)
    return (str(int(v)) if v == int(v) else f"{v:.2f}".rstrip("0")).replace(".", ",")


def _text(value) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while (candidate := path.with_name(f"{path.stem}-{n}{path.suffix}")).exists():
        n += 1
    return candidate


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow(header)
        w.writerows(rows)


def _sales_rows(conn, zone: ZoneInfo, since: float | None) -> list[list[str]]:
    out = []
    for r in reversed(sales.list_sales(conn, since=since)):   # старые сверху
        amounts = ["" if r["currency"] != cur else _num(r["amount"]) for cur, _ in CURRENCY_COLUMNS]
        known = r["currency"] in {cur for cur, _ in CURRENCY_COLUMNS}
        amounts.append("" if known else _num(r["amount"]))
        out.append([_date(r["created_ts"], zone), _text(r["title"]), _text(r["buyer"]), r["platform"], r["format"],
                    sales.STATUS_NAMES.get(r["status"], r["status"]), *amounts,
                    _date(r["paid_ts"], zone), _date(r["done_ts"], zone), _text(r["character_url"]),
                    _text(r["note"]), r["id"]])
    return out


def _outcome(r) -> str:
    if r["sale_status"]:
        return sales.STATUS_NAMES.get(r["sale_status"], r["sale_status"])
    n = r["n_bids"]
    if r["lot_type"] in ("auction", "bta"):
        if n is None:
            return "ставки не читались"
        if r["status"] == "closed" or r["ended"]:
            return "закрыт со ставками" if n else "без ставок"
        return "идёт"
    return "закрыт" if r["status"] == "closed" else "открыт"


def _lots_rows(conn, zone: ZoneInfo, since: float | None) -> list[list[str]]:
    types = ",".join(f"'{t}'" for t in LOT_TYPES)
    rows = conn.execute(
        "SELECT i.*, a.n_bids, a.top_amount, a.currency AS a_currency, a.ended, s.status AS sale_status FROM items i "
        "LEFT JOIN auctions a ON a.deviation_id = i.id LEFT JOIN sales s ON s.item_id = i.id "
        f"WHERE i.source = 'deviantart' AND i.own = 1 AND i.lot_type IN ({types}) "
        "AND COALESCE(i.published_ts, i.first_seen_ts) >= ? ORDER BY COALESCE(i.published_ts, i.first_seen_ts)",
        (since or 0,)).fetchall()
    out = []
    for r in rows:
        unit = "usd" if r["price_usd"] is not None else "points" if r["price_points"] is not None else None
        start = r["price_usd"] if unit == "usd" else r["price_points"]
        try:
            tags = ", ".join(json.loads(r["extra"] or "{}").get("tags") or [])
        except (json.JSONDecodeError, TypeError, AttributeError):
            tags = ""
        out.append([_date(r["published_ts"] or r["first_seen_ts"], zone), _text(r["title"]), r["lot_type"] or "",
                    _num(start), (unit or "").upper().replace("POINTS", "поинты"),
                    "" if r["n_bids"] is None else str(r["n_bids"]), _num(r["top_amount"]),
                    _outcome(r), _date(r["closed_ts"], zone), _text(r["url"]), _text(tags)])
    return out


def export_sales(conn, *, out_dir: str | Path | None = None, tz: str = DEFAULT_TZ, since: float | None = None,
                 include_lots: bool = False, now: float | None = None) -> dict:
    """Продажи → CSV (по умолчанию в ~/Downloads). include_lots — второй файл с её лотами и итогами.

    Возвращает {"path", "rows", "lots_path"?, "lots"?}."""
    if out_dir is None:
        try:
            return export_sales(conn, out_dir=default_dir(), tz=tz, since=since, include_lots=include_lots, now=now)
        except PermissionError:
            # macOS может не пустить в «Загрузки» процесс, запущенный из Claude Desktop: кладём в свою папку.
            res = export_sales(conn, out_dir=fallback_dir(), tz=tz, since=since, include_lots=include_lots, now=now)
            return {**res, "fallback": True}
    now = now or time.time()
    zone = _zone(tz)
    folder = Path(out_dir).expanduser()
    stamp = datetime.fromtimestamp(now, zone).strftime("%Y-%m-%d")
    header = [f"Дата продажи ({tz})", "Название", "Покупатель", "Площадка", "Формат", "Статус",
              *(name for _, name in CURRENCY_COLUMNS), NO_CURRENCY_COLUMN, "Оплачено", "Завершено", "Перс", "Заметка", "ID"]
    rows = _sales_rows(conn, zone, since)
    path = _unique(folder / f"продажи_{stamp}.csv")
    _write(path, header, rows)
    result: dict = {"path": str(path), "rows": len(rows)}
    if include_lots:
        lots_header = [f"Выложен ({tz})", "Название", "Тип", "Старт", "Валюта", "Ставок", "Максимум", "Итог",
                       "Закрыт", "Ссылка", "Теги"]
        lots = _lots_rows(conn, zone, since)
        lots_path = _unique(folder / f"лоты_{stamp}.csv")
        _write(lots_path, lots_header, lots)
        result.update(lots_path=str(lots_path), lots=len(lots))
    return result


def render(result: dict) -> str:
    text = f"Продажи: {result['rows']} строк → {result['path']}"
    if "lots_path" in result:
        text += f"\nЛоты: {result['lots']} строк → {result['lots_path']}"
    if result.get("fallback"):
        text += "\nВ «Загрузки» macOS не пустил — положила в папку помощника."
    return text
