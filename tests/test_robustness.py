"""Регрессии по итогам ревью: сеть, бэкофф, доставка, конфиг."""

import httpx
import pytest
from test_monitor import NOW, da_cfg, fake_http  # noqa: F401  (фикстуры ниже)

from mcp_devian import db, digest, monitor
from mcp_devian.classify import ThreadKind
from mcp_devian.config import Config, Filters, Notify, Toyhouse, load_config


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(monitor.da, "polite_pause", lambda: None)


def test_offline_deviantart_becomes_error_not_crash(conn):
    def offline(req):
        raise httpx.ConnectError("no route to host")

    http = httpx.Client(transport=httpx.MockTransport(offline))
    res = monitor.check_deviantart(conn, da_cfg(), NOW, http=http)
    assert res.error and "нет связи" in res.error
    assert db.last_run(conn, "deviantart")["last_error"] == res.error


def test_one_source_crash_does_not_stop_the_other(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("неожиданное")

    ran = []
    monkeypatch.setattr(monitor, "check_deviantart", boom)
    monkeypatch.setattr(monitor, "check_toyhouse", lambda *a, **k: ran.append(1) or monitor.SourceResult("toyhouse"))
    results = monitor.run_check(conn, Config(), force=True, now=NOW)
    assert results[0].error.startswith("RuntimeError") and ran == [1]
    assert db.last_run(conn, "deviantart")["last_error"]


def test_backoff_after_errors(conn):
    db.record_run(conn, "toyhouse", "cf", NOW)
    db.record_run(conn, "toyhouse", "cf", NOW)  # две ошибки подряд → интервал x4
    assert not monitor.is_due(conn, "toyhouse", 45, NOW + 45 * 60)
    assert monitor.is_due(conn, "toyhouse", 45, NOW + 4 * 45 * 60)
    db.record_run(conn, "toyhouse", None, NOW)
    assert db.last_run(conn, "toyhouse")["fail_count"] == 0


def test_backoff_is_capped(conn):
    for _ in range(20):
        db.record_run(conn, "deviantart", "429", NOW)
    assert monitor.is_due(conn, "deviantart", 30, NOW + monitor.MAX_BACKOFF_S)


def test_manual_cooldown(conn):
    db.record_run(conn, "toyhouse", None, NOW)
    assert monitor.cooldown_reason(conn, "toyhouse", NOW + 60)
    assert monitor.cooldown_reason(conn, "toyhouse", NOW + 11 * 60) is None
    db.record_run(conn, "deviantart", "DeviantArt просит притормозить (429)", NOW)
    assert "притормозить" in monitor.cooldown_reason(conn, "deviantart", NOW + 5 * 60)

    results = monitor.run_check(conn, Config(), sources=("toyhouse",), force=True, now=NOW + 60,
                                manual_cooldown=True)
    assert results[0].skipped


def test_broken_telegram_does_not_repeat_notifications(conn, monkeypatch):
    monitor.check_deviantart(conn, da_cfg(), NOW, http=fake_http())
    sent = []
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: sent.append((t, m)) or True)
    monkeypatch.setattr(digest, "send_telegram", lambda chunks: "Telegram ответил 401: Unauthorized")
    cfg = Config(notify=Notify(macos=True, telegram=True))

    first = digest.deliver(conn, cfg)
    assert first["items"] == 1 and db.pending_digest(conn) == []
    assert [t for t, _ in sent] == ["Адопты", "mcp_devian"]  # дайджест + одна жалоба на Telegram

    sent.clear()
    conn.execute("UPDATE items SET notified = 0")  # новые находки
    digest.deliver(conn, cfg)
    assert [t for t, _ in sent] == ["Адопты"]  # та же ошибка Telegram второй раз не шлётся


def test_notify_macos_never_raises(monkeypatch):
    import subprocess

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("osascript", 10)

    monkeypatch.setattr(digest.subprocess, "run", hang)
    assert digest.notify_macos("t", "m") is False


def test_send_telegram_network_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    class Down:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(digest.httpx, "Client", Down)
    assert "недоступен" in digest.send_telegram(["hi"])


def test_digest_line_with_dash_in_title(conn):
    monitor.check_deviantart(conn, da_cfg(), NOW, http=fake_http())
    conn.execute("UPDATE items SET title = 'Wolf adopt — OPEN' WHERE id = 'da:1404000001'")
    md = digest.render_markdown(db.pending_digest(conn), NOW)
    assert "[Wolf adopt — OPEN](" in md and "— auction" in md


def test_sale_thread_goes_to_sale_section():
    cfg = Config(filters=Filters(keywords=["dragon"]), toyhouse=Toyhouse(lf_keywords=["cat"]))
    kind = ThreadKind(wants_characters=True, sells_characters=True, pays=[])
    match_kind, reason = monitor.match_thread(cfg, kind, "Selling dragon adopts | LF: wolf OCs", None, NOW, NOW)
    assert match_kind == "sale" and reason.startswith("продают")


def test_config_warnings(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[toyhouse]\nboard = [71]\n[filtres]\nkeywords = ["x"]\n', encoding="utf-8")
    cfg = load_config(path)
    assert any("board" in w and "boards" in w for w in cfg.warnings)
    assert any("filtres" in w for w in cfg.warnings)
    assert cfg.toyhouse.boards == [13, 5866]


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (source TEXT PRIMARY KEY, last_run_ts REAL, last_ok_ts REAL, last_error TEXT)")
    old.commit()
    old.close()
    with db.session(path) as conn:
        db.record_run(conn, "toyhouse", "x", NOW)
        assert db.last_run(conn, "toyhouse")["fail_count"] == 1
