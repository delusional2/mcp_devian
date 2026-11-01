from contextlib import contextmanager

import httpx
import pytest
from fixtures import BOARD, RSS, THREAD

from mcp_devian import db, digest, monitor
from mcp_devian import toyhouse as th
from mcp_devian.config import Config, DAGroup, DeviantArt, Filters, Notify, Toyhouse

NOW = 1_800_000_000.0


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(monitor.da, "polite_pause", lambda: None)
    monkeypatch.setattr(monitor.th, "human_pause", lambda *a, **k: None)


def fake_http(status=200, body=RSS):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, text=body)))


def da_cfg(**filters):
    # В этих тестах она «покупает»: чужие лоты становятся находками.
    return Config(filters=Filters(**filters), notify=Notify(market_lots=True), deviantart=DeviantArt(
        groups=[DAGroup(url="https://www.deviantart.com/test-group/gallery/123/auctions")]))


def test_deviantart_check_saves_and_matches(conn):
    res = monitor.check_deviantart(conn, da_cfg(), NOW, http=fake_http())
    assert (res.seen, res.new, res.new_matched, res.error) == (3, 3, 1, None)
    item = db.get_item(conn, "da:1404000001")
    assert item["lot_type"] == "auction" and item["price_usd"] == 10 and item["matched"] == 1
    assert db.get_item(conn, "da:1404000002")["matched"] == 0  # YCH
    assert db.get_item(conn, "da:1404000003")["matched"] == 0  # закрыт

    again = monitor.check_deviantart(conn, da_cfg(), NOW, http=fake_http())
    assert again.new == 0


def test_deviantart_rate_limit_recorded(conn):
    # 429 — не ошибка проверки, а общая пауза (da_access): одна понятная строка, ступень паузы в базе.
    res = monitor.check_deviantart(conn, da_cfg(), NOW, http=fake_http(429, ""))
    assert res.error is None and "притормозил чтение лент до" in res.limited
    assert conn.execute("SELECT level FROM da_cooldown WHERE kind = 'rss'").fetchone()["level"] == 1
    assert db.last_run(conn, "deviantart") is None  # ничего не прочитали — проход не записан


def test_deviantart_without_sources_is_skipped(conn):
    res = monitor.check_deviantart(conn, Config(), NOW, http=fake_http())
    assert res.skipped and not res.error


class FakeReader:
    def __init__(self, context, **kw):
        self.first_posts = 0

    def board(self, board_id, page, now):
        return th.parse_board(BOARD, board_id, now) if page == 1 else []

    def first_post(self, url):
        self.first_posts += 1
        return th.parse_first_post(THREAD)


@contextmanager
def fake_browser(headless=True):
    yield object()


def th_cfg(**kw):
    return Config(toyhouse=Toyhouse(mode="bot", boards=[13], pages_per_board=1, **kw))


def test_toyhouse_check(conn, monkeypatch):
    monkeypatch.setattr(monitor.th, "Reader", FakeReader)
    res = monitor.check_toyhouse(conn, th_cfg(), NOW, browser=fake_browser)
    assert res.error is None and res.seen == 5

    seeking = db.get_item(conn, "th:865492")
    assert seeking["wants_characters"] == 1 and seeking["matched"] == 1
    assert seeking["snippet"].startswith("Looking for dragon")
    assert "платят" in seeking["match_reason"]

    # Старый тред давно без активности.
    assert db.get_item(conn, "th:175858")["matched"] == 0
    # Продажи без ключевых слов для покупок не показываем.
    assert db.get_item(conn, "th:869446")["matched"] == 0


def test_toyhouse_lf_keywords_use_first_post(conn, monkeypatch):
    monkeypatch.setattr(monitor.th, "Reader", FakeReader)
    monitor.check_toyhouse(conn, th_cfg(lf_keywords=["wyvern"]), NOW, browser=fake_browser)
    # «wyvern» есть только в первом посте, не в названии.
    assert db.get_item(conn, "th:865492")["matched"] == 1
    assert db.get_item(conn, "th:864957")["matched"] == 0


