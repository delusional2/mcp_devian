from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_devian import db, drafts, forecast, sales, selling
from mcp_devian.db import Item

DAY = 86400
HOUR = 3600
NY = ZoneInfo("America/New_York")
PUB = datetime(2026, 11, 1, 12, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.sqlite3")
    yield c
    c.close()


_n = [0]


def lot(conn, title="Dragon adopt", usd=10.0, own=False, published=PUB, snippet=None, n_bids=0, top=None,
        ended=0, status="open", lot_type="auction", last_bid=None, top_user=None, updated=None, points=None,
        uuid=None, tags=()):
    _n[0] += 1
    iid = f"da:{9000 + _n[0]}"
    extra = {"tags": list(tags)}
    if uuid:
        extra["uuid"] = uuid
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=title,
                              url=f"https://www.deviantart.com/her/art/lot-{9000 + _n[0]}",
                              author="her" if own else f"seller{_n[0]}", lot_type=lot_type, status=status,
                              price_usd=usd if points is None else None, price_points=points, own=own,
                              published_ts=published, snippet=snippet, extra=extra), now=published)
    if lot_type in ("auction", "bta") and n_bids is not None:
        conn.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, bidders, last_bid_ts, ended, "
                     "updated_ts, extra) VALUES (?,?,?,?,?,?,?,?,?)",
                     (iid, top, "usd" if points is None else "points", n_bids, min(n_bids, 2), last_bid, ended,
                      updated if updated is not None else published + 4 * DAY,
                      f'{{"top_user": {"null" if not top_user else chr(34) + top_user + chr(34)}}}'))
        conn.commit()
    return iid


def ts(y, mo, d, h=0, mi=0, tz=UTC):
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


# --- Срок аукциона ---

@pytest.mark.parametrize("text, expected", [
    ("Auction ends 03.11 20:00 EST", ts(2026, 11, 3, 20, 0, NY)),
    ("ENDS: 03.11.2026 20:00 (EST)", ts(2026, 11, 3, 20, 0, NY)),
    ("ends 11/03 at 8pm PST", ts(2026, 11, 3, 20, 0, ZoneInfo("America/Los_Angeles"))),
    ("Ends on November 5th, 8:30 PM GMT", ts(2026, 11, 5, 20, 30)),
    ("ends 03.11 20:00 GMT+3", ts(2026, 11, 3, 17, 0)),
    ("ends 03.11 20:00 UTC-5", ts(2026, 11, 4, 1, 0)),
    ("заканчивается 5 ноября в 20:00 по МСК", ts(2026, 11, 5, 17, 0)),
    ("Аукцион закончится 05.11 в 20.00 мск", ts(2026, 11, 5, 17, 0)),
    ("ends in 48h", PUB + 48 * HOUR),
    ("Auction ends in 2 days!", PUB + 2 * DAY),
    ("72 hour auction, SB $10", PUB + 72 * HOUR),
    ("закончится через 24 ч", PUB + 24 * HOUR),
    ("Ends 2026-11-05 20:00 UTC", ts(2026, 11, 5, 20, 0)),
    ("ends 12.11 20:00 UTC+05:30", ts(2026, 11, 12, 14, 30)),   # с точкой — день.месяц
    ("ends at 8pm EST on Nov 5", ts(2026, 11, 5, 20, 0, NY)),
    ("closed species, ends 6/11 9:00 pm ET", ts(2026, 11, 6, 21, 0, NY)),  # 11 июня — не срок этого лота
    ("закрою 5.11 в 21:00 мск", ts(2026, 11, 5, 18, 0)),
])
def test_parse_end_explicit(text, expected):
    info = selling.parse_end(text, published_ts=PUB, tz="Europe/Moscow")
    assert info.end_ts == pytest.approx(expected), info.reason
    assert info.precision == "minute"


def test_parse_end_date_only_is_end_of_day_in_her_zone():
    info = selling.parse_end("Closes Nov 5", published_ts=PUB, tz="Europe/Moscow")
    assert info.precision == "day"
    assert info.end_ts == ts(2026, 11, 5, 23, 59, ZoneInfo("Europe/Moscow"))
    assert info.tz_assumed


