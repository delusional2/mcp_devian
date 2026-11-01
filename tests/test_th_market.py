import pytest

from mcp_devian import db, sales
from mcp_devian import th_market as tm

NOW = 1_800_000_000.0
DAY = 86400


# ---------- суммы ----------

@pytest.mark.parametrize("text, expected", [
    ("budget $30", [(None, 30, "usd", "")]),
    ("Budget: 30$", [(None, 30, "usd", "")]),
    ("up to 50 USD", [(None, 50, "usd", "")]),
    ("can pay 2k pts", [(None, 2000, "points", "")]),
    ("could spend around 1,500 points", [(1500, 1500, "points", "")]),
    ("budget $1.5k", [(None, 1500, "usd", "")]),
    ("$20-40 each", [(20, 40, "usd", "each")]),
    ("$20 - $40 per character", [(20, 40, "usd", "each")]),
    ("20–40 USD", [(20, 40, "usd", "")]),
    ("от 20 до 40 долларов", [(20, 40, "usd", "")]),
    ("бюджет до 1500 руб", [(None, 1500, "rub", "")]),
    ("могу заплатить 30$", [(None, 30, "usd", "")]),
    ("готова отдать 2к поинтов", [(None, 2000, "points", "")]),
    ("around €15", [(15, 15, "eur", "")]),
    ("max £25", [(None, 25, "gbp", "")]),
    ("$20+", [(20, None, "usd", "")]),
    ("at least $20", [(20, None, "usd", "")]),
    ("$30 tops", [(None, 30, "usd", "")]),
    ("2 characters, $15 max", [(None, 15, "usd", "")]),
    ("CAD $20 max", [(None, 20, "cad", "")]),
    ("budget 25 AUD", [(None, 25, "aud", "")]),
    ("USD only, budget 30", [(None, 30, "usd", "")]),
    ("pts only! can pay 800", [(None, 800, "points", "")]),
    ("can pay $50 for the set", [(None, 50, "usd", "set")]),
    ("@kitty123 up to $10", [(None, 10, "usd", "")]),
    ("I paid $40 for her, budget up to $25", [(None, 25, "usd", "")]),
    ("she's worth $60 but I can pay $30", [(None, 30, "usd", "")]),
    ("budget 30", []),                         # без валюты и без «USD only» — не знаем, что это
    ("looking for 2-3 dragons, 24h", []),
    ("trades only", []),
    ("#3 in my wishlist https://toyhou.se/12345.dragon", []),
])
def test_lf_budgets(text, expected):
    st = tm.parse_stated(text, "lf")
    assert [(m.low, m.high, m.currency, m.per) for m in st.amounts] == expected


@pytest.mark.parametrize("text, flags", [
    ("trades only!", {"trades_only"}),
    ("Only trades, sorry", {"trades_only"}),
    ("art only", {"art_only"}),
    ("USD only", {"usd_only"}),
    ("paypal only pls", {"usd_only"}),
    ("pts only", {"points_only"}),
    ("no budget, just looking", {"no_budget"}),
    ("budget: none", {"no_budget"}),
    ("no budget limit!", {"flexible"}),
    ("budget is flexible", {"flexible"}),
    ("$40 OBO", {"obo"}),
    ("только обмен", {"trades_only"}),
    ("без бюджета", {"no_budget"}),
])
def test_flags(text, flags):
    assert tm.parse_stated(text, "lf").flags == flags


def test_sale_side_takes_plain_prices_and_sets():
    st = tm.parse_stated("Selling 3 dragons $15 each or $40 for the set OBO", "sale")
    assert [(m.low, m.per) for m in st.amounts] == [(15, "each"), (40, "set")]
    assert st.cap("usd") == 15 and "obo" in st.flags  # набор в цену «за перса» не идёт


def test_currencies_never_mixed():
    st = tm.parse_stated("budget $30 or 3000 pts or €25", "lf")
    assert st.currencies() == ["eur", "points", "usd"]
    assert (st.cap("usd"), st.cap("points"), st.cap("eur")) == (30, 3000, 25)
    assert all(m.low is None for m in st.amounts)  # все три — потолок бюджета, не «от»


def test_quartiles_only_on_big_samples():
    assert (tm.stats([35, 40, 45, 60]).low, tm.stats([35, 40, 45, 60]).high) == (35, 60)
    big = tm.stats([10, 20, 30, 40, 50, 60, 70, 80])
    assert (big.low, big.high) == (27.5, 62.5)


def test_title_offer_segment_is_budget():
    st = tm.parse_stated("", "lf", title="LF: dragons | O: $5-30")
    assert [(m.low, m.high, m.currency) for m in st.amounts] == [(5, 30, "usd")]
    st = tm.parse_stated("", "lf", title="LF dragons O: $25")
    assert st.cap("usd") == 25


def test_parse_price():
    assert tm.parse_price("$40").low == 40
    assert tm.parse_price("1500 pts").currency == "points"
    assert (tm.parse_price("$30-40").low, tm.parse_price("$30-40").high) == (30, 40)
    assert tm.parse_price("40") is None and tm.parse_price("") is None


