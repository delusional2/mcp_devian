import pytest

from mcp_devian import auctions, db, mcp_server, posting
from mcp_devian.config import Config, Toyhouse, save_config
from mcp_devian.db import Item

NOW = 1_800_000_000.0
H = 3600


def comment(i, user, text, minutes):
    return {"comment_id": str(i), "parent_id": None, "user": user, "posted_ts": NOW - 10 * H + minutes * 60,
            "text": text, "depth": 0}


THREAD = [
    comment(1, "moth", "Bid here please ❤️", 0),
    comment(2, "anna", "SB", 5),
    comment(3, "kiwi", "$15", 30),
    comment(4, "anna", "+5", 60),
]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "a.sqlite3")
    db.upsert_item(c, Item(id="da:111", source="deviantart", title="Moth adopt AUCTION [OPEN]",
                           url="https://www.deviantart.com/moth/art/Moth-adopt-111", author="moth", lot_type="auction",
                           status="open", price_usd=10, snippet="SB: $10 MI: $5 AB: $80", own=True,
                           published_ts=NOW - 10 * H), now=NOW - 10 * H)
    db.upsert_item(c, Item(id="da:222", source="deviantart", title="Fox auction", url="https://www.deviantart.com/x/art/Fox-222",
                           author="x", lot_type="auction", status="closed", price_usd=20, published_ts=NOW - 5 * 86400),
                   now=NOW - 5 * 86400)
    c.commit()
    yield c
    c.close()


@pytest.mark.parametrize("text,ab", [
    ("SB: $10 MI: $5 AB: $80", 80), ("AB 2000 pts", 2000), ("autobuy - $150", 150), ("AB: N/A", None),
    ("автобай 60$", 60), ("no ab", None), ("", None),
])
def test_ab_price(text, ab):
    assert auctions.ab_price(text) == ab


def test_analyse_and_save_event(conn):
    item = db.get_item(conn, "da:111")
    state = auctions.analyse(item, THREAD)
    assert state.top.amount == 20 and state.top.user == "anna" and state.n_bids == 3
    event = auctions.save(conn, item, state, len(THREAD), NOW)
    assert event and event.amount == 20
    # Та же ставка второй раз — не новое событие.
    assert auctions.save(conn, item, state, len(THREAD), NOW + 60) is None
    assert "максимум $20 у anna" in auctions.summary_for(conn, item)


def test_to_refresh_picks_hers_and_market_without_final_price(conn):
    ids = [r["id"] for r in auctions.to_refresh(conn, NOW)]
    assert ids == ["da:111", "da:222"]
    assert [r["id"] for r in auctions.to_refresh(conn, NOW, market_sample=0)] == ["da:111"]


class FakeClient:
    def __init__(self, comments):
        self.comments = comments
        self.lookups = []

    def is_connected(self):
        return True

    def deviation_uuid_from_url(self, url, max_pages=10):
        self.lookups.append(url)
        return "uuid-" + url[-3:]

    def deviation_comments(self, uuid, max_comments=500):
        return self.comments


def test_refresh_via_api_reports_new_bids_only_for_her(conn):
    client = FakeClient(THREAD)
    done, events = auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None)
    assert done == 2 and [e.deviation_id for e in events] == ["da:111"]
    assert "Новая ставка $20 от anna" in auctions.describe_event(events[0])


def test_refresh_survives_one_broken_lot(conn):
    class Flaky(FakeClient):
        def deviation_comments(self, uuid, max_comments=500):
            if uuid.endswith("111"):
                raise RuntimeError("api down")
            return self.comments

    done, _ = auctions.refresh_via_api(conn, Config(), Flaky(THREAD), NOW, sleep=lambda s: None)
    assert done == 1


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: False)
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "s.sqlite3"))
    cfg = Config(toyhouse=Toyhouse(mode="chrome"), configured=True)
    save_config(cfg)
    with db.session() as c:
        db.upsert_item(c, Item(id="da:111", source="deviantart", title="Moth adopt AUCTION [OPEN]",
                               url="https://www.deviantart.com/moth/art/Moth-adopt-111", author="moth",
                               lot_type="auction", status="open", price_usd=10, snippet="SB: $10 AB: $80", own=True,
                               published_ts=NOW), now=NOW)


def test_auction_status_via_api(server, monkeypatch):
    monkeypatch.setattr(posting, "da_client", lambda cfg: FakeClient(THREAD))
    out = mcp_server.auction_status("https://www.deviantart.com/moth/art/Moth-adopt-111")
    assert "(API)" in out and "$20" in out and "проверь глазами" in out


def test_auction_status_falls_back_to_her_chrome(server, monkeypatch):
    from mcp_devian import her_chrome

    def no_da(cfg):
        raise RuntimeError("нет client_id")

    monkeypatch.setattr(posting, "da_client", no_da)
    monkeypatch.setattr(her_chrome, "run_deviantart_comments", lambda url, **kw: THREAD, raising=False)
    out = mcp_server.auction_status("https://www.deviantart.com/moth/art/Moth-adopt-111")
    assert "её Chrome" in out and "$20" in out


def test_auction_status_rejects_other_urls(server):
    assert "Нужна ссылка" in mcp_server.auction_status("https://evil.example/art/x-1")


def test_my_auctions_lists_hers(server, monkeypatch):
    monkeypatch.setattr(posting, "da_client", lambda cfg: FakeClient(THREAD))
    monkeypatch.setattr(auctions.time, "sleep", lambda s: None)
    out = mcp_server.my_auctions()
    assert "Moth adopt" in out and "максимум $20" in out


def test_uuid_cached_and_survives_feed_reread(conn):
    client = FakeClient(THREAD)
    auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None, market_sample=0)
    auctions.refresh_via_api(conn, Config(), client, NOW + 60, sleep=lambda s: None, market_sample=0)
    assert len(client.lookups) == 1  # второй раз UUID взят из кэша
    # Лента перечитана — UUID не потерялся.
    db.upsert_item(conn, Item(id="da:111", source="deviantart", title="Moth adopt AUCTION [OPEN]",
                              url="https://www.deviantart.com/moth/art/Moth-adopt-111", author="moth",
                              lot_type="auction", status="open", own=True, extra={"tags": ["moth"]}), now=NOW + 120)
    assert auctions.known_uuid(db.get_item(conn, "da:111")) == "uuid-111"


@pytest.mark.parametrize("text", ["No AB", "AB: ???", "AB: ask", "AB: TBA. SB: $10 MI: $5", "без AB, SB 10$", "AB — "])
def test_ab_price_absent_or_unknown(text):
    assert auctions.ab_price(text) is None


def test_mention_digits_are_not_a_bid():
    from mcp_devian import bids

    assert bids.find_amount("@kitty123 $25") == (25, "usd")
    item_comments = [comment(1, "anna", "$15", 1), comment(2, "bob", "@anna123 $25", 2)]
    from mcp_devian.bids import parse_bids

    state = parse_bids(auctions.to_comments(item_comments), owner="moth", sb=10, currency="usd")
    assert state.top.amount == 25 and state.top.user == "bob"
