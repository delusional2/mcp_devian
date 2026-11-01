import pytest

from mcp_devian.classify import classify_lot, classify_thread, keyword_hits, match_lot
from mcp_devian.config import Filters


@pytest.mark.parametrize("title,lot_type,status", [
    ("Dragon adopt AUCTION [OPEN] SB: $10 AB: 60$", "auction", "open"),
    ("YCH commission Done [SLOTS OPEN]", "ych", "open"),
    ("OTA feline adopt (CLOSED)", "ota", "closed"),
    ("Flatsale 1,500 pts cat adopt", "flatsale", "unknown"),
    ("BTA moth [open]", "bta", "open"),
    ("Raffle! free adopt", "raffle", "unknown"),
    ("Sold - dragon", "other", "closed"),
    ("Kitsune adoptable", "adopt", "unknown"),
])
def test_lot_type_and_status(title, lot_type, status):
    lot = classify_lot(title)
    assert (lot.lot_type, lot.status) == (lot_type, status)


@pytest.mark.parametrize("title,text,usd,points", [
    ("Auction SB: $10 AB: $60", "", 10, None),
    ("Auction AB: 60$ SB: 15$", "", 15, None),
    ("Flatsale 1,500 pts", "", None, 1500),
    ("Adopt batch 1.5k points each", "", None, 1500),
    ("fantasy adopts [05$-55$ USD]", "", 5, None),
    ("Moth adopt", "price is 25 USD", 25, None),
    ("Adopt 12.50$", "", 12.5, None),
])
def test_prices(title, text, usd, points):
    lot = classify_lot(title, text)
    assert lot.usd == usd
    assert lot.points == points


def test_auction_detected_from_description():
    lot = classify_lot("Adoptable by Someone - OPEN", "------ SB : 10$ Mi : 1$ AB : 60$")
    assert (lot.lot_type, lot.usd, lot.price_text) == ("auction", 10, "SB $10")


def test_price_label():
    assert classify_lot("Cats Adopts", "SB: $10 (USD) OR 800 points").price_text == "SB $10 / 800 pts"
    assert classify_lot("OPEN 15$ - Witch Adoptable").price_text == "$15"


def test_description_mentions_do_not_change_type():
    # «commissions open» в описании адопта не делает его комишкой.
    assert classify_lot("Moth adopt", "my commissions are open too").lot_type == "adopt"


def test_match_lot_filters():
    f = Filters(keywords=["dragon"], exclude=["ych"], max_usd=20)
    ok, reason = match_lot(f, classify_lot("Dragon adopt auction SB: $10"), "Dragon adopt auction SB: $10")
    assert ok and "dragon" in reason
    assert not match_lot(f, classify_lot("Cat adopt auction SB: $10"), "Cat adopt auction SB: $10")[0]
    assert not match_lot(f, classify_lot("Dragon adopt SB: $30"), "Dragon adopt SB: $30")[0]
    assert not match_lot(f, classify_lot("Dragon OTA (closed)"), "Dragon OTA (closed)")[0]
    assert not match_lot(Filters(), classify_lot("YCH dragon"), "YCH dragon")[0]


def test_keyword_hits_word_start():
    assert keyword_hits(["cat"], "Cats for sale") == ["cat"]
    assert keyword_hits(["cat"], "Scatter adopt") == []


@pytest.mark.parametrize("title,wants,sells", [
    ("I AM SEEKING MEN !!!!! O: Trades/Art/$$", True, False),
    ("LF: OCS/CUSTOMS/$$ | OF: OCs/ART", True, True),
    ("LF dragon characters! O: $$ / pts", True, False),
    ("looking for cat ocs, paying usd", True, False),
    ("LF: $$$, O: CHARACTERS", False, True),
    ("✦ PURGE  | LF: TRADES OR € ✦", False, True),
    ("💌 OC purge! LF: $$$/ART", False, True),
    ("Adoptable Dragon Hybrid LF NEW HOME", False, True),
    ("♡ fantasy, feral, & anthro adopts! [05$-55$ USD] ♡", False, True),
    ("Syunthia's Resale Center", False, True),
])
def test_classify_thread(title, wants, sells):
    kind = classify_thread(title)
    assert (kind.wants_characters, kind.sells_characters) == (wants, sells)


