import pytest

from mcp_devian.classify import classify_lot, classify_thread, keyword_hits, match_lot, normalize_text
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


# --- Распознавание на живых постах DA (аудит ноября 2026) ---

@pytest.mark.parametrize("title,text,usd,points", [
    # Две валюты через «/»: раньше «$30 / 3000» давало $3.
    ("Pony adopts", "SB: $30 / 3000 points each", 30, 3000),
    ("Adopt", "Price: $15 / 1500 pts", 15, 1500),
    # «10$/1000 point», «Kofi/400pts» — «/» перед числом не мешает поинтам.
    ("Adopt (open)", "Fixed price ✦10$/1000 point ✦", 10, 1000),
    ("OTA Pony", "MIN OFFER IS: 4$ via PayPal or Kofi/400pts!", 4, 400),
    # Курс и скидки — не цены.
    ("Adopt", "Price $20 (1$ = 100)", 20, None),
    ("YCH batch from $15", "get $5 OFF total", 15, None),
    ("$5 SALE OFF FULL BATCH from $15 OPEN", "", 15, None),
    # Модные буквы, «|» после метки, «15 $» с пробелом.
    ("Adopt", "SB | $150 (Commercial) Min BID | $25 AB | $600", 150, None),
    ("Adoptable outfits", "For all: Sb: 15 $ Min: 2 $ Ab: 25 $", 15, None),
    # SB без валюты главнее сумм в наградах.
    ("Fawn", "SB: 25 MI: 5 AB: 280 UNLOCKABLE REWARDS Tier 2 (55$)", 25, None),
    # Цена в названии + та же цена с поинтами в описании.
    ("$9 PWYW Alien Pony Adopts", "Minimum offer: $9 / 900 points each", 9, 900),
    ("$3.5 Pony Gacha", "New Roll: $3.5 / 350 points Reroll: $1.8 / 180 points", 3.5, 350),
    # Пробел как разделитель тысяч — только у поинтов и только после метки или знака:
    # «Cat adopt 3 500» — это «3» и «500», а «SB: 1 000 pts» — тысяча.
    ("Cat adopt 3 500 pts", "", None, 500),
    ("[OPEN] Dragon adopt auction", "SB: 1 000 pts … AB: 5 000 pts", None, 1000),
    ("Adopt (OPEN)", "Price: $17 USD (approximately €15/ 1 360 Points)", 17, 1360),
    ("Moth adopt", "Tier 2 100$", 100, None),
])
def test_live_post_prices(title, text, usd, points):
    lot = classify_lot(title, text)
    assert (lot.usd, lot.points) == (usd, points)


def test_fancy_unicode_starting_bid():
    lot = classify_lot("[OPEN] Adoptable : Vesperia", "✦ Owner : ✧ 𝑺𝑩 : 150$ ✧ 𝑩𝑰𝑫 : 10$ auction end in 24h")
    assert (lot.lot_type, lot.usd, lot.is_sb) == ("auction", 150, True)
    assert normalize_text("𝓢𝓑: $10") == "SB: $10"


@pytest.mark.parametrize("title,text,lot_type", [
    ("$5 Smol Pony Adopts PWYW", "", "ota"),
    ("Smol pony adopt open", "Minimum offer: $9 each", "ota"),
    ("$3.5 Cheap Tiny Bean Pony Gacha", "", "raffle"),
    ("🔥 SUPER CHEAP $9 YCHS OPEN", "", "ych"),
    ("CHEAP $10 CUSTOMS PONY OPEN", "", "commission"),
    ("Customs adopt", "", "adopt"),
    ("Mostly $4–$9 TOYHOUSE CLEAROUT SALE!", "", "flatsale"),
    ("🎃 HALLOWEEN SALE 35$ each", "", "flatsale"),
    ("Oc for sale $10", "", "adopt"),
    ("Adopt (OPEN)", "Price: $17 USD", "flatsale"),
    ("Pokémon Gardevoir | sold adopt", "I — ADOPT ✦ SB 25$ MI 3$ AB 150$", "auction"),
])
def test_live_post_types(title, text, lot_type):
    assert classify_lot(title, text).lot_type == lot_type


