import json
from datetime import UTC, datetime

import pytest

from mcp_devian import db, forecast
from mcp_devian.db import Item

DAY = 86400
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()   # через 10 дней после Хэллоуина
OCT_15 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC).timestamp()
SEPT_1 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "f.sqlite3")
    yield c
    c.close()


_n = [0]


def lot(conn, title="Dragon adopt", usd=10.0, author=None, own=False, published=NOW - 10 * DAY, tags=(),
        snippet=None, sold=True, top=None, n_bids=None, ended=1, updated=None, ab_hit=0, points=None,
        lot_type="auction", status="closed", place=None, seen=None, extra=None):
    _n[0] += 1
    iid = f"da:{_n[0]}"
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=title, url=f"https://www.deviantart.com/x/art/a-{_n[0]}",
                              author=author or f"seller{_n[0]}", lot_type=lot_type, status=status,
                              price_usd=usd if points is None else None, price_points=points, own=own, place=place,
                              published_ts=published, snippet=snippet, extra={"tags": list(tags), **(extra or {})}),
                   now=seen or published)
    if lot_type in ("auction", "bta"):
        bids = (n_bids if n_bids is not None else 3) if sold else 0
        amount = (top if top is not None else (usd or points or 5) * 2) if sold else None
        conn.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, bidders, ended, ab_hit, "
                     "updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                     (iid, amount, "usd" if points is None else "points", bids, min(bids, 2), ended, ab_hit,
                      updated if updated is not None else published + 8 * DAY))
        conn.commit()
    return iid


def watchers(conn, author, n):
    conn.execute("INSERT OR REPLACE INTO author_stats (author, watchers, deviations, ts) VALUES (?, ?, 0, ?)",
                 (author.lower(), n, NOW))
    conn.commit()


# --- Данные: какие итоги известны ---

def test_only_resolved_auctions_count(conn):
    lot(conn, "A", published=NOW - 10 * DAY)                                                # автор закрыл
    lot(conn, "B", published=NOW - DAY, ended=0, status="open", updated=NOW)                  # ещё идёт
    lot(conn, "C", published=NOW - 20 * DAY, ended=0, status="open", sold=False)              # дочитан в 8 дней
    lot(conn, "C-early", published=NOW - 20 * DAY, ended=0, status="open", sold=False,
        updated=NOW - 16 * DAY)                                                               # прочитан в 4 дня
    lot(conn, "C-running", published=NOW - 20 * DAY, ended=0, status="open", sold=False,
        seen=NOW - 12 * DAY, updated=NOW - 12 * DAY)                                          # в ленте ещё [OPEN]
    lot(conn, "D", usd=None)                                                                  # без цены
    lot(conn, "E flatsale", lot_type="flatsale")                                              # не аукцион
    got = [o.title for o in forecast.load_outcomes(conn, NOW)]
    assert got == ["C", "A"]
    c, a = forecast.load_outcomes(conn, NOW)
    assert a.sold and a.ratio == 2.0 and not c.sold and c.final is None
    assert forecast.outcome_coverage(conn, NOW) == (2, 4)       # C, A из C, C-early, C-running, A


def test_read_age_from_extra_wins(conn):
    iid = lot(conn, "X", published=NOW - 20 * DAY, ended=0, status="open", sold=False, updated=NOW - 18 * DAY)
    assert forecast.load_outcomes(conn, NOW) == []
    conn.execute("UPDATE auctions SET extra = '{\"read_age_days\": 7.5}' WHERE deviation_id = ?", (iid,))
    conn.commit()
    assert [o.title for o in forecast.load_outcomes(conn, NOW)] == ["X"]


def test_zero_comments_by_stats_is_known(conn):
    iid = lot(conn, "Quiet", published=NOW - 20 * DAY, ended=0, status="open", sold=False, updated=NOW - 19 * DAY)
    conn.execute("UPDATE auctions SET extra = '{\"zero_by_stats\": true}' WHERE deviation_id = ?", (iid,))
    conn.commit()
    [o] = forecast.load_outcomes(conn, NOW)
    assert not o.sold and o.n_bids == 0


def test_bidders_list_used_for_distinct_bidders(conn):
    iid = lot(conn, "Busy", n_bids=4)
    conn.execute("UPDATE auctions SET extra = ? WHERE deviation_id = ?",
                 ('{"bidders": [{"user": "@Ann", "amount": 12}, {"user": "ann", "amount": 15}, {"user": "bo"}]}', iid))
    conn.commit()
    [o] = forecast.load_outcomes(conn, NOW)
    assert o.bidder_names == ("ann", "bo") and o.bidders == 2


