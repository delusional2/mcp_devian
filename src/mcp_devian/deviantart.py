"""DeviantArt: RSS папок групп и галерей художников. Логин не нужен."""

import html
import random
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote

import httpx
from bs4 import BeautifulSoup
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from . import da_access
from .db import Item

RSS_URL = "https://backend.deviantart.com/rss.xml"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) mcp_devian/0.1 (personal monitor)"
MEDIA = "{http://search.yahoo.com/mrss/}"
ATOM = "{http://www.w3.org/2005/Atom}"

FOLDER_URL = re.compile(r"deviantart\.com/([A-Za-z0-9_-]+)/gallery/(\d+)(?:/([^/?#\"'<>\s]*))?", re.I)
DEVIATION_ID = re.compile(r"-(\d+)/?$")


class DAError(Exception):
    pass


class RateLimited(DAError):
    pass


class Throttled(RateLimited):
    """Запрос не отправлен: DeviantArt недавно просил притормозить (пауза) или исчерпан свой бюджет."""

    def __init__(self, block: da_access.Block):
        super().__init__(block.message)
        self.block = block

    @property
    def why(self) -> str:
        return self.block.why


class IgnoredQuery(DAError):
    """DeviantArt не понял запрос и вернул общую ленту «Popular» (так бывает с tag:, sort:, max_age:)."""


@dataclass
class Folder:
    group: str
    folder_id: str
    name: str
    # Сколько работ в папке (totalItemCount со страницы группы) и её UUID для API (gallectionUuid).
    size: int | None = None
    uuid: str | None = None

    @property
    def query(self) -> str:
        return f"gallery:{self.group}/{self.folder_id}"

    @property
    def url(self) -> str:
        return f"https://www.deviantart.com/{self.group}/gallery/{self.folder_id}"


def parse_folder_url(url: str) -> Folder:
    m = FOLDER_URL.search(url)
    if not m:
        raise DAError(f"Не похоже на ссылку на папку группы: {url}")
    group, folder_id, slug = m.groups()
    return Folder(group=group, folder_id=folder_id, name=unquote(slug or "").replace("-", " ").strip())


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                        timeout=30, follow_redirects=True)


def _get(http: httpx.Client, url: str, *, headers: dict | None = None, **params) -> httpx.Response:
    """Один запрос к сайту/RSS. Сначала спрашивает da_access (пауза, бюджет) — иначе Throttled без запроса.
    403/429 включают общую паузу для всех, кто читает RSS. 304 возвращается как есть."""
    block = da_access.before(da_access.RSS)
    if block:
        raise Throttled(block)
    try:
        resp = http.get(url, params=params or None, headers=headers) if headers else http.get(url, params=params or None)
    except httpx.HTTPError as e:  # нет сети после сна, таймаут и т.п.
        raise DAError(f"нет связи с DeviantArt: {type(e).__name__}") from e
    da_access.after(da_access.RSS, resp.status_code, resp.headers)
    if resp.status_code == 429:
        raise RateLimited("DeviantArt просит притормозить (429)")
    if resp.status_code == 403:
        raise RateLimited("DeviantArt не пускает (403), подождём")
    if resp.status_code >= 400:
        raise DAError(f"DeviantArt ответил {resp.status_code} на {url}")
    return resp


# Данные папок во встроенном JSON страницы группы (кавычки там бывают экранированы: \\"folderId\\").
_Q = r'\\?"'
FOLDER_ID_JSON = re.compile(_Q + r"folderId" + _Q + r":(\d+)")
FOLDER_COUNT_JSON = re.compile(_Q + r"totalItemCount" + _Q + r":(\d+)")
FOLDER_UUID_JSON = re.compile(_Q + r"gallectionUuid" + _Q + r":" + _Q + r"([0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27})")


def folder_meta(page: str) -> dict[str, tuple[int | None, str | None]]:
    """{folder_id: (сколько работ, UUID папки)} из встроенного JSON страницы галереи группы.

    Проверено на da-adopts (9395, 7068, 6559). Нет данных — папки просто нет в словаре."""
    out: dict[str, tuple[int | None, str | None]] = {}
    marks = list(FOLDER_ID_JSON.finditer(page))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else min(len(page), m.end() + 4000)
        chunk = page[m.end():end]
        count, uuid = FOLDER_COUNT_JSON.search(chunk), FOLDER_UUID_JSON.search(chunk)
        if m.group(1) in out or not (count or uuid):
            continue
        out[m.group(1)] = (int(count.group(1)) if count else None, uuid.group(1).upper() if uuid else None)
    return out


