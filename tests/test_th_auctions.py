import json
import shutil
import subprocess

import pytest

from mcp_devian import db, sales
from mcp_devian import th_auctions as ta

T0 = 1_800_000_000.0
OWNER = "MothArt"
URL = "https://toyhou.se/~forums/13.adoption-center/865492.moth-dragon-auction"


def posts(*items, step=600):
    """(автор, текст) — по 10 минут между постами, номера постов по порядку."""
    return [{"post_id": str(5000 + i), "author": a, "text": t, "ts": T0 + i * step, "page": 1}
            for i, (a, t) in enumerate(items)]


NOW = T0 + 86400


# ---------- условия ----------

@pytest.mark.parametrize("text, sb, mi, ab, cur", [
    ("SB: $10 | MI: $2 | AB: $60", 10, 2, 60, "usd"),
    ("Starting bid 1.5k pts, min inc 100, no AB", 1500, 100, None, "points"),
    ("Bidding starts at 2k points. AB: N/A", 2000, None, None, "points"),
    ("SB 10 USD AB: TBA", 10, None, None, "usd"),
    ("SB: 15, AB: 80 (USD only)", 15, None, 80, "usd"),
    ("SB €5 / MI €1 / AB €40", 5, 1, 40, "eur"),
    ("стартовая ставка 500 поинтов, шаг 100, автобай 3000 поинтов", 500, 100, 3000, "points"),
    ("SB: £8 min bid increase: £1", 8, 1, None, "gbp"),
    ("SB 20 CAD, AB 100 CAD", None, None, None, None),
])
def test_parse_terms(text, sb, mi, ab, cur):
    t = ta.parse_terms(text)
    assert (t.sb, t.mi, t.ab, t.currency) == (sb, mi, ab, cur)


def test_terms_without_currency_are_flagged():
    t = ta.parse_terms("SB 10, AB 50")
    assert t.sb == 10 and t.currency is None
    assert any("нет валюты" in n for n in t.notes)


def test_terms_ab_in_other_currency_not_compared():
    t = ta.parse_terms("SB: $10, AB: 5000 pts")
    assert t.ab is None and any("другой валюте" in n for n in t.notes)


@pytest.mark.parametrize("text, seconds", [
    ("Ends 24h after last bid", 86400),
    ("Auction ends 48 hours after the final bid!", 172800),
    ("Закрытие через 2 дня после последней ставки", 172800),
    ("Ends on Friday", None),
])
def test_ends_after_rule(text, seconds):
    assert ta.parse_terms(text).ends_after_s == seconds


def test_usd_only_is_currency_not_cash_only():
    t = ta.parse_terms("SB $10. USD only. Art offers accepted")
    assert t.currency == "usd" and t.accepts == ["art"] and not t.cash_only
    assert ta.parse_terms("SB $10, no art/trades").cash_only
    assert ta.parse_terms("SB $10, money only please").cash_only


# ---------- разбор ставок ----------

def test_full_thread_flow():
    a = ta.analyse_thread(posts(
        (OWNER, "Moth dragon auction! SB: $10 MI: $2 AB: $60. USD only. Ends 24h after last bid."),
        ("anna", "SB"),
        ("bob", "$15 (@kitty123 told me about this!)"),
        ("cat", "can I offer art? https://toyhou.se/123.cat"),
        ("anna", "+5"),
        ("dan", "$25 CAD"),
        (OWNER, "Current top is $20, thanks everyone!"),
        ("bob", "nvm, retract my bid"),
        (OWNER, "Sold to @anna! Will send the character after payment"),
    ), now=NOW, thread_id="865492")
    assert [(b.user, b.amount, b.kind) for b in a.state.bids] == [("anna", 10, "sb"), ("anna", 20, "raise")]
    assert a.state.ended and a.state.winner == "anna" and a.currency == "usd"
    assert any("bob отозвал" in n for n in a.state.notes)
    assert any("другой валюте" in n and "dan" in n for n in a.state.notes)
    assert [o.user for o in a.offers] == ["cat"] and set(a.offers[0].kinds) >= {"art", "trade"}
    assert a.due_ts is None  # закрыт — правило «24h» уже не важно


