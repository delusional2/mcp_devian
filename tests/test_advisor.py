import re

import pytest

from mcp_devian import advisor, db, sales
from mcp_devian.config import Config
from mcp_devian.db import Item

NOW = 1_800_000_000.0   # пятница, 11:00 по Москве
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "a.sqlite3")
    yield c
    c.close()


@pytest.fixture
def cfg():
    return Config()


def lot(conn, n, title="Dragon adopt", lot_type="auction", usd=None, own=False, published=None, status="open",
        tags=(), snippet=None, place=None, source="deviantart", match_kind=None, extra=None, seen=None, author=None):
    item = Item(id=f"{'da' if source == 'deviantart' else 'th'}:{n}", source=source, title=title,
                url=f"https://www.deviantart.com/me/art/x-{n}", lot_type=lot_type, status=status, price_usd=usd,
                own=own, published_ts=published, activity_ts=published, snippet=snippet, place=place,
                match_kind=match_kind, author=author, extra={"tags": list(tags), **(extra or {})})
    db.upsert_item(conn, item, now=seen or published or NOW)
    return item.id


def bids(conn, item_id, top=None, n=0, ended=1, ab_hit=0, winner=None, updated=NOW):
    conn.execute("INSERT OR REPLACE INTO auctions (deviation_id, top_amount, currency, n_bids, ended, ab_hit, winner, "
                 "updated_ts) VALUES (?, ?, 'usd', ?, ?, ?, ?, ?)", (item_id, top, n, ended, ab_hit, winner, updated))
    conn.commit()


def kinds(items):
    return [a.kind for a in items]


def test_empty_base_gives_beginner_playbook_only(conn, cfg):
    items = advisor.advice(conn, cfg, NOW)
    assert kinds(items) == ["start"]
    start = items[0]
    assert "LF" in start.detail and "общая практика, не из твоих данных" in start.detail
    assert "лотерея" not in start.detail and "2–3 поста" not in start.detail
    assert "продаж пока 0" in start.evidence
    assert advisor.best_publish_time(conn, NOW) is None
    assert "Уверенность: средняя" in advisor.render_advice(items)
    assert "мало данных" in advisor.render_advice([]).lower()
    assert advisor.render_advice(items).count(advisor.GENTLE_ADVICE) == 1


def test_beginner_playbook_uses_market_numbers(conn, cfg):
    for i, usd in enumerate([5, 8, 10, 12, 15, 20]):
        lot(conn, i, f"Moth adopt #{i}", usd=usd, published=NOW - 3 * DAY, place="AdoptsGroup")
    lot(conn, 99, "Found", usd=5, published=NOW - 3 * DAY, place="поиск DA: moth")
    start = next(a for a in advisor.advice(conn, cfg, NOW) if a.kind == "start")
    assert "по 7 лотам" in start.detail and "AdoptsGroup (6)" in start.detail and "поиск DA" not in start.detail
    assert "n=7" in start.detail


def test_beginner_playbook_gone_after_three_sales(conn, cfg):
    for i in range(3):
        sales.add_sale(conn, title=f"S{i}", amount=10, currency="usd", platform="deviantart", format="auction",
                       status="done", now=NOW - DAY)
    assert "start" not in kinds(advisor.advice(conn, cfg, NOW))


def _market_at(conn, start_n, usd, sold, total):
    for i in range(total):
        iid = lot(conn, start_n + i, "Cat adopt", usd=usd, published=NOW - (20 + i) * DAY, status="closed",
                  author=f"m{start_n + i}")
        bids(conn, iid, top=usd * 2 if i < sold else None, n=2 if i < sold else 0)


def test_pricing_her_lots_beat_market(conn, cfg):
    for i in range(8):
        iid = lot(conn, i, usd=10, own=True, published=NOW - (10 + i) * DAY, status="closed", author="me")
        bids(conn, iid, top=40, n=6)
    _market_at(conn, 100, 10, 2, 10)
    a = next(a for a in advisor.advice(conn, cfg, NOW) if a.kind == "pricing")
    assert a.title == "Твои лоты получают ставку чаще рынка при том же старте"
    assert "8 из 8" in a.detail and "2 из 10" in a.detail and "P(у тебя чаще)" in a.detail
    assert "n=8" in a.evidence and a.confidence in ("низкая", "средняя")


