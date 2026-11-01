"""Подключение 0.5 (деньги): часы и цель, YCH и коммишены, защита, превью-лист, перевод, отчёт за месяц —
в инструментах сервера (действиями на старых инструментах), в фоне, в настройках, в манифесте и README.
И правило лички: личные предложения — только покупателям, ставившим на её лоты и пинглисту.

Всё на подделках: ни DeviantArt, ни Chrome, ни сети, ни настоящих уведомлений, папка данных — временная."""

import asyncio
import json
import re
import time
from pathlib import Path

import pytest
import test_integration_a as ia
from test_integration_a import ME, WORK, own_work

from mcp_devian import (
    buyers,
    commissions,
    db,
    digest,
    earnings,
    mcp_server,
    monitor,
    outreach,
    sales,
    selling,
    translate_guard,
)
from mcp_devian import th_characters as tc
from mcp_devian.config import load_config, save_config

fake, server = ia.fake, ia.server
ROOT = Path(__file__).parent.parent
DAY = 86400


def _call(name: str, args: dict) -> str:
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    return "".join(getattr(c, "text", "") for c in result.content)


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}


def _enum(tool: str, param: str = "action") -> list[str]:
    return _tools()[tool].input_schema["properties"][param]["enum"]


def _id(text: str) -> str:
    m = re.search(r"\[([0-9a-z]+)\]", text)
    assert m, text
    return m.group(1)


# --- Карта инструментов и бюджет ---

def test_new_actions_live_on_existing_tools_within_budget():
    tools = _tools()
    assert len(tools) == 29  # ни одного нового инструмента
    schema = sum(len(json.dumps({"name": t.name, "description": t.description, "inputSchema": t.input_schema},
                                ensure_ascii=False)) for t in tools.values())
    assert schema <= 16000
    assert {"hours", "hourly", "goal", "plan", "report", *mcp_server.YCH_ACTIONS} <= set(_enum("sales"))
    assert {"check", "blacklist", "mark", "scam_lists"} <= set(_enum("buyers"))
    assert {"sheet", "theft", "theft_report"} <= set(_enum("images"))
    assert {"translate", "translate_ok", "translate_forget"} <= set(_enum("advice"))
    # Chrome из этих инструментов не открывается: вкладки поиска и списков она открывает сама по ссылкам.
    assert not {"check_open", "theft_open"} & (set(_enum("buyers")) | set(_enum("images")))
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert [t["name"] for t in manifest["tools"]] == list(tools)
    assert "YCH" in next(t["description"] for t in manifest["tools"] if t["name"] == "sales")


def test_get_settings_explains_money_and_new_keys(server):
    text = mcp_server.get_settings()
    assert mcp_server.MONEY_RULE in text and "ych_claim" in text and "translate_ok" in text
    assert "YCH и коммишены:" in text and "commission_weekly_hours" in text and "goal_nudges" in text
    assert "правило 4.8" in mcp_server.MONEY_RULE
    assert "Сохранено" in mcp_server.update_settings({"commission_weekly_hours": 20, "goal_nudges": True})
    cfg = load_config()
    assert cfg.posting.commission_weekly_hours == 20 and cfg.posting.goal_nudges is True
    assert "Не сохранила" in mcp_server.update_settings({"commission_weekly_hours": 500})
    assert "Не сохранила" in mcp_server.update_settings({"commission_deposit_pct": 150})
    assert commissions.settings_from(load_config()).weekly_hours == 20


def test_prompts_and_readme_sections():
    prompts = {p.name for p in asyncio.run(mcp_server.mcp.list_prompts())}
    assert {"money_goal", "ych", "before_transfer", "translate_post", "month_report"} <= prompts
    readme = (ROOT / "README.md").read_text()
    for head in ("## Заработок в час и цель", "## YCH и коммишены", "## Защита: покупатель и кража",
                 "## Превью адопта", "## Перевод", "## Отчёт за месяц"):
        assert head in readme, head


# --- Часы, ₽ в час, цель, план недели ---

