import json
import re

import pytest

from mcp_devian import db, funnel, palette, sales
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400
NICHE = {"moth"}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "f.sqlite3")
    yield c
    c.close()


def lot(conn, n, *, own=True, published=NOW - 20 * DAY, views=None, favs=None, bidders=0, bids=None, usd=10,
        ended=1, status="closed", title="Moth adopt", place="MothGroup: adopts", snippet="", author=None,
        names=(), winner=None):
    iid = f"da:{n}"
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=title, url=f"https://www.deviantart.com/me/art/moth-{n}",
                              lot_type="auction", status=status, price_usd=usd, own=own, published_ts=published,
                              author=author or ("me" if own else f"seller{n}"), place=place, snippet=snippet),
                   now=published)
    if views is not None:
        conn.execute("INSERT INTO deviation_stats (deviation_id, ts, views, favourites, comments) VALUES (?, ?, ?, ?, 0)",
                     (iid, published + 5 * DAY, views, favs))
    n_bids = bids if bids is not None else bidders * 2
    extra = {"bidders": [{"user": u, "amount": 12, "n": 1} for u in names]} if names else {}
    conn.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, bidders, ended, winner, updated_ts, "
                 "extra) VALUES (?, ?, 'usd', ?, ?, ?, ?, ?, ?)",
                 (iid, 20 if n_bids else None, n_bids, bidders, ended, winner, published + 8 * DAY, json.dumps(extra)))
    conn.commit()
    return iid


def baseline(conn):
    """10 её лотов: просмотры 80–170, фавы ~10%, по 2 ставящих, все оплачены."""
    for i in range(10):
        iid = lot(conn, i, published=NOW - (20 + i) * DAY, views=80 + 10 * i, favs=8 + i, bidders=2)
        sales.add_sale(conn, title=f"M{i}", amount=20, currency="usd", platform="deviantart", format="auction",
                       item_id=iid, status="paid", now=NOW - (10 + i) * DAY)


def numeric_lines_tagged(lines):
    for line in lines:
        if re.search(r"\d", line):
            assert "(n=" in line or "ывода нет" in line or "общая практика" in line, line


def test_lot_funnel_numbers(conn):
    iid = lot(conn, 1, views=120, favs=14, bidders=2, bids=3)
    conn.execute("INSERT INTO deviation_stats (deviation_id, ts, views, favourites, comments) VALUES (?, ?, 150, 15, 4)",
                 (iid, NOW - DAY))
    sales.add_sale(conn, title="M", amount=40, currency="usd", platform="deviantart", format="auction", item_id=iid,
                   status="paid", now=NOW)
    f = funnel.lot_funnel(conn, iid, NOW)
    assert (f["views"], f["favs"], f["comments"], f["bids"], f["bidders"], f["sold"], f["amount"]) == \
        (150, 15, 4, 3, 2, True, 40)
    assert funnel.lot_funnel(conn, "da:404", NOW) is None


def test_weak_views(conn):
    baseline(conn)
    iid = lot(conn, 50, published=NOW - 8 * DAY, views=10, favs=1, bidders=0, ended=0, status="open")
    lines = funnel.diagnose(conn, iid, NOW, niche=NICHE)
    text = "\n".join(lines)
    assert "Просмотры: 10 — ниже, чем у 100% твоих лотов (медиана 125)" in text
    assert "Слабое место: мало просмотров" in text
    assert "Время:" in text and "Группы" in text
    numeric_lines_tagged(lines)


def test_weak_fav_rate_mentions_palette(conn):
    baseline(conn)
    iid = lot(conn, 51, published=NOW - 8 * DAY, views=130, favs=1, ended=0, status="open")
    palette.save(conn, iid, [((248, 187, 208), 1.0)], "u", NOW)
    lines = funnel.diagnose(conn, iid, NOW, niche=NICHE)
    text = "\n".join(lines)
    assert "Фавы от просмотров: 0.8% — ниже, чем у 100% твоих лотов" in text
    assert "Слабое место: смотрят, но мало фавят" in text
    assert "Палитра превью: пастельные розовые" in text and "Вид: moth." in text
    numeric_lines_tagged(lines)


def test_weak_bid_rate_mentions_listing_and_start(conn):
    baseline(conn)
    iid = lot(conn, 52, published=NOW - 12 * DAY, views=130, favs=13, bidders=0, usd=40, snippet="Moth adopt, SB $40")
    lines = funnel.diagnose(conn, iid, NOW, niche=NICHE)
    text = "\n".join(lines)
    assert "Ставящие на фав: 0.00 — ниже" in text and "Слабое место: фавят, но не ставят" in text
    assert "В посте не нашла:" in text and "автобай (AB)" in text and "как платить" in text
    assert "Старт $40 — выше медианы старта твоих лотов, получивших ставку ($10" in text
    numeric_lines_tagged(lines)


