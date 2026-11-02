"""ych.commishes.com: бережный клиент, разбор RSS/JSON/HTML, её аукционы, продажи, рынок YCH, советы.

Только фикстуры из tests/fixtures_commishes и подменённый транспорт httpx: в сеть тесты не ходят.
"""

import json
from pathlib import Path

import httpx
import pytest

from mcp_devian import __version__, auctions, bids, commishes, da_access, db, evidence, sales

FIX = Path(__file__).parent / "fixtures_commishes"
NOW = 1793624015.0          # "time" из auction_61A2P.json
ENDS = 1793646341.0         # endsunix 61A2P
HOME_RSS = (FIX / "home_index.rss").read_text()
ADOPT_RSS = (FIX / "category_adopt.rss").read_text()
JSON_61A2P = (FIX / "auction_61A2P.json").read_text()
PAGE_ADOPT = (FIX / "auction_adopt_61A2P.html").read_text()
PAGE_YCH = (FIX / "auction_ych_618QA.html").read_text()
PROFILE = (FIX / "user_Pinecrow.html").read_text()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class Site:
    """Подменённый ych.commishes.com: путь → (статус, тело, заголовки)."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        got = self.routes.get(req.url.path)
        if got is None:
            return httpx.Response(404, text="not found")
        status, body, headers = got if isinstance(got, tuple) else (200, got, {})
        return httpx.Response(status, text=body, headers=headers)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.calls]


def make_client(conn, site, clock=None, sleeps=None):
    http = commishes.make_http(transport=httpx.MockTransport(site.handler))
    return commishes.Client(conn, http=http, clock=clock or Clock(),
                            sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def json_with(**changes) -> str:
    data = json.loads(JSON_61A2P)
    slot = data["payload"]["slots"]["6334553"]
    for key, value in changes.items():
        if key in ("time",):
            data[key] = value
        elif key in ("endsunix",):
            data["payload"][key] = value
        elif key == "bids":
            slot["bids"] = value
            slot["highestbid"] = max((b["bid"] for b in value), default=0)
        else:
            slot[key] = value
    return json.dumps(data)


def bid(name, amount, ts, guest=False):
    return {"name": name, "guest": guest, "user": None if guest else {"id": "1", "name": name, "sponsor": False},
            "bid": amount, "created": ts}


BIDS_3 = json.loads(JSON_61A2P)["payload"]["slots"]["6334553"]["bids"]


# --- адреса и номера ---

def test_code_is_base32_id():
    assert commishes.code_to_id("61A2P") == 6334553
    assert commishes.code_to_id("618QA") == 6333258
    assert commishes.id_to_code(6334553) == "61A2P"
    assert commishes.id_to_code(commishes.code_to_id("61AIP")) == "61AIP"
    with pytest.raises(ValueError):
        commishes.code_to_id("61A2Z")  # Z вне base32 (0-9A-V)


def test_parse_url_accepts_only_auction_links_of_the_site():
    assert commishes.parse_url("https://ych.commishes.com/auction/show/61A2P/loyal-shepherd-dog-adopt/") == \
        (6334553, "61A2P")
    assert commishes.parse_url("http://ych.commishes.com/auction/show/618QA/") == (6333258, "618QA")
    # Ошибка общей ленты: в ссылке номер 0 — сопоставить нельзя.
    assert commishes.parse_url("http://ych.commishes.com/auction/show/0/ych-couple/") is None
    assert commishes.parse_url("https://evil.example/auction/show/61A2P/") is None
    assert commishes.parse_url("https://ych.commishes.com.evil.example/auction/show/61A2P/") is None
    assert commishes.auction_url("61A2P") == "https://ych.commishes.com/auction/show/61A2P/"


# --- RSS ---

def test_home_feed_has_no_ids_and_wrong_end_dates():
    entries, ttl = commishes.parse_rss(HOME_RSS)
    assert ttl == 1800
    assert len(entries) == 3
    assert all(e.id is None for e in entries)          # ссылки с ID 0
    shepherd, couple, sage = entries
    # Дата «Ends»/pubDate в ленте раньше настоящей: её не используем как конец.
    assert shepherd.rss_end_ts == ENDS - 86400
    assert shepherd.ends_ts is None
    assert (shepherd.sb, shepherd.mi, shepherd.ab) == (10, 5, None)
    assert (couple.sb, couple.mi, couple.ab) == (15, 5, None)
    assert couple.kind == "ych" and couple.ych_type == "couple"
    assert (50.0, "colored sketch") in couple.tiers and couple.sb_label == "Sketch"
    assert (sage.sb, sage.mi, sage.ab) == (15, 2, 60)
    assert "[Ends:" not in shepherd.text and "Hipolink" in shepherd.text
    assert "&#039;" not in shepherd.text and "you'll" in shepherd.text


def test_category_feed_has_real_ids():
    entries, _ = commishes.parse_rss(ADOPT_RSS, category="adopt")
    assert [e.id for e in entries] == [6334553, 6328412]
    assert [e.code for e in entries] == ["61A2P", "6142S"]
    assert all(e.kind == "adopt" and e.category == "adopt" for e in entries)
    assert entries[0].url == "https://ych.commishes.com/auction/show/61A2P/"


def test_bad_xml_is_an_error_not_a_crash():
    with pytest.raises(commishes.CommishesError):
        commishes.parse_rss("<html>oops")


# --- типы YCH ---

@pytest.mark.parametrize("title,text,kind,sub", [
    ("YCH COUPLE", "", "ych", "couple"),
    ("Chibi YCH open!", "", "ych", "chibi"),
    ("YCH icon $3", "", "ych", "icon"),
    ("Headshot YCH", "", "ych", "headshot"),
    ("YCH half-body", "", "ych", "halfbody"),
    ("Fullbody YCH [3 slots]", "", "ych", "fullbody"),
    ("YCH ref sheet", "", "ych", "ref"),
    ("Sketch YCH", "", "ych", "sketch"),
    ("Moon Moth - Adopt", "", "adopt", None),
    ("Commission slot", "", "commission", None),
    ("Sage The Wayfarer", "SB: 15$", "other", None),
])
def test_kind_and_ych_type(title, text, kind, sub):
    assert commishes.kind_of(title, text) == kind
    assert (commishes.ych_type_of(title, text) if kind == "ych" else None) == sub


def test_ych_type_ignores_bonus_words_deep_in_description():
    text = "Welcome!\nSB: 15$\nAB: 60$ + free Portrait\n" + "rules " * 60 + "headshot bonus"
    assert commishes.ych_type_of("YCH", text) is None


# --- JSON аукциона ---

def test_parse_auction_json():
    a = commishes.parse_auction_json(JSON_61A2P)
    assert (a.id, a.code, a.seller, a.title) == (6334553, "61A2P", "Pinecrow", "Loyal Shepherd Dog - Adopt")
    assert a.ends_ts == ENDS and a.fetched_ts == NOW
    assert a.promoted and not a.adult and a.rating == 0
    assert a.url == "https://ych.commishes.com/auction/show/61A2P/"
    (slot,) = a.slots
    assert (slot.startbid, slot.minincrease, slot.autobuy, slot.highest) == (10, 5, None, 37)
    assert [b.name for b in slot.bids] == ["Fernbyte_Arts", "LadyQuill", "Brindlefox"]  # по времени
    assert a.settings["security.sniping_disabled"] == "ON"


def test_parse_auction_json_rejects_errors():
    with pytest.raises(commishes.CommishesError):
        commishes.parse_auction_json('{"result": "404 Not Found", "payload": null}')
    with pytest.raises(commishes.CommishesError):
        commishes.parse_auction_json("<html>")


# --- страница аукциона ---

def test_parse_page_adopt_with_anti_snipe():
    p = commishes.parse_auction_page(PAGE_ADOPT)
    assert (p.id, p.code, p.seller, p.ends_ts) == (6334553, "61A2P", "Pinecrow", ENDS)
    assert p.category == "adopt"
    assert p.anti_snipe is True and p.anti_snipe_min == 10
    assert p.promoted and p.rating == "Safe"


def test_parse_page_ych_without_anti_snipe():
    p = commishes.parse_auction_page(PAGE_YCH)
    assert (p.id, p.code, p.seller, p.category) == (6333258, "618QA", "Mothwick", "human")
    assert p.anti_snipe is False and p.anti_snipe_min is None
    assert not p.promoted


# --- профиль ---

def test_parse_profile():
    prof = commishes.parse_profile(PROFILE)
    assert (prof.username, prof.userid, prof.registered) == ("Pinecrow", 195459, 1679299483)
    assert prof.stats["started"] == 748 and prof.stats["testimonials"] == 20 and prof.stats["approval"] == 100
    assert [c.id for c in prof.cards] == [6334553, 6335065]
    first, second = prof.cards
    assert (first.code, first.bid, first.ends_ts, first.promoted, first.sale) == ("61A2P", 37.0, ENDS, True, False)
    assert (second.code, second.rating, second.sale) == ("61AIP", 1, True)


def test_parse_history_is_defensive():
    assert commishes.parse_history("not json") == []
    assert commishes.parse_history('{"payload": {"weird": 1}}') == []
    cards = commishes.parse_history(json.dumps({"payload": [
        {"id": 6334553, "title": "Old", "endsunix": 1790000000, "bid": "25"},
        {"url": "/auction/show/618QA/x/", "title": "Old YCH", "ends": "Mon, 02 Nov 2026 20:05:41 +0100"},
        {"title": "no id"}]}))
    assert [(c.id, c.code) for c in cards] == [(6334553, "61A2P"), (6333258, "618QA")]
    assert cards[0].bid == 25 and cards[0].ends_ts == 1790000000
    assert cards[1].ends_ts == ENDS
    assert commishes.history_path("Pinecrow", 6334553) == "/user/history/Pinecrow.json?until=6334553"


# --- ставки ---

def test_slot_state_while_running_matches_bids_module():
    a = commishes.parse_auction_json(JSON_61A2P)
    st = commishes.slot_state(a, a.slots[0])
    assert isinstance(st, bids.AuctionState)
    assert (st.n_bids, st.bidders, st.top.user, st.top.amount, st.currency) == (3, 3, "Brindlefox", 37, "usd")
    assert st.last_bid_ts == 1793616569
    assert not st.ended and st.winner is None and not st.ab_hit
    text = bids.auction_summary(st, sb=10, currency="usd", now=NOW)  # совместимо с разбором DA
    assert "$37" in text and "Brindlefox" in text


def test_slot_state_final_only_when_read_after_end():
    late = commishes.parse_auction_json(json_with(time=ENDS + 120))
    st = commishes.slot_state(late, late.slots[0])
    assert st.ended and st.winner == "Brindlefox"
    assert commishes.is_final(late)
    early = commishes.parse_auction_json(JSON_61A2P)
    # Конец по часам наступил, но читали до конца: защита от снайпинга могла продлить — не итог.
    assert not commishes.is_final(early)
    assert not commishes.slot_state(early, early.slots[0], now=ENDS + 3600).ended


def test_autobuy_hit_and_guest_note():
    a = commishes.parse_auction_json(json_with(autobuy=30, bids=[bid("guesty", 30, NOW - 60, guest=True)] + BIDS_3[1:]))
    st = commishes.slot_state(a, a.slots[0])
    assert st.ab_hit and st.top.kind == "ab"
    assert any("гость" in n for n in st.notes)


def test_min_next_bid_and_steps():
    a = commishes.parse_auction_json(JSON_61A2P)
    assert commishes.min_next_bid(a.slots[0]) == 42
    empty = commishes.parse_auction_json(json_with(bids=[]))
    assert commishes.min_next_bid(empty.slots[0]) == 10
    # Как скрипт формы ставки (site_rules.txt): шаг самой большой границы строго меньше суммы.
    assert commishes.site_step(3) == 1 and commishes.site_step(10) == 2.5 and commishes.site_step(10.5) == 5
    assert commishes.site_step(5) == 1 and commishes.site_step(120) == 20 and commishes.site_step(1500) == 100
    assert commishes.site_step(0) == 0.5


def test_summary_text():
    a = commishes.parse_auction_json(JSON_61A2P)
    p = commishes.parse_auction_page(PAGE_ADOPT)
    text = commishes.summary(a, p, now=NOW)
    assert "$37" in text and "Brindlefox" in text and "SB $10" in text and "следующая ставка от $42" in text
    assert "10 мин" in text
    assert "разбор комментариев" not in text


# --- когда перечитывать ---

def test_next_read_schedule():
    assert commishes.next_read(ENDS, now=ENDS - 5 * 3600) == ENDS - 30 * 60
    assert commishes.next_read(ENDS, now=ENDS - 20 * 60) == ENDS - 10 * 60
    assert commishes.next_read(ENDS, now=ENDS - 5 * 60) == ENDS + 60
    assert commishes.next_read(ENDS, now=ENDS + 5) == ENDS + 60
    assert commishes.next_read(ENDS, now=ENDS + 3600) == ENDS + 3600
    assert commishes.next_read(ENDS, now=ENDS + 3600, final=True) is None


# --- бережный клиент ---

def test_client_sends_honest_user_agent_and_no_redirects(conn):
    site = Site({"/auction/show/61A2P/.json": JSON_61A2P,
                 "/user/Someone/": (302, "", {"Location": "https://account.commishes.com/login"})})
    c = make_client(conn, site)
    c.get("/auction/show/61A2P/.json", ttl=60)
    ua = site.calls[0].headers["User-Agent"]
    assert ua == commishes.USER_AGENT and ua.startswith(f"mcp_devian/{__version__} ")
    assert "Mozilla" not in ua and "Macintosh" not in ua  # не притворяемся браузером
    with pytest.raises(commishes.CommishesError):
        c.get("/user/Someone/", ttl=60)
    assert site.paths() == ["/auction/show/61A2P/.json", "/user/Someone/"]  # редирект не пошли


def test_client_only_talks_to_the_site(conn):
    c = make_client(conn, Site())
    for bad in ("https://evil.example/x", "//evil.example/x", "auction/show/1/", "/bids/post.json"):
        with pytest.raises(ValueError):
            c.get(bad, ttl=60)


def test_client_cache_avoids_requests(conn):
    site = Site({"/category/adopt/index.rss": ADOPT_RSS})
    clock = Clock()
    c = make_client(conn, site, clock)
    assert c.get("/category/adopt/index.rss", ttl=1800) == ADOPT_RSS
    clock.t += 600
    assert c.get("/category/adopt/index.rss", ttl=1800) == ADOPT_RSS
    assert len(site.calls) == 1
    clock.t += 1300
    c.get("/category/adopt/index.rss", ttl=1800)
    assert len(site.calls) == 2


def test_client_keeps_gap_between_requests(conn):
    site = Site({"/a.json": "{}", "/b.json": "{}"})
    sleeps: list[float] = []
    clock = Clock()
    c = make_client(conn, site, clock, sleeps)
    c.get("/a.json", ttl=0)
    clock.t += 1
    c.get("/b.json", ttl=0)
    assert sleeps and sleeps[-1] == pytest.approx(commishes.MIN_GAP_S - 1)


def test_client_hourly_budget(conn):
    site = Site({f"/p{i}.json": "{}" for i in range(30)})
    clock = Clock()
    c = make_client(conn, site, clock)
    for i in range(commishes.PER_HOUR):
        clock.t += commishes.MIN_GAP_S
        c.get(f"/p{i}.json", ttl=0)
    with pytest.raises(commishes.Throttled) as e:
        c.get("/p29.json", ttl=0)
    assert len(site.calls) == commishes.PER_HOUR
    assert "ych.commishes" in str(e.value)
    # Фоновая работа не съедает последние запросы часа.
    clock.t += 3601
    for i in range(commishes.PER_HOUR - commishes.LOW_RESERVE):
        clock.t += commishes.MIN_GAP_S
        c.get(f"/p{i}.json", ttl=0, low=True)
    with pytest.raises(commishes.Throttled):
        c.get("/p25.json", ttl=0, low=True)
    c.get("/p26.json", ttl=0)


@pytest.mark.parametrize("status", [403, 429, 503])
def test_client_pauses_after_refusal(conn, status):
    site = Site({"/a.json": (status, "no", {})})
    clock = Clock()
    c = make_client(conn, site, clock)
    with pytest.raises(commishes.Throttled):
        c.get("/a.json", ttl=0)
    clock.t += 60
    with pytest.raises(commishes.Throttled):
        c.get("/a.json", ttl=0)
    assert len(site.calls) == 1  # второй раз не спрашивали
    clock.t += commishes.COOLDOWN_STEPS_S[0] + 1
    site.routes["/a.json"] = "{}"
    assert c.get("/a.json", ttl=0) == "{}"


def test_client_honours_retry_after(conn):
    site = Site({"/a.json": (429, "", {"Retry-After": "7200"})})
    clock = Clock()
    c = make_client(conn, site, clock)
    with pytest.raises(commishes.Throttled):
        c.get("/a.json", ttl=0)
    clock.t += commishes.COOLDOWN_STEPS_S[0] + 60
    with pytest.raises(commishes.Throttled):
        c.get("/a.json", ttl=0)
    assert len(site.calls) == 1


def test_commishes_pause_does_not_look_like_deviantart(conn):
    site = Site({"/a.json": (429, "", {})})
    with pytest.raises(commishes.Throttled):
        make_client(conn, site).get("/a.json", ttl=0)
    assert da_access.status_line(conn, now=NOW + 5) is None
    assert not da_access.claim_notice(conn, now=NOW + 5)
    assert "ych.commishes" in commishes.health_line(conn, now=NOW + 5)


def test_cache_is_not_kept_long(conn):
    site = Site({"/a.json": "{}"})
    clock = Clock()
    c = make_client(conn, site, clock)
    c.get("/a.json", ttl=60)
    clock.t += commishes.KEEP_CACHE_S + 10
    commishes.prune(conn, now=clock.t)
    assert conn.execute("SELECT COUNT(*) FROM cm_cache").fetchone()[0] == 0


# --- её аукционы ---

def her_site(json_text=JSON_61A2P, profile=PROFILE):
    other = json.loads(JSON_61A2P)
    other["payload"].update(id=6335065, title="Shy Candy Bear - Adopt", endsunix=1793655336)
    other["payload"]["slots"] = {"6335065": {**other["payload"]["slots"]["6334553"], "name": "Shy Candy Bear",
                                             "highestbid": 50, "bids": [bid("Brindlefox", 50, NOW - 9000)]}}
    return Site({"/user/Pinecrow/": profile, "/auction/show/61A2P/.json": json_text,
                 "/auction/show/61A2P/": PAGE_ADOPT, "/auction/show/61AIP/.json": json.dumps(other),
                 "/auction/show/61AIP/": PAGE_ADOPT.replace("6334553", "6335065").replace("61A2P", "61AIP")})


def test_track_her_auctions_first_read(conn):
    site = her_site()
    c = make_client(conn, site)
    res = commishes.track_mine(conn, c, "Pinecrow", now=NOW)
    assert res.throttled is None
    assert res.events == []  # ставки старше 2 ч — точка отсчёта, не «новые»
    assert sorted(site.paths()) == sorted(["/user/Pinecrow/", "/auction/show/61A2P/.json", "/auction/show/61A2P/",
                                           "/auction/show/61AIP/.json", "/auction/show/61AIP/"])
    item = db.get_item(conn, "cm:6334553")
    assert item["source"] == "commishes" and item["own"] == 1 and item["author"] == "Pinecrow"
    assert item["price_usd"] == 10 and item["status"] == "open"
    extra = json.loads(item["extra"])
    assert extra["kind"] == "adopt" and extra["category"] == "adopt" and extra["anti_snipe"] is True
    assert extra["ends_ts"] == ENDS and extra["mi"] == 5
    row = conn.execute("SELECT * FROM auctions WHERE deviation_id = 'cm:6334553'").fetchone()
    assert row["top_amount"] == 37 and row["n_bids"] == 3 and row["currency"] == "usd" and not row["ended"]
    assert "Brindlefox" in res.text


def test_track_no_bid_lot_is_not_reread_every_time(conn):
    # Карточка лота без ставок показывает SB, а не 0: это не «новая ставка».
    site = her_site(json_text=json_with(bids=[]), profile=PROFILE.replace('bid="37"', 'bid="10"'))
    clock = Clock()
    c = make_client(conn, site, clock)
    commishes.track_mine(conn, c, "Pinecrow", now=clock.t)
    clock.t += 1900
    site.calls.clear()
    commishes.track_mine(conn, c, "Pinecrow", now=clock.t)
    assert site.paths() == ["/user/Pinecrow/"]


def test_track_keeps_bids_when_page_is_throttled(conn):
    site = her_site()
    site.routes["/auction/show/61A2P/"] = (429, "", {})
    c = make_client(conn, site)
    res = commishes.track_mine(conn, c, "Pinecrow", now=NOW)
    assert res.throttled
    assert conn.execute("SELECT top_amount FROM auctions WHERE deviation_id = 'cm:6334553'").fetchone()[0] == 37


def test_track_lot_missing_from_profile_is_checked_rarely(conn):
    site = her_site()
    clock = Clock()
    c = make_client(conn, site, clock)
    commishes.track_mine(conn, c, "Pinecrow", now=clock.t)
    site.routes["/user/Pinecrow/"] = PROFILE.split("<auction-card", 1)[0] + "</div></div></main></body></html>"
    for step, expect in ((1900, False), (1900, False), (3700, True), (1900, False)):
        clock.t += step
        site.calls.clear()
        commishes.track_mine(conn, c, "Pinecrow", now=clock.t)
        assert ("/auction/show/61A2P/.json" in site.paths()) is expect


def test_track_skips_unchanged_far_from_end(conn):
    site = her_site()
    clock = Clock()
    c = make_client(conn, site, clock)
    commishes.track_mine(conn, c, "Pinecrow", now=clock.t)
    clock.t += 1900  # профиль устарел, ставка на карточке та же
    site.calls.clear()
    commishes.track_mine(conn, c, "Pinecrow", now=clock.t)
    assert site.paths() == ["/user/Pinecrow/"]


def test_track_new_bid_event_when_card_bid_changes(conn):
    site = her_site()
    clock = Clock()
    c = make_client(conn, site, clock)
    commishes.track_mine(conn, c, "Pinecrow", now=clock.t)
    clock.t += 1900
    site.routes["/user/Pinecrow/"] = PROFILE.replace('bid="37"', 'bid="45"')
    site.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t, bids=[bid("Newfan", 45, clock.t - 30)] + BIDS_3)
    site.calls.clear()
    res = commishes.track_mine(conn, c, "Pinecrow", now=clock.t)
    assert "/auction/show/61A2P/.json" in site.paths() and "/auction/show/61A2P/" not in site.paths()
    assert [(e.amount, e.user) for e in res.events] == [(45, "Newfan")]
    assert "Новая ставка $45 от Newfan" in auctions.describe_event(res.events[0])


def test_track_near_end_every_10_minutes_and_after_end(conn):
    site = her_site()
    clock = Clock(ENDS - 25 * 60)
    c = make_client(conn, site, clock)
    site.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t)
    commishes.track_mine(conn, c, "", urls=["https://ych.commishes.com/auction/show/61A2P/x/"], now=clock.t)
    site.calls.clear()
    clock.t += 5 * 60
    commishes.track_mine(conn, c, "", now=clock.t)
    assert site.calls == []  # рано: раз в 10 минут
    clock.t += 6 * 60
    site.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t)
    commishes.track_mine(conn, c, "", now=clock.t)
    assert site.paths() == ["/auction/show/61A2P/.json"]
    # После конца — ещё одно чтение: итог.
    clock.t = ENDS + 120
    site.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t)
    site.calls.clear()
    commishes.track_mine(conn, c, "", now=clock.t)
    assert site.paths() == ["/auction/show/61A2P/.json"]
    row = conn.execute("SELECT * FROM auctions WHERE deviation_id = 'cm:6334553'").fetchone()
    assert row["ended"] == 1 and row["winner"] == "Brindlefox"
    assert db.get_item(conn, "cm:6334553")["status"] == "closed"
    site.calls.clear()
    clock.t += 3600
    commishes.track_mine(conn, c, "", now=clock.t)
    assert site.calls == []  # итог уже есть


def test_track_notices_anti_snipe_extension(conn):
    site = her_site()
    clock = Clock(ENDS - 20 * 60)
    c = make_client(conn, site, clock)
    site.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t)
    commishes.track_mine(conn, c, "", urls=["https://ych.commishes.com/auction/show/61A2P/"], now=clock.t)
    clock.t = ENDS + 120
    site.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t, endsunix=ENDS + 600,
                                                         bids=[bid("Sniper", 50, ENDS - 30)] + BIDS_3)
    res = commishes.track_mine(conn, c, "", now=clock.t)
    extra = json.loads(db.get_item(conn, "cm:6334553")["extra"])
    assert extra["ends_ts"] == ENDS + 600 and extra["first_ends_ts"] == ENDS and extra["extended"]
    assert not conn.execute("SELECT ended FROM auctions WHERE deviation_id = 'cm:6334553'").fetchone()[0]
    assert "продлил" in res.text


def test_track_refuses_someone_elses_url_when_username_known(conn):
    site = Site({"/user/Me/": "<html><body></body></html>", "/auction/show/61A2P/.json": JSON_61A2P,
                 "/auction/show/61A2P/": PAGE_ADOPT})
    c = make_client(conn, site)
    res = commishes.track_mine(conn, c, "Me", urls=["https://ych.commishes.com/auction/show/61A2P/"], now=NOW)
    assert db.get_item(conn, "cm:6334553")["own"] == 0
    assert "не твой" in res.text
    assert conn.execute("SELECT COUNT(*) FROM auctions").fetchone()[0] == 0


def test_track_stops_politely_when_throttled(conn):
    site = her_site()
    site.routes["/auction/show/61A2P/.json"] = (429, "", {})
    c = make_client(conn, site)
    res = commishes.track_mine(conn, c, "Pinecrow", now=NOW)
    assert res.throttled and "ych.commishes" in res.throttled
    assert "/auction/show/61AIP/.json" not in site.paths()  # дальше не долбим
    assert db.last_run(conn, "commishes")["last_error"]


def test_track_rejects_bad_username(conn):
    with pytest.raises(ValueError):
        commishes.track_mine(conn, make_client(conn, Site()), "../admin", now=NOW)


# --- продажи ---

def test_sale_sync_from_finished_auction(conn):
    site = her_site()
    clock = Clock(ENDS + 120)
    site.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t)
    c = make_client(conn, site, clock)
    commishes.track_mine(conn, c, "", urls=["https://ych.commishes.com/auction/show/61A2P/"], now=clock.t)
    created = commishes.sync_sales(conn, now=clock.t)
    assert len(created) == 1
    s = sales.get(conn, created[0])
    assert (s["platform"], s["format"], s["buyer"], s["amount"], s["currency"], s["item_id"], s["source"]) == \
        ("commishes", "adopt", "Brindlefox", 37, "usd", "cm:6334553", "auto")
    assert commishes.sync_sales(conn, now=clock.t) == []  # без дублей
    # Если она уже записала продажу сама по ссылке — авто-запись не заводит вторую.
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1


def test_sale_sync_ych_format_and_no_sale_without_bids(conn, tmp_path):
    site = her_site()
    clock = Clock(ENDS + 120)
    data = json.loads(json_with(time=clock.t))
    data["payload"]["title"] = "YCH icon"
    site.routes["/auction/show/61A2P/.json"] = json.dumps(data)
    site.routes["/auction/show/61A2P/"] = PAGE_YCH.replace("6333258", "6334553").replace("618QA", "61A2P")
    c = make_client(conn, site, clock)
    commishes.track_mine(conn, c, "", urls=["https://ych.commishes.com/auction/show/61A2P/"], now=clock.t)
    (sid,) = commishes.sync_sales(conn, now=clock.t)
    assert sales.get(conn, sid)["format"] == "ych"
    site2 = her_site()
    site2.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t, bids=[])
    with db.session(tmp_path / "b.sqlite3") as c2:
        commishes.track_mine(c2, make_client(c2, site2, clock), "",
                             urls=["https://ych.commishes.com/auction/show/61A2P/"], now=clock.t)
        assert commishes.sync_sales(c2, now=clock.t) == []


def test_manual_sale_by_url_then_auto_sync_no_duplicate(conn):
    sid, how = sales.record(conn, url="https://ych.commishes.com/auction/show/61A2P/loyal/", title="Shepherd",
                            amount=40, currency="usd", platform="", format="adopt", buyer="Brindlefox")
    assert how == "new"
    row = sales.get(conn, sid)
    assert (row["item_id"], row["platform"]) == ("cm:6334553", "commishes")
    site = her_site()
    clock = Clock(ENDS + 120)
    site.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t)
    commishes.track_mine(conn, make_client(conn, site, clock), "",
                         urls=["https://ych.commishes.com/auction/show/61A2P/"], now=clock.t)
    assert commishes.sync_sales(conn, now=clock.t) == []
    assert sales.get(conn, sid)["amount"] == 40  # её запись главнее


def test_sales_know_the_platform():
    assert "commishes" in sales.PLATFORMS and "ych" in sales.FORMATS


# --- рынок ---

def test_market_sample_from_category_feed(conn):
    site = Site({"/category/adopt/index.rss": ADOPT_RSS})
    c = make_client(conn, site)
    res = commishes.sample_market(conn, c, "adopt", now=NOW)
    assert res["new"] == 2 and site.paths()[0] == "/category/adopt/index.rss"
    item = db.get_item(conn, "cm:6334553")
    assert item["source"] == "commishes" and item["own"] == 0 and item["price_usd"] == 10
    assert item["matched"] == 0  # в «новые лоты» DA не попадает
    extra = json.loads(item["extra"])
    assert extra["price_basis"] == "sb_text" and extra["kind"] == "adopt" and "rss_end_ts" not in extra


def test_market_never_uses_home_feed(conn):
    with pytest.raises(ValueError):
        commishes.sample_market(conn, make_client(conn, Site()), "featured", now=NOW)


def test_market_finalize_reads_ended_lots_once(conn):
    site = Site({"/category/adopt/index.rss": ADOPT_RSS, "/auction/show/61A2P/.json": json_with(time=ENDS + 300)})
    clock = Clock()
    c = make_client(conn, site, clock)
    commishes.sample_market(conn, c, "adopt", now=clock.t)
    check_after = json.loads(db.get_item(conn, "cm:6334553")["extra"])["check_after_ts"]
    assert check_after == ENDS - 86400 + commishes.RSS_END_LAG_S  # дата RSS — только нижняя граница
    clock.t = ENDS + 3600
    site.calls.clear()
    assert commishes.finalize_market(conn, c, now=clock.t) == 0 and site.calls == []  # рано: до даты RSS + 3 дня
    clock.t = check_after + 120
    site.routes["/auction/show/61A2P/.json"] = json_with(time=clock.t)
    n = commishes.finalize_market(conn, c, now=clock.t, limit=5)
    assert n == 1 and site.paths() == ["/auction/show/61A2P/.json", "/auction/show/6142S/.json"]  # 6142S — 404
    extra = json.loads(db.get_item(conn, "cm:6334553")["extra"])
    assert extra["final"] and extra["top"] == 37 and extra["n_bids"] == 3
    assert "bidders" not in extra  # ники чужих покупателей рынка не храним
    assert conn.execute("SELECT COUNT(*) FROM auctions").fetchone()[0] == 0
    site.calls.clear()
    assert commishes.finalize_market(conn, c, now=clock.t + 60) == 0 and site.calls == []


def test_market_finalize_extends_and_forgets_deleted(conn):
    site = Site({"/category/adopt/index.rss": ADOPT_RSS,
                 "/auction/show/61A2P/.json": json_with(time=ENDS + 2 * 86400, endsunix=ENDS + 5 * 86400)})
    clock = Clock()
    c = make_client(conn, site, clock)
    commishes.sample_market(conn, c, "adopt", now=clock.t)
    clock.t = ENDS + 2 * 86400 + 600
    assert commishes.finalize_market(conn, c, now=clock.t) == 0  # 6142S — 404: забыт, 61A2P — ещё идёт
    assert json.loads(db.get_item(conn, "cm:6328412")["extra"])["gone"] is True
    extra = json.loads(db.get_item(conn, "cm:6334553")["extra"])
    assert extra["ends_ts"] == ENDS + 5 * 86400 and not extra["final"]
    site.calls.clear()
    assert commishes.finalize_market(conn, c, now=clock.t + 3600) == 0 and site.calls == []


def _market_lot(conn, i, kind, sub, sb, top=None, final=False, ts=NOW):
    extra = {"kind": kind, "ych_type": sub, "sb": sb, "final": final, "top": top, "n_bids": 1 if top else 0,
             "ends_ts": ts, "price_basis": "json" if final else "sb_text"}
    db.upsert_item(conn, db.Item(id=f"cm:{i}", source="commishes", title=f"lot {i}", url=f"u{i}", price_usd=sb,
                                 lot_type="ych" if kind == "ych" else "auction", extra=extra), now=ts)


def test_ych_stats_by_subtype_are_grounded(conn):
    for i in range(10):
        _market_lot(conn, i, "ych", "icon", 3 + i % 4, ts=NOW - i * 3600)
    for i in range(10, 13):
        _market_lot(conn, i, "ych", "fullbody", 30)
    for i in range(20, 30):
        _market_lot(conn, i, "adopt", None, 10, top=20 + i % 5, final=True)
    stats = commishes.price_stats(conn, now=NOW)
    icon = stats["ych:icon"]["sb"]
    assert icon.n == 10 and icon.unit == "usd" and icon.source == "market_commishes"
    assert 3 <= icon.value <= 6
    text = commishes.render_stats(stats, now=NOW)
    assert "иконки" in text and "n=10" in text and "рынок ych.commishes" in text
    assert "Вывода нет" in text  # fullbody: 3 лота
    assert "итог" in text and "адопты" in text
    assert "$" in text and "pts" not in text


def test_evidence_knows_the_source():
    assert evidence.SOURCE_LABEL["market_commishes"] == "рынок ych.commishes"


def test_prune_market_keeps_hers(conn):
    _market_lot(conn, 1, "adopt", None, 10, ts=NOW - 100 * 86400)
    db.upsert_item(conn, db.Item(id="cm:2", source="commishes", title="mine", url="u", own=True), now=NOW - 100 * 86400)
    commishes.prune(conn, now=NOW)
    assert db.get_item(conn, "cm:1") is None and db.get_item(conn, "cm:2") is not None


# --- советы ---

def test_ending_advice_with_anti_snipe():
    a = commishes.parse_auction_json(JSON_61A2P)
    lines = commishes.ending_advice(a, commishes.parse_auction_page(PAGE_ADOPT), now=NOW)
    text = "\n".join(lines).lower()
    assert "10 мин" in text and "насколько" in text  # сколько добавляет продление — не знаем, не выдумываем
    assert "$42" in text


def test_ending_advice_without_anti_snipe_or_page():
    a = commishes.parse_auction_json(JSON_61A2P)
    no = commishes.parse_auction_page(PAGE_YCH)
    assert any("Sniping protection" in s for s in commishes.ending_advice(a, no, now=NOW))
    assert any("не смотрела" in s for s in commishes.ending_advice(a, None, now=NOW))


def test_listing_check():
    items = commishes.listing_check("Cute YCH\nSB: 5\nPayment: PayPal only", lot_kind="ych")
    by = {i.key: i for i in items}
    assert not by["paypal"].ok and "PayPal" in by["paypal"].what
    assert not by["usd"].ok and "USD" in by["usd"].fix
    assert not by["mi"].ok
    assert by["ai"].ok is False or "AI" in by["ai"].what
    assert "pay_alt" not in by and commishes.listing_notes("Payment: PayPal only") == []
    good = commishes.listing_check("SB: $10 MI: $5 AB: $80. Prices in USD. Payment: Boosty. No AI.", lot_kind="adopt")
    assert all(i.ok for i in good if i.key in ("sb", "mi", "usd", "paypal"))
    (note,) = commishes.listing_notes("Payment: Hipolink or lava.top")
    assert "не проверено" in note and "Lava.top" in note


def test_crosspost_lines():
    out = commishes.crosspost_lines("https://ych.commishes.com/auction/show/61A2P/loyal/",
                                    "https://www.deviantart.com/her/art/Moth-123")
    assert "https://ych.commishes.com/auction/show/61A2P/" in out["da"]
    assert "https://www.deviantart.com/her/art/Moth-123" in out["commishes"]
    assert out["warnings"]  # ставки в двух местах — договорись, где главные
    with pytest.raises(ValueError):
        commishes.crosspost_lines("https://evil.example/auction/show/61A2P/")
    with pytest.raises(ValueError):
        commishes.crosspost_lines("https://ych.commishes.com/auction/show/61A2P/", "https://evil.example/art/1")


def test_crosspost_check_in_da_text():
    url = "https://ych.commishes.com/auction/show/61A2P/"
    assert commishes.crosspost_check(f"Bids only on ych.commishes: {url}")[0].ok
    bad = commishes.crosspost_check(f"Also here {url}")
    assert not bad[0].ok and "Bids only" in bad[0].fix


def test_listing_draft_is_text_only():
    d = commishes.listing_draft(title="Moth adopt", sb=10, mi=3, ab=8, kind="adopt", hours=48, payment="Boosty")
    assert d["fields"]["anti_snipe"] is True and d["fields"]["category"] == "adopt"
    assert "SB: $10" in d["description"] and "USD" in d["description"] and "Boosty" in d["description"]
    assert any("автобай" in w for w in d["warnings"])           # AB ≤ SB
    assert any("$2.5 " in w for w in d["warnings"])              # подсказка сайта для $10 — $2.5
    assert any("AI" in w for w in d["warnings"])
    assert d["submit"] is False and "TODO" in d["fill"]
    with pytest.raises(ValueError):
        commishes.listing_draft(title="", sb=10)


# --- 0.5.1: этикет и сроки после ревью ---

SELLER = json.loads(JSON_61A2P)["payload"]["username"]
CODE_URL = "https://ych.commishes.com/auction/show/61A2P/"


class LiveSite:
    """Сайт, у которого «time» в JSON — текущее время подменённых часов (как у настоящего сервера)."""

    def __init__(self, clock, profile="<html></html>", ends=ENDS, bids_list=None):
        self.clock, self.profile, self.ends, self.bids = clock, profile, ends, bids_list
        self.calls: list[str] = []
        self.json_status = 200

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        self.calls.append(p)
        if p.endswith(".json"):
            if self.json_status != 200:
                return httpx.Response(self.json_status, text="nf")
            d = json.loads(JSON_61A2P)
            d["time"], d["payload"]["endsunix"] = self.clock.t, self.ends
            if self.bids is not None:
                slot = d["payload"]["slots"]["6334553"]
                slot["bids"], slot["highestbid"] = self.bids, max(b["bid"] for b in self.bids)
            return httpx.Response(200, text=json.dumps(d))
        if p.startswith("/auction/show/"):
            return httpx.Response(200, text=PAGE_ADOPT)
        if p.startswith("/user/"):
            return httpx.Response(200, text=self.profile)
        return httpx.Response(404, text="nf")

    def json_reads(self) -> int:
        return sum(p.endswith(".json") for p in self.calls)


def test_after_end_read_skips_the_json_cache_and_closes(conn):
    # Она спросила статус за 30 с до конца (ответ в кэше 2 мин), фон пришёл через 70 с после конца.
    clock = Clock(ENDS - 30)
    site = LiveSite(clock)
    client = make_client(conn, site, clock=clock)
    commishes.status(conn, client, CODE_URL, SELLER, now=clock.t)
    clock.t = ENDS + 70
    commishes.track_mine(conn, client, SELLER, now=clock.t)
    row = db.get_item(conn, "cm:6334553")
    assert row["status"] == "closed" and json.loads(row["extra"])["final"] is True
    assert site.json_reads() == 2                          # второе чтение — настоящее, не из кэша
    assert len(commishes.sync_sales(conn, clock.t)) == 1


def test_not_final_after_end_is_reread_with_backoff(conn):
    # Часы мака впереди сервера: после конца сервер ещё отвечает «до конца» — перечитываем всё реже.
    clock = Clock(ENDS + 70)
    site = LiveSite(clock)
    site_clock_lag = 3600

    real = site.handler

    def lagging(req):
        saved = clock.t
        clock.t = min(saved - site_clock_lag, ENDS - 1)
        try:
            return real(req)
        finally:
            clock.t = saved

    client = commishes.Client(conn, http=commishes.make_http(transport=httpx.MockTransport(lagging)), clock=clock,
                              sleep=lambda s: None)
    commishes.status(conn, client, CODE_URL, SELLER, now=clock.t)
    for k in range(1, 49):                                 # 12 ч проходов раз в 15 мин
        clock.t = ENDS + 70 + k * 900
        commishes.track_mine(conn, client, SELLER, now=clock.t)
    assert 4 <= site.json_reads() <= 12                    # не 48 и не «никогда больше»


def test_deleted_auction_of_hers_is_closed_once(conn):
    clock = Clock(ENDS - 3600)
    site = LiveSite(clock)
    client = make_client(conn, site, clock=clock)
    commishes.status(conn, client, CODE_URL, SELLER, now=clock.t)
    site.json_status = 404
    before = site.json_reads()
    gone = []
    for _ in range(1, 200):                                # ~2 дня проходов раз в 15 мин
        clock.t += 900
        res = commishes.track_mine(conn, client, SELLER, now=clock.t)
        gone += res.gone
        if res.gone:
            assert any("больше его не проверяю" in n for n in res.notes)
    assert gone == [CODE_URL]                              # сказано один раз
    assert db.get_item(conn, "cm:6334553")["status"] == "closed"
    assert site.json_reads() == before + 1                 # один 404 — и больше не спрашиваем


def test_unknown_lot_whose_json_fails_is_not_asked_every_pass(conn):
    clock = Clock(NOW)
    site = Site({f"/user/{SELLER}/": PROFILE})             # карточки есть, JSON — 404
    client = make_client(conn, site, clock=clock)
    for k in range(8):                                     # 2 часа проходов
        clock.t = NOW + k * 900
        commishes.track_mine(conn, client, SELLER, now=clock.t)
    json_calls = [p for p in site.paths() if p.endswith(".json")]
    assert len(json_calls) == len(set(json_calls))         # каждый адрес — один раз за 6 ч


def test_profile_typo_is_asked_once_per_6h_and_reported(conn):
    clock = Clock(NOW)
    site = Site({})
    client = make_client(conn, site, clock=clock)
    results = []
    for k in range(8):
        clock.t = NOW + k * 900
        results.append(commishes.track_mine(conn, client, "Typo_name", now=clock.t))
    assert site.paths() == ["/user/Typo_name/"]
    assert all(r.profile_error and "commishes_username" in r.profile_error for r in results)
    clock.t = NOW + commishes.ERROR_TTL_S + 1
    commishes.track_mine(conn, client, "Typo_name", now=clock.t)
    assert len(site.paths()) == 2


def test_network_errors_count_pause_and_break_the_pass(conn):
    clock = Clock(NOW)
    calls, sleeps = [], []

    def slow(req):
        calls.append(req.url.path)
        raise httpx.ReadTimeout("slow", request=req)

    client = commishes.Client(conn, http=commishes.make_http(transport=httpx.MockTransport(slow)), clock=clock,
                              sleep=sleeps.append)
    urls = [commishes.auction_url(commishes.id_to_code(6000000 + i)) for i in range(30)]
    res = commishes.track_mine(conn, client, "", urls=urls, now=NOW)
    assert len(calls) == 1 and res.throttled and "не отвечает" in res.throttled
    assert conn.execute("SELECT COUNT(*) FROM da_budget WHERE kind = 'commishes'").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM da_cooldown WHERE kind = 'commishes'").fetchone()
    assert row["status"] == 0 and row["until_ts"] == NOW + commishes.NET_PAUSE_S and row["level"] == 0
    assert "Сайт не отвечал" in commishes.health_line(conn, NOW)
    # Пауза кончилась — снова можно, и между попытками — 3 с.
    clock.t = NOW + commishes.NET_PAUSE_S + 1
    commishes.track_mine(conn, client, "", urls=urls[:1], now=clock.t)
    assert len(calls) == 2


def test_market_finalize_does_not_forget_lots_on_network_errors(conn):
    db.upsert_item(conn, db.Item(id="cm:6334553", source="commishes", title="x", url=CODE_URL,
                                 extra={"code": "61A2P", "ends_ts": NOW - 7200, "kind": "adopt"}), now=NOW - 86400)

    def down(req):
        raise httpx.ConnectError("down", request=req)

    client = commishes.Client(conn, http=commishes.make_http(transport=httpx.MockTransport(down)), clock=Clock(),
                              sleep=lambda s: None)
    commishes.finalize_market(conn, client, now=NOW)
    assert not json.loads(db.get_item(conn, "cm:6334553")["extra"]).get("gone")


def test_anti_snipe_extension_beats_cached_profile_end(conn):
    clock = Clock(ENDS - 300)
    site = LiveSite(clock, profile=PROFILE)
    client = make_client(conn, site, clock=clock)
    commishes.track_mine(conn, client, SELLER, now=clock.t)
    # Ставка за 2 мин до конца продлила аукцион до T+8 мин; профиль (в кэше 30 мин) знает старый конец.
    site.ends = ENDS + 480
    site.bids = [bid("Late", 42, ENDS - 120), *BIDS_3]
    reads = {}
    for dt in (61, 5 * 60, 9 * 60, 15 * 60):
        clock.t = ENDS + dt
        n0 = site.calls.count("/auction/show/61A2P/.json")
        commishes.track_mine(conn, client, SELLER, now=clock.t)
        reads[dt] = site.calls.count("/auction/show/61A2P/.json") - n0
    assert reads[61] == 1 and reads[9 * 60] == 1           # после нового конца — читаем итог
    assert json.loads(db.get_item(conn, "cm:6334553")["extra"])["final"] is True
    assert [s for s in commishes.sync_sales(conn, clock.t)]


def test_other_sellers_json_is_not_kept_and_market_has_no_text(conn):
    clock = Clock(NOW)
    site = Site({"/auction/show/61A2P/.json": JSON_61A2P, "/category/adopt/index.rss": ADOPT_RSS})
    client = make_client(conn, site, clock=clock)
    client.get(commishes.json_path("61A2P"), ttl=commishes.JSON_TTL_S)
    clock.t = NOW + commishes.KEEP_JSON_S + 1
    commishes.sample_market(conn, client, "adopt", now=clock.t)  # любая следующая запись чистит старые JSON
    paths = [r[0] for r in conn.execute("SELECT path FROM cm_cache")]
    assert "/auction/show/61A2P/.json" not in paths and "/category/adopt/index.rss" in paths
    assert conn.execute("SELECT COUNT(*) FROM items WHERE source = 'commishes' AND snippet IS NOT NULL"
                        ).fetchone()[0] == 0


def test_maybe_prune_runs_daily_from_any_path(conn):
    commishes._ensure_cache(conn)
    conn.execute("INSERT INTO cm_cache VALUES ('/auction/show/X/.json', ?, '{}')", (NOW - 3600,))
    assert commishes.maybe_prune(conn, NOW) is True
    assert conn.execute("SELECT COUNT(*) FROM cm_cache").fetchone()[0] == 0
    assert commishes.maybe_prune(conn, NOW + 60) is False
    assert commishes.maybe_prune(conn, NOW + 86401) is True
    assert commishes.KEEP_MARKET_DAYS <= commishes.MARKET_SAMPLE_DAYS + 7


# --- 0.5.1: верность цифр и правил ---

def test_site_rules_come_from_saved_excerpts():
    rules = (FIX / "site_rules.txt").read_text()
    assert "AI generated images, neural network filters and similar are not permitted" in rules
    assert "you are required to buy the resulting commission" in rules
    assert "already a bid with the amount you offered" in rules
    table = json.loads("{" + rules.split("var Sn={", 1)[1].split("}", 1)[0].replace("1e3", "1000")
                       .replace(",", ',"').replace("{", "").replace(":", '":').join(['"', ""]) + "}")
    assert {float(k): v for k, v in table.items()} == {float(f): s for f, s in commishes.SITE_STEPS}


@pytest.mark.parametrize("text,sb,mi", [
    ("SB: 300 pts", None, None),
    ("SB: 1500 points\nMI: 100 pts", None, None),
    ("SB: 500₽", None, None),
    ("SB: 10€", None, None),
    ("SB: 5 DA points", None, None),
    ("SB: 1,500$", 1500, None),
    ("SB: 12,5$", 12.5, None),
    ("Minimum bid: $10", 10, None),
    ("SB: 10$\nMin: 5$", 10, 5),
    ("Min bid increment: 2$", None, 2),
    ("SB: 10$, MI: 5$", 10, 5),
])
def test_text_prices_only_take_dollars(text, sb, mi):
    got = commishes.text_prices(text)
    assert (got["sb"], got["mi"]) == (sb, mi)


@pytest.mark.parametrize("title,text,sub", [
    ("YCH Fullbody + free icon", "", "fullbody"),
    ("Full body YCH (bonus headshot)", "", "fullbody"),
    ("YCH", "SB - Sketch\nMI: $2\nFullbody, any species", "fullbody"),
    ("Pair of wings YCH", "", None),
    ("Icon / Halfbody YCH", "", "icon"),
])
def test_ych_type_is_the_main_word_not_a_bonus(title, text, sub):
    assert commishes.ych_type_of(title, text) == sub


def test_min_next_bid_without_step_is_not_the_top_bid():
    a = commishes.parse_auction_json(json_with(minincrease=None))
    assert commishes.min_next_bid(a.slots[0]) is None
    assert "выше $37" in commishes.summary(a, None, NOW)


def _market(conn, prices, kind="ych", sub="halfbody", category="furry", ends=None):
    for i, p in enumerate(prices):
        db.upsert_item(conn, db.Item(id=f"cm:{7000 + i}", source="commishes", title=f"lot {i}", url=f"u{i}",
                                     price_usd=p, extra={"kind": kind, "ych_type": sub, "sb": p, "category": category,
                                                         "ends_ts": ends}), now=NOW - i * 600)


def test_price_hint_compares_with_spread_not_median_interval(conn):
    prices = [5, 8, 10, 10, 12, 12, 14, 15, 15, 15, 16, 18, 19, 20, 20, 21, 24, 25, 28, 30]
    _market(conn, prices)
    for i in range(len(prices)):  # итоги со ставками — те же суммы
        db.merge_extra(conn, f"cm:{7000 + i}", {"final": True, "top": prices[i]})
    hint = commishes.price_hint(conn, "YCH halfbody", "ych", 20, "usd", now=NOW)
    assert "в средней половине цен" in hint and "80% для медианы" in hint
    assert "выше" not in hint.split(";")[-1]
    assert "выше, чем у" in commishes.price_hint(conn, "YCH halfbody", "ych", 29, "usd", now=NOW)


def test_stats_label_names_feeds_and_period_is_not_in_future(conn):
    _market(conn, [10] * 9, kind="adopt", sub=None, category="adopt", ends=NOW + 5 * 86400)
    ev = commishes.price_stats(conn, now=NOW)["adopt"]["sb"]
    assert "ленты: Adoptables" in ev.population and ev.until_ts <= NOW


def test_render_stats_says_when_ych_feeds_are_missing(conn):
    _market(conn, [10] * 9, kind="adopt", sub=None, category="adopt")
    text = commishes.render_stats(commishes.price_stats(conn, now=NOW), configured=["adopt"], focus="ych:icon")
    assert "YCH в выборке нет" in text and "commishes_categories" in text and "адопты" not in text
    empty = commishes.render_stats({}, background=False)
    assert "при следующей фоновой проверке" not in empty and "commishes_username" in empty


def test_status_of_her_auction_says_things_once_and_gives_da_line(conn):
    site = Site({"/auction/show/61A2P/.json": JSON_61A2P, "/auction/show/61A2P/": PAGE_ADOPT})
    text, _ = commishes.status(conn, make_client(conn, site), CODE_URL, SELLER, now=NOW)
    assert text.count("Конец ") == 1 and text.count("Защита от снайпинга включена") == 1
    assert text.lower().count("следующая ставка") == 1 and "сайт отбрасывает" in text
    assert "Bids only on ych.commishes (anti-snipe on, USD): " + CODE_URL in text
    assert "сама в фоне не слежу" in text                  # watched=False: без обещаний «смотрю раз в 10 мин»
    watched, _ = commishes.status(conn, make_client(conn, site), CODE_URL, SELLER, now=NOW, watched=True)
    assert "при каждой фоновой проверке" in watched


def test_crosspost_line_mentions_anti_snipe_only_when_seen():
    assert "anti-snipe" not in commishes.crosspost_lines(CODE_URL)["da"]
    assert "anti-snipe on" in commishes.crosspost_lines(CODE_URL, anti_snipe=True)["da"]


def _two_slots(ids=("6334553", "7000001")) -> str:
    d = json.loads(JSON_61A2P)
    d["time"] = ENDS + 100
    s = d["payload"]["slots"].pop("6334553")
    d["payload"]["slots"] = {ids[0]: {**s, "name": "Slot A"},
                             ids[1]: {**s, "name": "Slot B", "bids": s["bids"][1:], "highestbid": 15}}
    return json.dumps(d)


@pytest.mark.parametrize("ids", [("6334553", "7000001"), ("7000000", "7000001")])
def test_multi_slot_sales_are_not_doubled(conn, ids):
    site = Site({"/auction/show/61A2P/.json": _two_slots(ids), "/auction/show/61A2P/": PAGE_ADOPT})
    client = make_client(conn, site, clock=Clock(ENDS + 100))
    b = json.loads(_two_slots(ids))["payload"]["slots"][ids[1]]
    top_b = max(b["bids"], key=lambda x: x["bid"])
    # Она записала слот B по ссылке ещё до того, как помощник видел аукцион.
    sales.record(conn, url=CODE_URL, title="Slot B", amount=top_b["bid"], currency="usd", platform="commishes",
                 format="adopt", buyer=top_b["name"])
    commishes.status(conn, client, CODE_URL, SELLER, now=ENDS + 100)
    commishes.sync_sales(conn, ENDS + 100)
    rows = conn.execute("SELECT item_id, amount, buyer, source FROM sales ORDER BY amount").fetchall()
    assert len(rows) == 2 and {r["item_id"] for r in rows} == {f"cm:6334553:{ids[0]}", f"cm:6334553:{ids[1]}"}
    assert [r["source"] for r in rows if r["amount"] == top_b["bid"]] == ["manual"]
    # Теперь аукцион известен: без слота — спросить, со слотом — тот же лот.
    with pytest.raises(sales.SaleError, match="несколько слотов"):
        sales.record(conn, url=CODE_URL, title="?", amount=37, currency="usd", platform="commishes", format="adopt")
    sid, how = sales.record(conn, url=CODE_URL, title="Slot A", amount=37, currency="usd", platform="commishes",
                            format="adopt", lot="slot a")
    assert how == "merged" and conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2


# --- 0.5.1: исправления после проверки (деньги, трафик, тексты) ---

@pytest.mark.parametrize("amount,buyer", [(None, "Brindlefox"), (37, "@Brindlefox"), (None, "@brindlefox")])
def test_manual_base_sale_without_amount_or_with_at_is_not_doubled(conn, amount, buyer):
    site = Site({"/auction/show/61A2P/.json": _two_slots(), "/auction/show/61A2P/": PAGE_ADOPT})
    client = make_client(conn, site, clock=Clock(ENDS + 100))
    sales.record(conn, url=CODE_URL, title="Slot A", amount=amount, currency="usd", platform="commishes",
                 format="adopt", buyer=buyer)
    commishes.status(conn, client, CODE_URL, SELLER, now=ENDS + 100)
    commishes.sync_sales(conn, ENDS + 100)
    rows = conn.execute("SELECT item_id, amount, source FROM sales ORDER BY item_id").fetchall()
    assert [(r["item_id"], r["amount"], r["source"]) for r in rows] == [
        ("cm:6334553:6334553", 37, "manual"), ("cm:6334553:7000001", 15, "auto")]


def test_manual_base_sale_with_other_amount_is_hers_and_marked(conn):
    site = Site({"/auction/show/61A2P/.json": _two_slots(), "/auction/show/61A2P/": PAGE_ADOPT})
    client = make_client(conn, site, clock=Clock(ENDS + 100))
    sid, _ = sales.record(conn, url=CODE_URL, title="Slot B", amount=20, currency="usd", platform="commishes",
                          format="adopt", buyer="LadyQuill")
    commishes.status(conn, client, CODE_URL, SELLER, now=ENDS + 100)
    commishes.sync_sales(conn, ENDS + 100)
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2
    row = sales.get(conn, sid)
    assert row["item_id"] == "cm:6334553:7000001" and row["amount"] == 20 and "проверь" in row["note"]


def test_da_crosspost_sale_is_not_counted_again(conn):
    sid, _ = sales.record(conn, url="https://www.deviantart.com/her/art/Loyal-Shepherd-Dog-1234567", title="Dog",
                          amount=37, currency="usd", platform="", format="adopt", buyer="Brindlefox", now=ENDS + 60)
    clock = Clock(ENDS + 100)
    commishes.status(conn, make_client(conn, LiveSite(clock), clock=clock), CODE_URL, SELLER, now=clock.t)
    assert commishes.sync_sales(conn, clock.t) == []
    (row,) = conn.execute("SELECT * FROM sales").fetchall()
    assert row["id"] == sid and row["item_id"] == "cm:6334553" and row["source"] == "manual"


def _other_two_slots(t, a_bids, b_bids) -> str:
    d = json.loads(JSON_61A2P)
    d["time"], d["payload"]["username"] = t, "SomeoneElse"
    s = d["payload"]["slots"].pop("6334553")
    d["payload"]["slots"] = {
        "100": {**s, "name": "A", "startbid": 10, "bids": a_bids, "highestbid": max(b["bid"] for b in a_bids)},
        "200": {**s, "name": "B", "startbid": 5, "bids": b_bids, "highestbid": max(b["bid"] for b in b_bids)}}
    return json.dumps(d)


def test_market_finalize_keeps_each_slot_its_own_numbers(conn):
    site = Site({"/auction/show/61A2P/.json": _other_two_slots(NOW, [bid("X", 20, NOW - 60)],
                                                               [bid("Y", 6, NOW - 60)]),
                 "/auction/show/61A2P/": PAGE_ADOPT})
    clock = Clock(NOW)
    client = make_client(conn, site, clock=clock)
    commishes.status(conn, client, CODE_URL, SELLER, now=NOW)  # чужой аукцион, идёт
    clock.t = ENDS + 600
    site.routes["/auction/show/61A2P/.json"] = _other_two_slots(
        ENDS + 500, [bid("X", 20, NOW), bid("Z", 100, ENDS - 60)], [bid("Y", 6, NOW), bid("W", 10, ENDS - 60)])
    site.calls.clear()
    assert commishes.finalize_market(conn, client, now=clock.t) == 2
    assert site.paths() == ["/auction/show/61A2P/.json"]                  # один JSON на аукцион
    a = json.loads(db.get_item(conn, "cm:6334553:100")["extra"])
    b = json.loads(db.get_item(conn, "cm:6334553:200")["extra"])
    assert (a["top"], a["sb"], a["n_bids"]) == (100, 10, 2) and (b["top"], b["sb"], b["n_bids"]) == (10, 5, 2)
    lists, _ = commishes.price_lists(conn, now=clock.t)
    assert sorted(v for v, _ in lists["adopt"]["final"]) == [10, 100]


def test_market_sample_does_not_duplicate_lots_stored_per_slot(conn):
    site = Site({"/auction/show/61A2P/.json": _two_slots(), "/auction/show/61A2P/": PAGE_ADOPT,
                 "/category/adopt/index.rss": ADOPT_RSS})
    client = make_client(conn, site, clock=Clock(ENDS + 100))
    commishes.status(conn, client, CODE_URL, SELLER, now=ENDS + 100)
    commishes.sample_market(conn, client, "adopt", now=ENDS + 100)
    assert db.get_item(conn, "cm:6334553") is None                        # её лот не попал в «все продавцы»
    lists, _ = commishes.price_lists(conn, now=ENDS + 100)
    assert len(lists["adopt"]["sb"]) == 1                                  # только чужой 6142S


def test_market_resample_keeps_learned_end_and_gone(conn):
    site = Site({"/category/adopt/index.rss": ADOPT_RSS,
                 "/auction/show/61A2P/.json": json_with(time=ENDS + 2 * 86400, endsunix=ENDS + 5 * 86400)})
    clock = Clock()
    c = make_client(conn, site, clock)
    commishes.sample_market(conn, c, "adopt", now=clock.t)
    clock.t = ENDS + 2 * 86400 + 600
    commishes.finalize_market(conn, c, now=clock.t)
    for k in range(1, 5):                                                  # проходы рынка раз в 6 ч
        clock.t += 6 * 3600 + k
        site.calls.clear()
        commishes.sample_market(conn, c, "adopt", now=clock.t)
        commishes.finalize_market(conn, c, now=clock.t)
        assert all(not p.endswith(".json") for p in site.paths())
    assert json.loads(db.get_item(conn, "cm:6334553")["extra"])["ends_ts"] == ENDS + 5 * 86400
    gone = db.get_item(conn, "cm:6328412")
    assert json.loads(gone["extra"])["gone"] is True and gone["status"] == "closed"


def test_bidders_on_commishes_are_not_deviantart_contacts(conn):
    from mcp_devian import buyers, outreach

    commishes.status(conn, make_client(conn, Site({"/auction/show/61A2P/.json": JSON_61A2P,
                                                   "/auction/show/61A2P/": PAGE_ADOPT})), CODE_URL, SELLER, now=NOW)
    buyers.sync(conn, now=NOW, my_username=SELLER)
    c = buyers.contact(conn, "Brindlefox")
    assert c is not None and "bidder" in c["roles"]
    assert "deviantart" not in c["dm_roles"] and not buyers.dm_ok(c) and outreach.channel_for(c) is None


@pytest.mark.parametrize("text,sb", [
    ("SB: 1 500$", 1500), ("SB: $1 500", 1500), ("SB: 10 000 ₽", None), ("SB: 1 500 pts", None),
    ("SB: 1 500₽", None), ("SB: 5k", None), ("SB: 5k$", None), ("SB: 10 $", 10),
])
def test_text_prices_know_space_thousands_and_k(text, sb):
    assert commishes.text_prices(text)["sb"] == sb


def test_wide_spread_is_not_called_a_small_sample(conn):
    _market(conn, [1, 5, 20, 40, 60, 90, 120, 150, 180, 200], sub="icon")
    stats = commishes.price_stats(conn, now=NOW)
    assert evidence.level(stats["ych:icon"]["sb"]) == "нет вывода"
    text = commishes.render_stats(stats, now=NOW)
    assert "цены слишком разные (n=10" in text and "из нужных 8" not in text
    assert "из нужных 8" not in commishes.price_hint(conn, "YCH icon", "ych", now=NOW)


class _ErrSite(LiveSite):
    def __init__(self, clock, mode):
        super().__init__(clock)
        self.mode = None
        self.later = mode

    def handler(self, req):
        if self.mode and req.url.path.endswith(".json"):
            self.calls.append(req.url.path)
            if self.mode == "410":
                return httpx.Response(410, text="gone")
            return httpx.Response(200, text='{"result": "404 Not Found", "payload": null}')
        return super().handler(req)


@pytest.mark.parametrize("mode", ["410", "envelope"])
def test_her_auction_whose_json_breaks_after_end_is_not_read_every_pass(conn, mode):
    clock = Clock(NOW)
    site = _ErrSite(clock, mode)
    client = make_client(conn, site, clock=clock)
    commishes.track_mine(conn, client, SELLER, urls=[CODE_URL], now=NOW)
    site.mode = site.later
    before = site.json_reads()
    for k in range(192):                                                   # 2 дня проходов раз в 15 мин
        clock.t = ENDS + 300 + k * 900
        commishes.track_mine(conn, client, SELLER, now=clock.t)
    assert site.json_reads() - before <= commishes.ERR_GONE_AFTER
    assert db.get_item(conn, "cm:6334553")["status"] == "closed"


def test_client_remembers_other_4xx_like_404(conn):
    clock = Clock(NOW)
    site = Site({"/user/Someone/": (401, "", {})})
    client = make_client(conn, site, clock=clock)
    for k in range(8):
        clock.t = NOW + k * 900
        res = commishes.track_mine(conn, client, "Someone", now=clock.t)
        assert res.profile_error
    assert site.paths() == ["/user/Someone/"]


def test_feed_with_html_entities_in_title_still_parses():
    rss = ADOPT_RSS.replace("<title>", "<title>(&bdquo;&bull; ᴗ &bull;&bdquo;) &nosuch; ", 2)
    entries, _ = commishes.parse_rss(rss, "adopt")
    assert [e.id for e in entries] == [6334553, 6328412]
    assert entries[0].title.startswith("(„• ᴗ •„) &nosuch; ")


def test_client_reserves_budget_before_sending_across_processes(tmp_path):
    path = tmp_path / "shared.sqlite3"
    a_conn, b_conn = db.connect(path), db.connect(path)
    clock = Clock(NOW)
    sent, b_result, b_sleeps = [], [], []

    def b_handler(req):
        sent.append("B")
        return httpx.Response(200, text="{}")

    b = commishes.Client(b_conn, http=commishes.make_http(transport=httpx.MockTransport(b_handler)), clock=clock,
                         sleep=b_sleeps.append)

    def a_handler(req):
        sent.append("A")
        try:  # пока запрос A в пути, второй процесс спрашивает своё
            b_result.append(b.get(req.url.path.replace("a", "b"), ttl=0))
        except commishes.Throttled as e:
            b_result.append(e)
        return httpx.Response(200, text="{}")

    a = commishes.Client(a_conn, http=commishes.make_http(transport=httpx.MockTransport(a_handler)), clock=clock,
                         sleep=lambda s: None)
    a.get("/a1.json", ttl=0)
    assert sent == ["A", "B"] and b_sleeps == [pytest.approx(commishes.MIN_GAP_S)]  # B ждёт свой зазор
    for i in range(commishes.PER_HOUR - 3):
        a_conn.execute("INSERT INTO da_budget (ts, kind, cost) VALUES (?, 'commishes', 1)", (NOW - 60 - i,))
    a_conn.commit()
    sent.clear()
    b_result.clear()
    a.get("/a2.json", ttl=0)                                               # 19 в часе + A = 20: B уже некуда
    assert sent == ["A"] and isinstance(b_result[0], commishes.Throttled)
    used = a_conn.execute("SELECT SUM(cost) FROM da_budget WHERE kind = 'commishes'").fetchone()[0]
    assert used == commishes.PER_HOUR
    a_conn.close()
    b_conn.close()


def test_later_network_error_does_not_shorten_a_429_pause(conn):
    site = Site({"/a.json": (429, "", {"Retry-After": "7200"})})
    c = make_client(conn, site)
    with pytest.raises(commishes.Throttled):
        c.get("/a.json", ttl=0)
    c._pause(0, None)                                                      # запрос, ушедший до паузы, упал по сети
    row = conn.execute("SELECT until_ts, status FROM da_cooldown WHERE kind = 'commishes'").fetchone()
    assert (row["until_ts"], row["status"]) == (NOW + 7200, 429)
    assert "просил притормозить" in c.block().message


def test_json_with_nicks_is_not_kept_until_the_daily_prune(conn):
    site = Site({"/auction/show/61A2P/.json": JSON_61A2P, "/auction/show/61A2P/": PAGE_ADOPT})
    commishes.status(conn, make_client(conn, site), CODE_URL, "SomeoneElse", now=NOW)
    paths = [r[0] for r in conn.execute("SELECT path FROM cm_cache")]
    assert "/auction/show/61A2P/.json" not in paths                        # чужой: ники сразу не храним
    assert commishes.maybe_prune(conn, NOW) is True
    conn.execute("INSERT INTO cm_cache VALUES ('/auction/show/X/.json', ?, '{}')", (NOW,))
    assert commishes.maybe_prune(conn, NOW + commishes.KEEP_JSON_S + 1) is False  # сутки не прошли…
    assert conn.execute("SELECT COUNT(*) FROM cm_cache WHERE path LIKE '%.json'").fetchone()[0] == 0  # …а JSON убран


def test_status_of_a_final_auction_is_answered_from_the_database(conn):
    clock = Clock(ENDS + 100)
    site = LiveSite(clock)
    client = make_client(conn, site, clock=clock)
    first, _ = commishes.status(conn, client, CODE_URL, SELLER, now=clock.t)
    assert "Закончился: победитель Brindlefox" in first
    for k in range(1, 14):
        clock.t = ENDS + 100 + k * 3 * 3600
        text, _ = commishes.status(conn, client, CODE_URL, SELLER, now=clock.t)
    assert site.json_reads() == 1
    assert "победитель Brindlefox" in text and "$37" in text and "не изменится" in text


def test_anti_snipe_line_names_what_is_missing_and_is_silent_after_end():
    a = commishes.parse_auction_json(JSON_61A2P)
    page = commishes.parse_auction_page(PAGE_ADOPT)
    running = " ".join(commishes.ending_advice(a, page, NOW, brief=True, missing="commishes_background"))
    assert "не хватает commishes_background" in running and "ник" not in running
    done = commishes.parse_auction_json(json_with(time=ENDS + 60))
    for watched in (False, True):
        text = " ".join(commishes.ending_advice(done, page, ENDS + 60, brief=True, watched=watched))
        assert "ближе к концу" not in text and "смотрю ставки" not in text


def test_past_end_shows_the_date():
    a = commishes.parse_auction_json(json_with(time=ENDS + 3 * 86400))
    text = commishes.summary(a, None, ENDS + 3 * 86400)
    line = next(ln for ln in text.splitlines() if ln.startswith("Конец был"))
    assert line.startswith("Конец был ") and " в " in line
    day = __import__("datetime").datetime.fromtimestamp(ENDS)
    assert f"{day.day}.{day.month:02d} в " in line
    assert commishes.past_when(NOW - 60, NOW).startswith("в ") or "вчера" in commishes.past_when(NOW - 60, NOW)


@pytest.mark.parametrize("given,stored", [("ych.commishes", "commishes"), ("Commishes", "commishes"),
                                          ("cm", "commishes"), ("DA", "deviantart"), ("Twitter", "other")])
def test_sales_record_accepts_platform_names_she_uses(conn, given, stored):
    sid, _ = sales.record(conn, title="x", amount=5, currency="usd", platform=given, format="adopt")
    assert sales.get(conn, sid)["platform"] == stored
