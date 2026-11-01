"""Регрессии по ревью бережного доступа: прочитанное до отказа не пропадает, листание по делу,
пауза при занятой базе, UUID папки, порядок проходов ставок."""

import json
import sqlite3
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest
from fixtures import GROUP_GALLERY_PAGE
from test_da_access import API, FOLDER_UUID, NOW, Api, Clock, api_client, cfg, cooldown, dev, rss_http

from mcp_devian import auctions, da_access, da_api, da_browse, da_feeds, da_market, db, monitor
from mcp_devian import deviantart as da
from mcp_devian.config import Config, DAGroup, DeviantArt


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(da, "polite_pause", lambda: None)
    monkeypatch.setattr(da_access, "_unsaved_cooldown", {})


def _rfc(ts):
    return format_datetime(datetime.fromtimestamp(ts, tz=UTC), usegmt=True)


def rss_pages(calls, pages, status_after=None):
    """Лента из нескольких страниц: pages = [[(id, ts), …], …]; status_after — с какой страницы 403."""
    def handler(req):
        calls.append(req)
        n = int(req.url.params.get("offset") or 0) // 60
        if status_after is not None and n >= status_after:
            return httpx.Response(403, text="")
        nxt = (f'<atom:link rel="next" href="{da.RSS_URL}?type=deviation&amp;q=x&amp;offset={(n + 1) * 60}"/>'
               if n + 1 < len(pages) else "")
        items = "".join(
            f"<item><title>Dragon auction {i} [OPEN]</title>"
            f"<link>https://www.deviantart.com/seller/art/Lot-{i}</link><pubDate>{_rfc(ts)}</pubDate>"
            f"<media:description>SB: $10 AB: $50</media:description></item>" for i, ts in pages[n])
        return httpx.Response(200, text='<?xml version="1.0"?><rss version="2.0" '
                              'xmlns:atom="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">'
                              f'<channel>{nxt}{items}</channel></rss>')
    return httpx.Client(transport=httpx.MockTransport(handler))


def page_ids(start, n=60, ts=NOW - 3600):
    return [(start + k, ts) for k in range(n)]


def spend(conn, kind, n):
    guard = da_access.Guard(conn, clock=Clock())
    for _ in range(n):
        guard.record(kind)


# --- 1. Отказ на 2-й странице не выбрасывает уже прочитанное ---

def read_before(conn, key="gallery:group0/100"):
    """Лента уже читалась (час назад, глубоко): обычное чтение листает, пока на странице всё новое."""
    da_feeds.mark_read(conn, key, NOW - 3600, "rss")
    da_feeds.mark_deep(conn, key, NOW - 3600)


def test_rss_budget_cut_mid_feed_keeps_pages_read(conn):
    spend(conn, "rss", 18)  # осталось 2 запроса на час
    read_before(conn)
    calls: list = []
    http = rss_pages(calls, [page_ids(1000), page_ids(2000), page_ids(3000)])
    with da_access.session(conn, clock=Clock()):
        res = monitor.check_deviantart(conn, cfg(), NOW, http=http)
    assert len(calls) == 2                   # третья страница уже не ушла
    assert res.seen == 120 and res.new == 120  # две прочитанные страницы сохранены
    row = conn.execute("SELECT * FROM da_feeds").fetchone()
    assert row["last_read_ts"] == NOW and row["deep_ts"] == NOW - 3600  # прочитана, но не «глубоко»
    assert "лимит запросов" in res.limited


def test_fetch_feed_partial_only_with_state():
    calls: list = []
    http = rss_pages(calls, [page_ids(1), page_ids(100)], status_after=1)
    state: dict = {}
    got = da.fetch_feed(http, "gallery:g/1", "p", pages=3, state=state, deep=False)
    assert len(got) == 60 and isinstance(state["cut"], da.RateLimited)
    with pytest.raises(da.RateLimited):  # без state — как раньше
        da.fetch_feed(http, "gallery:g/1", "p", pages=3, deep=False)