def test_parse_end_without_zone_uses_her_zone_and_says_so():
    info = selling.parse_end("ends 03.11 20:00", published_ts=PUB, tz="Europe/Moscow")
    assert info.end_ts == ts(2026, 11, 3, 17, 0)
    assert info.tz_assumed and "Europe/Moscow" in info.reason


def test_est_in_summer_means_new_york_local_time():
    pub = ts(2026, 7, 10, 12)
    info = selling.parse_end("ends 15.07 20:00 EST", published_ts=pub)
    assert info.end_ts == ts(2026, 7, 16, 0, 0)   # 20:00 EDT


def test_after_last_bid():
    info = selling.parse_end("Ends 24h after the last bid. Min inc $1", published_ts=PUB, last_bid_ts=PUB + 5 * HOUR)
    assert info.end_ts == PUB + 29 * HOUR and info.kind == "after_last_bid"
    none = selling.parse_end("Ends 24h after the last bid", published_ts=PUB)
    assert none.end_ts is None and "ставок" in none.reason


@pytest.mark.parametrize("text, why", [
    ("SB $10 AB $50", "не указан"),
    ("ends at 8pm EST", "только время"),
    ("ends 03/04 20:00 EST", "дата"),                       # ни 3 апреля, ни 4 марта не рядом с 1 ноября
    ("ends 03.11 20:00 IST", "IST"),                       # Индия? Ирландия? Израиль?
    ("ends 03.11 20:00 EST. Edit: ends 05.11 20:00 EST", "несколько"),
    ("ends when AB is reached", "не указан"),
    ("ends tomorrow", "не указан"),
])
def test_parse_end_refuses_unclear(text, why):
    info = selling.parse_end(text, published_ts=PUB)
    assert info.end_ts is None
    assert why in info.reason


def test_same_deadline_twice_is_not_a_conflict():
    info = selling.parse_end("[ENDS 03.11 20:00 EST]\nAuction ends 03.11 20:00 EST", published_ts=PUB)
    assert info.end_ts == ts(2026, 11, 3, 20, 0, NY)


def test_price_is_not_a_date():
    info = selling.parse_end("SB: 10.50 ends in 48h", published_ts=PUB)
    assert info.end_ts == PUB + 48 * HOUR


# --- Скоро закончится / закончился, но не закрыт ---

def test_ending_soon_and_once_only(conn):
    end = ts(2026, 11, 3, 20, 0, NY)
    lot(conn, "Moth dragon", own=True, snippet="Auction ends 03.11 20:00 EST", n_bids=3, top=40.0, top_user="bob")
    lot(conn, "Later", own=True, snippet="ends 10.11 20:00 EST", n_bids=0)
    lot(conn, "Not hers", own=False, snippet="Auction ends 03.11 20:00 EST", n_bids=1, top=5.0)
    now = end - 90 * 60
    soon = selling.ending_soon(conn, now, within_h=3)
    assert [s["title"] for s in soon] == ["Moth dragon"]
    s = soon[0]
    assert s["left_s"] == 90 * 60 and s["n_bids"] == 3 and s["top"] == 40.0
    assert "через 1 ч 30 мин" in s["text"] and "ставок 3" in s["text"] and "$40" in s["text"] and "bob" in s["text"]
    assert len(selling.ending_soon(conn, now, within_h=3, unseen_only=True)) == 1
    assert selling.ending_soon(conn, now + 600, within_h=3, unseen_only=True) == []
    # Без unseen_only список для чата не меняется.
    assert len(selling.ending_soon(conn, now + 600, within_h=3)) == 1