def test_toyhouse_snippet_kept_between_runs(conn, monkeypatch):
    readers = []

    class Counting(FakeReader):
        def __init__(self, context, **kw):
            super().__init__(context)
            readers.append(self)

    monkeypatch.setattr(monitor.th, "Reader", Counting)
    monitor.check_toyhouse(conn, th_cfg(), NOW, browser=fake_browser)
    monitor.check_toyhouse(conn, th_cfg(), NOW + 60, browser=fake_browser)
    assert readers[0].first_posts > 0 and readers[1].first_posts == 0
    assert db.get_item(conn, "th:865492")["snippet"]


def test_toyhouse_sale_threads_with_buy_keywords(conn, monkeypatch):
    monkeypatch.setattr(monitor.th, "Reader", FakeReader)
    cfg = Config(filters=Filters(keywords=["dragon"]), toyhouse=Toyhouse(mode="bot", boards=[13], pages_per_board=1))
    monitor.check_toyhouse(conn, cfg, NOW, browser=fake_browser)
    sale = db.get_item(conn, "th:869446")
    assert sale["sells_characters"] == 1 and sale["matched"] == 1


def test_toyhouse_challenge(conn, monkeypatch):
    class Blocked(FakeReader):
        def board(self, *a):
            raise th.ChallengeError("cf")

    monkeypatch.setattr(monitor.th, "Reader", Blocked)
    res = monitor.check_toyhouse(conn, th_cfg(), NOW, browser=fake_browser)
    assert res.needs_login and res.error


def test_toyhouse_challenge_midway_keeps_first_board(conn, monkeypatch):
    class SecondBlocked(FakeReader):
        def board(self, board_id, page, now):
            if board_id == 5866:
                raise th.ChallengeError("cf")
            return super().board(board_id, page, now)

    monkeypatch.setattr(monitor.th, "Reader", SecondBlocked)
    cfg = Config(toyhouse=Toyhouse(mode="bot", boards=[13, 5866], pages_per_board=1))
    res = monitor.check_toyhouse(conn, cfg, NOW, browser=fake_browser)
    assert res.needs_login and res.seen == 5
    assert db.get_item(conn, "th:865492") is not None


def test_toyhouse_busy_is_skip(conn):
    @contextmanager
    def busy(headless=True):
        raise th.BrowserBusy("busy")
        yield

    res = monitor.check_toyhouse(conn, th_cfg(), NOW, browser=busy)
    assert res.skipped and not res.error


def test_due(conn):
    assert monitor.is_due(conn, "toyhouse", 45, NOW)
    db.record_run(conn, "toyhouse", None, NOW)
    assert not monitor.is_due(conn, "toyhouse", 45, NOW + 600)
    assert monitor.is_due(conn, "toyhouse", 45, NOW + 45 * 60)


def test_digest_flow(conn, monkeypatch):
    monkeypatch.setattr(monitor.th, "Reader", FakeReader)
    monitor.check_deviantart(conn, da_cfg(), NOW, http=fake_http())
    monitor.check_toyhouse(conn, th_cfg(), NOW, browser=fake_browser)
    rows = db.pending_digest(conn)
    md = digest.render_markdown(rows, NOW)
    assert "DeviantArt" in md and "ищут персов" in md
    assert "Moth dragon adopt" in md
    tg = digest.render_telegram(rows, NOW)
    assert tg and all(len(c) < 4096 for c in tg)

    db.mark_notified(conn, [r["id"] for r in rows])
    assert db.pending_digest(conn) == []

    db.set_dismissed(conn, ["da:1404000001"])
    assert not db.query_items(conn, source="deviantart")


def test_seller_default_market_lots_are_data_not_findings(conn):
    cfg = da_cfg()
    cfg.notify.market_lots = False
    res = monitor.check_deviantart(conn, cfg, NOW, http=fake_http())
    assert res.seen == 3 and res.new_matched == 0
    assert db.get_item(conn, "da:1404000001")["lot_type"] == "auction"  # для прогноза данные есть
    assert db.pending_digest(conn) == []


# --- Подбор LF-тредов: закрытые, её треды, оплата из поста, профиль ---

import json  # noqa: E402

import fixtures_th as fth  # noqa: E402

from mcp_devian.classify import classify_thread  # noqa: E402
from mcp_devian.config import Posting  # noqa: E402


@pytest.mark.parametrize("title", fth.CLOSED_TITLES + ["LF: dragons (found)"])
def test_match_thread_skips_closed_on_hold_found(title):
    kind = classify_thread(title)
    assert monitor.match_thread(Config(), kind, title, None, NOW - 60, NOW) == (None, "закрыт")


