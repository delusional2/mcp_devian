"""Пост в X: варианты a/b, хэштеги только из её списков, без чужих @, её отметка и честный отчёт."""

from urllib.parse import parse_qs, urlparse

import pytest

from mcp_devian import db, xpost
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400
URL = "https://www.deviantart.com/mothy/art/Pastel-moth-dragon-100"
TEMPLATE = "{title} is up! {price}"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "x.sqlite3")
    db.upsert_item(c, Item(id="da:100", source="deviantart", title="Pastel moth dragon adopt", url=URL, own=True,
                           lot_type="auction", price_text="SB $20", published_ts=NOW - DAY,
                           extra={"tags": ["moth", "dragon", "adopt"]}), now=NOW)
    c.commit()
    yield c
    c.close()


def item(conn, item_id="da:100"):
    return db.get_item(conn, item_id)


def q(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_variant_a_keeps_link_for_first_reply(conn):
    res = xpost.build(conn, item(conn), TEMPLATE + " {url}", ["adoptables", "ocadopt"], [], "a",
                      "/tmp/none.png", now=NOW)
    assert res["ok"] and URL not in res["text"] and res["first_reply_text"] == URL
    params = q(res["intent_url"])
    assert "url" not in params and params["text"] == "Pastel moth dragon adopt is up! SB $20"
    assert params["hashtags"] == "adoptables,ocadopt"
    assert any("картинки" in w for w in res["warnings"])
    assert any("{url}" in n for n in res["notes"])


def test_variant_b_link_in_post(conn):
    res = xpost.build(conn, item(conn), TEMPLATE, ["adoptables"], [], "b", now=NOW)
    assert q(res["intent_url"])["url"] == URL and res["first_reply_text"] == ""
    inline = xpost.build(conn, item(conn), TEMPLATE + " {url}", [], [], "b", now=NOW)
    assert URL in inline["text"] and "url" not in q(inline["intent_url"])
    assert not xpost.build(conn, item(conn), TEMPLATE, [], [], "c", now=NOW)["ok"]


def test_needs_her_template_and_known_slots(conn):
    assert "шаблона" in xpost.build(conn, item(conn), "", now=NOW)["reason"]
    assert "{buyer}" in xpost.build(conn, item(conn), "Hi {buyer}", now=NOW)["reason"]


def test_long_post_warned(conn):
    res = xpost.build(conn, item(conn), "x" * 260 + " {title}", [], [], "b", now=NOW)
    assert res["length"] > 270 and any("длина" in w for w in res["warnings"])
    short = xpost.build(conn, item(conn), "{title}", [], [], "a", now=NOW + 1)
    assert not any("длина" in w for w in short["warnings"])


def test_same_text_as_last_post_warned(conn):
    xpost.build(conn, item(conn), TEMPLATE, [], [], "a", now=NOW)
    again = xpost.build(conn, item(conn), TEMPLATE, [], [], "a", now=NOW + 60)
    assert any("такой же текст" in w for w in again["warnings"])


def test_hashtags_only_from_her_lists_max_four(conn):
    res = xpost.build(conn, item(conn), TEMPLATE, ["#adoptables", "adopts", "ocadopt", "extra1"],
                      ["mothadopt", "dragonadopt", "catadopt", "furryadopt"], "a", now=NOW)
    assert res["hashtags"] == ["adoptables", "adopts", "ocadopt", "mothadopt"]
    assert xpost.NICHE_NOTE in res["notes"]
    plain = xpost.build(conn, item(conn), TEMPLATE, ["adoptables"], ["catadopt"], "a", now=NOW)
    assert plain["hashtags"] == ["adoptables"] and xpost.NICHE_NOTE not in plain["notes"]


def test_no_mentions_unless_tag_ok(conn):
    accounts = [{"handle": "AdoptRT", "note": "use #adoptrt, no tags", "tag_ok": False},
                {"handle": "TagMe_RT", "tag_ok": True}, {"handle": "Shy", "tag_ok": "yes"}]
    res = xpost.build(conn, item(conn), TEMPLATE, [], [], "a", rt_accounts=accounts, now=NOW)
    assert "@TagMe_RT" in res["text"] and "@AdoptRT" not in res["text"] and "@Shy" not in res["text"]
    assert any("AdoptRT" in n and "use #adoptrt" in n for n in res["notes"])


def test_confirm_posted_and_reply_intent(conn):
    res = xpost.build(conn, item(conn), TEMPLATE, [], [], "a", now=NOW)
    bad = xpost.confirm_posted(conn, res["id"], "https://example.com/1", now=NOW)
    assert not bad["ok"]
    ok = xpost.confirm_posted(conn, res["id"], "https://x.com/mothy/status/1850000000000000001", now=NOW + 60)
    assert ok["ok"] and q(ok["reply_intent"]) == {"text": URL, "in_reply_to": "1850000000000000001"}
    row = conn.execute("SELECT * FROM x_posts WHERE id = ?", (res["id"],)).fetchone()
    assert row["posted_ts"] == NOW + 60 and row["post_url"].endswith("0001")
    assert not xpost.confirm_posted(conn, "nope", "https://x.com/m/status/123456", now=NOW)["ok"]


def _posted(conn, n, variant, sold, start):
    for i in range(n):
        item_id = f"da:{variant}{start + i}"
        db.upsert_item(conn, Item(id=item_id, source="deviantart", title=f"Lot {i}", url=f"{URL}{i}", own=True),
                       now=NOW)
        conn.commit()
        res = xpost.build(conn, db.get_item(conn, item_id), f"Lot {variant} {i}", [], [], variant, now=NOW)
        xpost.confirm_posted(conn, res["id"], f"https://x.com/m/status/{1000000 + start + i}", now=NOW + i)
        if i < sold:
            conn.execute("INSERT INTO sales (id, item_id, platform, title, status, created_ts) "
                         "VALUES (?, ?, 'deviantart', 't', 'paid', ?)", (f"s{variant}{start + i}", item_id, NOW + DAY))
    conn.commit()


def test_variant_report_says_no_conclusion_below_threshold(conn):
    _posted(conn, 5, "a", 4, 0)
    _posted(conn, 9, "b", 1, 100)
    rep = xpost.variant_report(conn, now=NOW + 20 * DAY)
    assert rep["verdict"] is None and "вывода нет" in rep["text"] and rep["a"] == {"k": 4, "n": 5}


def test_variant_report_with_enough_data_has_evidence_tag(conn):
    _posted(conn, 12, "a", 10, 0)
    _posted(conn, 12, "b", 1, 100)
    rep = xpost.variant_report(conn, now=NOW + 20 * DAY)
    assert rep["verdict"] == "better" and "n=12" in rep["text"] and "уверенность" in rep["text"]
    assert "Не чистый опыт" in rep["text"]


def test_variant_report_ignores_lots_still_selling(conn):
    """Регрессия: лот без продажи считался непроданным, даже если ещё продаётся, — свежий вариант выглядел хуже."""
    _posted(conn, 10, "a", 4, 0)
    _posted(conn, 10, "b", 0, 100)
    rep = xpost.variant_report(conn, now=NOW + 2 * DAY)       # b ещё идут, a тоже — итогов мало
    assert rep["verdict"] is None and rep["b"]["n"] == 0 and "Ещё продаются" in rep["text"]


def test_variant_report_skips_repromote_posts_and_weak_sides(conn, monkeypatch):
    """Регрессия: второй заход (лоты, что уже не продались) шёл в сравнение a/b и всегда давал «b хуже»."""
    _posted(conn, 12, "a", 10, 0)
    for i in range(12):
        item_id = f"da:r{i}"
        db.upsert_item(conn, Item(id=item_id, source="deviantart", title=f"Old {i}", url=f"{URL}r{i}", own=True),
                       now=NOW)
        res = xpost.build(conn, db.get_item(conn, item_id), f"Again {i}", [], [], "b", now=NOW, push="repromote")
        xpost.confirm_posted(conn, res["id"], f"https://x.com/m/status/{2000000 + i}", now=NOW + i)
    rep = xpost.variant_report(conn, now=NOW + 20 * DAY)
    assert rep["b"]["n"] == 0 and rep["verdict"] is None
    # Очередь вариантов — внутри своего вида захода.
    assert xpost.next_variant(conn, "first") == "b" and xpost.next_variant(conn, "repromote") == "a"


def test_no_verdict_when_a_side_has_no_confidence(conn, monkeypatch):
    _posted(conn, 12, "a", 10, 0)
    _posted(conn, 12, "b", 1, 100)
    monkeypatch.setattr(xpost.evidence, "level", lambda ev: "нет вывода")
    rep = xpost.variant_report(conn, now=NOW + 20 * DAY)
    assert rep["verdict"] is None and "вывода нет" in rep["text"]


def test_prefs_from_her_single_list():
    class P:
        x_hashtags = ["adoptables", "dragonadopt", "adoptsopen", "mothadopt"]

    default, niche = xpost.prefs(P())
    assert default == ["adoptables", "adoptsopen"] and niche == ["dragonadopt", "mothadopt"]

    class D:
        x_hashtags = {"default": ["a"], "allowed": ["b"]}

    assert xpost.prefs(D()) == (["a"], ["b"])


def test_no_post_for_someone_elses_lot(conn):
    """Регрессия: для чужого лота собирался готовый пост и ссылка."""
    db.upsert_item(conn, Item(id="da:200", source="deviantart", title="Not hers", url=URL + "-200", own=False,
                              lot_type="auction"), now=NOW)
    conn.commit()
    res = xpost.build(conn, item(conn, "da:200"), TEMPLATE, [], [], "b", now=NOW)
    assert not res["ok"] and "ЕЁ" in res["reason"]
    assert conn.execute("SELECT COUNT(*) FROM x_posts").fetchone()[0] == 0