def test_mentions_with_digits_are_not_amounts():
    a = ta.analyse_thread(posts((OWNER, "SB $5"), ("anna", "@user2020 good luck!"), ("bob", "6$")), now=NOW)
    assert [(b.user, b.amount) for b in a.state.bids] == [("bob", 6)]


def test_owner_posts_are_never_bids():
    a = ta.analyse_thread(posts((OWNER, "SB $10 AB $50"), (OWNER, "$30"), (OWNER, "bump! $25 is the minimum now")),
                          now=NOW)
    assert a.state.n_bids == 0 and not a.state.ended


def test_owner_defaults_to_first_poster_and_can_be_given():
    p = posts(("seller", "SB 100 pts"), ("anna", "150 pts"))
    assert ta.analyse_thread(p, now=NOW).owner == "seller"
    # Если первый пост не её (она открыла аукцион постом внутри чужого треда) — условия передаются явно.
    a = ta.analyse_thread(p, owner="MothArt", terms=ta.parse_terms("SB 120 pts"), now=NOW)
    assert a.state.top.amount == 150 and a.state.n_bids == 1 and any("seller" in n for n in a.state.notes)


def test_ab_claim_ends_auction_and_later_bids_are_ignored():
    a = ta.analyse_thread(posts((OWNER, "SB: $10 AB: $80"), ("anna", "$12"), ("bob", "AB!"), ("cat", "$90")), now=NOW)
    assert a.state.ab_hit and a.state.ended and a.state.winner == "bob" and a.state.top.amount == 80
    assert any("после закрытия" in n for n in a.state.notes)


def test_points_with_k_suffix_and_russian_bids():
    a = ta.analyse_thread(posts(
        (OWNER, "Аукцион! Стартовая 1k pts, шаг 100"),
        ("аня", "ставлю 1.2k pts"),
        ("боб", "+300"),
        ("аня", "отменяю ставку"),
    ), now=NOW)
    assert a.currency == "points"
    assert [(b.user, b.amount) for b in a.state.bids] == [("боб", 1500)]


def test_bid_in_other_currency_not_mixed():
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("anna", "500 pts"), ("bob", "$11")), now=NOW)
    assert [b.amount for b in a.state.bids] == [11]
    assert any("другой валюте" in n for n in a.state.notes)


def test_congrats_without_at_names_bidder():
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("anna", "$10"), ("bob", "$14"), (OWNER, "Closed! Congrats bob")),
                          now=NOW)
    assert a.state.ended and a.state.winner == "bob"


def test_congrats_to_non_bidder_is_not_winner():
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("anna", "$10"), (OWNER, "Closed! congrats everyone")), now=NOW)
    assert a.state.ended and a.state.winner is None


def test_future_close_is_not_close():
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("anna", "$10"), (OWNER, "Will be closed tomorrow!")), now=NOW)
    assert not a.state.ended


def test_due_time_from_her_rule():
    a = ta.analyse_thread(posts((OWNER, "SB $10. Ends 24h after last bid"), ("anna", "$10")), now=T0 + 3600)
    assert a.due_ts == T0 + 600 + 86400
    assert "закроется через" in ta.summary(a, now=T0 + 3600)
    assert "уже должен был закрыться" in ta.summary(a, now=T0 + 3 * 86400)


def test_no_lookahead_posts_after_now_ignored():
    p = posts((OWNER, "SB $10"), ("anna", "$10"), ("bob", "$20"))
    a = ta.analyse_thread(p, now=T0 + 700)  # bob написал позже «сейчас»
    assert [b.user for b in a.state.bids] == ["anna"] and a.posts_seen == 2


def test_cash_plus_art_is_bid_and_offer():
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("anna", "$15 + art")), now=NOW)
    assert a.state.top.amount == 15
    assert a.offers and a.offers[0].with_cash and a.offers[0].kinds == ["art"]


def test_credits_and_trade_offers_recorded():
    a = ta.analyse_thread(posts((OWNER, "SB $10, no art/trades"), ("anna", "would you take TH premium?"),
                                ("bob", "trade offer: https://toyhou.se/555.wolf")), now=NOW)
    kinds = {o.user: o.kinds for o in a.offers}
    assert "credits" in kinds["anna"] and "trade" in kinds["bob"]
    assert any("только деньги" in n for n in a.info)
    assert a.state.n_bids == 0