def test_hours_hourly_goal_plan(server):
    _call("sales", {"action": "record", "title": "Moth", "amount": 30, "buyer": "bob", "net": 2500})
    out = _call("sales", {"action": "hours", "title": "Moth", "amount": 3})
    assert "Записала: 3 ч" in out
    assert "уже записано" in _call("sales", {"action": "hours", "title": "Moth", "amount": 3})  # повтор
    hourly = _call("sales", {"action": "hourly", "days": 90})
    assert "ждут оплаты" in hourly and "n" in hourly  # продажа не оплачена — в ₽/ч не идёт
    with db.session() as conn:
        sale_id = conn.execute("SELECT id FROM sales").fetchone()["id"]
    _call("sales", {"action": "status", "sale_id": sale_id, "status": "paid"})
    hourly = _call("sales", {"action": "hourly", "days": 90})
    assert "вывода нет" in hourly.lower()  # одна работа — не вывод
    assert "Цель на" in _call("sales", {"action": "goal", "amount": 30000})
    progress = _call("sales", {"action": "goal"})
    assert "30,000 ₽" in progress and "2,500 ₽ (8%)" in progress and "из нужных 8" in progress
    plan = _call("sales", {"action": "plan", "amount": 3})
    assert "3" in plan and plan.strip()
    undo = _call("sales", {"action": "hours", "amount": 0})
    assert undo.strip() and "Записала" not in undo


# --- YCH и коммишены ---

def test_ych_flow_writes_ledger_and_never_offers_translation_for_toyhouse(server):
    out = _call("sales", {"action": "ych_offer", "title": "Moth YCH", "amount": 2000, "currency": "rub"})
    assert out == "Нужно: format — ych или commission."
    out = _call("sales", {"action": "ych_offer", "format": "ych", "title": "Moth YCH", "amount": 2000,
                          "currency": "rub", "slots": 2, "hours": 4, "platform": "toyhouse"})
    assert out.startswith("Сохранила"), out
    offer_id = _id(out)
    claim = _call("sales", {"action": "ych_claim", "sale_id": offer_id, "buyer": "bob", "title": "bob's moth",
                            "date": "+10"})
    assert claim.startswith("Слот занят"), claim
    slot_id = _id(claim)
    assert "status — deposit" in _call("sales", {"action": "ych_paid", "sale_id": slot_id})
    paid = _call("sales", {"action": "ych_paid", "sale_id": slot_id, "status": "deposit", "net": 900})
    assert "записана в продажи" in paid
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM sales WHERE item_id LIKE 'comm:%'").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "paid" and rows[0]["net_amount"] == 900
    step = _call("sales", {"action": "ych_slot", "sale_id": slot_id, "status": "in_progress", "hours": 3})
    assert "в работе" in step and "~3 ч" in step
    overview = _call("sales", {"action": "ych"})
    assert "Moth YCH" in overview and "bob" in overview
    assert _call("sales", {"action": "ych_load"}).strip()
    assert _call("sales", {"action": "ych_stats"}).strip()
    announce = _call("sales", {"action": "ych_announce", "sale_id": offer_id, "platform": "deviantart"})
    assert "4.8" in announce and "переведу" not in announce  # предложение на Toyhouse: перевода не предлагаем
    da_offer = _id(_call("sales", {"action": "ych_offer", "format": "commission", "title": "Sketch", "amount": 15,
                                   "platform": "deviantart"}))
    assert "переведу" in _call("sales", {"action": "ych_announce", "sale_id": da_offer})
    closed = _call("sales", {"action": "ych_offer", "sale_id": offer_id, "status": "close"})
    assert "закрыто" in closed
    assert "refund" in _call("sales", {"action": "ych_slot", "sale_id": slot_id})
    cancelled = _call("sales", {"action": "ych_slot", "sale_id": slot_id, "status": "refund"})
    assert "из дохода убрала" in cancelled


