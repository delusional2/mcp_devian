import json

import pytest

from mcp_devian import auctions, da_api, db, mcp_server, posting
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
    seen = NOW - 9 * H + 600  # через 10 минут после ставки anna
    event = auctions.save(conn, item, state, len(THREAD), seen)
    assert event and event.amount == 20
    # Та же ставка второй раз — не новое событие.
    assert auctions.save(conn, item, state, len(THREAD), seen + 60) is None
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
    done, events = auctions.refresh_via_api(conn, Config(), client, NOW - 9 * H + 600, sleep=lambda s: None)
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


# --- 0.3.1: автобай, уведомления, фон ---

@pytest.mark.parametrize("text,currency,ab", [
    ("SB: $10, No AB, MI: $1", None, None),
    ("AB: ??? SB: $5 MI: $1", None, None),
    ("AB: ask me | SB: $10", None, None),
    ("without AB! SB $10", None, None),
    ("AB: $25 or 3000 pts", "points", 3000),
    ("AB: $25 or 3000 pts", "usd", 25),
    ("SB: 500 pts / AB: $20", "points", None),
    ("SB: 500 pts / AB: $20", None, 20),
    ("SB 100 pts (AB $5)", "points", None),
    ("AB: 80 | SB: $10", "usd", 80),
    ("AB $80 MI $5", "usd", 80),
])
def test_ab_price_label_and_currency(text, currency, ab):
    assert auctions.ab_price(text, currency) == ab


def _points_lot(conn, snippet):
    db.upsert_item(conn, Item(id="da:333", source="deviantart", title="Points auction",
                              url="https://www.deviantart.com/moth/art/Points-333", author="moth", lot_type="auction",
                              status="open", price_points=500, snippet=snippet, own=True,
                              published_ts=NOW - 10 * H), now=NOW - 10 * H)
    return db.get_item(conn, "da:333")


def test_dollar_ab_on_points_lot_is_not_compared(conn):
    item = _points_lot(conn, "SB: 500 pts / AB: $20")
    state = auctions.analyse(item, [comment(1, "anna", "500 pts", 1), comment(2, "bob", "600 pts", 2)])
    assert not state.ab_hit and not state.ended and state.top.user == "bob" and state.top.amount == 600
    assert any("автобай в другой валюте ($20)" in n for n in state.notes)


def test_points_ab_picked_from_mixed(conn):
    item = _points_lot(conn, "SB 500 pts, AB: $25 or 3000 pts")
    state = auctions.analyse(item, [comment(1, "anna", "3000 pts", 1)])
    assert state.ab_hit and state.winner == "anna"


def test_bid_is_notified_once_even_if_a_read_misses_it(conn):
    item = db.get_item(conn, "da:111")
    full = [comment(1, "anna", "SB", 5), comment(2, "bob", "$20", 30)]
    seen = NOW - 10 * H + 35 * 60
    results = []
    for i, thread in enumerate([full, full[:1], full, full[:1], full]):
        event = auctions.save(conn, item, auctions.analyse(item, thread), len(thread), seen + i * 60)
        results.append(event.amount if event else None)
    assert results == [20, None, None, None, None]


def test_new_bid_after_retraction_is_notified(conn):
    item = db.get_item(conn, "da:111")
    seen = NOW - 10 * H + 35 * 60
    first = [comment(1, "anna", "SB", 5), comment(2, "bob", "$20", 30)]
    assert auctions.save(conn, item, auctions.analyse(item, first), 2, seen).amount == 20
    retracted = [comment(1, "anna", "SB", 5), comment(2, "bob", "$20", 30),
                 comment(3, "bob", "retract my bid", 40), comment(4, "kiwi", "$18", 50)]
    event = auctions.save(conn, item, auctions.analyse(item, retracted), 4, seen + 20 * 60)
    assert event and event.amount == 18 and event.user == "kiwi"


def test_first_read_old_bids_are_a_silent_baseline(conn):
    item = db.get_item(conn, "da:111")
    state = auctions.analyse(item, THREAD)  # последняя ставка 9 часов назад
    assert auctions.save(conn, item, state, len(THREAD), NOW) is None
    assert "максимум $20 у anna" in auctions.summary_for(conn, item)
    newer = [*THREAD, comment(5, "kiwi", "$25", 9 * 60 + 50)]
    event = auctions.save(conn, item, auctions.analyse(item, newer), len(newer), NOW)
    assert event and event.amount == 25 and event.user == "kiwi"