def test_ab_hit_not_in_ratio(conn):
    lot(conn, "Auto", usd=10, top=60, ab_hit=1)
    [o] = forecast.load_outcomes(conn, NOW)
    assert o.sold and o.final == 60 and o.ratio is None


def test_coverage_refusal(conn):
    for i in range(4):
        lot(conn, f"Read early {i}", published=NOW - 20 * DAY, ended=0, status="open", sold=False,
            updated=NOW - 18 * DAY)
    lot(conn, "Known", published=NOW - 20 * DAY)
    rep = forecast.niches(conn, now=NOW)
    assert rep.refusal and "итог известен только у 20%" in rep.refusal and "нужно ≥70%" in rep.refusal
    assert rep.refusal in forecast.render_niches(rep)
    plan = forecast.price_plan(conn, now=NOW)
    assert plan.best is None and "нужно ≥70%" in forecast.render_price_plan(plan)


# --- Сезон: только явная тема и только по данным ---

def test_season_phase():
    assert forecast.season_phase("halloween", OCT_15) == "in"
    assert forecast.season_phase("halloween", NOW) == "post"
    assert forecast.season_phase("halloween", SEPT_1) == "off"
    assert forecast.season_phase(None, NOW) == "off"
    jan = datetime(2027, 1, 5, tzinfo=UTC).timestamp()
    assert forecast.season_phase("christmas", jan) == "post"
    assert forecast.season_group(forecast.taxonomy.tag_text("Gothic vampire adopt")) is None   # тёмное ≠ праздник
    assert forecast.season_group(forecast.taxonomy.tag_text("pumpkin cat")) == "halloween"


def test_easter_is_computed():
    assert forecast.easter(2030).isoformat() == "2030-04-21"
    assert forecast.easter(2026).isoformat() == "2026-04-05"
    assert forecast.season_phase("easter", datetime(2030, 4, 30, tzinfo=UTC).timestamp()) == "in"
    assert forecast.season_phase("easter", datetime(2030, 5, 20, tzinfo=UTC).timestamp()) == "post"
    assert forecast.season_phase("easter", datetime(2030, 3, 20, tzinfo=UTC).timestamp()) == "off"


def test_pava_monotone():
    assert forecast._pava_decreasing([0.5, 0.7, 0.3, 0.4], [1, 1, 1, 1]) == pytest.approx([0.6, 0.6, 0.35, 0.35])


# --- Уровень продавца: по вотчерам, одинаково для неё и для других ---

def test_tiers_from_watchers(conn):
    lot(conn, "Moth auction #72", author="naruakin")
    lot(conn, "Cat", author="busy")
    lot(conn, "My first", own=True, author="me")
    watchers(conn, "naruakin", 12000)
    watchers(conn, "busy", 800)
    t = forecast.SellerTiers(conn)
    assert (t.tier("naruakin"), t.tier("busy"), t.tier("nobody")) == ("established", "growing", None)
    assert forecast.her_tier(conn).key == "unknown"          # о ней данных нет — уровни не сравнимы
    watchers(conn, "me", 120)
    assert forecast.her_tier(conn).key == "newcomer"


def test_no_tier_data_pools_all_and_says_so(conn):
    for i in range(14):
        lot(conn, "Cat adopt", published=NOW - (10 + i) * DAY, sold=i % 2 == 0)
    plan = forecast.price_plan(conn, unit="usd", now=NOW)
    assert plan.tier_fallback and forecast.NO_TIERS in plan.caveats
    assert "все продавцы" in plan.population and "без её лотов" in plan.population


def test_price_plan_compares_with_her_tier(conn):
    # Известные авторы продают со стартом $100, новички с таким стартом — нет.
    for i in range(10):
        lot(conn, f"Doll auction #{60 + i}", usd=100, author=f"star{i}", published=NOW - (20 + i) * DAY, top=150)
        watchers(conn, f"star{i}", 20000)
    for i in range(10):
        lot(conn, f"Doll {i}", usd=100, author=f"new{i}", published=NOW - (20 + i) * DAY, sold=False)
        lot(conn, f"Doll cheap {i}", usd=12, author=f"new{i}", published=NOW - (20 + i) * DAY, top=18)
        watchers(conn, f"new{i}", 50)
    lot(conn, "My doll", own=True, author="me", usd=10, published=NOW - 40 * DAY, top=15)
    watchers(conn, "me", 30)
    plan = forecast.price_plan(conn, unit="usd", now=NOW)
    assert plan.tier.key == "newcomer" and not plan.tier_fallback and plan.pool_n == 20
    high = next(c for c in plan.candidates if c["lo"] == 100)
    low = next(c for c in plan.candidates if c["lo"] == 10)
    assert high["k"] == 0 and low["k"] == 10
    assert plan.best and plan.best["start"] <= 20