def test_match_thread_skips_her_own_thread():
    cfg = Config(posting=Posting(th_username="MeArtist"))
    kind = classify_thread("LF: dragons")
    assert monitor.match_thread(cfg, kind, "LF: dragons", None, NOW - 60, NOW, author="meartist") == (None, "её тред")
    assert monitor.match_thread(cfg, kind, "LF: dragons", None, NOW - 60, NOW, author="buyer")[0] == "lf"


def test_thread_item_pays_from_snippet_and_profile_stored():
    row = th.ThreadRow(board_id=13, thread_id="5", title="LF: dragons", url="https://toyhou.se/~forums/13.x/5.t",
                       author="buyer", posts=2, created_ts=NOW - 3600, activity_ts=NOW - 60, last_poster="buyer")
    item = monitor._thread_item(Config(), row, fth.LONG_FIRST_POST, NOW, snippet_ts=NOW)
    assert item.pays == "usd"  # «paying via paypal invoice only» — только из поста
    lf = item.extra["lf"]
    assert lf["budget"] == [40.0, "usd"] and "anthro" in lf["negated"] and lf["pays_only"] == "paypal"
    assert json.loads(json.dumps(lf)) == lf
    assert item.extra["thread_key"] == "buyer|dragons" and item.extra["snippet_ts"] == NOW
    assert "платят: usd" in item.match_reason


def test_thread_item_snippet_found_drops_thread():
    row = th.ThreadRow(board_id=13, thread_id="6", title="LF: dragons", url="https://toyhou.se/~forums/13.x/6.t",
                       author="buyer", posts=2, created_ts=NOW - 3600, activity_ts=NOW - 60, last_poster="buyer")
    item = monitor._thread_item(Config(), row, "FOUND, thank you everyone!", NOW)
    assert item.match_kind is None and item.match_reason == "закрыт"


# --- Листание досок ---

class PagedReader:
    def __init__(self, pages, endless=False):
        self.pages, self.endless, self.calls = pages, endless, []

    def board(self, board_id, page, now):
        self.calls.append((board_id, page))
        if self.endless:  # каждая страница свежая и новая
            return [th.ThreadRow(13, f"{page}{i}", f"LF thing {page}{i}", f"https://toyhou.se/~forums/13.x/{page}{i}.t",
                                 "a", 1, now - 60, now - 60 * page, "a") for i in range(3)]
        html = self.pages.get(page)
        return th.parse_board(html, th.board_id(board_id), now) if html else []


def paging_cfg(pages=1, top=6, boards=(13,)):
    return Config(toyhouse=Toyhouse(mode="bot", boards=list(boards), pages_per_board=pages, max_pages_per_board=top))


def test_quiet_board_reads_one_page():
    reader, res = PagedReader({1: fth.QUIET_PAGE, 2: fth.BOARD_PAGE2}), monitor.SourceResult("toyhouse")
    rows = monitor._read_boards(reader, paging_cfg(), NOW, res, since_ts=NOW - 2 * 3600)
    assert reader.calls == [(13, 1)] and len(rows) == 2


def test_busy_board_reads_until_known_activity():
    reader, res = PagedReader(fth.BUSY_PAGES), monitor.SourceResult("toyhouse")
    rows = monitor._read_boards(reader, paging_cfg(), NOW, res, since_ts=NOW - 2 * 3600)
    # страницы 1 и 2 свежее границы, на 3-й конец страницы уже старый — дальше не листаем.
    assert [p for _, p in reader.calls] == [1, 2, 3] and len(rows) == 8


def test_busy_board_capped_by_max_pages():
    reader = PagedReader({}, endless=True)
    monitor._read_boards(reader, paging_cfg(pages=2, top=4), NOW, monitor.SourceResult("toyhouse"), since_ts=NOW - 1e6)
    assert [p for _, p in reader.calls] == [1, 2, 3, 4]
    reader = PagedReader({}, endless=True)
    monitor._read_boards(reader, paging_cfg(pages=2, top=4), NOW, monitor.SourceResult("toyhouse"), since_ts=None)
    assert [p for _, p in reader.calls] == [1, 2]  # без границы — только обязательные страницы