def test_first_read_fresh_bid_is_notified(conn):
    item = db.get_item(conn, "da:111")
    thread = [comment(1, "anna", "$12", 10 * 60 - 30)]  # 30 минут назад
    assert auctions.save(conn, item, auctions.analyse(item, thread), 1, NOW).amount == 12


class Lookups(FakeClient):
    """UUID находится только для перечисленных лотов, комментарии считаются."""

    def __init__(self, comments, found=None, fail=None):
        super().__init__(comments)
        self.found, self.fail, self.reads = found, fail or {}, []

    def deviation_uuid_from_url(self, url, max_pages=10):
        self.lookups.append(url[-3:])
        return None if self.found is not None and url[-3:] not in self.found else "uuid-" + url[-3:]

    def deviation_comments(self, uuid, max_comments=500):
        self.reads.append(uuid[-3:])
        if uuid[-3:] in self.fail:
            raise self.fail[uuid[-3:]]
        return self.comments


def test_uuid_miss_is_not_retried_every_run(conn):
    client = Lookups(THREAD, found=set())
    auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None)
    assert sorted(client.lookups) == ["111", "222"]
    auctions.refresh_via_api(conn, Config(), client, NOW + 1800, sleep=lambda s: None)
    assert len(client.lookups) == 2  # промахи помним
    # Свой лот — снова через час, чужой — через сутки.
    auctions.refresh_via_api(conn, Config(), client, NOW + 2 * H, sleep=lambda s: None)
    assert client.lookups[2:] == ["111"]
    client.found = {"111", "222"}
    auctions.refresh_via_api(conn, Config(), client, NOW + 26 * H, sleep=lambda s: None)
    assert "222" in client.lookups[3:]


def _market_lot(conn, n, status, age_days, seen_at):
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=f"Lot {n}",
                              url=f"https://www.deviantart.com/x/art/Lot-{n}", author="x", lot_type="auction",
                              status=status, price_usd=10, published_ts=NOW - age_days * 86400), now=seen_at)


def test_closed_market_lot_is_reread_once_and_unread_go_first(conn):
    client = Lookups(THREAD)
    auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None, own_limit=0)
    assert client.reads == ["222"]  # закрытый, ни разу не читали
    auctions.refresh_via_api(conn, Config(), client, NOW + 60, sleep=lambda s: None, own_limit=0)
    assert client.reads == ["222"]  # уже прочитан после закрытия — хватит
    # Открытый лот прочитан, потом закрылся — перечитываем ровно один раз.
    _market_lot(conn, 444, "open", 4, NOW - 4 * 86400)
    auctions.refresh_via_api(conn, Config(), client, NOW + 120, sleep=lambda s: None, own_limit=0)
    _market_lot(conn, 444, "closed", 4, NOW + 180)
    _market_lot(conn, 555, "open", 3, NOW + 180)
    client.reads.clear()
    auctions.refresh_via_api(conn, Config(), client, NOW + 240, sleep=lambda s: None, own_limit=0, market_sample=1)
    assert client.reads == ["555"]  # нечитанные — вперёд
    auctions.refresh_via_api(conn, Config(), client, NOW + 300, sleep=lambda s: None, own_limit=0)
    auctions.refresh_via_api(conn, Config(), client, NOW + 360, sleep=lambda s: None, own_limit=0)
    assert client.reads == ["555", "444"]


def _three_own_lots(conn):
    for n in ("112", "113"):
        db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=f"Mine {n}",
                                  url=f"https://www.deviantart.com/moth/art/Mine-{n}", author="moth",
                                  lot_type="auction", status="open", price_usd=10, own=True,
                                  published_ts=NOW - 10 * H - int(n) * 60), now=NOW - 10 * H)


@pytest.mark.parametrize("error,stopped", [
    (da_api.RateLimited("429"), "rate_limited"),
    (da_api.NotConnected("revoked"), "not_connected"),
])
def test_refresh_stops_on_rate_limit_or_disconnect(conn, error, stopped):
    _three_own_lots(conn)
    client = Lookups(THREAD, fail={"112": error})
    stats: dict = {}
    done, _ = auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None, market_sample=0, stats=stats)
    assert client.reads == ["111", "112"] and done == 1 and stats["stopped"] == stopped


def test_refresh_stops_after_errors_in_a_row(conn):
    _three_own_lots(conn)
    boom = RuntimeError("network down")
    client = Lookups(THREAD, fail={"111": boom, "112": boom, "113": boom, "222": boom})
    stats: dict = {}
    auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None, stats=stats)
    assert len(client.reads) == auctions.MAX_FAILS_IN_ROW and stats["stopped"] == "errors"