def test_ended_unclosed(conn):
    end = ts(2026, 11, 3, 20, 0, NY)
    a = lot(conn, "Moth dragon", own=True, snippet="Auction ends 03.11 20:00 EST", n_bids=2, top=40.0, top_user="bob")
    lot(conn, "Closed already", own=True, snippet="Auction ends 03.11 20:00 EST", n_bids=2, top=40.0, ended=1)
    lot(conn, "No bids", own=True, snippet="Auction ends 03.11 20:00 EST", n_bids=0)
    got = selling.ended_unclosed(conn, end + 3 * HOUR)
    assert {g["title"] for g in got} == {"Moth dragon", "No bids"}
    md = next(g for g in got if g["item_id"] == a)
    assert md["winner_guess"] == "bob" and md["ended_ago_s"] == 3 * HOUR
    assert "закончился 3 ч назад" in md["text"] and "$40" in md["text"]
    nb = next(g for g in got if g["title"] == "No bids")
    assert "без ставок" in nb["text"]
    assert len(selling.ended_unclosed(conn, end + 3 * HOUR, unseen_only=True)) == 2
    assert selling.ended_unclosed(conn, end + 4 * HOUR, unseen_only=True) == []


def test_auction_ends_report(conn):
    lot(conn, "A", own=True, snippet="ends 03.11 20:00 EST", n_bids=0)
    lot(conn, "B", own=True, snippet="SB $5", n_bids=0)
    rows = selling.auction_ends(conn, PUB + HOUR)
    by = {r["title"]: r for r in rows}
    assert by["A"]["end_ts"] and by["B"]["end_ts"] is None and by["B"]["reason"]
    text = selling.render_ends(rows, tz="Europe/Moscow")
    assert "A" in text and "B" in text


# --- Непроданное ---

def _market(conn, now):
    # 16 завершённых рыночных аукционов с драконами: старт $10 продаётся, $30 — почти нет.
    for i in range(8):
        lot(conn, f"Dragon cheap {i}", usd=10.0, published=now - (20 + i) * DAY, n_bids=3, top=20.0, ended=1,
            status="closed")
    for i in range(8):
        sold = i == 0
        lot(conn, f"Dragon pricey {i}", usd=30.0, published=now - (20 + i) * DAY, n_bids=2 if sold else 0,
            top=35.0 if sold else None, ended=1, status="closed")


def test_unsold_auction_options_are_grounded(conn):
    now = PUB + 30 * DAY
    _market(conn, now)
    for i in range(6):
        lot(conn, f"Dragon OTA {i}", usd=None, lot_type="ota", published=now - (10 + i) * DAY,
            status="closed" if i < 4 else "open")
    mine = lot(conn, "Dragon adopt", usd=30.0, own=True, published=now - 9 * DAY, n_bids=0, ended=1,
               status="closed")
    res = selling.unsold(conn, now, tz="Europe/Moscow")
    assert [r["item_id"] for r in res["lots"]] == [mine]
    opts = {o["kind"]: o for o in res["lots"][0]["options"]}
    plan = forecast.price_plan(conn, species=["dragon"], unit="usd", now=now)
    assert opts["lower_start"]["start"] == plan.best["start"] < 30
    assert opts["lower_start"]["p_sold"] == plan.best["p_sold"]
    assert opts["lower_start"]["confidence"] == plan.confidence
    assert opts["lower_start"]["p_now"] < opts["lower_start"]["p_sold"]
    assert opts["to_ota"]["n"] == 6 and opts["to_ota"]["closed"] == 4
    text = selling.render_unsold(res)
    assert "Dragon adopt" in text and "67%" in text


def test_unsold_price_option_shows_observed_counts_and_model_separately(conn):
    now = PUB + 30 * DAY
    _market(conn, now)
    lot(conn, "Dragon adopt", usd=30.0, own=True, published=now - 9 * DAY, n_bids=0, ended=1, status="closed")
    res = selling.unsold(conn, now, tz="Europe/Moscow")
    opt = next(o for o in res["lots"][0]["options"] if o["kind"] == "lower_start")
    plan = forecast.price_plan(conn, species=["dragon"], unit="usd", now=now)
    b = plan.best
    assert f"ставку получили {b['k']} из {b['n']}" in opt["text"] and "по модели ~" in opt["text"]
    assert f"ставка в {round(b['p_sold'] * 100)}% случаев" not in opt["text"]
    assert opt["k"] == b["k"] and opt["n_start"] == b["n"]


