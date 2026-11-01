"""Публикация через MCP-инструменты: папки галереи, партии, расписание; и отправка по расписанию из фона."""

import pytest
from test_scheduling import GROUP, FakeDA

from mcp_devian import cli, db, digest, drafts, mcp_server, posting
from mcp_devian.config import Config, save_config


@pytest.fixture
def fake(monkeypatch):
    f = FakeDA()
    monkeypatch.setattr(posting, "da_client", lambda c: f)
    return f


@pytest.fixture
def server(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: True)
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "s.sqlite3"))
    cfg = Config(configured=True)
    cfg.posting.da_client_id = "123"
    cfg.posting.group_presets = {"a": [dict(GROUP)]}
    save_config(cfg)
    return tmp_path


def image(home, name):
    p = home / "Downloads" / name
    p.write_bytes(b"\x89PNG" + name.encode())
    return str(p)


def test_publish_with_folders_and_schedule(server, fake, monkeypatch):
    out = mcp_server.prepare_deviantart_publish(image(server, "m.png"), "Moth", preset="a",
                                                gallery_folders=["featured"], send_at="завтра 19:00")
    assert out.startswith("Черновик ") and "Featured" in out and "schedule_draft" in out
    draft_id = out.split()[1].rstrip(":")
    assert "Запланировано" in mcp_server.schedule_draft(draft_id)
    assert "запланирован" in mcp_server.draft_status(draft_id)
    assert "запланирован" in mcp_server.list_drafts()
    assert mcp_server.cancel_draft(draft_id) == "Отменила."


def test_schedule_warns_without_background(server, monkeypatch):
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: False)
    out = mcp_server.prepare_deviantart_publish(image(server, "m.png"), "Moth", preset="a")
    draft_id = out.split()[1].rstrip(":")
    assert "set_background_checks" in mcp_server.schedule_draft(draft_id, "завтра 19:00")


def test_batch_submit_status_cancel(server, monkeypatch):
    works = [{"file_path": image(server, f"m{i}.png"), "title": f"Moth {i}", "preset": "a"} for i in range(3)]
    out = mcp_server.prepare_deviantart_batch(works)
    assert out.startswith("Партия p") and "3 работ" in out
    batch_id = out.split()[1].rstrip(":")
    spawned = []
    monkeypatch.setattr(posting, "start_sending", lambda d: spawned.append(d))
    assert "Партия пошла: 3 работ" in mcp_server.submit_draft(batch_id) and spawned == [batch_id]
    assert mcp_server.draft_status(batch_id).count("\n") == 2
    assert mcp_server.cancel_draft(batch_id) == "Отменила работ: 3."
    assert "нечего отправлять" in mcp_server.submit_draft(batch_id)
    assert "нечего отменять" in mcp_server.cancel_draft(batch_id)
    assert mcp_server.draft_status("pffffff0") == "Такой партии нет."


def test_my_gallery_folders(server):
    out = mcp_server.my_gallery_folders()
    assert "- Featured" in out and "Adopts — Open" in out


def test_new_settings(server):
    out = mcp_server.update_settings(default_gallery_folders=["Featured"], timezone="Europe/Berlin",
                                     weekly_report=False)
    assert "Не сохранила" not in out
    assert "(по умолчанию)" in mcp_server.my_gallery_folders()
    assert "Не сохранила" in mcp_server.update_settings(timezone="Mars/Olympus")


def test_new_tools_gated_when_sending_off(server, monkeypatch):
    monkeypatch.setattr(posting, "SENDING_ENABLED", False)
    assert mcp_server.prepare_deviantart_batch([]) == posting.NOT_YET
    assert mcp_server.schedule_draft("x") == posting.NOT_YET


def test_schedule_tool_asks_every_time():
    import asyncio

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert tools["schedule_draft"].annotations.destructive_hint is True
    assert tools["prepare_deviantart_publish"].annotations.destructive_hint is False
    assert "works" in tools["prepare_deviantart_publish"].input_schema["properties"]  # партия — тот же черновик


def test_background_sends_due_and_notifies(monkeypatch, capsys):
    told = []
    monkeypatch.setattr(posting, "send_due", lambda conn, cfg, now: [
        {"draft_id": "d1", "status": "sent", "result": "опубликовано: https://x"}])
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: told.append((t, m)) or True)
    cli._send_scheduled(None, Config(), 1.0)
    assert told == [("Публикация на DeviantArt", "опубликовано: https://x")]
    assert "публикация d1: sent" in capsys.readouterr().out


def test_background_send_result_goes_to_telegram(monkeypatch):
    """Только Telegram, без мака: «исход неизвестен» по расписанию она всё равно узнаёт."""
    monkeypatch.setattr(posting, "send_due", lambda conn, cfg, now: [
        {"draft_id": "d1", "status": "unknown", "result": "исход неизвестен <проверь галерею>"},
        {"draft_id": "d2", "status": "scheduled", "result": "занято"}])
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: pytest.fail("мак выключен"))
    sent = []
    monkeypatch.setattr(digest, "send_telegram", lambda chunks: sent.append(chunks) or None)
    cfg = Config()
    cfg.notify.macos, cfg.notify.telegram = False, True
    cli._send_scheduled(None, cfg, 1.0)
    assert len(sent) == 1 and "d1: исход неизвестен &lt;проверь галерею&gt;" in sent[0][0]
    assert "d2" not in sent[0][0]


def test_background_send_crash_is_reported(monkeypatch):
    def boom(conn, cfg, now):
        raise RuntimeError("сломалось")
    monkeypatch.setattr(posting, "send_due", boom)
    told = []
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: told.append(m) or True)
    cli._send_scheduled(None, Config(), 1.0)
    assert told and "сломалось" in told[0]


def test_send_draft_command_handles_batches(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(posting, "send_any", lambda conn, cfg, i: seen.append(i) or "ok")
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "s.sqlite3"))

    class Args:
        draft_id = "p1234567"
    assert cli.cmd_send_draft(Args()) == 0 and seen == ["p1234567"]
    assert drafts.STATUS_NAMES["scheduled"]


def test_submit_refuses_planned_draft_and_unknown_batch(server, monkeypatch):
    spawned = []
    monkeypatch.setattr(posting, "start_sending", lambda d: spawned.append(d))
    out = mcp_server.prepare_deviantart_publish(image(server, "m.png"), "Moth", send_at="завтра 19:00")
    draft_id = out.split()[1].rstrip(":")
    assert "schedule_draft" in mcp_server.submit_draft(draft_id) and spawned == []
    assert mcp_server.submit_draft("p0000000") == "Такой партии нет."
    assert mcp_server.cancel_draft("p0000000") == "Такой партии нет."


def test_cancel_batch_mentions_work_in_flight(server):
    works = [{"file_path": image(server, f"m{i}.png"), "title": f"Moth {i}"} for i in range(2)]
    out = mcp_server.prepare_deviantart_batch(works)
    batch_id = out.split()[1].rstrip(":")
    with db.session() as c:
        first = posting.batch_rows(c, batch_id)[0]["id"]
        c.execute("UPDATE drafts SET status = 'sending', sent_ts = strftime('%s','now') WHERE id = ?", (first,))
        c.commit()
    text = mcp_server.cancel_draft(batch_id)
    assert text.startswith("Отменила.") and first in text and "не остановить" in text