def test_refresh_respects_deadline(conn):
    _three_own_lots(conn)
    ticks = iter([0, 10, 31, 40])
    client = Lookups(THREAD)
    stats: dict = {}
    done, _ = auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None, market_sample=0,
                                       deadline=30, clock=lambda: next(ticks), stats=stats)
    assert done == 2 and stats["stopped"] == "time"


# --- monitor.refresh_auctions: пауза после 429, частота, отключение ---

@pytest.fixture
def notices(monkeypatch):
    from mcp_devian import digest

    sent: list = []
    monkeypatch.setattr(auctions.time, "sleep", lambda s: None)
    monkeypatch.setattr(digest, "notify_macos", lambda title, msg: sent.append((title, msg)) or True)
    monkeypatch.setattr(digest, "send_telegram", lambda chunks: sent.append(("tg", chunks)) and None)
    return sent


def test_refresh_auctions_backs_off_after_429(conn, monkeypatch, notices):
    from mcp_devian import monitor

    client = Lookups(THREAD, fail={"111": da_api.RateLimited("429")})
    monkeypatch.setattr(posting, "da_client", lambda cfg: client)
    lines, _ = monitor.refresh_auctions(conn, Config(), NOW)
    assert any("притормозить" in line for line in lines) and client.reads == ["111"]
    assert db.last_run(conn, auctions.RUN_SOURCE)["last_error"].startswith("429")
    lines, _ = monitor.refresh_auctions(conn, Config(), NOW + 10 * 60)
    assert "обновлю через 21 мин" in lines[0] and client.reads == ["111"]
    client.fail = {}
    lines, _ = monitor.refresh_auctions(conn, Config(), NOW + 31 * 60)
    assert "ставки: обновлено аукционов 2" in lines


def test_refresh_auctions_not_twice_in_five_minutes(conn, monkeypatch, notices):
    from mcp_devian import monitor

    client = Lookups(THREAD)
    monkeypatch.setattr(posting, "da_client", lambda cfg: client)
    monitor.refresh_auctions(conn, Config(), NOW)
    reads = len(client.reads)
    lines, events = monitor.refresh_auctions(conn, Config(), NOW + 120)
    assert lines == ["ставки: обновлено 2 мин назад, данные свежие"] and events == []
    assert len(client.reads) == reads


def test_refresh_auctions_says_once_that_da_disconnected(conn, monkeypatch, notices):
    from mcp_devian import monitor

    class Gone(FakeClient):
        def is_connected(self):
            return False

    monkeypatch.setattr(posting, "da_client", lambda cfg: Gone([]))
    assert monitor.refresh_auctions(conn, Config(), NOW) == ([], [])  # никогда не подключали — молчим
    db.record_run(conn, auctions.RUN_SOURCE, None, NOW)  # раньше работало
    lines, _ = monitor.refresh_auctions(conn, Config(), NOW + 3600)
    assert "подключи DeviantArt заново" in lines[0] and len(notices) == 1
    lines, _ = monitor.refresh_auctions(conn, Config(), NOW + 7200)
    assert "подключи DeviantArt заново" in lines[0] and len(notices) == 1  # уведомление одно


def test_refresh_auctions_token_revoked_mid_pass(conn, monkeypatch, notices):
    from mcp_devian import monitor

    client = Lookups(THREAD, fail={"111": da_api.NotConnected("revoked")})
    monkeypatch.setattr(posting, "da_client", lambda cfg: client)
    lines, _ = monitor.refresh_auctions(conn, Config(), NOW)
    assert any("подключи DeviantArt заново" in line for line in lines) and len(notices) == 1


def _event(n):
    return auctions.BidEvent(f"da:{n}", f"Lot {n}", "u", 10 + n, "usd", "anna")


def test_notify_bids_counts_all_and_returns_telegram_error(monkeypatch, notices):
    from mcp_devian import digest
    from mcp_devian.config import Notify

    monkeypatch.setattr(digest, "send_telegram", lambda chunks: notices.append(("tg", chunks)) or "нет токена")
    err = digest.notify_bids(Config(notify=Notify(macos=True, telegram=True)), [_event(i) for i in range(7)])
    title, msg = notices[0]
    assert title == "Новых ставок: 7" and err == "нет токена"
    tg = notices[1][1][0]
    assert tg.startswith("Новых ставок: 7") and "и ещё 2" in tg and tg.count("Новая ставка") == 5
    assert digest.notify_bids(Config(), []) is None


