"""Бережный доступ к DeviantArt: общий бюджет, общая пауза, ленты по кругу, API вместо RSS, условный GET."""

import json
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest
from fixtures import RSS

from mcp_devian import auctions, cli, da_access, da_api, da_feeds, da_market, db, evidence, monitor
from mcp_devian import deviantart as da
from mcp_devian.config import Config, DAGroup, DeviantArt, Notify
from mcp_devian.da_api import DAClient, TokenStore

NOW = 1_800_000_000.0
API = "/api/v1/oauth2"
FOLDER_UUID = "AAAA0000-1111-2222-3333-444455556666"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(da, "polite_pause", lambda: None)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def rss_http(calls, status=200, body=RSS, headers=None):
    def handler(req):
        calls.append(req)
        return httpx.Response(status, text=body, headers=headers or {})
    return httpx.Client(transport=httpx.MockTransport(handler))


def cfg(groups=1, me="", **kw):
    urls = [f"https://www.deviantart.com/group{i}/gallery/{100 + i}/auctions" for i in range(groups)]
    return Config(notify=Notify(market_lots=True), deviantart=DeviantArt(
        groups=[DAGroup(url=u) for u in urls], my_username=me, **kw))


# --- API-клиент на MockTransport ---

def dev(n, title, author="seller", published=NOW - 3600, comments=0):
    return {"deviationid": f"UUID-{n}", "url": f"https://www.deviantart.com/{author}/art/Lot-{n}", "title": title,
            "author": {"username": author}, "published_time": str(int(published)), "is_mature": False,
            "stats": {"comments": comments, "favourites": 2}, "thumbs": [{"src": "https://t.example/1.jpg"}]}


class Api:
    """Маршруты API: папка группы, её галерея, метаданные. Пишет пути всех запросов."""

    def __init__(self, folder=(), gallery=(), status=None, headers=None, folder_name="Auctions"):
        self.folder, self.gallery, self.status = list(folder), list(gallery), status
        self.headers, self.folder_name = headers or {}, folder_name
        self.calls: list[str] = []

    def __call__(self, req):
        path = req.url.path.removeprefix(API)
        self.calls.append(path)
        if self.status:
            return httpx.Response(self.status, text="blocked", headers=self.headers)
        if path == "/gallery/folders":
            return httpx.Response(200, json={"has_more": False,
                                             "results": [{"folderid": FOLDER_UUID, "name": self.folder_name}]})
        if path == f"/gallery/{FOLDER_UUID}":
            return httpx.Response(200, json={"has_more": False, "results": self.folder})
        if path == "/gallery/all":
            return httpx.Response(200, json={"has_more": False, "results": self.gallery})
        if path == "/deviation/metadata":
            ids = req.url.params.get_list("deviationids[]")
            return httpx.Response(200, json={"metadata": [
                {"deviationid": u, "description": "<p>Dragon auction. SB: $25 AB: $80 "
                                                  "https://www.deviantart.com/other-group/gallery/555/x</p>",
                 "tags": [{"tag_name": "dragonadopt"}], "stats": {}} for u in ids]})
        if path == "/comments/post/deviation/UUID-1":
            return httpx.Response(200, json={"commentid": "c1"})
        return httpx.Response(404, json={"error": "not_found"})


def api_client(tmp_path, api: Api) -> DAClient:
    store = TokenStore(tmp_path / "da-token.json")
    store.save({"access_token": "AT", "refresh_token": "RT", "expires_at": NOW + 10 * 86400})
    return DAClient("CID", store, http=httpx.Client(transport=httpx.MockTransport(api)), sleep=lambda s: None,
                    clock=lambda: NOW)


def cooldown(conn, kind="rss"):
    return conn.execute("SELECT * FROM da_cooldown WHERE kind = ?", (kind,)).fetchone()


# --- бюджет ---

