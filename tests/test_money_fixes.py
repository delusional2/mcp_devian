import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_devian import advisor, db, digest, enrich, mcp_server, sales
from mcp_devian.config import Config, Notify, Posting, load_config, save_config
from mcp_devian.da_api import DAApiError, RateLimited
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400
MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.sqlite3")
    yield c
    c.close()


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: False)
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "s.sqlite3"))
    monkeypatch.setattr(digest, "WEEKLY_FILE", tmp_path / "weekly-report.md")
    save_config(Config(configured=True))
    return tmp_path


def msk(*args) -> float:
    return datetime(*args, tzinfo=MSK).timestamp()


# --- 1. Отчёт недели не роняет проверку ---

def test_load_config_fixes_bad_timezone_and_schedule(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"posting": {"timezone": "Mars/Olympus"},
                                "notify": {"weekly_report_weekday": "5", "weekly_report_hour": 25}}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.posting.timezone == "Europe/Moscow"
    assert cfg.notify.weekly_report_weekday == 5          # строку «5» поняли
    assert cfg.notify.weekly_report_hour == 19            # 25 часов — нет, по умолчанию
    assert any("posting.timezone" in w for w in cfg.warnings)
    assert any("weekly_report_hour" in w for w in cfg.warnings)
    assert not any("weekly_report_weekday" in w for w in cfg.warnings)


@pytest.mark.parametrize("raw", [{"weekly_report_weekday": True}, {"weekly_report_weekday": "sunday"},
                                 {"weekly_report_hour": None}, {"weekly_report_weekday": 7.5}])
def test_load_config_bad_weekly_values_fall_back(tmp_path, raw):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"notify": raw}), encoding="utf-8")
    cfg = load_config(path)
    assert (cfg.notify.weekly_report_weekday, cfg.notify.weekly_report_hour) == (6, 19)
    assert cfg.warnings


def test_load_config_good_values_untouched(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"posting": {"timezone": "Asia/Tokyo"},
                                "notify": {"weekly_report_weekday": 0, "weekly_report_hour": 0}}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.posting.timezone == "Asia/Tokyo" and cfg.notify.weekly_report_hour == 0 and not cfg.warnings


@pytest.mark.parametrize("cfg", [Config(posting=Posting(timezone="Nowhere/Land")),
                                 Config(posting=Posting(timezone="")),
                                 Config(notify=Notify(weekly_report_weekday="x")),
                                 Config(notify=Notify(weekly_report_hour=99))])
def test_weekly_due_false_on_bad_settings(conn, cfg):
    assert digest.weekly_due(conn, cfg, msk(2026, 11, 1, 20)) is False


def test_deliver_weekly_failure_not_recorded_as_sent(server, monkeypatch, caplog):
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: True)
    real = advisor.weekly_report

    def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(advisor, "weekly_report", broken)
    ts = msk(2026, 11, 1, 20)
    with db.session() as c, caplog.at_level(logging.ERROR, logger="mcp_devian.digest"):
        assert digest.deliver_weekly(c, Config(configured=True), ts) is None
        run = db.last_run(c, digest.WEEKLY_SOURCE)
        assert run["last_ok_ts"] is None and "boom" in run["last_error"]
        assert "weekly report failed" in caplog.text
        # починилось — отчёт уходит на следующей проверке
        monkeypatch.setattr(advisor, "weekly_report", real)
        assert digest.deliver_weekly(c, Config(configured=True), ts + 900) == str(digest.WEEKLY_FILE)


# --- 5. Mac спал в её час — отчёт догоняет ---

def test_weekly_catches_up_after_sleep(conn):
    cfg = Config()                                              # воскресенье 19:00 по Москве
    assert digest.weekly_due(conn, cfg, msk(2026, 11, 2, 10))   # понедельник утром — ещё да
    assert digest.weekly_due(conn, cfg, msk(2026, 11, 4, 18))   # почти три дня спустя — да
    assert not digest.weekly_due(conn, cfg, msk(2026, 11, 5, 10))  # больше трёх дней — ждём следующего
    assert not digest.weekly_due(conn, cfg, msk(2026, 11, 1, 18, 59))  # до часа отчёта на этой неделе
    db.record_run(conn, digest.WEEKLY_SOURCE, None, msk(2026, 11, 2, 10))
    assert not digest.weekly_due(conn, cfg, msk(2026, 11, 2, 11))
    assert digest.weekly_due(conn, cfg, msk(2026, 11, 8, 19))   # следующая неделя


