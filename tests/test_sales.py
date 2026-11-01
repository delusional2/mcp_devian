import pytest

from mcp_devian import db, sales
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.sqlite3")
    yield c
    c.close()


def test_add_and_walk_statuses(conn):
    sid = sales.add_sale(conn, title="Moth #3", amount=40, currency="usd", platform="deviantart",
                         format="auction", buyer="bob", now=NOW)
    row = sales.get(conn, sid)
    assert row["status"] == "won" and row["paid_ts"] is None
    sales.set_status(conn, sid, "paid", now=NOW + 10)
    assert sales.get(conn, sid)["paid_ts"] == NOW + 10
    sales.set_status(conn, sid, "done", now=NOW + 20)
    row = sales.get(conn, sid)
    assert row["status"] == "done" and row["paid_ts"] == NOW + 10 and row["done_ts"] == NOW + 20


def test_back_to_won_clears_payment(conn):
    sid = sales.add_sale(conn, title="X", amount=5, currency="usd", platform="toyhouse", format="flatsale",
                         status="paid", now=NOW)
    sales.set_status(conn, sid, "won", now=NOW + 1)
    assert sales.get(conn, sid)["paid_ts"] is None


def test_validation(conn):
    with pytest.raises(sales.SaleError):
        sales.add_sale(conn, title=" ", amount=1, currency="usd", platform="deviantart", format="auction")
    with pytest.raises(sales.SaleError):
        sales.add_sale(conn, title="X", amount=-1, currency="usd", platform="deviantart", format="auction")
    with pytest.raises(sales.SaleError):
        sales.add_sale(conn, title="X", amount=1, currency="btc", platform="deviantart", format="auction")
    sid = sales.add_sale(conn, title="X", amount=1, currency="usd", platform="other", format="weird", now=NOW)
    assert sales.get(conn, sid)["format"] == "other"
    with pytest.raises(sales.SaleError):
        sales.set_status(conn, "nope", "paid")
    sales.set_status(conn, sid, "cancelled", now=NOW)
    with pytest.raises(sales.SaleError):
        sales.set_status(conn, sid, "done", now=NOW)


def test_income_by_currency_and_period(conn):
    sales.add_sale(conn, title="A", amount=30, currency="usd", platform="deviantart", format="auction",
                   status="paid", now=NOW - DAY)
    sales.add_sale(conn, title="B", amount=10, currency="usd", platform="toyhouse", format="flatsale",
                   status="done", now=NOW - 2 * DAY)
    sales.add_sale(conn, title="C", amount=500, currency="points", platform="deviantart", format="auction",
                   status="transferred", now=NOW - DAY)
    sales.add_sale(conn, title="D", amount=99, currency="usd", platform="deviantart", format="auction",
                   status="won", now=NOW - DAY)                       # не оплачено — не доход
    sales.add_sale(conn, title="E", amount=70, currency="usd", platform="deviantart", format="auction",
                   status="paid", now=NOW - 30 * DAY)                 # вне периода
    inc = sales.income(conn, NOW - 7 * DAY, NOW)
    assert inc["usd"]["total"] == 40 and inc["usd"]["count"] == 2 and inc["usd"]["avg"] == 20
    assert inc["usd"]["by_platform"] == {"deviantart": 30, "toyhouse": 10}
    assert inc["points"]["total"] == 500


def test_unpaid(conn):
    old = sales.add_sale(conn, title="Old", amount=5, currency="usd", platform="deviantart", format="auction",
                         now=NOW - 5 * DAY)
    sales.add_sale(conn, title="Fresh", amount=5, currency="usd", platform="deviantart", format="auction",
                   now=NOW - DAY)
    assert [r["id"] for r in sales.unpaid(conn, 3, NOW)] == [old]


def test_sync_from_auctions_once(conn):
    db.upsert_item(conn, Item(id="da:1", source="deviantart", title="My moth", url="u", lot_type="auction",
                              own=True), now=NOW)
    db.upsert_item(conn, Item(id="da:2", source="deviantart", title="Not mine", url="u", lot_type="auction"),
                   now=NOW)
    for did in ("da:1", "da:2"):
        conn.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, ended, winner, updated_ts) "
                     "VALUES (?, 45, 'usd', 4, 1, 'alice', ?)", (did, NOW))
    conn.commit()
    created = sales.sync_from_auctions(conn, NOW)
    assert len(created) == 1
    row = sales.get(conn, created[0])
    assert (row["item_id"], row["buyer"], row["amount"], row["status"], row["source"]) == \
        ("da:1", "alice", 45, "won", "auto")
    assert sales.sync_from_auctions(conn, NOW) == []


def test_money_and_describe(conn):
    assert sales.money(12.5, "usd") == "$12.50"
    assert sales.money(300, "points") == "300 pts"
    assert sales.money(None, "usd") == "?"
    sid = sales.add_sale(conn, title="Moth", amount=20, currency="eur", platform="deviantart", format="auction",
                         buyer="bob", now=NOW)
    assert "€20" in sales.describe(sales.get(conn, sid)) and "ждёт оплаты" in sales.describe(sales.get(conn, sid))


def test_missing_currency_is_unknown_not_dollars(conn):
    sales.add_sale(conn, title="A", amount=30, currency="usd", platform="deviantart", format="auction",
                   status="paid", now=NOW - DAY)
    sid = sales.add_sale(conn, title="B", amount=500, currency=None, platform="deviantart", format="auction",
                         status="paid", now=NOW - DAY)
    inc = sales.income(conn, NOW - 7 * DAY, NOW)
    assert inc == {"usd": {"total": 30, "count": 1, "avg": 30, "by_platform": {"deviantart": 30},
                           "by_format": {"auction": 30}}}
    assert sales.without_currency(conn, NOW - 7 * DAY, NOW) == 1
    assert "500 (валюта ?)" in sales.describe(sales.get(conn, sid)) and "$500" not in sales.describe(sales.get(conn, sid))


def test_manual_sale_platform_from_character_link(conn):
    """«Запиши продажу: Луна, $40, купила alice на Toyhouse» — без platform, со ссылкой на перса."""
    sid, _ = sales.record(conn, title="Луна", amount=40, currency="usd", platform="", format="other",
                          buyer="alice", character_url="https://toyhou.se/123.luna", now=NOW)
    assert sales.get(conn, sid)["platform"] == "toyhouse"
    sid, _ = sales.record(conn, title="Мотылёк", amount=10, currency="usd", platform="", format="other", now=NOW)
    assert sales.get(conn, sid)["platform"] == "deviantart"
    sid, _ = sales.record(conn, title="Лис", amount=10, currency="usd", platform="other", format="other",
                          character_url="https://toyhou.se/9.fox", now=NOW)
    assert sales.get(conn, sid)["platform"] == "other"  # её слово важнее догадки


def test_transferred_without_payment_is_still_unpaid(conn):
    on_trust = sales.add_sale(conn, title="Луна", amount=40, currency="usd", platform="toyhouse", format="auction",
                              buyer="alice", now=NOW - 5 * DAY)
    sales.set_status(conn, on_trust, "transferred", now=NOW - 4 * DAY)
    paid = sales.add_sale(conn, title="Сол", amount=30, currency="usd", platform="toyhouse", format="auction",
                          buyer="bob", now=NOW - 5 * DAY)
    sales.set_status(conn, paid, "paid", now=NOW - 4 * DAY)
    sales.set_status(conn, paid, "transferred", now=NOW - 3 * DAY)
    assert [r["id"] for r in sales.unpaid(conn, 3, NOW)] == [on_trust]