# ---------- несколько лотов ----------

LOTS_FIRST = "Two auctions!\n#1 Wolf — SB $5 AB $30\n#2 Cat — SB $8 AB $40\nUSD only. Ends 24h after last bid."


def test_split_lots():
    lots = ta.split_lots(LOTS_FIRST)
    assert set(lots) == {"1", "2"}
    assert ta.split_lots("SB $5 AB $30") == {}


def test_multi_lot_per_lot_parse():
    p = posts((OWNER, LOTS_FIRST), ("anna", "#1 $6"), ("bob", "#2 $10"), ("cat", "$12"), ("dan", "lot 2: +2"),
              (OWNER, "#1 sold to @anna, #2 still open"))
    one = ta.analyse_thread(p, lot="1", now=NOW)
    two = ta.analyse_thread(p, lot="2", now=NOW)
    assert (one.terms.sb, one.terms.ab, one.terms.currency) == (5, 30, "usd")
    assert one.state.top.amount == 6 and one.state.ended and one.state.winner == "anna"
    assert (two.terms.sb, two.terms.ab) == (8, 40) and two.terms.ends_after_s == 86400
    assert two.state.top.amount == 12 and two.state.top.user == "dan" and not two.state.ended
    assert any("номер лота" in n for n in two.state.notes)


def test_multi_lot_without_lot_hints_in_summary():
    a = ta.analyse_thread(posts((OWNER, LOTS_FIRST), ("anna", "#1 $6")), now=NOW)
    assert a.lots_found == ["1", "2"]
    assert "смотри по номеру лота" in ta.summary(a, now=NOW)


# ---------- сводка ----------

def test_summary_shape_matches_da_and_mentions_posts():
    a = ta.analyse_thread(posts((OWNER, "SB $10 AB $50"), ("anna", "$12"), ("cat", "art offer?")), now=NOW)
    s = ta.summary(a, now=NOW)
    assert s.startswith("Ставок: 1 от 1 человека, сейчас $12 у anna (SB $10)")
    assert "автобай $50 не взят" in s and "Не деньгами: 1" in s
    assert s.endswith("Это разбор постов треда — проверь глазами.") and "комментар" not in s


# ---------- посты из скрипта ----------

def test_to_posts_order_dedupe_and_monotonic_time():
    raw = [
        {"post_id": "post-12", "author": "b", "text": "x", "when": "2 hours ago", "page": 2},
        {"post_id": "10", "author": "a", "text": "y", "when": "3 hours ago", "page": 1},
        {"post_id": "11", "author": "c", "text": "z", "when": "1 hour ago", "page": 1},
        {"post_id": "11", "author": "c", "text": "dup", "when": "1 hour ago", "page": 1},
        {"author": "nobody"},
        "garbage",
    ]
    out = ta.to_posts(raw, now=T0)
    assert [p.post_id for p in out] == ["10", "11", "12"]
    assert [p.ts for p in out] == [T0 - 3 * 3600, T0 - 3600, T0 - 3600]  # время не идёт назад


def test_iso_and_title_times():
    out = ta.to_posts([{"post_id": "1", "author": "a", "text": "", "when_title": "2026-11-01T10:00:00+00:00"}], now=T0)
    assert out[0].ts == 1793527200.0


def test_thread_from_extracted_states():
    one = json.dumps({"state": "ready", "page": 1, "pages_total": 3,
                      "posts": [{"post_id": "1", "author": "a", "text": "SB $5", "when": "1 day ago"}]})
    r = ta.thread_from_extracted(one, now=T0)
    assert r.incomplete and r.pages_total == 3 and len(r.posts) == 1 and "1 из 3" in r.reason
    assert ta.thread_from_extracted('{"state": "wrong_page"}').reason == "вкладка открылась не на этом треде"
    assert ta.thread_from_extracted("not json").state == "error"
    assert ta.thread_from_extracted([1, 2]).posts == []
    # run_in_chrome вернул None (на странице нет .forum-post) — русская причина, не «no_posts».
    r = ta.thread_from_extracted(None)
    assert r.state == "no_posts" and r.reason.startswith("на странице нет постов")


