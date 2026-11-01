"""Toyhouse: чтение досок форума через настоящий Chrome.

У Toyhouse нет API, а Cloudflare не пускает обычные HTTP-клиенты. Поэтому страницы
открываются в Chrome с отдельным профилем, куда она один раз логинится сама.
Ходим редко и с паузами, как человек.
"""

import fcntl
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .config import DATA_DIR
from .db import Item
from .timeparse import absolute_to_ts, relative_to_ts

BASE = "https://toyhou.se"
PROFILE_DIR = DATA_DIR / "chrome-profile"
LOCK_FILE = DATA_DIR / "chrome.lock"

# id: (slug, название). Ссылки сняты с меню форума.
BOARDS = {
    13: ("13.adoption-center", "Adoption Center"),
    71: ("71.art-marketplace", "Art Marketplace"),
    5866: ("5866.design-marketplace", "Design Marketplace"),
    207: ("207.character-discussion", "Character Discussion"),
    5868: ("5868.species-discussion", "Species Discussion"),
    12: ("12.worlds", "Worlds"),
    13975: ("13975.art-trades", "Art Trades"),
    13973: ("13973.character-games-freebies", "Character Games & Freebies"),
}
THREAD_ID = re.compile(r"/~forums/(\d+)\.[^/]+/(\d+)\.")
BOARD_SLUG = re.compile(r"^(\d+)\.[a-z0-9][a-z0-9\-]*$", re.I)
# Сколько текста первого поста читаем: бюджет и «not looking for» в длинных LF-постах часто ниже 700 символов.
FIRST_POST_CHARS = 2500


class ToyhouseError(Exception):
    pass


class ChallengeError(ToyhouseError):
    """Cloudflare показал проверку, без неё дальше не пройти."""


class BrowserBusy(ToyhouseError):
    pass


class BrowserClosed(Exception):
    """Окно Chrome закрыли посреди проверки: дальше читать нечем."""


@dataclass
class ThreadRow:
    board_id: int
    thread_id: str
    title: str
    url: str
    author: str | None
    posts: int | None
    created_ts: float | None
    activity_ts: float | None
    last_poster: str | None
    # Сырые признаки строки (класс tr, есть ли значок замка). Селекторы за Cloudflare не проверены —
    # пока только собираем, решения по ним не принимаем.
    row_class: str | None = None
    row_locked: bool | None = None


def board_id(board) -> int:
    """Номер доски из настройки: 13 или «13.adoption-center»."""
    if isinstance(board, str):
        m = BOARD_SLUG.match(board.strip())
        if m:
            return int(m.group(1))
        return int(board.strip())
    return int(board)


def board_url(board, page: int = 1) -> str:
    """Адрес доски. «id.slug» из настройки берём как есть; номер не из BOARDS — /~forums/<id>
    (такой адрес не проверен: лучше задавать доску как «id.slug»)."""
    if isinstance(board, str) and BOARD_SLUG.match(board.strip()):
        slug = board.strip().lower()
    else:
        bid = board_id(board)
        slug = BOARDS[bid][0] if bid in BOARDS else str(bid)
    url = f"{BASE}/~forums/{slug}"
    return url if page <= 1 else f"{url}?page={page}"


def board_name(board) -> str:
    bid = board_id(board)
    if bid in BOARDS:
        return BOARDS[bid][1]
    if isinstance(board, str) and BOARD_SLUG.match(board.strip()):
        return board.strip().split(".", 1)[1].replace("-", " ").title()
    return f"доска {bid}"


def _when(relative: str | None, absolute: str | None, now: float) -> float | None:
    """Относительный текст («4 hours ago») не зависит от пояса; не разобрался — абсолютный title."""
    return relative_to_ts(relative, now) or absolute_to_ts(absolute)


def _abbr_ts(el, now: float) -> float | None:
    """Время из <abbr class="datetime">: относительный текст, запасной вариант — атрибут title."""
    return _when(el.get_text(" ", strip=True), el.get("title"), now) if el else None


def is_challenge(html_text: str, title: str = "") -> bool:
    # Скрипт /cdn-cgi/challenge-platform/ Cloudflare вставляет и в обычные страницы,
    # поэтому смотрим только на признаки самой страницы проверки. Заголовок переводится
    # на язык браузера («Один момент…»), так что надёжнее разметка.
    t = title.lower()
    return ("_cf_chl_opt" in html_text or 'id="challenge-error-text"' in html_text
            or 'id="challenge-form"' in html_text or "cf-turnstile" in html_text
            or "just a moment" in t or "один момент" in t or "attention required" in t)


