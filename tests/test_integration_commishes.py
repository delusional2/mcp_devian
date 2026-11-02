"""Подключение ych.commishes.com: действия на старых инструментах (market, da_auctions, sales, promo, advice,
forecast), подсказка цен YCH, настройки и строка онбординга, фон, манифест и README, бюджет токенов.

Всё на подделках: сайт — подменённый транспорт httpx с фикстурами tests/fixtures_commishes, время модуля
commishes — подменённое, папка данных — временная. Ни сети, ни osascript, ни браузера."""

import asyncio
import contextlib
import json
import time as time_mod
from pathlib import Path

import httpx
import pytest
import test_integration_a as ia
from test_commishes import ADOPT_RSS, ENDS, JSON_61A2P, NOW, PAGE_ADOPT, PAGE_YCH, PROFILE, Site, bid, json_with

from mcp_devian import cli, commishes, commissions, config, db, digest, mcp_server, monitor, report, sales
from mcp_devian.config import load_config, save_config

fake, server = ia.fake, ia.server
ROOT = Path(__file__).parent.parent
URL = "https://ych.commishes.com/auction/show/61A2P/loyal-shepherd-dog-adopt/"
ME = "Pinecrow"  # продавец из фикстур


def _call(name: str, args: dict) -> str:
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    return "".join(getattr(c, "text", "") for c in result.content)


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}


class FakeTime:
    """Время модуля commishes: фикстуры сняты в момент NOW, и ответы не должны зависеть от сегодняшней даты."""
    t = NOW

    @classmethod
    def time(cls) -> float:
        return cls.t

    @staticmethod
    def sleep(s: float) -> None:
        pass

    monotonic = staticmethod(time_mod.monotonic)


def her_site() -> Site:
    other = json.loads(JSON_61A2P)
    other["payload"].update(id=6335065, title="Shy Candy Bear - Adopt", endsunix=1793655336)
    other["payload"]["slots"] = {"6335065": {**other["payload"]["slots"]["6334553"], "name": "Shy Candy Bear",
                                             "highestbid": 50, "bids": [bid("Brindlefox", 50, NOW - 9000)]}}
    return Site({f"/user/{ME}/": PROFILE, "/auction/show/61A2P/.json": JSON_61A2P,
                 "/auction/show/61A2P/": PAGE_ADOPT, "/auction/show/61AIP/.json": json.dumps(other),
                 "/auction/show/61AIP/": PAGE_ADOPT.replace("6334553", "6335065").replace("61A2P", "61AIP"),
                 "/category/adopt/index.rss": ADOPT_RSS})


@pytest.fixture
def site(server, monkeypatch):
    s = her_site()
    FakeTime.t = NOW
    monkeypatch.setattr(commishes, "time", FakeTime)

    @contextlib.contextmanager
    def client_for(conn):
        yield commishes.Client(conn, http=commishes.make_http(transport=httpx.MockTransport(s.handler)))

    monkeypatch.setattr(commishes, "client_for", client_for)
    monkeypatch.setattr(monitor, "refresh_auctions", lambda *a, **k: ([], []))
    return s


def _set(**changes) -> str:
    return mcp_server.update_settings(changes)


def _conn():
    return db.session()


# --- карта инструментов, бюджет, манифест, README ---

def test_actions_live_on_existing_tools_within_budget():
    tools = _tools()
    assert len(tools) == 29 and len(tools) <= 32  # ни одного нового инструмента
    schema = sum(len(json.dumps({"name": t.name, "description": t.description, "inputSchema": t.input_schema},
                                ensure_ascii=False)) for t in tools.values())
    assert schema <= 16000
    assert "commishes" in tools["market"].input_schema["properties"]["action"]["enum"]
    assert "ych.commishes" in tools["da_auctions"].description
    assert "commishes" in tools["sales"].description
    for t in tools.values():
        assert len(t.description) <= 400 and t.description.count("\n") <= 3, t.name


def test_manifest_and_readme_know_the_site():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert [t["name"] for t in manifest["tools"]] == list(_tools())
    desc = {t["name"]: t["description"] for t in manifest["tools"]}
    for name in ("market", "da_auctions", "sales", "promo"):
        assert "ych.commishes" in desc[name], name
    assert "ych.commishes" in manifest["description"] and manifest["version"] == "0.5.1"
    readme = (ROOT / "README.md").read_text()
    assert "## ych.commishes.com" in readme
    section = readme.split("## ych.commishes.com", 1)[1].split("\n## ", 1)[0]
    for word in ("commishes_username", "market:commishes", "da_auctions:status", "variant=commishes",
                 "ai-train=no", "20 запросов", "Ставок он не делает никогда"):
        assert word in section, word