class PagedApi(Api):
    """Папка группы на несколько страниц (has_more/next_offset)."""

    def __init__(self, pages, **kw):
        super().__init__(**kw)
        self.pages = pages

    def __call__(self, req):
        path = req.url.path.removeprefix(API)
        if path == f"/gallery/{FOLDER_UUID}":
            self.calls.append(path)
            n = int(req.url.params.get("offset") or 0) // da_feeds.API_PAGE
            more = n + 1 < len(self.pages)
            return httpx.Response(200, json={"has_more": more, "next_offset": (n + 1) * da_feeds.API_PAGE if more
                                             else None, "results": self.pages[n]})
        return super().__call__(req)


def api_page(start, n=24, published=NOW - 3600):
    return [dev(start + k, f"Dragon auction {start + k} [OPEN]", published=published) for k in range(n)]


def known_folder(conn):
    db.save_folders(conn, "group0", [da.Folder("group0", "100", "auctions", uuid=FOLDER_UUID)], NOW)


def test_api_budget_cut_on_metadata_keeps_entries(conn, tmp_path, monkeypatch):
    monkeypatch.setitem(da_access.PER_HOUR, "api", 2)
    monkeypatch.setitem(da_access.LOW_RESERVE, "api", 0)
    known_folder(conn)
    read_before(conn)
    api = PagedApi([api_page(100), api_page(200), api_page(300)])
    rss_calls: list = []
    with da_access.session(conn, clock=Clock()):
        res = monitor.check_deviantart(conn, cfg(), NOW, http=rss_http(rss_calls),
                                       client=api_client(tmp_path, api), sleep=lambda s: None)
    assert api.calls == [f"/gallery/{FOLDER_UUID}"] * 2  # 3-я страница и метаданные не ушли
    assert res.new == 48 and conn.execute("SELECT via FROM da_feeds").fetchone()["via"] == "api"
    extra = json.loads(db.get_item(conn, "da:100")["extra"])
    assert "desc_ts" not in extra  # описание дочитаем в следующий раз
    assert rss_calls == []  # одна лента, и она прочитана


# --- 2. 403 при занятой базе всё равно включает паузу ---

def test_cooldown_survives_locked_database(conn, monkeypatch):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(da_access.Guard, "note_limited", locked)
    monkeypatch.setattr(da_access.Guard, "record", locked)
    calls: list = []
    http = rss_http(calls, status=403, body="")
    clock = Clock()
    with da_access.session(conn, clock=clock):
        with pytest.raises(da.RateLimited):
            da.fetch_feed(http, "gallery:x", "p")
        with pytest.raises(da.Throttled) as e:  # пауза в памяти: следующий запрос не уходит
            da.fetch_feed(http, "gallery:y", "p")
        assert e.value.why == "cooldown" and e.value.block.until_ts == NOW + 1800
        clock.t += 1801
        with pytest.raises(da.RateLimited):
            da.fetch_feed(http, "gallery:z", "p")  # пауза прошла — снова можно
    assert len(calls) == 2


def test_after_writes_pause_before_count(conn, monkeypatch):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(da_access.Guard, "record", locked)
    with da_access.session(conn, clock=Clock()), pytest.raises(da.RateLimited):
        da.fetch_feed(rss_http([], status=429, body=""), "gallery:x", "p")
    assert cooldown(conn)["level"] == 1 and not da_access._unsaved_cooldown


# --- 3. Метаданные: пачка пополам на ошибке 0, без ext_stats для описаний ---

class MetaClient:
    def __init__(self, max_ids=10):
        self.max_ids, self.sizes, self.params = max_ids, [], []

    def _request(self, method, path, params=None, **kw):
        ids = [v for k, v in params if k == "deviationids[]"]
        self.params.append(params)
        if len(ids) > self.max_ids:
            raise da_api.DAApiError("Requested too many deviations", 0, status=400)
        self.sizes.append(len(ids))
        return {"metadata": [{"deviationid": u, "description": "SB: $5"} for u in ids]}


def test_metadata_halves_batch_on_error_zero():
    client = MetaClient(max_ids=10)
    out = da_browse.metadata(client, [f"U{i}" for i in range(30)], sleep=lambda s: None)
    assert len(out) == 30 and max(client.sizes) <= 10


def test_fill_text_asks_without_ext_stats(conn):
    client = MetaClient(max_ids=50)
    entries = [{"deviation_id": "1", "uuid": "U1", "text": "", "tags": [], "author": "a"}]
    da_feeds.fill_text(conn, client, entries, NOW, sleep=lambda s: None)
    assert ("ext_stats", "true") not in client.params[0] and entries[0]["text"] == "SB: $5"