def test_commission_reminders_in_background_once_and_bounded(server, monkeypatch):
    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(digest, "notify_lines", lambda cfg, title, lines: seen.append((title, lines)) or True)
    now = time.time()
    with db.session() as conn:
        for i in range(5):  # пять слотов без предоплаты уже 5 дней
            commissions.claim(conn, client=f"c{i}", kind="commission", price=1000, currency="rub",
                              deposit_pct=50, now=now - 5 * DAY, tz="Europe/Moscow")
        cfg = load_config()
        first = monitor.money_pass(conn, cfg, now)
        assert len(first) == monitor.COMMISSION_NOTIFY_MAX and len(seen) == 1
        assert all("предоплата" in line for line in seen[0][1])
        second = monitor.money_pass(conn, cfg, now + 60)
        assert len(second) == 2  # оставшиеся два — в следующий раз, уже показанные не повторяются
        assert monitor.money_pass(conn, cfg, now + 120) == []
        # Выключено — ничего не шлём.
        cfg.posting.commission_reminders = False
        commissions.claim(conn, client="late", kind="commission", price=1000, currency="rub", deposit_pct=50,
                          now=now - 5 * DAY, tz="Europe/Moscow")
        assert monitor.money_pass(conn, cfg, now + 180) == []
    assert all(title == "YCH и коммишены" for title, _ in seen)