def test_rules_for_claude_live_in_get_settings_not_in_schema(server):
    text = mcp_server.get_settings()
    assert mcp_server.COMMISHES_RULE in text and "commishes_username" in text
    assert "market:commishes" not in "".join(t.description for t in _tools().values())


# --- настройки ---

def test_settings_username_from_link_and_onboarding_line(server):
    assert "ник не указан" in mcp_server.get_settings() and "commishes_username" in mcp_server.get_settings()
    out = _set(commishes_username=f"https://ych.commishes.com/user/{ME}/")
    assert out.startswith("Сохранено: commishes_username")
    cfg = load_config()
    # По умолчанию — и ленты по виду персонажа: YCH там, а не в Adoptables.
    assert cfg.commishes.username == ME and cfg.commishes.categories == ["adopt", "furry", "human"]
    assert cfg.commishes.background and cfg.commishes.market
    line = mcp_server.commishes_line(cfg)
    assert f"ник {ME}" in line and "её аукционы да" in line and "(adopt, furry, human) да" in line
    assert line in mcp_server.get_settings()
    assert "commishes_username" in mcp_server.SETUP_QUESTIONS


def test_settings_reject_bad_values(server):
    assert _set(commishes_username="../admin").startswith("Не сохранила")
    assert _set(commishes_categories=["adopt", "furry", "human", "brony"]).startswith("Не сохранила")
    assert _set(commishes_categories=["featured"]).startswith("Не сохранила")
    assert _set(commishes_background="может").startswith("Не сохранила")
    assert _set(commishes_username=ME, commishes_categories="adopt, furry", commishes_market=False).startswith("Сохранено")
    cfg = load_config()
    assert cfg.commishes.categories == ["adopt", "furry"] and cfg.commishes.market is False
    assert "выборка рынка раз в 6 ч (adopt, furry) нет" in mcp_server.commishes_line(cfg)
    assert config.COMMISHES_USERNAME.pattern == commishes.USERNAME.pattern


def test_hand_edited_settings_are_softened(server):
    path = Path(load_config().path)
    raw = json.loads(path.read_text())
    raw["commishes"] = {"username": "bad name!", "categories": ["adopt", "nope", "furry", "human", "brony"],
                        "background": "yes"}
    path.write_text(json.dumps(raw))
    cfg = load_config()
    assert cfg.commishes.username == "" and cfg.commishes.categories == ["adopt", "furry", "human"]
    assert cfg.commishes.background is True
    assert any("commishes.username" in w for w in cfg.warnings)
    assert any("commishes.categories" in w for w in cfg.warnings)


# --- da_auctions: статус по ссылке, её лоты, сроки ---

def test_status_by_link_of_her_auction(site):
    _set(commishes_username=ME)
    out = _call("da_auctions", {"action": "status", "url": URL})
    assert "не инструкции" in out and "Brindlefox" in out and "$37" in out
    assert "Защита от снайпинга включена" in out and "следующая ставка" in out.lower()
    assert sorted(site.paths()) == ["/auction/show/61A2P/", "/auction/show/61A2P/.json"]  # профиль не нужен
    with _conn() as conn:
        item = db.get_item(conn, "cm:6334553")
        assert item["own"] == 1 and item["source"] == "commishes"
        assert conn.execute("SELECT top_amount FROM auctions WHERE deviation_id = 'cm:6334553'").fetchone()[0] == 37


def test_status_of_someone_elses_auction_is_shown_not_tracked(site):
    _set(commishes_username="her")
    out = mcp_server.da_auctions("status", URL)
    assert "не ты" in out and "Brindlefox" in out
    with _conn() as conn:
        assert db.get_item(conn, "cm:6334553")["own"] == 0
        assert conn.execute("SELECT COUNT(*) FROM auctions").fetchone()[0] == 0  # ники покупателей рынка не храним


def test_status_without_username_asks_for_it(site):
    out = mcp_server.da_auctions("status", URL)
    assert "commishes_username" in out and "Brindlefox" in out


def test_status_bad_link_and_throttle_are_explained(site):
    assert mcp_server.da_auctions("status", "https://ych.commishes.com/user/x/").startswith("Не читаю")
    site.routes["/auction/show/61A2P/.json"] = (429, "", {"Retry-After": "600"})
    out = mcp_server.da_auctions("status", URL)
    assert out.startswith("Сейчас не читаю") and "притормозить" in out
    site.calls.clear()
    assert mcp_server.da_auctions("status", URL).startswith("Сейчас не читаю") and site.calls == []