def test_rss_budget_is_shared_by_all_callers(conn, monkeypatch):
    monkeypatch.setitem(da_access.PER_HOUR, "rss", 5)
    monkeypatch.setitem(da_access.LOW_RESERVE, "rss", 0)
    calls: list = []
    http = rss_http(calls)
    clock = Clock()
    with da_access.session(conn, clock=clock):
        monitor.check_deviantart(conn, cfg(groups=2), NOW, http=http)        # ленты: 2 запроса
        da.list_group_folders(http, "group0")                                  # страница группы: 3-й
        res = da_market.sample_market(conn, Config(), http=http, now=NOW, queries=["a", "b", "c"],
                                      pause=lambda: None)                    # поиск рынка: ещё 2 и стоп
        assert len(calls) == 5
        assert res.rate_limited and "лимит запросов к DeviantArt" in res.limit_note
        with pytest.raises(da.Throttled):
            da.fetch_feed(http, "gallery:x", "p")
        assert len(calls) == 5  # отказ — без запроса
        # через час окно сдвигается
        clock.t += 3601
        da.fetch_feed(http, "gallery:x", "p")
        assert len(calls) == 6
    assert not cooldown(conn)  # свой бюджет — не пауза от DeviantArt


def test_low_priority_leaves_reserve_for_feeds(conn, monkeypatch):
    monkeypatch.setitem(da_access.PER_HOUR, "rss", 4)
    monkeypatch.setitem(da_access.LOW_RESERVE, "rss", 2)
    calls: list = []
    http = rss_http(calls)
    with da_access.session(conn, clock=Clock()):
        with da_access.task("рынок", low=True):
            da.fetch_feed(http, "a", "p")
            da.fetch_feed(http, "b", "p")
            with pytest.raises(da.Throttled) as e:
                da.fetch_feed(http, "c", "p")
            assert e.value.why == "budget"
        res = monitor.check_deviantart(conn, cfg(groups=2), NOW, http=http)  # ленты берут резерв
    assert len(calls) == 4 and res.seen == 6 and not res.limited
    spent = {r["purpose"] for r in conn.execute("SELECT purpose FROM da_budget")}
    assert spent == {"рынок", "ленты"}


def test_api_budget_counts_every_read_and_spares_writes(conn, tmp_path, monkeypatch):
    monkeypatch.setitem(da_access.PER_HOUR, "api", 3)
    api = Api(gallery=[dev(1, "x")])
    client = api_client(tmp_path, api)
    with da_access.session(conn, clock=Clock()):
        client.gallery_all("me", max_pages=1)
        client.deviation_metadata(["UUID-1"])
        client.gallery_all("me", max_pages=1)
        with pytest.raises(da_api.Throttled):
            client.gallery_all("me", max_pages=1)
        assert len(api.calls) == 3
        # запись по её команде бюджетом не режется, но считается
        assert client.post_comment("UUID-1", "спасибо!")["commentid"] == "c1"
    assert conn.execute("SELECT COUNT(*) FROM da_budget WHERE kind = 'api'").fetchone()[0] == 4


def test_no_guard_means_no_limits(tmp_path):
    # Вне session() и без enable_default (тесты, разовые вызовы) ничего не считается.
    calls: list = []
    for _ in range(30):
        da.fetch_feed(rss_http(calls), "gallery:x", "p")
    assert len(calls) == 30


def test_default_guard_covers_callers_without_session(tmp_path, monkeypatch):
    monkeypatch.setitem(da_access.PER_HOUR, "rss", 2)
    path = tmp_path / "default.sqlite3"
    da_access.enable_default(path)
    try:
        calls: list = []
        http = rss_http(calls)
        da.fetch_feed(http, "a", "p")
        da.list_group_folders(http, "g")
        with pytest.raises(da.Throttled):
            da.fetch_feed(http, "b", "p")
        assert len(calls) == 2
    finally:
        da_access.disable_default()
    with db.session(path) as c:
        assert c.execute("SELECT COUNT(*) FROM da_budget").fetchone()[0] == 2


# --- пауза ---

def test_cooldown_escalates_caps_and_resets_after_clean_run(conn):
    clock = Clock()
    guard = da_access.Guard(conn, clock=clock)
    waits = []
    for _ in range(7):
        until = guard.note_limited("rss", 403)
        waits.append(until - clock.t)
        clock.t = until + 1  # пауза прошла, DeviantArt снова не пускает
    assert waits == [1800, 3600, 7200, 14400, 28800, 28800, 28800]
    # чистый проход: были удачные запросы, отказов не было — следующая пауза снова 30 минут
    calls: list = []
    with da_access.session(conn, clock=clock):
        da.fetch_feed(rss_http(calls), "gallery:x", "p")
    assert cooldown(conn)["level"] == 0
    assert da_access.Guard(conn, clock=clock).note_limited("rss", 429) - clock.t == 1800