# --- итоги рынка без перекоса: дочитывание на 7-й день, пачки UUID, пустые лоты, предел поисков ---

def test_read_age_and_bidders_stored(conn):
    item = db.get_item(conn, "da:111")
    auctions.save(conn, item, auctions.analyse(item, THREAD), len(THREAD), NOW)
    extra = json.loads(conn.execute("SELECT extra FROM auctions WHERE deviation_id = 'da:111'").fetchone()["extra"])
    assert extra["read_age_days"] == pytest.approx(10 / 24, abs=0.01)
    assert extra["bidders"] == [{"user": "anna", "amount": 20, "currency": "usd", "ts": NOW - 10 * H + 60 * 60, "n": 2},
                                {"user": "kiwi", "amount": 15, "currency": "usd", "ts": NOW - 10 * H + 30 * 60, "n": 1}]
    assert "zero_by_stats" not in extra


def test_market_lot_read_at_day_two_without_bids_is_reread_at_day_seven(conn):
    _market_lot(conn, 777, "open", 2.2, NOW - 2.2 * 86400)
    client = Lookups([])
    auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None, own_limit=0)
    assert "777" in client.reads
    client.reads.clear()
    # Через 3 дня (лоту 5.2 дня) ещё рано — итог не известен, но и читать незачем.
    auctions.refresh_via_api(conn, Config(), client, NOW + 3 * 86400, sleep=lambda s: None, own_limit=0)
    assert "777" not in client.reads
    # На 7+ день — дочитываем, хотя в названии всё ещё [OPEN].
    auctions.refresh_via_api(conn, Config(), client, NOW + 5 * 86400, sleep=lambda s: None, own_limit=0)
    assert "777" in client.reads
    row = conn.execute("SELECT extra FROM auctions WHERE deviation_id = 'da:777'").fetchone()
    assert json.loads(row["extra"])["read_age_days"] >= 7
    client.reads.clear()
    auctions.refresh_via_api(conn, Config(), client, NOW + 6 * 86400, sleep=lambda s: None, own_limit=0)
    assert "777" not in client.reads  # дочитан в зрелом возрасте — хватит


def test_lot_still_open_at_day_seven_is_reread_after_day_fourteen(conn):
    _market_lot(conn, 779, "open", 7.5, NOW)
    conn.execute("INSERT INTO auctions (deviation_id, n_bids, ended, updated_ts, extra) VALUES "
                 "('da:779', 0, 0, ?, '{\"read_age_days\": 7.2}')", (NOW - 0.3 * 86400,))
    conn.commit()
    assert "da:779" not in [r["id"] for r in auctions.to_refresh(conn, NOW, own_limit=0)]  # на 7.5 день — рано
    later = NOW + 7 * 86400
    assert "da:779" in [r["id"] for r in auctions.to_refresh(conn, later, own_limit=0)]  # 14.5 дня, всё ещё [OPEN]
    conn.execute("UPDATE auctions SET extra = '{\"read_age_days\": 14.5}' WHERE deviation_id = 'da:779'")
    conn.commit()
    assert "da:779" not in [r["id"] for r in auctions.to_refresh(conn, later + 86400, own_limit=0)]  # хватит


def test_ended_or_too_old_lots_not_reread(conn):
    _market_lot(conn, 778, "open", 50, NOW - 50 * 86400)
    conn.execute("INSERT INTO auctions (deviation_id, n_bids, ended, updated_ts, extra) VALUES "
                 "('da:778', 0, 0, ?, '{\"read_age_days\": 2}')", (NOW - 48 * 86400,))
    conn.commit()
    ids = [r["id"] for r in auctions.to_refresh(conn, NOW, own_limit=0)]
    assert "da:778" not in ids  # старше 45 дней — не трогаем


def test_zero_comment_lot_saved_without_comments_call(conn):
    _market_lot(conn, 888, "open", 8, NOW - 8 * 86400)
    conn.execute("INSERT INTO deviation_stats (deviation_id, ts, views, favourites, comments) "
                 "VALUES ('da:888', ?, NULL, 1, 0)", (NOW - 3600,))
    conn.commit()
    client = Lookups(THREAD)
    stats: dict = {}
    auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None, own_limit=0, stats=stats)
    assert "888" not in client.reads and "888" not in client.lookups
    row = conn.execute("SELECT * FROM auctions WHERE deviation_id = 'da:888'").fetchone()
    assert row["n_bids"] == 0 and json.loads(row["extra"])["zero_by_stats"] is True
    assert stats["zero_by_stats"] == 1