def test_weekly_slot_in_her_timezone():
    cfg = Config(posting=Posting(timezone="America/New_York"),
                 notify=Notify(weekly_report_weekday=0, weekly_report_hour=9))
    now = datetime(2026, 11, 4, 12, tzinfo=ZoneInfo("America/New_York")).timestamp()   # среда
    slot = datetime.fromtimestamp(digest.weekly_slot(cfg, now), ZoneInfo("America/New_York"))
    assert (slot.year, slot.month, slot.day, slot.hour) == (2026, 11, 2, 9)


# --- 6. Telegram: режем до экранирования, ошибку не глотаем ---

def test_telegram_text_cut_before_escape():
    text = digest._telegram_text("a&" * 3000)
    assert len(text) <= digest.TELEGRAM_LIMIT and text.endswith("…")
    assert text[:-1].endswith(("&amp;", "a"))                   # не «&am» посередине сущности
    assert digest._telegram_text("<b>") == "&lt;b&gt;"


def test_weekly_telegram_error_logged_but_sent(server, monkeypatch, caplog):
    monkeypatch.setattr(digest, "notify_macos", lambda t, m: True)
    monkeypatch.setattr(digest, "send_telegram", lambda chunks: "Telegram ответил 400: bad")
    cfg = Config(configured=True, notify=Notify(telegram=True))
    with db.session() as c, caplog.at_level(logging.WARNING, logger="mcp_devian.digest"):
        assert digest.deliver_weekly(c, cfg, msk(2026, 11, 1, 20)) == str(digest.WEEKLY_FILE)
        assert db.last_run(c, digest.WEEKLY_SOURCE)["last_ok_ts"]
    assert "Telegram ответил 400" in caplog.text


# --- 2. Одна продажа на лот ---

def test_old_db_duplicates_cleaned_and_index_created(tmp_path):
    path = tmp_path / "old.sqlite3"
    c = db.connect(path)
    c.execute("DROP INDEX sales_item")
    for sid, status, paid, ts in (("a", "won", None, NOW), ("b", "paid", NOW + 5, NOW + 1), ("c", "won", None, NOW + 2)):
        c.execute("INSERT INTO sales (id, item_id, platform, title, status, created_ts, paid_ts, source) "
                  "VALUES (?, 'da:1', 'deviantart', 'Moth', ?, ?, ?, 'auto')", (sid, status, ts, paid))
    c.execute("INSERT INTO sales (id, item_id, platform, title, status, created_ts) "
              "VALUES ('m1', NULL, 'toyhouse', 'X', 'won', 1), ('m2', NULL, 'toyhouse', 'Y', 'won', 2)")
    c.commit()
    c.close()
    c = db.connect(path)
    assert [r["id"] for r in c.execute("SELECT id FROM sales WHERE item_id = 'da:1'")] == ["b"]  # оплаченная осталась
    assert c.execute("SELECT COUNT(*) FROM sales WHERE item_id IS NULL").fetchone()[0] == 2     # ручные без лота не трогаем
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO sales (id, item_id, platform, title, status, created_ts) "
                  "VALUES ('z', 'da:1', 'deviantart', 'Moth', 'won', 1)")
    c.close()


def test_add_sale_if_new_ignores_duplicate(conn):
    first = sales.add_sale(conn, title="Moth", amount=5, currency="usd", platform="deviantart", format="auction",
                           item_id="da:1", source="auto", now=NOW, if_new=True)
    again = sales.add_sale(conn, title="Moth", amount=5, currency="usd", platform="deviantart", format="auction",
                           item_id="da:1", source="auto", now=NOW, if_new=True)
    assert first and again is None
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1


