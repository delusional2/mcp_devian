"""DeviantArt: RSS папок групп и галерей художников. Логин не нужен."""

import html
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote

import httpx
from bs4 import BeautifulSoup
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from .db import Item

RSS_URL = "https://backend.deviantart.com/rss.xml"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) mcp_devian/0.1 (personal monitor)"
MEDIA = "{http://search.yahoo.com/mrss/}"

FOLDER_URL = re.compile(r"deviantart\.com/([A-Za-z0-9_-]+)/gallery/(\d+)(?:/([^/?#\"'<>\s]*))?", re.I)
DEVIATION_ID = re.compile(r"-(\d+)/?$")


class DAError(Exception):
    pass


class RateLimited(DAError):
    pass


@dataclass
class Folder:
    group: str
    folder_id: str
    name: str

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


def _get(http: httpx.Client, url: str, **params) -> httpx.Response:
    try:
        resp = http.get(url, params=params or None)
    except httpx.HTTPError as e:  # нет сети после сна, таймаут и т.п.
        raise DAError(f"нет связи с DeviantArt: {type(e).__name__}") from e
    if resp.status_code == 429:
        raise RateLimited("DeviantArt просит притормозить (429)")
    if resp.status_code == 403:
        raise RateLimited("DeviantArt не пускает (403), подождём")
    if resp.status_code >= 400:
        raise DAError(f"DeviantArt ответил {resp.status_code} на {url}")
    return resp


def list_group_folders(http: httpx.Client, group: str) -> list[Folder]:
    """Папки группы с публичной страницы её галереи."""
    resp = _get(http, f"https://www.deviantart.com/{group}/gallery")
    seen: dict[str, Folder] = {}
    for m in FOLDER_URL.finditer(resp.text):
        g, folder_id, slug = m.groups()
        if g.lower() != group.lower() or folder_id in seen:
            continue
        seen[folder_id] = Folder(group=g, folder_id=folder_id,
                                 name=unquote(slug or "").replace("-", " ").strip())
    return list(seen.values())


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _plain(description_html: str, limit: int = 600) -> str:
    text = BeautifulSoup(html.unescape(description_html or ""), "html.parser").get_text(" ")
    return " ".join(text.split())[:limit]


def parse_rss(xml_text: str, place: str) -> list[dict]:
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
        entries.append({
            "deviation_id": m.group(1),
            "title": _text(it.find("title")) or _text(it.find(f"{MEDIA}title")),
            "url": link,
            "author": author,
            "tags": keywords,
            "text": _plain(_text(it.find(f"{MEDIA}description")) or _text(it.find("description"))),
            "published_ts": published,
            "rating": _text(it.find(f"{MEDIA}rating")),
            "thumb": thumb.get("url") if thumb is not None else None,
            "place": place,
        })
    return entries


def fetch_feed(http: httpx.Client, query: str, place: str) -> list[dict]:
    resp = _get(http, RSS_URL, type="deviation", q=query)
    try:
        return parse_rss(resp.text, place)
    except (ET.ParseError, DefusedXmlException) as e:
        raise DAError(f"Кривой RSS для {query}: {e}") from e


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
        snippet=entry["text"][:300] or None,
        extra={"tags": entry["tags"], "thumb": entry["thumb"], "rating": entry["rating"]},
    )


@dataclass
class Feed:
    query: str
    place: str
    notify: bool = True   # присылать ли находки (иначе только статистика рынка)
    own: bool = False     # её собственная галерея


MY_GALLERY = "мои работы"


def feeds_from_config(groups, artists, my_username: str = "") -> list[Feed]:
    """Все RSS-источники из настроек."""
    feeds = []
    for g in groups:
        folder = parse_folder_url(g.url)
        label = g.label or f"{folder.group}: {folder.name or folder.folder_id}"
        feeds.append(Feed(folder.query, label, notify=getattr(g, "notify", True)))
    for name in artists:
        feeds.append(Feed(f"gallery:{name}", f"художник {name}"))
    if my_username:
        feeds.append(Feed(f"gallery:{my_username}", MY_GALLERY, notify=False, own=True))
    return feeds


def polite_pause() -> None:
    # robots.txt просит Crawl-delay: 1, держим запас.
    time.sleep(random.uniform(1.5, 3.5))


def rss_link(query: str) -> str:
    return f"{RSS_URL}?type=deviation&q={quote(query)}"
