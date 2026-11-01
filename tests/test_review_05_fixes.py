"""Ревью 0.5: предложения только тем, кому можно (чёрный список, площадка, Toyhouse без перевода), честные деньги
(слоты с одной предоплатой, сделки вместо частей), фон не висит на зависшем DeviantArt, понятные ответы в чате.

Только подделки: без сети, без настоящих уведомлений, папка данных — временная."""

import asyncio
import json
import re

import pytest
import test_integration_a as ia

from mcp_devian import (
    backfill,
    buyers,
    commissions,
    db,
    digest,
    earnings,
    leads,
    mcp_server,
    monitor,
    offers,
    outreach,
    pinglist,
    posting,
    protection,
    report,
    sales,
    selling,
    th_market,
    translate_guard,
)
from mcp_devian.config import Config, load_config, save_config
from mcp_devian.da_api import DAApiError
from mcp_devian.db import Item

DAY = 86400
NOW = 1_790_000_000.0
TEMPLATE = "Hi {name}! New adopt «{title}» — {why}. {link}"
fake, server = ia.fake, ia.server


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "r.sqlite3")
    yield c
    c.close()


def _call(name: str, args: dict) -> str:
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    return "".join(getattr(c, "text", "") for c in result.content)


def set_offer(conn, text=TEMPLATE):
    selling.ensure(conn)
    conn.execute("INSERT OR REPLACE INTO selling_templates (kind, text, updated_ts) VALUES ('offer', ?, ?)",
                 (text, NOW))
    conn.commit()
    outreach.note_watch_run(conn, NOW - 3600)


def lot(conn, n, title, ts=NOW - 3 * DAY):
    iid = f"da:{n}"
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=title, url=f"https://www.deviantart.com/her/art/x-{n}",
                              author="her", lot_type="auction", own=True, published_ts=ts), now=ts)
    return iid


def sale(conn, buyer, platform="deviantart", item_id=None, status="paid", ts=NOW - 10 * DAY, title="Moth adopt"):
    return sales.add_sale(conn, title=title, amount=20, currency="usd", platform=platform, format="auction",
                          buyer=buyer, item_id=item_id, status=status, now=ts)


# --- 1. Чёрный список, неоплата и спор: ни черновика, ни отправки, ни пинга ---

def test_blacklist_and_bad_marks_block_offers_everywhere(conn):
    moth = lot(conn, 1, "Moth chibi adopt", ts=NOW - 20 * DAY)
    s_disp = sale(conn, "Disp", item_id=moth)
    sale(conn, "Good", item_id=lot(conn, 2, "Moth pastel adopt", ts=NOW - 20 * DAY))
    sale(conn, "NoPay", item_id=lot(conn, 3, "Moth adopt three", ts=NOW - 20 * DAY))
    protection.on_sale_status(conn, s_disp, "disputed", now=NOW - DAY)        # спор: продажа остаётся paid
    protection.blacklist_add(conn, "Disp", "спор через банк", now=NOW - DAY)
    protection.mark(conn, "NoPay", "no_pay", now=NOW - DAY)
    buyers.sync(conn, NOW, my_username="her")
    new = lot(conn, 9, "Moth chibi adopt NEW", ts=NOW)
    set_offer(conn)

    ranked = outreach.rank_for_adopt(conn, new, NOW)
    assert [r["user"] for r in ranked] == ["Good"]                 # кандидатов из чёрного списка нет вовсе
    res = outreach.drafts_for_adopt(conn, conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone(),
                                    [{"user": u, "roles": ["buyer"], "platforms": ["deviantart"],
                                      "dm_roles": {"deviantart": ["buyer"]}, "channel": "da_mention"}
                                     for u in ("Disp", "NoPay", "Good")], None, NOW)
    assert res["mention_users"] == ["Good"]
    skipped = dict(res["skipped"])
    assert "чёрном списке" in skipped["Disp"] and "не заплатил" in skipped["NoPay"]
    assert outreach.person_block(conn, "disp", NOW) == buyers.BAD_LISTED

    refused, _ = posting.offer_check(conn, Config(), ["Disp", "NoPay"], NOW)
    assert {u for u, _ in refused} == {"Disp", "NoPay"}             # и прямо перед отправкой

    crm = buyers.render_crm(conn, NOW)
    assert "предложить лично можно 1" in crm and "чёрном списке" in crm


