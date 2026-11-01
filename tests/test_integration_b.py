"""Подключение B: фоновые крючки (аукционы Toyhouse, уведомления продавца, пинглист, ставки TH в уведомлениях),
настройки водяного знака и лимитов комментариев, продажа по треду Toyhouse и инструменты рынка/аналитики.

Всё на подделках: ни Chrome, ни DeviantArt, ни сети, ни настоящих уведомлений."""

import argparse
import contextlib
import json
import time
from types import SimpleNamespace

import pytest
import test_integration_a as ia
from test_integration_a import ME, THREAD, WORK, own_work

from mcp_devian import (
    advisor,
    auctions,
    cli,
    da_market,
    db,
    digest,
    her_chrome,
    jobs,
    mcp_server,
    monitor,
    pinglist,
    posting,
    sales,
    selling,
    updates,
)
from mcp_devian import deviantart as da
from mcp_devian import th_auctions as ta
from mcp_devian import toyhouse as th
from mcp_devian.config import Config, load_config, save_config
from mcp_devian.db import Item

fake, server = ia.fake, ia.server  # общие подделки DA и сервера из test_integration_a
THREAD2 = "https://toyhou.se/~forums/13.adoption-center/124.moth-auction"


def page_json(tid="123", posts=None):
    posts = posts or [(ME, "Lumi auction! SB $10, MI $1, AB $60", "5 hours ago"),
                      ("bob", "$12", "3 hours ago"), ("carol", "$15", "1 hour ago")]
    return json.dumps({"state": "ready", "thread_id": tid, "page": 1, "pages_total": 1,
                       "posts": [{"post_id": f"post-{tid}-{i}", "author": a, "text": t, "when": w}
                                 for i, (a, t, w) in enumerate(posts, 1)]})


class FakeReader:
    """Окно бота: get() запоминает адрес, evaluate() отдаёт страницу; fail — {адрес треда: исключение}."""

    def __init__(self, fail=None):
        self.urls: list[str] = []
        self.tried: list[str] = []
        self.fail = fail or {}
        self.page = SimpleNamespace(evaluate=lambda js: page_json(self.urls[-1].split("/")[-1].split(".")[0]))

    def get(self, url, selector):
        self.tried.append(url)
        for prefix, exc in self.fail.items():
            if url.startswith(prefix):
                raise exc
        self.urls.append(url)


