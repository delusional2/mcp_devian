"""Экономия токенов: Claude Desktop шлёт имена, описания и схемы всех инструментов с каждым её сообщением.

Здесь бюджет на это, правило «старое действие → новый инструмент» и обрезка длинных ответов.
"""

import asyncio
import json
from pathlib import Path

import pytest

from mcp_devian import mcp_server, posting

MAX_TOOLS = 40
MAX_SCHEMA_CHARS = 16000

# Неизменные инструменты (имя осталось) и куда переехали остальные 78 из прежних 86.
UNCHANGED = ("get_settings", "update_settings", "set_background_checks", "dismiss", "prepare_deviantart_publish",
             "schedule_draft", "prepare_toyhouse_reply", "submit_draft")
OLD_TO_NEW = [
    ("check_now", "check", {"action": "run", "source": "toyhouse"}),
    ("check_status", "check", {"action": "status"}),
    ("bot_login", "accounts", {"action": "bot_login"}),
    ("da_connect", "accounts", {"action": "da_connect"}),
    ("da_disconnect", "accounts", {"action": "da_disconnect"}),
    ("da_status", "accounts", {"action": "da_status"}),
    ("list_new_adopts", "findings", {"action": "adopts"}),
    ("list_lf_threads", "findings", {"action": "lf"}),
    ("list_sale_threads", "findings", {"action": "sale"}),
    ("pending_digest", "findings", {"action": "digest"}),
    ("sources_status", "findings", {"action": "sources"}),
    ("suggest_replies", "findings", {"action": "replies", "show_blocked": True}),
    ("read_thread", "th_thread", {"thread": "th:1"}),
    ("inspect_reply_form", "th_thread", {"thread": "th:1", "action": "reply_form"}),
    ("market_forecast", "market", {"action": "forecast", "keywords": ["moth"]}),
    ("market_search", "market", {"action": "search", "query": "moth"}),
    ("group_scorecard", "market", {"action": "groups"}),
    ("market_buyers", "market", {"action": "buyers"}),
    ("what_sells", "market", {"action": "what_sells"}),
    ("listing_norms", "market", {"action": "norms"}),
    ("my_works_overview", "market", {"action": "my_works"}),
    ("th_market", "market", {"action": "th", "by": "styles"}),
    ("th_demand_for_my_characters", "market", {"action": "th_mine"}),
    ("niche_forecast", "forecast", {"action": "niches"}),
    ("price_plan", "forecast", {"action": "price", "species": "moth"}),
    ("forecast_accuracy", "forecast", {"action": "accuracy"}),
    ("best_time_to_post", "forecast", {"action": "best_time"}),
    ("data_health", "forecast", {"action": "health"}),
    ("explain_lot", "forecast", {"action": "explain", "title": "Moth adopt"}),
    ("sales_advice", "advice", {"action": "tips"}),
    ("weekly_sales_report", "advice", {"action": "week"}),
    ("plan_new_adopt", "advice", {"action": "plan", "species": "moth"}),
    ("review_my_post", "advice", {"action": "review", "url": "https://www.deviantart.com/m/art/M-1"}),
    ("unsold_lots", "advice", {"action": "unsold"}),
    ("leave_feedback", "feedback", {"action": "save", "text": "неудобно"}),
    ("feedback_report", "feedback", {"action": "report"}),
    ("feedback_sent", "feedback", {"action": "sent"}),
    ("my_auctions", "da_auctions", {"action": "mine"}),
    ("list_my_open_auctions", "da_auctions", {"action": "open"}),
    ("auction_status", "da_auctions", {"action": "status", "url": "https://www.deviantart.com/m/art/M-1"}),
    ("auction_deadlines", "da_auctions", {"action": "deadlines"}),
    ("da_group_folders", "da_groups", {"action": "folders", "group": "adopts"}),
    ("add_da_folders", "da_groups", {"action": "track", "urls": ["u"]}),
    ("remove_da_folders", "da_groups", {"action": "untrack", "urls": ["u"]}),
    ("add_group_to_preset", "da_groups", {"action": "preset_add", "preset": "p", "group": "g", "folder_name": "f"}),
    ("remove_group_from_preset", "da_groups", {"action": "preset_remove", "preset": "p", "group": "g"}),
    ("my_gallery_folders", "da_groups", {"action": "my_folders"}),
    ("recent_images", "images", {"action": "recent"}),
    ("prepare_preview", "images", {"action": "preview", "file_path": "/tmp/a.png"}),
    ("prepare_deviantart_batch", "prepare_deviantart_publish", {"works": [{"file_path": "a.png", "title": "A"}]}),
    ("prepare_close_auctions", "prepare_gallery", {"action": "close"}),
    ("prepare_gallery_folders", "prepare_gallery", {"action": "folders", "create": ["Sold"]}),
    ("prepare_rename_works", "prepare_gallery", {"action": "rename", "renames": {"da:1": "Moth — SOLD"}}),
    ("draft_status", "drafts", {"action": "status", "draft_id": "d1"}),
    ("cancel_draft", "drafts", {"action": "cancel", "draft_id": "d1"}),
    ("list_drafts", "drafts", {"action": "list"}),
    ("set_reply_template", "set_template", {"kind": "reply", "text": "hi {name}"}),
    ("set_selling_template", "set_template", {"kind": "pinglist", "text": "{mentions}"}),
    ("add_character", "characters", {"action": "add", "name": "Lumi", "url": "https://toyhou.se/1.lumi"}),
    ("remove_character", "characters", {"action": "remove", "name": "Lumi"}),
    ("th_auction_status", "th_auctions", {"action": "status", "url": "u"}),
    ("th_watch_auction", "th_auctions", {"action": "watch", "url": "u"}),
    ("my_th_auctions", "th_auctions", {"action": "mine"}),
    ("th_character_inspect", "th_character", {"action": "inspect", "form": "edit", "url": "u"}),
    ("th_character_folders", "th_character", {"action": "folders"}),
    ("th_character_prepare", "th_character", {"action": "new", "name": "Lumi", "images": ["a.png"]}),
    ("th_character_transfer", "th_character", {"action": "transfer", "url": "u", "buyer": "bob"}),
    ("th_character_listing", "th_character", {"action": "listing", "url": "u", "status": "for sale"}),
    ("th_character_move", "th_character", {"action": "move", "urls": ["u"], "folder": "Sold"}),
    ("record_sale", "sales", {"action": "record", "title": "Moth", "amount": 5}),
    ("set_sale_status", "sales", {"action": "status", "sale_id": "s1", "status": "paid"}),
    ("my_sales", "sales", {"action": "list"}),
    ("income", "sales", {"action": "income"}),
    ("unpaid_sales", "sales", {"action": "unpaid"}),
    ("prepare_payment_reminder", "sales", {"action": "remind", "sale_id": "s1"}),
    ("export_sales", "sales", {"action": "export"}),
    ("manage_pinglist", "pinglist", {"action": "add", "users": ["bob"]}),
    ("prepare_ping", "pinglist", {"action": "ping", "url": "u"}),
]


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}