# ---------- скрипт для её Chrome (только строка, не выполняется в Chrome) ----------

def test_read_page_js_guards_and_shape():
    js = ta.read_page_js(URL + "?page=4#post-9")
    assert '"865492"' in js
    assert "location.origin !== 'https://toyhou.se'" in js and "wrong_page" in js and "challenge" in js
    assert "blockquote" in js and "pages_total" in js
    # Ни фоновых запросов, ни действий на странице: каждая страница — обычный заход с паузой.
    for bad in ("XMLHttpRequest", "fetch(", ".submit", "click()", "DOMParser"):
        assert bad not in js
    with pytest.raises(ValueError):
        ta.read_page_js("https://evil.example/~forums/13.x/865492.y")
    with pytest.raises(ValueError):
        ta.read_page_js("https://toyhou.se/12345.character")


@pytest.mark.skipif(not shutil.which("node"), reason="нет node для проверки синтаксиса")
def test_read_page_js_is_valid_javascript():
    js = ta.read_page_js(URL)
    done = subprocess.run(["node", "-e", "new Function('return ' + process.argv[1])", js],
                          check=False, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr


def test_page1_url_and_ids():
    assert ta.page1_url(URL + "?page=3#post-1") == URL
    assert ta.thread_id_of(URL) == "865492" and ta.thread_id_of("https://toyhou.se/1.x") is None
    assert ta.item_id("865492") == "th:865492" and ta.item_id("865492", "2") == "th:865492#2"


# ---------- таблица, события, продажи ----------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


def _auction(*items, now=NOW, **kw):
    return ta.analyse_thread(posts(*items), now=now, thread_id="865492", **kw)


def test_first_read_old_bids_are_not_news(conn):
    a = _auction((OWNER, "SB $10"), ("anna", "$12"))
    assert ta.save(conn, a, URL, "Moth dragon", now=NOW) is None  # ставка суточной давности


def test_new_bid_events_once(conn):
    now = T0 + 1300
    a = _auction((OWNER, "SB $10"), ("anna", "$12"), now=now)
    e = ta.save(conn, a, URL, "Moth dragon", now=now)
    assert e and e.amount == 12 and e.user == "anna"
    assert ta.describe_event(e) == "Toyhouse: новая ставка $12 от anna — «Moth dragon»"
    assert ta.save(conn, a, URL, "Moth dragon", now=now + 60) is None
    b = _auction((OWNER, "SB $10"), ("anna", "$12"), ("bob", "$15"), now=now + 1200)
    e2 = ta.save(conn, b, URL, "", now=now + 1200)
    assert e2 and e2.amount == 15 and e2.title == "Moth dragon"


def test_sync_sales_toyhouse(conn):
    a = _auction((OWNER, "SB $10"), ("anna", "$12"), ("bob", "$20"), (OWNER, "Sold to @bob!"))
    ta.save(conn, a, URL, "Moth dragon", own=True, now=NOW)
    created = ta.sync_sales(conn, now=NOW)
    assert len(created) == 1
    row = sales.get(conn, created[0])
    assert (row["platform"], row["item_id"], row["buyer"], row["amount"], row["currency"], row["format"]) == (
        "toyhouse", "th:865492", "bob", 20, "usd", "auction")
    assert ta.sync_sales(conn, now=NOW) == []  # один раз на лот


def test_sale_candidates_winner_not_top(conn):
    a = _auction((OWNER, "SB $10"), ("anna", "$12"), ("bob", "$20"), (OWNER, "Sold to @anna"))
    ta.save(conn, a, URL, "Moth dragon", own=True, now=NOW)
    [c] = ta.sale_candidates(conn)
    assert c["buyer"] == "anna" and c["amount"] is None and c["note"] == ta.CHECK_AMOUNT


def test_sale_candidates_noncash_winner_and_lot_ids(conn):
    a = _auction((OWNER, LOTS_FIRST), ("anna", "#1 can I offer art?"), (OWNER, "#1 sold to @anna"), lot="1")
    ta.save(conn, a, URL, "Two auctions", own=True, now=NOW)
    [c] = ta.sale_candidates(conn)
    assert c["item_id"] == "th:865492#1" and c["amount"] is None and c["note"] == ta.NONCASH_WIN
    assert c["title"].endswith("лот #1")


def test_market_threads_do_not_create_sales(conn):
    a = _auction((OWNER, "SB $10"), ("anna", "$12"), (OWNER, "Sold to @anna"))
    ta.save(conn, a, URL, "someone else's", own=False, now=NOW)
    assert ta.sale_candidates(conn) == []


def test_to_refresh_from_items_and_watch(conn):
    db.upsert_item(conn, db.Item(id="th:865492", source="toyhouse", title="Moth dragon AUCTION [open]", url=URL,
                                 author="MothArt", activity_ts=NOW - 3600), now=NOW - 3600)
    db.upsert_item(conn, db.Item(id="th:1", source="toyhouse", title="LF dragons", url=URL.replace("865492", "1"),
                                 author="MothArt", activity_ts=NOW - 3600), now=NOW - 3600)
    other = "https://toyhou.se/~forums/71.art-marketplace/777.x"
    ta.watch(conn, other, "Friend's auction", own=False, now=NOW)
    got = {r["thread_id"]: r for r in ta.to_refresh(conn, "mothart", now=NOW)}
    assert set(got) == {"865492", "777"} and got["777"]["own"] == 0
    with pytest.raises(ValueError):
        ta.watch(conn, "https://toyhou.se/123.character")


def test_to_refresh_her_thread_first_even_with_many_watched(conn):
    """5 чужих тредов под наблюдением не вытесняют её новый аукцион из очереди на 5 мест."""
    for i in range(900, 905):
        ta.watch(conn, f"https://toyhou.se/~forums/71.art-marketplace/{i}.x", f"Other {i}", own=False, now=NOW)
    db.upsert_item(conn, db.Item(id="th:777", source="toyhouse", title="Lumi auction", url=URL.replace("865492", "777"),
                                 author="MothArt", activity_ts=NOW - 3600), now=NOW - 3600)
    got = [r["thread_id"] for r in ta.to_refresh(conn, "mothart", now=NOW, limit=5)]
    assert got[0] == "777" and len(got) == 5


def test_summary_row_and_my_rows(conn):
    ta.watch(conn, URL, "Moth dragon", now=NOW)
    assert ta.summary_row(ta.my_rows(conn)[0]) == "ставки ещё не читались"
    a = _auction((OWNER, "SB $10"), ("anna", "$12"), ("cat", "art?"), (OWNER, "Sold to @anna"))
    ta.save(conn, a, URL, now=NOW)
    [row] = ta.my_rows(conn)
    assert row["title"] == "Moth dragon"
    assert ta.summary_row(row) == "ставок 1 от 1 чел., максимум $12 у anna, не деньгами 1, закрыт, победитель anna"


# ---------- регрессии по проверке ----------

def _bids(a):
    return [(b.user, b.amount) for b in a.state.bids]


def test_post_without_time_keeps_page_order():
    p = posts((OWNER, "SB $10 AB $50"), ("bob", "$15"), (OWNER, "Closed!"), ("ann", "$20"))
    p[2]["ts"] = ""  # время её «Closed!» не разобралось
    a = ta.analyse_thread(p, now=NOW)
    assert _bids(a) == [("bob", 15)] and a.state.ended
    assert any("после закрытия" in n and "ann" in n for n in a.state.notes)
    assert any("нет времени" in n for n in a.info)
    p = posts((OWNER, "SB $10"), ("bob", "$15"), ("ann", "$20"), ("cy", "$18"))
    p[2]["ts"] = ""
    a = ta.analyse_thread(p, now=NOW)
    assert _bids(a) == [("bob", 15), ("ann", 20)]


def test_future_time_in_middle_does_not_drop_later_posts():
    p = posts((OWNER, "SB $10"), ("bob", "$15"), ("ann", "$20"))
    p[1]["ts"] = NOW + 3 * 3600  # пояс/часы соврали
    p[2]["ts"] = NOW - 60
    a = ta.analyse_thread(p, now=NOW)
    assert _bids(a) == [("bob", 15), ("ann", 20)] and a.posts_seen == 3
    assert any("позже следующих" in n for n in a.info)


def test_trailing_future_posts_dropped_with_note():
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("anna", "$10"), ("bob", "$20")), now=T0 + 700)
    assert _bids(a) == [("anna", 10)]
    assert any("позже момента разбора: 1" in n for n in a.info)


