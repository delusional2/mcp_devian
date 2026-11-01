import httpx
import pytest
from defusedxml import DefusedXmlException
from fixtures import (
    BOARD,
    CHALLENGE,
    CHALLENGE_RU,
    GROUP_GALLERY_PAGE,
    NORMAL_WITH_CF_SCRIPT,
    RSS,
    RSS_LIVE,
    RSS_POPULAR_FALLBACK,
    THREAD,
)

from mcp_devian import deviantart as da
from mcp_devian import toyhouse as th
from mcp_devian.config import DAGroup
from mcp_devian.timeparse import relative_seconds

NOW = 1_800_000_000.0


@pytest.mark.parametrize("text,expected", [
    ("1 month, 4 days ago", 30 * 86400 + 4 * 86400),
    ("20 hours, 17 minutes ago", 20 * 3600 + 17 * 60),
    ("2 minutes, 55 seconds ago", 175),
    ("1 year, 11 months ago", 365 * 86400 + 11 * 30 * 86400),
    ("N/A", None),
    ("", None),
])
def test_relative_seconds(text, expected):
    assert relative_seconds(text) == expected


def test_parse_rss():
    entries = da.parse_rss(RSS, "test: auctions")
    assert [e["deviation_id"] for e in entries] == ["1404000001", "1404000002", "1404000003"]
    first = entries[0]
    assert first["author"] == "SomeArtist"
    assert first["tags"] == ["adopt", "dragon", "auction"]
    assert "Min inc $1" in first["text"]
    assert first["published_ts"] is not None
    assert first["thumb"] == "https://example.com/t1.jpg"


def test_parse_rss_rejects_entities():
    evil = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
    with pytest.raises(DefusedXmlException):
        da.parse_rss(evil, "x")


def test_folder_url():
    f = da.parse_folder_url("https://www.deviantart.com/da-adopts/gallery/26780440/auctions")
    assert (f.group, f.folder_id, f.name) == ("da-adopts", "26780440", "auctions")
    assert f.query == "gallery:da-adopts/26780440"
    with pytest.raises(da.DAError):
        da.parse_folder_url("https://www.deviantart.com/da-adopts")


def test_list_group_folders_from_page():
    page = ('<a href="https://www.deviantart.com/da-adopts/gallery/26780440/auctions" class="x">'
            '<a href="https://www.deviantart.com/da-adopts/gallery/75027420/points-adopts"><img alt="a">'
            '<a href="https://www.deviantart.com/da-adopts/gallery/26780440/auctions">dup</a>'
            '<a href="https://www.deviantart.com/other/gallery/1/x">чужая</a>')
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=page)))
    folders = da.list_group_folders(http, "da-adopts")
    assert [(f.folder_id, f.name) for f in folders] == [("26780440", "auctions"), ("75027420", "points adopts")]


def test_feeds_from_config():
    feeds = da.feeds_from_config(
        [DAGroup(url="https://www.deviantart.com/g1/gallery/1/points-adopts", label=""),
         DAGroup(url="https://www.deviantart.com/g2/gallery/2", label="моя метка")],
        ["artist1"],
    )
    assert [(f.query, f.place, f.notify, f.own) for f in feeds] == [
        ("gallery:g1/1", "g1: points adopts", True, False), ("gallery:g2/2", "моя метка", True, False),
        ("gallery:artist1", "художник artist1", True, False)]

    mine = da.feeds_from_config([], [], "moth-art")
    assert [(f.query, f.notify, f.own) for f in mine] == [("gallery:moth-art", False, True)]


def test_parse_board():
    rows = th.parse_board(BOARD, 13, NOW)
    assert len(rows) == 5
    seeking = rows[0]
    assert seeking.thread_id == "865492"
    assert seeking.url == "https://toyhou.se/~forums/13.adoption-center/865492.some-slug"
    assert seeking.author == "buyer1"
    assert seeking.posts == 35
    assert seeking.activity_ts == NOW - (4 * 3600 + 40 * 60)
    assert seeking.last_poster == "someone"
    # «N/A» в последней активности: берём время создания.
    no_replies = rows[2]
    assert no_replies.posts == 0
    assert no_replies.activity_ts == no_replies.created_ts == NOW - (11 * 86400 + 20 * 3600)