def test_limit_inside_cooldown_does_not_escalate(conn):
    clock = Clock()
    guard = da_access.Guard(conn, clock=clock)
    first = guard.note_limited("api", 429)
    clock.t += 60
    assert guard.note_limited("api", 429) == first and cooldown(conn, "api")["level"] == 1


def test_level_forgotten_after_a_quiet_day(conn):
    clock = Clock()
    guard = da_access.Guard(conn, clock=clock)
    guard.note_limited("rss", 403)
    clock.t += 2 * 86400
    assert guard.note_limited("rss", 403) - clock.t == 1800


def test_failed_run_keeps_level(conn):
    clock = Clock()
    da_access.Guard(conn, clock=clock).note_limited("rss", 403)
    clock.t += 1801
    calls: list = []
    with da_access.session(conn, clock=clock), pytest.raises(da.RateLimited):
        da.fetch_feed(rss_http(calls, status=403, body=""), "gallery:x", "p")
    assert cooldown(conn)["level"] == 2 and cooldown(conn)["until_ts"] == clock.t + 3600


@pytest.mark.parametrize("header,expected", [
    ("7200", 7200),            # больше нашей ступени — ждём, сколько просят
    ("60", 1800),              # меньше — всё равно не раньше 30 минут
])
def test_retry_after_seconds(conn, header, expected):
    clock = Clock()
    calls: list = []
    with da_access.session(conn, clock=clock), pytest.raises(da.RateLimited):
        da.fetch_feed(rss_http(calls, status=429, body="", headers={"Retry-After": header}), "gallery:x", "p")
    assert cooldown(conn)["until_ts"] == pytest.approx(NOW + expected)


def test_retry_after_http_date_on_api(conn, tmp_path):
    at = datetime.fromtimestamp(NOW + 3 * 3600, tz=UTC)
    api = Api(status=429, headers={"Retry-After": format_datetime(at, usegmt=True)})
    client = api_client(tmp_path, api)
    with da_access.session(conn, clock=Clock()):
        with pytest.raises(da_api.RateLimited):
            client.gallery_all("me", max_pages=1)
        with pytest.raises(da_api.Throttled):  # дальше — ни одного запроса до конца паузы
            client.deviation_metadata(["UUID-1"])
    assert len(api.calls) == 1
    assert cooldown(conn, "api")["until_ts"] == pytest.approx(NOW + 3 * 3600, abs=1)


def test_api_403_block_vs_scope_error(conn, tmp_path):
    with da_access.session(conn, clock=Clock()), pytest.raises(da_api.RateLimited):
        api_client(tmp_path, Api(status=403)).gallery_all("me", max_pages=1)
    assert cooldown(conn, "api")["status"] == 403
    conn.execute("DELETE FROM da_cooldown")

    def scope(req):
        return httpx.Response(403, json={"error": "insufficient_scope", "error_description": "scope"})
    store = TokenStore(tmp_path / "t2.json")
    store.save({"access_token": "AT", "refresh_token": "RT", "expires_at": NOW + 86400})
    client = DAClient("CID", store, http=httpx.Client(transport=httpx.MockTransport(scope)), clock=lambda: NOW)
    with da_access.session(conn, clock=Clock()), pytest.raises(da_api.NeedsReconnect):
        client.gallery_all("me", max_pages=1)
    assert cooldown(conn, "api") is None  # нехватка прав — не повод для паузы


def test_cooldown_applies_to_every_rss_caller_and_no_retry_in_run(conn):
    calls: list = []
    blocked = rss_http(calls, status=403, body="")
    with da_access.session(conn, clock=Clock()):
        res = monitor.check_deviantart(conn, cfg(groups=3), NOW, http=blocked)
        assert len(calls) == 1  # после первого 403 остальные ленты в этом проходе не трогаем
        assert "лент не прочитано: 3" in res.limited and res.error is None
        sample = da_market.sample_market(conn, Config(), http=blocked, now=NOW, queries=["a"], pause=lambda: None)
        rows = da_market.group_scorecard_many(conn, blocked, Config(), groups=["g"], now=NOW, pause=lambda: None)
        assert sample.rate_limited and "притормозил" in rows[0]["error"]
    assert len(calls) == 1