def test_zero_comments_seen_too_early_is_not_an_outcome(conn):
    _market_lot(conn, 889, "open", 8, NOW - 8 * 86400)
    # снимок снят, когда лоту было 2 дня: потом ставки могли появиться
    conn.execute("INSERT INTO deviation_stats (deviation_id, ts, views, favourites, comments) "
                 "VALUES ('da:889', ?, 5, 1, 0)", (NOW - 6 * 86400,))
    conn.commit()
    item = db.get_item(conn, "da:889")
    assert not auctions.zero_by_stats(conn, item, {}, NOW)
    assert auctions.zero_by_stats(conn, item, {"889": {"comments": 0}}, NOW)
    assert not auctions.zero_by_stats(conn, item, {"889": {"comments": 4}}, NOW)


class FolderClient(Lookups):
    def __init__(self, comments, pages):
        super().__init__(comments)
        self.pages, self.requests = pages, []

    def _request(self, method, path, params=None, **kw):
        self.requests.append((path, params))
        return self.pages[path]


def _tracked_folder(conn):
    from mcp_devian.config import DAGroup, DeviantArt
    from mcp_devian.deviantart import Folder

    db.save_folders(conn, "x-group", [Folder("x-group", "26780440", "auctions", uuid="FOLDER-UUID")], NOW)
    return Config(deviantart=DeviantArt(
        groups=[DAGroup(url="https://www.deviantart.com/x-group/gallery/26780440", label="x-group: auctions")]))


def _api_dev(number, comments):
    return {"deviationid": f"FOLDER-{number}", "url": f"https://www.deviantart.com/x/art/Lot-{number}",
            "published_time": str(int(NOW - 8 * 86400)), "stats": {"comments": comments, "favourites": 2}}


def test_folder_page_uuids_remembered_and_zero_lots_skipped(conn):
    cfg = _tracked_folder(conn)
    _market_lot(conn, 901, "open", 8, NOW - 8 * 86400)
    _market_lot(conn, 902, "open", 8, NOW - 8 * 86400)
    client = FolderClient(THREAD, {"/gallery/FOLDER-UUID": {"results": [
        _api_dev(901, 0), _api_dev(902, 5), _api_dev(903, 1)]}})
    stats: dict = {}
    auctions.refresh_via_api(conn, cfg, client, NOW, sleep=lambda s: None, own_limit=0, stats=stats)
    assert client.requests[0][0] == "/gallery/FOLDER-UUID" and client.requests[0][1]["username"] == "x-group"
    assert auctions.known_uuid(db.get_item(conn, "da:901")) == "FOLDER-901"
    assert auctions.known_uuid(db.get_item(conn, "da:902")) == "FOLDER-902"
    assert stats["folder_uuids"] == 2  # 903 нет в базе — пропускаем
    assert "901" not in client.lookups and "902" not in client.lookups
    assert "901" not in client.reads and "902" in client.reads  # у 901 ноль комментариев
    snap = conn.execute("SELECT * FROM deviation_stats WHERE deviation_id = 'da:902'").fetchone()
    assert snap["comments"] == 5 and snap["views"] is None


def test_folder_without_known_uuid_is_skipped(conn):
    from mcp_devian.config import DAGroup, DeviantArt

    cfg = Config(deviantart=DeviantArt(groups=[DAGroup(url="https://www.deviantart.com/g/gallery/1")]))
    assert auctions.folder_targets(conn, cfg) == []


def test_market_lookup_cap_within_sample_of_twenty(conn):
    for n in range(300, 325):
        _market_lot(conn, n, "open", 3, NOW - 3 * 86400)
    client = Lookups([])
    auctions.refresh_via_api(conn, Config(), client, NOW, sleep=lambda s: None, own_limit=0)
    assert auctions.MARKET_SAMPLE_PER_RUN == 20
    assert len(client.lookups) == auctions.MARKET_LOOKUPS_PER_RUN == 8
    assert len(client.reads) == 8
    # Лоты с известным UUID в предел поисков не входят.
    for n in range(330, 340):
        _market_lot(conn, n, "open", 2.5, NOW - 2.5 * 86400)
        db.merge_extra(conn, f"da:{n}", {"uuid": f"uuid-{n}"})
    conn.commit()
    client = Lookups([])
    auctions.refresh_via_api(conn, Config(), client, NOW + 60, sleep=lambda s: None, own_limit=0)
    assert len(client.lookups) == 8 and len(client.reads) == 18