def test_one_seller_capped(conn):
    for i in range(20):
        lot(conn, "Moth adopt", usd=5, author="flood", published=NOW - (10 + i) * DAY)
    for i in range(12):
        lot(conn, "Moth adopt", usd=20, published=NOW - (10 + i) * DAY, sold=False)
    pool = forecast.market_pool(forecast.load_outcomes(conn, NOW))
    flood = [o for o in pool if o.author == "flood"]
    assert len(flood) <= 3 * 1 + 3     # не больше 3 за каждое 30-дневное окно
    assert len(pool) == 12 + len(flood)


def test_search_and_multi_not_in_rates(conn):
    lot(conn, "Found by search", place="поиск DA: dragon adopt")
    lot(conn, "Batch of 3", extra={"multi": True})
    lot(conn, "Normal", place="da-adopts: Auctions")
    outs = forecast.load_outcomes(conn, NOW)
    assert {o.title: (o.search, o.multi) for o in outs} == {
        "Found by search": (True, False), "Batch of 3": (False, True), "Normal": (False, False)}
    assert [o.title for o in forecast.market_pool(outs)] == ["Normal"]


# --- Вес свежих данных, сезон, малые выборки (модель) ---

def test_recent_lots_weigh_more(conn):
    for i in range(6):
        lot(conn, "Wolf adopt", published=NOW - (90 + i) * DAY)               # давно продавались
        lot(conn, "Wolf adopt", published=NOW - (9 + i) * DAY, sold=False)    # сейчас — нет
    pool = forecast.load_outcomes(conn, NOW)
    ref = forecast._cutoff(pool)
    p, _ = forecast._shrunk(pool, ref, 0.5, 1.0)
    assert p < 0.2


def test_shrinkage_small_niche(conn):
    for i in range(20):
        lot(conn, "Cat adopt", published=NOW - (9 + i % 10) * DAY, sold=i < 4)   # 20% рынка
    lot(conn, "Moth adopt", published=NOW - 10 * DAY)
    lot(conn, "Moth adopt", published=NOW - 11 * DAY)                           # 2 из 2
    pool = forecast.load_outcomes(conn, NOW)
    pr = forecast.predict(pool, forecast.taxonomy.tag_text("moth"), None, "usd", forecast._cutoff(pool), NOW)
    assert pr.niche_n == 2 and 0.2 < pr.p < 0.55


def test_post_season_uses_post_season_data(conn):
    for i in range(8):
        lot(conn, "Halloween pumpkin adopt", published=OCT_15 - i * DAY)                       # в сезон — со ставкой
        lot(conn, "Spooky ghost adopt", published=NOW - (3 + i % 3) * DAY, sold=False, updated=NOW)  # после — нет
    for i in range(8):
        lot(conn, "Dragon adopt", published=NOW - (9 + i) * DAY, sold=i % 2 == 0, updated=NOW)
    rep = forecast.niches(conn, now=NOW)
    hw = next(d for d in rep.niches if d["key"] == "сезон:halloween")
    assert hw["phase"] == "post" and hw["p_sold"] < hw["p_all"] and "после сезона — по 8 лотам" in hw["season"]
    assert rep.phases["halloween"] == "post"


def test_no_post_season_penalty_without_data(conn):
    for i in range(10):
        lot(conn, "Pumpkin witch adopt", published=OCT_15 - i * DAY)
    pool = forecast.load_outcomes(conn, NOW)
    ref = forecast._cutoff(pool)
    tags = forecast.taxonomy.tag_text("pumpkin witch")
    in_season = forecast.predict(pool, tags, None, "usd", ref, OCT_15)
    after = forecast.predict(pool, tags, None, "usd", ref, NOW)
    assert after.p == pytest.approx(in_season.p) and after.season == "после сезона данных нет"
    assert not hasattr(forecast, "POST_SEASON_PRIOR")