def test_chargeback_removes_buyer_role(conn):
    moth = lot(conn, 1, "Moth adopt", ts=NOW - 20 * DAY)
    sid = sale(conn, "Back", item_id=moth)
    buyers.sync(conn, NOW, my_username="her")
    assert "buyer" in buyers.contact(conn, "back")["roles"]
    sales.set_status(conn, sid, "cancelled", now=NOW - DAY)          # chargeback: продажа отменена
    buyers.sync(conn, NOW, my_username="her")
    assert "buyer" not in buyers.contact(conn, "back")["roles"]


def test_pinglist_skips_blacklisted_members(conn):
    iid = lot(conn, 1, "Moth adopt", ts=NOW)
    selling.set_template(conn, "pinglist", "New: {title} {url} {mentions}")
    pinglist.add(conn, ["Pia", "Scam"], now=NOW - 5 * DAY)
    protection.blacklist_add(conn, "Scam", "не заплатил дважды", now=NOW - DAY)
    res = pinglist.mention_comments(conn, deviation_id=iid, deviation_url="https://www.deviantart.com/her/art/x-1",
                                    title="Moth", now=NOW)
    users = [u for c in res["comments"] for u in c["users"]]
    assert users == ["Pia"] and [u for u, _ in res["blocked"]] == ["Scam"]
    assert pinglist.not_wanted(conn, iid, ["Pia", "Scam"]) == ["Scam"]


# --- 2. Ник с другой площадки не становится @отметкой на DeviantArt ---

def test_offsite_sale_gives_no_deviantart_channel(conn):
    sale(conn, "tg_masha", platform="other", item_id="comm:s1:final", title="YCH «moth» — оплата")
    buyers.sync(conn, NOW, my_username="her")
    c = buyers.contact(conn, "tg_masha")
    assert "deviantart" not in c["platforms"] and outreach.channel_for(c) is None
    assert "предложить лично можно 0" in buyers.render_crm(conn, NOW)


def test_toyhouse_buyer_and_da_faver_with_same_nick_stay_apart(conn):
    moth = lot(conn, 1, "Moth adopt", ts=NOW - 5 * DAY)
    sale(conn, "Pal", platform="toyhouse", item_id="th:55", title="Moth adopt")
    buyers.add_event(conn, "pal", "faver", moth, NOW - 2 * DAY)
    buyers.sync(conn, NOW, my_username="her")
    c = buyers.contact(conn, "pal")
    assert c["dm_roles"] == {"toyhouse": ["buyer"]}
    assert outreach.channel_for(c) == "th_message_manual"           # не da_mention с @pal под её работой
    # бид на её лоте DA — личка DA, а не отметка (роль «покупатель» была на Toyhouse)
    conn.execute("INSERT INTO auctions (deviation_id, owner, updated_ts, extra) VALUES (?, 'her', ?, ?)",
                 (moth, NOW - DAY, json.dumps({"bidders": [{"user": "Pal", "amount": 5, "ts": NOW - DAY}]})))
    conn.commit()
    buyers.sync(conn, NOW, my_username="her")
    assert outreach.channel_for(buyers.contact(conn, "pal")) == "da_message_manual"


# --- 3. Toyhouse: ни перевода, ни слов помощника ---

def test_toyhouse_dm_never_carries_translation_or_helper_words(conn):
    db.upsert_item(conn, Item(id="th:7", source="toyhouse", title="Moth adopt", url="https://toyhou.se/7.moth",
                              author="her", own=True, published_ts=NOW - 20 * DAY), now=NOW - 20 * DAY)
    sale(conn, "Thb", platform="toyhouse", item_id="th:7", title="Moth adopt")
    buyers.sync(conn, NOW, my_username="her")
    new = lot(conn, 9, "Moth adopt NEW", ts=NOW)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    row = outreach.rank_for_adopt(conn, new, NOW)
    assert row and row[0]["channel"] == "th_message_manual"
    set_offer(conn, "Hi {name}, my new {title} is up {why}")
    res = outreach.drafts_for_adopt(conn, item, row, None, NOW)
    assert res["manual"] and "you adopted" not in res["manual"][0]["text"]   # {why} — не её слова: пусто

    en = "Hi {name}, a brand new design called {title} is up for adoption now, check it out please"
    translate_guard.save(conn, "Привет, новый дизайн уже можно забрать, загляни пожалуйста", en, "da", now=NOW)
    conn.execute("DELETE FROM outreach_log")
    conn.commit()
    set_offer(conn, en)
    res = outreach.drafts_for_adopt(conn, item, row, None, NOW)
    assert not res["manual"] and outreach.TH_TRANSLATED in dict(res["skipped"])["Thb"]


