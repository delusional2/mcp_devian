"""Чтение API DeviantArt для рынка: разбор ответов, листание, 429 летит наверх. Только подставной клиент."""

import pytest

from mcp_devian import da_browse
from mcp_devian.da_api import NotConnected, RateLimited


def dev(number, uuid=None, author="seller", published="1793500000", comments=3, favourites=7, title=None):
    return {"deviationid": uuid or f"UUID-{number}", "url": f"https://www.deviantart.com/{author}/art/Lot-{number}",
            "title": title or f"Lot {number}", "author": {"username": author}, "published_time": published,
            "stats": {"comments": comments, "favourites": favourites}}


class Fake:
    def __init__(self, answers):
        self.answers = answers  # path -> body | Exception
        self.calls = []

    def _request(self, method, path, params=None, **kw):
        self.calls.append((method, path, params))
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_browse_tags_maps_items_and_next_offset():
    client = Fake({"/browse/tags": {"has_more": True, "next_offset": 50, "results": [
        dev(1405000001), {"deviationid": "J", "url": "https://www.deviantart.com/x/journal/no-number"},
        dev(1405000002, published="2026-10-30T10:00:00+0000", comments=0)]}})
    sleeps = []
    items, nxt = da_browse.browse_tags(client, "#DragonAdopt", limit=500, sleep=sleeps.append)
    assert nxt == 50 and sleeps == [da_browse.PAUSE_S]
    assert client.calls == [("GET", "/browse/tags", {"tag": "DragonAdopt", "limit": 50, "offset": 0})]
    assert [i["number"] for i in items] == ["1405000001", "1405000002"]  # журнал без номера пропущен
    first = items[0]
    assert first == {"uuid": "UUID-1405000001", "number": "1405000001",
                     "url": "https://www.deviantart.com/seller/art/Lot-1405000001", "title": "Lot 1405000001",
                     "author": "seller", "published_ts": 1793500000.0, "comments": 3, "favourites": 7}
    assert items[1]["published_ts"] is not None and items[1]["comments"] == 0


def test_browse_tags_last_page_has_no_next():
    client = Fake({"/browse/tags": {"has_more": False, "next_offset": None, "results": []}})
    assert da_browse.browse_tags(client, "adoptauction", offset=50, sleep=lambda s: None) == ([], None)
    assert client.calls[0][2]["offset"] == 50


def test_tag_suggest_names():
    client = Fake({"/browse/tags/search": {"results": [{"tag_name": "dragonadopt"}, {"tag_name": "dragonadopts"},
                                                       {"tag_name": "dragonadopt"}]}})
    assert da_browse.tag_suggest(client, "dragonadopt", sleep=lambda s: None) == ["dragonadopt", "dragonadopts"]
    assert client.calls[0][2] == {"tag_name": "dragonadopt"}


def test_folder_page_params_and_mapping():
    client = Fake({"/gallery/A4A80DA9": {"has_more": True, "results": [dev(11), dev(12, comments=0)]}})
    items = da_browse.folder_page(client, "da-adopts", "A4A80DA9", sleep=lambda s: None)
    assert [(i["number"], i["uuid"], i["comments"]) for i in items] == [("11", "UUID-11", 3), ("12", "UUID-12", 0)]
    assert client.calls[0][2] == {"username": "da-adopts", "mature_content": "false", "offset": 0, "limit": 24}


def test_user_stats():
    client = Fake({"/user/profile/Seller": {"user": {"username": "Seller", "type": "regular",
                                                      "stats": {"watchers": 1234, "friends": 50}},
                                             "stats": {"user_deviations": 321}}})
    assert da_browse.user_stats(client, " Seller ", sleep=lambda s: None) == {
        "watchers": 1234, "friends": 50, "deviations": 321, "type": "regular"}
    assert client.calls[0][2] == {"expand": "user.stats"}


def test_metadata_batches_and_description():
    meta = {"metadata": [{"deviationid": "U1", "title": "T", "author": {"username": "a"},
                          "description": "<p>SB: $15</p>", "tags": [{"tag_name": "dragonadopt"}, {"x": 1}],
                          "stats": {"views": 10, "favourites": 2, "comments": 1}}]}
    client = Fake({"/deviation/metadata": meta})
    out = da_browse.metadata(client, [f"U{i}" for i in range(60)] + ["U1"], sleep=lambda s: None)
    assert len(client.calls) == 2  # 60 разных id — две пачки по ≤50
    assert len([p for p in client.calls[0][2] if p[0] == "deviationids[]"]) == 50
    assert out["U1"] == {"title": "T", "author": "a", "description": "<p>SB: $15</p>", "tags": ["dragonadopt"],
                         "views": 10, "favourites": 2, "comments": 1}


@pytest.mark.parametrize("call", [
    lambda c: da_browse.browse_tags(c, "x", sleep=lambda s: None),
    lambda c: da_browse.tag_suggest(c, "x", sleep=lambda s: None),
    lambda c: da_browse.folder_page(c, "g", "F", sleep=lambda s: None),
    lambda c: da_browse.user_stats(c, "x", sleep=lambda s: None),
    lambda c: da_browse.metadata(c, ["U"], sleep=lambda s: None),
])
@pytest.mark.parametrize("error", [RateLimited("429", 429, status=429), NotConnected("нет входа")])
def test_rate_limit_and_disconnect_propagate(call, error):
    class Always(Fake):
        def _request(self, method, path, params=None, **kw):
            raise error
    with pytest.raises(type(error)):
        call(Always({}))
