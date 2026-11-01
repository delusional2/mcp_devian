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