def test_saving_translated_offer_template_warns_about_toyhouse(server):
    en = "Hi {name}, a brand new design called {title} is up for adoption now, check it out please"
    with db.session() as conn:
        translate_guard.save(conn, "Привет, новый дизайн уже можно забрать, загляни пожалуйста", en, "da")
    out = _call("set_template", {"kind": "offer", "text": en})
    assert "сохранён" in out and "Toyhouse" in out and "4.8" in out


# --- 4. LF-лид: лично незнакомым не пишем ---

def test_lf_lead_never_suggests_dm(conn):
    selling.ensure(conn)
    conn.execute("INSERT INTO selling_templates (kind, text, updated_ts) VALUES ('offer', ?, ?)",
                 ("Hi {name}! {title} — {why}: {link}", NOW))
    conn.commit()
    lid = leads.add_manual(conn, "https://x.com/Fan/status/111111", "LF wolf adopts, dm me offers!", NOW)
    res = leads.reply_draft(conn, lid, intent_builder=lambda **kw: "https://x.com/intent", now=NOW)
    assert res["dm_ok"]
    assert not any("можно и так" in w for w in res["warnings"])
    assert any("публичный" in w for w in res["warnings"])


# --- 5. buyers:for_adopt при выключенной отправке ---

def test_for_adopt_respects_sending_switch(server, monkeypatch):
    with db.session() as conn:
        ia.own_work(conn)
    monkeypatch.setattr(posting, "SENDING_ENABLED", False)
    assert _call("buyers", {"action": "for_adopt", "ref": "da:5"}) == posting.NOT_YET


# --- Деньги ---

def test_deposit_only_slot_is_not_a_paid_deal(conn):
    oid = commissions.add_offer(conn, kind="ych", title="Moth YCH", price=1500, currency="rub", slots=10,
                                deposit_pct=50, now=NOW - 20 * DAY)
    slots = []
    for i in range(8):
        sid = commissions.claim(conn, client=f"c{i}", offer_id=oid, now=NOW - 20 * DAY)["slot_id"]
        commissions.mark_paid(conn, sid, "deposit", net_amount=700, net_currency="rub", now=NOW - 19 * DAY)
        earnings.log_hours(conn, f"comm:{sid}", 3, now=NOW - 18 * DAY)
        slots.append(sid)
    hist = conn.execute("SELECT * FROM sales WHERE status = 'paid'").fetchall()
    assert earnings._deals(conn, hist) == []
    ws = earnings.works(conn, NOW)
    assert ws and all(w.status == "pending" and w.income_rub is None for w in ws)
    commissions.mark_paid(conn, slots[0], "final", net_amount=700, net_currency="rub", now=NOW - DAY)
    hist = conn.execute("SELECT * FROM sales WHERE status = 'paid'").fetchall()
    deals = earnings._deals(conn, hist)
    assert len(deals) == 1 and deals[0]["net"] == 1400
    done = [w for w in earnings.works(conn, NOW) if w.key == f"comm:{slots[0]}"]
    assert done[0].status == "paid" and done[0].income_rub == 1400


def test_commission_parts_count_as_one_deal_and_stay_out_of_adopt_prices(conn):
    for i in range(4):
        sales.add_sale(conn, title=f"Adopt {i}", amount=60, currency="usd", platform="toyhouse", format="adopt",
                       buyer=f"a{i}", item_id=f"th:{i}", status="paid", now=NOW - 5 * DAY)
    for i in range(3):
        for part in ("deposit", "final"):
            sales.add_sale(conn, title=f"YCH {i} {part}", amount=30, currency="usd", platform="toyhouse",
                           format="ych", buyer=f"y{i}", item_id=f"comm:s{i}:{part}", status="paid",
                           source="commissions", now=NOW - 4 * DAY)
    inc = sales.income(conn, NOW - 30 * DAY, NOW)["usd"]
    assert inc["count"] == 7 and inc["total"] == 420
    realized, _ = th_market.realized_sales(conn, NOW - 30 * DAY, NOW)
    assert realized["usd"].n == 4
    rows = report._sales_rows(conn, NOW - 30 * DAY, NOW)
    assert len({sales.deal_key(r) for r in rows}) == 7
    assert all(not sales.is_commission_part(r) for r in sales.list_sales(conn, ("paid",))
               if r["item_id"].startswith("th:"))
    med, _ = offers.her_median(conn, "usd", NOW)
    assert med is None or med == 60