@pytest.mark.parametrize("title,text,status", [
    ("[ADOPTABLE AUCTION close] The reflection", "", "closed"),
    ("Adopts [0/3 OPEN]", "", "closed"),
    ("ADOPTABLES AUCTION (OPEN 2/3) #10", "", "open"),
    ("Calista", "#AUCTION : Open Status: Available SB: 30 USD", "open"),
    ("Adoptable Incredible Mage ( 2026/11)", "[Status: Auction Open] ✦ Brief", "open"),
    ("Moth", "Status: SOLD", "closed"),
    ("[OPEN] Moth", "Status: closed", "open"),  # метка в названии главнее
])
def test_live_post_status(title, text, status):
    assert classify_lot(title, text).status == status


@pytest.mark.parametrize("title,text,multi", [
    ("ADOPTABLES AUCTION (OPEN 2/3) #10", "", True),
    ("Halloween Batch Auction", "", True),
    ("🎃 HALLOWEEN SALE 35$ each", "", True),
    ("Cheap cutiemarks", "Still open: 1. $6 2. SOLD 3. $9", True),
    ("Cats Adopts Auction", "AUCTION for each adopt separately SB: $10", True),
    ("Breedable Fusion Grid Open", "", True),
    # Даты — не доли пачки.
    ("Adoptable Incredible Mage ( 2026/11)", "", False),
    ("ADOPT (OPEN) | 11/11|", "", False),
    ("[OPEN] Adoptable Auction - Maid Paladin", "SB: 25 USD", False),
])
def test_multi_lot_flag(title, text, multi):
    assert classify_lot(title, text).multi is multi


def test_open_call_lf_with_offer_is_a_buyer():
    kind = classify_thread("LF: anything! O: OTA")
    assert (kind.wants_characters, kind.sells_characters) == (True, False)
    assert classify_thread("Purge | LF: anything | O: ota").wants_characters is False


@pytest.mark.parametrize("title,text,other", [
    ("Fox adopt", "SB: CAD 20", (20, "cad")),
    ("Fox adopt C$20", "", (20, "cad")),
    ("Fox adopt 15 AUD", "", (15, "aud")),
    ("Fox adopt A$15", "", (15, "aud")),
    ("Fox adopt NZ$30", "", (30, "nzd")),
])
def test_other_dollars_are_not_usd(title, text, other):
    lot = classify_lot(title, text)
    assert lot.usd is None and lot.other_price == other
    assert classify_lot("Fox adopt $20").usd == 20


# --- Находки проверки распознавания ---

@pytest.mark.parametrize("title,text,price", [
    # Вторая валюта — только из пары «$X / N pts», а не из доплат в описании.
    ("Cat adopt 1500 pts", "1500 points. Want a PNG? +$5", "1500 pts"),
    ("Cat adopt $15", "Price $15. Extra chibi: +500 pts", "$15"),
    ("Cat adopt $9", "Price: $9 / 900 points", "$9 / 900 pts"),
    # «start bid» — стартовая ставка, а не пропуск.
    ("[CLOSED] ADOPT AUCTION Number:102", "start bid 70 USD minimum increase: 4 USD ab: 130 USD", "SB $70"),
    ("Adoptable [OPEN]", "Start bid: 10$ / 1000 points", "SB $10 / 1000 pts"),
    # Курс поинтов — не цена.
    ("Adoptable [OPEN]", "Price: 2000 points (1$/100pts)", "2000 pts"),
    ("Adoptable [OPEN]", "SB: $20 (1$/100pts)", "SB $20"),
    ("Adoptable [OPEN]", "Price: $20 or points ($1 = 100 points)", "$20"),
    ("Adoptable [OPEN]", "Price: 20$. Points accepted, 100 pts per 1$", "$20"),
    ("Adoptable [OPEN]", "Price: 10$/1000 point", "$10 / 1000 pts"),
    ("Adoptable [OPEN]", "Ticket: $1 / 100 pts", "$1 / 100 pts"),  # других сумм нет — это и есть цена
])
def test_verified_price_regressions(title, text, price):
    assert classify_lot(title, text).price_text == price