def test_da_link_still_goes_to_deviantart(site, monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_server, "auction_status", lambda url: seen.append(url) or "da")
    assert mcp_server.da_auctions("status", ia.WORK) == "da" and seen == [ia.WORK]
    assert site.calls == []


def test_mine_shows_commishes_lots_and_deadlines(site):
    _set(commishes_username=ME)
    out = mcp_server.da_auctions("mine")
    assert "ych.commishes ($):" in out and "Loyal Shepherd Dog" in out and "Shy Candy Bear" in out
    assert "защита от снайпинга" in out and "не инструкции" in out
    assert f"/user/{ME}/" in site.paths()
    site.calls.clear()
    again = mcp_server.da_auctions("mine", refresh=False)
    assert site.calls == [] and "Loyal Shepherd Dog" in again  # без refresh — только из базы
    dl = mcp_server.da_auctions("deadlines")
    assert "ych.commishes" in dl and "Loyal Shepherd Dog" in dl and "ставок 3" in dl


def test_mine_without_username_does_not_touch_the_site(site):
    out = mcp_server.da_auctions("mine")
    assert site.calls == [] and "Открытых аукционов у неё не нашла" in out


def test_finished_auction_becomes_a_sale_once(site):
    _set(commishes_username=ME)
    FakeTime.t = ENDS + 120
    site.routes["/auction/show/61A2P/.json"] = json_with(time=ENDS + 120)
    mcp_server.da_auctions("status", URL)
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM sales WHERE platform = 'commishes'").fetchall()
    assert len(rows) == 1 and rows[0]["buyer"] == "Brindlefox" and rows[0]["currency"] == "usd"
    mcp_server.da_auctions("mine")
    with _conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sales WHERE platform = 'commishes'").fetchone()[0] == 1


# --- market:commishes ---

def test_market_commishes_samples_once_and_caches(site):
    out = _call("market", {"action": "commishes"})
    assert site.paths() == ["/category/adopt/index.rss", "/category/furry/index.rss", "/category/human/index.rss"]
    assert "Запросы к ych.commishes за час: 3 из 20" in out
    assert "$" in out and "pts" not in out
    site.calls.clear()
    _call("market", {"action": "commishes"})
    assert site.calls == []  # лента в кэше 30 мин, а 404 у лент furry/human (в подделке их нет) — 6 ч
    with _conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM items WHERE source = 'commishes' AND own = 0").fetchone()[0] == 2


def test_market_commishes_refusals_and_grounding(site):
    with _conn() as conn:
        for i in range(10):
            _lot(conn, i, "ych", "icon", 3 + i % 4)
    out = mcp_server.market_tool("commishes")
    assert "иконки" in out and "n=10" in out and "рынок ych.commishes" in out
    assert "Вывода нет" in out  # адопты из ленты: 2 лота


def test_market_commishes_stops_when_throttled(site):
    site.routes["/category/adopt/index.rss"] = (429, "", {})
    out = mcp_server.market_tool("commishes")
    assert "Новую выборку не взяла" in out and "притормозить" in out


# --- деньги: продажи и подсказка цен YCH ---

def test_sales_record_by_link_and_report_names(site):
    out = _call("sales", {"action": "record", "title": "Shepherd", "amount": 40, "currency": "usd", "url": URL})
    assert out.startswith("Записала продажу")
    with _conn() as conn:
        row = conn.execute("SELECT * FROM sales").fetchone()
    assert (row["platform"], row["item_id"]) == ("commishes", "cm:6334553")
    assert _call("sales", {"action": "record", "title": "Bear", "amount": 12, "platform": "commishes"}).startswith(
        "Записала")
    assert report.PLATFORM_NAMES["commishes"] == "ych.commishes"
    assert "commishes" in sales.PLATFORMS


def _lot(conn, i, kind, sub, sb, top=None, final=False):
    extra = {"kind": kind, "ych_type": sub, "sb": sb, "final": final, "top": top, "n_bids": 1 if top else 0,
             "ends_ts": NOW - i * 600, "price_basis": "json" if final else "sb_text"}
    db.upsert_item(conn, db.Item(id=f"cm:{900 + i}", source="commishes", title=f"lot {i}", url=f"u{i}",
                                 price_usd=sb, lot_type="ych" if kind == "ych" else "auction", extra=extra),
                   now=NOW - i * 600)