def test_writes_go_through_during_api_cooldown(conn, tmp_path):
    api = Api()
    client = api_client(tmp_path, api)
    clock = Clock()
    da_access.Guard(conn, clock=clock).note_limited("api", 429)
    with da_access.session(conn, clock=clock):
        with pytest.raises(da_api.Throttled):
            client.gallery_all("me", max_pages=1)
        client.post_comment("UUID-1", "спасибо!")
    assert api.calls == ["/comments/post/deviation/UUID-1"]


# --- ленты по кругу ---

def feeds(n, own=False):
    out = [da.Feed(f"gallery:g{i}/{i}", f"g{i}", name="a") for i in range(n)]
    if own:
        out.append(da.Feed("gallery:me", da.MY_GALLERY, notify=False, own=True))
    return out


def test_pick_rotates_fairly():
    fs = feeds(7)
    last: dict = {}
    seen = []
    for run in range(5):
        chosen = da_feeds.pick(fs, last, set(), 3)
        assert len(chosen) == 3
        seen.append({f.query for f in chosen})
        for f in chosen:
            last[f.query] = NOW + run
    # за 3 прохода (9 мест) прочитаны все 7 лент, и ни одна не ждёт дольше остальных
    assert set().union(*seen[:3]) == {f.query for f in fs}


def test_pick_priority_but_one_slot_for_the_queue():
    fs = feeds(5, own=True)
    hot = {"gallery:me", "gallery:g0/0", "gallery:g1/1"}
    last: dict = {}
    reads = {f.query: 0 for f in fs}
    for run in range(6):
        chosen = da_feeds.pick(fs, last, hot, 3)
        assert len([f for f in chosen if f.query in hot]) == 2  # одно место всегда за очередью
        for i, f in enumerate(chosen):
            last[f.query] = NOW + run + i * 1e-3  # как monitor: порядок внутри прохода
            reads[f.query] += 1
    assert all(reads[q] >= 4 for q in hot)
    assert all(reads[f.query] >= 1 for f in fs)
    assert da_feeds.pick(fs, {}, hot, None) == fs  # без предела — все, в порядке настроек


def test_priority_keys_her_gallery_and_open_auctions(conn):
    fs = feeds(3, own=True)
    db.upsert_item(conn, db.Item(id="da:1", source="deviantart", title="My dragon auction [OPEN]", url="u",
                                 place="g1", own=True, lot_type="auction", status="open", published_ts=NOW - 86400,
                                 extra={"places": ["g1", "g2"]}), NOW)
    db.upsert_item(conn, db.Item(id="da:2", source="deviantart", title="old", url="u2", place="g0", own=True,
                                 lot_type="auction", status="closed", published_ts=NOW - 86400), NOW)
    assert da_feeds.priority_keys(conn, fs, NOW) == {"gallery:me", "gallery:g1/1", "gallery:g2/2"}


def test_scheduled_run_reads_three_feeds_and_rotates(conn):
    calls: list = []
    http = rss_http(calls)
    config = cfg(groups=6, me="me")
    with da_access.session(conn, clock=Clock()):
        queried = []
        for run in range(3):
            calls.clear()
            monitor.check_deviantart(conn, config, NOW + run * 900, http=http, max_feeds=3)
            queried.append([r.url.params["q"] for r in calls])
    assert all(len(q) == 3 for q in queried)
    assert all("gallery:me" in q for q in queried)          # её галерея — каждый проход
    others = [q for run in queried for q in run if q != "gallery:me"]
    assert len(set(others)) == 6                             # остальные 6 — по кругу, без повторов


def test_run_check_uses_rotation_only_when_scheduled(conn, monkeypatch):
    got = []
    monkeypatch.setattr(monitor, "check_deviantart",
                        lambda conn, cfg, now, **kw: got.append(kw["max_feeds"]) or monitor.SourceResult("deviantart"))
    monitor.run_check(conn, cfg(), sources=("deviantart",), force=True, now=NOW)
    monitor.run_check(conn, cfg(), sources=("deviantart",), force=True, now=NOW, interactive=True)
    assert got == [da_feeds.FEEDS_PER_RUN, da_feeds.MANUAL_FEEDS_PER_RUN]


