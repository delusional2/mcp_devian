import pytest

from mcp_devian import db, market, mcp_server
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.sqlite3")
    yield c
    c.close()


def lot(conn, n, title, lot_type, usd=None, pts=None, status="open", age_days=5, tags=(), own=False,
        seen_at=None, price_text=None):
    item = Item(id=f"da:{n}", source="deviantart", title=title, url=f"https://www.deviantart.com/a/art/x-{n}",
                lot_type=lot_type, status=status, price_usd=usd, price_points=pts, own=own,
                price_text=price_text, published_ts=NOW - age_days * DAY, extra={"tags": list(tags)})
    db.upsert_item(conn, item, now=seen_at or NOW - age_days * DAY)
    return item


@pytest.fixture
def market_db(conn):
    for i, usd in enumerate([8, 10, 12, 15, 15, 20, 25, 40]):
        lot(conn, i, f"Dragon adopt auction #{i}", "auction", usd=usd, tags=["dragon", "adopt"])
    for i, usd in enumerate([20, 25, 30, 35, 45], start=100):
        lot(conn, i, f"Moth flatsale #{i}", "flatsale", usd=usd, tags=["moth"])
    lot(conn, 200, "YCH dragon", "ych", usd=5)                   # не адопт — не считается
    lot(conn, 201, "My dragon adopt", "auction", usd=500, own=True)  # её работа — не рынок
    conn.commit()
    return conn


def test_price_stats():
    s = market.price_stats([10, 20, 30, 40, None, 0])
    assert (s.n, s.median) == (4, 25) and s.low < s.median < s.high
    assert s.position(35) == 0.75
    assert market.price_stats([]).n == 0


def test_forecast_by_keywords(market_db):
    fc = market.forecast(market_db, ["dragon"], "auction", my_usd=30, now=NOW)
    assert fc.comparables == 8 and not fc.widened
    assert fc.auction_usd.median == 15 and fc.auction_usd.n == 8
    assert fc.position_for("auction") == 0.875  # дороже 7 из 8
    assert fc.confidence == "средняя"


def test_position_without_lot_type_is_per_group(market_db):
    # #23: без lot_type её SB не сравнивается с фикс-ценами молча — у каждой группы своя строка.
    fc = market.forecast(market_db, [], None, my_usd=20, now=NOW)
    assert {p.group for p in fc.positions} == {"auction", "fixed"}
    text = market.render(fc)
    assert "аукционы (SB)" in text and "фикс-цены" in text


def test_widening_keeps_real_hits_and_is_honest(conn):
    # #24: 3 дорогих мотылька среди дешёвого рынка — не теряем их и не называем рынок «похожими».
    for i, usd in enumerate([150, 180, 200]):
        lot(conn, i, f"Moth dragon auction {i}", "auction", usd=usd, tags=["moth"])
    for i in range(10, 30):
        lot(conn, i, f"Cat auction {i}", "auction", usd=10, tags=["cat"])
    fc = market.forecast(conn, ["moth"], "auction", my_usd=170, now=NOW)
    assert fc.widened and len(fc.hits) == 3 and fc.comparables == 23 and fc.confidence == "низкая"
    text = market.render(fc, "auction")
    assert "нашлось всего 3" in text and "$150" in text and "$200" in text
    assert "весь рынок аукционов" in text and "лотов на рынке" in text
    assert "похожих" not in text.split("Ограничения")[0].replace("не «похожие»", "")


def test_widen_without_lot_type_says_all_types(market_db):
    fc = market.forecast(market_db, ["kitsune"], None, now=NOW)
    assert fc.widened and fc.comparables == 13 and fc.confidence == "низкая"
    assert "всех типов" in market.render(fc)


def test_generic_keywords_are_ignored(market_db):
    # #27: «adopt», «oc» совпадают со всем рынком — это не «похожие».
    fc = market.forecast(market_db, ["adopt", "oc"], None, now=NOW)
    assert fc.widened and fc.ignored_keywords == ["adopt", "oc"] and fc.keywords == []
    assert "Слишком общие слова" in market.render(fc)