def list_group_folders(http: httpx.Client, group: str) -> list[Folder]:
    """Папки группы с публичной страницы её галереи (с размером папки, если он есть на странице)."""
    resp = _get(http, f"https://www.deviantart.com/{group}/gallery")
    meta = folder_meta(resp.text)
    seen: dict[str, Folder] = {}
    for m in FOLDER_URL.finditer(resp.text):
        g, folder_id, slug = m.groups()
        if g.lower() != group.lower() or folder_id in seen:
            continue
        size, uuid = meta.get(folder_id, (None, None))
        seen[folder_id] = Folder(group=g, folder_id=folder_id,
                                 name=unquote(slug or "").replace("-", " ").strip(), size=size, uuid=uuid)
    return list(seen.values())


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


# Сколько текста описания держим: SB часто стоит после 600-го символа (длинные TOS сверху).
TEXT_LIMIT = 3000
SNIPPET_LIMIT = 1000


def _plain(description_html: str, limit: int = TEXT_LIMIT) -> str:
    text = BeautifulSoup(html.unescape(description_html or ""), "html.parser").get_text(" ")
    return " ".join(text.split())[:limit]


def parse_rss(xml_text: str, place: str) -> list[dict]:
    return parse_rss_page(xml_text, place)[0]


def _next_url(root) -> str | None:
    """Ссылка на следующую страницу ленты (atom:link rel="next"), только на тот же адрес RSS."""
    for link in root.iter(f"{ATOM}link"):
        href = link.get("href") or ""
        if link.get("rel") == "next" and href.startswith(RSS_URL + "?"):
            return href
    return None


POPULAR_TITLE = "DeviantArt: Popular"


def is_fallback_feed(xml_text: str, query: str) -> bool:
    """DeviantArt не понял запрос и отдал общую ленту популярного: работы в ней к запросу не относятся."""
    if not (query or "").strip():
        return False
    try:
        root = safe_fromstring(xml_text)
    except (ET.ParseError, DefusedXmlException):
        return False
    channel = root.find("channel")
    return channel is not None and _text(channel.find("title")) == POPULAR_TITLE


def folder_links(description_html: str, author: str | None = None) -> list[str]:
    """Ссылки на папки групп в описании: «группа/номер_папки». Папки самого автора не считаем."""
    found = set()
    for m in FOLDER_URL.finditer(html.unescape(description_html or "")):
        g, folder_id, _ = m.groups()
        if author and g.lower() == author.lower():
            continue
        found.add(f"{g.lower()}/{folder_id}")
    return sorted(found)


def parse_rss_page(xml_text: str, place: str) -> tuple[list[dict], str | None]:
    """(работы на странице, ссылка на следующую страницу или None)."""
    root = safe_fromstring(xml_text)
    entries = []
    for it in root.iter("item"):
        link = _text(it.find("link"))
        m = DEVIATION_ID.search(link)
        if not m:
            continue
        credits = [_text(c) for c in it.findall(f"{MEDIA}credit")]
        author = next((c for c in credits if c and not c.startswith("http")), None)
        keywords = [k.strip() for k in _text(it.find(f"{MEDIA}keywords")).split(",") if k.strip()]
        pub = _text(it.find("pubDate"))
        try:
            published = parsedate_to_datetime(pub).timestamp() if pub else None
        except (TypeError, ValueError):
            published = None
        thumb = it.find(f"{MEDIA}thumbnail")
        raw = _text(it.find(f"{MEDIA}description")) or _text(it.find("description"))
        entries.append({
            "deviation_id": m.group(1),
            "title": _text(it.find("title")) or _text(it.find(f"{MEDIA}title")),
            "url": link,
            "author": author,
            "tags": keywords,
            "text": _plain(raw),
            "folder_links": folder_links(raw, author),
            "published_ts": published,
            "rating": _text(it.find(f"{MEDIA}rating")),
            "thumb": thumb.get("url") if thumb is not None else None,
            "place": place,
        })
    return entries, _next_url(root)