# --- API вместо RSS ---

def test_api_first_maps_entries_like_rss(conn, tmp_path):
    api = Api(folder=[dev(1404100001, "Dragon adopt AUCTION [OPEN]", comments=3)],
              gallery=[dev(1404100002, "My moth auction [OPEN]", author="me")])
    rss_calls: list = []
    res = monitor.check_deviantart(conn, cfg(me="me"), NOW, http=rss_http(rss_calls),
                                   client=api_client(tmp_path, api), sleep=lambda s: None)
    assert rss_calls == []  # подключён API — RSS не трогаем
    assert (res.seen, res.new, res.error, res.limited) == (2, 2, None, None)
    item = db.get_item(conn, "da:1404100001")
    extra = json.loads(item["extra"])
    assert item["lot_type"] == "auction" and item["price_usd"] == 25 and item["author"] == "seller"
    assert item["published_ts"] == NOW - 3600 and item["matched"] == 1
    assert extra["uuid"] == "UUID-1404100001" and extra["tags"] == ["dragonadopt"]
    assert extra["folder_links"] == ["other-group/555"] and extra["thumb"] == "https://t.example/1.jpg"
    assert extra["places"] == ["group0: auctions"]
    mine = db.get_item(conn, "da:1404100002")
    assert mine["own"] == 1 and json.loads(mine["extra"])["uuid"] == "UUID-1404100002"
    # UUID папки нашёлся по названию из ссылки и запомнен
    assert conn.execute("SELECT uuid FROM da_folders WHERE grp = 'group0'").fetchone()["uuid"] == FOLDER_UUID
    snap = conn.execute("SELECT comments FROM deviation_stats WHERE deviation_id = 'da:1404100001'").fetchone()
    assert snap["comments"] == 3
    assert api.calls.count("/deviation/metadata") == 2 and api.calls.count("/gallery/folders") == 1

    # второй проход: UUID папки уже известен, описания свежие — по запросу на ленту
    api.calls.clear()
    again = monitor.check_deviantart(conn, cfg(me="me"), NOW + 900, http=rss_http(rss_calls),
                                     client=api_client(tmp_path, api), sleep=lambda s: None)
    assert again.new == 0 and api.calls == [f"/gallery/{FOLDER_UUID}", "/gallery/all"]
    assert db.get_item(conn, "da:1404100001")["price_usd"] == 25  # описание не стёрлось
    # папку, только что прочитанную через API, ставки не перечитывают
    assert FOLDER_UUID in da_feeds.fresh_api_folders(conn, NOW + 900)


def test_folder_pass_skips_folders_just_read_via_api(conn, monkeypatch):
    config = cfg()
    db.save_folders(conn, "group0", [da.Folder("group0", "100", "auctions", uuid=FOLDER_UUID)], NOW)
    da_feeds.mark_read(conn, "gallery:group0/100", NOW, "api", uuid=FOLDER_UUID)
    asked = []
    monkeypatch.setattr(auctions.da_browse, "folder_page", lambda *a, **k: asked.append(a) or [])
    auctions.folder_pass(conn, config, object(), NOW + 60)
    assert asked == []
    auctions.folder_pass(conn, config, object(), NOW + da_feeds.API_FRESH_S + 60)
    assert len(asked) == 1


def test_api_error_falls_back_to_rss_for_that_feed(conn, tmp_path):
    api = Api(folder_name="Something else")  # UUID папки не найти — эту ленту через RSS
    rss_calls: list = []
    res = monitor.check_deviantart(conn, cfg(), NOW, http=rss_http(rss_calls), client=api_client(tmp_path, api),
                                   sleep=lambda s: None)
    assert len(rss_calls) == 1 and res.seen == 3
    assert conn.execute("SELECT via FROM da_feeds").fetchone()["via"] == "rss"

    class Broken:
        def is_connected(self):
            return True

        def gallery_folders(self, group):
            raise da_api.DAApiError("DeviantArt не принял запрос: server_error", status=500)

    conn.execute("DELETE FROM da_folders")
    rss_calls.clear()
    monitor.check_deviantart(conn, cfg(), NOW + 900, http=rss_http(rss_calls), client=Broken(), sleep=lambda s: None)
    assert len(rss_calls) == 1