def _best(slot=("weekday", 3), level="средняя"):
    return {"slot": slot, "weekday": 0, "hour": 18, "weekday_name": "будни", "hours": "18–24", "level": level,
            "evidence": "e"}


def test_repost_option_uses_the_same_slot_as_best_time():
    tz = "Europe/Moscow"
    wed_19 = datetime(2026, 10, 28, 19, 0, tzinfo=ZoneInfo(tz)).timestamp()
    row = {"published_ts": wed_19}
    lot_ = {"options": []}
    selling._time_option(lot_, row, _best(), tz)          # среда 19:00 — это и есть «будни вечером»
    assert lot_["options"] == []
    selling._time_option(lot_, row, _best(slot=("weekend", 2)), tz)
    assert [o["kind"] for o in lot_["options"]] == ["repost"]
    lot_ = {"options": []}
    selling._time_option(lot_, row, _best(slot=("weekend", 2), level="низкая"), tz)
    assert lot_["options"] == []                            # слабое окно — не повод перевыкладывать


def test_unsold_skips_sold_and_bidded_and_unread(conn):
    now = PUB + 30 * DAY
    sold = lot(conn, "Sold one", usd=30.0, own=True, published=now - 9 * DAY, n_bids=0, ended=1, status="closed")
    sales.add_sale(conn, title="Sold one", amount=30, currency="usd", platform="deviantart", format="auction",
                   item_id=sold)
    lot(conn, "With bids", usd=30.0, own=True, published=now - 9 * DAY, n_bids=2, top=40.0, ended=1, status="closed")
    lot(conn, "Unread", usd=30.0, own=True, published=now - 9 * DAY, n_bids=None, status="closed")
    lot(conn, "Fresh open", usd=30.0, own=True, published=now - 1 * DAY, n_bids=0)
    lot(conn, "Old open", usd=30.0, own=True, published=now - 8 * DAY, n_bids=0)
    lot(conn, "Stale flatsale", usd=25.0, own=True, published=now - 10 * DAY, lot_type="flatsale")
    res = selling.unsold(conn, now)
    assert {r["title"] for r in res["lots"]} == {"Old open", "Stale flatsale"}
    assert res["unread"] == 1


def test_unsold_without_data_says_so_and_bundles(conn):
    now = PUB + 30 * DAY
    lot(conn, "Moth dragon A", usd=30.0, own=True, published=now - 9 * DAY, n_bids=0, ended=1, status="closed")
    lot(conn, "Moth dragon B", usd=20.0, own=True, published=now - 9 * DAY, n_bids=0, ended=1, status="closed")
    res = selling.unsold(conn, now)
    assert len(res["lots"]) == 2
    for r in res["lots"]:
        kinds = {o["kind"] for o in r["options"]}
        assert "lower_start" not in kinds            # данных нет — цену не выдумываем
        assert "bundle" in kinds
        assert any("нужно хотя бы" in n for n in r["notes"])
    text = selling.render_unsold(res)
    assert "набор" in text


# --- Напоминания об оплате ---

def test_template_slots_checked(conn):
    with pytest.raises(selling.SellingError):
        selling.set_template(conn, "payment_reminder", "Hi {name}!")
    with pytest.raises(selling.SellingError):
        selling.set_template(conn, "payment_reminder", "   ")
    selling.set_template(conn, "payment_reminder", "Hi @{buyer}! {title} — {amount}, {days} days")
    assert selling.get_template(conn, "payment_reminder").startswith("Hi @{buyer}")


def _won(conn, days_ago, now, platform="deviantart", buyer="bob", uuid="UUID-1"):
    iid = lot(conn, "Moth dragon", usd=10.0, own=True, n_bids=2, top=40.0, ended=1, status="closed", uuid=uuid) \
        if platform == "deviantart" else None
    return sales.add_sale(conn, title="Moth dragon", amount=40, currency="usd", platform=platform, format="auction",
                          buyer=buyer, item_id=iid, now=now - days_ago * DAY)