def test_goal_nudge_off_by_default_then_weekly(server, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(digest, "notify_lines", lambda cfg, title, lines: seen.extend(lines) or True)
    now = time.time()
    with db.session() as conn:
        earnings.set_goal(conn, 30000, now=now, tz="Europe/Moscow")
        cfg = load_config()
        assert cfg.posting.goal_nudges is False
        assert monitor.money_pass(conn, cfg, now) == [] and not seen
        cfg.posting.goal_nudges = True
        lines = monitor.money_pass(conn, cfg, now)
        assert len(lines) == 1 and "Цель месяца" in lines[0] and "спроси в чате" in lines[0]
        assert monitor.money_pass(conn, cfg, now + DAY) == []  # не чаще раза в неделю
        assert len(monitor.money_pass(conn, cfg, now + 8 * DAY)) == 1


def test_growth_pass_includes_money_and_survives_its_failure(server, monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "money_pass", lambda *a, **k: calls.append("money") or ["слоты: x"])
    monkeypatch.setattr(buyers, "sync", lambda *a, **k: {})
    cfg = load_config()
    cfg.deviantart.palette_downloads_per_hour = 0
    with db.session() as conn:
        assert "слоты: x" in monitor.growth_pass(conn, cfg, client=None)

        def boom(*a, **k):
            raise RuntimeError("x")

        monkeypatch.setattr(monitor, "money_pass", boom)
        monkeypatch.setattr(monitor, "_api_client", lambda cfg: None)
        assert isinstance(monitor.growth_pass(conn, cfg), list)
    assert calls == ["money"]


# --- Защита ---

def test_blacklist_check_and_transfer_warning(server):
    out = _call("buyers", {"action": "blacklist", "users": ["scammer"], "text": "не заплатила в марте"})
    assert "scammer" in out
    assert "не заплатила в марте" in _call("buyers", {"action": "blacklist"})
    check = _call("buyers", {"action": "check", "users": ["scammer"], "text": "toyhouse"})
    assert "чёрном списке" in check and "не инструкции" in check
    assert "не гарантия" in check
    with db.session() as conn:
        warns = tc.transfer_warnings(conn, "https://toyhou.se/123.lumi", "scammer")
    assert any("чёрном списке" in w for w in warns)
    assert "Убрала" in _call("buyers", {"action": "blacklist", "users": ["scammer"], "text": "remove"})
    with db.session() as conn:
        assert not any("чёрном списке" in w for w in tc.transfer_warnings(conn, "https://toyhou.se/1.x", "scammer"))


def test_check_reads_da_profile_through_fake_and_survives_da_failure(server, monkeypatch):
    from datetime import UTC, datetime

    from mcp_devian import posting

    class Profiles:
        def __init__(self, fail=False):
            self.calls, self.fail = [], fail

        def user_profile_raw(self, name):
            self.calls.append(name)
            if self.fail:
                raise RuntimeError("DeviantArt упал")
            joined = datetime.fromtimestamp(time.time() - 3 * DAY, UTC).strftime("%Y-%m-%dT%H:%M:%S%z")
            return {"user": {"username": name, "type": "regular", "details": {"joindate": joined, "age": 15}},
                    "stats": {"user_deviations": 0, "user_comments": 0}}

    ok = Profiles()
    monkeypatch.setattr(posting, "da_client", lambda cfg: ok)
    out = _call("buyers", {"action": "check", "users": ["newbie"], "text": "DA"})
    assert ok.calls == ["newbie"] and "младше 18" in out and "3 дн" in out
    # Toyhouse: DeviantArt не спрашиваем — там может быть другой человек.
    _call("buyers", {"action": "check", "users": ["other"], "text": "toyhouse"})
    assert ok.calls == ["newbie"]
    bad = Profiles(fail=True)
    monkeypatch.setattr(posting, "da_client", lambda cfg: bad)
    out = _call("buyers", {"action": "check", "users": ["ghost"], "text": "deviantart"})
    assert bad.calls == ["ghost"] and "Проверка покупателя ghost" in out and "твой чёрный список" in out


def test_sale_status_no_pay_and_refund_mark_buyer(server):
    _call("sales", {"action": "record", "title": "Moth", "amount": 30, "buyer": "bob"})
    with db.session() as conn:
        sale_id = conn.execute("SELECT id FROM sales").fetchone()["id"]
    out = _call("sales", {"action": "status", "sale_id": sale_id, "status": "no_pay"})
    assert "не заплатил" in out
    with db.session() as conn:
        assert sales.get(conn, sale_id)["status"] == "cancelled"
    check = _call("buyers", {"action": "check", "users": ["bob"], "text": "toyhouse"})
    assert "не заплатил" in check
    assert "Не получилось" in _call("sales", {"action": "status", "sale_id": "nope0000", "status": "disputed"})
    mark = _call("buyers", {"action": "mark", "users": ["amy"], "text": "paid: всё вовремя"})
    assert "оплатил" in mark
    assert "Отметка" in _call("buyers", {"action": "mark", "users": ["amy"], "text": "weird"})


def test_scam_lists_theft_and_report_kit(server):
    lists = _call("buyers", {"action": "scam_lists"})
    assert "Страницы для ручной проверки" in lists
    added = _call("buyers", {"action": "scam_lists", "ref": "https://example.org/list?u={user}", "text": "Мой список"})
    assert "Мой список" in added
    page = _call("images", {"action": "theft", "file_path": "https://www.deviantart.com/someone/art/Moth-1",
                            "text": "https://www.deviantart.com/someone/art/Moth-1"})
    assert "адрес страницы" in page
    img = _call("images", {"action": "theft", "file_path": "https://images-wixmp-ed30a86b8c4ca887773594c2.wixmp.com/f/a.png"})
    assert "tineye" in img.lower() and "Chrome" not in img
    kit = _call("images", {"action": "theft_report", "file_path": WORK, "text": "https://www.deviantart.com/thief/art/Copy-9"})
    assert "DeviantArt" in kit
    assert _call("images", {"action": "theft_report", "file_path": WORK}) == "Нужно: text."


# --- Превью-лист и отчёт ---

def test_sheet_and_month_report_write_only_local_files(server):
    from PIL import Image

    src = server / "Downloads" / "moth.png"
    Image.new("RGB", (800, 600), (200, 100, 50)).save(src)
    out = _call("images", {"action": "sheet", "file_path": str(src), "text": "Moth",
                           "prices": {"sb": 15, "ab": 60}, "status": "open"})
    made = list((server / "Downloads").glob("moth-sheet*"))
    assert made and "moth-sheet" in out
    one = _call("images", {"action": "sheet", "file_path": str(src), "prices": {"price": 25}, "position": "side"})
    assert "moth-sheet" in one
    _call("sales", {"action": "record", "title": "Moth", "amount": 30, "buyer": "bob", "net": 2500})
    month = time.strftime("%Y-%m")
    rep = _call("sales", {"action": "report", "date": month})
    assert "никуда не отправлен" in rep
    files = list((server / "Downloads").glob("*.html"))
    assert len(files) == 1 and "<html" in files[0].read_text().lower()
    assert "Отчёт не собрала" in _call("sales", {"action": "report", "date": "вчера"})


# --- Перевод: DA/X — да, Toyhouse — никогда ---

RU = "Привет! Это мой новый мотылёк, стартовая ставка пятнадцать долларов, автовыкуп шестьдесят."
EN = "Hi! This is my new moth, starting bid fifteen dollars, autobuy sixty dollars, thank you so much."


def test_translate_flow_and_toyhouse_block(server):
    assert "4.8" in _call("advice", {"action": "translate", "text": RU, "target": "toyhouse"})
    instr = _call("advice", {"action": "translate", "text": RU, "target": "da"})
    assert "DeviantArt" in instr and RU in instr
    saved = _call("advice", {"action": "translate", "text": RU, "translation": EN, "target": "x"})
    tid = re.search(r"\[(tr[0-9a-f]+)\]", saved).group(1)
    assert "RU" in saved and "EN" in saved
    # Toyhouse: шаблон отклика и профиль перса с этим переводом — не пускаем.
    assert "Не сохранила" in _call("set_template", {"kind": "reply", "text": EN})
    assert load_config().posting.reply_templates == {}
    blocked = _call("th_character", {"action": "new", "name": "Lumi", "images": ["a.png"], "profile": EN})
    assert "Черновик не готовлю" in blocked and "4.8" in blocked
    # DA/X: напоминание, пока она не проверила; после «ок» — тихо.
    assert "Перевод:" in mcp_server._publish_notes(load_config(), "Moth", EN)
    assert mcp_server._translation_note(EN, "x")
    assert "проверила" in _call("advice", {"action": "translate_ok", "text": tid})
    assert "Перевод:" not in mcp_server._publish_notes(load_config(), "Moth", EN)
    # На Toyhouse — даже проверенный.
    with db.session() as conn:
        assert translate_guard.check_toyhouse(conn, EN)
    assert "сняла" in _call("advice", {"action": "translate_forget", "text": tid})
    assert "сохранён" in _call("set_template", {"kind": "reply", "text": EN})


def test_toyhouse_reply_draft_refuses_translated_template(server, monkeypatch):
    from mcp_devian import posting
    from mcp_devian.db import Item

    with db.session() as conn:
        translate_guard.save(conn, RU, EN, "da")
        db.upsert_item(conn, Item(id="th:77", source="toyhouse", title="LF moth", url="https://toyhou.se/~forums/1.x/77",
                                  author="someone", lot_type="lf", status="open"))
    cfg = load_config()
    cfg.posting.reply_templates = {"": EN + " {url}"}
    cfg.posting.characters = [{"name": "Lumi", "url": "https://toyhou.se/1.lumi", "price": "", "tags": []}]
    save_config(cfg)
    with db.session() as conn:
        draft_id, text = posting.prepare_toyhouse_reply(conn, load_config(), "th:77", "Lumi")
    assert draft_id is None and text.startswith("Отклик не готовлю") and "4.8" in text, text


# --- Личка: только покупателям, ставившим на её лоты и пинглисту ---

def test_for_adopt_never_drafts_dm_for_favers_only(server):
    with db.session() as conn:
        own_work(conn)
        selling.set_template(conn, "offer", "Hi {name}! «{title}» is up — {why}. {link}")
        outreach.note_watch_run(conn, time.time() - 60)
        now = time.time()
        buyers.add_event(conn, "FaveOnly", "faver", "da:5", now - DAY)
        buyers.add_event(conn, "Commenter", "commenter", "da:5", now - DAY)
        buyers.add_event(conn, "Bidder", "bidder", "da:5", now - DAY)
        buyers.sync(conn, now, ME)
    out = _call("buyers", {"action": "for_adopt", "ref": WORK})
    assert "FaveOnly" not in out and "Commenter" not in out
    with db.session() as conn:
        logged = {r["username"] for r in conn.execute("SELECT username FROM outreach_log")}
    assert "faveonly" not in logged and "commenter" not in logged
    crm = _call("buyers", {"action": "crm"})
    assert "лично не пишем" in crm
    assert outreach.channel_for({"roles": ["faver"], "platforms": ["deviantart"]}) is None
    assert outreach.channel_for({"roles": ["bidder"], "platforms": ["deviantart"]}) == "da_message_manual"


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    monkeypatch.setattr(digest, "notify_macos", lambda *a, **k: (_ for _ in ()).throw(AssertionError("уведомление")))
    monkeypatch.setattr(digest, "send_telegram", lambda *a, **k: (_ for _ in ()).throw(AssertionError("telegram")))
