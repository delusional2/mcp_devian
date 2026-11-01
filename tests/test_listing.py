import pytest

from mcp_devian import advisor, db, listing
from mcp_devian.config import Config
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400


@pytest.mark.parametrize("text, on, off", [
    ("SB: $15 MI: $2 AB: $80. Payment via PayPal invoice only.", {"sb", "mi", "ab", "pay_paypal"},
     {"pay_kofi", "pay_points", "end_time"}),
    ("You can pay with Ko-fi or 1500 pts", {"pay_kofi", "pay_points"}, {"pay_paypal"}),
    ("Auction ends 48 hours after the last bid (GMT+3)", {"end_time", "timezone"}, {"sb"}),
    ("The auction will end 24h after last bid", {"end_time"}, set()),
    ("Ends 03.11 20:00 EST", {"end_time", "timezone"}, set()),
    ("Auction Duration: 48 hours", {"end_time"}, set()),
    ("After the auction ends, you must pay within 24h", set(), {"end_time"}),
    ("Winner gets a non-watermark full res PNG", {"unwatermarked", "fullres"}, {"rights"}),
    ("AB2: $250 (commercial use included)", {"ab", "rights"}, set()),
    ("Please read my TOS before bidding. Holds up to a week.", {"tos", "hold"}, set()),
    ("Will be transferred on Toyhouse", {"toyhouse"}, set()),
    ("hi", set(), {"tos", "pay_paypal", "sb", "end_time", "unwatermarked"}),
])
def test_listing_facts(text, on, off):
    f = listing.listing_facts(text)
    assert set(listing.FACT_KEYS) == set(f)
    assert f["desc_len"] == len(text)
    for k in on:
        assert f[k] is True, k
    for k in off:
        assert f[k] is False, k


def test_full_description_preferred_over_snippet():
    row = {"extra": '{"desc": "Payment: PayPal. TOS in journal."}', "snippet": "SB: $10"}
    facts, full = listing.row_facts(row)
    assert full and facts["pay_paypal"] and facts["tos"]
    facts, full = listing.row_facts({"extra": "{}", "snippet": "SB: $10"})
    assert not full and facts["sb"] and not facts["pay_paypal"]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "l.sqlite3")
    yield c
    c.close()


def market_lot(conn, n, title, desc, lot_type="auction"):
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=title, url=f"u{n}", lot_type=lot_type,
                              status="open", price_usd=10, published_ts=NOW - 5 * DAY, snippet=desc[:300],
                              extra={"desc": desc}), now=NOW - 5 * DAY)


def test_listing_norms_refuses_below_30(conn):
    for i in range(29):
        market_lot(conn, i, "[OPEN] Cat adopt auction", "SB: $10. Ends in 48h.")
    assert advisor.listing_norms(conn, "auction", 60, now=NOW).startswith("мало данных")


def test_listing_norms_shares(conn):
    for i in range(30):
        desc = "SB: $10 AB: $50. " + ("Ends 24h after last bid. " if i < 27 else "") + ("PayPal only." if i < 10 else "")
        market_lot(conn, i, "[OPEN] Cat adopt auction" + (" SB $10" if i < 2 else ""), desc)
    market_lot(conn, 99, "Cat flatsale", "Price $10", lot_type="flatsale")
    text = advisor.listing_norms(conn, "auction", 60, now=NOW)
    assert "(n=30, " in text
    assert "- срок окончания: 27 из 30 (90%)" in text
    assert "- цена в названии: 2 из 30 (7%)" in text
    assert "- статус в названии ([OPEN] и т.п.): 30 из 30 (100%)" in text
    assert "- способ оплаты: 10 из 30 (33%)" in text
    assert "не причины" in text and "поднимает" not in text


def test_listing_review_uses_market_share_for_end_time(conn):
    for i in range(30):
        market_lot(conn, i, "[OPEN] Cat adopt auction", "SB: $10. Ends 24h after last bid." if i < 25 else "SB: $10")
    db.upsert_item(conn, Item(id="da:500", source="deviantart", title="[OPEN] Moth adopt auction", url="u",
                              lot_type="auction", status="open", own=True, published_ts=NOW - DAY,
                              snippet="SB: $10 PayPal TOS png"), now=NOW - DAY)
    row = db.get_item(conn, "da:500")
    text = "\n".join(advisor.listing_review(row, conn, Config(), now=NOW))
    assert "когда закончится аукцион" in text and "срок пишут 25 из 30 аукционов (83%)" in text
    assert "В названии нет цены" not in text and "поднимает клики" not in text


@pytest.mark.parametrize("text, on, off", [
    ("Payment via boosty.to/mothart (donate)", {"pay_boosty"}, {"pay_other", "foreign_pay_hint"}),
    ("Pay on Boosty: pick «card issued outside Russia», switch the currency to USD, 3-D Secure needed",
     {"pay_boosty", "foreign_pay_hint"}, {"pay_other"}),
    ("Foreign cards work on my Boosty, 3DS required", {"pay_boosty", "foreign_pay_hint"}, set()),
    # Валюту на Boosty не выбирают (FAQ 12.2025) — «switch to USD» подсказкой не считается.
    ("Boosty only. I'll switch to USD later", {"pay_boosty"}, {"foreign_pay_hint"}),
    ("Boosty, set price eur", {"pay_boosty"}, {"foreign_pay_hint"}),
    ("Paying by card via Stripe", {"pay_other"}, {"pay_boosty"}),
    ("Please pay within 48 hours after the auction ends", {"payment_window"}, set()),
    ("You have 24h to pay, otherwise it goes to the 2nd bidder", {"payment_window"}, set()),
    ("Payment is due in 2 days", {"payment_window"}, set()),
    ("Auction ends in 48 hours", set(), {"payment_window"}),
])
def test_boosty_and_payment_facts(text, on, off):
    f = listing.listing_facts(text)
    for k in on:
        assert f[k] is True, k
    for k in off:
        assert f[k] is False, k
    assert listing.pays_any(f) is bool(f["pay_boosty"] or f["pay_other"])