def _call(name: str, args: dict) -> str:
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    return "".join(getattr(c, "text", "") for c in result.content)


def _schema_chars(tools: dict) -> int:
    return sum(len(json.dumps({"name": t.name, "description": t.description, "inputSchema": t.input_schema},
                              ensure_ascii=False)) for t in tools.values())


def test_tool_budget():
    tools = _tools()
    assert len(tools) <= MAX_TOOLS, len(tools)
    assert _schema_chars(tools) <= MAX_SCHEMA_CHARS, _schema_chars(tools)


def test_every_old_tool_has_a_home():
    old = [o for o, _, _ in OLD_TO_NEW]
    assert len(set(old)) + len(UNCHANGED) == 86
    tools = _tools()
    assert set(UNCHANGED) <= set(tools) and {n for _, n, _ in OLD_TO_NEW} <= set(tools)
    manifest = json.loads((Path(__file__).parent.parent / "manifest.json").read_text())
    assert [t["name"] for t in manifest["tools"]] == list(tools)


@pytest.mark.parametrize(("old", "new", "args"), OLD_TO_NEW, ids=[f"{o}->{n}" for o, n, _ in OLD_TO_NEW])
def test_old_action_routes_to_its_code(old, new, args, monkeypatch):
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    calls = []
    monkeypatch.setattr(mcp_server, old, lambda *a, **k: calls.append((a, k)) or f"ok:{old}")
    assert _call(new, args) == f"ok:{old}"
    assert len(calls) == 1


