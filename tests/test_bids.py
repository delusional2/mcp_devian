import pytest

from mcp_devian.bids import Comment, auction_summary, find_amount, money, parse_bids

T0 = 1_800_000_000.0
OWNER = "MothArt"


def thread(*items):
    """(user, text) или (user, text, parent_index) — по минуте между комментариями."""
    out = []
    for i, it in enumerate(items):
        user, text = it[0], it[1]
        parent = out[it[2]].comment_id if len(it) > 2 else None
        out.append(Comment(str(1000 + i), parent, user, T0 + i * 60, text, 1 if parent else 0))
    return out


def test_simple_auction_with_raises_and_ab():
    st = parse_bids(thread(
        (OWNER, "Bid here please ❤️"),
        ("anna", "SB", 0),
        ("bob", "$15", 0),
        ("anna", "+5", 0),
        ("cat", "25$", 0),
    ), OWNER, sb=10, ab=80, currency="usd")
    assert [b.amount for b in st.bids] == [10, 15, 20, 25]
    assert [b.kind for b in st.bids] == ["sb", "bid", "raise", "bid"]
    assert st.top.user == "cat" and st.top.amount == 25
    assert (st.n_bids, st.bidders) == (4, 3)
    assert st.last_bid_ts == T0 + 4 * 60
    assert not st.ab_hit and not st.ended


def test_autobuy_ends_auction():
    st = parse_bids(thread(("anna", "sb"), ("bob", "AB!"), ("cat", "$100")), OWNER, sb=10, ab=80, currency="usd")
    assert st.ab_hit and st.ended and st.winner == "bob"
    assert st.top.amount == 80 and st.top.kind == "ab"
    assert any("после закрытия" in n for n in st.notes)


def test_explicit_amount_reaching_ab_counts_as_autobuy():
    st = parse_bids(thread(("anna", "$80")), OWNER, sb=10, ab=80, currency="usd")
    assert st.ab_hit and st.ended


def test_out_of_order_input_is_sorted():
    comments = thread(("anna", "$15"), ("bob", "$20"), ("cat", "$30"))
    st = parse_bids(list(reversed(comments)), OWNER, sb=10, currency="usd")
    assert [b.user for b in st.bids] == ["anna", "bob", "cat"]


def test_lower_or_equal_bid_ignored_with_note():
    st = parse_bids(thread(("anna", "$20"), ("bob", "$20"), ("cat", "$18")), OWNER, sb=10, currency="usd")
    assert st.top.user == "anna" and st.n_bids == 1
    assert sum("не выше текущей" in n for n in st.notes) == 2


def test_below_sb_ignored():
    st = parse_bids(thread(("anna", "$5")), OWNER, sb=10, currency="usd")
    assert st.top is None and "ниже SB" in st.notes[0]


def test_retraction_removes_latest_bid():
    st = parse_bids(thread(("anna", "$15"), ("bob", "$20"), ("bob", "nvm, retracting my bid sorry")),
                    OWNER, sb=10, currency="usd")
    assert st.top.user == "anna" and st.n_bids == 1 and "отозвал" in st.notes[-1]


def test_russian_retraction_and_bids():
    st = parse_bids(thread(("маша", "ставка 15$"), ("петя", "20 долларов"), ("петя", "отменяю, простите")),
                    OWNER, sb=10, currency="usd")
    assert st.top.user == "маша" and st.top.amount == 15


def test_owner_closes_with_winner():
    st = parse_bids(thread(("anna", "$15"), ("bob", "$22"), (OWNER, "Closed! Congrats @bob, check notes")),
                    OWNER, sb=10, currency="usd")
    assert st.ended and st.winner == "bob" and not st.ab_hit


@pytest.mark.parametrize("text,winner", [
    ("SOLD to @kiwi_cat", "kiwi_cat"),
    ("Auction ended, winner is Bob123", "Bob123"),
    ("Аукцион закрыт, победитель: Маша", "Маша"),
    ("@lynx won! thank you all", "lynx"),
    ("closed, thank you everyone!", None),
])
def test_owner_winner_forms(text, winner):
    st = parse_bids(thread(("x", "$15"), (OWNER, text)), OWNER, sb=10, currency="usd")
    assert st.ended and st.winner == winner