def test_paging_pauses_between_pages(monkeypatch):
    pauses = []
    monkeypatch.setattr(monitor.th, "human_pause", lambda *a, **k: pauses.append(1))
    reader = PagedReader(fth.BUSY_PAGES)
    monitor._read_boards(reader, paging_cfg(boards=(13, "5866.design-marketplace")), NOW,
                         monitor.SourceResult("toyhouse"), since_ts=NOW - 2 * 3600)
    assert len(pauses) == len(reader.calls) - 1
    assert (("5866.design-marketplace", 1)) in reader.calls


def test_board_since_uses_last_ok_run(conn):
    cfg = paging_cfg()
    assert monitor.board_since(conn, cfg, NOW) == NOW - cfg.toyhouse.active_days * 86400
    db.record_run(conn, "toyhouse", None, NOW - 7200)
    assert monitor.board_since(conn, cfg, NOW) == NOW - 7200 - monitor.SINCE_OVERLAP_S


# --- Чтение первых постов ---

class PostReader:
    def __init__(self):
        self.read = []

    def first_post(self, url):
        self.read.append(url)
        return f"post of {url}"


def lf_row(tid, title, posts, created_ago, activity_ago=600):
    return th.ThreadRow(13, tid, title, f"https://toyhou.se/~forums/13.x/{tid}.t", "buyer" + tid, posts,
                        NOW - created_ago, NOW - activity_ago, "x")


def snippet_cfg(budget=2):
    chars = [{"name": "Ember", "url": "https://toyhou.se/1.e", "price": "$40", "tags": ["wolf"]}]
    return Config(toyhouse=Toyhouse(mode="bot", boards=[13], read_first_posts=budget),
                  posting=Posting(characters=chars))


def test_snippet_budget_goes_to_best_fit_then_fewest_posts(conn):
    rows = [lf_row("1", "LF: cats", 1, 3600), lf_row("2", "LF: wolves", 30, 7200),
            lf_row("3", "LF: canines", 2, 9000), lf_row("4", "LF: OCs", 1, 600)]
    reader = PostReader()
    got = monitor._fetch_snippets(conn, reader, snippet_cfg(budget=2), rows, NOW, monitor.SourceResult("toyhouse"))
    # Волк подходит к «wolves» и «canines» (по 40): из них сначала тот, где меньше постов.
    assert [u.rsplit("/", 1)[1] for u in reader.read] == ["3.t", "2.t"]
    assert got["3"] == ("post of https://toyhou.se/~forums/13.x/3.t", NOW) and got["1"] == (None, None)


def test_snippet_reread_after_a_day_only_for_fitting_threads(conn):
    old = NOW - 30 * 3600
    for tid, title in (("7", "LF: wolves"), ("8", "LF: cats")):
        item = th.row_to_item(lf_row(tid, title, 2, 3600))
        item.snippet, item.extra["snippet_ts"] = "old text", old
        db.upsert_item(conn, item, old)
    reader = PostReader()
    got = monitor._fetch_snippets(conn, reader, snippet_cfg(budget=5),
                                  [lf_row("7", "LF: wolves", 2, 3600), lf_row("8", "LF: cats", 2, 3600)],
                                  NOW, monitor.SourceResult("toyhouse"))
    assert [u.rsplit("/", 1)[1] for u in reader.read] == ["7.t"]
    assert got["7"][1] == NOW and got["8"] == ("old text", old)
    # Свежий (меньше суток) не перечитываем.
    reader = PostReader()
    monitor._fetch_snippets(conn, reader, snippet_cfg(budget=5), [lf_row("7", "LF: wolves", 2, 3600)],
                            NOW - 29 * 3600, monitor.SourceResult("toyhouse"))
    assert reader.read == []


# --- DeviantArt: флаг «несколько лотов в посте» ---

def test_check_deviantart_stores_multi(conn, monkeypatch):
    real = monitor.classify_lot

    def with_multi(*a, **k):
        lot = real(*a, **k)
        lot.multi = "batch" in a[0].lower() or "adopt" in a[0].lower()
        return lot

    monkeypatch.setattr(monitor, "classify_lot", with_multi)
    monitor.check_deviantart(conn, da_cfg(), NOW, http=fake_http())
    extras = [json.loads(r["extra"] or "{}") for r in conn.execute("SELECT extra FROM items WHERE source = 'deviantart'")]
    assert extras and all("multi" in e for e in extras) and any(e["multi"] for e in extras)