@pytest.mark.parametrize("title,text,usd,points", [
    # «k» — множитель, только если это отдельный суффикс.
    ("$5 kitsune adopt [OPEN]", "", 5, None),
    ("Adopt $15 kitty", "", 15, None),
    ("Adopt $5 ko-fi only", "", 5, None),
    ("Moth", "SB: $5 kofi", 5, None),
    ("2k pts adopt", "", None, 2000),
    ("$1.5k adopt", "", 1500, None),
    # Посторонние числа рядом с валютой.
    ("Adopt #3 $10 [OPEN]", "", 10, None),
    ("Adopt 5$ 3 left", "", 5, None),
    ("Purge! $5 each, LF $ 3 left", "", 5, None),
    ("Cat adopt 3 500 pts", "", None, 500),
    ("Adopt 1,500 pts", "", None, 1500),
    ("Adopt 1.500 pts", "", None, 1500),
    ("SB : $ 60 MD : $ 1", "", 60, None),
    # Без SB не берём MI/AB/TOS за цену.
    ("Moth adopt", "SB: 5 / MI: $1 / AB: $50", 5, None),
    ("Moth adopt", "AB: $50 MI: $1", None, None),
    ("Flat sale adopt", "Price $30. TOS: no reselling for more than 10$", 30, None),
    ("Owl", "•SB: $34 •MI: $5 •AB: $500", 34, None),
    # SB в одной валюте — AB в другой не выдаём за стартовую цену.
    ("Moth", "SB: 100 pts | AB: $50", None, 100),
    ("Moth", "SB: $10 | AB: $100 or 10000 pts", 10, None),
    ("Cats Adopts", "SB: $10 (USD) OR 800 points", 10, 800),
    # Поинты, записанные через DA.
    ("Pretty boy", "500 DA points", None, 500),
    ("Pretty boy", "500 dA pts", None, 500),
])
def test_price_regressions(title, text, usd, points):
    lot = classify_lot(title, text)
    assert (lot.usd, lot.points) == (usd, points)


@pytest.mark.parametrize("title,status", [
    ("Adopt batch [OPEN] 2/5 sold", "open"),
    ("3/6 OPEN - 3 taken", "open"),
    ("Fox adopt OPEN (2 left, 1 sold)", "open"),
    ("ADOPTS [2/4 SOLD]", "open"),
    ("Adopt OPEN 1 taken", "open"),
    ("Resale [OPEN] - adopted from @someartist", "open"),
    ("Adopt [OPEN] - gone in 24h if no offers", "open"),
    ("{open} cat adopt", "open"),
    ("Batch 5/5 SOLD", "closed"),
    ("Sold out", "closed"),
    ("Adoptable - not available", "closed"),
    ("Adopt - no longer available", "closed"),
    ("Adopt [ON HOLD]", "closed"),
    ("Adopt [PENDING]", "closed"),
    ("Adopt (reserved for X)", "closed"),
])
def test_status_regressions(title, status):
    assert classify_lot(title).status == status


@pytest.mark.parametrize("title,text,tags,lot_type", [
    ("Kemonomimi girl", "price 15$", (), "adopt"),
    ("Oc for sale $10", "", (), "adopt"),
    ("Selling oc", "", (), "adopt"),
    ("Pretty", "", ("adoptauction",), "auction"),
    ("Pretty", "", ("adoptsopen",), "adopt"),
    ("Pretty", "", ("dakota",), "other"),
    # Реклама и отрицания в описании не меняют тип.
    ("Moth adopt [OPEN]", "check my other auctions! price $20", (), "adopt"),
    ("Moth adopt", "no raffles, flat price $20", (), "flatsale"),
])
def test_lot_type_regressions(title, text, tags, lot_type):
    assert classify_lot(title, text, tags).lot_type == lot_type