def test_owner_future_tense_does_not_close():
    st = parse_bids(thread((OWNER, "Will be closed 24 hours after the last bid"), ("anna", "$15")),
                    OWNER, sb=10, currency="usd")
    assert not st.ended and st.top.amount == 15


def test_owner_ab_taken():
    st = parse_bids(thread(("anna", "$15"), (OWNER, "AB taken, thank you!")), OWNER, sb=10, ab=60, currency="usd")
    assert st.ab_hit and st.ended


def test_owner_comments_are_never_bids():
    st = parse_bids(thread((OWNER, "SB $10, MI $1, AB $60")), OWNER, sb=10, currency="usd")
    assert st.top is None


@pytest.mark.parametrize("text", [
    "is $20 ok?",
    "I paid $25 last time for one of yours and loved it!",
    "my commissions start at $10 btw",
    "omg she is so pretty, I wish I had money, maybe 50$ next month if I get paid on time haha",
])
def test_chatty_and_questions_are_not_bids(text):
    st = parse_bids(thread(("anna", text)), OWNER, sb=10, currency="usd")
    assert st.top is None and st.notes


def test_explicit_bid_word_allows_long_comment():
    st = parse_bids(thread(("anna", "I would love to bid 30$ on this beautiful moth dragon, she's perfect for my story")),
                    OWNER, sb=10, currency="usd")
    assert st.top.amount == 30


def test_bare_number_only_when_currency_known():
    st = parse_bids(thread(("anna", "15"), ("bob", "20!")), OWNER, sb=10, currency="usd")
    assert [b.amount for b in st.bids] == [15, 20]
    st = parse_bids(thread(("anna", "15")), OWNER, sb=None, currency=None)
    assert st.top is None


def test_points_auction_and_other_currency_not_mixed():
    st = parse_bids(thread(("anna", "SB"), ("bob", "1,500 pts"), ("cat", "$20"), ("dan", "+100")),
                    OWNER, sb=1000, currency="points")
    assert [b.amount for b in st.bids] == [1000, 1500, 1600]
    assert st.currency == "points"
    assert any("другой валюте" in n for n in st.notes)


def test_currency_learned_from_first_bid():
    st = parse_bids(thread(("anna", "€12"), ("bob", "15")), OWNER, sb=None, currency=None)
    assert st.currency == "eur" and st.top.amount == 15


def test_min_increment_violation_kept_but_noted():
    st = parse_bids(thread(("anna", "$15"), ("bob", "$15.50")), OWNER, sb=10, currency="usd", min_inc=1)
    assert st.top.amount == 15.5 and any("меньше MI" in n for n in st.notes)


def test_raise_without_base_is_noted():
    st = parse_bids(thread(("anna", "+5")), OWNER, sb=None, currency="usd")
    assert st.top is None and "+N" in st.notes[0]


def test_ab_without_known_amount():
    st = parse_bids(thread(("anna", "AB")), OWNER, sb=10, ab=None, currency="usd")
    assert st.top is None and "автобая" in st.notes[0]


def test_find_amount_forms():
    assert find_amount("SB: $ 25") == (25, "usd")
    assert find_amount("30 USD please") == (30, "usd")
    assert find_amount("2k pts") == (2000, "points")
    assert find_amount("£8") == (8, "gbp")
    assert find_amount("cute!") is None


def test_summary_text():
    st = parse_bids(thread(("anna", "SB"), ("bob", "$20"), ("anna", "+5")), OWNER, sb=10, ab=80, currency="usd")
    text = auction_summary(st, sb=10, ab=80, currency="usd", now=T0 + 2 * 3600)
    assert text.startswith("Ставок: 3 от 2 человек, сейчас $25 у anna (SB $10)")
    assert "автобай $80 не взят" in text and "проверь глазами" in text
    assert "1 человека" in auction_summary(parse_bids(thread(("a", "$15")), OWNER, sb=10, currency="usd"), 10)