def test_payment_reminders_flow(conn):
    now = PUB + 10 * DAY
    old = _won(conn, 5, now)
    _won(conn, 1, now)                                         # ещё рано
    th = _won(conn, 6, now, platform="toyhouse")
    res = selling.payment_reminders(conn, now)
    assert [d["sale_id"] for d in res["due"]] == [old]
    assert res["due"][0]["text"] is None and not res["template"]
    assert any(s["sale_id"] == th and "не DeviantArt" in s["reason"] for s in res["skipped"])

    selling.set_template(conn, "payment_reminder", "Hi @{buyer}! {title} — {amount}, {days} days")
    d = selling.payment_reminders(conn, now)["due"][0]
    assert d["text"] == "Hi @bob! Moth dragon — $40, 5 days"
    assert d["deviation_uuid"] == "UUID-1" and d["deviation_url"].startswith("https://www.deviantart.com/")

    def due(at):
        return [x["sale_id"] for x in selling.payment_reminders(conn, at)["due"]]

    selling.mark_reminded(conn, old, "draft", now)
    assert old in due(now + HOUR)                       # отметка без живого черновика (отменён) не мешает
    draft_id = drafts.create(conn, "da_comment_reminder", {"ref": f"payment:{old}"}, "p", now)
    assert old not in due(now + HOUR)                   # черновик ждёт «го» — второй не нужен
    drafts.cancel(conn, draft_id)
    assert old in due(now + HOUR)                       # черновик бросили — можно снова
    selling.mark_reminded(conn, old, "sent", now + DAY + 2)
    r = selling.payment_reminders(conn, now + 3 * DAY)
    assert old not in [x["sale_id"] for x in r["due"]]
    assert any(s["sale_id"] == old and "напоминание было" in s["reason"] for s in r["skipped"])
    assert old not in due(now + 4 * DAY + 3)            # одно напоминание на продажу — дальше решает она


def test_paid_sale_not_reminded(conn):
    now = PUB + 10 * DAY
    sid = _won(conn, 5, now)
    sales.set_status(conn, sid, "paid", now)
    assert selling.payment_reminders(conn, now)["due"] == []


def test_render_reminders(conn):
    now = PUB + 10 * DAY
    _won(conn, 5, now)
    text = selling.render_reminders(selling.payment_reminders(conn, now))
    assert "bob" in text and "$40" in text and "5 дн" in text and "шаблон" in text


# --- Регрессии после проверки ---

@pytest.mark.parametrize("text, expected", [
    ("Ends: 03.11 (edit: extended to 05.11)", None),                                   # две даты — не время
    ("Ends 5.11, MI 1.50 usd", ts(2026, 11, 5, 23, 59, ZoneInfo("Europe/Moscow"))),   # цена — не время
    ("ends 05.11 at 20.30 GMT", ts(2026, 11, 5, 20, 30)),
    ("ends nov 4 at noon EST", ts(2026, 11, 4, 12, 0, NY)),
    ("ends nov 4 at midnight EST", ts(2026, 11, 4, 23, 59, NY)),
    ("заканчивается 5 ноября в полдень мск", ts(2026, 11, 5, 9, 0)),
    ("Ends on the 3rd of November 20:00 GMT", ts(2026, 11, 3, 20, 0)),
    ("Ends Saturday 7 November 20:00 GMT", ts(2026, 11, 7, 20, 0)),
])
def test_parse_end_regressions(text, expected):
    info = selling.parse_end(text, published_ts=PUB, tz="Europe/Moscow")
    if expected is None:
        assert info.end_ts is None, info
    else:
        assert info.end_ts == pytest.approx(expected), info.reason


def test_two_dates_with_edit_are_a_conflict_not_a_time():
    info = selling.parse_end("Ends: 03.11 (edit: extended to 05.11)", published_ts=PUB, tz="Europe/Moscow")
    assert info.end_ts is None and "несколько дат" in info.reason
    same = selling.parse_end("Ends Nov 5 (05.11) 20:00 GMT", published_ts=PUB)
    assert same.end_ts == ts(2026, 11, 5, 20, 0)


