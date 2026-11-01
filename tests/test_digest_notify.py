from mcp_devian import digest
from mcp_devian.config import Config, Notify


def test_notify_lines_macos_and_telegram(monkeypatch):
    told, tg = [], []
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: told.append((t, m)) or True)
    monkeypatch.setattr(digest, "send_telegram", lambda chunks: tg.extend(chunks) or None)
    cfg = Config(notify=Notify(macos=True, telegram=True))
    assert digest.notify_lines(cfg, "Нашла покупателя на Toyhouse", ["«LF wolves» — Ember", "«LF cats» — Mia"])
    assert told[0][0] == "Нашла покупателя на Toyhouse" and "+ещё 1" in told[0][1]
    assert "LF cats" in tg[0] and "&lt;" not in tg[0]


def test_notify_lines_nothing(monkeypatch):
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: (_ for _ in ()).throw(AssertionError("no")))
    monkeypatch.setattr(digest, "send_telegram", lambda chunks: (_ for _ in ()).throw(AssertionError("no")))
    assert digest.notify_lines(Config(notify=Notify(macos=True, telegram=True)), "x", []) is False
    assert digest.notify_lines(Config(notify=Notify(macos=False, telegram=False)), "x", ["a"]) is False


def test_notify_lines_telegram_error_is_not_delivered(monkeypatch):
    monkeypatch.setattr(digest, "send_telegram", lambda chunks: "нет токена")
    assert digest.notify_lines(Config(notify=Notify(macos=False, telegram=True)), "x", ["a"]) is False
