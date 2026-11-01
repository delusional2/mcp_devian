import pytest

from mcp_devian import feedback, mcp_server
from mcp_devian.config import Config, save_config

NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_FILE", tmp_path / "feedback.jsonl")
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: False)


def test_leave_and_report():
    assert "Сохранила" in mcp_server.leave_feedback("Прогноз непонятный", topic="прогноз", rating=3)
    mcp_server.leave_feedback("Хочу уведомления в Telegram")
    report = mcp_server.feedback_report()
    assert "Прогноз непонятный" in report and "оценка 3/5" in report and "Telegram" in report
    assert "Режим Toyhouse" in report
    assert "отправленных отзывов: 2" in mcp_server.feedback_sent()
    assert mcp_server.feedback_report() == "Отзывов пока нет."


def test_empty_and_bad_rating():
    assert "Пустой" in mcp_server.leave_feedback("   ")
    mcp_server.leave_feedback("ок", rating=9)
    assert "оценка" not in mcp_server.feedback_report()


def test_should_ask_rhythm():
    # Первый день после настройки не спрашиваем.
    assert not feedback.should_ask(now=NOW, configured_since=NOW - 3600)
    assert feedback.should_ask(now=NOW, configured_since=NOW - 2 * DAY)
    feedback.add("норм", now=NOW)
    assert not feedback.should_ask(now=NOW + DAY, configured_since=NOW - 10 * DAY)
    assert feedback.should_ask(now=NOW + 4 * DAY, configured_since=NOW - 10 * DAY)


def test_declined_counts_as_asked():
    mcp_server.leave_feedback(declined=True)
    assert not feedback.should_ask(configured_since=1)
    assert mcp_server.feedback_report() == "Отзывов пока нет."


def test_get_settings_suggests_asking_gently(monkeypatch):
    cfg = Config(configured=True, configured_at=1)
    save_config(cfg)
    assert "мягко спроси" in mcp_server.get_settings()
    mcp_server.leave_feedback("всё хорошо")
    assert "мягко спроси" not in mcp_server.get_settings()


def test_corrupt_feedback_lines_are_skipped():
    feedback.FEEDBACK_FILE.write_text('{"ts": 1, "text": "ок"}\nне json\n[1]\n', encoding="utf-8")
    assert len(feedback.entries()) == 1
