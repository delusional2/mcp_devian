"""YCH и коммишены: предложения, очередь слотов, предоплата, сроки, напоминания, загрузка и связь с продажами."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_devian import commissions as cm
from mcp_devian import db, sales

DAY = 86400
NOW = 1_800_000_000.0   # 15.01.2027
TZ = "Europe/Moscow"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "c.sqlite3")
    yield c
    c.close()


def ych(conn, slots=3, price=1500.0, hours=4.0, deposit_pct=50):
    return cm.add_offer(conn, kind="ych", title="Moth YCH", price=price, currency="rub", slots=slots,
                        deposit_pct=deposit_pct, hours=hours, turnaround_days=14, now=NOW)


def comm_sales(conn):
    return conn.execute("SELECT * FROM sales WHERE source = 'commissions' ORDER BY created_ts, id").fetchall()


# --- Предложения ---

def test_offer_validation(conn):
    with pytest.raises(cm.CommissionError):
        cm.add_offer(conn, kind="adopt", title="x", price=10, now=NOW)
    with pytest.raises(cm.CommissionError):
        cm.add_offer(conn, kind="ych", title="  ", price=10, now=NOW)
    with pytest.raises(cm.CommissionError):
        cm.add_offer(conn, kind="ych", title="x", price=-1, now=NOW)
    with pytest.raises(cm.CommissionError):
        cm.add_offer(conn, kind="ych", title="x", price=10, deposit_pct=120, now=NOW)
    with pytest.raises(cm.CommissionError):
        cm.add_offer(conn, kind="ych", title="x", price=10, currency="btc", now=NOW)
    with pytest.raises(cm.CommissionError):
        cm.add_offer(conn, kind="ych", title="x", price=10, slots=0, now=NOW)


def test_ych_slots_run_out_and_closed_offer_refuses(conn):
    oid = ych(conn, slots=2)
    cm.claim(conn, client="ann", offer_id=oid, now=NOW)
    assert cm.slots_left(conn, oid) == 1
    cm.claim(conn, client="bob", offer_id=oid, now=NOW)
    assert cm.slots_left(conn, oid) == 0
    with pytest.raises(cm.CommissionError, match="мест"):
        cm.claim(conn, client="cat", offer_id=oid, now=NOW)
    oid2 = ych(conn)
    cm.close_offer(conn, oid2)
    with pytest.raises(cm.CommissionError, match="закрыт"):
        cm.claim(conn, client="cat", offer_id=oid2, now=NOW)


def test_cancelled_slot_frees_place(conn):
    oid = ych(conn, slots=1)
    sid = cm.claim(conn, client="ann", offer_id=oid, now=NOW)["slot_id"]
    cm.cancel(conn, sid, refund_deposit=True, now=NOW)
    assert cm.slots_left(conn, oid) == 1


def test_claim_takes_offer_defaults(conn):
    oid = ych(conn, price=2000, deposit_pct=30)
    sid = cm.claim(conn, client="@Ann", offer_id=oid, now=NOW)["slot_id"]
    s = cm.get_slot(conn, sid)
    assert (s["kind"], s["price"], s["currency"], s["deposit_pct"], s["hours"]) == ("ych", 2000, "rub", 30, 4.0)
    assert s["client"] == "Ann"
    assert s["status"] == "claimed"
    # Срок по умолчанию — от срока выполнения в предложении.
    assert s["deadline_ts"] and s["deadline_ts"] > NOW + 13 * DAY


def test_commission_without_offer_needs_price(conn):
    with pytest.raises(cm.CommissionError, match="цен"):
        cm.claim(conn, client="ann", kind="commission", what="bust", now=NOW)
    sid = cm.claim(conn, client="ann", kind="commission", what="bust", price=30, currency="usd", now=NOW)["slot_id"]
    assert cm.get_slot(conn, sid)["deposit_pct"] == cm.DEFAULT_DEPOSIT_PCT


# --- Сроки ---

def test_parse_deadline_forms():
    tz = ZoneInfo(TZ)
    ts = cm.parse_deadline("2027-02-01", now=NOW, tz=TZ)
    d = datetime.fromtimestamp(ts, tz)
    assert (d.year, d.month, d.day, d.hour, d.minute) == (2027, 2, 1, 23, 59)
    assert datetime.fromtimestamp(cm.parse_deadline("01.02.2027", now=NOW, tz=TZ), tz).day == 1
    # «01.02» без года — ближайшее будущее 1 февраля.
    assert datetime.fromtimestamp(cm.parse_deadline("01.02", now=NOW, tz=TZ), tz).year == 2027
    assert datetime.fromtimestamp(cm.parse_deadline("10.01", now=NOW, tz=TZ), tz).year == 2028
    assert abs(cm.parse_deadline("10", now=NOW, tz=TZ) - (NOW + 10 * DAY)) < DAY
    assert cm.parse_deadline("", now=NOW, tz=TZ) is None
    with pytest.raises(cm.CommissionError):
        cm.parse_deadline("завтра-послезавтра", now=NOW, tz=TZ)
    with pytest.raises(cm.CommissionError, match="прош"):
        cm.parse_deadline("2026-01-01", now=NOW, tz=TZ)


# --- Статусы и деньги ---

def test_status_flow_and_forbidden_jumps(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn), now=NOW)["slot_id"]
    with pytest.raises(cm.CommissionError):
        cm.set_status(conn, sid, "delivered", now=NOW)
    with pytest.raises(cm.CommissionError):
        cm.set_status(conn, sid, "nonsense", now=NOW)
    for st in ("deposit", "in_progress", "wip_sent", "final_paid", "delivered"):
        cm.set_status(conn, sid, st, now=NOW + DAY)
    assert cm.get_slot(conn, sid)["status"] == "delivered"
    with pytest.raises(cm.CommissionError):
        cm.cancel(conn, sid, now=NOW)


def test_start_before_deposit_warns(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn), now=NOW)["slot_id"]
    warnings = cm.set_status(conn, sid, "in_progress", now=NOW)
    assert any("предоплат" in w for w in warnings)


def test_deposit_and_final_go_to_sales_once(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn, price=1500), platform="toyhouse", now=NOW)["slot_id"]
    cm.mark_paid(conn, sid, "deposit", net_amount=700, pay_method="boosty", now=NOW + DAY)
    cm.mark_paid(conn, sid, "deposit", now=NOW + DAY)          # повтор — не дубль
    cm.set_status(conn, sid, "deposit", now=NOW + DAY)         # и через статус — не дубль
    rows = comm_sales(conn)
    assert len(rows) == 1
    dep = rows[0]
    assert (dep["amount"], dep["currency"], dep["status"], dep["buyer"], dep["platform"]) == (750, "rub", "paid",
                                                                                              "ann", "toyhouse")
    assert (dep["net_amount"], dep["pay_method"]) == (700, "boosty")
    assert dep["item_id"] == f"comm:{sid}:deposit"
    assert cm.get_slot(conn, sid)["status"] == "deposit"
    cm.set_status(conn, sid, "in_progress", now=NOW + 2 * DAY)
    cm.set_status(conn, sid, "wip_sent", now=NOW + 3 * DAY)
    cm.mark_paid(conn, sid, "final", now=NOW + 5 * DAY)
    rows = comm_sales(conn)
    assert [r["amount"] for r in rows] == [750, 750]
    assert cm.get_slot(conn, sid)["status"] == "final_paid"
    cm.set_status(conn, sid, "delivered", now=NOW + 6 * DAY)
    assert {r["status"] for r in comm_sales(conn)} == {"done"}
    # Доход считается в обычном sales.income, в рублях.
    assert sales.income(conn, NOW, NOW + 10 * DAY)["rub"]["total"] == 1500


def test_format_kept_when_sales_knows_it(conn, monkeypatch):
    monkeypatch.setattr(sales, "FORMATS", (*sales.FORMATS, "ych", "commission"))
    sid = cm.claim(conn, client="ann", offer_id=ych(conn), now=NOW)["slot_id"]
    cm.mark_paid(conn, sid, "deposit", now=NOW)
    assert comm_sales(conn)[0]["format"] == "ych"


def test_no_deposit_commission_single_payment(conn):
    sid = cm.claim(conn, client="ann", kind="commission", what="headshot", price=20, currency="usd",
                   deposit_pct=0, now=NOW)["slot_id"]
    assert cm.set_status(conn, sid, "in_progress", now=NOW) == []
    with pytest.raises(cm.CommissionError, match="предоплат"):
        cm.mark_paid(conn, sid, "deposit", now=NOW)
    cm.mark_paid(conn, sid, "final", now=NOW + DAY)
    rows = comm_sales(conn)
    assert len(rows) == 1 and rows[0]["amount"] == 20 and rows[0]["currency"] == "usd"


def test_full_prepay_has_no_final_sale(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn, price=1000, deposit_pct=100), now=NOW)["slot_id"]
    cm.mark_paid(conn, sid, "deposit", now=NOW)
    for st in ("in_progress", "wip_sent", "final_paid", "delivered"):
        cm.set_status(conn, sid, st, now=NOW + DAY)
    rows = comm_sales(conn)
    assert len(rows) == 1 and rows[0]["amount"] == 1000 and rows[0]["status"] == "done"


def test_step_back_cancels_and_restores_sale(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn), now=NOW)["slot_id"]
    cm.set_status(conn, sid, "deposit", now=NOW)
    cm.set_status(conn, sid, "claimed", now=NOW)               # ошиблась: предоплаты не было
    assert comm_sales(conn)[0]["status"] == "cancelled"
    assert "rub" not in sales.income(conn, NOW - DAY, NOW + DAY)
    cm.set_status(conn, sid, "deposit", now=NOW + DAY)          # а теперь пришла
    rows = comm_sales(conn)
    assert len(rows) == 1 and rows[0]["status"] == "paid"


def test_cancel_keeps_or_refunds_deposit(conn):
    oid = ych(conn)
    keep = cm.claim(conn, client="ann", offer_id=oid, now=NOW)["slot_id"]
    refund = cm.claim(conn, client="bob", offer_id=oid, now=NOW)["slot_id"]
    for sid in (keep, refund):
        cm.mark_paid(conn, sid, "deposit", now=NOW)
    cm.cancel(conn, keep, refund_deposit=False, now=NOW + DAY)
    cm.cancel(conn, refund, refund_deposit=True, now=NOW + DAY)
    by_buyer = {r["buyer"]: r["status"] for r in comm_sales(conn)}
    assert by_buyer == {"ann": "done", "bob": "cancelled"}
    assert cm.get_slot(conn, keep)["status"] == "cancelled"


def test_net_validation_comes_from_sales(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn), now=NOW)["slot_id"]
    with pytest.raises(cm.CommissionError):
        cm.mark_paid(conn, sid, "deposit", net_amount=-5, now=NOW)
    assert comm_sales(conn) == []
    assert cm.get_slot(conn, sid)["status"] == "claimed"


# --- Напоминания ---

def test_reminders(conn):
    oid = ych(conn)
    unpaid = cm.claim(conn, client="ann", offer_id=oid, now=NOW)["slot_id"]
    soon = cm.claim(conn, client="bob", offer_id=oid, deadline="2", now=NOW)["slot_id"]
    cm.mark_paid(conn, soon, "deposit", now=NOW)
    wip = cm.claim(conn, client="cat", kind="commission", price=20, currency="usd", deadline="40", now=NOW)["slot_id"]
    cm.mark_paid(conn, wip, "deposit", now=NOW)
    cm.set_status(conn, wip, "in_progress", now=NOW)
    cm.set_status(conn, wip, "wip_sent", now=NOW)
    later = NOW + 6 * DAY
    kinds = {(r["slot_id"], r["kind"]) for r in cm.reminders(conn, now=later)}
    assert (unpaid, "deposit_unpaid") in kinds
    assert (soon, "overdue") in kinds
    assert (wip, "final_unpaid") in kinds
    assert all(r["text"] for r in cm.reminders(conn, now=later))
    # Рано — ничего про предоплату и остаток.
    early = {r["kind"] for r in cm.reminders(conn, now=NOW + 3600)}
    assert "deposit_unpaid" not in early and "final_unpaid" not in early
    assert (soon, "deadline_soon") in {(r["slot_id"], r["kind"]) for r in cm.reminders(conn, now=NOW + 3600)}


def test_final_paid_not_delivered_reminder(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn, deposit_pct=0), now=NOW)["slot_id"]
    cm.set_status(conn, sid, "in_progress", now=NOW)
    cm.mark_paid(conn, sid, "final", now=NOW)
    assert "deliver" in {r["kind"] for r in cm.reminders(conn, now=NOW + 3 * DAY)}


def test_pending_reminders_dedup(conn):
    cm.claim(conn, client="ann", offer_id=ych(conn), now=NOW)
    first = cm.pending_reminders(conn, now=NOW + 5 * DAY)
    assert first
    assert cm.pending_reminders(conn, now=NOW + 5 * DAY + 3600) == []
    # Без mark — просто показать, повторять можно.
    assert cm.pending_reminders(conn, now=NOW + 6 * DAY, mark=False) == []


def test_delivered_and_cancelled_never_remind(conn):
    oid = ych(conn, deposit_pct=0)
    a = cm.claim(conn, client="ann", offer_id=oid, deadline="1", now=NOW)["slot_id"]
    cm.cancel(conn, a, now=NOW)
    assert cm.reminders(conn, now=NOW + 30 * DAY) == []


# --- Загрузка ---

def test_capacity_and_overbook_refusal(conn):
    oid = ych(conn, slots=10, hours=6)
    cm.claim(conn, client="ann", offer_id=oid, deadline="7", now=NOW, weekly_hours=10)
    cap = cm.capacity(conn, weekly_hours=10, now=NOW)
    assert cap["ok"] and cap["must_this_week"] == pytest.approx(6)
    assert cap["free_this_week"] == pytest.approx(4)
    with pytest.raises(cm.CommissionError, match="(?i)не успе"):
        cm.claim(conn, client="bob", offer_id=oid, deadline="7", now=NOW, weekly_hours=10)
    # С более поздним сроком влезает.
    cm.claim(conn, client="bob", offer_id=oid, deadline="14", now=NOW, weekly_hours=10)
    # force — её решение взять всё равно, но с предупреждением.
    got = cm.claim(conn, client="cat", offer_id=oid, deadline="7", now=NOW, weekly_hours=10, force=True)
    assert any("не успе" in w.lower() for w in got["warnings"])
    assert not cm.capacity(conn, weekly_hours=10, now=NOW)["ok"]


def test_capacity_unknown_without_hours_or_setting(conn):
    oid = ych(conn, hours=None)
    cm.claim(conn, client="ann", offer_id=oid, now=NOW)
    cap = cm.capacity(conn, weekly_hours=10, now=NOW)
    assert cap["unknown_hours"] == 1
    assert cm.capacity(conn, weekly_hours=0, now=NOW)["ok"] is None
    # Без настройки часов в неделю проверка не мешает брать слоты.
    cm.claim(conn, client="bob", offer_id=ych(conn, hours=50), deadline="2", now=NOW, weekly_hours=0)


def test_wip_counts_less_and_hours_left_overrides(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn, hours=8, deposit_pct=0), deadline="30", now=NOW)["slot_id"]
    cm.set_status(conn, sid, "in_progress", now=NOW)
    cm.set_status(conn, sid, "wip_sent", now=NOW)
    assert cm.capacity(conn, weekly_hours=10, now=NOW)["hours_left"] == pytest.approx(2)
    cm.set_hours_left(conn, sid, 5)
    assert cm.capacity(conn, weekly_hours=10, now=NOW)["hours_left"] == pytest.approx(5)


def test_committed_hours_for_week_planner(conn):
    oid = ych(conn, slots=5, hours=6)
    cm.claim(conn, client="ann", offer_id=oid, deadline="5", now=NOW)
    cm.claim(conn, client="bob", offer_id=oid, deadline="20", now=NOW)
    assert cm.committed_hours(conn, NOW, NOW + 7 * DAY) == pytest.approx(6)
    assert cm.committed_hours(conn, NOW, NOW + 21 * DAY) == pytest.approx(12)


# --- Статистика с опорой ---

def test_stats_refuse_below_threshold(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn, deposit_pct=0), now=NOW)["slot_id"]
    for st in ("in_progress", "final_paid", "delivered"):
        cm.set_status(conn, sid, st, now=NOW + DAY)
    text = cm.render_stats(conn, now=NOW + 2 * DAY)
    assert "Вывода нет" in text


def test_stats_with_enough_data(conn):
    oid = ych(conn, slots=20, deposit_pct=0)
    for i in range(10):
        sid = cm.claim(conn, client=f"c{i}", offer_id=oid, deadline="10", now=NOW)["slot_id"]
        cm.set_status(conn, sid, "in_progress", now=NOW)
        cm.set_status(conn, sid, "final_paid", now=NOW)
        cm.set_status(conn, sid, "delivered", now=NOW + (5 if i < 8 else 15) * DAY)
    text = cm.render_stats(conn, now=NOW + 20 * DAY)
    assert "n=10" in text and "её продажи" in text and "уверенность" in text
    assert "8 из 10" in text


# --- Анонс: только факты, текст пишет она ---

def test_announcement_facts_no_generated_text(conn):
    oid = ych(conn, slots=3)
    cm.claim(conn, client="ann", offer_id=oid, now=NOW)
    f = cm.announcement_facts(conn, oid, platform="deviantart")
    assert f["slots_left"] == 2 and f["slots_total"] == 3
    assert f["price"] == "1,500 ₽" and f["deposit_pct"] == 50
    assert "toyhouse_rule" not in f
    th = cm.announcement_facts(conn, oid, platform="toyhouse")
    assert "4.8" in th["toyhouse_rule"]
    text = cm.render_announcement(th)
    assert "своими словами" in text and "4.8" in text


# --- Виды и обработчик ---

def test_queue_view_compact(conn, tmp_path):
    empty = db.connect(tmp_path / "empty.sqlite3")
    assert "Занятых слотов пока нет" in cm.render_queue(empty, now=NOW, tz=TZ)
    empty.close()
    oid = ych(conn)
    sid = cm.claim(conn, client="ann", offer_id=oid, deadline="2027-01-20", now=NOW)["slot_id"]
    text = cm.render_queue(conn, now=NOW, tz=TZ)
    assert sid in text and "ann" in text and "ждёт предоплату" in text and "20.01" in text


def test_handle_dispatcher(conn):
    st = cm.Settings(weekly_hours=20, tz=TZ)
    out = cm.handle(conn, "offer", settings=st, now=NOW, kind="ych", title="Fox YCH", price=900, currency="rub",
                    slots=2, hours=3)
    oid = out.split("[", 1)[1].split("]", 1)[0]
    assert "Fox YCH" in cm.handle(conn, "offers", settings=st, now=NOW)
    out = cm.handle(conn, "claim", settings=st, now=NOW, id=oid, client="ann", deadline="10")
    sid = cm.list_slots(conn)[0]["id"]
    assert sid in out
    assert "Предоплата" in cm.handle(conn, "paid", settings=st, now=NOW, id=sid, part="deposit", net=430)
    assert "в работе" in cm.handle(conn, "status", settings=st, now=NOW, id=sid, status="in_progress")
    assert "ann" in cm.handle(conn, "queue", settings=st, now=NOW)
    assert "часов" in cm.handle(conn, "capacity", settings=st, now=NOW)
    assert "Нужно" in cm.handle(conn, "claim", settings=st, now=NOW)
    assert "Не вышло" in cm.handle(conn, "status", settings=st, now=NOW, id="nope", status="delivered")
    assert "Действия" in cm.handle(conn, "bogus", settings=st, now=NOW)


def test_settings_from_config_tolerates_missing_fields():
    class P:
        timezone = "Asia/Yekaterinburg"

    class Cfg:
        posting = P()

    s = cm.settings_from(Cfg())
    assert s.tz == "Asia/Yekaterinburg" and s.weekly_hours == 0 and s.deposit_days == 3


def test_tables_are_own_and_idempotent(conn):
    cm.ensure_tables(conn)
    cm.ensure_tables(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"comm_offers", "comm_slots", "comm_reminded"} <= names


def test_pay_all_at_once_records_both_parts(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn, price=1000), now=NOW)["slot_id"]
    got = cm.mark_paid(conn, sid, "final", net_amount=900, now=NOW)
    assert got["status"] == "final_paid" and got["warnings"]
    rows = comm_sales(conn)
    assert sorted(r["amount"] for r in rows) == [500, 500]
    assert sum(r["net_amount"] or 0 for r in rows) == 900


def test_undo_delivered_restores_paid_sales(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn, deposit_pct=0), now=NOW)["slot_id"]
    for st in ("in_progress", "final_paid", "delivered"):
        cm.set_status(conn, sid, st, now=NOW)
    cm.set_status(conn, sid, "final_paid", now=NOW)
    assert comm_sales(conn)[0]["status"] == "paid"
    cm.set_status(conn, sid, "in_progress", now=NOW)           # оплату отметила по ошибке
    assert comm_sales(conn)[0]["status"] == "cancelled"
    assert cm.get_slot(conn, sid)["final_paid_ts"] is None


def test_refunded_then_reopened_slot_needs_deposit_again(conn):
    sid = cm.claim(conn, client="ann", offer_id=ych(conn), now=NOW)["slot_id"]
    cm.mark_paid(conn, sid, "deposit", now=NOW)
    cm.cancel(conn, sid, refund_deposit=True, now=NOW)
    cm.set_status(conn, sid, "claimed", now=NOW)
    assert cm.get_slot(conn, sid)["deposit_paid_ts"] is None
    cm.mark_paid(conn, sid, "deposit", now=NOW + DAY)
    rows = comm_sales(conn)
    assert len(rows) == 1 and rows[0]["status"] == "paid"


def test_x_platform_goes_to_sales_as_other(conn):
    sid = cm.claim(conn, client="ann", kind="commission", price=10, currency="usd", platform="x", now=NOW)["slot_id"]
    cm.mark_paid(conn, sid, "deposit", now=NOW)
    assert comm_sales(conn)[0]["platform"] == "other"


def test_handle_other_actions(conn):
    st = cm.Settings(tz=TZ)
    assert "пока нет" in cm.handle(conn, "offers", settings=st, now=NOW)
    oid = cm.add_offer(conn, kind="ych", title="Cat YCH", price=500, slots=2, turnaround_days=10, now=NOW)
    assert "закрыто" in cm.handle(conn, "close", settings=st, now=NOW, id=oid)
    assert "Не вышло" in cm.handle(conn, "claim", settings=st, now=NOW, id=oid, client="ann")
    assert "открыто" in cm.handle(conn, "open", settings=st, now=NOW, id=oid)
    cm.handle(conn, "claim", settings=st, now=NOW, id=oid, client="ann")
    sid = cm.list_slots(conn)[0]["id"]
    assert "01.02" in cm.handle(conn, "deadline", settings=st, now=NOW, id=sid, deadline="01.02")
    assert "~3 ч" in cm.handle(conn, "hours", settings=st, now=NOW, id=sid, hours=3)
    assert "Скажи, сколько часов" in cm.handle(conn, "capacity", settings=st, now=NOW)
    assert "спокойно" in cm.handle(conn, "remind", settings=st, now=NOW)
    assert "предоплата" in cm.handle(conn, "remind", settings=st, now=NOW + 4 * DAY)
    assert "Вывода нет" in cm.handle(conn, "stats", settings=st, now=NOW)
    text = cm.handle(conn, "announce", settings=st, now=NOW, id=oid, platform="x")
    assert "мест: 1 из 2" in text and "переведу" in text and "«го»" in text
    assert "вернула" in cm.handle(conn, "cancel", settings=st, now=NOW, id=sid, refund=True)


def test_overdue_shown_in_queue(conn):
    cm.claim(conn, client="ann", offer_id=ych(conn), deadline="2", now=NOW)
    assert "просрочен" in cm.render_queue(conn, now=NOW + 5 * DAY, tz=TZ)


# --- Регрессии после проверки ---

def test_toyhouse_offer_never_offers_translation(conn):
    oid = cm.add_offer(conn, kind="ych", title="TH YCH", price=1000, platform="toyhouse", now=NOW)
    # Площадка не названа — берём площадку предложения, а не DA.
    text = cm.handle(conn, "announce", now=NOW, id=oid)
    assert "4.8" in text and "переведу" not in text
    # Регистр и короткие имена — та же Toyhouse.
    for p in ("Toyhouse", " TOYHOUSE ", "th"):
        f = cm.announcement_facts(conn, oid, p)
        assert f["platform"] == "toyhouse" and f["translation_allowed"] is False and "4.8" in f["toyhouse_rule"]
    # Предложение на Toyhouse, анонс просят «для DA» — перевод всё равно не предлагаем: работа та же.
    f = cm.announcement_facts(conn, oid, "deviantart")
    assert f["translation_allowed"] is False and "toyhouse_rule" in f
    assert "переведу" not in cm.render_announcement(f)
    with pytest.raises(cm.CommissionError, match="Площадка"):
        cm.announcement_facts(conn, oid, "tumblr")


def test_translation_only_for_da_and_x(conn):
    da = cm.add_offer(conn, kind="ych", title="DA YCH", price=1000, platform="DA", now=NOW)
    assert cm.get_offer(conn, da)["platform"] == "deviantart"
    assert "переведу" in cm.handle(conn, "announce", now=NOW, id=da)
    assert "переведу" in cm.handle(conn, "announce", now=NOW, id=da, platform="Twitter")
    other = cm.add_offer(conn, kind="ych", title="Other", price=1000, now=NOW)
    assert "переведу" not in cm.handle(conn, "announce", now=NOW, id=other)


def test_cancel_after_undone_deposit_is_not_income(conn):
    sid = cm.claim(conn, client="ann", kind="commission", price=1000, now=NOW)["slot_id"]
    cm.set_status(conn, sid, "deposit", now=NOW)
    cm.set_status(conn, sid, "claimed", now=NOW)                # ошиблась: денег не было
    cm.cancel(conn, sid, now=NOW + DAY)                          # по умолчанию refund=False
    assert [r["status"] for r in comm_sales(conn)] == ["cancelled"]
    assert "rub" not in sales.income(conn, NOW - DAY, NOW + 2 * DAY)


def test_cancel_after_undone_final_is_not_income(conn):
    sid = cm.claim(conn, client="ann", kind="commission", price=1000, deposit_pct=0, now=NOW)["slot_id"]
    for st in ("in_progress", "final_paid", "in_progress"):
        cm.set_status(conn, sid, st, now=NOW)
    cm.cancel(conn, sid, now=NOW + DAY)
    assert [r["status"] for r in comm_sales(conn)] == ["cancelled"]
    assert "rub" not in sales.income(conn, NOW - DAY, NOW + 2 * DAY)


def test_overdue_slot_does_not_block_unrelated_claim(conn):
    old = cm.claim(conn, client="ann", kind="commission", price=10, hours=2, deadline="2", now=NOW)["slot_id"]
    later = NOW + 5 * DAY
    got = cm.claim(conn, client="bob", kind="commission", price=10, hours=1, deadline="60", now=later,
                   weekly_hours=40)
    assert got["slot_id"]
    cap = cm.capacity(conn, weekly_hours=40, now=later)
    assert cap["ok"] and [o["slot_id"] for o in cap["overdue"]] == [old]
    assert "прошёл" in cm.render_capacity(cap, TZ)
    # Новый слот, который сам не влезает, по-прежнему не берём — и в отказе назван он, а не старый.
    with pytest.raises(cm.CommissionError, match="новому слоту"):
        cm.claim(conn, client="cat", kind="commission", price=10, hours=30, deadline="3", now=later,
                 weekly_hours=40)


def test_existing_conflict_warns_but_does_not_block(conn):
    oid = ych(conn, slots=10, hours=20)
    cm.claim(conn, client="ann", offer_id=oid, deadline="7", now=NOW, weekly_hours=10, force=True)
    got = cm.claim(conn, client="bob", kind="commission", price=10, hours=1, deadline="60", now=NOW, weekly_hours=10)
    assert any("уже взятое" in w for w in got["warnings"])


def test_full_prepay_paid_as_final_keeps_net(conn):
    oid = cm.add_offer(conn, kind="ych", title="Y", price=1000, deposit_pct=100, now=NOW)
    sid = cm.claim(conn, client="ann", offer_id=oid, now=NOW)["slot_id"]
    text = cm.handle(conn, "paid", now=NOW, id=sid, part="final", net=880)
    assert text.startswith("Оплата 1,000 ₽ записана") and "к остатку" not in text
    rows = comm_sales(conn)
    assert len(rows) == 1 and (rows[0]["amount"], rows[0]["net_amount"]) == (1000, 880)
    assert cm.get_slot(conn, sid)["status"] == "final_paid"


def test_pay_all_at_once_in_usd_splits_net(conn):
    sid = cm.claim(conn, client="ann", kind="commission", price=40, currency="usd", now=NOW)["slot_id"]
    got = cm.mark_paid(conn, sid, "final", net_amount=3001, now=NOW)
    assert got["parts"] == ["deposit", "final"] and got["amount"] == 40
    rows = comm_sales(conn)
    assert all(r["net_amount"] is not None for r in rows)
    assert sum(r["net_amount"] for r in rows) == pytest.approx(3001)
    assert sales.received(conn, NOW - 1, NOW + 1)["missing"] == {}
    assert "не вписано" not in sales.render_income(conn, 7, NOW + 1)


def test_committed_hours_matches_capacity(conn):
    cm.claim(conn, client="ann", kind="commission", price=1, hours=20, deadline="9", now=NOW)
    must = cm.capacity(conn, weekly_hours=10, now=NOW)["must_this_week"]
    assert must == pytest.approx(20 - 10 * 2 / 7, abs=0.05)
    assert cm.committed_hours(conn, NOW, NOW + 7 * DAY, weekly_hours=10, now=NOW) == pytest.approx(must)
    # Следующая неделя: до её начала успеет 10 ч, остальное ляжет на неё.
    nxt = cm.committed_hours(conn, NOW + 7 * DAY, NOW + 14 * DAY, weekly_hours=10, now=NOW)
    assert nxt == pytest.approx(10)


def test_reopen_respects_slot_limit_and_closed_offer(conn):
    oid = ych(conn, slots=1)
    a = cm.claim(conn, client="ann", offer_id=oid, now=NOW)["slot_id"]
    cm.cancel(conn, a, now=NOW)
    cm.claim(conn, client="bob", offer_id=oid, now=NOW)
    with pytest.raises(cm.CommissionError, match="мест"):
        cm.set_status(conn, a, "claimed", now=NOW)
    oid2 = ych(conn, slots=2)
    b = cm.claim(conn, client="cat", offer_id=oid2, now=NOW)["slot_id"]
    cm.cancel(conn, b, now=NOW)
    cm.close_offer(conn, oid2)
    with pytest.raises(cm.CommissionError, match="закрыт"):
        cm.set_status(conn, b, "claimed", now=NOW)
    assert len(cm.list_slots(conn, active_only=True)) == 1


def test_reopen_with_kept_deposit_is_not_waiting_for_deposit(conn):
    sid = cm.claim(conn, client="ann", kind="commission", price=1000, now=NOW)["slot_id"]
    cm.mark_paid(conn, sid, "deposit", now=NOW)
    cm.cancel(conn, sid, now=NOW)                                # предоплату оставила
    warnings = cm.set_status(conn, sid, "claimed", now=NOW)
    s = cm.get_slot(conn, sid)
    assert s["status"] == "deposit" and warnings
    assert "ждёт предоплату" not in cm.render_queue(conn, NOW, TZ)
    assert [r["status"] for r in comm_sales(conn)] == ["paid"]
    assert "deposit_unpaid" not in {r["kind"] for r in cm.reminders(conn, NOW + 10 * DAY)}


def test_settings_from_tolerates_none_and_junk():
    class P:
        timezone = "Europe/Moscow"
        commission_deposit_days = None
        commission_weekly_hours = "abc"
        commission_deposit_pct = 250
        commission_final_days = -3

    class Cfg:
        posting = P()

    s = cm.settings_from(Cfg())
    assert (s.deposit_days, s.weekly_hours, s.deposit_pct, s.final_days) == (3, 0, 100, 0)


def test_input_edge_cases(conn):
    oid = cm.add_offer(conn, kind="YCH", title="x", price=100, currency=" RUB ", now=NOW)
    assert cm.get_offer(conn, oid)["currency"] == "rub" and cm.get_offer(conn, oid)["kind"] == "ych"
    with pytest.raises(cm.CommissionError, match="Мест"):
        cm.add_offer(conn, kind="ych", title="x", price=1, slots=True, now=NOW)
    with pytest.raises(cm.CommissionError, match="Цена"):
        cm.add_offer(conn, kind="ych", title="x", price=True, now=NOW)
    sid = cm.claim(conn, client="ann", kind="commission", price=5, currency="USD", now=NOW)["slot_id"]
    assert cm.get_slot(conn, sid)["currency"] == "usd"
    # 29.02 без года — ближайший високосный (2028).
    d = datetime.fromtimestamp(cm.parse_deadline("29.02", now=NOW, tz=TZ), ZoneInfo(TZ))
    assert (d.year, d.month, d.day) == (2028, 2, 29)
    with pytest.raises(cm.CommissionError, match="даты нет"):
        cm.parse_deadline("31.04", now=NOW, tz=TZ)


def test_deadline_can_be_cleared(conn):
    sid = cm.claim(conn, client="ann", kind="commission", price=5, deadline="10", now=NOW)["slot_id"]
    assert "Нужно" in cm.handle(conn, "deadline", now=NOW, id=sid)
    assert "снят" in cm.handle(conn, "deadline", now=NOW, id=sid, deadline="нет")
    assert cm.get_slot(conn, sid)["deadline_ts"] is None


def test_points_payment_gets_da_points_warnings(conn):
    sid = cm.claim(conn, client="ann", kind="ych", price=10000, currency="points", now=NOW)["slot_id"]
    got = cm.mark_paid(conn, sid, "deposit", now=NOW)
    assert any("3,500" in w for w in got["warnings"])
    # Повтор той же части — новых денег нет, оговорок о лимитах не повторяем.
    assert cm.mark_paid(conn, sid, "deposit", now=NOW)["warnings"] == []
