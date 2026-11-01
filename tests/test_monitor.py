from contextlib import contextmanager

import httpx
import pytest
from fixtures import BOARD, RSS, THREAD

from mcp_devian import db, digest, monitor
from mcp_devian import toyhouse as th
from mcp_devian.config import Config, DAGroup, DeviantArt, Filters, Toyhouse

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
    return Config(filters=Filters(**filters), deviantart=DeviantArt(
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
    res = monitor.check_deviantart(conn, da_cfg(), NOW, http=fake_http(429, ""))
    assert "429" in res.error
    assert db.last_run(conn, "deviantart")["last_error"] == res.error


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
    return Config(toyhouse=Toyhouse(boards=[13], pages_per_board=1, **kw))


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
    cfg = Config(filters=Filters(keywords=["dragon"]), toyhouse=Toyhouse(boards=[13], pages_per_board=1))
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
    cfg = Config(toyhouse=Toyhouse(boards=[13, 5866], pages_per_board=1))
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
