"""Публикация: черновики, лимиты, отправка только по черновику и ровно один раз."""

import pytest

from mcp_devian import db, drafts, mcp_server, posting
from mcp_devian.config import Config, SettingsError, Toyhouse, load_config, save_config
from mcp_devian.db import Item

NOW = 1_800_000_000.0
TEMPLATE = "Привет! Могу предложить {name} — {url}, цена {price}. {note}"
CHAR = {"name": "Луна", "url": "https://toyhou.se/123.luna", "price": "$30", "tags": ["dragon", "moth"]}


@pytest.fixture(autouse=True)
def sending_on(monkeypatch):
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)


@pytest.fixture
def cfg():
    c = Config(toyhouse=Toyhouse(mode="chrome"))
    c.posting.reply_templates = {"основной": TEMPLATE}
    c.posting.characters = [dict(CHAR)]
    c.posting.th_username = "mothy"
    c.posting.th_auto_submit = True
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p.sqlite3")
    for i in range(1, 4):
        db.upsert_item(c, Item(id=f"th:{i}", source="toyhouse", title=f"LF: dragon OCs #{i}", url=f"https://toyhou.se/~forums/13.x/{i}.t",
                               wants_characters=True, matched=True, match_kind="lf", activity_ts=NOW), now=NOW)
    c.commit()
    yield c
    c.close()


def fake_reply(state="posted", calls=None):
    def run(thread_url, html, submit, username, marker, **kw):
        if calls is not None:
            calls.append((thread_url, html, submit, username, marker))
        return {"state": state, "permalink": thread_url + "#post-1"}
    return run


def test_template_kept_verbatim_and_slots_checked(cfg):
    posting.set_template(cfg, "второй", "Её текст слово в слово: {name} {url}")
    assert cfg.posting.reply_templates["второй"] == "Её текст слово в слово: {name} {url}"
    with pytest.raises(SettingsError):
        posting.set_template(cfg, "плохой", "Привет {nickname}")


def test_character_url_validated(cfg):
    with pytest.raises(SettingsError):
        posting.add_character(cfg, "X", "https://example.com/1")
    posting.add_character(cfg, "Бо", "https://toyhou.se/555.bo", "$10", ["cat"])
    assert posting.find_character(cfg, "бо")["price"] == "$10"


def test_prepare_reply_shows_everything(conn, cfg):
    draft_id, preview = posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", note="Смотрела твой тред!", now=NOW)
    assert draft_id and "Луна" in preview and "https://toyhou.se/123.luna" in preview
    assert "Смотрела твой тред!" in preview and "чужой текст" in preview
    assert drafts.get(conn, draft_id)["status"] == "pending"


def test_reply_needs_chrome_mode(conn, cfg):
    cfg.toyhouse.mode = "bot"
    draft_id, text = posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW)
    assert draft_id is None and "chrome" in text


def test_send_once_and_log(conn, cfg, monkeypatch):
    from mcp_devian import her_chrome

    calls = []
    monkeypatch.setattr(her_chrome, "run_reply", fake_reply("posted", calls))
    draft_id, _ = posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW)
    assert "опубликован" in posting.send(conn, cfg, draft_id, now=NOW + 10)
    # Второе «го» на тот же черновик ничего не отправляет.
    assert "уже отправлен" in posting.send(conn, cfg, draft_id, now=NOW + 20)
    assert len(calls) == 1 and calls[0][2] is True and calls[0][3] == "mothy"
    assert drafts.count_sent(conn, "th_reply", NOW - 1) == 1


def test_limits_block_second_reply_to_same_thread_and_fast_replies(conn, cfg, monkeypatch):
    from mcp_devian import her_chrome

    monkeypatch.setattr(her_chrome, "run_reply", fake_reply())
    d1, _ = posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW)
    posting.send(conn, cfg, d1, now=NOW)
    assert posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW + 3600)[0] is None  # тот же тред
    blocked = posting.prepare_toyhouse_reply(conn, cfg, "th:2", "Луна", now=NOW + 30)
    assert blocked[0] is None and "слишком быстро" in blocked[1]
    d2, _ = posting.prepare_toyhouse_reply(conn, cfg, "th:2", "Луна", now=NOW + 600)
    posting.send(conn, cfg, d2, now=NOW + 600)
    third = posting.prepare_toyhouse_reply(conn, cfg, "th:3", "Луна", now=NOW + 1200)
    assert third[0] is None and "этого перса" in third[1]