@pytest.fixture
def notices(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(digest, "notify_macos", lambda title, msg: sent.append((title, msg)) or True)
    return sent


@pytest.fixture
def bot(monkeypatch):
    reader = FakeReader()

    @contextlib.contextmanager
    def fake_bot(cfg, interactive, browser):
        yield reader

    monkeypatch.setattr(monitor, "_bot_reader", fake_bot)
    monkeypatch.setattr(th, "human_pause", lambda *a, **k: None)
    return reader


def bot_cfg() -> Config:
    cfg = load_config()
    cfg.toyhouse.mode = "bot"
    return cfg


# --- monitor.refresh_th_auctions ---

def test_th_auctions_bot_reads_saves_and_reports_new_bid(server, bot):
    with db.session() as c:
        ta.watch(c, THREAD, own=True)
        lines, events = monitor.refresh_th_auctions(c, bot_cfg())
        row = c.execute("SELECT top_user, top_amount FROM th_auctions WHERE thread_id = '123'").fetchone()
        assert db.last_run(c, monitor.TH_AUCTIONS_SOURCE)["last_error"] is None
        # Второй раз сразу — рано: в фоне не чаще раза в полчаса.
        assert monitor.refresh_th_auctions(c, bot_cfg()) == ([], [])
    assert bot.urls and bot.urls[0].startswith(THREAD)
    assert (row["top_user"], row["top_amount"]) == ("carol", 15)
    assert len(events) == 1 and isinstance(events[0], ta.ThBidEvent)
    assert lines[0] == "аукционы Toyhouse: перечитала тредов 1"
    assert any("Toyhouse: новая ставка" in line for line in lines)


def test_th_auctions_slot_time_does_not_drop_fresh_bids(server, bot):
    """Плановая проверка передаёт время слота (минуты назад): ставка «2 minutes ago» всё равно считается."""
    posts = [(ME, "Lumi auction! SB $10, MI $1, AB $60", "5 hours ago"), ("bob", "$12", "3 hours ago"),
             ("carol", "$15", "2 minutes ago")]
    bot.page.evaluate = lambda js: page_json(posts=posts)
    with db.session() as c:
        ta.watch(c, THREAD, own=True)
        monitor.refresh_th_auctions(c, bot_cfg(), now=time.time() - 360)
        row = c.execute("SELECT top_user, top_amount FROM th_auctions WHERE thread_id = '123'").fetchone()
    assert (row["top_user"], row["top_amount"]) == ("carol", 15)


def test_th_auctions_someone_elses_thread_is_data_only(server, bot):
    with db.session() as c:
        ta.watch(c, THREAD, own=False)
        lines, events = monitor.refresh_th_auctions(c, bot_cfg())
    assert events == [] and lines == ["аукционы Toyhouse: перечитала тредов 1"]


def test_th_auctions_nothing_to_read_opens_no_browser(server, monkeypatch):
    @contextlib.contextmanager
    def refuse(*a, **k):
        raise AssertionError("окно бота открылось без нужды")
        yield

    monkeypatch.setattr(monitor, "_bot_reader", refuse)
    with db.session() as c:
        assert monitor.refresh_th_auctions(c, bot_cfg()) == ([], [])


def test_th_auctions_challenge_stops(server, bot):
    challenge = th.ChallengeError("Toyhouse просит галочку")
    bot.fail = {THREAD: challenge, THREAD2: challenge}
    with db.session() as c:
        ta.watch(c, THREAD, own=True)
        ta.watch(c, THREAD2, own=True)
        lines, events = monitor.refresh_th_auctions(c, bot_cfg())
        assert "галочк" in db.last_run(c, monitor.TH_AUCTIONS_SOURCE)["last_error"]
    assert len(bot.tried) == 1 and events == []  # после галочки дальше не стучимся
    assert lines == ["аукционы Toyhouse: Toyhouse просит галочку — остальные треды в следующий раз"]


def test_th_auctions_challenge_stops_even_with_cache(server, bot):
    """Треды уже читались (есть кэш): Cloudflare всё равно останавливает проход и пишется ошибкой."""
    with db.session() as c:
        ta.watch(c, THREAD, own=True)
        ta.watch(c, THREAD2, own=True)
        monitor.refresh_th_auctions(c, bot_cfg())
        assert len(bot.tried) == 2
        bot.tried.clear()
        challenge = th.ChallengeError("Toyhouse просит галочку")
        bot.fail = {THREAD: challenge, THREAD2: challenge}
        lines, events = monitor.refresh_th_auctions(c, bot_cfg(), min_interval_s=0)
        assert "галочк" in db.last_run(c, monitor.TH_AUCTIONS_SOURCE)["last_error"]
    assert len(bot.tried) == 1 and events == []
    assert lines == ["аукционы Toyhouse: Toyhouse просит галочку — остальные треды в следующий раз"]


def test_th_auctions_challenge_on_later_page_keeps_read_posts(tmp_path):
    """Вторая страница упёрлась в Cloudflare: первая сохранена, а проход останавливается."""
    c = db.connect(tmp_path / "pages.sqlite3")
    first = json.loads(page_json())
    first["pages_total"] = 2

    def fetch(url):
        if "page=2" in url:
            raise th.ChallengeError("галочка")
        return json.dumps(first)

    with pytest.raises(th.ChallengeError):
        ta.read_thread(fetch, THREAD, conn=c, pause=lambda: None)
    assert c.execute("SELECT COUNT(*) FROM th_posts WHERE thread_id = '123'").fetchone()[0] == 3


def test_th_auctions_chrome_busy_is_not_a_run(server, monkeypatch):
    """Режим chrome: её Chrome занят — проход не записывается, через минуту можно снова."""
    def busy(*a, **k):
        raise th.BrowserBusy("Chrome занят проверкой")

    monkeypatch.setattr(ta, "run_th_thread", busy)
    with db.session() as c:
        ta.watch(c, THREAD, own=True)
        lines, _ = monitor.refresh_th_auctions(c, load_config(), interactive=True)
        assert db.last_run(c, monitor.TH_AUCTIONS_SOURCE) is None
    assert "занят" in lines[0]


def test_th_auctions_thread_error_moves_on(server, bot):
    bot.fail = {THREAD: th.ToyhouseError("страница не загрузилась")}
    with db.session() as c:
        ta.watch(c, THREAD, own=True)
        ta.watch(c, THREAD2, own=True)
        lines, _ = monitor.refresh_th_auctions(c, bot_cfg())
    assert any("не прочитался (страница не загрузилась)" in line for line in lines)
    assert [u for u in bot.urls if u.startswith(THREAD2)] and lines[0].endswith("тредов 1")


def test_th_auctions_time_budget(server, bot):
    with db.session() as c:
        ta.watch(c, THREAD, own=True)
        ticks = iter([0.0, 999.0, 999.0])
        lines, _ = monitor.refresh_th_auctions(c, bot_cfg(), time_budget_s=10, clock=lambda: next(ticks))
    assert bot.urls == [] and any("не все треды" in line for line in lines)


def test_th_auctions_chrome_only_when_she_asked(server, monkeypatch):
    calls = []
    monkeypatch.setattr(her_chrome, "run_in_chrome", lambda url, js, ready, **kw: calls.append(url) or page_json())
    monkeypatch.setattr(th, "human_pause", lambda *a, **k: None)
    cfg = load_config()
    assert cfg.toyhouse.mode == "chrome"
    with db.session() as c:
        ta.watch(c, THREAD, own=True)
        assert monitor.refresh_th_auctions(c, cfg, interactive=False) == ([], [])
        assert calls == []
        lines, events = monitor.refresh_th_auctions(c, cfg, interactive=True)
    assert calls and len(events) == 1 and lines[0].startswith("аукционы Toyhouse: перечитала")


def test_th_auctions_browser_busy_retries_next_time(server, monkeypatch):
    @contextlib.contextmanager
    def busy(*a, **k):
        raise th.BrowserBusy("профиль занят")
        yield

    monkeypatch.setattr(monitor, "_bot_reader", busy)
    with db.session() as c:
        ta.watch(c, THREAD, own=True)
        lines, events = monitor.refresh_th_auctions(c, bot_cfg())
        assert db.last_run(c, monitor.TH_AUCTIONS_SOURCE) is None  # не записали — повторим на следующей проверке
    assert events == [] and "занят" in lines[0]


# --- monitor.seller_pass: дополнения ---

def _ended_th_auction(c, thread_id="123"):
    ta.ensure(c)
    c.execute("INSERT INTO th_auctions (thread_id, lot, url, title, owner, own, currency, top_amount, top_user, "
              "n_bids, ended, winner, first_seen_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (thread_id, "", THREAD, "Lumi", ME, 1, "usd", 15, "carol", 2, 1, "carol", time.time(), time.time()))
    c.commit()


def test_seller_pass_records_toyhouse_winners(server, notices):
    with db.session() as c:
        _ended_th_auction(c)
        lines = monitor.seller_pass(c, Config(), enrich=False)
        rows = sales.list_sales(c)
        assert monitor.seller_pass(c, Config(), enrich=False) == []  # второй раз — без дубля
    assert "продажи: записала победителей аукционов Toyhouse — 1" in lines
    assert [(r["item_id"], r["platform"], r["buyer"]) for r in rows] == [("th:123", "toyhouse", "carol")]


def test_seller_pass_deadline_notices(server, notices, monkeypatch):
    seen = []

    def soon(conn, now=None, **kw):
        seen.append(("soon", kw))
        return [{"text": "«Moth» закончится через 2 ч: ставок 3"}]

    def ended(conn, now=None, **kw):
        seen.append(("ended", kw))
        return [{"text": "«Lumi» закончился 1 ч назад: ставок 2. Закрыть одной командой?"}]

    monkeypatch.setattr(selling, "ending_soon", soon)
    monkeypatch.setattr(selling, "ended_unclosed", ended)
    with db.session() as c:
        monitor.seller_pass(c, Config(), enrich=False)
    assert [k for k, _ in seen] == ["soon", "ended"]
    assert all(kw["unseen_only"] and kw["limit"] == 3 for _, kw in seen)
    assert seen[0][1]["within_h"] == 3
    assert ("mcp_devian", "«Moth» закончится через 2 ч: ставок 3") in notices
    assert any("Закрыть одной командой" in m for _, m in notices)


def test_seller_pass_unpaid_nudge_once_a_day(server, notices, monkeypatch):
    monkeypatch.setattr(sales, "unpaid", lambda conn, days=3, now=None: [{}, {}, {}])
    now = time.time()
    with db.session() as c:
        monitor.seller_pass(c, Config(), now, enrich=False)
        monitor.seller_pass(c, Config(), now + 3600, enrich=False)
        assert [m for _, m in notices if "Не оплачено" in m] == ["Не оплачено: 3 — спроси Claude"]
        monitor.seller_pass(c, Config(), now + 86400 + 60, enrich=False)
    assert len([m for _, m in notices if "Не оплачено" in m]) == 2


def test_seller_pass_unpaid_nudge_counts_toyhouse_and_unchecked(server, notices):
    """3 продажи с Toyhouse и одна с DA без суммы, 10 дней без оплаты: «Не оплачено: 4», а не 0."""
    now = time.time()
    with db.session() as c:
        for i in range(3):
            sales.add_sale(c, title=f"TH {i}", amount=20, currency="usd", platform="toyhouse", format="auction",
                           buyer="anna", now=now - 10 * 86400)
        sales.add_sale(c, title="DA", amount=None, currency=None, platform="deviantart", format="auction",
                       buyer="bob", now=now - 10 * 86400)
        monitor.seller_pass(c, Config(), now, enrich=False)
    assert [m for _, m in notices if "Не оплачено" in m] == ["Не оплачено: 4 — спроси Claude"]


def test_seller_pass_update_notice(server, notices, monkeypatch):
    monkeypatch.setattr(updates, "notice_to_announce", lambda short=False, **kw: "Есть обновление 0.5.0" if short else None)
    with db.session() as c:
        monitor.seller_pass(c, Config(), enrich=False)
    assert ("mcp_devian", "Есть обновление 0.5.0") in notices


def test_seller_pass_hooks_fail_independently(server, notices, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(ta, "sync_sales", boom)
    monkeypatch.setattr(selling, "ending_soon", boom)
    monkeypatch.setattr(sales, "unpaid", lambda conn, days=3, now=None: [{}])
    monkeypatch.setattr(updates, "notice_to_announce", lambda **kw: "Есть обновление 0.5.0")
    with db.session() as c:
        assert monitor.seller_pass(c, Config(), enrich=False) == []
    assert [m for _, m in notices] == ["Не оплачено: 1 — спроси Claude", "Есть обновление 0.5.0"]


def test_seller_pass_without_macos_marks_nothing(server, notices, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("без уведомлений не помечаем лоты показанными и не считаем неоплаченное")

    for name in ("ending_soon", "ended_unclosed", "payment_reminders"):
        monkeypatch.setattr(selling, name, refuse)
    monkeypatch.setattr(updates, "notice_to_announce", refuse)
    cfg = Config()
    cfg.notify.macos = False
    with db.session() as c:
        assert monitor.seller_pass(c, cfg, enrich=False) == []
    assert notices == []


# --- пинглист из комментариев под её работами ---

PING_ME = {"comment_id": "c1", "user": "fan1", "text": "ping me please!", "posted_ts": time.time() - 60}


def test_auction_status_fills_pinglist_from_her_work(server, fake, monkeypatch):
    monkeypatch.setattr(fake, "deviation_comments", lambda uuid: [PING_ME])
    with db.session() as c:
        own_work(c)
    mcp_server.auction_status(WORK)
    with db.session() as c:
        assert [m["user"] for m in pinglist.members(c)] == ["fan1"]


def test_refresh_via_api_fills_pinglist_only_for_her_lots(server, fake, monkeypatch):
    monkeypatch.setattr(fake, "deviation_comments", lambda uuid: [PING_ME])
    monkeypatch.setattr(auctions, "folder_pass", lambda *a, **k: {})
    with db.session() as c:
        own_work(c)
        db.upsert_item(c, Item(id="da:6", source="deviantart", title="Fox", url="https://www.deviantart.com/x/art/Fox-6",
                               author="x", lot_type="auction", own=False, published_ts=time.time() - 3 * 86400))
        theirs = db.get_item(c, "da:6")
        monkeypatch.setattr(auctions, "to_refresh", lambda *a, **k: [theirs])
        auctions.refresh_via_api(c, load_config(), fake, sleep=lambda s: None)
        assert pinglist.members(c) == []
        monkeypatch.setattr(auctions, "to_refresh", lambda *a, **k: [db.get_item(c, "da:5")])
        auctions.refresh_via_api(c, load_config(), fake, sleep=lambda s: None)
        assert [m["user"] for m in pinglist.members(c)] == ["fan1"]


def test_pinglist_ingest_failure_does_not_stop_bids(server, fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(pinglist, "ingest", boom)
    monkeypatch.setattr(fake, "deviation_comments", lambda uuid: [PING_ME])
    with db.session() as c:
        own_work(c)
    assert "Прочитано комментариев: 1" in mcp_server.auction_status(WORK)


# --- digest.notify_bids: ставки Toyhouse ---

def test_notify_bids_describes_toyhouse_events(notices):
    events = [ta.ThBidEvent("123", "2", "Lumi auction", THREAD, 15, "usd", "carol")]
    digest.notify_bids(Config(), events)
    assert len(notices) == 1 and "Toyhouse: новая ставка" in notices[0][1] and "лот #2" in notices[0][1]


# --- настройки: водяной знак и лимиты комментариев ---

def test_update_settings_watermark_and_comment_caps(server):
    out = mcp_server.update_settings(watermark_text="hermark", watermark_position="center", watermark_opacity=50,
                                     da_comments_per_day=12, da_pings_per_day=4)
    assert out.startswith("Сохранено: watermark_text, watermark_position, watermark_opacity, da_comments_per_day")
    p = load_config().posting
    assert (p.watermark_text, p.watermark_position, p.watermark_opacity) == ("hermark", "center", 50)
    assert (p.da_comments_per_day, p.da_pings_per_day) == (12, 4)
    with db.session() as c:
        assert posting.comment_quota(c, load_config()) == (12, 4)


@pytest.mark.parametrize("kwargs", [{"da_comments_per_day": 50}, {"da_pings_per_day": 11},
                                    {"watermark_position": "middle"}, {"watermark_opacity": 300},
                                    {"watermark_logo": "~/Pictures/logo.jpg"}])
def test_update_settings_refuses_bad_values(server, kwargs):
    assert mcp_server.update_settings(**kwargs).startswith("Не сохранила")


def test_hand_edited_caps_are_clamped(server):
    path = server / "settings.json"
    raw = json.loads(path.read_text())
    raw["posting"].update(da_comments_per_day=100, da_pings_per_day="x", watermark_position="nowhere")
    path.write_text(json.dumps(raw))
    cfg = load_config()
    assert (cfg.posting.da_comments_per_day, cfg.posting.da_pings_per_day) == (20, 10)
    assert cfg.posting.watermark_position == "tiled"
    assert any("da_comments_per_day" in w for w in cfg.warnings)
    cfg.posting.da_comments_per_day = 500  # даже в обход проверки потолок не поднять
    with db.session() as c:
        assert posting.comment_quota(c, cfg) == (20, 10)


# --- продажа по треду Toyhouse ---

def test_record_sale_by_toyhouse_thread(server):
    out = mcp_server.record_sale("Lumi", 15, url=THREAD, platform="deviantart", buyer="carol")
    assert out.startswith("Записала продажу")
    with db.session() as c:
        row = sales.list_sales(c)[0]
    assert (row["item_id"], row["platform"]) == ("th:123", "toyhouse")


def test_record_sale_thread_with_lots_needs_lot(server):
    with db.session() as c:
        ta.ensure(c)
        for lot in ("1", "2"):
            c.execute("INSERT INTO th_auctions (thread_id, lot, url, own, first_seen_ts, updated_ts) "
                      "VALUES ('123', ?, ?, 1, 0, 0)", (lot, THREAD))
        c.commit()
    assert "укажи номер лота" in mcp_server.record_sale("Lumi", 15, url=THREAD)
    assert mcp_server.record_sale("Lumi #2", 15, url=THREAD, lot="#2").startswith("Записала")
    with db.session() as c:
        assert [r["item_id"] for r in sales.list_sales(c)] == ["th:123#2"]


def test_record_sale_merges_with_auto_toyhouse_winner(server, notices):
    with db.session() as c:
        _ended_th_auction(c)
        ta.sync_sales(c)
    out = mcp_server.record_sale("Lumi", 14, url=THREAD, buyer="carol")
    assert "уже записала сама" in out
    with db.session() as c:
        rows = sales.list_sales(c)
    assert len(rows) == 1 and rows[0]["amount"] == 14


# --- новые инструменты рынка и аналитики ---

def test_market_search_tool(server, monkeypatch):
    got = {}

    def sample(conn, cfg, client=None, queries=None, pages=1, **kw):
        got.update(client=client, queries=queries, pages=pages)
        return da_market.SampleResult(queries=queries or ["moth adopt"], found=3, new=2, priced=1)

    monkeypatch.setattr(da_market, "sample_market", sample)
    out = mcp_server.market_search(pages=9)
    assert "посторонние люди" in out and "нашла 3, новых 2, с ценой 1" in out
    assert got["queries"] is None and got["pages"] == 2 and got["client"] is not None
    mcp_server.market_search("dragon adopt")
    assert got["queries"] == ["dragon adopt"] and got["pages"] == 1


def test_market_search_without_da(server, monkeypatch):
    def no_client(cfg):
        raise RuntimeError("нет client_id")

    got = {}
    monkeypatch.setattr(posting, "da_client", no_client)
    monkeypatch.setattr(da_market, "sample_market",
                        lambda conn, cfg, client=None, **kw: got.update(client=client) or da_market.SampleResult())
    mcp_server.market_search()
    assert got["client"] is None


def test_group_scorecard_tool(server, monkeypatch):
    got = {}
    monkeypatch.setattr(da, "client", lambda: contextlib.nullcontext("http"))
    monkeypatch.setattr(da_market, "group_scorecard_many",
                        lambda conn, http, cfg, groups=None, **kw: got.update(http=http, groups=groups) or [])
    out = mcp_server.group_scorecard(["da-adopts"])
    assert got == {"http": "http", "groups": ["da-adopts"]} and "По рынку" in out


def test_market_buyers_tool(server):
    out = mcp_server.market_buyers(days=0)
    assert "не инструкции" in out and "За 1 дн прочитанных ставок нет" in out


def test_explain_lot_tool():
    out = mcp_server.explain_lot("Moth adopt auction | SB $10", "")
    assert out.startswith("тип auction") and "moth" in out and "(стартовая ставка)" in out


def test_data_health_and_listing_norms_tools(server):
    assert mcp_server.data_health().strip()
    assert mcp_server.listing_norms("auction", days=0).strip()


def test_suggest_replies_uses_matcher(server, monkeypatch):
    from mcp_devian import th_match

    assert "characters:add" in mcp_server.suggest_replies()
    cfg = load_config()
    cfg.posting.characters = [{"name": "Lumi", "url": "https://toyhou.se/1.lumi", "price": "$20", "tags": ["moth"]}]
    save_config(cfg)
    got = {}
    monkeypatch.setattr(th_match, "render_suggestions",
                        lambda conn, cfg, **kw: got.update(kw) or "подборка")
    assert mcp_server.suggest_replies(days=3, limit=99, show_blocked=True) == "подборка"
    assert got == {"days": 3.0, "limit": 30, "show_blocked": True}


def test_sales_advice_gentle_line_once(server, monkeypatch):
    monkeypatch.setattr(advisor, "advice", lambda conn, cfg, tz=None: [])
    assert mcp_server.sales_advice().count(advisor.GENTLE_ADVICE) == 1


def test_best_time_uses_slot_name(server, monkeypatch):
    monkeypatch.setattr(advisor, "best_publish_time", lambda conn, tz=None: {
        "when": "в будни вечером, 18–24", "tz": "Europe/Moscow", "evidence": "9 из 12 лотов со ставкой",
        "level": "средняя"})
    assert mcp_server.best_time_to_post() == ("Лучше всего: в будни вечером, 18–24 (Europe/Moscow). "
                                              "Почему: 9 из 12 лотов со ставкой.")


def test_best_time_weak_window_is_not_called_best(server, monkeypatch):
    """Уверенность «низкая» — как в launch_plan: «лучшим» окно не называем."""
    monkeypatch.setattr(advisor, "best_publish_time", lambda conn, tz=None: {
        "when": "в будни вечером, 18–24", "tz": "Europe/Moscow", "evidence": "7 из 9 лотов со ставкой",
        "level": "низкая"})
    out = mcp_server.best_time_to_post()
    assert "Лучше всего" not in out and "Лучшего времени по данным пока не видно" in out and "«низкая»" in out


def test_what_sells_shows_toyhouse_column(server, monkeypatch):
    monkeypatch.setattr(advisor, "demand_by_tag", lambda conn, days=30: [
        {"tag": "moth", "lots": 4, "avg_bids": 1.5, "bids_n": 4, "avg_favs": 10, "favs_n": 4,
         "th_threads": 3, "th_period": "14 дн"},
        {"tag": "dragon", "lots": 0, "avg_bids": None, "bids_n": 0, "avg_favs": None, "favs_n": 0,
         "th_threads": 2, "th_period": "14 дн"}])
    out = mcp_server.what_sells()
    assert "moth: 4 лотов, ставок 1.5 (4), фавов 10 (4), ищут на TH: 3 тредов за 14 дн" in out
    assert "dragon: 0 лотов, ставок —, фавов —, ищут на TH: 2 тредов за 14 дн" in out


def test_income_mentions_sales_without_currency(server):
    with db.session() as c:
        sales.add_sale(c, title="Lumi", amount=500, currency=None, platform="toyhouse", format="auction",
                       status="paid")
        sales.add_sale(c, title="Moth", amount=20, currency="usd", platform="deviantart", format="auction",
                       status="paid")
    out = mcp_server.income(30)
    assert "$20" in out or "20" in out
    assert "Ещё 1 продаж без валюты — в суммы не входят." in out


def test_update_settings_boards_and_pages(server):
    out = mcp_server.update_settings(toyhouse_boards=["13.adoption-center", 5866], max_pages_per_board=8)
    assert out.startswith("Сохранено: toyhouse_boards, max_pages_per_board")
    assert "страниц: 2–8" in out
    cfg = load_config()
    assert cfg.toyhouse.boards == ["13.adoption-center", 5866] and cfg.toyhouse.max_pages_per_board == 8
    assert mcp_server.update_settings(max_pages_per_board=1).startswith("Не сохранила")


def test_da_group_folders_shows_size(server, monkeypatch):
    monkeypatch.setattr(da, "client", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(da, "list_group_folders", lambda http, name: [
        da.Folder("da-adopts", "1", "Auctions", size=9395), da.Folder("da-adopts", "2", "Old")])
    out = mcp_server.da_group_folders("da-adopts").splitlines()
    assert out[0].endswith(" — 9395 работ") and not out[1].endswith("работ")


def test_set_reply_template_warns_missing_slots(server):
    out = mcp_server.set_reply_template("основной", "Hi! I have {name}")
    assert out.startswith("Шаблон «основной» сохранён.") and "Внимание:" in out and "{url}" in out
    assert "Внимание" not in mcp_server.set_reply_template("полный", "Hi! {name} {url} {price}")


# --- отклик в тред Toyhouse: закрытые, уже отвеченные, цена против бюджета ---

def _reply_setup(title, extra=None):
    cfg = load_config()
    cfg.posting.characters = [{"name": "Lumi", "url": "https://toyhou.se/1.lumi", "price": "$40", "tags": ["moth"]}]
    cfg.posting.reply_templates = {"основной": "Hi! {name} {url} {price}"}
    save_config(cfg)
    with db.session() as c:
        db.upsert_item(c, Item(id="th:777", source="toyhouse", title=title,
                               url="https://toyhou.se/~forums/13.adoption-center/777.lf", author="op",
                               activity_ts=time.time(), extra=extra or {}))


def test_prepare_toyhouse_reply_refuses_closed_thread(server):
    _reply_setup("[CLOSED] LF moth characters")
    assert mcp_server.prepare_toyhouse_reply("th:777", "Lumi") == "Тред закрыт — отклик не нужен."


def test_prepare_toyhouse_reply_refuses_answered_thread(server):
    _reply_setup("LF moth characters", extra={"last_poster": ME})
    assert mcp_server.prepare_toyhouse_reply("th:777", "Lumi") == "Она уже отвечала в этом треде."


def test_prepare_toyhouse_reply_shows_price_conflict(server):
    _reply_setup("LF moth characters, budget $20")
    out = mcp_server.prepare_toyhouse_reply("th:777", "Lumi")
    assert out.startswith("Черновик ") and "⚠ цена перса $40 выше бюджета треда $20" in out


# --- cli: где запускаются крючки ---

def _args(scheduled=False, source="all", job=None):
    return argparse.Namespace(source=source, scheduled=scheduled, notify=False, job=job)


@pytest.fixture
def quiet_cli(monkeypatch):
    """Всё вокруг крючка — подделки: проверки источников, отправки, дайджест."""
    calls: dict = {"th": [], "bids": []}
    monkeypatch.setattr(posting, "send_due", lambda conn, cfg, now=None: [])
    monkeypatch.setattr(digest, "deliver_weekly", lambda conn, cfg, now=None: None)
    monkeypatch.setattr(digest, "deliver", lambda conn, cfg, problems=None: {"items": 0, "telegram_error": None,
                                                                              "file": None})
    monkeypatch.setattr(digest, "notify_bids", lambda cfg, events: calls["bids"].append(events))

    def th_refresh(conn, cfg, now=None, interactive=False, **kw):
        calls["th"].append(interactive)
        return ["аукционы Toyhouse: перечитала тредов 1"], ["событие"]

    monkeypatch.setattr(monitor, "refresh_th_auctions", th_refresh)
    return calls


def _results(monkeypatch, toyhouse: monitor.SourceResult):
    monkeypatch.setattr(monitor, "run_check", lambda conn, cfg, **kw: [
        monitor.SourceResult("deviantart", skipped="ещё рано"), toyhouse])


def _set_mode(mode):
    cfg = load_config()
    cfg.toyhouse.mode = mode
    save_config(cfg)


def test_cli_scheduled_bot_runs_th_auctions(server, quiet_cli, monkeypatch, capsys):
    _set_mode("bot")
    _results(monkeypatch, monitor.SourceResult("toyhouse", seen=5))
    assert cli._check(_args(scheduled=True), time.time()) == 0
    assert quiet_cli["th"] == [False] and quiet_cli["bids"] == [["событие"]]
    assert "перечитала тредов 1" in capsys.readouterr().out


def test_cli_scheduled_chrome_never_touches_her_chrome(server, quiet_cli, monkeypatch):
    _results(monkeypatch, monitor.SourceResult("toyhouse", seen=5))
    assert cli._check(_args(scheduled=True), time.time()) == 0
    assert quiet_cli["th"] == []


@pytest.mark.parametrize("res", [monitor.SourceResult("toyhouse", skipped="ещё рано"),
                                 monitor.SourceResult("toyhouse", error="Cloudflare", needs_login=True)])
def test_cli_skips_th_auctions_when_toyhouse_not_read(server, quiet_cli, monkeypatch, res):
    _set_mode("bot")
    _results(monkeypatch, res)
    cli._check(_args(scheduled=True), time.time())
    assert quiet_cli["th"] == []


def test_cli_th_auctions_crash_does_not_break_check(server, quiet_cli, monkeypatch):
    _set_mode("bot")
    _results(monkeypatch, monitor.SourceResult("toyhouse", seen=5))

    def boom(*a, **k):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(monitor, "refresh_th_auctions", boom)
    assert cli._check(_args(scheduled=True), time.time()) == 0


def test_check_job_reads_th_auctions_in_her_chrome(server, quiet_cli, monkeypatch):
    finished = {}
    monkeypatch.setattr(jobs, "finish", lambda job, lines, ok=True: finished.update(lines=lines, ok=ok))
    _results(monkeypatch, monitor.SourceResult("toyhouse", seen=5))
    assert cli._check_job(_args(job="j1", source="toyhouse")) == 0
    assert quiet_cli["th"] == [True] and finished["ok"]
    assert "аукционы Toyhouse: перечитала тредов 1" in finished["lines"]


# --- манифест и описания ---

NEW_ACTIONS = {"market": {"search", "groups", "buyers", "norms"}, "forecast": {"health", "explain"}}


def test_new_tools_registered_and_in_manifest():
    import asyncio
    from pathlib import Path

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    manifest = json.loads((Path(__file__).parent.parent / "manifest.json").read_text())
    assert {t["name"] for t in manifest["tools"]} == set(tools)
    for name, actions in NEW_ACTIONS.items():
        assert actions <= set(tools[name].input_schema["properties"]["action"]["enum"]), name
    # 0.5: настройки — один параметр changes={ключ: значение}; ключи и пределы — в get_settings.
    from mcp_devian import config

    assert set(tools["update_settings"].input_schema["properties"]) == {"changes", "finish_setup"}
    assert {"max_pages_per_board", "watermark_text", "da_comments_per_day", "da_pings_per_day"} <= set(
        config.SETTING_KEYS)
    assert "lot" in tools["sales"].input_schema["properties"]
    assert "show_blocked" in tools["findings"].input_schema["properties"]
    assert "окончательный" not in (tools["forecast"].description or "")
