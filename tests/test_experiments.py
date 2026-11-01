from datetime import UTC, datetime

import pytest

from mcp_devian import db, experiments, sales
from mcp_devian.db import Item
from mcp_devian.experiments import ExperimentError

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC).timestamp()
DAY = 86400
TUE = datetime(2026, 10, 6, 12, tzinfo=UTC).timestamp()   # вторник
SAT = datetime(2026, 10, 10, 12, tzinfo=UTC).timestamp()  # суббота


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "x.sqlite3")
    yield c
    c.close()


def lot(conn, n, sold, published=TUE, title="Moth adopt", place="AdoptGroup", top=20.0):
    iid = f"da:{n}"
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=title, url=f"https://www.deviantart.com/me/art/moth-{n}",
                              lot_type="auction", status="closed", price_usd=10, own=True, published_ts=published,
                              author="me", place=place), now=published)
    conn.execute("INSERT OR REPLACE INTO auctions (deviation_id, top_amount, currency, n_bids, ended, updated_ts) "
                 "VALUES (?, ?, 'usd', ?, 1, ?)", (iid, top if sold else None, 2 if sold else 0, published + 3 * DAY))
    conn.commit()
    return iid


def test_create_add_close(conn):
    e = experiments.create(conn, "старт 5 против 10", variable="sb", hypothesis="низкий старт собирает ставки", now=NOW)
    lot(conn, 1, True)
    assert experiments.add(conn, e, "a", "https://www.deviantart.com/me/art/moth-1", NOW) == "da:1"
    assert experiments.add(conn, f"e{e}", "A", "da:1", NOW) == "da:1"          # повтор в ту же ветку — без ошибки
    assert experiments.add(conn, e, "B", "th:77", NOW) == "th:77"
    assert experiments.add(conn, e, "B", "12345", NOW) == "da:12345"
    with pytest.raises(ExperimentError, match="уже в ветке A"):
        experiments.add(conn, e, "B", "da:1", NOW)
    with pytest.raises(ExperimentError, match="Ветки «C» нет"):
        experiments.add(conn, e, "C", "da:2", NOW)
    with pytest.raises(ExperimentError, match="Не поняла"):
        experiments.add(conn, e, "A", "https://example.com/x", NOW)
    experiments.close(conn, e, "хватит")
    with pytest.raises(ExperimentError, match="закрыт"):
        experiments.add(conn, e, "A", "da:9", NOW)
    assert experiments.get(conn, e)["status"] == "closed" and experiments.get(conn, e)["note"] == "хватит"
    with pytest.raises(ExperimentError):
        experiments.get(conn, 999)


def test_double_assignment_across_open_experiments_refused(conn):
    e1 = experiments.create(conn, "один", now=NOW)
    e2 = experiments.create(conn, "два", now=NOW)
    experiments.add(conn, e1, "A", "da:5", NOW)
    with pytest.raises(ExperimentError, match=f"открытом эксперименте e{e1}"):
        experiments.add(conn, e2, "A", "da:5", NOW)
    experiments.close(conn, e1)
    assert experiments.add(conn, e2, "A", "da:5", NOW) == "da:5"              # закрытый опыт лот отпускает


def test_resolve_ref():
    r = experiments.resolve_ref
    assert r(None, "https://www.deviantart.com/me/art/Moth-Adopt-OPEN-1234567") == "da:1234567"
    assert r(None, "https://toyhou.se/~forums/12.adoptables/345678.cat-auction") == "th:345678"
    assert r(None, "DA:42") == "da:42" and r(None, "th:9") == "th:9" and r(None, "nope") is None


def test_report_refuses_below_eight_per_arm(conn):
    e = experiments.create(conn, "старт", now=NOW)
    for i in range(5):
        experiments.add(conn, e, "A", lot(conn, i, True), NOW)
    for i in range(10, 13):
        experiments.add(conn, e, "B", lot(conn, i, False), NOW)
    text = experiments.report(conn, e, NOW)
    assert "вывода нет: нужно ещё 5 лотов в каждой ветке" in text
    assert "вероятно лучше" not in text
    assert "(n=5," in text and "её эксперимент" in text