def test_limits_rechecked_at_send_time(conn, cfg, monkeypatch):
    from mcp_devian import her_chrome

    monkeypatch.setattr(her_chrome, "run_reply", fake_reply())
    d1, _ = posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW)
    d2, _ = posting.prepare_toyhouse_reply(conn, cfg, "th:2", "Луна", now=NOW)  # оба подготовлены заранее
    posting.send(conn, cfg, d1, now=NOW + 5)
    assert "не отправила" in posting.send(conn, cfg, d2, now=NOW + 10)
    assert drafts.get(conn, d2)["status"] == "failed"


def test_filled_mode_counts_but_says_click(conn, cfg, monkeypatch):
    from mcp_devian import her_chrome

    cfg.posting.th_auto_submit = False
    calls = []
    monkeypatch.setattr(her_chrome, "run_reply", fake_reply("filled", calls))
    d, preview = posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW)
    assert "«Post» она нажмёт сама" in preview
    assert "осталось нажать" in posting.send(conn, cfg, d, now=NOW)
    assert calls[0][2] is False and drafts.count_sent(conn, "th_reply", NOW - 1) == 1


def test_stale_draft_rejected(conn, cfg, monkeypatch):
    from mcp_devian import her_chrome

    monkeypatch.setattr(her_chrome, "run_reply", fake_reply())
    d, _ = posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW)
    assert "устарел" in posting.send(conn, cfg, d, now=NOW + 2 * 86400)


def test_crash_during_auto_send_is_unknown_and_counted(conn, cfg, monkeypatch):
    from mcp_devian import her_chrome

    def boom(*a, **k):
        raise RuntimeError("chrome упал")

    monkeypatch.setattr(her_chrome, "run_reply", boom)
    d, _ = posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW)
    # Сбой после возможного клика: исход неизвестен, повторный отклик в тот же тред не пройдёт.
    assert "неизвестно" in posting.send(conn, cfg, d, now=NOW)
    assert drafts.get(conn, d, now=NOW)["status"] == "unknown"
    assert posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW + 3600)[0] is None


class FakeDA:
    def __init__(self):
        self.calls = []

    def is_connected(self):
        return True

    def stash_submit(self, file, title, description, tags):
        self.calls.append(("submit", file, title))
        return 42

    def stash_publish(self, itemid, **kw):
        self.calls.append(("publish", itemid, kw))
        return {"url": "https://www.deviantart.com/me/art/x-1", "deviationid": "uuid-1"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    return tmp_path


def test_deviantart_publish_flow(conn, cfg, home, monkeypatch):
    img = home / "Downloads" / "moth.png"
    img.write_bytes(b"\x89PNG fake")
    cfg.posting.group_presets = {"аукционы": [
        {"group": "da-adopts", "group_id": "g-1", "folder_id": "f-1", "folder_name": "auctions"},
        {"group": "other", "group_id": "g-2", "folder_id": "f-2", "folder_name": "open"}]}
    fake = FakeDA()
    monkeypatch.setattr(posting, "da_client", lambda c: fake)
    d, preview = posting.prepare_da_publish(conn, cfg, str(img), "Moth #1", "SB $10", ["moth"], "аукционы", now=NOW)
    assert d and "da-adopts → auctions" in preview and "запрет на обучение ИИ: да" in preview
    assert "опубликовано" in posting.send(conn, cfg, d, now=NOW)
    publish = fake.calls[1][2]
    assert publish["groups"] == [("g-1", "f-1"), ("g-2", "f-2")] and publish["noai"] is True
    assert drafts.count_sent(conn, "da_group", NOW - 1, target="da-adopts") == 1


def test_deviantart_group_daily_limit(conn, cfg, home):
    img = home / "Downloads" / "m.png"
    img.write_bytes(b"x")
    cfg.posting.group_presets = {"a": [{"group": "da-adopts", "group_id": "g", "folder_id": "f", "folder_name": "x"}]}
    for _ in range(cfg.posting.da_submissions_per_group_per_day):
        drafts.log_sent(conn, "da_group", "da-adopts", "dev", True, now=NOW)
    _, preview = posting.prepare_da_publish(conn, cfg, str(img), "T", preset="a", now=NOW + 60)
    assert "пропущу da-adopts" in preview and "никаких" in preview


def test_publish_missing_file(conn, cfg):
    assert posting.prepare_da_publish(conn, cfg, "/nope/x.png", "T")[0] is None


# --- через MCP-инструменты ---

@pytest.fixture
def server(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: False)
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "s.sqlite3"))
    cfg.configured = True
    save_config(cfg)
    with db.session() as c:
        db.upsert_item(c, Item(id="th:7", source="toyhouse", title="LF dragon OCs", url="https://toyhou.se/~forums/13.x/7.t",
                               wants_characters=True, matched=True, match_kind="lf", activity_ts=NOW), now=NOW)


