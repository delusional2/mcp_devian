"""Наборы из непроданного и оплата частями: цена от её продаж или «вывода нет»."""

import pytest

from mcp_devian import db, offers, sales
from mcp_devian.db import Item

DAY = 86400
NOW = 1_800_000_000.0
_n = [0]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "o.sqlite3")
    yield c
    c.close()


def lot(conn, title, usd=20.0, age_days=10, lot_type="flatsale", status="open", points=None):
    _n[0] += 1
    iid = f"da:{7000 + _n[0]}"
    pub = NOW - age_days * DAY
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=title, url=f"https://www.deviantart.com/her/art/x-{iid}",
                              author="her", lot_type=lot_type, status=status, own=True, published_ts=pub,
                              price_usd=usd if points is None else None, price_points=points), now=pub)
    return iid


def sold(conn, n, amount=30.0, title="Moth adopt", currency="usd"):
    for i in range(n):
        iid = lot(conn, f"{title} {i}", usd=amount, age_days=40 + i, status="closed")
        sales.add_sale(conn, title=f"{title} {i}", amount=amount + i, currency=currency, platform="deviantart",
                       format="flatsale", item_id=iid, status="paid", now=NOW - (30 + i) * DAY)


def test_bundle_needs_two_stale_similar_lots(conn):
    lot(conn, "Moth adopt A")
    lot(conn, "Fox adopt B")
    lot(conn, "Moth adopt fresh", age_days=2)            # ещё не залежался
    assert offers.bundle_suggestions(conn, NOW) == []
    assert "Набора не собрать" in offers.render_bundles([])
    lot(conn, "Moth adopt C")
    found = offers.bundle_suggestions(conn, NOW)
    assert len(found) == 1 and found[0]["key"] == "moth" and len(found[0]["lots"]) == 2


def test_bundle_without_history_says_no_conclusion(conn):
    lot(conn, "Moth adopt A")
    lot(conn, "Moth adopt C")
    b = offers.bundle_suggestions(conn, NOW)[0]
    assert b["price"] is None and b["median"] is None and "Вывода нет" in b["text"]
    assert "$" not in b["text"]


def test_bundle_priced_from_her_sales_median_with_evidence(conn):
    sold(conn, 9)                                          # 30..38 → медиана 34
    lot(conn, "Moth adopt A")
    lot(conn, "Moth adopt C")
    b = offers.bundle_suggestions(conn, NOW)[0]
    assert b["median"] == 34 and b["price"] == 70.0        # 68 → округление цены
    assert "n=9" in b["text"] and "её продажи" in b["text"] and "уверенность" in b["text"]
    assert "общая практика" in b["text"]


def test_bundle_keeps_currencies_apart(conn):
    lot(conn, "Moth adopt A")
    lot(conn, "Moth adopt P", points=500)
    assert offers.bundle_suggestions(conn, NOW) == []


def test_payment_plan_only_when_enabled_and_above_median():
    assert offers.payment_plan_suggestion(120, 40, enabled=False) is None
    assert offers.payment_plan_suggestion(30, 40, enabled=True) is None
    assert offers.payment_plan_suggestion(120, None, enabled=True) is None
    plan = offers.payment_plan_suggestion(121, 40, enabled=True)
    assert plan["deposit"] == 60.5 and plan["rest"] == 60.5
    assert "$60.50" in plan["text"] and "after full payment" in plan["text"]
    # Срок остатка не придумываем: без её правила — заглушка, с правилом — её слова.
    assert "30 days" not in plan["text"] and "<N> days" in plan["text"]
    own = offers.payment_plan_suggestion(121, 40, enabled=True, holds_policy="Rest within 2 weeks.")
    assert "Rest within 2 weeks." in own["text"] and "<N>" not in own["text"]
    assert "перс передаётся после полной оплаты" in plan["note"] and "общая практика" in plan["note"]


def test_bundle_refusal_never_says_have_more_than_needed(conn, monkeypatch):
    """8+ продаж, но интервал широкий — «вывода нет» без «10 из нужных 8»."""
    from mcp_devian import evidence

    sold(conn, 10)
    lot(conn, "Moth adopt A")
    lot(conn, "Moth adopt C")
    monkeypatch.setattr(evidence, "level", lambda ev: "нет вывода")
    b = offers.bundle_suggestions(conn, NOW)[0]
    assert b["price"] is None and "Вывода нет" in b["text"]
    assert "из нужных" not in b["text"] and "слишком разные" in b["text"]