def test_payment_friction(conn):
    baseline(conn)
    iid = lot(conn, 53, published=NOW - 12 * DAY, views=125, favs=13, bidders=3)
    sales.add_sale(conn, title="M", amount=25, currency="usd", platform="deviantart", format="auction", item_id=iid,
                   status="won", now=NOW - 5 * DAY)
    lines = funnel.diagnose(conn, iid, NOW, niche=NICHE)
    text = "\n".join(lines)
    assert "Слабое место: ставки были, а оплаты нет — ждёт оплаты 5 дн" in text
    assert "Boosty" in text and "3-D Secure" in text and "DA Points: до 3500 за перевод" in text
    assert "Не оплачены или отменены у тебя 1 из 11 продаж" in text
    numeric_lines_tagged(lines)


def test_no_baseline_says_no_conclusion(conn):
    iid = lot(conn, 60, views=10, favs=1)
    lines = funnel.diagnose(conn, iid, NOW, niche=NICHE)
    assert any(line.startswith("Вывода нет: просмотры — твоих лотов для сравнения — 0 из нужных 8") for line in lines)
    assert "Слабое место" not in "\n".join(lines)
    numeric_lines_tagged(lines)


def test_render_one_and_list(conn):
    baseline(conn)
    text = funnel.render(conn, NOW, "https://www.deviantart.com/me/art/moth-3")
    assert text.startswith("Воронка лота «Moth adopt»") and "Воронка: просмотры 110 → фавы 11" in text
    listing = funnel.render(conn, NOW)
    assert listing.startswith("Твои последние лоты") and "(n=10," in listing
    assert listing.count("\n• ") == 10 and "оплачено $20" in listing
    assert funnel.render(conn, NOW, "nonsense").startswith("Не поняла")


def test_group_targets_rank_and_few(conn):
    def market(start, count, group, bidder_pool, sold=True):
        for j in range(count):
            names = [bidder_pool[(j + t) % len(bidder_pool)] for t in range(2)] if sold else []
            lot(conn, start + j, own=False, published=NOW - (5 + j) * DAY, place=f"{group}: adopts",
                bidders=len(names), names=names, winner=names[0] if names else None)

    market(100, 10, "BigMoths", [f"u{i}" for i in range(6)])
    market(200, 10, "SmallMoths", ["a", "b"])
    market(300, 3, "TinyMoths", [f"t{i}" for i in range(5)])
    lot(conn, 400, own=False, title="Dragon adopt", place="Dragons: x", bidders=2, names=["d1", "d2"])  # не ниша
    conn.execute("INSERT INTO sent_log (kind, target, ref, ts, ok, detail) VALUES ('da_group', 'bigmoths', 'd', ?, 1, "
                 "'https://www.deviantart.com/me/art/moth-9')", (NOW - DAY,))
    conn.commit()
    rows = funnel.group_targets(conn, NOW, niche=NICHE)
    assert [r["group"] for r in rows] == ["bigmoths", "smallmoths", "tinymoths"]
    big, small, tiny = rows
    assert big["bidders"] == 6 and big["n"] == 10 and small["bidders"] == 2 and tiny["few"] and not big["few"]
    assert big["her_sent"] == 1 and big["winners"] == 6
    text = funnel.render_group_targets(rows)
    assert "• bigmoths: ставящих 6" in text and "палитр" not in text
    assert "• tinymoths:" in text and text.rstrip().endswith("— мало данных")
    for line in text.splitlines()[1:]:
        assert "(n=" in line
    assert "вывода нет" in funnel.render_group_targets([])


# --- Регрессии после проверки ---

def test_running_auction_with_bids_is_not_payment_trouble(conn):
    baseline(conn)
    iid = lot(conn, 70, published=NOW - 4 * DAY, views=125, favs=13, bidders=3, ended=0, status="open")
    conn.execute("UPDATE auctions SET updated_ts = ? WHERE deviation_id = ?", (NOW - 3600, iid))
    conn.commit()
    text = "\n".join(funnel.diagnose(conn, iid, NOW, niche=NICHE))
    assert "оплаты нет" not in text and "Boosty" not in text and "не записана" not in text


def test_finished_auction_without_sale_and_transferred_unpaid(conn):
    baseline(conn)
    iid = lot(conn, 71, published=NOW - 12 * DAY, views=125, favs=13, bidders=3)
    text = "\n".join(funnel.diagnose(conn, iid, NOW, niche=NICHE))
    assert "ставки были, а оплаты нет — продажа не записана" in text
    sales.add_sale(conn, title="M", amount=25, currency="usd", platform="deviantart", format="auction", item_id=iid,
                   status="transferred", now=NOW - DAY)
    conn.execute("UPDATE sales SET paid_ts = NULL WHERE item_id = ?", (iid,))   # передан до оплаты
    conn.commit()
    text = "\n".join(funnel.diagnose(conn, iid, NOW, niche=NICHE))
    assert "перс передан, а оплата не отмечена" in text and "Boosty" in text