def test_first_post_and_challenge():
    assert th.parse_first_post(THREAD).startswith("Looking for dragon or wyvern characters!")
    assert th.is_challenge(CHALLENGE, "Just a moment...")
    assert th.is_challenge(CHALLENGE_RU, "")
    assert not th.is_challenge(BOARD, "Adoption Center on Toyhouse")
    assert not th.is_challenge(NORMAL_WITH_CF_SCRIPT, "Adoption Center on Toyhouse")


def test_board_url():
    assert th.board_url(13) == "https://toyhou.se/~forums/13.adoption-center"
    assert th.board_url(5866, 2) == "https://toyhou.se/~forums/5866.design-marketplace?page=2"


# --- Листание ленты (atom:link rel="next") ---

def rss_page(ids, next_href=None):
    link = f'<atom:link rel="next" href="{next_href}"/>' if next_href else ""
    items = "".join(
        f"<item><title>Adopt {i}</title><link>https://www.deviantart.com/someone/art/Adopt-{i}</link>"
        f"<pubDate>Sun, 01 Nov 2026 10:00:00 GMT</pubDate></item>" for i in ids)
    return ('<?xml version="1.0"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
            f'xmlns:media="http://search.yahoo.com/mrss/"><channel>{link}{items}</channel></rss>')


NEXT_60 = da.RSS_URL + "?type=deviation&amp;q=gallery%3Ag%2F1&amp;offset=60"
NEXT_120 = da.RSS_URL + "?type=deviation&amp;q=gallery%3Ag%2F1&amp;offset=120"


def paged_http(seen):
    pages = {None: rss_page(["1", "2"], NEXT_60), "60": rss_page(["3", "4"], NEXT_120), "120": rss_page(["5"])}

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, text=pages[req.url.params.get("offset")])
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_rss_page_returns_next_link():
    entries, nxt = da.parse_rss_page(rss_page(["1"], NEXT_60), "p")
    assert [e["deviation_id"] for e in entries] == ["1"]
    assert nxt is not None and nxt.endswith("offset=60")
    assert da.parse_rss_page(rss_page(["1"]), "p")[1] is None


def test_next_link_to_other_host_ignored():
    evil = "https://evil.example.com/rss.xml?offset=60"
    assert da.parse_rss_page(rss_page(["1"], evil), "p")[1] is None


def test_fetch_feed_default_reads_one_page():
    seen = []
    entries = da.fetch_feed(paged_http(seen), "gallery:g/1", "p")
    assert [e["deviation_id"] for e in entries] == ["1", "2"]
    assert len(seen) == 1
    assert seen[0].params["q"] == "gallery:g/1" and seen[0].params["type"] == "deviation"


def test_fetch_feed_follows_next_pages_with_pause():
    seen, pauses = [], []
    entries = da.fetch_feed(paged_http(seen), "gallery:g/1", "p", pages=5, pause=lambda: pauses.append(1))
    assert [e["deviation_id"] for e in entries] == ["1", "2", "3", "4", "5"]
    assert [u.params.get("offset") for u in seen] == [None, "60", "120"]
    assert len(pauses) == 2


def test_fetch_feed_respects_page_limit():
    seen = []
    entries = da.fetch_feed(paged_http(seen), "gallery:g/1", "p", pages=2, pause=lambda: None)
    assert [e["deviation_id"] for e in entries] == ["1", "2", "3", "4"]
    assert len(seen) == 2


# Даты на страницах — 01.11.2026; NOW на 2,5 месяца позже: знакомая страница уже старая.
@pytest.mark.parametrize("known", [{"da:1", "da:2"}, {"1", "2"}, lambda dev_id: dev_id in ("1", "2")])
def test_fetch_feed_stops_when_page_already_known(known):
    seen = []
    entries = da.fetch_feed(paged_http(seen), "gallery:g/1", "p", pages=5, known=known, pause=lambda: None, now=NOW)
    assert [e["deviation_id"] for e in entries] == ["1", "2"]
    assert len(seen) == 1


def test_fetch_feed_continues_while_page_has_new():
    seen = []
    entries = da.fetch_feed(paged_http(seen), "gallery:g/1", "p", pages=5, known={"da:1", "da:3", "da:4"},
                            pause=lambda: None, now=NOW)
    assert [e["deviation_id"] for e in entries] == ["1", "2", "3", "4"]
    assert len(seen) == 2


def test_fetch_feed_bad_xml_is_da_error():
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<rss><oops")))
    with pytest.raises(da.DAError):
        da.fetch_feed(http, "gallery:g/1", "p")


