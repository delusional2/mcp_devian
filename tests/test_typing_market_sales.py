"""Пути, которые правились ради mypy: поведение то же, крайние случаи не падают TypeError."""

import sqlite3
from datetime import timedelta

import pytest

from mcp_devian import db, sales, selling, th_auctions, th_market
from mcp_devian.th_market import Money, Stated

NOW = 1_800_000_000.0


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.sqlite3")
    yield c
    c.close()


def test_floor_takes_low_or_high_and_skips_empty():
    st = Stated(side="sale", amounts=[Money(None, 30.0, "usd"), Money(20.0, 40.0, "usd"), Money(None, None, "usd")])
    assert st.floor("usd") == 20.0
    assert Stated(side="sale", amounts=[Money(None, None, "usd")]).floor("usd") is None


def test_record_returns_new_id(conn):
    sid, what = sales.record(conn, title="Moth", amount=10, currency="usd", platform="deviantart",
                             format="flatsale", now=NOW)
    assert what == "new" and sales.get(conn, sid)["title"] == "Moth"


def test_record_race_merges_into_auto_row(conn, monkeypatch):
    real = sales.add_sale

    def racing(c, **kw):  # фон записал победителя между SELECT и INSERT
        real(c, title="auto", amount=5, currency="usd", platform="deviantart", format="auction",
             item_id=kw["item_id"], source="auto", now=NOW)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: sales.item_id")

    monkeypatch.setattr(sales, "add_sale", racing)
    sid, what = sales.record(conn, url="https://www.deviantart.com/her/art/moth-123", title="Moth", amount=10,
                             currency="usd", platform="", format="auction", now=NOW)
    assert what == "merged" and sales.get(conn, sid)["amount"] == 10


def test_record_integrity_error_without_lot_is_not_typeerror(conn, monkeypatch):
    def broken(c, **kw):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: sales.id")

    monkeypatch.setattr(sales, "add_sale", broken)
    with pytest.raises(sqlite3.IntegrityError):
        sales.record(conn, title="Moth", amount=10, currency="usd", platform="deviantart", format="flatsale", now=NOW)


def test_parse_terms_sb_sets_currency():
    t = th_auctions.parse_terms("SB $10, AB $50")
    assert t.sb == 10 and t.currency == "usd"


def test_tz_after_offset_is_tzinfo():
    zone, name, err = selling._tz_after(" UTC+3")
    assert err == "" and zone is not None and zone.utcoffset(None) == timedelta(hours=3)


def test_money_span_with_known_currency():
    assert th_market.money(None, "usd") == "?"
    assert th_market._span(th_market.stats([10.0, 20.0]), "usd") == "$10–$20"