def test_young_lot_gets_no_verdict(conn):
    baseline(conn)
    iid = lot(conn, 72, published=NOW - DAY, ended=0, status="open")
    conn.execute("INSERT INTO deviation_stats (deviation_id, ts, views, favourites, comments) VALUES (?, ?, 40, 5, 0)",
                 (iid, NOW - DAY / 2))
    conn.commit()
    lines = funnel.diagnose(conn, iid, NOW, niche=NICHE)
    text = "\n".join(lines)
    assert "Вывода нет: лот моложе трёх дней" in text
    assert "ниже, чем у" not in text and "Слабое место" not in text
    numeric_lines_tagged(lines)


def test_old_lot_without_day_3_10_snapshot_gets_no_verdict(conn):
    baseline(conn)
    iid = lot(conn, 73, published=NOW - 60 * DAY, bidders=2)
    conn.execute("INSERT INTO deviation_stats (deviation_id, ts, views, favourites, comments) VALUES (?, ?, 900, 90, 0)",
                 (iid, NOW - 10 * DAY))
    conn.commit()
    lines = funnel.diagnose(conn, iid, NOW, niche=NICHE)
    text = "\n".join(lines)
    assert "Вывода нет: снимка просмотров на 3–10 день" in text and "выше, чем у" not in text
    numeric_lines_tagged(lines)


def test_baseline_ignores_lots_without_day_3_10_snapshot(conn):
    baseline(conn)
    for i in range(10):                                    # снимки только на 50-й день — огромные просмотры
        iid = lot(conn, 80 + i, published=NOW - 60 * DAY)
        conn.execute("INSERT INTO deviation_stats (deviation_id, ts, views, favourites, comments) "
                     "VALUES (?, ?, 5000, 500, 0)", (iid, NOW - 10 * DAY))
    conn.commit()
    vals, _ts = funnel._baseline(conn, "da:none", NOW)
    assert len(vals["views"]) == 10 and max(vals["views"]) == 170


def test_closed_without_reliable_bid_read_is_not_finished(conn):
    iid = lot(conn, 90, published=NOW - 5 * DAY, views=100, favs=10, bidders=0, ended=0, status="closed")
    conn.execute("UPDATE items SET closed_ts = ? WHERE id = ?", (NOW - DAY, iid))
    conn.execute("UPDATE auctions SET updated_ts = ? WHERE deviation_id = ?", (NOW - 4 * DAY, iid))
    conn.commit()
    row = db.get_item(conn, iid)
    assert funnel._finished(conn, row, NOW) is False
    f = funnel.lot_funnel(conn, iid, NOW)
    snap, state = funnel._comparable(conn, iid, f["published_ts"])
    assert state == "ok" and "bid_rate" not in funnel._metrics(f, snap, funnel._finished(conn, row, NOW))


# --- регрессии ревью 0.5 ---

def test_group_targets_exclude_her_lots_and_cap_sellers(conn):
    """Регрессия: её лоты в группе считались рынком группы, а один плодовитый продавец раздувал n."""
    for j in range(10):
        lot(conn, 500 + j, own=False, published=NOW - (5 + j) * DAY, place="Moths: adopts", bidders=2,
            names=[f"u{j}", f"v{j}"], author="prolific")
    for j in range(6):
        lot(conn, 600 + j, own=True, published=NOW - (5 + j) * DAY, place="Moths: adopts", bidders=2,
            names=["x", "y"])
    rows = {r["group"]: r for r in funnel.group_targets(conn, NOW, niche=NICHE)}
    assert rows["moths"]["n"] == 3            # SELLER_CAP лотов одного продавца за окно, без её 6
    assert rows["moths"]["her_n"] == 6


def test_transferred_without_payment_counts_as_unpaid(conn):
    baseline(conn)
    sid = sales.add_sale(conn, title="T", amount=25, currency="usd", platform="deviantart", format="auction",
                         item_id="da:77", status="won", now=NOW - 5 * DAY)
    conn.execute("UPDATE sales SET status = 'transferred' WHERE id = ?", (sid,))  # передала по доверию
    conn.commit()
    assert "Не оплачены или отменены у тебя 1 из 11" in funnel._her_unpaid_line(conn, NOW)


def test_boosty_rules_cite_their_own_sources():
    """Регрессия: «PayPal там больше нет; много попыток — банк заблокирует» приписывалось FAQ Boosty,
    а FAQ этого не говорит."""
    rules = funnel._payment_rules()
    faq = next(r for r in rules if "FAQ Boosty" in r)
    assert "PayPal" not in faq and "заблок" not in faq
    paypal = next(r for r in rules if "PayPal" in r)
    assert "сторонний" in paypal and "FAQ Boosty" not in paypal