def test_points_slot_payment_records_even_with_boosty(conn):
    oid = commissions.add_offer(conn, kind="ych", title="Pts YCH", price=1000, currency="points", slots=2,
                                deposit_pct=0, now=NOW)
    sid = commissions.claim(conn, client="pp", offer_id=oid, now=NOW)["slot_id"]
    out = commissions.handle(conn, "paid", id=sid, part="final", pay_method="boosty", now=NOW)
    assert "Не вышло" not in out
    row = conn.execute("SELECT * FROM sales WHERE item_id LIKE ?", (f"comm:{sid}:%",)).fetchone()
    assert row["status"] == "paid" and row["pay_method"] == "points"


def test_sales_status_saves_note_and_net_without_duplicate(server):
    out = _call("sales", {"action": "record", "title": "Lumi", "amount": 40, "buyer": "alice"})
    sid = re.search(r"\[([0-9a-z]+)\]", out).group(1)
    _call("sales", {"action": "status", "sale_id": sid, "status": "paid"})
    out = _call("sales", {"action": "status", "sale_id": sid, "net": 3200})
    assert "дошло" in out and "3,200" in out.replace(" ", ",")
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1
        assert sales.get(conn, sid)["net_amount"] == 3200 and sales.get(conn, sid)["status"] == "paid"
    out2 = _call("sales", {"action": "record", "title": "Moth", "amount": 10, "buyer": "bob"})
    sid2 = re.search(r"\[([0-9a-z]+)\]", out2).group(1)
    _call("sales", {"action": "status", "sale_id": sid2, "status": "no_pay", "note": "пропал после выигрыша"})
    with db.session() as conn:
        assert conn.execute("SELECT note FROM buyer_marks WHERE sale_id = ?", (sid2,)).fetchone()[0] \
            == "пропал после выигрыша"


def test_sales_hours_accepts_hours_param(server):
    out = _call("sales", {"action": "hours", "hours": 2.5, "title": "Moth adopt"})
    assert "Нужны часы" not in out and "2.5" in out.replace(",", ".")


def test_ych_offer_without_currency_is_rubles(server):
    out = _call("sales", {"action": "ych_offer", "format": "ych", "title": "Moth YCH", "amount": 1500, "slots": 3})
    assert "₽" in out and "$" not in out


def test_ych_announce_names_the_missing_param(server):
    assert _call("sales", {"action": "ych_announce"}) == "Нужно: sale_id."


def test_report_without_date_is_last_month(server, monkeypatch):
    seen = []
    monkeypatch.setattr(report, "build", lambda conn, month, **kw: seen.append(month) or {"summary": "ok"})
    out = _call("sales", {"action": "report"})
    assert seen == [report.previous_month(load_config().posting.timezone or report.DEFAULT_TZ)]
    assert "прошлый месяц" in out
    assert report.previous_month("Europe/Moscow", NOW) == "2026-08"   # NOW — 21.09.2026
    assert report.previous_month("Europe/Moscow", 1_767_300_000.0) == "2025-12"   # январь → декабрь прошлого года


# --- Фон ---

class _NetFail:
    def __init__(self):
        self.calls = []

    def _request(self, *a, **kw):
        self.calls.append("req")
        raise DAApiError("нет связи с DeviantArt: ReadTimeout", "network")

    def deviation_comments(self, *a, **kw):
        self.calls.append("comments")
        raise DAApiError("нет связи с DeviantArt: ReadTimeout", "network")

    def gallery_folders(self, *a, **kw):
        self.calls.append("folders")
        raise DAApiError("нет связи с DeviantArt: ReadTimeout", "network")


def test_backfill_stops_after_one_network_timeout(conn):
    conn.execute("INSERT INTO group_scores (grp, folder, size, lots_per_day, niche_share, n, ts) "
                 "VALUES ('dragonadopts', '555', 100, 3.0, 0.5, 24, ?)", (NOW,))
    conn.execute("INSERT INTO group_scores (grp, folder, size, lots_per_day, niche_share, n, ts) "
                 "VALUES ('mothadopts', '556', 100, 3.0, 0.5, 24, ?)", (NOW,))
    for g, f in (("dragonadopts", "555"), ("mothadopts", "556")):
        conn.execute("INSERT INTO da_folders (grp, folder_id, uuid, name, size, ts) VALUES (?, ?, ?, 'Auctions', 100, ?)",
                     (g, f, f"F-{f}", NOW))
    conn.commit()
    backfill.enqueue(conn, None, "", 75, NOW, cfg=Config())
    assert conn.execute("SELECT COUNT(*) FROM backfill_state").fetchone()[0] >= 2
    client = _NetFail()
    lines = backfill.step(conn, client, NOW, sleep=lambda s: None)
    assert len(client.calls) == 1 and any("не отвечает" in line for line in lines)