def test_relative_time_preferred_over_naive_title():
    raw = [{"post_id": "1", "author": "a", "text": "", "when": "2 hours ago", "when_title": "1 Nov 2030, 10:00:00 am"}]
    assert ta.to_posts(raw, now=T0)[0].ts == T0 - 7200
    raw = [{"post_id": "1", "author": "a", "text": "", "when": "", "when_title": "1 Nov 2026, 10:00:00 am"}]
    assert ta.to_posts(raw, now=T0)[0].ts > 0  # без относительного — дата без пояса как запасной вариант


LOTS_THREAD = [(OWNER, LOTS_FIRST), ("bob", "#1 $6"), ("ann", "#2 $10"), (OWNER, "#1 sold to @bob, #2 still open")]


def test_multi_lot_thread_as_whole_never_closes():
    a = ta.analyse_thread(posts(*LOTS_THREAD), now=NOW, thread_id="865492")
    assert a.lots_found == ["1", "2"] and not a.state.ended and a.state.winner is None


def test_multi_lot_refresh_saves_per_lot_and_keeps_open_lot(conn):
    db.upsert_item(conn, db.Item(id="th:865492", source="toyhouse", title="Two AUCTIONS", url=URL,
                                 author=OWNER, activity_ts=NOW - 3600), now=NOW - 3600)
    [entry] = ta.to_refresh(conn, OWNER, now=NOW)
    read = ta.ThreadRead(ta.to_posts(posts(*LOTS_THREAD), NOW))
    auctions, _ = ta.refresh(conn, read, entry, now=NOW)
    assert [(a.lot, a.state.ended, a.state.winner) for a in auctions] == [("1", True, "bob"), ("2", False, None)]
    [c] = ta.sale_candidates(conn)
    assert c["item_id"] == "th:865492#1" and c["buyer"] == "bob" and c["amount"] == 6
    assert [r["thread_id"] for r in ta.to_refresh(conn, OWNER, now=NOW)] == ["865492"]  # лот #2 ещё открыт