def test_not_connected_switches_to_rss(conn):
    class Gone:
        def is_connected(self):
            return True

        def _request(self, *a, **k):
            raise da_api.NotConnected("истекло")

        gallery_folders = _request

    rss_calls: list = []
    res = monitor.check_deviantart(conn, cfg(me="me"), NOW, http=rss_http(rss_calls), client=Gone(),
                                   sleep=lambda s: None)
    assert len(rss_calls) == 2 and res.seen == 6 and "через RSS" in res.notes[0]


def test_no_rss_hammering_after_api_429(conn, tmp_path):
    api = Api(status=429)
    db.save_folders(conn, "group0", [da.Folder("group0", "100", "auctions", uuid=FOLDER_UUID)], NOW)
    rss_calls: list = []
    with da_access.session(conn, clock=Clock()):
        res = monitor.check_deviantart(conn, cfg(groups=3), NOW, http=rss_http(rss_calls),
                                       client=api_client(tmp_path, api), sleep=lambda s: None)
    assert api.calls == [f"/gallery/{FOLDER_UUID}"] and rss_calls == []
    assert "притормозил API" in res.limited and "лент не прочитано: 3" in res.limited
    assert cooldown(conn, "api")["level"] == 1


def test_rss_cooldown_reads_via_api_and_says_so(conn, tmp_path):
    clock = Clock()
    da_access.Guard(conn, clock=clock).note_limited("rss", 403)
    api = Api(folder=[dev(1404100001, "Dragon adopt AUCTION [OPEN]")])
    rss_calls: list = []
    with da_access.session(conn, clock=clock):
        res = monitor.check_deviantart(conn, cfg(), NOW, http=rss_http(rss_calls), client=api_client(tmp_path, api),
                                       sleep=lambda s: None)
    assert rss_calls == [] and res.seen == 1
    until = da_access.until_text(NOW + 1800, NOW)
    assert res.limited == f"DeviantArt притормозил чтение лент до {until} — читаю через API"


def test_everything_paused_makes_no_requests_and_no_error_record(conn):
    clock = Clock()
    da_access.Guard(conn, clock=clock).note_limited("rss", 403)
    rss_calls: list = []
    with da_access.session(conn, clock=clock):
        res = monitor.check_deviantart(conn, cfg(), NOW, http=rss_http(rss_calls))
    assert rss_calls == [] and res.skipped == res.limited
    assert res.limited.endswith("подожду, ничего делать не нужно")
    assert db.last_run(conn, "deviantart") is None  # фоновая проверка не уходит в backoff по fail_count
    assert monitor.format_results([res]) == [f"deviantart: {res.limited}"]


# --- условный GET ---

def test_conditional_get_and_304(conn):
    seen_headers = []

    def handler(req):
        seen_headers.append(req.headers.get("If-None-Match"))
        if req.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text=RSS, headers={"ETag": '"v1"', "Last-Modified": "Sun, 01 Nov 2026 10:00:00 GMT"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with da_access.session(conn, clock=Clock()):
        first = monitor.check_deviantart(conn, cfg(), NOW, http=http)
        second = monitor.check_deviantart(conn, cfg(), NOW + 900, http=http)
    assert seen_headers == [None, '"v1"']
    assert first.new == 3 and second.seen == 0 and second.error is None
    row = conn.execute("SELECT * FROM da_feeds").fetchone()
    assert row["etag"] == '"v1"' and row["last_modified"].startswith("Sun") and row["last_read_ts"] == NOW + 900
    costs = [r["cost"] for r in conn.execute("SELECT cost FROM da_budget ORDER BY ts, rowid")]
    assert costs == [1.0, da_access.NOT_MODIFIED_COST]
    assert db.last_run(conn, "deviantart")["last_ok_ts"] == NOW + 900


def test_fetch_feed_304_skips_parsing():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(304, text="not xml")))
    valid = {"etag": '"x"'}
    assert da.fetch_feed(http, "gallery:g/1", "p", pages=3, validators=valid) == []
    assert valid["not_modified"] is True


# --- сообщения ---