def fetch_feed(http: httpx.Client, query: str, place: str, pages: int = 1, known=None, pause=None,
               horizon_s: float = 3 * 86400, now: float | None = None, validators: dict | None = None,
               deep: bool = True, state: dict | None = None) -> list[dict]:
    """Лента папки/галереи/поиска. pages > 1 — листаем дальше по 60 работ (atom:link rel="next").

    known — уже виденные работы (множество id «123» или «da:123», либо функция id → bool): если вся
    страница уже знакома и самая старая работа на ней старше horizon_s, дальше не идём — в обычный
    проход это одна страница. На оживлённой папке (60+ лотов в день) знакомая страница может
    покрывать меньше суток: тогда листаем дальше, чтобы увидеть, какие лоты закрылись за эти дни.

    Если DeviantArt вместо ответа на запрос отдал общую ленту «Popular» — IgnoredQuery.

    validators — {"etag", "last_modified"} прошлого чтения первой страницы: шлём If-None-Match /
    If-Modified-Since. Ответ 304 — лента не менялась: ничего не разбираем, возвращаем [] и ставим
    validators["not_modified"] = True. Новые ETag/Last-Modified записываются обратно в словарь.

    deep=False — обычный проход ленты: следующую страницу читаем, только если на этой всё новое
    (иначе дальше уже знакомое), без «3 дней назад». Глубоко листаем изредка (monitor решает когда).

    state — если передан, отказ (403/429, свой бюджет, сбой) на 2-й и дальше странице не выбрасывает
    уже прочитанное: возвращаем его и ставим state["cut"] = исключение. Без state — как раньше, RateLimited.
    """
    is_known: Callable[[str], bool]
    if known is None:
        is_known = lambda dev_id: False  # noqa: E731
    elif callable(known):
        is_known = known
    else:
        is_known = lambda dev_id: dev_id in known or f"da:{dev_id}" in known  # noqa: E731
    entries: list[dict] = []
    url: str = RSS_URL
    params: dict | None = {"type": "deviation", "q": query}
    for page in range(max(1, pages)):
        if page:
            (pause or polite_pause)()
        headers = None
        if page == 0 and validators is not None:
            validators.pop("not_modified", None)
            headers = {k: v for k, v in (("If-None-Match", validators.get("etag")),
                                          ("If-Modified-Since", validators.get("last_modified"))) if v} or None
        try:
            resp = _get(http, url, headers=headers, **(params or {}))
        except DAError as e:  # RateLimited/Throttled — тоже DAError
            if page and state is not None:
                state["cut"] = e
                break
            raise
        if page == 0 and validators is not None:
            if resp.status_code == 304:
                validators["not_modified"] = True
                return []
            validators["etag"] = resp.headers.get("ETag")
            validators["last_modified"] = resp.headers.get("Last-Modified")
        try:
            found, next_url = parse_rss_page(resp.text, place)
        except (ET.ParseError, DefusedXmlException) as e:
            raise DAError(f"Кривой RSS для {query}: {e}") from e
        if page == 0 and is_fallback_feed(resp.text, query):
            raise IgnoredQuery(f"DeviantArt не понял запрос «{query}» и вернул общую ленту популярного")
        entries.extend(found)
        if not found or not next_url:
            break
        if not deep:
            if any(is_known(e["deviation_id"]) for e in found):
                break
        elif all(is_known(e["deviation_id"]) for e in found):
            stamps = [e["published_ts"] for e in found if e["published_ts"]]
            limit = (time.time() if now is None else now) - horizon_s
            if not stamps or min(stamps) <= limit:
                break
        url, params = next_url, None
    return entries


def entry_to_item(entry: dict) -> Item:
    return Item(
        id=f"da:{entry['deviation_id']}",
        source="deviantart",
        title=entry["title"],
        url=entry["url"],
        author=entry["author"],
        place=entry["place"],
        published_ts=entry["published_ts"],
        activity_ts=entry["published_ts"],
        snippet=entry["text"][:SNIPPET_LIMIT] or None,
        extra={"tags": entry["tags"], "thumb": entry["thumb"], "rating": entry["rating"],
               "desc": entry["text"], "folder_links": list(entry.get("folder_links") or []),
               "places": [entry["place"]] if entry.get("place") else [],
               # Через API UUID приходит сразу: потом его не надо искать по галерее автора.
               **({"uuid": entry["uuid"]} if entry.get("uuid") else {}),
               **({"desc_ts": entry["desc_ts"]} if entry.get("desc_ts") else {})},
    )


@dataclass
class Feed:
    query: str
    place: str
    notify: bool = True   # присылать ли находки (иначе только статистика рынка)
    own: bool = False     # её собственная галерея
    name: str = ""        # название папки из ссылки (чтобы найти её UUID в API)

    @property
    def user(self) -> str:
        """Группа или художник, чья это галерея."""
        return self.query.removeprefix("gallery:").split("/")[0]

    @property
    def folder_id(self) -> str | None:
        """Номер папки группы из ссылки; None — вся галерея (художник или она сама)."""
        rest = self.query.removeprefix("gallery:")
        return rest.split("/", 1)[1] if "/" in rest else None


MY_GALLERY = "мои работы"


def feeds_from_config(groups, artists, my_username: str = "") -> list[Feed]:
    """Все RSS-источники из настроек."""
    feeds = []
    for g in groups:
        folder = parse_folder_url(g.url)
        label = g.label or f"{folder.group}: {folder.name or folder.folder_id}"
        feeds.append(Feed(folder.query, label, notify=getattr(g, "notify", True), name=folder.name))
    for name in artists:
        feeds.append(Feed(f"gallery:{name}", f"художник {name}"))
    if my_username:
        feeds.append(Feed(f"gallery:{my_username}", MY_GALLERY, notify=False, own=True))
    return feeds


def polite_pause() -> None:
    # robots.txt просит Crawl-delay: 1, держим запас: не меньше 2 секунд между запросами.
    time.sleep(random.uniform(2.0, 4.0))


def rss_link(query: str) -> str:
    return f"{RSS_URL}?type=deviation&q={quote(query)}"