def test_ych_offer_gets_a_grounded_price_hint(site):
    with _conn() as conn:
        for i in range(10):
            _lot(conn, i, "ych", "icon", 4 + i % 3)
    sb_only = _call("sales", {"action": "ych_offer", "format": "ych", "title": "YCH icon", "amount": 20,
                              "currency": "usd", "slots": 3})
    # Итогов нет — только SB: её цену со стартовыми ставками в ряд не ставим.
    assert "ych.commishes, YCH — иконки: SB" in sb_only and "выше, чем у" not in sb_only
    assert "с SB не сравниваю" in sb_only
    with _conn() as conn:
        for i in range(10, 20):
            _lot(conn, i, "ych", "icon", 3, top=4 + i % 3, final=True)
    out = _call("sales", {"action": "ych_offer", "format": "ych", "title": "YCH icon 2", "amount": 20,
                          "currency": "usd", "slots": 3})
    assert out.startswith("Сохранила") and "ych.commishes, YCH — иконки" in out
    # Сравнение — с разбросом итогов (все 10 иконок ушли за $4–6, дешевле $20), а не с интервалом медианы.
    assert "n=10" in out and "рынок ych.commishes" in out and "выше, чем у 100% лотов там" in out
    assert "80% для медианы" in out and "половина лотов — $" in out
    rub = _call("sales", {"action": "ych_offer", "format": "ych", "title": "YCH icon", "amount": 900,
                          "currency": "rub"})
    assert "с $ не сравниваю" in rub
    plain = _call("sales", {"action": "ych_offer", "format": "commission", "title": "Fullbody", "amount": 50})
    assert "ych.commishes" not in plain  # коммишену подсказка YCH не нужна


def test_ych_offer_without_market_data_has_no_hint(site):
    out = _call("sales", {"action": "ych_offer", "format": "ych", "title": "YCH halfbody", "amount": 15})
    assert out.startswith("Сохранила") and "ych.commishes" not in out and site.calls == []


def test_commissions_accept_the_platform():
    assert commissions._platform("ych.commishes") == "commishes"
    assert commissions._platform("commishes") == "commishes"


# --- promo:checklist и advice:plan ---

def test_checklist_da_post_with_commishes_link(server):
    text = f"Adopt! SB $10 MI $5 AB $60\n{URL}"
    out = _call("promo", {"action": "checklist", "text": text, "variant": "auction"})
    assert "где принимаются ставки" in out and "Bids only on ych.commishes: https://ych.commishes.com/auction/show/61A2P/" in out
    ok = _call("promo", {"action": "checklist", "text": text + "\nBids only on ych.commishes!", "variant": "auction"})
    assert "можно вставить: Bids only" not in ok


def test_checklist_commishes_listing(server):
    out = _call("promo", {"action": "checklist", "variant": "commishes",
                          "text": "Loyal dog adopt\nSB: $10\nMI: $5\nPayPal only"})
    assert "PayPal" in out and "AI" in out and "USD" in out
    good = _call("promo", {"action": "checklist", "variant": "commishes",
                           "text": "Dog adopt\nSB: $10\nMI: $5\nAll prices in USD. Payment: Boosty. Hand-drawn, no AI."})
    assert "можно вставить: SB" not in good and "PayPal не обещан" in good


def test_plan_for_commishes_is_text_only(site):
    out = _call("advice", {"action": "plan", "format": "commishes", "species": "ych", "style": "icon"})
    assert out.startswith("План аукциона на ych.commishes (YCH)") and "Защита от снайпинга" in out
    assert "отправляешь ты" in out and site.calls == []
    adopt = mcp_server.plan_new_adopt("dog", "", "commishes")
    assert "(адопты)" in adopt


# --- forecast:health ---

def test_health_shows_commishes_only_when_used(site):
    plain = mcp_server.data_health()
    assert "ych.commishes" not in plain
    _set(commishes_username=ME)
    mcp_server.da_auctions("mine")
    out = mcp_server.data_health()
    assert "её аукционы ych.commishes" in out and "Запросы к ych.commishes за час" in out


# --- фон ---

def test_background_needs_her_username(site):
    lines, events = monitor.commishes_pass(None, load_config(), NOW, client=object())
    assert (lines, events) == ([], []) and site.calls == []