def test_noon_with_zone_is_not_called_unzoned():
    info = selling.parse_end("ends nov 4 at noon EST", published_ts=PUB, tz="Europe/Moscow")
    assert not info.tz_assumed and info.precision == "minute"


def test_after_last_bid_with_in_is_not_from_publication():
    none = selling.parse_end("Ends in 24h after the last bid", published_ts=PUB)
    assert none.end_ts is None and "ставок" in none.reason
    info = selling.parse_end("Ends in 24h after the last bid", published_ts=PUB, last_bid_ts=PUB + 5 * HOUR)
    assert info.end_ts == PUB + 29 * HOUR and info.kind == "after_last_bid"


@pytest.mark.parametrize("text", ["Оплата до 10.11", "pay until 10.11 please", "Payment deadline 10.11",
                                  "hold until 10.11"])
def test_payment_deadline_is_not_auction_end(text):
    assert selling.parse_end(text, published_ts=PUB).end_ts is None


def test_payment_deadline_next_to_real_end():
    info = selling.parse_end("Ends 05.11 20:00 GMT. Оплата до 10.11", published_ts=PUB)
    assert info.end_ts == ts(2026, 11, 5, 20, 0)


def test_unseen_alerts_are_not_lost_when_limited(conn):
    now = PUB + HOUR
    for i in range(5):
        lot(conn, f"Lot {i}", own=True, snippet=f"ends 01.11 {14 + i // 2}:{30 * (i % 2):02d} UTC", n_bids=1)
    first = selling.ending_soon(conn, now, within_h=3, unseen_only=True, limit=3)
    second = selling.ending_soon(conn, now, within_h=3, unseen_only=True, limit=3)
    third = selling.ending_soon(conn, now, within_h=3, unseen_only=True, limit=3)
    assert len(first) == 3 and len(second) == 2 and third == []
    assert not {x["item_id"] for x in first} & {x["item_id"] for x in second}
    assert len(selling.ending_soon(conn, now, within_h=3, limit=2)) == 2


@pytest.mark.parametrize("amount,note,needle", [(40, sales.CHECK_BUYER, "покупател"),
                                                (None, sales.CHECK_AMOUNT, "сумм"),
                                                (None, "", "сумм")])
def test_unverified_sales_are_not_reminded(conn, amount, note, needle):
    now = PUB + 10 * DAY
    iid = lot(conn, "Moth dragon", own=True, n_bids=2, top=40.0, ended=1, status="closed", uuid="U")
    sid = sales.add_sale(conn, title="Moth dragon", amount=amount, currency="usd", platform="deviantart",
                         format="auction", buyer="bob", item_id=iid, note=note, now=now - 5 * DAY)
    res = selling.payment_reminders(conn, now)
    assert res["due"] == []
    assert any(s["sale_id"] == sid and needle in s["reason"] for s in res["skipped"])


def test_pending_reminder_draft_blocks_past_a_day(conn):
    from mcp_devian import drafts
    now = PUB + 10 * DAY
    sid = _won(conn, 5, now)
    did = drafts.create(conn, "da_comment", {"ref": f"payment:{sid}", "text": "x"}, "x", now=now)
    selling.mark_reminded(conn, sid, "draft", now)
    later = selling.payment_reminders(conn, now + DAY + HOUR)
    assert sid not in [d["sale_id"] for d in later["due"]]
    conn.execute("UPDATE drafts SET status = 'cancelled' WHERE id = ?", (did,))
    assert sid in [d["sale_id"] for d in selling.payment_reminders(conn, now + DAY + HOUR)["due"]]


def test_reminder_block_at_send_time(conn):
    now = PUB + 10 * DAY
    sid = _won(conn, 5, now)
    assert selling.reminder_block(conn, sid, now) == ""
    selling.mark_reminded(conn, sid, "sent", now)
    assert "было" in selling.reminder_block(conn, sid, now + DAY)
    assert selling.reminder_block(conn, sid, now + 4 * DAY) == selling.TOO_MANY_REMINDERS
    sales.set_status(conn, sid, "paid", now + 4 * DAY)
    assert "не ждёт" in selling.reminder_block(conn, sid, now + 5 * DAY)
    assert selling.reminder_block(conn, "nope", now) == "продажи нет в журнале"


