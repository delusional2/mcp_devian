import pytest
from fixtures import BOARD, CHALLENGE

from mcp_devian import toyhouse as th


class FakePage:
    """Сначала показывает проверку, после `solve_after` опросов — нормальную страницу."""

    def __init__(self, solve_after=None):
        self.polls = 0
        self.solve_after = solve_after
        self.fronted = False

    def _solved(self):
        return self.solve_after is not None and self.polls >= self.solve_after

    def goto(self, url, **kw):
        pass

    def query_selector(self, sel):
        self.polls += 1
        return object() if self._solved() else None

    def content(self):
        return BOARD if self._solved() else CHALLENGE

    def title(self):
        return "Adoption Center" if self._solved() else "Just a moment..."

    def bring_to_front(self):
        self.fronted = True

    def screenshot(self, path):
        pass


class FakeContext:
    def __init__(self, page):
        self.pages = [page]


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch, tmp_path):
    clock = {"t": 0.0}
    monkeypatch.setattr(th.time, "time", lambda: clock["t"])
    monkeypatch.setattr(th.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(th, "DATA_DIR", tmp_path)


def test_interactive_waits_for_human():
    page = FakePage(solve_after=60)  # «нажала галочку» через минуту
    asked = []
    reader = th.Reader(FakeContext(page), interactive=True, on_challenge=lambda: asked.append(1))
    rows = reader.board(13, 1, now=1_800_000_000)
    assert rows and asked == [1] and page.fronted


def test_scheduled_gives_up_on_challenge():
    page = FakePage(solve_after=None)
    reader = th.Reader(FakeContext(page), interactive=False)
    with pytest.raises(th.ChallengeError):
        reader.board(13, 1, now=1_800_000_000)
    assert page.polls <= 26


def test_playwright_errors_become_toyhouse_errors():
    from playwright.sync_api import Error, TimeoutError

    class Broken(FakePage):
        def __init__(self, exc):
            super().__init__()
            self.exc = exc

        def goto(self, url, **kw):
            raise self.exc

    with pytest.raises(th.ToyhouseError):
        th.Reader(FakeContext(Broken(TimeoutError("Timeout 25000ms exceeded")))).board(13, 1, now=0)
    with pytest.raises(th.BrowserClosed):
        th.Reader(FakeContext(Broken(Error("Target page, context or browser has been closed")))).board(13, 1, now=0)


def test_interactive_gives_up_after_wait():
    reader = th.Reader(FakeContext(FakePage(solve_after=None)), interactive=True, challenge_wait_s=30)
    with pytest.raises(th.ChallengeError):
        reader.board(13, 1, now=1_800_000_000)


# --- Разбор доски и первого поста ---

def test_first_post_reads_2500_chars():
    from fixtures_th import LONG_THREAD_HTML

    text = th.parse_first_post(LONG_THREAD_HTML)
    assert "Budget: $15-$40" in text and "paypal invoice only" in text
    assert len(th.parse_first_post("<div class='forum-post'><div class='user-content'>" + "a " * 3000 + "</div></div>")) == 2500
    assert ".slice(0, 2500)" in th.EXTRACT_POST_JS


def test_board_rows_keep_raw_class_and_lock():
    from fixtures_th import board_page, row

    html = board_page(row(1, "LF: cats", "a", 3600, 2, 60, "a", css="row forum-thread-row sticky", locked=True),
                      row(2, "LF: dogs", "b", 3600, 2, 60, "b"))
    rows = th.parse_board(html, 13, 1_800_000_000)
    assert rows[0].row_class == "row forum-thread-row sticky" and rows[0].row_locked is True
    assert rows[1].row_locked is False
    item = th.row_to_item(rows[0])
    assert item.extra["row_class"] == "row forum-thread-row sticky" and item.extra["row_locked"] is True
    assert "row_class" in th.EXTRACT_BOARD_JS and "row_locked" in th.EXTRACT_BOARD_JS


def test_abbr_title_is_fallback_for_time():
    from fixtures_th import board_page, row

    html = board_page(row(5, "LF: cats", "a", 7200, 2, 300, "a")).replace(
        'title="x">5 minutes ago', 'title="2026-10-31 20:00:00">sometime')
    rows = th.parse_board(html, 13, 1_800_000_000)
    assert rows[0].activity_ts == th.absolute_to_ts("2026-10-31 20:00:00") is not None
    assert rows[0].created_ts == 1_800_000_000 - 7200


@pytest.mark.parametrize("board,url", [
    (13, "https://toyhou.se/~forums/13.adoption-center"),
    ("13.adoption-center", "https://toyhou.se/~forums/13.adoption-center"),
    ("777.closed-species-forum", "https://toyhou.se/~forums/777.closed-species-forum?page=2"),
])
def test_board_url_accepts_slug(board, url):
    assert th.board_url(board, 2 if "777" in str(board) else 1) == url


def test_slug_board_place_name():
    assert th.board_name("777.closed-species-forum") == "Closed Species Forum"
    assert th.board_name(5866) == "Design Marketplace" and th.board_name(99) == "доска 99"


@pytest.mark.parametrize("text,seconds", [("yesterday", 86400), ("today", 0), ("an hour ago", 3600),
                                          ("a day ago", 86400), ("2 days, 3 hours ago", 2 * 86400 + 3 * 3600)])
def test_relative_time_words(text, seconds):
    from mcp_devian.timeparse import relative_seconds

    assert relative_seconds(text) == seconds
