"""Отправка после ревью 0.4: «на потом» не уходит сразу, после необратимого шага — не «не отправился»,
прошлое время не принимается, неделя считается от времени публикации, паузы внутри замка."""

import json

import pytest
from test_scheduling import HOUR, NOW, FakeDA, Sleeps, image

from mcp_devian import cli, db, digest, drafts, posting
from mcp_devian.config import Config, Toyhouse

GROUP = {"group": "da-adopts", "group_id": "g-1", "folder_id": "f-1", "folder_name": "auctions"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()


@pytest.fixture
def fake(monkeypatch):
    f = FakeDA()
    monkeypatch.setattr(posting, "da_client", lambda c: f)
    return f


@pytest.fixture
def cfg():
    c = Config(toyhouse=Toyhouse(mode="chrome"))
    c.posting.da_client_id = "123"
    c.posting.group_presets = {"a": [dict(GROUP)]}
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.sqlite3")
    yield c
    c.close()


def test_planned_draft_does_not_go_out_on_plain_send(conn, cfg, fake, tmp_path):
    d, preview = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW, schedule="20:00")
    assert "Когда:" in preview
    out = posting.send(conn, cfg, d, now=NOW)
    assert "schedule_draft" in out and fake.published == []
    assert drafts.get(conn, d, now=NOW)["status"] == "pending"
    posting.approve_draft(conn, cfg, d, now=NOW)
    assert posting.send_due(conn, cfg, now=NOW + 9 * HOUR, sleep=Sleeps())[0]["status"] == "sent"


def test_planned_batch_skipped_by_send_batch(conn, cfg, fake, tmp_path):
    ids, _ = posting.prepare_da_batch(conn, cfg, [{"file_path": str(image(tmp_path, "x.png")), "title": "X"}],
                                      schedule="20:00", now=NOW)
    batch = json.loads(drafts.get(conn, ids[0], now=NOW)["payload"])["batch_id"]
    assert posting.send_batch(conn, cfg, batch, now=NOW, sleep=Sleeps()) == [] and fake.published == []


def test_error_after_publish_is_unknown_not_failed(conn, cfg, fake, tmp_path, monkeypatch):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", preset="a", now=NOW)

    def locked(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(drafts, "settle", locked)
    out = posting.send(conn, cfg, d, now=NOW)
    assert out.startswith("опубликовано") and len(fake.published) == 1
    assert drafts.get(conn, d, now=NOW)["status"] == "sent"


def test_unexpected_sender_crash_is_unknown(conn, cfg, fake, tmp_path, monkeypatch):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)

    def boom(*a, **k):
        raise KeyError("x")
    monkeypatch.setattr(posting, "_send_da_publish", boom)
    out = posting.send(conn, cfg, d, now=NOW)
    assert drafts.UNKNOWN_RESULT in out and drafts.get(conn, d, now=NOW)["status"] == "unknown"


def test_stash_upload_failure_is_plain_failed(conn, cfg, fake, tmp_path, monkeypatch):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)

    def down(*a, **k):
        raise OSError("network")
    monkeypatch.setattr(fake, "stash_submit", down)
    assert "публично ничего не появилось" in posting.send(conn, cfg, d, now=NOW)
    assert drafts.get(conn, d, now=NOW)["status"] == "failed"


@pytest.mark.parametrize("when", ["сегодня 10:00", "2025-11-03 19:00", "31.10 19:00"])
def test_past_time_is_refused(conn, cfg, fake, tmp_path, when):
    d, text = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW, schedule=when)
    assert d is None and "уже прошло" in text
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path, "b.png")), "Moth", now=NOW)
    assert "уже прошло" in posting.approve_draft(conn, cfg, d, when, now=NOW)
    assert drafts.get(conn, d, now=NOW)["status"] == "pending"


def test_just_typed_time_is_not_past(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    assert "уже прошло" not in posting.approve_draft(conn, cfg, d, NOW - 60, now=NOW)


def test_planned_time_passed_before_go_asks_again(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW, schedule="13:00")
    assert "уже прошло" in posting.approve_draft(conn, cfg, d, now=NOW + 2 * HOUR)


def test_schedule_near_week_limit_still_sent_after_short_sleep(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    send_at = NOW + drafts.SCHEDULE_MAX_AGE_S - 60
    assert "Запланировано" in posting.approve_draft(conn, cfg, d, send_at, now=NOW)
    results = posting.send_due(conn, cfg, now=send_at + 2 * HOUR, sleep=Sleeps())  # мак спал пару часов
    assert results[0]["status"] == "sent"


def test_schedule_expires_a_day_after_its_time(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    posting.approve_draft(conn, cfg, d, "20:00", now=NOW)
    send_at = drafts.get(conn, d, now=NOW)["send_at"]
    later = send_at + drafts.SCHEDULE_GRACE_S + HOUR
    assert posting.send_due(conn, cfg, now=later, sleep=Sleeps())[0]["status"] == "cancelled"
    assert fake.published == []


def test_pace_runs_inside_the_lock(conn, cfg, fake, tmp_path, monkeypatch):
    order = []
    real_lock = posting.send_lock

    def tracked_lock(*a, **k):
        order.append("lock")
        return real_lock(*a, **k)
    monkeypatch.setattr(posting, "send_lock", tracked_lock)
    monkeypatch.setattr(posting, "_pace", lambda *a, **k: order.append("pace"))
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    posting._send_paced(conn, cfg, d, NOW, Sleeps(), None)
    assert order == ["lock", "pace"]


def test_send_respects_off_switch(conn, cfg, fake, tmp_path, monkeypatch):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    monkeypatch.setattr(posting, "SENDING_ENABLED", False)
    assert posting.send_any(conn, cfg, d, now=NOW) == posting.NOT_YET and fake.published == []


def test_stale_pending_drafts_leave_the_list(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    assert [r["id"] for r in posting.list_open(conn, NOW + HOUR)] == [d]
    assert posting.list_open(conn, NOW + drafts.DRAFT_TTL_S + HOUR) == []
    assert drafts.get(conn, d)["result"] == drafts.EXPIRED_PENDING


def test_background_busy_send_is_not_notified(monkeypatch):
    told = []
    monkeypatch.setattr(posting, "send_due", lambda conn, cfg, now: [
        {"draft_id": "d1", "status": "scheduled", "result": posting.BUSY_TEXT}])
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: told.append(m) or True)
    cli._send_scheduled(None, Config(), None)
    assert told == []