def test_pricing_her_lots_lose_to_market(conn, cfg):
    for i in range(8):
        iid = lot(conn, i, usd=30, own=True, published=NOW - (10 + i) * DAY, status="closed", author="me")
        bids(conn, iid, top=None, n=0)
    _market_at(conn, 100, 30, 8, 10)
    a = next(a for a in advisor.advice(conn, cfg, NOW) if a.kind == "pricing")
    assert a.title == "Твои лоты получают ставку реже рынка при том же старте"
    assert "0 из 8" in a.detail and "8 из 10" in a.detail


def test_pricing_needs_enough_lots_and_market(conn, cfg):
    for i in range(4):
        iid = lot(conn, i, usd=10, own=True, published=NOW - 10 * DAY, status="closed")
        bids(conn, iid, top=50, n=5)
    assert "pricing" not in kinds(advisor.advice(conn, cfg, NOW))
    for i in range(4, 8):                     # 8 её лотов, но рынка при таком старте нет — молчим
        iid = lot(conn, i, usd=10, own=True, published=NOW - 10 * DAY, status="closed")
        bids(conn, iid, top=None, n=0)
    assert "pricing" not in kinds(advisor.advice(conn, cfg, NOW))


def test_running_auctions_are_not_finished(conn, cfg):
    for i in range(6):
        iid = lot(conn, i, f"Cat auction {i} [OPEN]", usd=15, own=True, published=NOW - 4 * DAY, status="open",
                  seen=NOW)
        bids(conn, iid, top=None, n=0, ended=0, updated=NOW)
    assert advisor._her_auction_outcomes(conn, NOW) == []
    text = advisor.render_advice(advisor.advice(conn, cfg, NOW))
    assert "закрылись без ставок" not in text


def test_autobuy_too_high(conn, cfg):
    for i in range(8):  # меньше 8 аукционов — по общей шкале «нет вывода», совета нет
        iid = lot(conn, i, usd=10, own=True, published=NOW - 10 * DAY, status="closed", snippet="SB: $10 AB: $100")
        bids(conn, iid, top=20, n=3)
    a = next(a for a in advisor.advice(conn, cfg, NOW) if a.kind == "autobuy")
    assert a.title == "Автобай никто не берёт" and "$100" in a.detail and "$20" in a.detail
    assert "около $35" not in a.detail and "общая практика, не из твоих данных" in a.detail


def test_autobuy_needs_eight_auctions(conn, cfg):
    for i in range(7):
        iid = lot(conn, i, usd=10, own=True, published=NOW - 10 * DAY, status="closed", snippet="SB: $10 AB: $100")
        bids(conn, iid, top=20, n=3)
    assert not [a for a in advisor.advice(conn, cfg, NOW) if a.kind == "autobuy"]


def test_unpaid_first(conn, cfg):
    sales.add_sale(conn, title="Moth #1", amount=25, currency="usd", platform="deviantart", format="auction",
                   buyer="bob", now=NOW - 5 * DAY)
    items = advisor.advice(conn, cfg, NOW)
    assert items[0].kind == "unpaid" and "Moth #1 — $25, bob, 5 дн" in items[0].detail


def test_groups_from_places_and_sent_log(conn, cfg):
    for i in range(8):
        good = lot(conn, i, own=True, published=NOW - 10 * DAY, usd=10,
                   extra={"places": ["hotgroup", "мои работы", "поиск DA: x"]})
        bad = lot(conn, 10 + i, own=True, published=NOW - 10 * DAY, usd=10, extra={"uuid": f"U{i}"})
        bids(conn, good, top=30, n=5)
        bids(conn, bad, top=None, n=0)
        conn.execute("INSERT INTO sent_log (kind, target, ref, ts, ok, detail) VALUES ('da_group', 'coldgroup', 'd', "
                     "?, 1, ?)", (NOW - 10 * DAY, f"U{i}"))
    # Отправка, которую не удалось сопоставить, просто не участвует.
    conn.execute("INSERT INTO sent_log (kind, target, ref, ts, ok, detail) VALUES ('da_group', 'coldgroup', 'd', "
                 "?, 1, 'unknown')", (NOW,))
    conn.commit()
    groups = advisor._group_lots(conn)
    assert set(groups) == {"hotgroup", "coldgroup"}
    ranked = advisor.group_ranking(conn)
    assert [g["group"] for g in ranked] == ["hotgroup", "coldgroup"]
    items = [a for a in advisor.advice(conn, cfg, NOW) if a.kind == "groups"]
    assert items[0].title == "Лучше всего работает hotgroup" and "8 из 8" in items[0].detail
    assert "совпадение" in items[0].detail
    assert items[1].title == "coldgroup пока не приносит ставок"