def test_declined_feedback_and_missing_params(monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_server, "leave_feedback", lambda *a, **k: seen.append(k) or "ok")
    assert _call("feedback", {"action": "declined"}) == "ok" and seen == [{"declined": True}]
    assert _call("sales", {"action": "record", "title": "Moth"}) == "Нужно: amount."
    assert _call("drafts", {"action": "cancel"}) == "Нужно: draft_id."
    assert _call("th_character", {"action": "transfer", "url": "u"}) == "Нужно: buyer."


def test_shown_schema_is_slim_but_keeps_types():
    tools = _tools()
    text = json.dumps({n: t.input_schema for n, t in tools.items()}, ensure_ascii=False)
    assert '"type": "null"' not in text and "Arguments" not in text
    assert all(t.output_schema is None for t in tools.values())
    props = tools["sales"].input_schema["properties"]
    assert props["title"] == {"type": "string"}  # параметр title не путается с автоматическим title
    assert props["action"]["enum"][0] == "record" and props["amount"] == {"type": "number"}
    assert tools["prepare_gallery"].input_schema["properties"]["record_sales"]["default"] is True
    assert tools["submit_draft"].input_schema["required"] == ["draft_id"]


def test_slim_schema_unit():
    schema = {"title": "xArguments", "type": "object", "properties": {
        "title": {"title": "Title", "type": "string"},
        "tags": {"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": None},
        "n": {"default": 5, "type": "integer"}, "flag": {"default": True, "type": "boolean"}}}
    assert mcp_server.slim_schema(schema) == {"type": "object", "properties": {
        "title": {"type": "string"}, "tags": {"items": {"type": "string"}, "type": "array"},
        "n": {"type": "integer"}, "flag": {"default": True, "type": "boolean"}}}


def test_go_rule_stays_on_publishing_tools():
    tools = _tools()
    for name in ("submit_draft", "schedule_draft"):
        assert tools[name].annotations.destructive_hint is True, name
        assert "ТОЛЬКО после её явного «го»" in tools[name].description, name
    published = [n for n, t in tools.items() if t.annotations.destructive_hint]
    assert sorted(published) == ["schedule_draft", "submit_draft"]


def test_descriptions_are_short():
    for name, t in _tools().items():
        assert len(t.description) <= 400, name
        assert t.description.count("\n") <= 3, name


def test_instructions_are_short_and_rules_live_in_get_settings(server_settings):
    assert len(mcp_server.INSTRUCTIONS) < 700 and "«го»" in mcp_server.INSTRUCTIONS
    text = mcp_server.get_settings()
    for rule in (mcp_server.TONE, mcp_server.POSTING_RULE, mcp_server.TEXT_RULE, mcp_server.NUMBERS_RULE,
                 mcp_server.GALLERY_CAN, mcp_server.HOW_TO):
        assert rule in text
    # update_settings показывает настройки, но длинные правила второй раз не повторяет.
    assert mcp_server.NUMBERS_RULE not in mcp_server.update_settings()