# --- 4. Промах UUID папки в API не мешает найти его на странице группы ---

def _da_adopts_cfg():
    return Config(deviantart=DeviantArt(groups=[DAGroup(
        url="https://www.deviantart.com/da-adopts/gallery/26780440/renamed-folder")]))


def test_api_folder_miss_does_not_block_group_page_lookup(conn, tmp_path):
    config = _da_adopts_cfg()
    feed = da.feeds_from_config(config.deviantart.groups, [], "")[0]
    api = Api(folder_name="Auctions")  # в API папка называется иначе, чем в ссылке
    client = api_client(tmp_path, api)
    with pytest.raises(da_feeds.NoFolderUuid):
        da_feeds.folder_uuid(conn, client, feed, NOW)
    assert conn.execute("SELECT COUNT(*) FROM da_folders").fetchone()[0] == 0  # промах — не в da_folders
    with pytest.raises(da_feeds.NoFolderUuid):
        da_feeds.folder_uuid(conn, client, feed, NOW + 86400)
    assert api.calls == ["/gallery/folders"]  # промах помним неделю
    # страница группы (поиск рынка) находит UUID — и лента сразу идёт через API
    pages: list = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: pages.append(r) or
                                                      httpx.Response(200, text=GROUP_GALLERY_PAGE)))
    assert da_market.resolve_folders(conn, config, http, NOW + 3600) == 1
    assert da_feeds.folder_uuid(conn, client, feed, NOW + 7200) == "A4A80DA9-DEA9-2A1E-26EB-EB72E5F164BC"


def test_group_page_without_uuid_rechecked_after_a_week(conn):
    config = _da_adopts_cfg()
    db.save_folders(conn, "da-adopts", [da.Folder("da-adopts", "26780440", "x")], NOW)  # страница была, UUID нет
    calls: list = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or
                                                      httpx.Response(200, text=GROUP_GALLERY_PAGE)))
    assert da_market.resolve_folders(conn, config, http, NOW + 86400) == 0
    assert da_market.resolve_folders(conn, config, http, NOW + 8 * 86400) == 1


def test_gallery_folders_error_remembered_as_miss(conn):
    asked = []

    class Renamed:
        def gallery_folders(self, group):
            asked.append(group)
            raise da_api.DAApiError("group not found", "invalid_request", status=400)

    feed = da.Feed("gallery:group0/100", "g", name="auctions")
    for t in (NOW, NOW + 900):
        with pytest.raises(da_api.DAApiError if t == NOW else da_feeds.NoFolderUuid):
            da_feeds.folder_uuid(conn, Renamed(), feed, t)
    assert asked == ["group0"]


# --- 5. Условный GET не прячет закрытия ниже первой страницы ---

def test_deep_read_skips_conditional_get(conn):
    sent = []

    def handler(req):
        sent.append(req.headers.get("If-None-Match"))
        if req.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text=rss_body(), headers={"ETag": '"v1"'})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with da_access.session(conn, clock=Clock()):
        for dt in (0, 900, da_feeds.DEEP_EVERY_S + 60, da_feeds.DEEP_EVERY_S + 960):
            monitor.check_deviantart(conn, cfg(), NOW + dt, http=http)
    # первое чтение — глубокое, потом условный GET, через 6 ч снова глубокое (без If-None-Match)
    assert sent == [None, '"v1"', None, '"v1"']


def rss_body():
    calls: list = []
    http = rss_pages(calls, [page_ids(1, n=3)])
    return http.get(da.RSS_URL).text


# --- 6. Мелочи ---

def test_desc_ts_survives_rss_reread(conn):
    item = db.Item(id="da:1", source="deviantart", title="t", url="u", extra={"desc": "x", "desc_ts": NOW})
    db.upsert_item(conn, item, NOW)
    db.upsert_item(conn, db.Item(id="da:1", source="deviantart", title="t", url="u", extra={"desc": "x"}), NOW + 60)
    assert json.loads(db.get_item(conn, "da:1")["extra"])["desc_ts"] == NOW