def test_report_clear_effect(conn):
    e = experiments.create(conn, "старт 5 против 10", variable="sb", now=NOW)
    for i in range(10):
        experiments.add(conn, e, "A", lot(conn, i, i < 9, top=20 + i), NOW)
    for i in range(10):
        experiments.add(conn, e, "B", lot(conn, 100 + i, i < 2, top=15), NOW)
    text = experiments.report(conn, e, NOW)
    assert "Со ставкой или продан: 9 из 10 (90%" in text and "2 из 10 (20%" in text
    assert "A вероятно лучше B по доле со ставкой (P≈0.99)" in text
    assert "медиана ставки $24" in text and "до закрытия медиана" not in text  # закрытие лота не записано
    assert "по ставке вывода нет" in text                                         # у B лишь 2 со ставкой
    assert "Что ещё различается" in text and "дни недели похожи" in text


def test_report_unclear_and_confounders(conn):
    e = experiments.create(conn, "время", variable="time", now=NOW)
    for i in range(8):
        experiments.add(conn, e, "A", lot(conn, i, i < 5, published=TUE - i * 7 * DAY), NOW)
    for i in range(8):
        experiments.add(conn, e, "B", lot(conn, 50 + i, i < 4, published=SAT - i * 7 * DAY, place="OtherGroup",
                                         title="Cat adopt"), NOW)
    text = experiments.report(conn, e, NOW)
    assert "пока не различить" in text and "вероятно лучше" not in text
    assert "дни недели разные: A — чаще вт; B — чаще сб" in text
    assert "группы/места разные" in text and "виды разные" in text
    assert "не учтены: сезон" in text


def test_sales_count_as_sold_and_days_to_close(conn):
    experiments.create(conn, "флэт", variable="price", now=NOW)
    db.upsert_item(conn, Item(id="da:7", source="deviantart", title="Moth", url="u", lot_type="flatsale",
                              status="open", price_usd=30, own=True, published_ts=TUE), now=TUE)
    sales.add_sale(conn, title="Moth", amount=30, currency="usd", platform="deviantart", format="flatsale",
                   item_id="da:7", status="paid", now=TUE + 2 * DAY)
    o = experiments.lot_outcome(conn, "da:7", NOW)
    assert o["finished"] and o["sold"] and o["top"] == 30 and o["days"] == pytest.approx(2)
    assert experiments.lot_outcome(conn, "da:404", NOW)["found"] is False


def test_handle_routes(conn):
    out = experiments.handle(conn, text="старт 5 против 10; sb; низкий старт", now=NOW)
    assert out.startswith("Завела эксперимент e1") and "старт (SB)" in out
    lot(conn, 1, True)
    out = experiments.handle(conn, ref="e1", items=["A=https://www.deviantart.com/me/art/moth-1", "B=da:2", "oops"],
                             now=NOW)
    assert "Добавила в e1: A: da:1, B: da:2" in out and "нужно в виде A=<ссылка>" in out
    assert experiments.handle(conn, ref="e1", now=NOW).startswith("Эксперимент e1 «старт 5 против 10»")
    assert "e1 «старт 5 против 10»" in experiments.handle(conn, now=NOW)
    assert experiments.handle(conn, ref="e1", variant="close", now=NOW).startswith("Закрыла")
    assert experiments.handle(conn, now=NOW).startswith("Открытых экспериментов нет")
    assert "Номер эксперимента" in experiments.handle(conn, ref="xyz", now=NOW)


# --- Регрессии после проверки ---

def test_closed_auction_without_reliable_bids_is_not_finished(conn):
    pub = NOW - 5 * DAY
    db.upsert_item(conn, Item(id="da:900", source="deviantart", title="Moth adopt", url="u", lot_type="auction",
                              status="closed", price_usd=10, own=True, published_ts=pub, author="me"), now=pub)
    conn.execute("UPDATE items SET closed_ts = ? WHERE id = 'da:900'", (NOW - DAY,))
    conn.execute("INSERT INTO auctions (deviation_id, n_bids, ended, updated_ts) VALUES ('da:900', 0, 0, ?)",
                 (pub + DAY,))                             # ставки читали до закрытия
    conn.commit()
    o = experiments.lot_outcome(conn, "da:900", NOW)
    assert o["finished"] is False and o["sold"] is False
    db.upsert_item(conn, Item(id="da:901", source="deviantart", title="Moth adopt", url="u", lot_type="auction",
                              status="closed", price_usd=10, own=True, published_ts=pub, author="me"), now=pub)
    conn.commit()
    assert experiments.lot_outcome(conn, "da:901", NOW)["finished"] is False   # аукцион без чтения ставок