def own_auction(conn, n=1, winner="alice", top_user="alice", amount=45, currency="usd"):
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=f"Moth {n}",
                              url=f"https://www.deviantart.com/me/art/Moth-{n}", lot_type="auction", own=True), now=NOW)
    conn.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, ended, winner, updated_ts, extra) "
                 "VALUES (?, ?, ?, 3, 1, ?, ?, ?)",
                 (f"da:{n}", amount, currency, winner, NOW, json.dumps({"top_user": top_user})))
    conn.commit()


def test_record_with_url_merges_into_auto_sale(conn):
    own_auction(conn, winner="alice", top_user="bob")
    [auto] = sales.sync_from_auctions(conn, NOW)
    sale_id, outcome = sales.record(conn, url="https://www.deviantart.com/me/art/Moth-1", title="Moth 1", amount=60,
                                    currency="usd", platform="deviantart", format="other", buyer="alice")
    assert (sale_id, outcome) == (auto, "merged")
    row = sales.get(conn, auto)
    assert (row["amount"], row["buyer"], row["format"], row["source"], row["note"]) == \
        (60, "alice", "auction", "manual", "")
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1
    # второй раз — уже её запись, не трогаем и не дублируем
    assert sales.record(conn, url="https://www.deviantart.com/me/art/Moth-1", title="Moth 1", amount=1,
                        currency="usd", platform="deviantart", format="other")[1] == "exists"
    assert sales.get(conn, auto)["amount"] == 60


def test_record_with_url_first_blocks_auto_duplicate(conn):
    sale_id, outcome = sales.record(conn, url="https://www.deviantart.com/me/art/Moth-1?x=1", title="Moth",
                                    amount=45, currency="usd", platform="toyhouse", format="auction", buyer="alice")
    assert outcome == "new"
    row = sales.get(conn, sale_id)
    assert (row["item_id"], row["platform"]) == ("da:1", "deviantart")   # лота ещё нет в базе — номер из ссылки
    own_auction(conn)
    assert sales.sync_from_auctions(conn, NOW) == []
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1


def test_record_rejects_non_da_url(conn):
    with pytest.raises(sales.SaleError):
        sales.record(conn, url="https://toyhou.se/12345.moth", title="Moth", amount=5, currency="usd",
                     platform="toyhouse", format="flatsale")


def test_record_sale_tool_with_url(server):
    with db.session() as c:
        own_auction(c)
    first = mcp_server.my_sales()
    assert "alice" in first and "$45" in first
    out = mcp_server.record_sale("Moth 1", 50, buyer="alice", url="https://www.deviantart.com/me/art/Moth-1")
    assert "дубля нет" in out and "$50" in out
    assert mcp_server.my_sales().count("Moth 1") == 1
    assert "Не записала" in mcp_server.record_sale("Moth", 5, url="https://example.com/x")


def test_sales_tools_not_read_only():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    # sales пишет (record, status, remind, export) и при чтении сверяет продажи с аукционами.
    assert tools["sales"].annotations.read_only_hint is False


# --- 3. Победитель и сумма из ставок ---

def test_auto_sale_winner_differs_from_top_bidder(conn):
    own_auction(conn, winner="carol", top_user="bob", amount=45)
    [sid] = sales.sync_from_auctions(conn, NOW)
    row = sales.get(conn, sid)
    assert row["buyer"] == "carol" and row["amount"] is None and "сумму проверь" in row["note"]
    assert "сумма ?" in sales.describe(row) and "сумму проверь" in sales.describe(row)
    sales.set_status(conn, sid, "paid", now=NOW + 10)
    assert sales.income(conn, NOW - DAY, NOW + DAY) == {}             # без суммы — не доход
    assert sales.without_amount(conn, NOW - DAY) == 1


def test_auto_sale_same_user_with_at_sign(conn):
    own_auction(conn, winner="@Bob", top_user="bob", amount=45)
    [sid] = sales.sync_from_auctions(conn, NOW)
    row = sales.get(conn, sid)
    assert row["amount"] == 45 and row["note"] == ""


