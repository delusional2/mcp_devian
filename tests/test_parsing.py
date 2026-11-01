import httpx
import pytest
from defusedxml import DefusedXmlException
from fixtures import BOARD, CHALLENGE, CHALLENGE_RU, NORMAL_WITH_CF_SCRIPT, RSS, THREAD

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