@pytest.mark.parametrize("title,text", [
    ("Auction Adoptable 017 | CLOSED | Grim Goat", "SB: $10. Each new bid resets the timer"),
    ("Adopt #3 (OPEN)", "Price $40. Reference Sheet Pack ... +$50"),
    ("Adoption Auction Sold", "SB $20. Commission: $60 (flat) / $120 (full render) each"),
    ("Adopt (OPEN)", "SB $20 " + "x" * 400 + " batch"),  # дальше начала описания — реклама и правила
    ("Adopt (OPEN)", "SB $20 https://ko-fi.com/Base-Pack-on-Kofi"),
])
def test_not_a_batch_on_full_descriptions(title, text):
    assert classify_lot(title, text).multi is False


@pytest.mark.parametrize("text,lot_type", [
    ("Price: SB $10 MI $1 AB $50", "auction"), ("Price: OTA, min offer $5", "ota"),
    ("Price: $20 (BTA)", "bta"), ("Price: $20", "flatsale"),
])
def test_price_label_does_not_override_specific_type(text, lot_type):
    assert classify_lot("Adoptable [OPEN]", text).lot_type == lot_type


@pytest.mark.parametrize("title,status", [
    ("Dragon Auction (close 3/11)", "unknown"), ("Dragon auction close at 8pm EST", "unknown"),
    ("Dragon auction Close Date: Nov 5", "unknown"), ("Dragon auction [CLOSE]", "closed"),
])
def test_close_with_date_is_not_closed(title, status):
    assert classify_lot(title).status == status


def test_offers_on_anything_is_a_seller():
    k = classify_thread("LF: offers on anything | O: ota")
    assert not k.wants_characters and k.sells_characters


def test_custom_adopt_auction_is_an_auction():
    assert classify_lot("Custom Dragon Adopt Auction", "SB: 45 USD").lot_type == "auction"
    assert classify_lot("Custom dragon commission", "").lot_type == "commission"


@pytest.mark.parametrize("title,text,other,label", [
    ("Moth adopt 1500₽", "", (1500, "rub"), "1500 ₽"),
    ("Moth adopt", "Price: 1500 руб", (1500, "rub"), "1500 ₽"),
    ("Moth adopt", "цена 2000 рублей", (2000, "rub"), "2000 ₽"),
    ("Moth adopt", "SB: 900 RUB", (900, "rub"), "SB 900 ₽"),
    ("Moth adopt", "SB 500₽ AB 2000₽", (500, "rub"), "SB 500 ₽"),
    # Тысячи через пробел — обычная русская запись (раньше «1 500 руб» читалось как 500 ₽).
    ("Moth adopt", "Цена: 1 500 руб", (1500, "rub"), "1500 ₽"),
    ("Moth adopt", "за 12 000 ₽", (12000, "rub"), "12000 ₽"),
    ("Moth adopt", "price 1 200₽", (1200, "rub"), "1200 ₽"),
    ("Moth adopt", "SB 1 200 rub", (1200, "rub"), "SB 1200 ₽"),
    # Без метки число перед тысячами — из названия («Cat 3»), как у поинтов.
    ("Cat 3 500 руб", "", (500, "rub"), "500 ₽"),
])
def test_rub_prices_not_read_as_usd(title, text, other, label):
    lot = classify_lot(title, text)
    assert lot.usd is None
    assert (lot.other_price, lot.price_text) == (other, label)


def test_rub_next_to_usd_kept_apart():
    lot = classify_lot("Moth", "$10 or 900 руб")
    assert lot.usd == 10 and lot.other_price == (900, "rub")
    assert classify_lot("рубин adopt $5").other_price is None