def test_auto_sale_no_named_winner_uses_top_bidder(conn):
    own_auction(conn, winner=None, top_user="dave", amount=30)
    [sid] = sales.sync_from_auctions(conn, NOW)
    row = sales.get(conn, sid)
    assert (row["buyer"], row["amount"]) == ("dave", 30) and "проверь" in row["note"]


def test_auto_sale_skipped_without_anyone(conn):
    own_auction(conn, winner=None, top_user=None)
    assert sales.sync_from_auctions(conn, NOW) == []


def test_auto_sale_tolerates_broken_extra(conn):
    own_auction(conn)
    conn.execute("UPDATE auctions SET extra = 'not json'")
    conn.commit()
    [sid] = sales.sync_from_auctions(conn, NOW)
    assert sales.get(conn, sid)["amount"] == 45


def test_income_tool_mentions_sales_without_amount(server):
    with db.session() as c:
        own_auction(c, winner="carol", top_user="bob")
    out = mcp_server.income()
    assert "оплаченных продаж нет" in out and "без суммы: 1" in out


# --- 9. «Перс передан» — ещё не оплата ---

def test_transferred_from_won_is_not_income(conn):
    sid = sales.add_sale(conn, title="Moth", amount=40, currency="usd", platform="toyhouse", format="flatsale", now=NOW)
    sales.set_status(conn, sid, "transferred", now=NOW + 10)
    row = sales.get(conn, sid)
    assert row["paid_ts"] is None and "оплаты ещё нет" in sales.describe(row)
    assert sales.income(conn, NOW - DAY, NOW + DAY) == {}
    sales.set_status(conn, sid, "paid", now=NOW + 20)
    assert sales.get(conn, sid)["paid_ts"] == NOW + 20
    assert sales.income(conn, NOW - DAY, NOW + DAY)["usd"]["total"] == 40


def test_paid_then_transferred_keeps_payment(conn):
    sid = sales.add_sale(conn, title="Moth", amount=40, currency="usd", platform="toyhouse", format="flatsale", now=NOW)
    sales.set_status(conn, sid, "paid", now=NOW + 10)
    sales.set_status(conn, sid, "transferred", now=NOW + 20)
    assert sales.get(conn, sid)["paid_ts"] == NOW + 10


def test_done_from_won_counts_as_paid(conn):
    sid = sales.add_sale(conn, title="Moth", amount=40, currency="usd", platform="toyhouse", format="flatsale", now=NOW)
    sales.set_status(conn, sid, "done", now=NOW + 10)
    assert sales.get(conn, sid)["paid_ts"] == NOW + 10


# --- 8. Деньги ---

@pytest.mark.parametrize("amount,currency,text", [
    (12.5, "usd", "$12.50"), (40, "usd", "$40"), (40.0, "eur", "€40"), (1234.5, "usd", "$1,234.50"),
    (1500, "points", "1,500 pts"), (1500.4, "points", "1,500 pts"), (999, "points", "999 pts"),
    (0.1 + 0.2, "gbp", "£0.30"), (None, "usd", "?"), (2_000_000, None, "$2,000,000"),
])
def test_money_format(amount, currency, text):
    assert sales.money(amount, currency) == text


# --- 4 и 7. Статистика: промахи не съедают бюджет, ошибка пачки не теряет остальное ---

def add_lot(conn, n, age_days=1, uuid=None, own=False):
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=f"Lot {n}", url=lot_url(n), author="a",
                              own=own, lot_type="auction", published_ts=NOW - age_days * DAY,
                              extra={"uuid": uuid} if uuid else {}), now=NOW)
    conn.commit()


def lot_url(n):
    return f"https://www.deviantart.com/a/art/Lot-{n}"