def test_rss_read_marks_description_fresh(conn, tmp_path):
    """После чтения через RSS (полное описание) API-чтение той же ленты не тратит запрос на метаданные."""
    calls: list = []
    monitor.check_deviantart(conn, cfg(), NOW, http=rss_pages(calls, [[(1404100001, NOW - 3600)]]))
    known_folder(conn)
    api = Api(folder=[dev(1404100001, "Dragon auction [OPEN]")])
    monitor.check_deviantart(conn, cfg(), NOW + 900, http=rss_http([]), client=api_client(tmp_path, api),
                             sleep=lambda s: None)
    assert api.calls == [f"/gallery/{FOLDER_UUID}"]


@pytest.mark.parametrize("per_hour,api_calls", [
    (1, [f"/gallery/{FOLDER_UUID}"]),                          # отказ на метаданных 1-й ленты
    (2, [f"/gallery/{FOLDER_UUID}", "/deviation/metadata"]),   # отказ на первом запросе 2-й ленты
])
def test_api_own_budget_falls_back_to_rss(conn, tmp_path, monkeypatch, per_hour, api_calls):
    monkeypatch.setitem(da_access.PER_HOUR, "api", per_hour)
    monkeypatch.setitem(da_access.LOW_RESERVE, "api", 0)
    known_folder(conn)
    api = Api(folder=[dev(1404100001, "Dragon auction [OPEN]")])
    rss_calls: list = []
    with da_access.session(conn, clock=Clock()):
        res = monitor.check_deviantart(conn, cfg(groups=2), NOW, http=rss_pages(rss_calls, [page_ids(5000, n=2)]),
                                       client=api_client(tmp_path, api), sleep=lambda s: None)
    # 1-я лента — через API, 2-я — через RSS: свой бюджет API — не пауза DeviantArt
    assert api.calls == api_calls and len(rss_calls) == 1
    assert res.seen == 3 and cooldown(conn, "api") is None


def test_low_priority_budget_message_names_its_limit():
    block = da_access.Block("rss", "budget", NOW, limit=14)
    assert "лент: 14 в час, для фоновой работы" in block.message
    assert "лент: 20 в час)" in da_access.Block("rss", "budget", NOW).message


# --- Листание: обычно одна страница, глубже — раз в 6 часов ---

def test_shallow_read_stops_at_first_known(conn, tmp_path):
    known_folder(conn)
    api = PagedApi([api_page(100), api_page(200), api_page(300)])
    client = api_client(tmp_path, api)
    with da_access.session(conn, clock=Clock()):
        monitor.check_deviantart(conn, cfg(), NOW, http=rss_http([]), client=client, sleep=lambda s: None)
        assert api.calls.count(f"/gallery/{FOLDER_UUID}") == 3  # первое чтение — всё новое
        api.calls.clear()
        api.pages = [api_page(50, n=2) + api_page(100, n=22), api_page(122), api_page(146)]
        res = monitor.check_deviantart(conn, cfg(), NOW + 900, http=rss_http([]), client=client,
                                       sleep=lambda s: None)
    # знакомое на первой странице — дальше не листаем, хотя всё свежее 3 дней
    assert api.calls == [f"/gallery/{FOLDER_UUID}", "/deviation/metadata"] and res.new == 2


def test_new_feeds_take_one_page_until_a_deep_read_fits(conn, monkeypatch):
    """Свежая установка: все ленты новые и «всё новое» — не листаем их все сразу до 3 страниц."""
    monkeypatch.setitem(da_feeds.DEEP_KEEP, "rss", 18)  # глубокому чтению сейчас не хватает бюджета
    calls: list = []
    http = rss_pages(calls, [page_ids(1000), page_ids(2000), page_ids(3000)])
    with da_access.session(conn, clock=Clock()):
        res = monitor.check_deviantart(conn, cfg(groups=3), NOW, http=http, max_feeds=3)
    assert len(calls) == 3 and res.seen == 180  # по странице на ленту


def test_deep_read_skipped_when_budget_is_short(conn):
    spend(conn, "rss", 18)
    calls: list = []
    known = page_ids(1000)
    http = rss_pages(calls, [known, page_ids(2000)])
    for i, _ts in known:
        db.upsert_item(conn, db.Item(id=f"da:{i}", source="deviantart", title="t", url="u"), NOW - 7200)
    with da_access.session(conn, clock=Clock()):
        monitor.check_deviantart(conn, cfg(), NOW, http=http)
    assert len(calls) == 1  # глубокое чтение подождёт, бюджет на него не тратим
    assert conn.execute("SELECT deep_ts FROM da_feeds").fetchone()["deep_ts"] is None