def test_save_all_replaces_whole_thread_row(conn):
    ta.watch(conn, URL, "Two auctions", own=True, now=NOW)
    auctions = ta.analyse_lots(posts(*LOTS_THREAD), thread_id="865492", now=NOW)
    ta.save_all(conn, auctions, URL, now=NOW)
    rows = ta.my_rows(conn)
    assert sorted(r["lot"] for r in rows) == ["1", "2"] and all(r["title"] == "Two auctions" for r in rows)


def test_whole_row_with_lots_is_not_a_sale(conn):
    a = ta.analyse_thread(posts(*LOTS_THREAD), now=NOW, thread_id="865492")
    a.state.ended, a.state.winner = True, "bob"  # старая строка, записанная до исправления
    ta.save(conn, a, URL, own=True, now=NOW)
    assert ta.sale_candidates(conn) == []


def test_congrats_name_respects_lot_chunk():
    p = posts((OWNER, LOTS_FIRST), ("bob", "#1 $6"), ("bob", "#2 $9"), ("ann", "#2 $12"),
              (OWNER, "#1 congrats bob! #2 closed, congrats ann"))
    assert ta.analyse_thread(p, lot="2", now=NOW).state.winner == "ann"
    assert ta.analyse_thread(p, lot="1", now=NOW).state.winner == "bob"


def test_conditional_name_is_not_winner():
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("bob", "$15"), (OWNER, "if no one else bids, bob wins!"),
                                ("ann", "$20"), (OWNER, "Closed! Thanks everyone")), now=NOW)
    assert a.state.ended and a.state.winner is None and _bids(a) == [("bob", 15), ("ann", 20)]


def test_congrats_alone_closes_and_later_bids_ignored():
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("bob", "$15"), (OWNER, "Congrats bob! Will send an invoice"),
                                ("ann", "$20")), now=NOW)
    assert a.state.ended and a.state.winner == "bob" and _bids(a) == [("bob", 15)]
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("bob", "$15"), (OWNER, "bob won, pay when you can")), now=NOW)
    assert a.state.winner == "bob"