def test_stats_small_and_quartiles():
    s = tm.stats([10, 30])
    assert (s.n, s.low, s.high, s.median) == (2, 10, 30, 20)
    s = tm.stats([10, 20, 30, 40, 100])
    assert (s.low, s.high, s.median) == (10, 100, 30)
    # Та же шкала, что в advice:tips и forecast (evidence.level): меньше 8 — «нет вывода».
    assert tm.confidence(3) == "нет вывода" and tm.confidence(10) == "низкая" and tm.confidence(15) == "средняя"
    assert tm.confidence(40) == "высокая"


# ---------- база ----------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.sqlite3")
    yield c
    c.close()


_n = iter(range(10_000, 99_999))


def add(conn, title, snippet=None, side="lf", author=None, days_ago=1.0, seen_ago=None, price_usd=None, pays=None):
    i = next(_n)
    item = db.Item(id=f"th:{i}", source="toyhouse", title=title, url=f"https://toyhou.se/~forums/13.adoption-center/{i}.x",
                   author=author or f"user{i}", snippet=snippet, activity_ts=NOW - days_ago * DAY,
                   wants_characters=side == "lf", sells_characters=side == "sale", match_kind=side if side else None,
                   price_usd=price_usd, pays=pays)
    db.upsert_item(conn, item, now=NOW - (seen_ago if seen_ago is not None else days_ago) * DAY)
    conn.commit()
    return item.id


def _market(conn):
    add(conn, "LF dragons!", "Budget up to $40, can also do art", author="a1")
    add(conn, "Seeking wyvern characters", "can pay $25-35", author="a2")
    add(conn, "LF: dragon OCs | O: $$", "budget $60, USD only", author="a3")
    add(conn, "LF dragon", "trades only sorry", author="a4", pays="trades")
    add(conn, "LF dragons again", "up to $45", author="a1")      # тот же человек — второй тред
    add(conn, "LF cats", "up to 2k pts", author="c1")
    add(conn, "LF cats", "budget 1500 points", author="c2")
    add(conn, "Dragon adopts for sale $20", side="sale", price_usd=20, author="s1")
    add(conn, "Selling dragon OCs $15-30", side="sale", author="s2")
    add(conn, "Cat purge", side="sale", author="s3")


def test_demand_supply(conn):
    _market(conn)
    r = tm.demand_supply(conn, days=30, now=NOW)
    seg = {s.key: s for s in r.segments}
    d = seg["dragon"]
    assert (d.lf, d.lf_people, d.sale) == (5, 4, 2) and d.ratio == 2.5
    b = d.budgets["usd"]
    assert (b.n, b.people, b.min, b.max, b.median) == (3, 3, 35, 60, 45)  # a1 — один человек, берём его $45
    assert d.asks["usd"].n == 2 and d.asks["usd"].min == 15
    assert d.flags["trades_only"] == 1
    assert "points" in seg["cat"].budgets and "usd" not in seg["cat"].budgets
    assert (r.n_lf, r.n_sale, r.n_lf_text) == (7, 3, 7)


def test_render_market_is_compact_and_honest(conn):
    _market(conn)
    text = tm.render_market(tm.demand_supply(conn, days=30, now=NOW))
    assert "Toyhouse за 30 дн" in text and "LF n=7" in text and "продажи n=3" in text
    assert "• dragon: ищут 4 человека (LF 5) / продают 2; спрос/предл. 2.5" in text
    assert "бюджеты $35–$60, медиана $45 (n=3)" in text and "Уверенность: нет вывода" in text
    assert "намерения" in text and "Реальных цен продаж на TH у нас нет" in text
    assert len(text) < 1500


def test_render_market_empty(conn):
    assert "Данных нет" in tm.render_market(tm.demand_supply(conn, now=NOW))


def test_no_lookahead_and_period(conn):
    add(conn, "LF dragons", "up to $40", days_ago=1)
    add(conn, "LF dragons", "up to $90", days_ago=-1, seen_ago=-1)   # появится после now
    add(conn, "LF dragons", "up to $99", days_ago=1, seen_ago=-0.5)  # мы узнали о нём позже now
    add(conn, "LF dragons", "up to $10", days_ago=45)                # старше периода
    threads = tm.load_threads(conn, days=30, now=NOW)
    assert [t.stated.cap("usd") for t in threads] == [40]


def test_her_own_threads_excluded(conn):
    add(conn, "LF dragons", "up to $40", author="MothArt")
    add(conn, "LF dragons", "up to $30", author="x")
    assert [t.author for t in tm.load_threads(conn, now=NOW, my_username="mothart")] == ["x"]


def test_realized_sales_shown(conn):
    _market(conn)
    sales.add_sale(conn, title="Ember", amount=35, currency="usd", platform="toyhouse", format="auction",
                   status="paid", now=NOW - DAY)
    sales.add_sale(conn, title="DA one", amount=99, currency="usd", platform="deviantart", format="auction",
                   status="paid", now=NOW - DAY)
    text = tm.render_market(tm.demand_supply(conn, days=30, now=NOW))
    assert "Твои оплаченные продажи на TH за период: $35, медиана $35 (n=1)" in text