def test_api_order_guard_falls_back_to_rss(conn, tmp_path):
    known_folder(conn)
    db.upsert_item(conn, db.Item(id="da:9", source="deviantart", title="t", url="u", place="group0: auctions",
                                 published_ts=NOW - 3600), NOW)
    old = PagedApi([api_page(100, published=NOW - 90 * 86400), api_page(200, published=NOW - 91 * 86400)])
    rss_calls: list = []
    client = api_client(tmp_path, old)
    monitor.check_deviantart(conn, cfg(), NOW, http=rss_pages(rss_calls, [page_ids(5000, n=1)]), client=client,
                             sleep=lambda s: None)
    assert len(rss_calls) == 1 and old.calls == [f"/gallery/{FOLDER_UUID}"]
    old.calls.clear()
    monitor.check_deviantart(conn, cfg(), NOW + 900, http=rss_pages(rss_calls, [page_ids(5000, n=1)]),
                             client=client, sleep=lambda s: None)
    assert old.calls == [] and len(rss_calls) == 2  # неделю эта лента — через RSS


# --- Ставки: её лоты первыми, страницы папок — фоновая работа ---

class BidClient:
    def __init__(self):
        self.calls = []

    def _call(self, what):
        block = da_access.before("api")
        if block:
            raise da_api.Throttled(block)
        da_access.after("api", 200)
        self.calls.append(what)

    def _request(self, method, path, params=None, **kw):
        self._call(path)
        return {"results": []}

    def deviation_comments(self, uuid):
        self._call(uuid)
        return []


def _bid_setup(conn):
    db.upsert_item(conn, db.Item(id="da:2", source="deviantart", title="auction", url="u2", own=True,
                                 lot_type="auction", status="open", published_ts=NOW - 86400,
                                 extra={"uuid": "UUID-own"}), NOW)
    db.save_folders(conn, "x-group", [da.Folder("x-group", "1", "a", uuid="F1")], NOW)
    return Config(deviantart=DeviantArt(groups=[DAGroup(url="https://www.deviantart.com/x-group/gallery/1")]))


def test_her_lots_before_folder_pages(conn):
    config = _bid_setup(conn)
    client = BidClient()
    with da_access.session(conn, clock=Clock()):
        auctions.refresh_via_api(conn, config, client, NOW, sleep=lambda s: None)
    assert client.calls == ["UUID-own", "/gallery/F1"]


def test_folder_pages_are_low_priority(conn, monkeypatch):
    monkeypatch.setitem(da_access.PER_HOUR, "api", 3)
    monkeypatch.setitem(da_access.LOW_RESERVE, "api", 2)
    config = _bid_setup(conn)
    client = BidClient()
    with da_access.session(conn, clock=Clock()):
        stats: dict = {}
        auctions.refresh_via_api(conn, config, client, NOW, sleep=lambda s: None, stats=stats)
    assert client.calls == ["UUID-own"] and stats["stopped"] is None  # папка — в следующий раз, не ошибка


def test_folder_pass_rotates_by_oldest_feed_read(conn, monkeypatch):
    urls = [f"https://www.deviantart.com/g{i}/gallery/{i}" for i in range(8)]
    config = Config(deviantart=DeviantArt(groups=[DAGroup(url=u) for u in urls]))
    for i in range(8):
        db.save_folders(conn, f"g{i}", [da.Folder(f"g{i}", str(i), "a", uuid=f"F{i}")], NOW)
        # первые в настройках прочитаны недавно, последние — давно
        da_feeds.mark_read(conn, f"gallery:g{i}/{i}", NOW - 3600 * (i + 1), "api", uuid=f"F{i}")
    asked = []
    monkeypatch.setattr(auctions.da_browse, "folder_page", lambda c, g, f, **k: asked.append(f) or [])
    auctions.folder_pass(conn, config, object(), NOW)
    assert asked == ["F7", "F6", "F5", "F4", "F3", "F2"]
