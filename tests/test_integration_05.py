"""Подключение 0.5: покупатели, продвижение, история рынка, аналитика, оплата — в инструментах сервера,
в фоновой проверке, в настройках, в манифесте и README.

Всё на подделках: ни DeviantArt, ни X, ни Chrome, ни сети, ни настоящих уведомлений."""

import argparse
import asyncio
import json
import re
import time
import tomllib
from pathlib import Path

import pytest
import test_integration_a as ia
from test_integration_a import ME, WORK, own_work

import mcp_devian
from mcp_devian import (
    backfill,
    buyers,
    cli,
    da_access,
    db,
    digest,
    leads,
    mcp_server,
    monitor,
    outreach,
    palette,
    pinglist,
    posting,
    selling,
    similar,
)
from mcp_devian.config import Config, load_config, save_config

fake, server = ia.fake, ia.server
ROOT = Path(__file__).parent.parent


def _call(name: str, args: dict) -> str:
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    return "".join(getattr(c, "text", "") for c in result.content)


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}


def _enum(tool: str, param: str = "action") -> list[str]:
    return _tools()[tool].input_schema["properties"][param]["enum"]


def _drafts(kind: str | None = None) -> list:
    with db.session() as conn:
        sql = "SELECT * FROM drafts" + (" WHERE kind = ?" if kind else "")
        return conn.execute(sql, (kind,) if kind else ()).fetchall()


def _template(kind: str, text: str) -> None:
    with db.session() as conn:
        selling.set_template(conn, kind, text)


# --- Инструменты, манифест, версии ---

def test_versions_and_manifest_match_server_tools():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert manifest["version"] == pyproject["project"]["version"] == mcp_devian.__version__ == "0.5.1"
    tools = _tools()
    assert [t["name"] for t in manifest["tools"]] == list(tools)
    assert {"buyers", "promo"} <= set(tools) and len(tools) <= 40
    assert all(t.get("description") for t in manifest["tools"])


def test_new_actions_live_on_consolidated_tools():
    assert {"th_prices", "similar", "backfill"} <= set(_enum("market"))
    assert "palette" in _enum("forecast")
    assert set(_enum("buyers")) == {"crm", "for_adopt", "leads", "lead_add", "offer", "contacted", "optout",
                                    "x_search", "check", "blacklist", "mark", "scam_lists"}
    assert set(_enum("promo")) == {"x_post", "x_posted", "repromote", "group_done", "status", "journal", "raffle",
                                   "raffle_draw", "funnel", "groups", "experiment", "checklist", "bundle", "facts",
                                   "plan"}
    assert {"payment_info", "offer", "x_post", "raffle", "raffle_winner", "status"} <= set(_enum("set_template",
                                                                                                "kind"))
    assert "limit" in _tools()["da_groups"].input_schema["properties"]
    # Каждый новый шаблон знает свои слоты (selling ← xpost, raffle).
    assert set(_enum("set_template", "kind")) - {"reply"} <= set(selling.TEMPLATE_SLOTS)


def test_buyers_and_promo_never_publish_by_themselves():
    tools = _tools()
    for name in ("buyers", "promo"):
        assert tools[name].annotations.destructive_hint is False, name
    assert "черновик" in tools["promo"].description.lower()


def test_prompts_find_buyers_and_promote_post():
    prompts = {p.name: p for p in asyncio.run(mcp_server.mcp.list_prompts())}
    assert {"find_buyers", "promote_post"} <= set(prompts)
    assert "«го»" in mcp_server.find_buyers() and "buyers:for_adopt" in mcp_server.find_buyers()
    assert "«го»" in mcp_server.promote_post() and "promo:x_post" in mcp_server.promote_post()


def test_readme_has_05_sections():
    text = (ROOT / "README.md").read_text()
    for head in ("Покупатели", "Продвижение", "Twitter", "Розыгрыши", "Boosty и оплата", "История рынка",
                 "Что проверить вживую"):
        assert re.search(rf"^##+ .*{head}", text, re.M), head
    assert "USD/EUR" not in text  # валюту доната Boosty не выбрать: «переключи на USD/EUR» — неправда


