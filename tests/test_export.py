import csv
from datetime import UTC, datetime

import pytest

from mcp_devian import db, export, sales
from mcp_devian.db import Item

T = datetime(2026, 10, 31, 21, 30, tzinfo=UTC).timestamp()   # в Москве уже 1 ноября 00:30


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "e.sqlite3")
    yield c
    c.close()


def read(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh, delimiter=";"))


def test_sales_csv(conn, tmp_path):
    a = sales.add_sale(conn, title="Moth dragon", amount=40.5, currency="usd", platform="deviantart",
                       format="auction", buyer="bob", now=T)
    sales.set_status(conn, a, "paid", T + 3600)
    sales.add_sale(conn, title="=HYPERLINK(\"http://x\")", amount=1500, currency="points", platform="deviantart",
                   format="flatsale", buyer="@amy", now=T + 60)
    sales.add_sale(conn, title="Cat", amount=None, currency=None, platform="toyhouse", format="ota", now=T + 120)
    res = export.export_sales(conn, out_dir=tmp_path, tz="Europe/Moscow", now=T + 7200)
    assert res["rows"] == 3 and res["path"].endswith(".csv")
    rows = read(res["path"])
    head = rows[0]
    assert head[:4] == ["Дата продажи (Europe/Moscow)", "Название", "Покупатель", "Площадка"]
    for col in ("Сумма USD", "Сумма EUR", "Сумма GBP", "Поинты", "Статус", "Оплачено"):
        assert col in head
    by = {r[head.index("Название")]: dict(zip(head, r, strict=True)) for r in rows[1:]}
    moth = by["Moth dragon"]
    assert moth["Дата продажи (Europe/Moscow)"] == "2026-11-01 00:30"
    assert moth["Сумма USD"] == "40,5" and moth["Поинты"] == "" and moth["Статус"] == "оплачено"
    assert moth["Оплачено"] == "2026-11-01 01:30"
    pts = by["'=HYPERLINK(\"http://x\")"]                                 # формула не выполнится
    assert pts["Поинты"] == "1500" and pts["Сумма USD"] == "" and pts["Покупатель"] == "'@amy"
    assert by["Cat"]["Сумма USD"] == "" and by["Cat"]["Площадка"] == "toyhouse"


def test_unique_file_names(conn, tmp_path):
    sales.add_sale(conn, title="A", amount=1, currency="usd", platform="other", format="other", now=T)
    p1 = export.export_sales(conn, out_dir=tmp_path, now=T)["path"]
    p2 = export.export_sales(conn, out_dir=tmp_path, now=T)["path"]
    assert p1 != p2


def test_since_filter_and_empty(conn, tmp_path):
    sales.add_sale(conn, title="Old", amount=1, currency="usd", platform="other", format="other", now=T - 90 * 86400)
    sales.add_sale(conn, title="New", amount=2, currency="usd", platform="other", format="other", now=T)
    res = export.export_sales(conn, out_dir=tmp_path, since=T - 86400, now=T)
    assert res["rows"] == 1
    assert [r[1] for r in read(res["path"])[1:]] == ["New"]


def test_lots_csv(conn, tmp_path):
    db.upsert_item(conn, Item(id="da:1", source="deviantart", title="Moth dragon", url="https://www.deviantart.com/h/art/m-1",
                              author="her", lot_type="auction", status="closed", price_usd=10.0, own=True,
                              published_ts=T), now=T)
    db.upsert_item(conn, Item(id="da:2", source="deviantart", title="Not hers", url="https://www.deviantart.com/x/art/n-2",
                              author="x", lot_type="auction", status="open", price_usd=10.0, published_ts=T), now=T)
    conn.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, ended, winner, updated_ts) "
                 "VALUES ('da:1', 40, 'usd', 3, 1, 'bob', ?)", (T,))
    conn.commit()
    sales.sync_from_auctions(conn, now=T)
    res = export.export_sales(conn, out_dir=tmp_path, include_lots=True, now=T)
    rows = read(res["lots_path"])
    head = rows[0]
    assert len(rows) == 2
    lot = dict(zip(head, rows[1], strict=True))
    assert lot["Название"] == "Moth dragon" and lot["Старт"] == "10" and lot["Ставок"] == "3"
    assert lot["Максимум"] == "40" and lot["Валюта"] == "USD" and lot["Итог"] == "ждёт оплаты"


def test_default_dir_is_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(export.Path, "home", lambda: tmp_path)
    (tmp_path / "Downloads").mkdir()
    assert export.default_dir() == tmp_path / "Downloads"


def test_sale_without_currency_keeps_its_amount(conn, tmp_path):
    sales.add_sale(conn, title="Ash", amount=4000, currency=None, platform="toyhouse", format="auction", now=T)
    head, row = read(export.export_sales(conn, out_dir=tmp_path, tz="Europe/Moscow", now=T + 60)["path"])
    got = dict(zip(head, row, strict=True))
    assert got["Сумма (валюта ?)"] == "4000" and got["Сумма USD"] == ""