@pytest.mark.parametrize("bid", ["AB + art", "AB plus a sketch", "AB and my oc"])
def test_ab_with_noncash_extra_is_autobuy(bid):
    a = ta.analyse_thread(posts((OWNER, "SB $10 AB $50, art accepted"), ("bob", bid)), now=NOW)
    assert a.state.ab_hit and a.state.winner == "bob" and a.state.top.amount == 50
    assert a.offers and a.offers[0].with_cash


@pytest.mark.parametrize("bid", ["AB on #2!", "#2 AB", "AB for lot 2"])
def test_ab_for_lot(bid):
    a = ta.analyse_thread(posts((OWNER, LOTS_FIRST), ("ann", bid)), lot="2", now=NOW)
    assert a.state.ab_hit and a.state.top.amount == 40


def test_short_bid_with_design_word_is_not_offer():
    a = ta.analyse_thread(posts((OWNER, "SB $10"), ("bob", "$20 (love this design!!)")), now=NOW)
    assert a.offers == [] and a.state.top.amount == 20


@pytest.mark.parametrize("text", ["Rules:\n1) SB: $10\n2) MI: $1\n3) AB: $60",
                                  "Auction!\n1. SB is $10\n2. MI is $2\n3. AB is $60\n4. Paypal only"])
def test_numbered_rules_are_not_lots(text):
    assert ta.split_lots(text) == {}
    a = ta.analyse_thread(posts((OWNER, text), ("bob", "$15")), now=NOW)
    assert a.terms.sb == 10 and a.terms.ab == 60 and _bids(a) == [("bob", 15)]


def test_numbered_lots_with_sb_still_split():
    assert set(ta.split_lots("1) Wolf SB $5 AB $30\n2) Cat SB $8 AB $40")) == {"1", "2"}


def test_mi_written_after_amount():
    assert ta.parse_terms("SB $10 / $5 inc / AB $100").mi == 5
    assert ta.parse_terms("SB 500 pts, 50 pts min increments").mi == 50


def test_summary_counts_only_rejected_bids():
    # «автобай в другой валюте» — пометка про условия, а не отброшенная ставка.
    a = ta.analyse_thread(posts((OWNER, "SB $10, AB: 5000 pts"), ("bob", "$12")), now=NOW)
    s = ta.summary(a, now=NOW)
    assert "Не посчитала как ставки" not in s and "автобай в другой валюте" in s.lower()


def test_own_none_keeps_watch_choice(conn):
    ta.watch(conn, URL, "Moth dragon", own=True, now=NOW)
    a = _auction((OWNER, "SB $10"), ("anna", "$12"), (OWNER, "Sold to @anna"))
    ta.save(conn, a, URL, own=ta.decide_own(a.owner, "", None), now=NOW)  # ника в настройках нет
    assert ta.my_rows(conn)[0]["own"] == 1 and len(ta.sale_candidates(conn)) == 1
    other = "https://toyhou.se/~forums/71.art-marketplace/777.x"
    b = ta.analyse_thread(posts(("friend", "SB $10"), ("anna", "$12")), thread_id="777", now=NOW)
    ta.save(conn, b, other, now=NOW)  # новый и непонятно чей — не её
    assert conn.execute("SELECT own FROM th_auctions WHERE thread_id = '777'").fetchone()["own"] == 0
    assert ta.decide_own("MothArt", "@mothart") is True and ta.decide_own("x", "mothart", True) is False


def test_sale_item_id_for_manual_record(conn):
    assert ta.sale_item_id(conn, URL) == "th:865492"
    ta.save_all(conn, ta.analyse_lots(posts(*LOTS_THREAD), thread_id="865492", now=NOW), URL, own=True, now=NOW)
    with pytest.raises(ValueError, match="укажи номер лота"):
        ta.sale_item_id(conn, URL)
    assert ta.sale_item_id(conn, URL, "#2") == "th:865492#2"
    with pytest.raises(ValueError):
        ta.sale_item_id(conn, "https://toyhou.se/123.character")