def parse_board(html_text: str, board_id: int, now: float) -> list[ThreadRow]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows = []
    for tr in soup.select("tr.forum-thread-row"):
        link = tr.select_one(".forum-thread-title a")
        if not link or not link.get("href"):
            continue
        href = urljoin(BASE, link["href"])
        m = THREAD_ID.search(href)
        if not m:
            continue
        info = tr.select_one(".forum-thread-info")
        author_el = info.select_one(".display-user a") if info else None
        posts_el = tr.select_one(".forum-thread-posts")
        posts_m = re.search(r"\d[\d,]*", posts_el.get_text()) if posts_el else None
        created = _abbr_ts(info.select_one("abbr.datetime") if info else None, now)
        last_el = tr.select_one(".forum-last-update-date abbr.datetime")
        last_author = tr.select_one(".forum-last-update-author .display-user a")
        rows.append(ThreadRow(
            board_id=board_id,
            thread_id=m.group(2),
            title=" ".join(link.get_text(" ").split()),
            url=href.split("?")[0],
            author=author_el.get_text(strip=True) if author_el else None,
            posts=int(posts_m.group().replace(",", "")) if posts_m else None,
            created_ts=created,
            activity_ts=_abbr_ts(last_el, now) or created,
            last_poster=last_author.get_text(strip=True) if last_author else None,
            row_class=" ".join(tr.get("class") or []) or None,
            row_locked=tr.select_one("[class*=lock]") is not None,
        ))
    return rows


# Режим Claude: эти скрипты Claude выполняет на открытой странице в её Chrome
# и передаёт результат в ingest_toyhouse_board / ingest_first_post.
EXTRACT_BOARD_JS = """JSON.stringify([...document.querySelectorAll('tr.forum-thread-row')].map(tr => {
  const a = tr.querySelector('.forum-thread-title a');
  const info = tr.querySelector('.forum-thread-info');
  const t = el => el ? el.textContent.replace(/\\s+/g, ' ').trim() : null;
  const at = el => el ? el.getAttribute('title') : null;
  const created = info && info.querySelector('abbr.datetime');
  const last = tr.querySelector('.forum-last-update-date abbr.datetime');
  return {
    title: t(a), href: a && a.href,
    author: t(info && info.querySelector('.display-user a')),
    created: t(created), created_title: at(created),
    posts: t(tr.querySelector('.forum-thread-posts')),
    last: t(last), last_title: at(last),
    last_poster: t(tr.querySelector('.forum-last-update-author .display-user a')),
    row_class: tr.className || null,
    row_locked: !!tr.querySelector('[class*=lock]'),
  };
}))"""

EXTRACT_POST_JS = ("""(document.querySelector('.forum-post .user-content') || {innerText: ''}).innerText"""
                   f""".slice(0, {FIRST_POST_CHARS})""")


def rows_from_extracted(items: list[dict], board_id: int, now: float) -> list[ThreadRow]:
    """Строки, которые вернул EXTRACT_BOARD_JS, в ThreadRow. Мусор молча пропускаем."""
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        href = urljoin(BASE, str(it.get("href") or ""))
        m = THREAD_ID.search(href)
        title = " ".join(str(it.get("title") or "").split())
        if not m or not title or not href.startswith(BASE + "/~forums/"):
            continue
        posts_m = re.search(r"\d[\d,]*", str(it.get("posts") or ""))
        created = _when(it.get("created"), it.get("created_title"), now)
        rows.append(ThreadRow(
            board_id=board_id,
            thread_id=m.group(2),
            title=title,
            url=href.split("?")[0].split("#")[0],
            author=it.get("author") or None,
            posts=int(posts_m.group().replace(",", "")) if posts_m else None,
            created_ts=created,
            activity_ts=_when(it.get("last"), it.get("last_title"), now) or created,
            last_poster=it.get("last_poster") or None,
            row_class=str(it.get("row_class"))[:200] if it.get("row_class") else None,
            row_locked=bool(it.get("row_locked")) if it.get("row_locked") is not None else None,
        ))
    return rows


def parse_first_post(html_text: str, limit: int = FIRST_POST_CHARS) -> str | None:
    soup = BeautifulSoup(html_text, "html.parser")
    body = soup.select_one(".forum-post .user-content")
    if not body:
        return None
    return " ".join(body.get_text(" ").split())[:limit] or None


def row_to_item(row: ThreadRow) -> Item:
    return Item(
        id=f"th:{row.thread_id}",
        source="toyhouse",
        title=row.title,
        url=row.url,
        author=row.author,
        place=board_name(row.board_id),
        posts=row.posts,
        activity_ts=row.activity_ts,
        published_ts=row.created_ts,
        extra={"board_id": row.board_id, "last_poster": row.last_poster, "row_class": row.row_class,
               "row_locked": row.row_locked},
    )


