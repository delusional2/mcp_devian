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


# --- 0.5: оплата, X, тёплые покупатели, история рынка ---

def test_new_defaults_are_safe_and_roundtrip_without_warnings(tmp_path):
    cfg = Config()
    p, da = cfg.posting, cfg.deviantart
    assert (p.boosty_url, p.payment_methods, p.payment_window_hours, p.payment_plans) == ("", [], 48, False)
    assert p.x_hashtags == ["adoptables", "adoptsopen"] and p.x_rt_accounts == []
    assert (p.outreach_per_day, p.outreach_cooldown_days, p.outreach_window_days) == (5, 14, 180)
    assert p.pinglist_early_access_hours == 0 and p.da_extra_scopes == []
    assert (da.backfill_days, da.backfill_groups, da.backfill_api_per_run) == (75, 6, 40)
    assert da.palette_downloads_per_hour == 30 and da.similar_watch and da.similar_artists == []
    assert cfg.toyhouse.background_lf is False
    path = tmp_path / "s.json"
    save_config(cfg, path)
    assert not load_config(path).warnings


def test_hand_edited_file_cannot_raise_caps(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"posting": {"outreach_per_day": 9, "outreach_cooldown_days": 3, "outreach_window_days": 900,'
                    ' "pinglist_early_access_hours": 100, "payment_window_hours": 2,'
                    ' "x_hashtags": ["a1", "b2", "c3", "d4", "e5", "f6"], "da_extra_scopes": ["message", "user.watch"],'
                    ' "payment_methods": ["boosty", "paypal"], "boosty_url": "https://evil.example/x"},'
                    ' "deviantart": {"backfill_days": 200, "backfill_groups": 0, "backfill_api_per_run": "x",'
                    ' "palette_downloads_per_hour": 500, "similar_watch": "yes"},'
                    ' "toyhouse": {"background_lf": "on"}}')
    cfg = load_config(path)
    p, da = cfg.posting, cfg.deviantart
    assert (p.outreach_per_day, p.outreach_cooldown_days, p.outreach_window_days) == (5, 14, 180)
    assert p.pinglist_early_access_hours == 48 and p.payment_window_hours == 12
    assert p.x_hashtags == ["a1", "b2", "c3", "d4"]
    assert p.da_extra_scopes == ["message"] and p.payment_methods == ["boosty"] and p.boosty_url == ""
    assert (da.backfill_days, da.backfill_groups, da.backfill_api_per_run) == (90, 1, 40)
    assert da.palette_downloads_per_hour == 60 and da.similar_watch is True
    assert cfg.toyhouse.background_lf is False
    assert any("outreach_per_day" in w for w in cfg.warnings)


@pytest.mark.parametrize("changes", [
    {"outreach_per_day": 9}, {"outreach_cooldown_days": 3}, {"outreach_window_days": 365},
    {"pinglist_early_access_hours": 72}, {"payment_window_hours": 6}, {"backfill_days": 200},
    {"backfill_groups": 11}, {"palette_downloads_per_hour": 61},
    {"x_hashtags": ["a1", "b2", "c3", "d4", "e5", "f6"]}, {"x_hashtags": ["two words"]},
    {"da_extra_scopes": ["user.manage", "user.watch"]}, {"payment_methods": ["paypal"]},
    {"boosty_url": "https://boosty.to/name/posts/123"}, {"boosty_url": "https://patreon.com/name"},
    {"x_rt_accounts": [{"handle": "bad handle!"}]}, {"x_rt_accounts": [{"handle": "rtacc", "tag_ok": "yes"}]},
    {"tos_url": "моё TOS"}, {"payment_plans": "может быть"}, {"similar_artists": ["bad nick!"]},
])
def test_new_bad_values_rejected_in_russian(changes):
    with pytest.raises(SettingsError) as e:
        apply_changes(Config(), changes)
    assert any("а" <= c <= "я" for c in str(e.value).lower())


def test_boosty_url_normalised_and_turns_on_boosty():
    cfg = Config()
    apply_changes(cfg, {"boosty_url": "boosty.to/moth_art/"})
    assert cfg.posting.boosty_url == "https://boosty.to/moth_art"
    assert cfg.posting.payment_methods == ["boosty"]
    apply_changes(cfg, {"boosty_url": "http://boosty.to/Moth.Art-2"})
    assert cfg.posting.boosty_url == "https://boosty.to/Moth.Art-2"
    apply_changes(cfg, {"boosty_url": ""})
    assert cfg.posting.boosty_url == ""


def test_x_settings_parsed():
    cfg = Config()
    apply_changes(cfg, {"x_handle": "https://x.com/@MothArt", "x_hashtags": ["#adoptables", "OCadopt", "adoptables"],
                        "x_rt_accounts": ["@rt_one", {"handle": "rt_two", "method": "tag us + #adoptables",
                                                      "tag_ok": True}]})
    assert cfg.posting.x_handle == "MothArt"
    assert cfg.posting.x_hashtags == ["adoptables", "OCadopt"]
    assert cfg.posting.x_rt_accounts == [{"handle": "rt_one", "method": "", "tag_ok": False},
                                         {"handle": "rt_two", "method": "tag us + #adoptables", "tag_ok": True}]


def test_new_ranges_accept_edges():
    cfg = Config()
    done = apply_changes(cfg, {"outreach_per_day": 0, "outreach_cooldown_days": 90, "payment_window_hours": "72",
                               "backfill_days": 60, "toyhouse_background_lf": True, "payment_plans": True,
                               "da_extra_scopes": ["user.manage", "message"], "pinglist_early_access_hours": 24})
    assert len(done) == 8
    assert cfg.posting.outreach_per_day == 0 and cfg.posting.payment_window_hours == 72
    assert cfg.deviantart.backfill_days == 60 and cfg.toyhouse.background_lf and cfg.posting.payment_plans


def test_group_preset_limits_validated(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"posting": {"group_presets": {"moths": ['
                    '{"group": "a", "folder_id": "1", "limit": {"count": 1, "days": 7}},'
                    '{"group": "b", "folder_id": "2", "limit": {"count": 0, "days": 7}},'
                    '{"group": "c", "folder_id": "3", "limit": "1/week"},'
                    '{"group": "d", "folder_id": "4"}]}}}')
    cfg = load_config(path)
    entries = cfg.posting.group_presets["moths"]
    assert entries[0]["limit"] == {"count": 1, "days": 7}
    assert [("limit" in e) for e in entries] == [True, False, False, False]
    assert sum("limit" in w for w in cfg.warnings) == 2
    assert config.valid_group_limit({"count": "2", "days": 1}) == {"count": 2, "days": 1}
    assert config.valid_group_limit({"count": 2}) is None


def test_every_apply_key_is_described():
    import inspect
    import re

    src = inspect.getsource(apply_changes)
    keys = set(re.findall(r'key == "(\w+)"', src))
    for group in re.findall(r"key in \(([^)]*)\)", src):
        keys |= set(re.findall(r'"(\w+)"', group))
    keys |= set(config.RANGES)
    assert keys <= set(config.SETTING_KEYS), keys - set(config.SETTING_KEYS)
    assert "outreach_per_day: 0–5" in config.describe_keys()


@pytest.mark.parametrize("raw", ["Infinity", "-Infinity", "NaN", "1e400"])
def test_non_finite_number_in_file_falls_back_with_warning(tmp_path, raw):
    path = tmp_path / "s.json"
    path.write_text('{"posting": {"outreach_per_day": ' + raw + ', "payment_window_hours": ' + raw + '},'
                    ' "deviantart": {"backfill_days": ' + raw + '}}')
    cfg = load_config(path)
    assert cfg.posting.outreach_per_day == 5 and cfg.posting.payment_window_hours == 48
    assert cfg.deviantart.backfill_days == 75
    assert any("outreach_per_day" in w for w in cfg.warnings)


def test_hand_edited_strings_split_and_drops_warned(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"posting": {"payment_methods": "boosty, points", "x_hashtags": "#adoptables #adoptsopen",'
                    ' "x_rt_accounts": [{"handle": "okacc"}, {"handle": "rtacc", "tag_ok": "yes"}],'
                    ' "da_extra_scopes": ["message", "user.watch"]}}')
    cfg = load_config(path)
    p = cfg.posting
    assert p.payment_methods == ["boosty", "points"] and p.x_hashtags == ["adoptables", "adoptsopen"]
    assert [a["handle"] for a in p.x_rt_accounts] == ["okacc"] and p.da_extra_scopes == ["message"]
    assert any("x_rt_accounts" in w and "rtacc" in w for w in cfg.warnings)
    assert any("da_extra_scopes" in w and "user.watch" in w for w in cfg.warnings)


def test_hand_edited_bad_hashtags_warned(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"posting": {"x_hashtags": ["ok_tag", "bad tag!", "a1", "b2", "c3", "d4"]}}')
    cfg = load_config(path)
    assert cfg.posting.x_hashtags == ["ok_tag", "a1", "b2", "c3"]
    warned = [w for w in cfg.warnings if "x_hashtags" in w]
    assert warned and "bad tag!" in warned[0] and "d4" in warned[0]
