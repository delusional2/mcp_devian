import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_devian import db, digest, mcp_server, monitor, posting, sales
from mcp_devian.config import Config, Notify, save_config
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: False)
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "s.sqlite3"))
    monkeypatch.setattr(digest, "WEEKLY_FILE", tmp_path / "weekly-report.md")
    save_config(Config(configured=True))
    return tmp_path


def test_record_and_walk_sale(server):
    out = mcp_server.record_sale("Moth #3", 40, platform="toyhouse", format="flatsale", buyer="alice")
    assert "Записала продажу" in out
    sale_id = out.split("[")[1].split("]")[0]
    assert "alice" in mcp_server.my_sales()
    assert "оплачено" in mcp_server.set_sale_status(sale_id, "paid")
    assert "$40" in mcp_server.income(days=30)
    assert "Не получилось" in mcp_server.set_sale_status("nope", "paid")
    assert "Не записала" in mcp_server.record_sale(" ", 5)


def test_income_empty_and_filters(server):
    assert "оплаченных продаж нет" in mcp_server.income()
    assert mcp_server.my_sales(status="done") == "Продаж за этот период нет."


def test_my_sales_picks_up_auction_winners(server):
    with db.session() as c:
        db.upsert_item(c, Item(id="da:1", source="deviantart", title="My moth", url="u", lot_type="ych", own=True),
                       now=time.time())
        c.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, ended, winner, updated_ts) "
                  "VALUES ('da:1', 45, 'usd', 4, 1, 'bob', ?)", (time.time(),))
        c.commit()
    out = mcp_server.my_sales()
    assert "bob" in out and "My moth" in out


def test_advice_tools_on_empty_db(server):
    assert "С чего начать продажи" in mcp_server.sales_advice()
    assert "Неделя продаж" in mcp_server.weekly_sales_report()
    assert mcp_server.plan_new_adopt(species="dragon", format="auction")
    assert "мало данных" in mcp_server.best_time_to_post()
    assert "мало лотов" in mcp_server.what_sells()
    assert "нет в базе" in mcp_server.review_my_post("https://www.deviantart.com/moth/art/Moth-123")


def test_review_my_post_known(server):
    with db.session() as c:
        db.upsert_item(c, Item(id="da:123", source="deviantart", title="adopt", url="u", lot_type="auction",
                               own=True, published_ts=time.time()), now=time.time())
    out = mcp_server.review_my_post("https://www.deviantart.com/moth/art/Moth-123")
    assert out.startswith("- ") or "поправить нечего" in out


def test_what_sells_lists_market(server):
    with db.session() as c:
        for i in range(3):
            db.upsert_item(c, Item(id=f"da:{i}", source="deviantart", title=f"Dragon adopt auction {i}", url="u",
                                   lot_type="auction", published_ts=time.time()), now=time.time())
    out = mcp_server.what_sells()
    assert "вид:dragon: 3 лотов" in out


# --- Фоновая часть ---

def _sunday_evening(tz: str = "Europe/Moscow") -> float:
    return datetime(2026, 11, 1, 20, 0, tzinfo=ZoneInfo(tz)).timestamp()  # воскресенье


def test_weekly_report_once_a_week(server, monkeypatch):
    sent = []
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: sent.append(t) or True)
    cfg = Config(configured=True)
    ts = _sunday_evening()
    with db.session() as c:
        assert digest.deliver_weekly(c, cfg, ts - 3 * 3600) is None   # 17:00 — ещё рано
        assert digest.deliver_weekly(c, cfg, ts) == str(digest.WEEKLY_FILE)
        assert digest.deliver_weekly(c, cfg, ts + 1800) is None        # уже отправили
        assert digest.deliver_weekly(c, cfg, ts + 7 * DAY) is not None
    assert sent == ["Неделя продаж", "Неделя продаж"]
    assert "Неделя продаж" in digest.WEEKLY_FILE.read_text(encoding="utf-8")


def test_weekly_report_off_or_other_day(server):
    with db.session() as c:
        assert not digest.weekly_due(c, Config(notify=Notify(weekly_report=False)), _sunday_evening())
        assert not digest.weekly_due(c, Config(), _sunday_evening() - DAY)


def test_seller_pass_without_da(server, monkeypatch):
    def no_client(cfg):
        raise RuntimeError("нет client_id")
    monkeypatch.setattr(posting, "da_client", no_client)
    with db.session() as c:
        db.upsert_item(c, Item(id="da:9", source="deviantart", title="Mine", url="u", lot_type="auction",
                               own=True), now=NOW)
        c.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, ended, winner, updated_ts) "
                  "VALUES ('da:9', 20, 'usd', 2, 1, 'carol', ?)", (NOW,))
        c.commit()
        lines = monitor.seller_pass(c, Config(), NOW)
        assert lines == ["продажи: записала победителей аукционов — 1"]
        assert sales.list_sales(c)[0]["buyer"] == "carol"
        assert db.last_run(c, monitor.ENRICH_SOURCE)["last_error"].startswith("RuntimeError")


def test_seller_pass_enrich_hourly(server, monkeypatch):
    class Connected:
        def is_connected(self):
            return True

    calls = []
    monkeypatch.setattr(posting, "da_client", lambda cfg: Connected())
    from mcp_devian import enrich
    monkeypatch.setattr(enrich, "enrich_stats", lambda conn, client, now: calls.append(now) or
                        {"lots": 2, "snapshots": 2, "lookups": 0, "misses": 0, "stopped": None})
    with db.session() as c:
        assert monitor.seller_pass(c, Config(), NOW) == ["статистика: просмотры и фавы для 2 лотов"]
        assert monitor.seller_pass(c, Config(), NOW + 600) == []
        assert monitor.seller_pass(c, Config(), NOW + 600, enrich=False) == []
        monitor.seller_pass(c, Config(), NOW + 3700)
    assert calls == [NOW, NOW + 3700]