# --- Настройки и первая настройка ---

def test_settings_show_growth_and_opt_ins(server):
    text = _call("get_settings", {})
    assert "Оплата: Boosty" in text and "Покупатели: предложений до" in text
    assert "Toyhouse LF в фоне (уведомления о покупателях): нет" in text
    assert "ранний доступ пинглиста выкл" in text
    assert mcp_server.GROWTH_RULE in text
    out = mcp_server.update_settings({"boosty_url": "boosty.to/her", "payment_methods": ["boosty"],
                                      "toyhouse_background_lf": True, "pinglist_early_access_hours": 12,
                                      "outreach_per_day": 3})
    assert out.startswith("Сохранено"), out
    cfg = load_config()
    assert cfg.posting.boosty_url == "https://boosty.to/her" and cfg.toyhouse.background_lf is True
    assert cfg.posting.pinglist_early_access_hours == 12 and cfg.posting.outreach_per_day == 3
    assert "Не сохранила" in mcp_server.update_settings({"outreach_per_day": 50})


def test_setup_questions_offer_undecided_options_off_by_default():
    q = mcp_server.SETUP_QUESTIONS
    assert "toyhouse_background_lf" in q and "pinglist_early_access_hours" in q
    cfg = Config()
    assert cfg.toyhouse.background_lf is False and cfg.posting.pinglist_early_access_hours == 0


# --- Рынок и аналитика ---

def test_market_backfill_needs_connected_da(server, fake, monkeypatch):
    fake.connected = False
    assert "accounts:da_connect" in _call("market", {"action": "backfill"})
    fake.connected = True
    seen = {}
    monkeypatch.setattr(backfill, "step", lambda conn, client, now, **kw: seen.update(kw) or ["история: +3 лота"])
    assert _call("market", {"action": "backfill"}) == "история: +3 лота"
    cfg = load_config()
    # Регрессия: из чата шёл полный проход (40 запросов × 1,5 с пауз = 60+ с внутри вызова инструмента).
    assert seen["days"] == cfg.deviantart.backfill_days and seen["max_api"] == backfill.CHAT_MAX_API < 40
    assert seen["cfg"] is not None


@pytest.mark.parametrize(("tool", "args"), [
    ("market", {"action": "similar"}),
    ("market", {"action": "th_prices"}),
    ("forecast", {"action": "palette"}),
    ("forecast", {"action": "health"}),
    ("buyers", {"action": "crm"}),
    ("buyers", {"action": "leads"}),
    ("buyers", {"action": "x_search"}),
    ("promo", {"action": "funnel"}),
    ("promo", {"action": "groups"}),
    ("promo", {"action": "experiment"}),
    ("promo", {"action": "repromote"}),
    ("promo", {"action": "bundle"}),
    ("promo", {"action": "facts"}),
    ("promo", {"action": "checklist", "text": "Moth adopt auction. SB $10, AB $60"}),
    ("promo", {"action": "plan", "text": "40"}),
    ("sales", {"action": "unpaid"}),
    ("sales", {"action": "income"}),
])
def test_read_actions_answer_on_empty_base(server, tool, args):
    out = _call(tool, args)
    assert out and "Traceback" not in out and "Error executing" not in out, out


def test_health_carries_market_history_line(server, monkeypatch):
    monkeypatch.setattr(backfill, "progress", lambda conn, now=None, **kw: "История рынка: 12 из 40 лотов")
    assert "История рынка: 12 из 40 лотов" in _call("forecast", {"action": "health"})