def test_clip_cuts_long_lists_but_never_drafts():
    long = "\n".join(f"- лот {i}: {'x' * 60}" for i in range(200))
    out = mcp_server.clip(long)
    assert len(out) <= mcp_server.MAX_OUTPUT + 120 and "попроси подробнее" in out
    shown = out.count("\n- лот")
    assert f"ещё {200 - shown - 1} строк" in out
    draft = f"Черновик abc:\n{long}\n\n{mcp_server.GO_RULE}"
    assert mcp_server.clip(draft) == draft
    assert mcp_server.clip("коротко") == "коротко"


def test_clip_keeps_caveats_paragraph():
    """Длинный список режется, а последний абзац — оговорки и «на чём основано» — остаётся."""
    rows = "\n".join(f"- ниша {i}: {'x' * 80}" for i in range(80))
    text = f"Ниши на рынке DA\n{rows}\n\nОговорка: мало данных.\nОпора: n=40, данные на 01.11."
    out = mcp_server.clip(text)
    assert len(out) <= mcp_server.MAX_OUTPUT + 120 and "попроси подробнее" in out
    assert out.endswith("\n\nОговорка: мало данных.\nОпора: n=40, данные на 01.11.")
    assert out.startswith("Ниши на рынке DA\n- ниша 0")


def test_rules_and_feedback_are_never_clipped(server_settings, monkeypatch):
    from mcp_devian import config, feedback

    cfg = config.load_config()
    cfg.configured = False  # с вопросами настройки ответ длиннее порога
    config.save_config(cfg, force=True)
    text = _call("get_settings", {})
    assert len(text) > mcp_server.MAX_OUTPUT and "попроси подробнее" not in text
    assert mcp_server.HOW_TO in text and mcp_server.SETUP_QUESTIONS in text
    monkeypatch.setattr(feedback, "report", lambda context="": "\n".join(["её отзыв " * 10] * 100))
    assert "попроси подробнее" not in _call("feedback", {"action": "report"})


def test_tool_output_is_clipped(monkeypatch):
    monkeypatch.setattr(mcp_server, "my_sales", lambda status, days: "\n".join(["строка " * 20] * 100))
    out = _call("sales", {"action": "list"})
    assert len(out) < mcp_server.MAX_OUTPUT + 120 and "попроси подробнее" in out


def test_send_results_are_never_clipped(monkeypatch):
    """Итог отправки по 25 работам длиннее порога: строку «исход неизвестен» срезать нельзя."""
    long = "\n".join([f"{i}) работа {i}: готово " + "x" * 150 for i in range(1, 26)] + ["18) исход неизвестен"])
    monkeypatch.setattr(mcp_server, "draft_status", lambda draft_id: long)
    assert _call("drafts", {"action": "status", "draft_id": "abc"}) == long
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setattr(posting, "is_batch_id", lambda d: False)
    monkeypatch.setattr(mcp_server.drafts, "get", lambda conn, d: {"status": "sent", "result": long})
    assert long in _call("submit_draft", {"draft_id": "abc"})


def test_unknown_action_from_python_is_explained():
    assert mcp_server.drafts_tool("delete") == "action: list, status, cancel."


@pytest.fixture
def server_settings(tmp_path, monkeypatch):
    from mcp_devian import db
    from mcp_devian.config import Config, save_config

    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "srv.sqlite3"))
    save_config(Config(configured=True))
    from mcp_devian import updates

    monkeypatch.setattr(updates, "check_for_update", lambda: None)
    return tmp_path


def test_public_text_tools_say_her_words_only():
    """Desktop не передаёт instructions: правило «её слова» должно быть в описании самих инструментов."""
    tools = _tools()
    for name in ("prepare_deviantart_publish", "th_character"):
        assert "её слова, не сочиняй" in tools[name].description, name
    assert "th_character:new" in tools["characters"].description
    assert "platform deviantart|toyhouse|commishes|other" in tools["sales"].description