def test_keywords_from_tags_filters_before_slicing():
    tags = ["adoptables", "adoptauction", "oc", "originalcharacter", "digitalart", "adopt", "moth", "dragon"]
    assert market.keywords_from_tags(tags, "Moth dragon adopt [OPEN]") == ["moth", "dragon"]
    assert market.keywords_from_tags(["adopt", "ota"], "Kitsune witch OTA") == ["Kitsune", "witch"]


def test_three_keywords_need_two_hits(conn):
    for i in range(6):
        lot(conn, i, f"Moth dragon auction {i}", "auction", usd=20, tags=["moth", "dragon"])
    for i in range(10, 16):
        lot(conn, i, f"Dragon auction {i}", "auction", usd=99, tags=["dragon"])
    fc = market.forecast(conn, ["moth", "dragon", "witch"], "auction", now=NOW)
    assert not fc.widened and fc.comparables == 6 and fc.auction_usd.median == 20


def test_close_rate_only_counts_decided_and_reseen(conn):
    # #26: «закрылись» — только из лотов с понятным статусом, открытые — только если видели после 3-го дня.
    for i in range(4):
        lot(conn, i, f"Fox auction {i}", "auction", usd=10, age_days=6)
        lot(conn, i, f"Fox auction {i} [SOLD]", "auction", usd=10, status="closed", age_days=6, seen_at=NOW - 4 * DAY)
    lot(conn, 9, "Fox auction 9", "auction", usd=10, age_days=6)
    lot(conn, 9, "Fox auction 9", "auction", usd=10, age_days=6, seen_at=NOW - DAY)        # видели после 3-го дня
    lot(conn, 10, "Fox auction 10", "auction", usd=10, age_days=6)                          # выпал из ленты
    lot(conn, 11, "Fox auction 11", "auction", usd=10, status="unknown", age_days=6)        # без статуса
    fc = market.forecast(conn, ["fox"], "auction", now=NOW)
    assert fc.closed_share == 0.8 and fc.closed_share_n == 5
    assert fc.stale == 1 and fc.unknown_status == 1
    assert fc.open_now == 2  # моложе недели и со статусом open: 9 и 10
    text = market.render(fc, "auction")
    assert "1 без статуса" in text and "1 выпали из ленты" in text


def test_open_now_counts_only_open(conn):
    lot(conn, 1, "Fox auction", "auction", usd=10, age_days=1)
    lot(conn, 2, "Fox auction [SOLD]", "auction", usd=10, status="closed", age_days=1)
    lot(conn, 3, "Fox auction", "auction", usd=10, status="unknown", age_days=1)
    assert market.forecast(conn, [], "auction", now=NOW).open_now == 1


def test_speed_is_censoring_aware(conn):
    # #25: два закрылись быстро, три медленных ещё открыты — «закрылись в первые 3 дня» = 2/5, а не 100%.
    for i in range(2):
        lot(conn, i, f"Fox {i}", "auction", usd=10, age_days=10)
        lot(conn, i, f"Fox {i} SOLD", "auction", usd=10, status="closed", age_days=10, seen_at=NOW - 9 * DAY)
    for i in range(2, 5):
        lot(conn, i, f"Fox {i}", "auction", usd=10, age_days=10)
        lot(conn, i, f"Fox {i}", "auction", usd=10, age_days=10, seen_at=NOW - DAY)
    fc = market.forecast(conn, [], "auction", now=NOW)
    assert fc.closed_within_horizon == pytest.approx(0.4) and fc.horizon_n == 5


def test_lots_first_seen_closed_do_not_count_for_speed(conn):
    for i in range(4):
        lot(conn, i, f"Old {i} SOLD", "auction", usd=10, status="closed", age_days=10)
    fc = market.forecast(conn, [], "auction", now=NOW)
    assert fc.closed_within_horizon is None and fc.closed_share == 1.0


