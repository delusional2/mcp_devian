"""Второй заход для непроданных лотов: группы по правилам, без перезаливов, раз в 7 дней на лот."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_devian import db, drafts, pinglist, repromote, selling
from mcp_devian.config import Config
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400
URL = "https://www.deviantart.com/mothy/art/Moth-adopt-100"
URL2 = "https://www.deviantart.com/mothy/art/Other-200"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "r.sqlite3")
    for item_id, url, title in (("da:100", URL, "Moth adopt"), ("da:200", URL2, "Other")):
        db.upsert_item(c, Item(id=item_id, source="deviantart", title=title, url=url, own=True, lot_type="flatsale",
                               published_ts=NOW - 10 * DAY), now=NOW)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def cfg():
    c = Config()
    c.posting.group_presets = {
        "main": [{"group": "Used-Api", "folder_name": "Open"}, {"group": "Used-Manual", "folder_name": "Open"},
                 {"group": "Weekly", "folder_name": "Adopts", "limit": {"count": 1, "days": 7}},
                 {"group": "Free", "folder_name": "Adopts", "limit": {"count": 2, "days": 1}}],
        "extra": [{"group": "free", "folder_name": "dup"}, {"group": "Second", "folder_name": "x"}]}
    return c


@pytest.fixture(autouse=True)
def fake_unsold(monkeypatch):
    def unsold(conn, now=None, tz="Europe/Moscow", limit=5, **kw):
        lots = [{"item_id": "da:100", "title": "Moth adopt", "url": URL, "why": "висит 10 дн",
                 "options": [{"kind": "repost", "text": "перевыложить: в будни вечером"}]}]
        return {"lots": lots[:limit], "total": 1, "unread": 0, "best_time": None}

    monkeypatch.setattr(selling, "unsold", unsold)


def test_excludes_used_and_over_limit_groups(conn, cfg):
    drafts.log_sent(conn, "da_group", "used-api", "draft1", True, detail=URL, now=NOW - 5 * DAY)
    drafts.log_sent(conn, "da_group_manual", "used-manual", "da:100", True, detail=URL, now=NOW - 20 * DAY)
    drafts.log_sent(conn, "da_group_manual", "weekly", "da:200", True, detail=URL2, now=NOW - 2 * DAY)
    res = repromote.plan(conn, cfg, now=NOW)
    p = res["lots"][0]
    assert [g["group"] for g in p["groups"]] == ["Free", "Second"]
    assert p["groups"][0]["rule"] == "2 за 1 дн"
    joined = " ".join(p["skipped_groups"])
    assert "Used-Api: эта работа там уже была" in joined and "Used-Manual" in joined
    assert "Weekly: правило группы Weekly: 1 за 7 дн" in joined
    assert "promo:group_done" in res["text"]


def test_never_suggests_reupload(conn, cfg):
    text = repromote.plan(conn, cfg, now=NOW)["text"].lower()
    assert "перевыложить" not in text and "re-upload" not in text and "reupload" not in text
    assert repromote.NO_REUPLOAD.lower() in text


def test_once_per_seven_days_per_lot(conn, cfg):
    selling.ensure(conn)
    conn.execute("INSERT INTO selling_templates (kind, text, updated_ts) VALUES ('x_post', '{title} is open', 0)")
    conn.commit()
    first = repromote.plan(conn, cfg, now=NOW)
    assert first["lots"][0]["x_post"]["ok"]
    same_day = repromote.plan(conn, cfg, now=NOW + 3600)
    assert same_day["lots"][0]["repeat"] and same_day["lots"][0]["x_post"]["id"] == first["lots"][0]["x_post"]["id"]
    assert conn.execute("SELECT COUNT(*) FROM x_posts").fetchone()[0] == 1
    from mcp_devian import xpost

    xpost.confirm_posted(conn, first["lots"][0]["x_post"]["id"], "https://x.com/m/status/1234567", now=NOW + 7200)
    later = repromote.plan(conn, cfg, now=NOW + 3 * DAY)
    assert later["lots"] == [] and "не раньше" in later["waiting"][0] and "пост X" in later["waiting"][0]
    again = repromote.plan(conn, cfg, now=NOW + 8 * DAY)
    assert again["lots"] and not again["lots"][0].get("repeat")


def test_just_viewing_the_plan_does_not_start_cooldown(conn, cfg):
    """Регрессия: показ плана сам записывал «уже продвигали» — через 2 дня она видела «не раньше 09.11»,
    хотя ничего не сделала."""
    repromote.plan(conn, cfg, now=NOW)
    later = repromote.plan(conn, cfg, now=NOW + 2 * DAY)
    assert later["lots"] and not later["lots"][0].get("repeat") and not later["waiting"]
    repromote.log_manual_group(conn, URL, "Free", now=NOW + 2 * DAY + 60, cfg=cfg)
    after = repromote.plan(conn, cfg, now=NOW + 3 * DAY)
    assert after["lots"] == [] and "группа free" in after["waiting"][0]


def test_ping_only_if_never_pinged(conn, cfg):
    p = repromote.plan(conn, cfg, now=NOW)["lots"][0]
    assert "pinglist:ping" in p["ping"]
    pinglist.mark_pinged(conn, "da:100", ["fan1"], now=NOW)
    assert repromote.ever_pinged(conn, "da:100")
    p = repromote.plan(conn, cfg, now=NOW + 8 * DAY)["lots"][0]
    assert p["ping"] == ""


def test_ping_draft_in_flight_counts_as_pinged(conn):
    drafts.create(conn, "da_ping", {"deviation_id": "da:100", "comments": []}, "p", now=NOW)
    assert repromote.ever_pinged(conn, "da:100") and not repromote.ever_pinged(conn, "da:200")


def test_log_manual_group_counts_for_limits(conn, cfg):
    text = repromote.log_manual_group(conn, URL, "Weekly", now=NOW, cfg=cfg)
    assert "Записала" in text
    rows = conn.execute("SELECT target, ref, detail FROM sent_log WHERE kind = 'da_group_manual'").fetchall()
    assert [(r["target"], r["ref"], r["detail"]) for r in rows] == [("weekly", "da:100", URL)]
    assert "Внимание" in repromote.log_manual_group(conn, "da:200", "Weekly", now=NOW + 60, cfg=cfg)
    assert "нет в базе" in repromote.log_manual_group(conn, "https://www.deviantart.com/x/art/y-9", "Weekly", now=NOW)


def test_rank_groups_injected(conn, cfg):
    res = repromote.plan(conn, cfg, now=NOW, rank_groups=lambda c, groups, now: list(reversed(groups)))
    assert [g["group"] for g in res["lots"][0]["groups"]][0] == "Second"


def test_next_slot_from_best_window():
    best = {"slot": ("weekend", 3), "level": "средняя", "evidence": "x"}
    ts = repromote.next_slot(best, NOW, "Europe/Moscow")
    local = datetime.fromtimestamp(ts, ZoneInfo("Europe/Moscow"))
    assert local.weekday() >= 5 and 18 <= local.hour < 24 and ts > NOW
    assert repromote.next_slot({**best, "level": "низкая"}, NOW, "Europe/Moscow") is None
    assert repromote.next_slot(None, NOW, "Europe/Moscow") is None