def test_growth_pass_skips_rest_of_deviantart_after_timeout(server, monkeypatch):
    calls = []

    def part(name):
        def run(conn, client, *a, **kw):
            calls.append(name)
            client.whofaved("u")   # каждая часть идёт в DA
            return [] if name != "favers" else {}
        return run

    class Hang:
        def whofaved(self, *a, **kw):
            calls.append("net")
            raise DAApiError("нет связи с DeviantArt: ReadTimeout", "network")

    monkeypatch.setattr(backfill, "step", lambda conn, client, *a, **kw: part("backfill")(conn, client))
    from mcp_devian import similar
    monkeypatch.setattr(similar, "step", lambda conn, client, *a, **kw: part("similar")(conn, client))
    monkeypatch.setattr(buyers, "refresh_favers", lambda conn, client, *a, **kw: part("favers")(conn, client))
    monkeypatch.setattr(leads, "da_lf_scan", lambda conn, client, *a, **kw: part("da_lf")(conn, client) or {})
    monkeypatch.setattr(outreach, "watch_offer_replies",
                        lambda conn, client, *a, **kw: part("offer_replies")(conn, client) or {})
    pal = []
    from mcp_devian import palette
    monkeypatch.setattr(palette, "step", lambda *a, **kw: pal.append(1))
    cfg = load_config()
    cfg.deviantart.similar_watch = True
    with db.session() as conn:
        lines = monitor.growth_pass(conn, cfg, client=Hang(), palette_fetch=lambda url: b"",
                                    palette_sleep=lambda s: None)
    assert calls.count("net") == 1          # одна настоящая попытка — остальные части сразу без сети
    assert pal == [] and any("не отвечает" in line for line in lines)


def test_backfill_does_not_hold_write_lock_during_request(conn):
    from test_backfill import NOW as BNOW
    from test_backfill import FakeClient, enqueue_one, folder

    enqueue_one(conn)
    seen = []

    class Watch(FakeClient):
        def deviation_comments(self, uuid, max_comments=500):
            seen.append(conn.in_transaction)
            return super().deviation_comments(uuid, max_comments)

    backfill.step(conn, Watch({"FOLD-1": folder(1, 5)}), BNOW, sleep=lambda s: None)
    assert seen and not any(seen)


def test_commission_reminder_kept_when_notification_fails(server, monkeypatch):
    cfg = load_config()
    cfg.notify.macos = True
    save_config(cfg)
    cfg = load_config()
    monkeypatch.setattr(commissions, "pending_reminders",
                        lambda *a, **kw: [{"slot_id": "s1", "key": "deposit", "text": "предоплата не пришла"}])
    marked = []
    monkeypatch.setattr(commissions, "mark_reminded", lambda conn, rows, ts: marked.append(rows))
    monkeypatch.setattr(digest, "notify_lines", lambda cfg, title, lines: False)
    with db.session() as conn:
        monitor.money_pass(conn, cfg)
    assert marked == []
    monkeypatch.setattr(digest, "notify_lines", lambda cfg, title, lines: True)
    with db.session() as conn:
        monitor.money_pass(conn, cfg)
    assert len(marked) == 1


# --- Тексты ---

def test_buyers_check_wording_and_platform_links(conn):
    res = protection.check_buyer(conn, "Some_Buyer", platform="toyhouse")
    urls = [lk["url"] for lk in res["links"]]
    assert not any("deviantart.com/Some_Buyer" in u for u in urls) and any("toyhou.se/Some_Buyer" in u for u in urls)
    text = protection.render_check(res)
    assert "банные" not in text and "забаненные быстро меняют ники" in text
    da = protection.check_buyer(conn, "Some_Buyer", platform="deviantart")
    assert protection.TH_BLACKLIST_NOTE not in da["notes"]


def test_buyers_for_adopt_understands_word_forms(conn):
    moth = lot(conn, 1, "Dragon adopt", ts=NOW - 5 * DAY)
    sale(conn, "Dra", item_id=moth, title="Dragon adopt")
    buyers.sync(conn, NOW, my_username="her")
    assert outreach.adopt_tags(conn, ["дракона"])[0] == outreach.adopt_tags(conn, ["дракон"])[0] != []