@contextmanager
def browser_lock():
    """Один профиль Chrome нельзя открыть из двух процессов сразу."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise BrowserBusy("Chrome-профиль сейчас занят другим процессом") from e
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


NO_CHROME = ("Не нашла Google Chrome. Его нужно поставить с google.com/chrome — "
             "и бот, и режим «её Chrome» работают через него.")
CHROME_APP = "Google Chrome.app"


def chrome_app_path() -> Path | None:
    """Где стоит Chrome (без Apple Events, просто по файлам)."""
    for base in (Path("/Applications"), Path.home() / "Applications"):
        if (base / CHROME_APP).exists():
            return base / CHROME_APP
    return None


@contextmanager
def open_browser(headless: bool = True, profile_dir: Path = PROFILE_DIR):
    from playwright.sync_api import sync_playwright

    app = chrome_app_path()
    if not app:
        raise ToyhouseError(NO_CHROME)
    # channel="chrome" ищет только в /Applications; из ~/Applications запускаем по пути.
    where = ({"channel": "chrome"} if app.parent == Path("/Applications")
             else {"executable_path": str(app / "Contents/MacOS/Google Chrome")})
    profile_dir.mkdir(parents=True, exist_ok=True)
    with browser_lock(), sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            **where,
        )
        try:
            yield context
        finally:
            context.close()


class Reader:
    """Открытый браузер, через который читаем страницы по одной.

    Проверку Cloudflare бот сам не проходит. В интерактивном режиме (она сидит рядом)
    он просит её нажать галочку в окне и ждёт; по расписанию просто сдаётся.
    """

    def __init__(self, context, interactive: bool = False, on_challenge=None, challenge_wait_s: int = 120):
        self.context = context
        self.page = context.pages[0] if context.pages else context.new_page()
        self.interactive = interactive
        self.on_challenge = on_challenge
        self.challenge_wait_s = challenge_wait_s

    def _ready(self, selector: str) -> bool:
        from playwright.sync_api import Error as PlaywrightError

        try:
            return self.page.query_selector(selector) is not None
        except PlaywrightError:  # страница как раз перезагружается после проверки
            return False

    def _challenged(self) -> bool:
        from playwright.sync_api import Error as PlaywrightError

        try:
            return is_challenge(self.page.content(), self.page.title())
        except PlaywrightError:
            return False

    def get(self, url: str, ready_selector: str, timeout_s: int = 25) -> str:
        from playwright.sync_api import Error as PlaywrightError

        try:
            return self._get(url, ready_selector, timeout_s)
        except PlaywrightError as e:
            if "closed" in str(e).lower():
                raise BrowserClosed(str(e)) from e
            # Таймаут или сетевая ошибка одной страницы: пропускаем её, остальное читаем.
            raise ToyhouseError(f"{url}: {type(e).__name__}: {str(e).splitlines()[0][:200]}") from e

    def _get(self, url: str, ready_selector: str, timeout_s: int) -> str:
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        deadline = time.time() + timeout_s
        asked = False
        while time.time() < deadline:
            if self._ready(ready_selector):
                return self.page.content()
            if self.interactive and not asked and self._challenged():
                asked = True
                self.page.bring_to_front()
                if self.on_challenge:
                    self.on_challenge()
                deadline = time.time() + self.challenge_wait_s
            time.sleep(1)
        html_text = self.page.content()
        shot = self._save_debug(html_text)
        if is_challenge(html_text, self.page.title()):
            raise ChallengeError(f"Toyhouse показывает проверку Cloudflare (снимок: {shot})")
        raise ToyhouseError(f"Страница не загрузилась как ожидалось: {url} (снимок: {shot})")

    def _save_debug(self, html_text: str) -> Path | None:
        debug = DATA_DIR / "debug"
        try:
            debug.mkdir(parents=True, exist_ok=True)
            (debug / "last-page.html").write_text(html_text, encoding="utf-8")
            self.page.screenshot(path=str(debug / "last-page.png"))
            return debug / "last-page.png"
        except Exception:
            return None

    def board(self, board, page: int, now: float) -> list[ThreadRow]:
        return parse_board(self.get(board_url(board, page), "tr.forum-thread-row"), board_id(board), now)

    def first_post(self, url: str) -> str | None:
        return parse_first_post(self.get(url, ".forum-post"))


def human_pause(low: float = 4, high: float = 11) -> None:
    time.sleep(random.uniform(low, high))