def test_realized_sales_skip_unknown_currency(conn):
    """4000 без валюты не превращается в «$4000»: медиана — только по продажам с валютой."""
    _market(conn)
    sales.add_sale(conn, title="Ember", amount=40, currency="usd", platform="toyhouse", format="auction",
                   status="paid", now=NOW - DAY)
    sales.add_sale(conn, title="Ash", amount=4000, currency=None, platform="toyhouse", format="auction",
                   status="paid", now=NOW - DAY)
    text = tm.render_market(tm.demand_supply(conn, days=30, now=NOW))
    assert "$40, медиана $40 (n=1)" in text and "$2020" not in text
    assert "Ещё 1 оплаченных без валюты — в цены не вошли" in text


# ---------- её персы ----------

def test_character_demand_phrase(conn):
    _market(conn)
    add(conn, "LF anything but dragons", "no dragons please, up to $80", author="z9")
    chars = [{"name": "Ember", "url": "https://toyhou.se/1.ember", "price": "$40", "tags": ["dragon", "fire"]},
             {"name": "Mochi", "price": "1800 pts", "tags": ["cat"]},
             {"name": "Pip", "price": "40", "tags": ["moth"]}]
    res = {d.name: d for d in tm.character_demand(conn, chars, days=30, now=NOW)}
    e = res["Ember"]
    assert (e.threads, e.people, e.numeric, e.fits) == (5, 4, 3, 2)
    m = res["Mochi"]
    assert (m.numeric, m.fits) == (2, 1)
    text = tm.render_character_demand(list(res.values()))
    assert "Ember: под этого перса ищут 4 человека с бюджетом $35–$60 (n=3 с суммой, LF 5" in text
    assert "твоя цена $40 — укладываются 2 из 3" in text and "только обмен 1" in text
    assert "Mochi: под этого перса ищут 2 человека с бюджетом 1,500 pts–2,000 pts" in text
    assert "Pip: под этого перса за 30 дн LF-тредов не нашлось" in text
    assert "намерения" in text


def test_character_without_species_uses_tag_match(conn):
    add(conn, "LF pastel goth babies", "budget $20", author="p1")
    d = tm.character_demand(conn, [{"name": "Lilac", "price": "$15", "tags": ["pastel goth"]}], now=NOW)[0]
    assert d.people == 1 and d.fits == 1


def test_character_price_without_currency(conn):
    add(conn, "LF dragons", "up to $40", author="a")
    text = tm.render_character_demand(tm.character_demand(conn, [{"name": "X", "price": "40", "tags": ["dragon"]}],
                                                           now=NOW))
    assert "цену «40» не поняла" in text


def test_no_characters():
    assert "Персов на продажу нет" in tm.render_character_demand([])


# ---------- регрессии по проверке ----------

@pytest.mark.parametrize("text", [
    "LF dragon, USD only. I have 3 ocs to trade too",
    "USD only! must be under 18 friendly",
    "usd only. offering 5 characters",
    "USD only. Up to 2 characters",
    "USD only. budget up to 3 weeks of comms",
    "max 2 per person, USD only",
    "pts only, over 18 please",
])
def test_bare_numbers_that_are_not_money(text):
    assert tm.parse_stated(text, "lf").amounts == []


def test_bare_number_with_budget_cue_still_counts():
    st = tm.parse_stated("USD only! must be under 18 friendly. budget 40", "lf")
    assert [(m.high, m.currency) for m in st.amounts] == [(40, "usd")]


@pytest.mark.parametrize("text, expected", [
    ("$70 AUD", [(70, None, "aud")]),
    ("$20 - 40 CAD", [(20, 40, "cad")]),
    ("$15 NZD max", [(15, None, "nzd")]),
    ("$20 USD", [(20, None, "usd")]),
    ("1 500 pts", [(1500, None, "points")]),
    ("2 300", []),  # без валюты — не «2300»
])
def test_find_money_currency_after_dollar_sign(text, expected):
    assert [(m.low, m.high, m.currency) for m in tm.find_money(text) if m.currency] == expected


def test_other_dollars_cue_before_symbol():
    st = tm.parse_stated("can pay CAD $40", "lf")
    assert [(m.high, m.currency) for m in st.amounts] == [(40, "cad")] and st.unclear == 0


def test_for_a_set_is_set_price():
    st = tm.parse_stated("budget up to $100 for a set of 3", "lf")
    assert [m.per for m in st.amounts] == ["set"] and st.cap("usd") is None


def test_age_number_does_not_spoil_character_fit(conn):
    add(conn, "LF dragon", "USD only, must be under 18 friendly", author="k1")
    add(conn, "LF dragon", "USD only, budget 50", author="k2")
    [d] = tm.character_demand(conn, [{"name": "Ember", "price": "$40", "tags": ["dragon"]}], now=NOW)
    assert (d.people, d.numeric, d.fits) == (2, 1, 1)