def test_price_label_sb_in_points():
    assert classify_lot("Moth", "SB: 100 pts | AB: $50").price_text == "SB 100 pts"


@pytest.mark.parametrize("title,wants,sells,pays", [
    # Продавец ищет покупателя для своих персов.
    ("LF: $$ for my ocs", False, True, []),
    ("LF: usd or art for my characters", False, True, []),
    ("lf usd, art, trades for my ocs", False, True, []),
    ("LF: offers on my characters", False, True, []),
    ("LF: new homes for my ocs", False, True, []),
    ("LF homes for my babies", False, True, []),
    ("LF: people to buy my ocs", False, True, []),
    # Покупатель без слова LF.
    ("Offering $ for deer characters", True, False, ["usd"]),
    ("Paying usd for customs", True, False, ["usd"]),
    ("Looking to buy cats", True, False, []),
    ("Will pay $ for dragons", True, False, ["usd"]),
    ("Looking to buy: cats | O: $", True, False, ["usd"]),
    ("Selling ocs, offering discounts for bulk", False, True, []),
    # Заказ рисунка — не поиск персов и не продажа.
    ("LF: art of my oc | O: pts", False, False, []),
    ("LF: someone to draw my oc", False, False, []),
    ("LF: artists | O: $$", False, False, []),
    ("LF: Toyhouse invite code", False, False, []),
    ("LF: art | O: $$", False, False, []),
    ("Paying $ for art/comms", False, False, []),
    ("LF: Art / O: Characters", False, True, []),
    # Оплата после тире.
    ("LF customs - paying usd", True, False, ["usd"]),
    ("LF: anthro ocs - USD", True, False, ["usd"]),
])
def test_thread_regressions(title, wants, sells, pays):
    kind = classify_thread(title)
    assert (kind.wants_characters, kind.sells_characters, kind.pays) == (wants, sells, pays)


def test_keyword_whole_words():
    assert keyword_hits(["base"], "Moth adopt based on a luna moth") == []
    assert keyword_hits(["base"], "f2u bases") == ["base"]
    assert keyword_hits(["commission"], "commissioned piece") == []
    assert keyword_hits(["fox"], "foxes for sale") == ["fox"]


def test_exclude_base_keeps_based_on():
    f = Filters(exclude=["base"])
    title = "Moth adopt based on a luna moth [OPEN] $10"
    assert match_lot(f, classify_lot(title), title)[0]


def test_thread_pays():
    assert classify_thread("I AM SEEKING MEN O: Trades/Art/$$").pays == ["usd", "art", "trades"]
    assert classify_thread("looking for cat ocs, paying usd").pays == ["usd"]
    # У распродажи «LF: $$» — это что продавец хочет, а не чем платит.
    assert classify_thread("ORANGE'S PURGE (LF: Art, Ocs, $$, etc.)").pays == []


@pytest.mark.parametrize("title,text,usd,points,other,label", [
    # #28: у каждой суммы своя валюта; € и £ не попадают в usd.
    ("Moth auction", "SB: €30 AB: €90", None, None, (30, "eur"), "SB €30"),
    ("Fox flatsale £25", "", None, None, (25, "gbp"), "£25"),
    ("Cat adopt 20 EUR", "", None, None, (20, "eur"), "€20"),
    ("Owl", "SB: $10 AB: €50", 10, None, None, "SB $10"),
    ("Kitsune", "SB: €15 or 1500 points", None, 1500, (15, "eur"), "SB €15 / 1500 pts"),
    ("Dragon adopt $12", "", 12, None, None, "$12"),
])
def test_currencies(title, text, usd, points, other, label):
    lot = classify_lot(title, text)
    assert (lot.usd, lot.points, lot.other_price, lot.price_text) == (usd, points, other, label)


def test_euro_lot_skips_usd_budget():
    # Курс не угадываем: бюджет в $ к лоту в € не применяется, но тип и цена видны.
    f = Filters(max_usd=10)
    lot = classify_lot("Moth adopt €40")
    assert lot.lot_type == "adopt" and match_lot(f, lot, "Moth adopt €40")[0]