def test_tools_end_to_end(server, monkeypatch):
    out = mcp_server.prepare_toyhouse_reply("th:7", "Луна")
    assert out.startswith("Черновик ") and "«го»" in out
    draft_id = out.split()[1].rstrip(":")
    assert draft_id in mcp_server.list_drafts()
    spawned = []
    monkeypatch.setattr(posting, "start_sending", lambda d: spawned.append(d))
    monkeypatch.setattr(posting, "wait_for", lambda conn, d, t=45: {"status": "sent", "result": "отклик опубликован: x"})
    assert "Отправлен" in mcp_server.submit_draft(draft_id) and spawned == [draft_id]
    assert "Такого черновика нет" in mcp_server.submit_draft("nope")


def test_set_template_tool_keeps_text(server):
    text = "Хей! {name} ищет дом: {url} ({price})"
    assert "сохранён" in mcp_server.set_reply_template("короткий", text)
    assert load_config().posting.reply_templates["короткий"] == text


def test_publish_tool_is_marked_destructive():
    import asyncio

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert tools["submit_draft"].annotations.destructive_hint is True
    assert tools["prepare_toyhouse_reply"].annotations.destructive_hint is False


def test_default_is_fill_only():
    assert Config().posting.th_auto_submit is False


def test_sending_switched_off_in_this_release(tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "SENDING_ENABLED", False)
    for call in (lambda: mcp_server.submit_draft("x"), lambda: mcp_server.prepare_toyhouse_reply("th:1", "Луна"),
                 lambda: mcp_server.prepare_deviantart_publish("/x.png", "T"), mcp_server.da_connect):
        assert call() == posting.NOT_YET


# --- 0.5: правила групп и честная заметка о длине названия ---

@pytest.mark.parametrize("text, want", [("1/week", {"count": 1, "days": 7}), ("2/day", {"count": 2, "days": 1}),
                                        ("1/month", {"count": 1, "days": 30}), ("3/7d", {"count": 3, "days": 7}),
                                        (" 1 / 2 weeks ", {"count": 1, "days": 14}), ("1/неделю", {"count": 1, "days": 7}),
                                        ("", None)])
def test_group_limit_parsing(text, want):
    assert posting.parse_group_limit(text) == want


@pytest.mark.parametrize("text", ["weekly", "1/fortnight", "0/day", "5", "1/400d", "-1/week"])
def test_group_limit_bad_strings_rejected(text):
    with pytest.raises(SettingsError, match="1/week"):
        posting.parse_group_limit(text)


class FakeGroups:
    def resolve_group_folder(self, group, folder):
        return {"group": group, "group_id": "g", "folder_id": "f", "folder_name": folder}


def test_preset_stores_limit_and_keeps_it_on_readd(cfg, monkeypatch):
    monkeypatch.setattr(posting, "da_client", lambda c: FakeGroups())
    entry = posting.add_group_to_preset(cfg, "a", "Adopt-Club", "Open", limit="1/week")
    assert entry["limit"] == {"count": 1, "days": 7}
    posting.add_group_to_preset(cfg, "a", "adopt-club", "Open")
    assert cfg.posting.group_presets["a"][0]["limit"] == {"count": 1, "days": 7}
    with pytest.raises(SettingsError):
        posting.add_group_to_preset(cfg, "a", "x", "y", limit="often")