def test_closed_non_auction_counts_as_finished(conn):
    db.upsert_item(conn, Item(id="da:910", source="deviantart", title="Moth", url="u", lot_type="flatsale",
                              status="closed", price_usd=30, own=True, published_ts=TUE), now=TUE)
    conn.commit()
    o = experiments.lot_outcome(conn, "da:910", NOW)
    assert o["finished"] and not o["sold"]


def test_toyhouse_auction_outcome_from_th_auctions(conn):
    from mcp_devian import th_auctions

    th_auctions.ensure(conn)
    for tid in ("5", "6", "7"):
        db.upsert_item(conn, Item(id=f"th:{tid}", source="toyhouse", title="Moth auction", url="u",
                                  lot_type="auction", status="closed", own=True, published_ts=TUE), now=TUE)
    rows = [("5", 1, 3, 40.0, None), ("6", 1, 0, None, None), ("7", 1, 2, 30.0, "страницы 3–9")]
    for tid, ended, n_bids, top, incomplete in rows:
        conn.execute("INSERT INTO th_auctions (thread_id, lot, n_bids, top_amount, currency, ended, incomplete, "
                     "first_seen_ts, updated_ts) VALUES (?, '', ?, ?, 'usd', ?, ?, ?, ?)",
                     (tid, n_bids, top, ended, incomplete, TUE, TUE + 3 * DAY))
    conn.commit()
    a, b, c = (experiments.lot_outcome(conn, f"th:{t}", NOW) for t in ("5", "6", "7"))
    assert a["finished"] and a["sold"] and a["top"] == 40 and a["unit"] == "usd"
    assert b["finished"] and not b["sold"]
    assert not c["finished"]                                # тред дочитан не весь — итог неизвестен


def test_handle_items_as_single_string(conn):
    experiments.handle(conn, text="старт; sb", now=NOW)
    out = experiments.handle(conn, ref="e1", items="A=da:5", now=NOW)
    assert out == "Добавила в e1: A: da:5"


# --- регрессии ревью 0.5 ---

def flat(conn, n, sold, published):
    iid = f"da:{n}"
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=f"Moth adopt {n}",
                              url=f"https://www.deviantart.com/me/art/moth-{n}", lot_type="flatsale", status="open",
                              price_usd=10, own=True, published_ts=published, author="me", place="AdoptGroup"),
                   now=published)
    if sold:
        sales.add_sale(conn, title=f"Moth {n}", amount=10, currency="usd", platform="deviantart", format="flatsale",
                       item_id=iid, status="paid", now=published + DAY)
    conn.commit()
    return iid


def test_unsold_flat_sales_count_as_finished_after_two_weeks(conn):
    """Регрессия: непроданный flat sale/OTA вечно «открыт» и выпадал из счёта — обе ветки «9 из 9 (100%)»
    при настоящих 90% против 30%."""
    e = experiments.create(conn, "цена 10 против 20", variable="price", now=NOW - 60 * DAY)
    for i in range(10):
        experiments.add(conn, e, "A", flat(conn, 100 + i, i < 9, NOW - 40 * DAY), NOW)
    for i in range(30):
        experiments.add(conn, e, "B", flat(conn, 200 + i, i < 9, NOW - 40 * DAY), NOW)
    out = experiments.report(conn, e, NOW)
    assert "9 из 10" in out and "9 из 30" in out
    assert "A вероятно лучше B" in out


def test_verdict_refused_while_many_lots_unresolved(conn):
    """Проданные видны сразу, непроданные — только через 14 дней: пока у многих лотов итога нет, вывода нет."""
    e = experiments.create(conn, "цена", variable="price", now=NOW - 60 * DAY)
    for i in range(10):
        experiments.add(conn, e, "A", flat(conn, 100 + i, True, NOW - 40 * DAY), NOW)
    for i in range(10):
        experiments.add(conn, e, "B", flat(conn, 200 + i, i < 5, NOW - (40 if i < 5 else 2) * DAY), NOW)
    out = experiments.report(conn, e, NOW)
    assert "итог ещё неизвестен у B: 5 из 10" in out and "вероятно лучше" not in out