def test_fetch_feed_429_on_second_page():
    def handler(req):
        if req.url.params.get("offset"):
            return httpx.Response(429)
        return httpx.Response(200, text=rss_page(["1"], NEXT_60))
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(da.RateLimited):
        da.fetch_feed(http, "gallery:g/1", "p", pages=3, pause=lambda: None)


# --- живая лента 2026: пустые теги, длинные описания, запасная лента «Popular» ---

LIVE_NOW = 1_793_534_400.0  # 01.11.2026 12:00 UTC
PAGE_TS = 1_793_527_200.0   # 01.11.2026 10:00 GMT — время работ на страницах paged_http


def test_is_fallback_feed():
    assert da.is_fallback_feed(RSS_POPULAR_FALLBACK, "tag:adoptable sort:time")
    assert not da.is_fallback_feed(RSS_POPULAR_FALLBACK, "")
    assert not da.is_fallback_feed(RSS_LIVE, "dragon adoptable auction")
    assert not da.is_fallback_feed("<rss><oops", "x")


def test_fetch_feed_raises_ignored_query_on_fallback():
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=RSS_POPULAR_FALLBACK)))
    with pytest.raises(da.IgnoredQuery):
        da.fetch_feed(http, "tag:dragonadopt sort:time", "p")
    assert issubclass(da.IgnoredQuery, da.DAError)


def test_live_feed_has_no_tags_and_keeps_long_text():
    entries, nxt = da.parse_rss_page(RSS_LIVE, "поиск DA: x")
    assert all(e["tags"] == [] for e in entries)
    assert nxt is not None and nxt.endswith("offset=60")
    long = next(e for e in entries if e["deviation_id"] == "1405000003")
    pos = long["text"].find("Starting Bid (SB): $15")
    assert pos > 600  # раньше текст обрезался на 600 символах, и цена терялась
    item = da.entry_to_item(long)
    assert item.extra["desc"] == long["text"] and len(item.snippet) <= da.SNIPPET_LIMIT


def test_entry_to_item_folder_links_and_places():
    entries = da.parse_rss(RSS_LIVE, "da-adopts: auctions")
    linker = next(e for e in entries if e["deviation_id"] == "1405000004")
    item = da.entry_to_item(linker)
    # ссылку на папку самого автора не считаем — только группы
    assert item.extra["folder_links"] == ["da-adopts/26780440"]
    assert item.extra["places"] == ["da-adopts: auctions"]
    assert item.extra["tags"] == []


def test_fetch_feed_horizon_continues_past_known_recent_page():
    seen = []
    entries = da.fetch_feed(paged_http(seen), "gallery:g/1", "p", pages=3, known={"da:1", "da:2", "da:3", "da:4"},
                            pause=lambda: None, now=PAGE_TS + 3600)
    # страница знакома, но ей час — лоты, закрывшиеся за 3 дня, могут быть дальше
    assert [e["deviation_id"] for e in entries] == ["1", "2", "3", "4", "5"]
    assert len(seen) == 3


def test_fetch_feed_horizon_stops_on_old_known_page():
    seen = []
    da.fetch_feed(paged_http(seen), "gallery:g/1", "p", pages=3, known={"da:1", "da:2"},
                  pause=lambda: None, now=PAGE_TS + 4 * 86400)
    assert len(seen) == 1


def test_fetch_feed_horizon_capped_by_pages():
    seen = []
    da.fetch_feed(paged_http(seen), "gallery:g/1", "p", pages=2, known=lambda dev_id: True,
                  pause=lambda: None, now=PAGE_TS)
    assert len(seen) == 2


def test_list_group_folders_reads_size_and_uuid():
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=GROUP_GALLERY_PAGE)))
    folders = {f.folder_id: f for f in da.list_group_folders(http, "da-adopts")}
    assert folders["26780440"].size == 9395
    assert folders["26780440"].uuid == "A4A80DA9-DEA9-2A1E-26EB-EB72E5F164BC"
    assert folders["75027420"].size == 4914
    assert folders["11111111"].size is None and folders["11111111"].uuid is None


def test_folder_meta_regex_plain_and_escaped():
    plain = '{"folderId":5,"gallectionUuid":"A4A80DA9-DEA9-2A1E-26EB-EB72E5F164BC","totalItemCount":12}'
    assert da.folder_meta(plain) == {"5": (12, "A4A80DA9-DEA9-2A1E-26EB-EB72E5F164BC")}
    assert da.folder_meta('{"folderId":7,"name":"x"}') == {}