def test_raffles_and_ota_are_separate_groups(conn):
    # #29, #30: билеты лотерей не тянут вниз фикс-цены, у OTA свои честные подписи.
    for i, usd in enumerate([40, 45, 50, 55, 60]):
        lot(conn, i, f"Moth flatsale {i}", "flatsale", usd=usd)
    for i in range(10, 16):
        lot(conn, i, f"Moth raffle {i}", "raffle", usd=2)
    lot(conn, 20, "Moth OTA", "ota")
    fc = market.forecast(conn, [], "flatsale", my_usd=50, now=NOW)
    assert fc.fixed_usd.median == 50 and fc.position_for("fixed") == 0.4
    text = market.render(market.forecast(conn, [], "ota", now=NOW), "ota")
    assert "сравнить её цену не с чем" in text


def test_confidence_from_price_bucket(conn):
    # #31: 20 лотов, но с ценой только 2 — уверенность низкая.
    for i in range(18):
        lot(conn, i, f"Fox auction {i}", "auction")
    lot(conn, 50, "Fox auction a", "auction", usd=10)
    lot(conn, 51, "Fox auction b", "auction", usd=12)
    fc = market.forecast(conn, [], "auction", my_usd=11, now=NOW)
    assert fc.confidence == "низкая"
    assert "цен очень мало" in market.render(fc, "auction")


def test_small_samples_listed_not_quartiled(conn):
    # #32: при n<5 показываем сами цены, без «обычно».
    for i, usd in enumerate([5, 10, 200]):
        lot(conn, i, f"Fox auction {i}", "auction", usd=usd)
    text = market.render(market.forecast(conn, [], "auction", now=NOW), "auction")
    assert "$5, $10, $200 (всего 3)" in text and "обычно" not in text


def test_other_currencies_reported(conn):
    # #28: лоты в €/£ не попадают в $ и об этом сказано.
    lot(conn, 1, "Fox auction", "auction", usd=10)
    lot(conn, 2, "Fox auction EU", "auction", price_text="SB €30")
    fc = market.forecast(conn, [], "auction", now=NOW)
    assert fc.auction_usd.n == 1 and fc.other_currency == 1
    assert "1 лотов с ценой в €/£" in market.render(fc, "auction")


def test_render_is_gentle_and_honest(market_db):
    text = market.render(market.forecast(market_db, ["dragon"], "auction", my_usd=30, now=NOW), "auction")
    assert "не всегда «продан»" in text and "Уверенность: средняя" in text
    assert "обычно $" in text and "выше, чем у 88% похожих" in text


def test_gentle_text_is_honest():
    # #33: бережно, но цифры не трогать, не хвалить невиденное, про высокую цену — прямо.
    g = market.GENTLE
    assert "не округляй" in g and "картинку ты не видишь" in g and "скажи это прямо" in g
    assert "рынок такое берёт" not in g


def test_my_works(market_db):
    text = market.my_works(market_db, now=NOW)
    assert "My dragon adopt" in text and "Dragon adopt auction" not in text


def test_my_works_reopened_lot_has_no_close_time(conn):
    # #34: закрытие считаем только у лотов, которые сейчас закрыты.
    lot(conn, 1, "My moth adopt", "auction", usd=10, own=True, age_days=3, seen_at=NOW - 3 * DAY)
    conn.execute("UPDATE items SET closed_ts = ?, status = 'open' WHERE id = 'da:1'", (NOW - DAY,))
    assert "закрылся за" not in market.my_works(conn, now=NOW)


def test_my_works_empty(conn):
    assert "my_da_username" in market.my_works(conn, now=NOW)


def test_closed_ts_kept_once(conn):
    lot(conn, 1, "Cat adopt", "auction", usd=5, age_days=3, seen_at=NOW - 3 * DAY)
    lot(conn, 1, "Cat adopt SOLD", "auction", usd=5, status="closed", age_days=3, seen_at=NOW - DAY)
    lot(conn, 1, "Cat adopt SOLD", "auction", usd=5, status="closed", age_days=3, seen_at=NOW)
    assert db.get_item(conn, "da:1")["closed_ts"] == NOW - DAY


def test_market_forecast_tool_without_data(tmp_path, monkeypatch):
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "e.sqlite3"))
    out = mcp_server.market_forecast(keywords=["dragon"])
    assert "мало данных" in out and "бережно" in out