def test_render_reminders_uses_her_days(conn):
    res = selling.payment_reminders(conn, PUB, older_than_days=7)
    assert selling.render_reminders(res) == "Неоплаченных продаж старше 7 дн нет."


def test_market_sample_like_forecast(conn):
    """Похожие лоты для советов — как в прогнозе: без найденных только поиском и без вида из описания."""
    from mcp_devian import taxonomy

    now = PUB + 30 * DAY
    folder = lot(conn, "Dragon OTA", lot_type="ota", n_bids=None)
    search = lot(conn, "Dragon OTA", lot_type="ota", n_bids=None)
    conn.execute("UPDATE items SET place = ? WHERE id = ?", (db.SEARCH_PLACE_PREFIX + "dragon", search))
    lot(conn, "Cute OTA", lot_type="ota", n_bids=None, snippet="also drawing dragon commissions")
    conn.commit()
    got = [r["id"] for r in selling._market(conn, "ota", taxonomy.tag_text("Dragon OTA"), now)]
    assert got == [folder]


# --- 0.5: шаблоны оплаты и продвижения, срок оплаты 48 ч ---

def test_new_template_kinds(conn):
    for kind, text in (("payment_info", "Pay {amount} at {boosty_url} within {window_hours}h"),
                       # Слоты — как в модулях, которые эти шаблоны заполняют (outreach/leads, xpost, raffle).
                       ("offer", "Hi {name}! {title} might suit you ({why}): {link}"),
                       ("x_post", "{title} {price} {url} {species}"),
                       ("raffle", "{prize} until {ends}. {rules} {commitment}"),
                       ("raffle_winner", "{winner} won {title}! seed {seed}, {count} entries, {list_hash}"),
                       ("status", "New: {title} {price} {url}")):
        selling.set_template(conn, kind, text)
        assert selling.get_template(conn, kind) == text
    with pytest.raises(selling.SellingError):
        selling.set_template(conn, "x_post", "{title} {mentions}")


def test_default_payment_info_mentions_paypal_only_as_unavailable(conn):
    info = selling.payment_info(conn, buyer="bob", title="Moth", amount="$40", boosty_url="https://boosty.to/moth",
                                window_hours=48)
    text = info["text"]
    assert info["default"] and "https://boosty.to/moth" in text and "$40" in text and "48h" in text
    low = text.lower()
    for fact in ("outside russia", "converted automatically", "3-d secure", "don't retry", "da/th name"):
        assert fact in low, fact
    # FAQ Boosty (12.2025): валюту доната не выбрать, конвертация автоматическая — «switch to USD/EUR» нет.
    assert "usd" not in low and "eur" not in low and "switch" not in low
    assert "points" not in low  # поинтов нет в её способах — и строки про них нет
    paypal = [line for line in text.splitlines() if "paypal" in line.lower()]
    assert paypal == ["- PayPal is not available on Boosty, sorry!"]
    for banned in ("ko-fi", "kofi", "stripe", "donation"):
        assert banned not in low
    assert "{" not in text
    no_link = selling.payment_info(conn, buyer="bob")["text"]
    assert "link in my profile" in no_link and "  " not in no_link
    selling.set_template(conn, "payment_info", "My way: {boosty_url}")
    assert selling.payment_info(conn, boosty_url="https://boosty.to/m") == {"text": "My way: https://boosty.to/m",
                                                                           "default": False}