def test_status_lines(conn):
    clock = Clock()
    assert da_access.status_line(conn, True, NOW) is None
    da_access.Guard(conn, clock=clock).note_limited("rss", 403)
    until = da_access.until_text(NOW + 1800, NOW)
    assert da_access.status_line(conn, True, NOW) == f"DeviantArt притормозил чтение лент до {until} — читаю через API"
    assert da_access.status_line(conn, False, NOW) == (f"DeviantArt притормозил чтение лент до {until} — "
                                                       "подожду, ничего делать не нужно")
    da_access.Guard(conn, clock=clock).note_limited("api", 429)
    assert "и ленты, и API" in da_access.status_line(conn, True, NOW)


def test_until_text_days():
    base = datetime(2026, 11, 1, 18, 0).timestamp()
    assert da_access.until_text(base + 40 * 60, base) == "18:40"
    assert da_access.until_text(base + 8 * 3600, base) == "завтра в 02:00"


def test_one_notification_per_cooldown_series(conn):
    clock = Clock()
    guard = da_access.Guard(conn, clock=clock)
    guard.note_limited("rss", 403)
    res = [monitor.SourceResult("deviantart", limited="DeviantArt притормозил чтение лент до 18:40 — подожду")]
    with da_access.session(conn, clock=clock):
        assert cli._problems({}, res, conn=conn) == ["deviantart: DeviantArt притормозил чтение лент до 18:40 — "
                                                     "подожду."]
        assert cli._problems({}, res, conn=conn) == []         # следующая проверка через 15 минут — тишина
        clock.t += 1801
        guard.note_limited("rss", 403)                          # ступень 2 той же серии — тоже тишина
        assert cli._problems({}, res, conn=conn) == []
        # свой бюджет часа — не повод для уведомления
        conn.execute("DELETE FROM da_cooldown")
        assert cli._problems({}, [monitor.SourceResult("deviantart", limited="лимит запросов")], conn=conn) == []
        # новая серия после чистого прохода — одно новое уведомление
        clock.t += 4000
        guard.note_limited("rss", 429)
        assert len(cli._problems({}, res, conn=conn)) == 1


def test_data_health_shows_budget_and_pause(conn):
    guard = da_access.Guard(conn)
    guard.record("rss")
    guard.record("api", 1.0)
    guard.note_limited("rss", 403)
    text = evidence.render_health(conn)
    assert "Запросы к DeviantArt за час: ленты 1 из 20, API 1 из 120" in text
    assert "DeviantArt притормозил чтение лент до" in text


def test_refresh_auctions_waits_out_api_cooldown(conn, monkeypatch):
    from mcp_devian import posting

    class Connected:
        def is_connected(self):
            return True

    monkeypatch.setattr(posting, "da_client", lambda cfg: Connected())
    monkeypatch.setattr(auctions, "refresh_via_api", lambda *a, **k: pytest.fail("API на паузе — не читать"))
    da_access.Guard(conn).note_limited("api", 429)
    lines, events = monitor.refresh_auctions(conn, Config(), NOW)
    assert events == [] and lines[0].startswith("ставки: DeviantArt притормозил API до")


def test_market_auctions_yield_budget_to_her_lots(conn, monkeypatch):
    """Чужие лоты (статистика) — низкий приоритет: бюджет кончился — пропускаем их, её лоты читаем."""
    monkeypatch.setitem(da_access.PER_HOUR, "api", 3)
    monkeypatch.setitem(da_access.LOW_RESERVE, "api", 2)
    for n, own in ((1, False), (2, True)):
        db.upsert_item(conn, db.Item(id=f"da:{n}", source="deviantart", title="auction", url=f"u{n}", own=own,
                                     lot_type="auction", status="open", published_ts=NOW - 3 * 86400,
                                     extra={"uuid": f"UUID-{n}"}), NOW)
    read = []

    class Client:
        def deviation_comments(self, uuid):
            block = da_access.before("api")
            if block:
                raise da_api.Throttled(block)
            da_access.after("api", 200)
            read.append(uuid)
            return []

    with da_access.session(conn, clock=Clock()):
        da_access.Guard(conn, clock=Clock()).record("api")  # уже потрачено до порога низкого приоритета
        stats: dict = {}
        auctions.refresh_via_api(conn, Config(), Client(), NOW, sleep=lambda s: None, stats=stats)
    assert read == ["UUID-2"] and stats["stopped"] is None