def test_summary_without_bids_and_closed():
    st = parse_bids(thread((OWNER, "closed, sold to @kiwi")), OWNER, sb=10, currency="usd")
    text = auction_summary(st, sb=10, currency="usd")
    assert text.startswith("Ставок пока нет (SB $10)") and "победитель — kiwi" in text


def test_money_format():
    assert money(1500, "points") == "1500 pts" and money(12.5, "eur") == "€12.5" and money(None, "usd") == "?"


# --- Регрессии по ревью 0.3 ---

@pytest.mark.parametrize("owner_text", [
    "@bob not sold yet, still open!",
    "I won't end it early, don't worry",
    "аукцион ещё не закрыт :)",
    "@bob this is a closed species, but you can still bid",
    "@anna you're currently the winner!",
    "congrats on the first bid @anna!",
    "thanks! winner gets announced once it ends",
    "AB not taken yet! still open",
])
def test_owner_chatter_does_not_end_auction(owner_text):
    st = parse_bids(thread(("anna", "SB"), (OWNER, owner_text), ("bob", "$15"), ("cara", "$20")),
                    OWNER, sb=10, ab=80, currency="usd")
    assert not st.ended and not st.ab_hit and st.winner is None
    assert st.top.user == "cara" and st.top.amount == 20


@pytest.mark.parametrize("owner_text,winner", [
    ("Sold to @bob! Will send the files after payment", "bob"),
    ("Closed! I will send files soon", None),
    ("Sold for $20! thank you all", None),
    ("Ended! Winner: TBA after payment check", None),
    ("Closed, sold to highest bidder", None),
    ("sold to bob, thanks", "bob"),
])
def test_real_close_even_with_future_words(owner_text, winner):
    st = parse_bids(thread(("anna", "$12"), ("bob", "$20"), (OWNER, owner_text), ("cara", "$25")),
                    OWNER, sb=10, currency="usd")
    assert st.ended and st.winner == winner and st.top.amount == 20


@pytest.mark.parametrize("owner_text", ["Will be closed tomorrow", "Winner will be announced tomorrow",
                                        "закроется через сутки после последней ставки"])
def test_future_close_keeps_auction_open(owner_text):
    st = parse_bids(thread((OWNER, owner_text), ("anna", "$15")), OWNER, sb=10, currency="usd")
    assert not st.ended and st.winner is None and st.top.amount == 15


@pytest.mark.parametrize("text", ["omg wish I could AB 😭", "dang the AB is too much for me 💔",
                                  "can I AB with points instead", "SB! (can't afford AB lol)"])
def test_mentioning_ab_is_not_an_autobuy(text):
    st = parse_bids(thread(("bob", text)), OWNER, sb=10, ab=80, currency="usd")
    assert not st.ab_hit and not st.ended and (st.top is None or st.top.amount < 80)


def test_ab_username_is_not_ab():
    st = parse_bids(thread(("bob", "@ab_cat $20")), OWNER, sb=10, ab=80, currency="usd")
    assert st.top.amount == 20 and st.top.kind == "bid" and not st.ab_hit


@pytest.mark.parametrize("text", ["AB!", "AB please", "I'll AB", "AB $80", "автобай"])
def test_real_ab_claims(text):
    st = parse_bids(thread(("anna", "$15"), ("bob", text)), OWNER, sb=10, ab=80, currency="usd")
    assert st.ab_hit and st.ended and st.winner == "bob"


def test_retracted_ab_reopens_auction():
    st = parse_bids(thread(("anna", "$15"), ("bob", "AB!"), ("bob", "nvm sorry, can't pay right now, retracting"),
                           ("carl", "$20")), OWNER, sb=10, ab=80, currency="usd")
    assert st.top.user == "carl" and st.top.amount == 20
    assert not st.ab_hit and not st.ended and st.winner is None
    assert any("автобай" in n and "снова открыт" in n for n in st.notes)


def test_quoted_previous_bid_and_correction():
    st = parse_bids(thread(("anna", "$20"), ("bob", "@anna bid $20 so I bid $25")), OWNER, sb=10, currency="usd")
    assert st.top.user == "bob" and st.top.amount == 25
    st = parse_bids(thread(("anna", "$40"), ("bob", "$30 wait no, $45")), OWNER, sb=10, currency="usd")
    assert st.top.user == "bob" and st.top.amount == 45