def test_niches_rank_and_saturation(conn):
    lot(conn, "Old flatsale", lot_type="flatsale", published=NOW - 40 * DAY, place="grp")   # папка давно в работе
    for i in range(8):
        lot(conn, "Dragon adopt", published=NOW - (8 + i) * DAY, place="grp")
        lot(conn, "Cat adopt", published=NOW - (8 + i) * DAY, sold=i == 0, place="grp")
        lot(conn, "Cat adopt", published=NOW - (8 + i) * DAY, sold=False, place="grp")
    rep = forecast.niches(conn, now=NOW)
    dragon = next(d for d in rep.niches if d["key"] == "вид:dragon")
    cat = next(d for d in rep.niches if d["key"] == "вид:cat")
    assert dragon["verdict"] == "sells" and (dragon["sold"], dragon["lots"]) == (8, 8)
    assert cat["verdict"] == "saturated" and cat["supply"] == 16
    assert rep.tested == 2 and rep.threshold == pytest.approx(0.95)
    text = forecast.render_niches(rep, now=NOW)
    assert "Чаще остальных получают ставку" in text and "Реже остальных" in text
    assert "покупают" not in text and "набирают" not in text and "окончательн" not in text
    assert "n=8" in text and "уверенность" in text
    assert text == forecast.render_niches(forecast.niches(conn, now=NOW + 3600), now=NOW)


def test_supply_ignores_search_and_young_folders(conn):
    lot(conn, "Old", lot_type="flatsale", published=NOW - 40 * DAY, place="grp")
    for i in range(10):
        lot(conn, "Cat adopt", published=NOW - (8 + i) * DAY, place="grp", sold=i < 2)
        lot(conn, "Cat adopt", published=NOW - (8 + i) * DAY, place="поиск DA: cat", sold=False)
        lot(conn, "Dog adopt", published=NOW - (8 + i) * DAY, place="new folder")
    rep = forecast.niches(conn, now=NOW)
    cat = next(d for d in rep.niches if d["key"] == "вид:cat")
    assert cat["supply"] == 10 and cat["lots"] == 10        # поиск не в долях и не в предложении


# --- Цена ---

def test_price_plan_uses_lower_bound(conn):
    for i in range(12):
        lot(conn, "Moth adopt", usd=8, published=NOW - (9 + i) * DAY, sold=i % 10 != 0, top=24)    # ~90%, ×3
        lot(conn, "Moth adopt", usd=18, published=NOW - (9 + i) * DAY, sold=i % 2 == 0, top=27)    # 50%, ×1.5
        lot(conn, "Moth adopt", usd=60, published=NOW - (9 + i) * DAY, sold=False, snippet="SB $60 AB: $200")
    plan = forecast.price_plan(conn, species=["moth"], unit="usd", now=NOW)
    ps = [c["p_sold"] for c in plan.candidates]
    assert ps == sorted(ps, reverse=True)
    assert plan.best["start"] == 8 and plan.best["k"] == 10 and plan.best["n"] == 12
    assert plan.confidence in ("низкая", "средняя")
    assert plan.ab["n"] == 12 and plan.ab["hits"] == 0 and "suggest" not in plan.ab
    text = forecast.render_price_plan(plan, now=NOW)
    assert "Решение: старт $8" in text and "10 из 12" in text and "по модели" in text
    assert "взяли 0 раз" in text and "общая практика, не из твоих данных: AB обычно 3–5× SB" in text
    assert "набирают" not in text and "окончательн" not in text and "не пересматривай" not in text.lower()


def test_price_plan_too_little_data(conn):
    lot(conn, "Moth adopt")
    plan = forecast.price_plan(conn, species=["moth"], now=NOW)
    text = forecast.render_price_plan(plan)
    assert plan.best is None and "нужно хотя бы 12" in text and text.count("Вывода нет:") == 1


def test_points_are_separate(conn):
    for i in range(14):
        lot(conn, "Bat adopt", usd=None, points=200, published=NOW - (9 + i) * DAY)
    assert forecast.price_plan(conn, unit="usd", now=NOW).pool_n == 0
    plan = forecast.price_plan(conn, unit="points", now=NOW)
    assert plan.pool_n == 14 and plan.best and "pts" in forecast.render_price_plan(plan)


def test_weak_calibration_caps_confidence(conn):
    for i in range(45):
        lot(conn, "Cat adopt", usd=10, published=NOW - (9 + i) * DAY, sold=i % 4 != 0)
    plan = forecast.price_plan(conn, unit="usd", now=NOW)
    assert plan.best and plan.best["n"] == 45
    assert plan.skill["skill"] is None or plan.skill["skill"] <= 0 or plan.skill["n"] < forecast.MIN_SKILL_N
    assert plan.confidence == "низкая" and any("не точнее простого среднего" in c for c in plan.caveats)


# --- Проверка на прошлом ---