# ---------- чтение по страницам: паузы, кэш, обрывы ----------

def _page(n, total, ids, author="anna", text="$1{}"):
    return json.dumps({"state": "ready", "page": n, "pages_total": total,
                       "posts": [{"post_id": str(i), "author": OWNER if i == 1 else author,
                                  "text": "SB $10" if i == 1 else text.format(i), "ts": T0 + i * 60} for i in ids]})


class FakeChrome:
    def __init__(self, pages, fail=None):
        self.pages, self.fail, self.calls, self.pauses = pages, fail or {}, [], 0

    def fetch(self, url):
        self.calls.append(url)
        n = int(url.split("?page=")[1]) if "?page=" in url else 1
        if n in self.fail:
            if isinstance(self.fail[n], Exception):
                raise self.fail[n]
            return self.fail[n]
        return self.pages[n]

    def pause(self):
        self.pauses += 1


def test_read_thread_long_thread_reads_first_and_last_pages_with_pauses():
    pages = {n: _page(n, 12, [n * 10 + 1, n * 10 + 2] if n > 1 else [1, 2]) for n in range(1, 13)}
    ch = FakeChrome(pages)
    r = ta.read_thread(ch.fetch, URL, max_pages=4, pause=ch.pause, now=NOW)
    assert [c.split("?page=")[-1] if "?page=" in c else "1" for c in ch.calls] == ["1", "10", "11", "12"]
    assert ch.pauses == 3 and r.pages_read == 4 and r.incomplete and "пропущены страницы 2–9" in r.reason


def test_read_thread_cache_reads_only_new_pages(conn):
    pages = {1: _page(1, 2, [1, 2]), 2: _page(2, 2, [3])}
    ch = FakeChrome(pages)
    r = ta.read_thread(ch.fetch, URL, conn=conn, pause=ch.pause, now=NOW)
    assert len(ch.calls) == 2 and [p.post_id for p in r.posts] == ["1", "2", "3"] and not r.incomplete
    ch.pages = {2: _page(2, 3, [3, 4]), 3: _page(3, 3, [5])}
    ch.calls = []
    r = ta.read_thread(ch.fetch, URL, conn=conn, pause=ch.pause, now=NOW + 60)
    assert ch.calls == [URL + "?page=2", URL + "?page=3"]
    assert [p.post_id for p in r.posts] == ["1", "2", "3", "4", "5"] and r.posts[0].text == "SB $10"


def test_read_thread_stops_on_challenge_and_keeps_what_was_read(conn):
    ch = FakeChrome({1: _page(1, 3, [1, 2]), 2: _page(2, 3, [3])}, fail={3: '{"state": "challenge"}'})
    r = ta.read_thread(ch.fetch, URL, conn=conn, pause=ch.pause, now=NOW)
    assert r.incomplete and "страница 3" in r.reason and "Cloudflare" in r.reason and len(r.posts) == 3
    ch.fail = {2: RuntimeError("Chrome не ответил")}
    r = ta.read_thread(ch.fetch, URL, conn=conn, pause=ch.pause, now=NOW)  # с кэшем ошибка не роняет
    assert r.incomplete and "Chrome не ответил" in r.reason and len(r.posts) == 3


def test_read_thread_first_page_error_without_cache_raises():
    ch = FakeChrome({}, fail={1: RuntimeError("нет Chrome")})
    with pytest.raises(RuntimeError):
        ta.read_thread(ch.fetch, URL, now=NOW)
    r = ta.read_thread(FakeChrome({1: None}).fetch, URL, now=NOW)
    assert r.posts == [] and r.state == "no_posts"


def test_read_thread_feeds_analysis_end_to_end(conn):
    pages = {1: _page(1, 2, [1, 2], text="$1{}"), 2: _page(2, 2, [3], text="$2{}")}
    ch = FakeChrome(pages)
    read = ta.read_thread(ch.fetch, URL, conn=conn, now=NOW)
    auctions, _ = ta.refresh(conn, read, {"thread_id": "865492", "url": URL, "title": "Moth", "own": 1}, now=NOW)
    assert auctions[0].state.top.amount == 23 and auctions[0].owner == OWNER