def test_reminders_after_48h_once_per_buyer(conn):
    now = PUB + 10 * DAY
    early = _won(conn, 1.5, now)
    due_sale = _won(conn, 2.1, now, uuid="UUID-2")
    res = selling.payment_reminders(conn, now, boosty_url="https://boosty.to/moth")
    ids = [d["sale_id"] for d in res["due"]]
    assert due_sale in ids and early not in ids
    d = next(x for x in res["due"] if x["sale_id"] == due_sale)
    # Текст по умолчанию — только предложение: в payment_info (то, что можно класть в черновик) его нет.
    assert d["payment_info"] is None and d["payment_info_default"]
    assert "https://boosty.to/moth" in d["payment_info_suggestion"]
    assert "Предложение (не отправляю" in selling.render_reminders(res)
    assert early in [d["sale_id"] for d in selling.payment_reminders(conn, now, payment_window_hours=24)["due"]]
    selling.mark_reminded(conn, due_sale, "sent", now)
    later = selling.payment_reminders(conn, now + 30 * DAY)
    assert due_sale not in [d["sale_id"] for d in later["due"]]
    assert any(s["sale_id"] == due_sale and s["reason"] == selling.TOO_MANY_REMINDERS for s in later["skipped"])


def test_default_payment_info_respects_currency_and_methods(conn):
    pts = selling.payment_info(conn, buyer="bob", amount="500 pts", currency="points", methods=["boosty", "points"])
    assert pts["text"] is None  # продажа за поинты: Boosty тут ни при чём
    rub = selling.payment_info(conn, buyer="bob", amount=sales.money(1500, "rub"), currency="rub",
                               boosty_url="https://boosty.to/m")["text"]
    assert "1,500 ₽" in rub and "usd" not in rub.lower() and "rub" not in rub.lower().replace("russia", "")
    both = selling.payment_info(conn, buyer="bob", amount="$40", currency="usd", methods=["boosty", "points"])["text"]
    assert "DA Points are an option too" in both and "{" not in both
    only_boosty = selling.payment_info(conn, buyer="bob", amount="$40", currency="usd", methods=["boosty"])["text"]
    assert "Points" not in only_boosty


def test_her_payment_info_goes_to_due_items(conn):
    now = PUB + 10 * DAY
    sid = _won(conn, 3, now)
    selling.set_template(conn, "payment_info", "Pay {amount} here: {boosty_url}")
    d = next(x for x in selling.payment_reminders(conn, now, boosty_url="https://boosty.to/m")["due"]
             if x["sale_id"] == sid)
    assert d["payment_info"] == "Pay $40 here: https://boosty.to/m"
    assert not d["payment_info_default"] and d["payment_info_suggestion"] is None


def test_points_sale_reminder_has_no_boosty_text(conn):
    now = PUB + 10 * DAY
    iid = lot(conn, "Moth pts", usd=None, points=500, own=True, n_bids=2, top=900.0, ended=1, status="closed",
              uuid="UUID-P")
    sid = sales.add_sale(conn, title="Moth pts", amount=900, currency="points", platform="deviantart",
                         format="auction", buyer="bob", item_id=iid, now=now - 3 * DAY)
    d = next(x for x in selling.payment_reminders(conn, now, boosty_url="https://boosty.to/m",
                                                  payment_methods=["boosty", "points"])["due"] if x["sale_id"] == sid)
    assert d["payment_info"] is None and d["payment_info_suggestion"] is None and not d["payment_info_default"]


def test_default_payment_text_never_becomes_a_draft(conn):
    """Платформенная безопасность: публичный комментарий — только её слова. Есть лишь текст по умолчанию
    (её шаблонов нет) — черновика нет; есть её напоминание — в черновике только оно, без текста по умолчанию."""
    from mcp_devian import posting
    from mcp_devian.config import Config

    now = PUB + 10 * DAY
    sid = _won(conn, 3, now)
    assert selling.template_or_default(conn, "payment_info")[1]
    draft_id, why = posting.prepare_payment_reminder(conn, Config(), sid, now)
    assert draft_id is None and "шаблона" in why
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    res = selling.payment_reminders(conn, now, older_than_days=0, boosty_url="https://boosty.to/m")
    selling.set_template(conn, "payment_reminder", "Hi {buyer}, reminder about {title}")
    res = selling.payment_reminders(conn, now, older_than_days=0, boosty_url="https://boosty.to/m")
    d = next(x for x in res["due"] if x["sale_id"] == sid)
    assert d["text"] == "Hi bob, reminder about Moth dragon" and d["payment_info"] is None
