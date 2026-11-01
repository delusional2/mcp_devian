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


# --- 0.5: рубли, сколько дошло, Boosty, поинты ---

def test_rub_and_net_columns_saved(conn):
    sid = sales.add_sale(conn, title="Moth", amount=1500, currency="rub", platform="other", format="flatsale",
                         status="paid", now=NOW)
    assert sales.money(1500, "rub") == "1,500 ₽" and sales.get(conn, sid)["currency"] == "rub"
    sid, outcome = sales.record(conn, title="Fox", amount=40, currency="usd", platform="deviantart",
                                format="auction", buyer="bob", net_amount=3100, pay_method="boosty", now=NOW)
    row = sales.get(conn, sid)
    assert outcome == "new" and (row["net_amount"], row["net_currency"], row["pay_method"]) == (3100, "rub", "boosty")
    assert "дошло 3,100 ₽" in sales.describe(row) and "boosty" in sales.describe(row)
    sales.set_net(conn, sid, 3050)
    assert sales.get(conn, sid)["net_amount"] == 3050
    with pytest.raises(sales.SaleError):
        sales.add_sale(conn, title="X", amount=1, currency="usd", platform="other", format="other", net_amount=-1)
    with pytest.raises(sales.SaleError):
        sales.add_sale(conn, title="X", amount=1, currency="usd", platform="other", format="other",
                       pay_method="paypal")


def test_points_never_converted(conn):
    sid = sales.add_sale(conn, title="P", amount=800, currency="points", platform="deviantart", format="auction",
                         status="paid", now=NOW - DAY)
    assert sales.get(conn, sid)["pay_method"] == "points"
    with pytest.raises(sales.SaleError):
        sales.add_sale(conn, title="P2", amount=800, currency="points", platform="deviantart", format="auction",
                       net_amount=500)
    sales.add_sale(conn, title="U", amount=20, currency="usd", platform="deviantart", format="auction",
                   status="paid", net_amount=1500, now=NOW - DAY)
    text = sales.render_income(conn, 30, now=NOW)
    pts_line = next(line for line in text.splitlines() if "pts" in line)
    assert pts_line.startswith("- поинты: 800 pts") and "не выводятся в деньги" in pts_line
    assert "$" not in pts_line and "₽" not in pts_line
    assert "Дошло до тебя (как ты вписала): 1,500 ₽" in text
    assert "курс" in text  # курсом не пересчитываем


def test_fee_note_with_source_when_net_missing(conn):
    sales.add_sale(conn, title="B", amount=30, currency="usd", platform="deviantart", format="auction",
                   status="paid", pay_method="boosty", now=NOW - DAY)
    text = sales.render_income(conn, 30, now=NOW)
    assert sales.BOOSTY_FEE_NOTE in text and "FAQ Boosty, обновлено 12.2025" in text
    got = sales.received(conn, NOW - 30 * DAY, NOW)
    assert got["boosty_missing"] == {"usd": 1} and got["net"] == {}
    # Курса нет нигде: нет и «≈ ₽» рядом с долларами.
    assert "≈" not in text


def test_points_warnings(conn):
    out = sales.points_warnings(conn, "bob", 3500, now=NOW)
    assert out == [sales.POINTS_NOT_CASH]
    out = sales.points_warnings(conn, "bob", 3501, now=NOW)
    assert "не больше 3,500" in out[0] and "2 перевода" in out[0] and out[-1] == sales.POINTS_NOT_CASH
    for i in range(5):
        sales.add_sale(conn, title=f"P{i}", amount=3500, currency="points", platform="deviantart",
                       format="auction", buyer="@Bob", status="paid", now=NOW - (i + 1) * 30 * DAY)
    out = sales.points_warnings(conn, "bob", 3000, now=NOW)
    assert any("20,000" in w and "17,500" in w and "20,500" in w for w in out)
    # Старше года — не считается.
    assert len(sales.points_warnings(conn, "bob", 3000, now=NOW + 400 * DAY)) == 1


def test_sales_migration_idempotent_on_old_db(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE sales (id TEXT PRIMARY KEY, item_id TEXT, platform TEXT NOT NULL, title TEXT NOT NULL, "
                "character_url TEXT, buyer TEXT, amount REAL, currency TEXT, format TEXT, status TEXT NOT NULL, "
                "created_ts REAL NOT NULL, paid_ts REAL, done_ts REAL, source TEXT, note TEXT)")
    old.execute("INSERT INTO sales (id, platform, title, amount, currency, status, created_ts) "
                "VALUES ('a1', 'deviantart', 'Old', 10, 'usd', 'paid', 1)")
    old.commit()
    old.close()
    for _ in range(2):
        c = db.connect(path)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sales)")}
        assert {"net_amount", "net_currency", "pay_method"} <= cols
        assert sales.get(c, "a1")["title"] == "Old" and sales.get(c, "a1")["net_amount"] is None
        c.close()


def test_points_warnings_after_record_does_not_double_count(conn):
    """sales:record сначала пишет продажу, потом предупреждает: её сумма не должна считаться дважды."""
    sid = sales.add_sale(conn, title="Big", amount=12000, currency="points", platform="deviantart",
                         format="auction", buyer="bob", now=NOW)
    assert sales.points_warnings(conn, "bob", 12000, now=NOW, exclude_sale_id=sid)[-1] == sales.POINTS_NOT_CASH
    assert not any("20,000" in w for w in sales.points_warnings(conn, "bob", 12000, now=NOW, exclude_sale_id=sid))
    sid2 = sales.add_sale(conn, title="Big 2", amount=9000, currency="points", platform="deviantart",
                          format="auction", buyer="bob", now=NOW)
    out = sales.points_warnings(conn, "bob", 9000, now=NOW, exclude_sale_id=sid2)
    assert any("уже 12,000" in w and "будет 21,000" in w for w in out)


def test_points_split_count_rounds_up_fractions(conn):
    out = sales.points_warnings(conn, None, 3500.5, now=NOW)
    assert "2 перевода" in out[0]
    assert "3 перевода" in sales.points_warnings(conn, None, 7001, now=NOW)[0]


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_amounts_refused(conn, bad):
    with pytest.raises(sales.SaleError):
        sales.add_sale(conn, title="X", amount=40, currency="usd", platform="deviantart", format="auction",
                       net_amount=bad)
    with pytest.raises(sales.SaleError):
        sales.add_sale(conn, title="X", amount=bad, currency="usd", platform="deviantart", format="auction")
    sid = sales.add_sale(conn, title="X", amount=40, currency="usd", platform="deviantart", format="auction",
                         status="paid", now=NOW - DAY)
    with pytest.raises(sales.SaleError):
        sales.set_net(conn, sid, bad)
    assert "Оплачено" in sales.render_income(conn, 30, now=NOW)