def test_background_tracks_hers_and_samples_market_rarely(site):
    _set(commishes_username=ME)
    cfg = load_config()
    with _conn() as conn, commishes.client_for(conn) as client:
        lines, events = monitor.commishes_pass(conn, cfg, NOW, client=client)
        paths = site.paths()
        assert f"/user/{ME}/" in paths and "/category/adopt/index.rss" in paths
        assert events == []  # первое чтение — точка отсчёта
        assert db.last_run(conn, monitor.CM_MARKET_SOURCE) and db.last_run(conn, commishes.PRUNE_SOURCE)
        site.calls.clear()
        FakeTime.t = NOW + 1900
        site.routes[f"/user/{ME}/"] = PROFILE.replace('bid="37"', 'bid="45"')
        site.routes["/auction/show/61A2P/.json"] = json_with(time=NOW + 1900,
                                                            bids=[bid("Newfan", 45, NOW + 1800)] + json.loads(
                                                                JSON_61A2P)["payload"]["slots"]["6334553"]["bids"])
        lines, events = monitor.commishes_pass(conn, cfg, NOW + 1900, client=client)
        assert [(e.amount, e.user) for e in events] == [(45, "Newfan")]
        assert "/category/adopt/index.rss" not in site.paths()  # рынок — не чаще раза в 6 ч
        assert any("Newfan" in line for line in lines)


def test_background_market_off_and_background_off(site):
    _set(commishes_username=ME, commishes_market=False)
    with _conn() as conn, commishes.client_for(conn) as client:
        monitor.commishes_pass(conn, load_config(), NOW, client=client)
        assert "/category/adopt/index.rss" not in site.paths() and f"/user/{ME}/" in site.paths()
    site.calls.clear()
    _set(commishes_background=False)
    with _conn() as conn, commishes.client_for(conn) as client:
        assert monitor.commishes_pass(conn, load_config(), NOW + 60, client=client) == ([], [])
        assert site.calls == []


def test_background_survives_site_errors(site):
    _set(commishes_username=ME)
    for path in list(site.routes):
        site.routes[path] = (503, "", {})
    with _conn() as conn, commishes.client_for(conn) as client:
        lines, events = monitor.commishes_pass(conn, load_config(), NOW, client=client)
    assert events == [] and any("притормозить" in line for line in lines)


def test_cli_hook_notifies_new_bids(server, monkeypatch):
    sent = []
    monkeypatch.setattr(monitor, "commishes_pass", lambda conn, cfg, tick, **kw: (["ych: ставка"], ["событие"]))
    monkeypatch.setattr(digest, "notify_bids", lambda cfg, events: sent.append(events))
    with _conn() as conn:
        assert cli._commishes(conn, load_config(), None) == ["ych: ставка"]
    assert sent == [["событие"]]
    monkeypatch.setattr(monitor, "commishes_pass", lambda conn, cfg, tick, **kw: ([], []))
    with _conn() as conn:
        cli._commishes(conn, load_config(), None)
    assert sent == [["событие"]]  # без событий — без уведомления


def test_tests_never_reach_the_real_site():
    with pytest.raises(AssertionError):
        commishes.make_http()


def test_settings_survive_save_roundtrip(server):
    cfg = load_config()
    cfg.commishes.username = ME
    cfg.commishes.categories = ["furry"]
    save_config(cfg)
    again = load_config()
    assert again.commishes.username == ME and again.commishes.categories == ["furry"] and not again.warnings


def test_page_fixture_for_ych_is_known():
    page = commishes.parse_auction_page(PAGE_YCH)
    assert page.anti_snipe is False


# --- 0.5.1: после ревью ---

def test_market_commishes_ych_question_reads_ych_feeds(site):
    _set(commishes_categories=["adopt"])
    out = _call("market", {"action": "commishes", "keywords": ["ych", "icon"]})
    assert "/category/furry/index.rss" in site.paths()       # разово — лента YCH, раз в настройках её нет
    assert "commishes_categories" in out and "YCH в выборке" in out
    assert "адопты:" not in out                              # спросила про YCH — адопты не показываем
    assert len(set(site.paths())) <= 3


def test_mine_without_username_points_to_commishes_setting(site):
    out = mcp_server.da_auctions("mine")
    assert "commishes_username" in out and "da_auctions:status url=" in out and site.calls == []


def test_checklist_by_link_of_her_commishes_auction(site):
    _set(commishes_username=ME)
    mcp_server.da_auctions("status", URL)
    out = _call("promo", {"action": "checklist", "variant": "commishes", "text": URL})
    assert "этой работы нет в базе" not in out and "SB" in out
    other = _call("promo", {"action": "checklist", "variant": "commishes",
                            "text": "https://ych.commishes.com/auction/show/618QA/"})
    assert "вставь текст описания" in other and "advice:review" not in other