def test_ambiguous_multiple_amounts_are_noted_not_guessed():
    st = parse_bids(thread(("anna", "$15"), ("bob", "$25 or $30")), OWNER, sb=10, currency="usd")
    assert st.top.user == "anna" and any("несколько сумм" in n for n in st.notes)


def test_nvm_with_new_amount_replaces_bid():
    st = parse_bids(thread(("anna", "$15"), ("bob", "$20"), ("anna", "nvm make it $30 then")),
                    OWNER, sb=10, currency="usd")
    assert st.top.user == "anna" and st.top.amount == 30


def test_retraction_after_close_is_ignored():
    st = parse_bids(thread(("anna", "$15"), ("bob", "$20"), (OWNER, "Sold to @bob!"), ("bob", "nvm")),
                    OWNER, sb=10, currency="usd")
    assert st.top.user == "bob" and st.winner == "bob" and any("после закрытия" in n for n in st.notes)


@pytest.mark.parametrize("text,amount", [
    ("$15 - I read the TOS", 15), ("$20, read the terms!", 20), ("$30, can be paid immediately", 30),
])
def test_tos_and_payment_mentions_are_still_bids(text, amount):
    st = parse_bids(thread(("anna", text)), OWNER, sb=10, currency="usd")
    assert st.top is not None and st.top.amount == amount


def test_ab_with_tos_note():
    st = parse_bids(thread(("anna", "AB, TOS read")), OWNER, sb=10, ab=80, currency="usd")
    assert st.ab_hit and st.top.amount == 80


@pytest.mark.parametrize("text,amount", [
    ("SB please!", 10), ("sb por favor", 10), ("I bid 25", 25), ("20 pls", 20),
    ("20 dólares", 20), ("Puja: 20", 20), ("oferto 25 dolares", 25),
])
def test_common_phrasings(text, amount):
    st = parse_bids(thread(("anna", text)), OWNER, sb=10, currency="usd")
    assert st.top is not None and st.top.amount == amount


def test_unparsed_short_numeric_comment_gets_a_note():
    st = parse_bids(thread(("anna", "SB"), ("bob", "20 if possible 2")), OWNER, sb=10, currency="usd")
    assert any("не поняла" in n or "несколько сумм" in n for n in st.notes) or st.top.user == "bob"


def test_find_amounts_dedupes_overlaps():
    from mcp_devian.bids import find_amounts

    assert [(v, c) for _, v, c in find_amounts("$20 or 25$ or 300 pts")] == [(20, "usd"), (25, "usd"), (300, "points")]
    assert find_amount("@kitty123 $25") == (25, "usd")


def test_bidder_records_one_per_user_highest_first():
    from mcp_devian.bids import Comment, bidder_records, parse_bids

    comments = [Comment(str(i), None, u, 1000.0 + i, t) for i, (u, t) in enumerate(
        [("anna", "SB"), ("Kiwi", "$15"), ("anna", "$20"), ("kiwi", "$25"), ("bob", "$26")])]
    state = parse_bids(comments, owner="moth", sb=10, currency="usd")
    assert len(state.bids) == 5  # все ставки на месте
    records = bidder_records(state)
    assert [(r["user"], r["amount"], r["n"]) for r in records] == [("bob", 26, 1), ("kiwi", 25, 2), ("anna", 20, 2)]
    assert all(r["currency"] == "usd" and r["ts"] for r in records)
    assert len(bidder_records(state, limit=2)) == 2


def test_points_with_space_thousands():
    """«1 000 / 1 500 / 2 000 pts» — тысячи через пробел, а не 0 и 500."""
    st = parse_bids(thread(("anna", "1 000 pts"), ("bob", "1 500 pts"), ("cat", "2 000 points")),
                    OWNER, sb=1000, currency="points")
    assert [b.amount for b in st.bids] == [1000, 1500, 2000] and st.top.user == "cat"
    assert find_amount("#2 100$") == (100, "usd")  # лот 2 за $100, не $2100
