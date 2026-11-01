"""Настройка через чат: settings.json, проверка значений, инструменты MCP."""

import pytest

from mcp_devian import config, db, mcp_server, monitor
from mcp_devian.config import Config, SettingsError, Toyhouse, apply_changes, load_config, save_config

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: False)
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "t.sqlite3"))


def test_fresh_install_asks_setup_questions():
    text = mcp_server.get_settings()
    assert "НАСТРОЙКА НЕ ПРОЙДЕНА" in text and "toyhouse_mode" in text


def test_update_settings_roundtrip():
    out = mcp_server.update_settings(buy_keywords=["dragon", "Dragon", " moth "], max_usd=40,
                                     toyhouse_mode="bot", lot_types=["auction", "OTA"], finish_setup=True)
    assert "Сохранено" in out and "Настройка пройдена." in out
    cfg = load_config()
    assert cfg.configured and cfg.filters.keywords == ["dragon", "moth"]
    assert cfg.filters.lot_types == ["auction", "ota"] and cfg.toyhouse.mode == "bot"
    assert cfg.filters.max_usd == 40


@pytest.mark.parametrize("changes", [
    {"toyhouse_mode": "robot"},
    {"lot_types": ["nft"]},
    {"max_usd": "дорого"},
    {"max_usd": -5},
    {"toyhouse_boards": []},
    {"deviantart_poll_minutes": 1},
    {"unknown": 1},
])
def test_bad_values_rejected(changes):
    with pytest.raises(SettingsError):
        apply_changes(Config(), changes)


def test_bad_value_not_saved():
    assert mcp_server.update_settings(toyhouse_mode="robot").startswith("Не сохранила")
    assert not config.settings_path().exists()


def test_add_and_remove_da_folders():
    out = mcp_server.add_da_folders(["https://www.deviantart.com/da-adopts/gallery/26780440/auctions",
                                     "https://www.deviantart.com/da-adopts/gallery/26780440/auctions",
                                     "не ссылка"])
    assert "da-adopts: auctions" in out and "не ссылка" in out
    assert len(load_config().deviantart.groups) == 1
    assert mcp_server.remove_da_folders(["da-adopts: auctions"]) == "Убрано: 1."
    assert load_config().deviantart.groups == []


def test_save_is_atomic_and_readable(tmp_path):
    cfg = Config()
    cfg.toyhouse.lf_keywords = ["кот"]
    path = save_config(cfg, tmp_path / "s.json")
    assert load_config(path).toyhouse.lf_keywords == ["кот"]
    assert not (tmp_path / "s.json.tmp").exists()


def test_chrome_mode_skips_toyhouse_only_in_background(tmp_path):
    with db.session(tmp_path / "x.sqlite3") as conn:
        cfg = Config(toyhouse=Toyhouse(mode="chrome"))
        results = monitor.run_check(conn, cfg, sources=("toyhouse",), force=True, now=NOW)
    assert results[0].skipped == monitor.CHROME_MODE_SKIP


def test_legacy_claude_mode_becomes_chrome(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"toyhouse": {"mode": "claude"}}', encoding="utf-8")
    assert load_config(path).toyhouse.mode == "chrome"


def test_my_username_normalised():
    cfg = Config()
    apply_changes(cfg, {"my_da_username": "https://www.deviantart.com/Moth-Art/gallery"})
    assert cfg.deviantart.my_username == "Moth-Art"
    with pytest.raises(SettingsError):
        apply_changes(cfg, {"my_da_username": "плохой ник!"})


def test_market_only_folder():
    mcp_server.add_da_folders(["https://www.deviantart.com/da-adopts/gallery/75027420/points-adopts"], for_market_only=True)
    group = load_config().deviantart.groups[0]
    assert group.notify is False and "только для рынка" in mcp_server.get_settings()


def test_bot_login_spawns_window(monkeypatch):
    started = []
    monkeypatch.setattr(mcp_server.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    assert "Пароль" in mcp_server.bot_login()
    assert started and started[0][-1] == "login"


def test_posting_settings_via_tool():
    out = mcp_server.update_settings(da_client_id="12345", th_username="@mothy", th_auto_submit=False,
                                     toyhouse_poll_minutes=90, default_noai=True)
    assert "Сохранено" in out
    cfg = load_config()
    assert cfg.posting.da_client_id == "12345" and cfg.posting.th_username == "mothy"
    assert cfg.posting.th_auto_submit is False and cfg.toyhouse.poll_minutes == 90
    assert "Не сохранила" in mcp_server.update_settings(da_client_id="abc")
    assert "Не сохранила" in mcp_server.update_settings(toyhouse_poll_minutes=5)


def test_da_connect_without_client_id_explains_steps(monkeypatch):
    from mcp_devian import posting

    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    out = mcp_server.da_connect()
    assert "developers/register" in out and "http://localhost:8765/callback" in out and "client_secret НЕ нужен" in out


def test_settings_carry_posting_rule_and_drafts_are_labelled():
    assert "только по её явному «го»" in mcp_server.get_settings()
    assert mcp_server.list_drafts() == "Открытых черновиков нет."


def test_max_pages_per_board_parse_and_bounds():
    cfg = Config()
    assert cfg.toyhouse.max_pages_per_board == 6
    apply_changes(cfg, {"max_pages_per_board": "8"})
    assert cfg.toyhouse.max_pages_per_board == 8
    for bad in (0, 1, 21, "много", 2.5, True):
        with pytest.raises(SettingsError):
            apply_changes(Config(), {"max_pages_per_board": bad})  # 1 < pages_per_board (2)
    cfg = Config()
    apply_changes(cfg, {"pages_per_board": 9})
    assert cfg.toyhouse.max_pages_per_board == 9


def test_board_as_id_slug(tmp_path):
    cfg = Config()
    apply_changes(cfg, {"toyhouse_boards": [13, "5866.Design-Marketplace", "777", "https://toyhou.se/~forums/12.worlds"]})
    assert cfg.toyhouse.boards == [13, "5866.design-marketplace", 777, "12.worlds"]
    with pytest.raises(SettingsError):
        apply_changes(Config(), {"toyhouse_boards": ["adoption center"]})
    path = tmp_path / "s.json"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.toyhouse.boards == cfg.toyhouse.boards and not loaded.warnings


def test_same_board_twice_is_read_once(tmp_path):
    cfg = Config()
    apply_changes(cfg, {"toyhouse_boards": [13, "13.adoption-center", 5866, "5866"]})
    assert cfg.toyhouse.boards == ["13.adoption-center", 5866]
    path = tmp_path / "s.json"
    path.write_text('{"toyhouse": {"boards": ["13.adoption-center", 13]}}')
    assert load_config(path).toyhouse.boards == ["13.adoption-center"]


def test_broken_paging_values_fall_back(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"toyhouse": {"boards": [13, "nope"], "pages_per_board": 3, "max_pages_per_board": "x"}}')
    cfg = load_config(path)
    assert cfg.toyhouse.boards == [13] and cfg.toyhouse.pages_per_board == 3
    assert cfg.toyhouse.max_pages_per_board == 6 and len(cfg.warnings) == 2