def test_groups_need_eight_lots_each_side(conn, cfg):
    for i in range(3):
        good = lot(conn, i, own=True, published=NOW - 10 * DAY, usd=10, extra={"places": ["hot"]})
        bad = lot(conn, 10 + i, own=True, published=NOW - 10 * DAY, usd=10, extra={"places": ["cold"]})
        bids(conn, good, top=30, n=5)
        bids(conn, bad, top=None, n=0)
    assert not [a for a in advisor.advice(conn, cfg, NOW) if a.title.startswith("Лучше всего")]


def _timing_data(conn, k_max=16):
    for k in range(1, k_max + 1):
        good = lot(conn, k, own=True, usd=10, published=NOW - k * 7 * DAY)                    # пт 11:00 МСК
        bad = lot(conn, 100 + k, own=True, usd=10, published=NOW - k * 7 * DAY - 2 * DAY + 8 * 3600)  # ср 19:00
        bids(conn, good, top=40, n=6)
        bids(conn, bad, top=None, n=0)


def test_best_publish_time_from_her_lots(conn, cfg):
    _timing_data(conn)
    best = advisor.best_publish_time(conn, NOW)
    assert best and best["weekday_name"] == "будни" and best["hours"] == "06–12" and best["source"] == "её лоты"
    assert "16 из 16" in best["evidence"] and "0 из 16" in best["evidence"] and "n=16" in best["evidence"]
    timing = next(a for a in advisor.advice(conn, cfg, NOW) if a.kind == "timing")
    assert timing.title == "Выкладывай в будни утром (06–12)"


def test_best_publish_time_needs_eight_per_slot(conn):
    _timing_data(conn, 5)
    assert advisor.best_publish_time(conn, NOW) is None


def test_best_publish_time_counts_group_submission(conn):
    _timing_data(conn)
    for k in range(1, 17):    # хорошие лоты на самом деле отправили в группы в среду вечером
        conn.execute("INSERT INTO sent_log (kind, target, ref, ts, ok, detail) VALUES ('da_group', 'g', 'd', ?, 1, ?)",
                     (NOW - k * 7 * DAY - 2 * DAY + 8 * 3600, f"https://www.deviantart.com/me/art/x-{k}"))
    conn.commit()
    best = advisor.best_publish_time(conn, NOW)
    assert best is None      # в одном окне теперь и хорошие, и плохие — разницы нет


def test_demand_by_tag_finished_bids_and_toyhouse_column(conn):
    for i in range(3):
        iid = lot(conn, i, "Dragon adopt", usd=10, published=NOW - 10 * DAY, status="closed")
        bids(conn, iid, top=20, n=8)
    running = lot(conn, 5, "Dragon adopt", usd=10, published=NOW - DAY, seen=NOW)
    bids(conn, running, top=20, n=5, ended=0)
    lot(conn, 50, "LF dragon OCs", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf",
        extra={"thread_key": "k1"})
    lot(conn, 51, "LF dragons pls", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf",
        extra={"thread_key": "k1"})
    lot(conn, 52, "LF dragon", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf",
        extra={"lf": {"closed": True}})
    lot(conn, 53, "LF anything", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf",
        extra={"lf": {"wanted_species": ["wolf"]}})
    rows = {d["tag"]: d for d in advisor.demand_by_tag(conn, NOW)}
    dragon = rows["вид:dragon"]
    assert dragon["lots"] == 4 and dragon["bids_n"] == 3 and dragon["avg_bids"] == 8 and dragon["th_threads"] == 1
    assert rows["вид:wolf"]["lots"] == 0 and rows["вид:wolf"]["th_threads"] == 1
    th = {d["tag"]: d for d in advisor.th_demand(conn, 14, NOW)}
    assert th["вид:dragon"]["threads"] == 1 and th["вид:dragon"]["species"] == "dragon"


