"""Цены из первых постов тредов продаж Toyhouse: заявленные, отдельно от реальных продаж."""

import pytest

from mcp_devian import db, th_auctions, th_prices

NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.sqlite3")
    th_auctions.ensure(c)
    yield c
    c.close()


def thread(conn, tid: int, title: str, side: str = "sale", first_post: str | None = None, author: str = "seller",
           days_ago: float = 1.0) -> str:
    item = db.Item(id=f"th:{tid}", source="toyhouse", title=title, url=f"https://toyhou.se/~forums/13.x/{tid}.t",
                   author=author, activity_ts=NOW - days_ago * DAY, match_kind=side,
                   wants_characters=side == "lf", sells_characters=side == "sale")
    db.upsert_item(conn, item, NOW - days_ago * DAY)
    if first_post is not None:
        conn.execute("INSERT INTO th_posts (thread_id, post_id, page, pos, author, text, ts, seen_ts) "
                     "VALUES (?, ?, 1, 0, ?, ?, ?, ?)", (str(tid), f"{tid}01", author, first_post, NOW - DAY, NOW))
        conn.execute("INSERT INTO th_posts (thread_id, post_id, page, pos, author, text, ts, seen_ts) "
                     "VALUES (?, ?, 1, 1, 'buyer', 'can I get #2 for $5?', ?, ?)", (str(tid), f"{tid}02", NOW, NOW))
    conn.commit()
    return item.id


def stated(conn) -> dict:
    return {r["item_id"]: r for r in conn.execute("SELECT * FROM th_stated")}


def test_extracts_usd_eur_points_labelled_as_stated(conn):
    thread(conn, 1, "Dragon purge", first_post="1. Ember — $25\n2. Ash — $40\n3. Coal — SOLD")
    thread(conn, 2, "Selling cats", first_post="cats €15 each, €40 for the set")
    thread(conn, 3, "Points sale", first_post="all babies 1500 pts each")
    assert th_prices.extract(conn, NOW) == 3
    rows = stated(conn)
    assert (rows["th:1"]["amount"], rows["th:1"]["currency"]) == (32.5, "usd")   # медиана $25 и $40
    assert (rows["th:2"]["amount"], rows["th:2"]["currency"]) == (15, "eur")   # «за набор» не цена перса
    assert (rows["th:3"]["amount"], rows["th:3"]["currency"]) == (1500, "points")
    assert {r["label"] for r in rows.values()} == {"заявлено в первом посте"}
    # не продажа: в sales ничего не появилось
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0


def test_ignores_lf_threads_and_posts_without_price(conn):
    thread(conn, 4, "LF dragons", side="lf", first_post="budget up to $40")
    thread(conn, 5, "Selling dragons", first_post="offer anything! trades only")
    thread(conn, 6, "Old purge", first_post="$10 each", days_ago=60)
    assert th_prices.extract(conn, NOW, days=30) == 0
    assert stated(conn) == {}


def test_reply_posts_are_not_the_first_post(conn):
    thread(conn, 7, "Selling", first_post="prices in the image, offer!")
    th_prices.extract(conn, NOW)
    assert stated(conn) == {}   # «$5» из ответа покупателя — не цена продавца


def test_missing_lists_threads_without_first_post(conn):
    thread(conn, 8, "Purge A")
    thread(conn, 9, "Purge B", first_post="$20 each")
    thread(conn, 10, "LF", side="lf")
    got = th_prices.missing(conn, limit=10, now=NOW)
    assert [m["item_id"] for m in got] == ["th:8"]
    assert got[0]["url"].startswith("https://toyhou.se/")


def test_missing_without_posts_table(tmp_path):
    c = db.connect(tmp_path / "x.sqlite3")
    item = db.Item(id="th:11", source="toyhouse", title="Purge", url="u", activity_ts=NOW - DAY, match_kind="sale",
                   sells_characters=True)
    db.upsert_item(c, item, NOW - DAY)
    assert th_prices.extract(c, NOW) == 0
    assert [m["item_id"] for m in th_prices.missing(c, now=NOW)] == ["th:11"]
    c.close()


# --- регрессии: не цены персов не тянут «заявленную цену» вниз ---

@pytest.mark.parametrize("post, want", [
    ("SB: $10 / MI: $2 / AB: $80", 10),                 # аукцион: стартовая ставка, не шаг и не автовыкуп
    ("Dragon $25 / Paypal fees +$1", 25),               # комиссия — не цена
    ("Ember $30 (+$5 for extra art)", 30),              # доплата — не цена
    ("#1 $20 #2 $35 / tip jar $1", 27.5),               # чаевые — не цена; медиана $20 и $35
    ("Starting bid $15, min increment $1, autobuy $60", 15),
    ("cats €15 each, €40 for the set", 15),             # явная цена «each» при цене набора
])
def test_stated_price_ignores_addons(conn, post, want):
    thread(conn, 20, "Sale", first_post=post)
    assert th_prices.extract(conn, NOW) == 1
    assert stated(conn)["th:20"]["amount"] == want


def test_set_price_without_each_skips_thread(conn):
    thread(conn, 21, "Cats", first_post="cats €15, the set is €40")   # что за перса — не понять
    assert th_prices.extract(conn, NOW) == 0
    assert stated(conn) == {}


def test_reextract_removes_row_when_post_no_longer_has_price(conn):
    thread(conn, 22, "Sale", first_post="$20 each")
    th_prices.extract(conn, NOW)
    conn.execute("UPDATE th_posts SET text = 'SB $10 / MI $1 / AB $50 -- set only, $40 for the set' "
                 "WHERE thread_id = '22' AND pos = 0")
    conn.commit()
    th_prices.extract(conn, NOW)
    assert "th:22" not in stated(conn)