def test_calibration_without_lookahead(conn, monkeypatch):
    for i in range(40):
        lot(conn, "Dragon adopt" if i % 2 else "Cat adopt", published=NOW - (80 - 2 * i) * DAY, sold=i % 2 == 1,
            updated=NOW - (80 - 2 * i) * DAY + 3 * DAY)
    seen = []
    real = forecast.predict
    forecast._CAL_CACHE.clear()

    def spy(pool, tags, start, unit, ref, phase_ts=None):
        assert all(o.known_ts < ref for o in pool)
        seen.append(len(pool))
        return real(pool, tags, start, unit, ref, phase_ts)
    monkeypatch.setattr(forecast, "predict", spy)
    cal = forecast.calibration(conn, NOW)
    assert cal["n"] == len(seen) >= forecast.MIN_CALIBRATION and seen == sorted(seen)
    assert cal["brier"] < cal["brier_base"] and cal["skill"] > 0   # вид предсказывает итог лучше среднего
    assert cal["hit_rate"] > 0.8
    text = forecast.render_calibration(cal)
    assert "Проверка на прошлом" in text and "точнее среднего" in text and "skill" in text


def test_calibration_says_how_many_needed(conn):
    for i in range(5):
        lot(conn, "Cat adopt", published=NOW - (30 - i) * DAY)
    cal = forecast.calibration(conn, NOW)
    assert cal["n"] == 0 and cal["needed"] == forecast.MIN_CALIBRATION
    assert f"ещё {forecast.MIN_CALIBRATION}" in forecast.render_calibration(cal)


def test_empty_database_is_honest(conn):
    rep = forecast.niches(conn, now=NOW)
    assert "Вывода нет" in forecast.render_niches(rep)
    assert forecast.price_plan(conn, now=NOW).best is None


# --- Советник пользуется прогнозом ---

def test_advisor_demand_and_pricing_use_forecast(conn):
    from mcp_devian import advisor
    from mcp_devian.config import Config

    lot(conn, "Old flatsale", lot_type="flatsale", published=NOW - 40 * DAY, place="grp")
    for i in range(10):
        lot(conn, "Dragon adopt", usd=10, published=NOW - (8 + i) * DAY, top=25, place="grp")
        lot(conn, "Cat adopt", usd=10, published=NOW - (8 + i) * DAY, sold=i == 0, place="grp")
        lot(conn, "Cat adopt", usd=30, published=NOW - (8 + i) * DAY, sold=False, place="grp")
    items = advisor.advice(conn, Config(), NOW)
    hot = next(a for a in items if a.title == "Где аукционы чаще получают ставку")
    assert "dragon — 10 из 10" in hot.detail and "все продавцы" in hot.detail
    cold = next(a for a in items if a.title == "Где рынок перегрет")
    assert "cat" in cold.detail
    price = next(a for a in items if a.title.startswith("Старт по рынку"))
    assert "старта $10" in price.detail and price.confidence != "нет вывода" and "n=20" in price.evidence
    assert "аукционов рынка в целом" in price.detail and "похожих" not in price.detail  # без тегов — не «похожих»


def test_launch_plan_uses_forecast_price(conn):
    from mcp_devian import advisor
    from mcp_devian.config import Config

    for i in range(12):
        lot(conn, "Moth adopt", usd=8, published=NOW - (9 + i) * DAY, sold=i % 10 != 0, top=24)
        lot(conn, "Moth adopt", usd=60, published=NOW - (9 + i) * DAY, sold=False, snippet="SB $60 AB: $200")
    text = advisor.launch_plan(conn, Config(), species="moth", fmt="auction", now=NOW)
    assert "Цена: SB $8" in text and "10 из 12" in text and "Ниша: по модели" in text
    assert "Автобай на рынке: был у 12 лотов, взяли 0 раз" in text
    assert "общая практика, не из твоих данных: MI $1–2, AB 3–5× SB" in text
    assert "MI $2" not in text and "окончательн" not in text


def test_competition_from_distinct_bidders(conn):
    lot(conn, "Old flatsale", lot_type="flatsale", published=NOW - 40 * DAY, place="grp")
    for i in range(8):
        iid = lot(conn, "Dragon adopt", published=NOW - (8 + i) * DAY, place="grp")
        conn.execute("UPDATE auctions SET extra = ? WHERE deviation_id = ?",
                     (json.dumps({"bidders": [{"user": "ann"}, {"user": f"bo{i % 2}"}]}), iid))
        lot(conn, "Cat adopt", published=NOW - (8 + i) * DAY, sold=False, place="grp")
    conn.commit()
    rep = forecast.niches(conn, now=NOW)
    dragon = next(d for d in rep.niches if d["key"] == "вид:dragon")
    assert dragon["bidders_recent"] == 3 and dragon["competition"] == pytest.approx(8 / 3)
    assert "лота на одного участника торгов (3 разных)" in forecast.render_niches(rep, now=NOW)