def test_th_demand_advice(conn, cfg):
    for i in range(3):
        lot(conn, 50 + i, f"LF moth oc {i}", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf")
    a = next(a for a in advisor.advice(conn, cfg, NOW) if a.title == "Кого ищут на Toyhouse")
    assert "moth — 3" in a.detail and "n=3" in a.evidence and "не продажи" in a.detail


def test_stale_lots_and_characters_with_lf(conn, cfg):
    iid = lot(conn, 1, "Moth adopt — FLATSALE", lot_type="flatsale", usd=40, own=True, published=NOW - 20 * DAY)
    lot(conn, 1, "Moth adopt — FLATSALE", lot_type="flatsale", usd=40, own=True, published=NOW - 20 * DAY,
        seen=NOW - DAY)                                                         # видели открытым вчера
    lot(conn, 2, "Fresh adopt", lot_type="flatsale", usd=40, own=True, published=NOW - 2 * DAY)
    lot(conn, 3, "Gone quiet", lot_type="flatsale", usd=40, own=True, published=NOW - 20 * DAY)  # не видели 20 дн
    lot(conn, 50, "LF moth characters!", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf")
    cfg.posting.characters = [{"name": "Luna", "url": "https://toyhou.se/1.luna", "tags": ["moth"]},
                              {"name": "Sold", "url": "https://toyhou.se/2.sold", "tags": ["moth"]}]
    sales.add_sale(conn, title="Sold", amount=5, currency="usd", platform="toyhouse", format="flatsale",
                   character_url="https://toyhou.se/2.sold", status="done", now=NOW)
    items = [a for a in advisor.advice(conn, cfg, NOW) if a.kind == "stale"]
    stale = next(a for a in items if a.title.startswith("Не продаются"))
    assert stale.title == "Не продаются: 1" and "Moth adopt" in stale.detail and "Fresh" not in stale.detail
    assert "Gone quiet" not in stale.detail and "OTA" in stale.detail and "15–25%" not in stale.detail
    assert "общая практика" in stale.detail and "20 дн" in stale.detail and iid
    wanted = next(a for a in items if a.title == "Твоих персов ищут прямо сейчас")
    assert "Luna: 1 тред" in wanted.detail and "Sold:" not in wanted.detail and "«го»" in wanted.detail


def test_sold_fixed_price_lot_is_not_stale(conn, cfg):
    iid = lot(conn, 1, "Fox flatsale $20", lot_type="flatsale", usd=20, own=True, published=NOW - 20 * DAY,
              seen=NOW - DAY)
    sales.add_sale(conn, title="Fox", amount=20, currency="usd", platform="deviantart", format="flatsale",
                   item_id=iid, status="done", now=NOW - 15 * DAY)
    assert not [a for a in advisor._rule_stale(conn, cfg, NOW) if a.title.startswith("Не продаются")]


def test_time_to_close_replaces_fixed_threshold(conn):
    for i in range(10):
        lot(conn, i, "Cat flatsale", lot_type="flatsale", usd=10, own=True, published=NOW - 40 * DAY,
            seen=NOW - 40 * DAY)
        lot(conn, i, "Cat flatsale SOLD", lot_type="flatsale", usd=10, own=True, status="closed",
            published=NOW - 40 * DAY, seen=NOW - 40 * DAY + (2 + i % 3) * DAY)
    p80, n = advisor.time_to_close(conn, "flatsale", True)
    assert n == 10 and p80 == pytest.approx(4.0)
    days, why = advisor._stale_days(conn, "flatsale")
    assert days == 4 and "n=10" in why


def test_format_rule(conn, cfg):
    for i in range(8):
        sales.add_sale(conn, title="A", amount=40 + i, currency="usd", platform="deviantart", format="auction",
                       status="paid", now=NOW - DAY)
        sales.add_sale(conn, title="F", amount=15 + i, currency="usd", platform="deviantart", format="flatsale",
                       status="paid", now=NOW - DAY)
    sales.add_sale(conn, title="?", amount=999, currency=None, platform="deviantart", format="flatsale",
                   status="paid", now=NOW - DAY)
    a = next(a for a in advisor.advice(conn, cfg, NOW) if a.kind == "format")
    assert a.title == "Больше приносят аукционы" and "$999" not in a.detail and "1 продаж без валюты" in a.detail


def test_format_rule_needs_eight_each(conn, cfg):
    for amount in (40, 50):
        sales.add_sale(conn, title="A", amount=amount, currency="usd", platform="deviantart", format="auction",
                       status="paid", now=NOW - DAY)
        sales.add_sale(conn, title="F", amount=amount - 30, currency="usd", platform="deviantart", format="flatsale",
                       status="paid", now=NOW - DAY)
    assert "format" not in kinds(advisor.advice(conn, cfg, NOW))


def test_weekly_report(conn, cfg):
    sales.add_sale(conn, title="A", amount=40, currency="usd", platform="deviantart", format="auction",
                   status="paid", now=NOW - DAY)
    sales.add_sale(conn, title="B", amount=10, currency="usd", platform="deviantart", format="auction",
                   status="paid", now=NOW - 10 * DAY)
    sales.add_sale(conn, title="C", amount=7, currency="usd", platform="deviantart", format="auction", now=NOW)
    sales.add_sale(conn, title="D", amount=5, currency=None, platform="deviantart", format="auction",
                   status="paid", now=NOW - DAY)
    text = advisor.weekly_report(conn, cfg, NOW)
    assert "Заработано: $40 за 1 продаж (+$30 к прошлой неделе) (n=1" in text and "её продажи" in text
    assert "Ещё 1 оплаченных продаж без валюты" in text and "$45" not in text
    assert "Ждут оплаты: 1 на $7" in text
    assert "Что можно сделать" in text and advisor.GENTLE_ADVICE in text


def test_weekly_report_empty(conn, cfg):
    text = advisor.weekly_report(conn, cfg, NOW)
    assert "Оплаченных продаж на этой неделе нет." in text


def test_th_reply_conversion(conn, cfg):
    for i in range(5):
        conn.execute("INSERT INTO sent_log (kind, target, ref, ts, ok, detail) VALUES ('th_reply', ?, ?, ?, 1, '')",
                     (f"th:{i}", "https://toyhou.se/1.luna" if i < 3 else "https://toyhou.se/2.mira", NOW - 10 * DAY))
    conn.commit()
    sales.add_sale(conn, title="Luna", amount=30, currency="usd", platform="toyhouse", format="flatsale",
                   character_url="https://toyhou.se/1.luna", status="paid", now=NOW - 5 * DAY)
    conv = advisor.th_reply_conversion(conn, 60, NOW)
    assert (conv["replies"], conv["characters"], conv["sales"]) == (5, 2, 1)
    assert "откликов 5 → продаж этих персов 1" in advisor.weekly_report(conn, cfg, NOW)


def test_launch_plan_with_market(conn, cfg):
    for i, usd in enumerate([8, 10, 12, 15, 15, 20]):
        iid = lot(conn, i, f"Dragon adopt auction #{i}", usd=usd, published=NOW - 5 * DAY,
                  extra={"api_tags": ["dragon", "adopt", "fantasy art", "dragon-oc", "wings"]})
        bids(conn, iid, top=usd * 3, n=4)
    lot(conn, 50, "LF dragon OCs", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf")
    lot(conn, 51, "LF cats only", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf")
    text = advisor.launch_plan(conn, cfg, species="dragon", fmt="auction", now=NOW)
    assert "Формат: auction" in text and "[OPEN] Dragon adopt — AUCTION" in text
    assert "AUCTION |" not in text and "MI $2" not in text and "AB $60" not in text
    assert "Вывода нет: старт по данным" in text and "общая практика, не из твоих данных: MI $1–2" in text
    tags_line = next(line for line in text.splitlines() if line.startswith("Теги"))
    tags = tags_line.split(": ", 2)[-1].split(", ")
    assert "dragon" in tags and "wings" in tags and "fantasyart" in tags
    assert all(re.fullmatch(r"[A-Za-z0-9_]+", t) for t in tags) and len(tags) <= 15
    assert "наборов групп пока нет" in text and "DA Exclusives" in text and "Core" in text
    assert "LF dragon OCs" in text and "LF cats only" not in text
    assert "30 минут" not in text and "15–25%" not in text and "через сутки" in text
    assert "когда закончится аукцион" in text
    assert "вечером в будни" not in text and "Когда: Вывода нет" in text
    assert "Уверенность:" in text


def test_launch_plan_without_data_is_honest(conn, cfg):
    cfg.posting.group_presets = {"main": [{"group": "AdoptGroup"}]}
    text = advisor.launch_plan(conn, cfg, species="moth", now=NOW)
    assert "Формат: ota" in text and "Уверенность: нет вывода" in text and "Мало данных" in text
    assert "«main»: AdoptGroup" in text
    assert "живых LF-тредов под это сейчас не нашла" in text


def test_launch_plan_groups_by_market_scorecard(conn, cfg):
    cfg.posting.group_presets = {"main": [{"group": "Small"}, {"group": "Big"}]}
    # 40 лотов на странице, но итог известен у 10: доля 60% — из 10, это и есть n.
    conn.execute("INSERT INTO group_scores (grp, folder, size, lots_per_day, niche_share, median_sb, unit, bid_share, "
                 "n, ts, known, sb_n) VALUES ('big', 'f', 9000, 7, 0.2, 15, 'usd', 0.6, 40, ?, 10, 30)", (NOW,))
    conn.commit()
    text = advisor.launch_plan(conn, cfg, species="moth", now=NOW)
    assert ("«main»: Big (по рынку, не по твоим продажам: ставку получают 60% лотов, n=10 с известным итогом), "
            "Small") in text


def test_launch_plan_respects_chosen_format(conn, cfg):
    for i, usd in enumerate([20, 25, 30, 35, 45]):
        lot(conn, i, f"Moth flatsale #{i}", lot_type="flatsale", usd=usd, published=NOW - 5 * DAY, tags=["moth"])
    text = advisor.launch_plan(conn, cfg, species="moth", fmt="flatsale", now=NOW)
    assert "Формат: flatsale — она так выбрала" in text and "Цена: около $30" in text and "n=5" in text
    assert "[OPEN] Moth adopt — FLATSALE" in text and "FLATSALE |" not in text


def test_listing_review_bad_post(conn, cfg):
    lot(conn, 1, "my new oc", lot_type="other", own=True, published=NOW - DAY, snippet="hi", tags=["art"])
    row = conn.execute("SELECT * FROM items WHERE id = 'da:1'").fetchone()
    text = "\n".join(advisor.listing_review(row, conn, cfg, now=NOW))
    for part in ("[OPEN]", "формата", "видом", "TOS", "оплаты", "не видела"):
        assert part in text
    assert "Тегов" not in text and "в разы больше" not in text and "15–25" not in text


def test_listing_review_without_tags_blames_rss(conn, cfg):
    lot(conn, 1, "[OPEN] Dragon adopt auction", own=True, published=NOW - DAY, snippet="SB: $15")
    row = conn.execute("SELECT * FROM items WHERE id = 'da:1'").fetchone()
    text = "\n".join(advisor.listing_review(row, conn, cfg, now=NOW))
    assert "RSS DeviantArt не отдаёт теги" in text and "Тегов 0" not in text
    assert "deviantartsupport.com" in text and "30" in text
    assert "когда закончится аукцион" in text


def test_listing_review_reads_full_description(conn, cfg):
    lot(conn, 1, "[OPEN] Dragon adopt auction", own=True, published=NOW - DAY, snippet="SB: $15 MI: $2",
        tags=["dragon"],
        extra={"desc": "SB: $15 MI: $2 AB: $80. Ends 48 hours after the last bid (GMT+3). "
                       "Payment via PayPal invoice. You get a non-watermarked PNG. Please read my TOS."})
    row = conn.execute("SELECT * FROM items WHERE id = 'da:1'").fetchone()
    text = "\n".join(advisor.listing_review(row, conn, cfg, now=NOW))
    assert "оплаты" not in text and "TOS" not in text and "закончится" not in text and "получает" not in text


def test_listing_review_good_post(conn, cfg):
    tags = ["dragon", "adopt", "adoptable", "auction", "fantasy", "wings", "oc", "openadopt", "dragonadopt"]
    lot(conn, 1, "[OPEN] Dragon adopt — AUCTION", lot_type="auction", own=True, published=NOW - DAY,
        tags=tags, snippet="SB: $15 MI: $2 AB: $80. Ends in 48h. Payment via PayPal. TOS in my journal. "
                           "You get a full res PNG.", extra={"places": ["g"]})
    row = conn.execute("SELECT * FROM items WHERE id = 'da:1'").fetchone()
    out = advisor.listing_review(row, conn, cfg, now=NOW)
    assert out[0] == "По этим проверкам пост оформлен хорошо." and "Видела этот пост в группах: g" in out[1]


def test_listing_review_timing(conn, cfg):
    _timing_data(conn)
    row = conn.execute("SELECT * FROM items WHERE id = 'da:101'").fetchone()
    text = "\n".join(advisor.listing_review(row, conn, cfg, now=NOW))
    assert "в среду в 19:00" in text and "в будни утром (06–12)" in text and "n=16" in text


def test_broken_rule_does_not_hide_others(conn, cfg, monkeypatch):
    sales.add_sale(conn, title="X", amount=5, currency="usd", platform="deviantart", format="auction",
                   now=NOW - 5 * DAY)
    monkeypatch.setattr(advisor, "_rule_pricing", lambda *a: 1 / 0)
    assert "unpaid" in kinds(advisor.advice(conn, cfg, NOW))


# --- Находки проверки: Toyhouse без исключённых видов, честные периоды и итоги ---

def test_th_demand_skips_species_the_buyer_excluded(conn):
    lot(conn, 60, "LF characters", snippet="no dragons please, no cats", source="toyhouse", lot_type=None,
        published=NOW - DAY, match_kind="lf")
    lot(conn, 61, "LF ocs, NOT dragons", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf")
    lot(conn, 62, "LF ocs", snippet="not dragons", source="toyhouse", lot_type=None, published=NOW - DAY,
        match_kind="lf", extra={"lf": {"wanted_species": [], "negated": ["dragon"]}})
    th = {d["tag"] for d in advisor.th_demand(conn, 14, NOW)}
    assert "вид:dragon" not in th and "вид:cat" not in th
    assert all(d["th_threads"] == 0 for d in advisor.demand_by_tag(conn, NOW) if d["tag"] == "вид:dragon")


def test_launch_plan_skips_threads_that_exclude_her_species(conn, cfg):
    lot(conn, 70, "LF ocs, NOT dragons", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf")
    lot(conn, 71, "LF characters", snippet="no dragons please", source="toyhouse", lot_type=None,
        published=NOW - DAY, match_kind="lf")
    lot(conn, 72, "LF: dragons!", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf")
    text = advisor.launch_plan(conn, cfg, species="dragon", now=NOW)
    assert "[th:72]" in text and "[th:70]" not in text and "[th:71]" not in text


def test_th_rows_carry_their_own_period(conn):
    lot(conn, 80, "LF: wolves", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf")
    row = next(d for d in advisor.demand_by_tag(conn, NOW, days=30) if d["tag"] == "вид:wolf")
    assert row["period"] == "30 дн" and row["th_period"] == "14 дн"


def test_th_total_counts_open_threads_once(conn, cfg):
    for i in range(3):
        lot(conn, 90 + i, f"LF moth oc {i}", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf",
            extra={"thread_key": f"m{i}"})
    lot(conn, 93, "LF moth oc 0 (crosspost)", source="toyhouse", lot_type=None, published=NOW - DAY,
        match_kind="lf", extra={"thread_key": "m0"})
    lot(conn, 94, "LF moths [closed]", source="toyhouse", lot_type=None, published=NOW - DAY, match_kind="lf",
        extra={"lf": {"closed": True}})
    a = next(a for a in advisor.advice(conn, cfg, NOW) if a.title == "Кого ищут на Toyhouse")
    assert "(всего тредов 3)" in a.detail


def test_weekly_report_says_data_is_stale(conn, cfg):
    db.record_run(conn, "deviantart", None, NOW - 10 * DAY)
    db.record_run(conn, "deviantart", "сеть", NOW)
    text = advisor.weekly_report(conn, cfg, NOW)
    assert text.startswith("Внимание:") and "данные старые" in text


def test_transferred_but_unpaid_is_not_revenue(conn):
    """«Перс передан» без оплаты не входит в «больше приносят»: это ещё не деньги."""
    sid = sales.add_sale(conn, title="Луна", amount=500, currency="usd", platform="toyhouse", format="auction",
                         buyer="alice", now=NOW - 5 * DAY)
    sales.set_status(conn, sid, "transferred", now=NOW - 4 * DAY)
    paid = sales.add_sale(conn, title="Сол", amount=30, currency="usd", platform="toyhouse", format="flatsale",
                          buyer="bob", now=NOW - 5 * DAY)
    sales.set_status(conn, paid, "paid", now=NOW - 4 * DAY)
    by, _ = advisor._paid_by_format(conn, NOW)
    assert dict(by["usd"]) == {"flatsale": [30]}