def test_palette_of_her_file_stays_local(server, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(palette, "of_file", lambda conn, path, niche=None: seen.append(path) or "палитра")
    assert _call("forecast", {"action": "palette", "file_path": str(tmp_path / "a.png")}) == "палитра"
    assert seen == [str(tmp_path / "a.png")]


def test_review_appends_funnel_verdict(server, monkeypatch):
    from mcp_devian import advisor, funnel

    with db.session() as conn:
        own_work(conn)
    monkeypatch.setattr(advisor, "listing_review", lambda item, conn, cfg, tz=None: ["правка"])
    monkeypatch.setattr(funnel, "diagnose", lambda conn, item_id, tz=None, niche=None: ["Вывода нет: лот моложе"])
    assert _call("advice", {"action": "review", "url": WORK}) == "- правка\n- Вывода нет: лот моложе"


# --- Покупатели ---

def test_for_adopt_without_template_or_watch_makes_no_mention_draft(server):
    assert "нет в базе" in _call("buyers", {"action": "for_adopt", "ref": "da:404"})
    with db.session() as conn:
        own_work(conn)
        pinglist.add(conn, ["bob"])
    out = _call("buyers", {"action": "for_adopt", "ref": "da:5"})
    assert "шаблон" in out and mcp_server.GO_RULE not in out
    _template("offer", "Hi {name}! My new adopt {title} might be your style — {why}.")
    out = _call("buyers", {"action": "for_adopt", "ref": "da:5"})
    # Ответы «stop» под её работами фон ещё не читал: обещание «больше не отмечу» не даём — без отметок.
    assert not _drafts("da_offer"), out
    assert mcp_server.GO_RULE not in out


def test_opted_out_and_minor_never_offered(server):
    with db.session() as conn:
        own_work(conn)
        pinglist.add(conn, ["bob", "kid"])
        buyers.sync(conn, time.time(), ME)
        conn.execute("UPDATE contacts SET is_minor = 1 WHERE username = 'kid'")
        conn.commit()
    assert "bob" in _call("buyers", {"action": "optout", "users": ["bob"]})
    with db.session() as conn:
        ranked = outreach.rank_for_adopt(conn, "da:5", time.time(), 10)
    assert {c["user"] for c in ranked}.isdisjoint({"bob", "kid"})
    assert "bob" in _call("buyers", {"action": "optout", "users": ["bob"], "text": "in"})


def test_x_lead_reply_is_her_template_and_an_intent_link(server):
    assert "Не добавила" in _call("buyers", {"action": "lead_add", "ref": "https://x.com/i/status/1"})
    out = _call("buyers", {"action": "lead_add", "ref": "https://x.com/moth_fan/status/1851234567890123456",
                           "text": "LF moth adopts, budget $40"})
    lead = re.search(r"Лид (\d+)", out).group(1)
    assert "Не готовлю" in _call("buyers", {"action": "offer", "ref": lead})  # нет её шаблона — ничего не сочиняю
    _template("offer", "Hi {name}! I have a moth adopt open, {why}")
    out = _call("buyers", {"action": "offer", "ref": lead})
    assert "Hi moth_fan! I have a moth adopt open, saw your LF post" in out
    assert "x.com/intent/" in out and "in_reply_to=1851234567890123456" in out and "«Post» жмёт она" in out
    assert not _drafts()  # в X ничего не уходит: ссылку открывает она
    done = _call("buyers", {"action": "contacted", "users": ["moth_fan"], "ref": f"lead:{lead}"})
    assert "moth_fan" in done
    with db.session() as conn:
        assert outreach.person_block(conn, "moth_fan", time.time())


def test_x_search_gives_links_only(server):
    out = _call("buyers", {"action": "x_search"})
    assert "x.com/search" in out and "помощник X не читает" in out


# --- Продвижение ---

def test_status_and_journal_are_drafts_waiting_for_go(server, fake):
    out = _call("promo", {"action": "status", "text": "New moth adopt is up!"})
    assert out.startswith("Черновик ") and mcp_server.GO_RULE in out
    assert fake.posted == [] and [r["status"] for r in _drafts("da_status")] == ["pending"]


def test_x_post_refuses_someone_elses_lot(server):
    from mcp_devian.db import Item

    with db.session() as conn:
        db.upsert_item(conn, Item(id="da:9", source="deviantart", title="Fox", author="other",
                                  url="https://www.deviantart.com/other/art/Fox-9", lot_type="auction", own=False,
                                  published_ts=time.time()))
    out = _call("promo", {"action": "x_post", "ref": "da:9"})
    assert "x.com/intent" not in out


def test_raffle_refuses_money_prize_and_needs_her_template(server):
    ends = time.strftime("%d.%m 19:00", time.localtime(time.time() + 3 * 86400))
    assert "не деньги" in _call("promo", {"action": "raffle", "text": "500 рублей", "ref": ends})
    assert "шаблон" in _call("promo", {"action": "raffle", "text": "moth adopt", "ref": ends})
    assert not _drafts()


def test_payment_plan_off_by_default(server):
    assert "не предлагается" in _call("promo", {"action": "plan", "text": "500"})


# --- Продажи и оплата ---

def test_points_sale_warns_without_counting_itself(server):
    out = _call("sales", {"action": "record", "title": "Moth", "amount": 12000, "currency": "points",
                          "buyer": "bob"})
    assert "Записала" in out and "уже" not in out, out  # сама продажа в «уже переведено» не считается
    assert "3,500" in out  # одним переводом — не больше 3 500 поинтов


def test_default_payment_text_is_only_a_suggestion(server):
    with db.session() as conn:
        own_work(conn)
    _call("sales", {"action": "record", "title": "Moth", "amount": 30, "buyer": "bob", "url": WORK})
    out = _call("sales", {"action": "unpaid", "days": 0})
    assert "set_template" in out or "Неоплаченных" in out or "нет" in out.lower()
    assert not _drafts("da_comment")


# --- Фон: monitor.growth_pass и cli ---

class FakeClient:
    def is_connected(self):
        return True


@pytest.fixture
def bg(server, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(backfill, "step", lambda *a, **kw: calls.append(f"backfill quiet={kw.get('quiet')}") or [])
    monkeypatch.setattr(similar, "step", lambda *a, **kw: calls.append("similar") or [])
    monkeypatch.setattr(buyers, "refresh_favers", lambda *a, **kw: calls.append("favers") or {})
    monkeypatch.setattr(leads, "da_lf_scan", lambda *a, **kw: calls.append("da_lf") or {"new": 2})
    monkeypatch.setattr(outreach, "watch_offer_replies",
                        lambda *a, **kw: calls.append("offer_replies") or {"opted_out": 0})
    monkeypatch.setattr(leads, "th_alerts", lambda *a, **kw: calls.append("th_alerts") or ["LF: moth"])
    monkeypatch.setattr(digest, "notify_lines", lambda cfg, title, lines: calls.append(f"notify {title}") or True)
    monkeypatch.setattr(palette, "client", lambda: (_ for _ in ()).throw(AssertionError("сеть в тесте")))
    return calls


def test_growth_pass_runs_each_part_bounded_and_default_off_th(bg):
    cfg = load_config()
    with db.session() as conn:
        lines = monitor.growth_pass(conn, cfg, client=FakeClient(), palette_fetch=lambda url: b"",
                                    palette_sleep=lambda s: None)
    assert {"backfill quiet=True", "similar", "favers", "da_lf", "offer_replies"} <= set(bg)
    assert "th_alerts" not in bg  # фон Toyhouse LF — только если она включила
    assert any("buyers:leads" in line for line in lines)


def test_growth_pass_throttles_buyer_reads_and_opt_in_th(bg):
    cfg = load_config()
    cfg.toyhouse.background_lf = True
    save_config(cfg)
    cfg = load_config()
    now = time.time()
    with db.session() as conn:
        monitor.growth_pass(conn, cfg, now, client=FakeClient(), palette_fetch=lambda url: b"",
                            palette_sleep=lambda s: None)
        bg.clear()
        monitor.growth_pass(conn, cfg, now + 60, client=FakeClient(), palette_fetch=lambda url: b"",
                            palette_sleep=lambda s: None)
    assert "favers" not in bg and "da_lf" not in bg and "offer_replies" not in bg  # не чаще раза в час
    assert "th_alerts" in bg and "notify Нашла покупателя на Toyhouse" in bg


def test_growth_pass_survives_failures_and_skips_without_client(bg, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("DA лежит")

    monkeypatch.setattr(backfill, "step", boom)
    monkeypatch.setattr(similar, "step", boom)
    cfg = load_config()
    with db.session() as conn:
        assert isinstance(monitor.growth_pass(conn, cfg, client=FakeClient(), palette_fetch=lambda url: b"",
                                              palette_sleep=lambda s: None), list)
    assert "favers" in bg and "offer_replies" in bg
    bg.clear()
    monkeypatch.setattr(monitor, "_api_client", lambda cfg: None)
    with db.session() as conn:
        monitor.growth_pass(conn, cfg, palette_fetch=lambda url: b"", palette_sleep=lambda s: None)
    assert not {"favers", "da_lf", "offer_replies"} & set(bg)


def test_palette_waits_out_da_cooldown(bg, monkeypatch):
    steps = []
    monkeypatch.setattr(palette, "step", lambda *a, **kw: steps.append(kw.get("per_hour")))
    cfg = load_config()
    with db.session() as conn:
        monkeypatch.setattr(da_access, "cooldowns", lambda conn, now=None: {"api": object()})
        monitor.growth_pass(conn, cfg, client=FakeClient(), palette_fetch=lambda url: b"")
        assert steps == []
        monkeypatch.setattr(da_access, "cooldowns", lambda conn, now=None: {})
        monitor.growth_pass(conn, cfg, client=FakeClient(), palette_fetch=lambda url: b"")
    assert steps == [cfg.deviantart.palette_downloads_per_hour]
    cfg.deviantart.palette_downloads_per_hour = 0
    steps.clear()
    with db.session() as conn:
        monitor.growth_pass(conn, cfg, client=FakeClient(), palette_fetch=lambda url: b"")
    assert steps == []


def test_da_connect_queues_market_history_only_on_success(server, monkeypatch):
    from mcp_devian import da_api

    queued = []
    monkeypatch.setattr(backfill, "enqueue", lambda conn, groups, me, days, now, cfg=None: queued.append((me, days)))
    monkeypatch.setattr(da_api, "connect_main", lambda client_id, scope="": 1)
    assert cli.cmd_da_connect(argparse.Namespace()) == 1 and queued == []
    scopes = []
    monkeypatch.setattr(da_api, "connect_main", lambda client_id, scope="": scopes.append(scope) or 0)
    assert cli.cmd_da_connect(argparse.Namespace()) == 0
    assert queued == [(ME, load_config().deviantart.backfill_days)]
    assert scopes == [da_api.scope_string(load_config().posting.da_extra_scopes)]


def test_chat_check_does_not_run_slow_growth_pass(server, monkeypatch):
    """check:run из чата ждёт ~45 с: фон 0.5 (палитры с паузами, история рынка) идёт только в плановой проверке."""
    from mcp_devian import jobs

    monkeypatch.setattr(monitor, "growth_pass", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("growth")))
    monkeypatch.setattr(monitor, "run_check", lambda *a, **kw: [])
    monkeypatch.setattr(jobs, "finish", lambda job, lines, ok=True: None)
    assert cli._check_job(argparse.Namespace(source="all", job="j1")) == 0


def test_no_public_text_without_go(server, fake):
    """Ни одно действие buyers/promo само ничего не публикует: максимум — черновик, ждущий «го»."""
    _template("offer", "Hi {name}! {title} — {why}")
    _template("status", "New: {title} {url}")
    with db.session() as conn:
        own_work(conn)
        pinglist.add(conn, ["bob"])
    for tool, args in (("buyers", {"action": "for_adopt", "ref": "da:5"}),
                       ("promo", {"action": "status", "text": "Moth is up"}),
                       ("promo", {"action": "repromote"}),
                       ("promo", {"action": "x_post", "ref": "da:5"})):
        _call(tool, args)
    assert fake.posted == []
    assert all(r["status"] == "pending" for r in _drafts())
    assert posting.offer_check  # отправка da_offer перепроверяет всё по тем же правилам


def test_scheduled_check_runs_growth_without_da_read(server, monkeypatch):
    """Регрессия: фон 0.5 шёл только после чтения DeviantArt — продавщица только на Toyhouse (или пока DA на
    паузе) не получала LF-уведомлений Toyhouse, синхронизации покупателей и палитр."""
    ran = []
    monkeypatch.setattr(monitor, "growth_pass", lambda *a, **kw: ran.append("growth") or [])
    monkeypatch.setattr(monitor, "run_check", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_send_scheduled", lambda *a, **kw: None)
    monkeypatch.setattr(digest, "deliver_weekly", lambda *a, **kw: None)
    monkeypatch.setattr(digest, "deliver", lambda *a, **kw: {"items": 0, "file": "", "telegram_error": None})
    assert cli._check(argparse.Namespace(source="all", scheduled=True, notify=False), None) == 0
    assert ran == ["growth"]
    ran.clear()
    assert cli._check(argparse.Namespace(source="toyhouse", scheduled=False, notify=False), None) == 0
    assert ran == []  # ручная проверка одного Toyhouse — без фона


# --- регрессии ревью UX 0.5 ---

def test_promo_description_glosses_actions():
    desc = " ".join(_tools()["promo"].description.split())
    for gloss in ("почему не берут", "facts — Boosty", "plan — частями", "journal (text, ref заголовок)",
                  "x_posted (ref x…|лот"):
        assert gloss in desc, gloss
    assert "promo:checklist" not in mcp_server.promote_post() and "advice:review" in mcp_server.promote_post()


def test_checklist_with_link_uses_stored_description(server, fake):
    """Регрессия: promo:checklist со ссылкой проверял саму ссылку — «на месте 0 из 4»."""
    with db.session() as conn:
        own_work(conn)
    out = _call("promo", {"action": "checklist", "text": WORK})
    assert "0 из 4" not in out and "Не проверяю" in out and "advice:review" in out
    with db.session() as conn:
        conn.execute("UPDATE items SET extra = ? WHERE id = 'da:5'",
                     (json.dumps({"desc": "Moth dragon auction. SB $10, MI $2, AB $80. Ends 48h. Pay via Boosty "
                                          "within 48h. TOS in my profile.", "desc_ts": time.time()}),))
        conn.commit()
    out = _call("promo", {"action": "checklist", "text": WORK})
    assert "Не проверяю" not in out


def test_for_adopt_and_x_post_explain_non_da_refs(server, fake):
    """Регрессия: ссылка Toyhouse или «дракон» давали «обнови её работы (check:run source=deviantart)»."""
    th = "https://toyhou.se/12345.ember"
    for tool, args in (("buyers", {"action": "for_adopt", "ref": th}), ("promo", {"action": "x_post", "ref": th})):
        out = _call(tool, args)
        assert "Toyhouse" in out and "findings:replies" in out and "check:run" not in out, out
    out = _call("promo", {"action": "x_post", "ref": "дракон"})
    assert "По названию" in out and "check:run" not in out


def test_x_posted_accepts_the_lot_link(server, fake):
    _template("x_post", "{title} is up!")
    with db.session() as conn:
        own_work(conn)
    made = _call("promo", {"action": "x_post", "ref": "da:5"})
    assert "promo:x_posted ref=x" in made
    out = _call("promo", {"action": "x_posted", "ref": WORK, "text": "https://x.com/her/status/1234567890"})
    assert out.startswith("Отметила: пост x"), out


@pytest.mark.parametrize("text,price", [("https://www.deviantart.com/her/art/Moth-123456 $40", 40.0),
                                        ("Dragon 2, AB 120$", 120.0), ("120", 120.0), ("SB: 15", 15.0),
                                        ("1500 pts", 1500.0), ("цена 40", 40.0)])
def test_plan_price_ignores_links_and_prefers_currency(text, price):
    assert mcp_server._price(text) == price
