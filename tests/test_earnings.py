"""Деньги её времени: ₽ в час по форматам и нишам, цель на месяц, план недели. Только фейки, без сети."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_devian import db, earnings, evidence, sales
from mcp_devian.config import Config
from mcp_devian.db import Item

DAY = 86400
NOW = 1_800_000_000.0   # 15.01.2027, 11:00 по Москве
TZ = "Europe/Moscow"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "e.sqlite3")
    yield c
    c.close()


def paid(conn, title, net, *, fmt="auction", currency="usd", amount=40.0, ts=NOW - 40 * DAY, item_id=None,
         net_currency="rub", status="done"):
    sid = sales.add_sale(conn, title=title, amount=amount, currency=currency, platform="deviantart", format=fmt,
                         buyer="ann", item_id=item_id, status=status, now=ts,
                         net_amount=net, net_currency=net_currency if net is not None else None)
    return sid


def work(conn, title, net, hours, *, fmt="auction", ts=NOW - 40 * DAY, explicit="", **kw):
    sid = paid(conn, title, net, fmt=fmt, ts=ts, **kw)
    earnings.log_hours(conn, sid, hours, format=explicit, now=ts + 3600)
    return sid


# --- Часы: разбор и запись ---

@pytest.mark.parametrize("text, hours", [
    ("3.5 ч", 3.5), ("3,5ч", 3.5), ("2 ч 30 мин", 2.5), ("90 мин", 1.5), ("1:30", 1.5), ("3h", 3.0),
    ("2.5 hours", 2.5), (4, 4.0), ("4", 4.0), ("2 часа", 2.0), ("45 минут", 0.75)])
def test_parse_hours(text, hours):
    assert earnings.parse_hours(text) == pytest.approx(hours)


@pytest.mark.parametrize("bad", ["", "abc", "0", "-1", "500 ч", "3 дня", True, float("nan"), "3 ч кота"])
def test_parse_hours_rejects(bad):
    with pytest.raises(earnings.EarningsError):
        earnings.parse_hours(bad)


def test_log_by_sale_id_title_and_free(conn):
    sid = paid(conn, "Dragon adopt", 3000, ts=NOW - 2 * DAY)       # свежая продажа — по названию привязываем
    res = earnings.log_hours(conn, sid, "3 ч", now=NOW)
    assert res["linked"] == "sale" and res["sale_id"] == sid
    res = earnings.log_hours(conn, "dragon ADOPT", "1.5 ч", now=NOW)   # по названию, без учёта регистра
    assert res["sale_id"] == sid
    assert res["total_hours"] == pytest.approx(4.5)
    assert "4.5 ч" in res["text"]
    free = earnings.log_hours(conn, "Moth WIP", "2 ч", now=NOW)
    assert free["linked"] == "none"
    assert "привяж" in free["text"]


def test_log_cyrillic_title_case_insensitive(conn):
    sid = paid(conn, "Лунный Дракон", 3000, ts=NOW - 2 * DAY)
    assert earnings.log_hours(conn, "лунный дракон", "2 ч", now=NOW)["sale_id"] == sid


def test_log_by_item_and_url(conn):
    url = "https://www.deviantart.com/me/art/Fox-adopt-123"
    db.upsert_item(conn, Item(id="da:123", source="deviantart", title="Fox adopt", url=url, own=True,
                              lot_type="auction", published_ts=NOW - 5 * DAY), now=NOW)
    conn.commit()
    res = earnings.log_hours(conn, url, 2, now=NOW)
    assert res["linked"] == "item" and res["item_id"] == "da:123"
    res = earnings.log_hours(conn, "da:123", 1, now=NOW)
    assert res["total_hours"] == pytest.approx(3)


def test_log_ambiguous_title_lists_ids(conn):
    a = paid(conn, "Cat", 1000, ts=NOW - 2 * DAY)
    b = paid(conn, "Cat", 1200, ts=NOW - 3 * DAY)
    with pytest.raises(earnings.EarningsError) as e:
        earnings.log_hours(conn, "cat", 2, now=NOW)
    assert a in str(e.value) and b in str(e.value)


def test_log_format_alias_and_bad_format(conn):
    sid = paid(conn, "Three moths", 4000, fmt="flatsale")
    assert earnings.log_hours(conn, sid, 3, format="набор", now=NOW)["format"] == "bundle"
    with pytest.raises(earnings.EarningsError):
        earnings.log_hours(conn, sid, 3, format="зебра", now=NOW)


def test_overhead_needs_format(conn):
    with pytest.raises(earnings.EarningsError):
        earnings.log_hours(conn, "YCH base", 3, overhead=True, now=NOW)
    res = earnings.log_hours(conn, "YCH base", 3, overhead=True, format="ych", now=NOW)
    assert res["linked"] == "overhead"


def test_link_and_undo(conn):
    free = earnings.log_hours(conn, "Moth WIP", "2 ч", now=NOW - 2 * DAY)
    sid = paid(conn, "Moth final", 2000, ts=NOW - DAY)
    assert "Привязала" in earnings.link(conn, "moth wip", sid)
    ws = earnings.works(conn, NOW)
    assert len(ws) == 1 and ws[0].sale_ids == [sid] and ws[0].status == "paid"
    assert "Убрала" in earnings.undo(conn, free["id"])
    assert earnings.works(conn, NOW) == []
    with pytest.raises(earnings.EarningsError):
        earnings.undo(conn, "nope")


def test_commission_slot_sums_deposit_and_final(conn):
    paid(conn, "YCH ann (deposit)", 700, fmt="other", currency="rub", amount=750, item_id="comm:s1:deposit")
    paid(conn, "YCH ann (final)", 700, fmt="other", currency="rub", amount=750, item_id="comm:s1:final",
         status="won")
    res = earnings.log_hours(conn, "comm:s1", 5, format="ych", now=NOW - 30 * DAY)
    assert res["linked"] == "commission"
    w = earnings.works(conn, NOW)[0]
    assert w.status == "pending"            # остаток ещё не оплачен — работу не считаем
    assert len(w.sale_ids) == 2
    final = conn.execute("SELECT id FROM sales WHERE item_id = 'comm:s1:final'").fetchone()[0]
    sales.set_status(conn, final, "paid", now=NOW - DAY)
    conn.execute("UPDATE sales SET net_amount = 700, net_currency = 'rub' WHERE item_id = 'comm:s1:final'")
    w = earnings.works(conn, NOW)[0]
    assert w.status == "paid" and w.income_rub == 1400 and w.format == "ych"


# --- Работы: статусы ---

def test_unsold_counts_as_zero_after_settle_pending_before(conn):
    old = earnings.log_hours(conn, "Old moth", 4, now=NOW - 40 * DAY)
    young = earnings.log_hours(conn, "Young moth", 4, now=NOW - 3 * DAY)
    ws = {w.title: w for w in earnings.works(conn, NOW)}
    assert ws["Old moth"].status == "unsold" and ws["Old moth"].income_rub == 0
    assert ws["Young moth"].status == "pending" and ws["Young moth"].income_rub is None
    assert old["id"] != young["id"]


def test_unpaid_sale_stays_pending_and_cancelled_becomes_unsold(conn):
    won = paid(conn, "Won", None, status="won", ts=NOW - 50 * DAY)
    earnings.log_hours(conn, won, 3, now=NOW - 50 * DAY)
    gone = paid(conn, "Gone", None, status="won", ts=NOW - 50 * DAY)
    sales.set_status(conn, gone, "cancelled", now=NOW - 45 * DAY)
    earnings.log_hours(conn, gone, 3, now=NOW - 50 * DAY)
    ws = {w.title: w for w in earnings.works(conn, NOW)}
    assert ws["Won"].status == "pending"
    assert ws["Gone"].status == "unsold"


def test_format_detection(conn):
    earnings.log_hours(conn, paid(conn, "Batch of 3 moths", 3000, fmt="flatsale"), 3, now=NOW)
    earnings.log_hours(conn, paid(conn, "Dragon", 3000, fmt="bta"), 3, now=NOW)
    earnings.log_hours(conn, paid(conn, "Fox YCH slot", 3000, fmt="other"), 3, now=NOW)
    earnings.log_hours(conn, paid(conn, "Wolf", 3000, fmt="flatsale"), 3, format="commission", now=NOW)
    fmts = {w.title: w.format for w in earnings.works(conn, NOW)}
    assert fmts == {"Batch of 3 moths": "bundle", "Dragon": "auction", "Fox YCH slot": "ych", "Wolf": "commission"}


# --- ₽ в час ---

def seed_formats(conn):
    # Наборы: быстро и в час выгоднее; аукционы: средний чек выше, но рисуются дольше.
    for i in range(10):
        work(conn, f"Moth set {i}", 3400 + 100 * (i % 3), 3 + 0.2 * (i % 2), fmt="flatsale", explicit="bundle",
             ts=NOW - (40 + i) * DAY)
        work(conn, f"Dragon {i}", 5000 + 200 * (i % 3), 8 + 0.5 * (i % 2), fmt="auction", ts=NOW - (40 + i) * DAY)


def test_hourly_compares_formats_honestly(conn):
    seed_formats(conn)
    rep = earnings.hourly(conn, NOW)
    by = {g.key: g for g in rep.formats}
    assert by["bundle"].rate > by["auction"].rate
    assert by["bundle"].level != "нет вывода"
    cmp_ = rep.compare_formats
    assert cmp_["leader"].key == "bundle" and cmp_["beats"]
    assert cmp_["seems"].key == "auction"         # кажется выгоднее по среднему чеку
    text = earnings.render_hourly(rep)
    assert "₽/ч" in text and "наборы" in text and "аукционы" in text
    assert "Кажется" in text
    assert "n=10" in text and "её продажи" in text      # метка evidence.tag
    assert text == earnings.render_hourly(earnings.hourly(conn, NOW))   # детерминированно


def test_hourly_refuses_rate_below_threshold(conn):
    for i in range(3):
        work(conn, f"YCH {i}", 2000, 4, fmt="other", explicit="ych", ts=NOW - (40 + i) * DAY)
    rep = earnings.hourly(conn, NOW)
    g = {g.key: g for g in rep.formats}["ych"]
    assert g.level == "нет вывода"
    text = earnings.render_hourly(rep)
    assert "YCH: вывода нет" in text
    assert "₽/ч" not in text.split("YCH: вывода нет")[1].split("\n")[0]
    assert rep.compare_formats is None


def test_hourly_includes_unsold_and_overhead_hours(conn):
    for i in range(8):
        work(conn, f"Moth {i}", 3000, 3, fmt="flatsale", ts=NOW - (40 + i) * DAY)
    base = {g.key: g for g in earnings.hourly(conn, NOW).formats}["flatsale"].rate
    earnings.log_hours(conn, "Unsold moth", 6, format="flatsale", now=NOW - 40 * DAY)
    after = {g.key: g for g in earnings.hourly(conn, NOW).formats}["flatsale"]
    assert after.rate < base and after.unsold_n == 1
    assert "непроданн" in earnings.render_hourly(earnings.hourly(conn, NOW))


def test_hourly_missing_net_reported_not_mixed(conn):
    for i in range(8):
        work(conn, f"Moth {i}", 3000, 3, fmt="flatsale", ts=NOW - (40 + i) * DAY)
    work(conn, "Dollar moth", None, 3, fmt="flatsale", ts=NOW - 40 * DAY)
    rep = earnings.hourly(conn, NOW)
    g = {g.key: g for g in rep.formats}["flatsale"]
    assert g.rate == pytest.approx(1000)          # $ без «сколько дошло» в ₽ не смешиваем
    assert g.missing_n == 1
    text = earnings.render_hourly(rep)
    assert "впиши" in text and "$40" in text


def test_hourly_points_separate(conn):
    work(conn, "Points moth", None, 2, fmt="flatsale", currency="points", amount=500)
    text = earnings.render_hourly(earnings.hourly(conn, NOW))
    assert "pts" in text and "не выводятся" in text


def test_hourly_empty(conn):
    assert "Часов пока не записано" in earnings.render_hourly(earnings.hourly(conn, NOW))


def test_hourly_by_species(conn):
    for i in range(9):
        work(conn, f"Dragon adopt {i}", 3000 + 50 * i, 3, fmt="flatsale", ts=NOW - (40 + i) * DAY)
    rep = earnings.hourly(conn, NOW)
    keys = [g.key for g in rep.niches]
    assert "вид:dragon" in keys
    assert "dragon" in earnings.render_hourly(rep)


# --- Цель на месяц ---

def history(conn, n=10, fmt="auction", net=4000, start=60):
    for i in range(n):
        paid(conn, f"{fmt} {i}", net + 100 * (i % 3), fmt=fmt, ts=NOW - (start + i) * DAY)


def test_month_bounds_in_her_tz():
    start, end, key = earnings.month_bounds(NOW, TZ)
    assert key == "2027-01"
    assert datetime.fromtimestamp(start, ZoneInfo(TZ)).strftime("%d.%m %H:%M") == "01.01 00:00"
    assert datetime.fromtimestamp(end, ZoneInfo(TZ)).strftime("%d.%m %H:%M") == "01.02 00:00"


def test_goal_progress_and_need_per_format(conn):
    history(conn, 10, "auction", 4000)
    history(conn, 9, "flatsale", 2000, start=70)
    for i in range(3):
        paid(conn, f"Jan {i}", 4000, ts=NOW - (i + 1) * DAY)       # в январе: 12 000 ₽
    paid(conn, "Jan $", None, ts=NOW - DAY)                         # без суммы в ₽ — не входит
    text = earnings.set_goal(conn, 30000, now=NOW, tz=TZ)
    assert "30,000 ₽" in text
    st = earnings.goal_status(conn, NOW, TZ)
    assert st["got"] == pytest.approx(12000) and st["remaining"] == pytest.approx(18000)
    assert st["days_left"] == 17
    need = {d["format"]: d for d in st["need"]}
    assert need["auction"]["count"] == 5            # 18000 / ~4090 → 5
    assert need["flatsale"]["count"] == 9           # 18000 / ~2100 → 9
    text = earnings.render_goal(st)
    assert "ещё ~5 аукционов" in text and "январь" in text
    assert "n=13" in text                           # 10 прошлых + 3 январских аукциона
    assert "1 оплаченная продажа, где не вписано, сколько дошло" in text and "$40" in text


def test_goal_refuses_without_history(conn):
    paid(conn, "Jan", 4000, ts=NOW - DAY)
    earnings.set_goal(conn, 30000, now=NOW, tz=TZ)
    text = earnings.render_goal(earnings.goal_status(conn, NOW, TZ))
    assert "Вывода нет" in text
    assert "ещё ~" not in text


def test_goal_reached_and_unset(conn):
    text = earnings.render_goal(earnings.goal_status(conn, NOW, TZ))
    assert "не задана" in text
    paid(conn, "Big", 50000, ts=NOW - DAY)
    earnings.set_goal(conn, 30000, now=NOW, tz=TZ)
    assert "Цель достигнута" in earnings.render_goal(earnings.goal_status(conn, NOW, TZ))


def test_goal_validation_and_month(conn):
    for bad in (0, -5, float("inf"), 1e9):
        with pytest.raises(earnings.EarningsError):
            earnings.set_goal(conn, bad, now=NOW, tz=TZ)
    earnings.set_goal(conn, 20000, month="2027-02", now=NOW, tz=TZ)
    earnings.set_goal(conn, 25000, month="02", now=NOW, tz=TZ)       # тот же месяц — перезапись
    assert conn.execute("SELECT target FROM earn_goals WHERE month = '2027-02'").fetchone()[0] == 25000
    with pytest.raises(earnings.EarningsError):
        earnings.set_goal(conn, 1000, month="13", now=NOW, tz=TZ)


def test_goal_uses_hourly_when_known(conn):
    seed_formats(conn)
    earnings.set_goal(conn, 200000, now=NOW, tz=TZ)
    text = earnings.render_goal(earnings.goal_status(conn, NOW, TZ))
    assert "ч работы" in text


# --- Сколько она успевает ---

def test_capacity_set_inferred_and_unknown(conn):
    assert earnings.capacity(conn, NOW)["value"] is None
    for w in range(6):
        for k in range(3):
            db.upsert_item(conn, Item(id=f"da:{w}{k}", source="deviantart", title=f"a{w}{k}", url=f"u{w}{k}",
                                      own=True, lot_type="auction", published_ts=NOW - (w * 7 + 1 + k) * DAY),
                           now=NOW)
    conn.commit()
    cap = earnings.capacity(conn, NOW)
    assert cap["value"] == 3 and cap["source"] == "history"
    assert "4" in earnings.set_capacity(conn, 4, now=NOW)
    cap = earnings.capacity(conn, NOW)
    assert cap == {**cap, "value": 4, "source": "her"}
    with pytest.raises(earnings.EarningsError):
        earnings.set_capacity(conn, 500, now=NOW)
    earnings.set_capacity(conn, 0, now=NOW)            # 0 — забыть, считать по истории
    assert earnings.capacity(conn, NOW)["source"] == "history"


# --- План недели ---

class FakeReport:
    def __init__(self, niches=(), refusal=None):
        self.niches = list(niches)
        self.refusal = refusal


def niche(key, verdict, sold=12, lots=20, kr=30, nr=100):
    ev = evidence.span([NOW - 30 * DAY, NOW - 10 * DAY], "market_da", "аукционы DA", None, "observed", n=lots,
                       interval=evidence.wilson(sold, lots), measure="rate", value=sold / lots)
    return {"key": key, "verdict": verdict, "sold": sold, "lots": lots, "p_sold": sold / lots, "rest": (kr, nr),
            "ev": ev, "confidence": evidence.level(ev), "supply": 5}


BEST = {"slot": ("weekday", 3), "level": "средняя", "when": "в будни вечером (18–24)",
        "evidence": "в будни вечером ставку получили 9 из 12"}


def fakes(niches_rep=None, best=None, unsold=None, ending=None):
    return dict(niches_fn=lambda c, n: niches_rep or FakeReport(refusal="Вывода нет: итог известен у 10%."),
                best_time_fn=lambda c, n, tz: best,
                unsold_fn=lambda c, n, tz: unsold or {"lots": []},
                ending_fn=lambda c, n, tz: ending or [])


def test_week_plan_grounded(conn):
    earnings.set_capacity(conn, 3, now=NOW)
    rep = FakeReport([niche("вид:dragon", "sells"), niche("стиль:chibi", "saturated"), niche("вид:cat", "neutral")])
    unsold = {"lots": [{"item_id": "da:1", "title": "Old fox", "why": "аукцион без ставок"}]}
    ending = [{"item_id": "da:2", "title": "Moth", "end_ts": NOW + 2 * DAY, "text": "«Moth» закончится через 2 дн"}]
    plan = earnings.week_plan(conn, Config(), NOW, **fakes(rep, BEST, unsold, ending))
    text = plan["text"]
    assert "dragon" in text and "уверенность" in text
    assert "chibi" in text                      # пореже — насыщено
    assert "cat" not in text                    # нейтральную нишу спросом не называем
    assert len(plan["slots"]) == 3
    for ts in plan["slots"]:
        local = datetime.fromtimestamp(ts, ZoneInfo(TZ))
        assert local.weekday() < 5 and 18 <= local.hour < 24 and ts > NOW
    assert len({datetime.fromtimestamp(ts, ZoneInfo(TZ)).date() for ts in plan["slots"]}) == 3
    assert "Old fox" in text and "Moth" in text
    assert plan["ask"] is None


def test_week_plan_without_data_invents_nothing(conn):
    plan = earnings.week_plan(conn, Config(), NOW, **fakes())
    text = plan["text"]
    assert "вывода нет" in text.lower()
    assert "не видно" in text
    assert plan["slots"] == []
    assert plan["ask"] and "в неделю" in plan["ask"]


def test_week_plan_weak_time_not_used(conn):
    earnings.set_capacity(conn, 2, now=NOW)
    weak = {**BEST, "level": "низкая"}
    plan = earnings.week_plan(conn, Config(), NOW, **fakes(best=weak))
    assert plan["slots"] == []


def test_week_plan_survives_failing_sources(conn):
    def boom(*a, **k):
        raise RuntimeError("x")

    earnings.set_capacity(conn, 2, now=NOW)
    plan = earnings.week_plan(conn, Config(), NOW, niches_fn=boom, best_time_fn=boom, unsold_fn=boom, ending_fn=boom)
    assert "не посчитал" in plan["text"]


def test_week_plan_recommends_format_only_when_proven(conn):
    seed_formats(conn)
    earnings.set_capacity(conn, 2, now=NOW)
    text = earnings.week_plan(conn, Config(), NOW, **fakes())["text"]
    assert "Формат: наборы" in text


def test_week_plan_mentions_goal(conn):
    history(conn, 10, "auction", 4000)
    earnings.set_goal(conn, 30000, now=NOW, tz=TZ)
    earnings.set_capacity(conn, 2, now=NOW)
    text = earnings.week_plan(conn, Config(), NOW, **fakes())["text"]
    assert "Цель" in text


# --- Вход для mcp_server ---

def test_handle_routes(conn):
    cfg = Config()
    sid = paid(conn, "Dragon", 3000)
    assert "Записала" in earnings.handle(conn, cfg, "hours", ref=sid, hours="2 ч", now=NOW)
    assert "вывода нет" in earnings.handle(conn, cfg, "hourly", now=NOW)
    assert "30,000 ₽" in earnings.handle(conn, cfg, "goal", target=30000, now=NOW)
    assert "Цель" in earnings.handle(conn, cfg, "goal", now=NOW)
    assert "Сколько адоптов" in earnings.handle(conn, cfg, "week", now=NOW, **fakes())
    assert "4" in earnings.handle(conn, cfg, "capacity", per_week=4, now=NOW)
    assert "action" in earnings.handle(conn, cfg, "nope", now=NOW)
    assert "часы" in earnings.handle(conn, cfg, "hours", ref=sid, hours="", now=NOW).lower()
    assert "Не поняла" in earnings.handle(conn, cfg, "hours", ref=sid, hours="3 ч кота", now=NOW)
    assert "Убрала" in earnings.handle(conn, cfg, "hours_undo", now=NOW)        # последняя запись


def test_no_network_or_apple_events_imports():
    import inspect

    src = inspect.getsource(earnings)
    for bad in ("osascript", "her_chrome", "urllib.request", "requests", "subprocess", "httpx"):
        assert bad not in src


def test_commission_slot_from_queue_module(conn):
    from mcp_devian import commissions as cm

    oid = cm.add_offer(conn, kind="ych", title="Moth", price=1500.0, currency="rub", slots=3, deposit_pct=50,
                       hours=4.0, turnaround_days=14, now=NOW - 40 * DAY)
    sid = cm.claim(conn, client="ann", offer_id=oid, now=NOW - 40 * DAY)["slot_id"]
    cm.mark_paid(conn, sid, "deposit", net_amount=700, pay_method="boosty", now=NOW - 39 * DAY)
    cm.mark_paid(conn, sid, "final", net_amount=700, pay_method="boosty", now=NOW - 35 * DAY)
    res = earnings.log_hours(conn, f"comm:{sid}", "5 ч", now=NOW - 34 * DAY)
    assert res["linked"] == "commission"
    w = earnings.works(conn, NOW)[0]
    assert (w.status, w.format, w.income_rub) == ("paid", "ych", 1400)
    deals = earnings._deals(conn, conn.execute("SELECT * FROM sales").fetchall())
    assert len(deals) == 1 and deals[0]["net"] == 1400 and deals[0]["format"] == "ych"


def test_sales_action_maps_tool_params(conn):
    cfg = Config()
    sid = paid(conn, "Dragon", 3000)
    # format="other" — значение по умолчанию инструмента sales, не её уточнение.
    out = earnings.sales_action(conn, cfg, "hours", sale_id=sid, amount=2.5, format="other", now=NOW)
    assert "Записала: 2.5 ч" in out and "аукционы" in out
    out = earnings.sales_action(conn, cfg, "hours", title="YCH base", amount=3, format="ych", status="overhead",
                                now=NOW)
    assert "общая работа" in out
    earnings.sales_action(conn, cfg, "hours", title="Moth WIP", amount=1, now=NOW)
    assert "Привязала" in earnings.sales_action(conn, cfg, "hours", title="Moth WIP", sale_id=sid, now=NOW)
    assert "Убрала" in earnings.sales_action(conn, cfg, "hours", amount=0, now=NOW)
    assert "Нужны часы" in earnings.sales_action(conn, cfg, "hours", title="x", now=NOW)
    assert "Заработок в час" in earnings.sales_action(conn, cfg, "hourly", days=90, now=NOW)
    assert "30,000 ₽" in earnings.sales_action(conn, cfg, "goal", amount=30000, now=NOW)
    out = earnings.sales_action(conn, cfg, "plan", amount=3, now=NOW, **fakes())
    assert out.startswith("Запомнила: 3") and "План на неделю" in out
    assert "целое" in earnings.sales_action(conn, cfg, "plan", amount=2.5, now=NOW, **fakes())
    assert "action" in earnings.sales_action(conn, cfg, "nope", now=NOW)


# --- Регрессии после проверки ---

def test_window_takes_whole_work_not_half(conn):
    # Часы 100 и 85 дней назад, оплата 80 дней назад: окно 90 дн берёт работу целиком, а не 1 ч из 10.
    for i in range(8):
        sid = paid(conn, f"D{i}", 3000, ts=NOW - 80 * DAY)
        earnings.log_hours(conn, sid, 9, now=NOW - 100 * DAY)
        earnings.log_hours(conn, sid, 1, now=NOW - 85 * DAY)
    assert earnings.hourly(conn, NOW, 90).overall.rate == pytest.approx(300)
    assert earnings.hourly(conn, NOW, 180).overall.rate == pytest.approx(300)
    assert earnings.works(conn, NOW, 30) == []               # ни часов, ни оплаты за 30 дн — вне окна


def test_goal_with_zero_net_format_does_not_crash(conn):
    for i in range(9):
        paid(conn, f"Gift {i}", 0, fmt="raffle", ts=NOW - (30 + i) * DAY)
    text = earnings.set_goal(conn, 30000, now=NOW, tz=TZ)       # раньше TypeError в _plural(None)
    assert "ещё ~" not in text and "0 ₽" in text
    st = earnings.goal_status(conn, NOW, TZ)
    assert st["need"] == [] and st["zero"]
    earnings.set_capacity(conn, 2, now=NOW)
    assert "Цель" in earnings.week_plan(conn, Config(), NOW, **fakes())["text"]
    assert "Цель" in earnings.handle(conn, Config(), "goal", now=NOW)


def test_goal_zero_format_does_not_hide_paying_one(conn):
    history(conn, 10, "auction", 4000)
    for i in range(9):
        paid(conn, f"Gift {i}", 0, fmt="raffle", ts=NOW - (30 + i) * DAY)
    st = earnings.goal_status(conn, NOW, TZ) if earnings.set_goal(conn, 30000, now=NOW, tz=TZ) else None
    assert [a["format"] for a in st["need"]] == ["auction"]
    assert "ещё ~" in earnings.render_goal(st)


def test_title_does_not_attach_new_hours_to_old_sale(conn):
    old = paid(conn, "Dragon adopt", 3000, ts=NOW - 150 * DAY)
    earnings.log_hours(conn, old, 3, now=NOW - 150 * DAY)
    res = earnings.log_hours(conn, "Dragon adopt", 5, now=NOW)
    assert res["linked"] == "none" and res["sale_id"] is None
    assert old in res["text"]                                   # подсказывает id, но сама не привязывает
    ws = {w.key: w for w in earnings.works(conn, NOW, 365)}
    assert ws[f"sale:{old}"].hours == 3


def test_title_does_not_attach_to_sale_paid_weeks_ago(conn):
    paid(conn, "Moth", 3000, ts=NOW - 20 * DAY)                 # продана и оплачена 20 дн назад
    assert earnings.log_hours(conn, "Moth", 2, now=NOW)["linked"] == "none"
    fresh = paid(conn, "Fox", 3000, status="won", ts=NOW - 20 * DAY)   # ещё ждёт оплаты — та же работа
    assert earnings.log_hours(conn, "Fox", 2, now=NOW)["sale_id"] == fresh


def test_free_entry_does_not_link_to_far_later_sale(conn):
    earnings.log_hours(conn, "Moth", 4, now=NOW - 200 * DAY)
    paid(conn, "Moth", 5000, ts=NOW - 5 * DAY)
    ws = earnings.works(conn, NOW, 365)
    assert [(w.status, w.income_rub) for w in ws] == [("unsold", 0.0)]
    # а продажа через пару недель после записи — та же работа
    earnings.log_hours(conn, "Wolf", 4, now=NOW - 30 * DAY)
    paid(conn, "Wolf", 5000, ts=NOW - 16 * DAY)
    assert {w.title: w.status for w in earnings.works(conn, NOW, 365)}["Wolf"] == "paid"


def test_capacity_counts_only_adopts(conn):
    for w in range(5):
        for k in range(6):
            db.upsert_item(conn, Item(id=f"da:o{w}{k}", source="deviantart", title=f"sketch {w}{k}", url=f"o{w}{k}",
                                      own=True, lot_type="other", published_ts=NOW - (w * 7 + 1 + k * 0.1) * DAY),
                           now=NOW)
            db.upsert_item(conn, Item(id=f"th:{w}{k}", source="toyhouse", title=f"th {w}{k}", url=f"t{w}{k}",
                                      own=True, lot_type="auction", published_ts=NOW - (w * 7 + 1 + k * 0.1) * DAY),
                           now=NOW)
    conn.commit()
    assert earnings.capacity(conn, NOW)["value"] is None


def test_goal_counts_commission_slot_only_when_fully_paid(conn):
    def slot(i, final_paid):
        for part in ("deposit", "final"):
            done = part == "deposit" or final_paid
            sales.add_sale(conn, title=f"YCH {i}", amount=750, currency="rub", platform="other", format="other",
                           buyer="b", item_id=f"comm:s{i}:{part}", status="paid" if done else "won",
                           now=NOW - (20 + i) * DAY, net_amount=750 if done else None,
                           net_currency="rub" if done else None)

    for i in range(8):
        slot(i, True)
    for i in range(8, 12):
        slot(i, False)
    hist = conn.execute("SELECT * FROM sales WHERE status IN ('paid','transferred','done')").fetchall()
    deals = earnings._deals(conn, hist)
    assert len(deals) == 8 and all(d["net"] == 1500 for d in deals)
    earnings.set_goal(conn, 30000, now=NOW, tz=TZ)
    need = earnings.goal_status(conn, NOW, TZ)["need"]
    assert need and need[0]["mean"] == pytest.approx(1500)


def test_week_plan_asks_with_existing_action(conn):
    plan = earnings.week_plan(conn, Config(), NOW, **fakes())
    assert "sales plan amount=N" in plan["ask"]
    assert "capacity" not in plan["ask"]


def test_slot_times_never_repeat():
    weekend = {"slot": ("weekend", 3)}
    ts = earnings.slot_times(weekend, NOW, TZ, 7)
    assert len(ts) == len(set(ts)) == 6                         # 2 выходных × 3 места — больше не ставим
    assert len(earnings.slot_times(BEST, NOW, TZ, 3)) == 3


def test_week_plan_reports_slots_that_did_not_fit(conn):
    earnings.set_capacity(conn, 9, now=NOW)
    best = {**BEST, "slot": ("weekend", 3), "when": "в выходные вечером"}
    plan = earnings.week_plan(conn, Config(), NOW, **fakes(best=best))
    assert len(plan["slots"]) == 6 and "ещё 3 — когда удобно" in plan["text"]


def test_rub_price_without_net_is_not_called_foreign(conn):
    for i in range(8):
        work(conn, f"M{i}", 3000, 3, fmt="flatsale", ts=NOW - (40 + i) * DAY)
    work(conn, "Rub gross", None, 3, fmt="flatsale", currency="rub", amount=1500, ts=NOW - 40 * DAY)
    text = earnings.render_hourly(earnings.hourly(conn, NOW))
    assert "не в ₽" not in text and "не вписано, сколько дошло" in text and "1,500 ₽" in text
    paid(conn, "Jan rub", None, currency="rub", amount=1500, ts=NOW - DAY)
    earnings.set_goal(conn, 30000, now=NOW, tz=TZ)
    goal = earnings.render_goal(earnings.goal_status(conn, NOW, TZ))
    assert "без суммы в ₽ (1,500 ₽)" not in goal
    assert "1 оплаченная продажа, где не вписано" in goal


def test_undo_by_work_title(conn):
    sid = paid(conn, "Dragon", 3000, ts=NOW - DAY)
    earnings.log_hours(conn, sid, 2, now=NOW - 3600)
    earnings.log_hours(conn, "Moth WIP", 1, now=NOW)
    assert "Dragon" in earnings.sales_action(conn, Config(), "hours", title="dragon", amount=0, now=NOW)
    assert [w.title for w in earnings.works(conn, NOW)] == ["Moth WIP"]
    assert "Такой записи" in earnings.sales_action(conn, Config(), "hours", title="nothing", amount=0, now=NOW)


def test_retry_does_not_double_hours(conn):
    sid = paid(conn, "Dragon", 3000, ts=NOW - DAY)
    first = earnings.sales_action(conn, Config(), "hours", sale_id=sid, amount=3, now=NOW)
    again = earnings.sales_action(conn, Config(), "hours", sale_id=sid, amount=3, now=NOW + 20)
    assert "Записала" in first and "уже записано" in again
    assert conn.execute("SELECT COUNT(*) FROM work_log").fetchone()[0] == 1
    later = earnings.log_hours(conn, sid, 3, now=NOW + 600)       # через 10 минут — это уже новая запись
    assert later["total_hours"] == pytest.approx(6)


def test_ending_line_day_precision_not_today(conn):
    earnings.set_capacity(conn, 1, now=NOW)
    ending = [{"item_id": "da:2", "title": "Moth", "end_ts": NOW + 4 * DAY, "precision": "day",
               "text": "«Moth» сегодня последний день (время не указано): ставок пока нет"},
              {"item_id": "da:3", "title": "Fox", "end_ts": NOW + DAY, "precision": "minute"}]
    text = earnings.week_plan(conn, Config(), NOW, **fakes(ending=ending))["text"]
    assert "сегодня последний день" not in text
    assert "«Moth» — до конца 19.01" in text
    assert "«Fox» — закончится сб 16.01 11:00" in text


def test_goal_status_reuses_hourly_report(conn, monkeypatch):
    seed_formats(conn)
    rep = earnings.hourly(conn, NOW)
    monkeypatch.setattr(earnings, "hourly", lambda *a, **k: (_ for _ in ()).throw(AssertionError("пересчёт")))
    assert earnings.goal_status(conn, NOW, TZ, rep=rep)["rate"] is rep.overall