def test_group_window_blocks_with_manual_logs(conn, cfg):
    from mcp_devian import limits

    cfg.posting.group_presets = {"a": [{"group": "Adopt-Club", "group_id": "g", "folder_id": "f", "folder_name": "x",
                                        "limit": {"count": 2, "days": 7}}],
                                 "b": [{"group": "adopt-club", "group_id": "g", "folder_id": "f", "folder_name": "y",
                                        "limit": {"count": 3, "days": 7}}]}
    assert limits.group_limit(cfg.posting, "ADOPT-CLUB") == (2, 7)  # самое строгое правило
    drafts.log_sent(conn, "da_group", "adopt-club", "d1", True, now=NOW - 5 * 86400)
    assert limits.deviantart_group_block(conn, cfg.posting, "Adopt-Club", now=NOW) is None
    drafts.log_sent(conn, "da_group_manual", "adopt-club", "da:5", True, now=NOW - 3 * 86400)
    why = limits.deviantart_group_block(conn, cfg.posting, "Adopt-Club", now=NOW)
    assert why and "2 за 7 дн" in why and "уже отправлено 2" in why
    assert limits.deviantart_group_block(conn, cfg.posting, "Adopt-Club", now=NOW + 3 * 86400) is None
    assert limits.deviantart_group_block(conn, cfg.posting, "other", now=NOW) is None


def test_group_window_in_publish_preview(conn, cfg, home):
    img = home / "Downloads" / "m.png"
    img.write_bytes(b"x")
    cfg.posting.group_presets = {"a": [{"group": "club", "group_id": "g", "folder_id": "f", "folder_name": "x",
                                        "limit": {"count": 1, "days": 7}}]}
    drafts.log_sent(conn, "da_group_manual", "club", "da:5", True, now=NOW - 2 * 86400)
    _, preview = posting.prepare_da_publish(conn, cfg, str(img), "T", preset="a", now=NOW)
    assert "пропущу club: правило группы club: 1 за 7 дн" in preview


def test_publish_preview_checks_each_group_rule(conn, cfg, home):
    img = home / "Downloads" / "m.png"
    img.write_bytes(b"x")
    cfg.posting.group_presets = {"a": [{"group": "club", "group_id": "g", "folder_id": "f", "folder_name": "x",
                                        "limit": {"count": 3, "days": 30}}],
                                 "b": [{"group": "club", "group_id": "g", "folder_id": "f", "folder_name": "x",
                                        "limit": {"count": 1, "days": 2}}]}
    drafts.log_sent(conn, "da_group_manual", "club", "da:5", True, now=NOW - 86400 - 3600)
    _, preview = posting.prepare_da_publish(conn, cfg, str(img), "T", preset="a", now=NOW)
    assert "пропущу club: правило группы club: 1 за 2 дн" in preview


def test_title_51_chars_has_honest_note(conn, cfg, home):
    img = home / "Downloads" / "m.png"
    img.write_bytes(b"x")
    d, text = posting.prepare_da_publish(conn, cfg, str(img), "x" * 51, now=NOW)
    assert d is None and "51 символов" in text
    assert "в описании API предел не назван — оставляю 50, чтобы не рисковать" in text
    assert posting.prepare_da_publish(conn, cfg, str(img), "x" * 50, now=NOW)[0]


def test_every_group_rule_is_enforced(conn, cfg):
    """Регрессия: из правил 1/день и 3/30 дней проверялось одно (3/30) — второй раз в тот же день проходил."""
    from mcp_devian import limits

    cfg.posting.group_presets = {"a": [{"group": "Adopt-Club", "group_id": "g", "folder_id": "f", "folder_name": "x",
                                        "limit": {"count": 1, "days": 1}}],
                                 "b": [{"group": "adopt-club", "group_id": "g", "folder_id": "f", "folder_name": "y",
                                        "limit": {"count": 3, "days": 30}}]}
    assert limits.group_limits(cfg.posting, "ADOPT-CLUB") == [(1, 1), (3, 30)]
    drafts.log_sent(conn, "da_group_manual", "adopt-club", "da:5", True, now=NOW - 3600)
    why = limits.deviantart_group_block(conn, cfg.posting, "Adopt-Club", now=NOW)
    assert why and "1 за 1 дн" in why
    drafts.log_sent(conn, "da_group", "adopt-club", "d2", True, now=NOW - 10 * 86400)
    drafts.log_sent(conn, "da_group", "adopt-club", "d3", True, now=NOW - 5 * 86400)
    why = limits.deviantart_group_block(conn, cfg.posting, "Adopt-Club", now=NOW + 2 * 86400)
    assert why and "3 за 30 дн" in why
    assert limits.limits_text(limits.group_limits(cfg.posting, "adopt-club")) == "1 за 1 дн и 3 за 30 дн"
