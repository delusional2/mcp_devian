"""Слияние лота из разных лент и поиска: место, via/places, поля, которые досчитали мы сами."""

import json
import sqlite3

import pytest

from mcp_devian import da_market, db
from mcp_devian.db import Item

NOW = 1_800_000_000.0


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.sqlite3")
    yield c
    c.close()


def lot(place, **extra):
    return Item(id="da:1", source="deviantart", title="Dragon adopt", url="https://www.deviantart.com/a/art/x-1",
                author="a", place=place, lot_type="auction", status="open", extra=extra)


def extra_of(conn):
    return json.loads(db.get_item(conn, "da:1")["extra"])


def test_places_and_via_merge_as_sets(conn):
    db.upsert_item(conn, lot("da-adopts: auctions", places=["da-adopts: auctions"], via=["search:dragon adopt"]), NOW)
    db.upsert_item(conn, lot("dragon-adopts: open", places=["dragon-adopts: open"], via=["api_tag:dragonadopt"]), NOW + 1)
    db.upsert_item(conn, lot("da-adopts: auctions", places=["da-adopts: auctions"]), NOW + 2)
    e = extra_of(conn)
    assert e["places"] == ["da-adopts: auctions", "dragon-adopts: open"]
    assert e["via"] == ["api_tag:dragonadopt", "search:dragon adopt"]


def test_first_feed_place_is_kept(conn):
    db.upsert_item(conn, lot("da-adopts: auctions"), NOW)
    db.upsert_item(conn, lot("dragon-adopts: open"), NOW + 1)
    assert db.get_item(conn, "da:1")["place"] == "da-adopts: auctions"


def test_search_place_replaced_by_feed_place(conn):
    db.upsert_item(conn, lot(f"{da_market.SEARCH_PLACE_PREFIX}: dragon adopt"), NOW)
    db.upsert_item(conn, lot("da-adopts: auctions"), NOW + 1)
    assert db.get_item(conn, "da:1")["place"] == "da-adopts: auctions"


def test_feed_place_not_replaced_by_search_place(conn):
    db.upsert_item(conn, lot("da-adopts: auctions"), NOW)
    db.upsert_item(conn, lot(f"{da_market.SEARCH_PLACE_PREFIX}: dragon adopt"), NOW + 1)
    assert db.get_item(conn, "da:1")["place"] == "da-adopts: auctions"


def test_keep_extra_survives_rss_reread(conn):
    db.upsert_item(conn, lot("da-adopts: auctions", places=["da-adopts: auctions"]), NOW)
    assert db.merge_extra(conn, "da:1", {"uuid": "U1", "api_tags": ["dragonadopt", "moth"], "via": ["api_tag:moth"]})
    # RSS снова пришёл без тегов, UUID и via
    db.upsert_item(conn, lot("da-adopts: auctions", tags=[], places=["da-adopts: auctions"]), NOW + 1)
    e = extra_of(conn)
    assert e["uuid"] == "U1" and e["api_tags"] == ["dragonadopt", "moth"]
    assert e["via"] == ["api_tag:moth"] and e["places"] == ["da-adopts: auctions"] and e["tags"] == []


def test_merge_extra_unions_lists_and_replaces_scalars(conn):
    assert not db.merge_extra(conn, "da:404", {"uuid": "x"})
    db.upsert_item(conn, lot("p", api_tags=["a"], via=["search:x"]), NOW)
    db.merge_extra(conn, "da:1", {"api_tags": ["b", "a"], "via": ["search:x", "api_tag:t"], "uuid": "U"})
    e = extra_of(conn)
    assert e["api_tags"] == ["a", "b"] and e["via"] == ["api_tag:t", "search:x"] and e["uuid"] == "U"


def test_search_prefix_shared_with_db():
    assert da_market.SEARCH_PLACE_PREFIX == db.SEARCH_PLACE_PREFIX == "поиск DA"
    assert db.is_search_place("поиск DA: dragon adopt") and not db.is_search_place("da-adopts: auctions")


def test_new_tables_exist_on_old_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    # База версии 0.3: всё до deviation_stats включительно, новых таблиц нет.
    old.executescript(db.SCHEMA.split("-- Размер чужих продавцов")[0])
    assert not old.execute("SELECT 1 FROM sqlite_master WHERE name = 'author_stats'").fetchone()
    old.commit()
    old.close()
    c = db.connect(path)
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"author_stats", "group_scores", "da_folders"} <= tables
    cols = {r["name"] for r in c.execute("PRAGMA table_info(group_scores)")}
    assert {"grp", "folder", "size", "lots_per_day", "niche_share", "median_sb", "unit", "bid_share", "n", "ts"} <= cols
    assert {r["name"] for r in c.execute("PRAGMA table_info(author_stats)")} == {"author", "watchers", "deviations", "ts"}
    c.close()


def test_save_folders_keeps_uuid_when_missing(conn):
    from mcp_devian.deviantart import Folder

    db.save_folders(conn, "DA-Adopts", [Folder("da-adopts", "1", "auctions", size=10, uuid="U-1")], NOW)
    db.save_folders(conn, "da-adopts", [Folder("da-adopts", "1", "auctions")], NOW + 1)
    row = conn.execute("SELECT * FROM da_folders WHERE grp = 'da-adopts' AND folder_id = '1'").fetchone()
    assert row["uuid"] == "U-1" and row["size"] == 10 and row["ts"] == NOW + 1