def remember_miss(conn, n, ts):
    conn.executescript(enrich.MISS_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO uuid_misses (url, ts) VALUES (?, ?)", (lot_url(n), ts))
    conn.commit()


class Client:
    def __init__(self, found=None, bad=(), fail_all=False):
        self.found, self.bad, self.fail_all = found or {}, set(bad), fail_all
        self.lookups, self.meta_calls = [], []

    def deviation_uuid_from_url(self, url, folders=(), max_pages=10, miss_cache=None):
        self.lookups.append(url)
        uuid = self.found.get(url)
        if uuid is None and miss_cache is not None:
            miss_cache[url] = NOW
        return uuid

    def deviation_metadata(self, uuids, ext_stats=True):
        self.meta_calls.append(list(uuids))
        if self.fail_all or self.bad & set(uuids):
            raise DAApiError("bad request", 1)
        return {u: {"views": 10, "favourites": 2, "comments": 1} for u in uuids}


def test_cached_misses_not_candidates_and_dont_crowd_out(conn):
    for n in range(3):
        add_lot(conn, n, age_days=1)          # самые свежие — но не находятся
        remember_miss(conn, n, NOW - 3600)
    add_lot(conn, 7, age_days=5)
    add_lot(conn, 8, age_days=6)
    assert [r["id"] for r in enrich.candidates(conn, NOW, limit=2)] == ["da:7", "da:8"]
    # срок промаха вышел — снова пробуем
    assert "da:0" in [r["id"] for r in enrich.candidates(conn, NOW + 2 * DAY, limit=10)]


def test_cached_miss_does_not_spend_lookup_budget(conn, monkeypatch):
    add_lot(conn, 1, age_days=1)
    add_lot(conn, 2, age_days=2)
    remember_miss(conn, 1, NOW - 60)
    # даже если кандидат прошёл (например, гонка с другой проверкой), бюджет на него не тратим
    monkeypatch.setattr(enrich, "candidates", lambda c, now, limit: list(
        c.execute("SELECT * FROM items ORDER BY id")))
    client = Client(found={lot_url(2): "U2"})
    s = enrich.enrich_stats(conn, client, now=NOW, lookup_budget=1, sleep=lambda s: None)
    assert client.lookups == [lot_url(2)] and s["lookups"] == 1 and s["snapshots"] == 1


def test_sleep_after_miss_too(conn):
    add_lot(conn, 1, age_days=1)
    add_lot(conn, 2, age_days=2)
    sleeps = []
    s = enrich.enrich_stats(conn, Client(found={lot_url(2): "U2"}), now=NOW, sleep=sleeps.append)
    assert s["misses"] == 1 and sleeps == [1.0, 1.0]


def test_lookup_api_error_remembered_as_miss(conn):
    add_lot(conn, 1)

    class Broken(Client):
        def deviation_uuid_from_url(self, url, **kw):
            raise DAApiError("bad url", "bad_url")
    enrich.enrich_stats(conn, Broken(), now=NOW, sleep=lambda s: None)
    assert enrich.candidates(conn, NOW + 60) == []


def test_metadata_error_skips_only_bad_work(conn):
    for n in range(5):
        add_lot(conn, n, uuid=f"U{n}", age_days=n + 1)
    client = Client(bad={"U2"})
    s = enrich.enrich_stats(conn, client, now=NOW, sleep=lambda s: None)
    assert s["snapshots"] == 4 and s["stopped"] is None
    assert enrich.latest_stats(conn, "da:2") is None and enrich.latest_stats(conn, "da:0") is not None


def test_metadata_errors_bounded_when_api_down(conn):
    for n in range(40):
        add_lot(conn, n, uuid=f"U{n}", age_days=1)
    client = Client(fail_all=True)
    s = enrich.enrich_stats(conn, client, now=NOW, sleep=lambda s: None)
    assert s["snapshots"] == 0 and len(client.meta_calls) == enrich.METADATA_CALLS_MAX


def test_metadata_rate_limit_still_stops(conn):
    add_lot(conn, 1, uuid="U1")

    class Limited(Client):
        def deviation_metadata(self, uuids, ext_stats=True):
            raise RateLimited("429", status=429)
    s = enrich.enrich_stats(conn, Limited(), now=NOW, sleep=lambda s: None)
    assert "притормозить" in s["stopped"]