def test_background_reports_profile_error_once_and_market_feed_errors(site):
    _set(commishes_username="Typo_name", commishes_categories=["furry", "adopt"])
    cfg = load_config()
    told = []
    with _conn() as conn, commishes.client_for(conn) as client:
        for k in range(4):
            problems: list[str] = []
            monitor.commishes_pass(conn, cfg, NOW + k * 900, client=client, problems=problems)
            told += problems
        assert len(told) == 1 and "commishes_username" in told[0]
        assert "/category/adopt/index.rss" in site.paths()   # 404 у furry не остановил adopt
        assert db.last_run(conn, monitor.CM_MARKET_SOURCE)
        assert site.paths().count("/category/furry/index.rss") == 1


def test_cli_sends_one_time_problems(server, monkeypatch):
    sent = []

    def fake_pass(conn, cfg, tick, problems=None):
        problems.append("ych.commishes: профиль не открылся")
        return [], []

    monkeypatch.setattr(monitor, "commishes_pass", fake_pass)
    monkeypatch.setattr(digest, "notify_lines", lambda cfg, title, lines: sent.append(lines) or True)
    with _conn() as conn:
        assert cli._commishes(conn, load_config(), None) == ["ych.commishes: профиль не открылся"]
    assert sent == [["ych.commishes: профиль не открылся"]]


def test_chat_prunes_even_without_username(site):
    with _conn() as conn:
        commishes._ensure_cache(conn)
        conn.execute("INSERT INTO cm_cache VALUES ('/auction/show/X/.json', ?, '{\"bidder\": 1}')", (NOW - 3600,))
        conn.commit()
    mcp_server.market_tool("commishes")
    with _conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cm_cache WHERE path LIKE '%.json'").fetchone()[0] == 0
        assert db.last_run(conn, commishes.PRUNE_SOURCE)


def test_rules_answer_when_to_end_and_where_ych_live():
    rule = mcp_server.COMMISHES_RULE
    assert "forecast:best_time — только DA" in rule and "furry/human" in rule and "lot" in rule
    assert "furry" in mcp_server.SETUP_QUESTIONS.split("7.", 1)[1].split("8.", 1)[0]


# --- 0.5.1: исправления после проверки ---

def test_update_settings_during_setup_is_not_clipped(server):
    from mcp_devian.config import Config

    save_config(Config())                                   # настройка не пройдена — в ответе вопросы
    for changes in ({"timezone": "Europe/Moscow"}, {"my_da_username": "testartist"}):
        out = _call("update_settings", {"changes": changes})
        assert out.startswith("Сохранено") and "строк не показано" not in out
        assert "8. По желанию" in out and "finish_setup=True" in out


def test_commishes_checklist_does_not_count_unverified_payments(server):
    out = _call("promo", {"action": "checklist", "variant": "commishes", "text": "Cute dog adopt, starting soon"})
    assert "на месте 1 из 6" in out and "Hipolink" not in out and ".." not in out
    hip = _call("promo", {"action": "checklist", "variant": "commishes",
                          "text": "Dog adopt\nSB: $10\nPayment: Hipolink"})
    have = next(ln for ln in hip.splitlines() if ln.startswith("Есть:"))
    assert "не проверено" not in have and "Hipolink — не проверено" in hip


def test_mine_with_username_and_only_ended_auction_talks_about_commishes(site):
    _set(commishes_username=ME)
    with _conn() as conn:
        db.upsert_item(conn, db.Item(id="cm:6334553", source="commishes", title="Dog", url=URL, own=True,
                                     status="closed", extra={"code": "61A2P", "final": True}), now=NOW)
    out = mcp_server.da_auctions("mine", refresh=False)
    assert "ych.commishes: открытых аукционов не вижу" in out and "Проверь ник на DA" not in out
    assert "На DA открытых аукционов у неё нет" in out and site.calls == []


def test_status_names_only_the_missing_background_piece(site):
    _set(commishes_username=ME)
    FakeTime.t = NOW
    out = mcp_server.da_auctions("status", URL)
    assert "не хватает включённой фоновой проверки" in out and "ника commishes_username" not in out


def test_trimmed_docs_still_name_every_parameter():
    tools = _tools()
    assert "from_folder" in tools["prepare_gallery"].description
    assert "currency" in tools["images"].description and "theme" in tools["images"].description
    assert "lot_type" in tools["market"].description and "my_price_" in tools["market"].description
