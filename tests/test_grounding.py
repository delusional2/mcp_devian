"""Аналитика на синтетических данных с известной правдой (из аудита audit_sim.py).

Каждый тест — сценарий, где старый код выдавал неправду: завышенную долю ставок, «лучшее время» на шуме,
ложные «здесь покупают», смешанные валюты, советы по ещё идущим аукционам. Только фейки, без сети.
"""

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcp_devian import advisor, db, forecast, market, sales
from mcp_devian.config import Config
from mcp_devian.db import Item

DAY = 86400
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC).timestamp()
SRC = Path(__file__).parent.parent / "src" / "mcp_devian"


class Lots:
    def __init__(self, conn):
        self.conn, self.n = conn, 0

    def add(self, title, usd=10.0, author=None, own=False, published=NOW - 10 * DAY, tags=(), snippet="",
            n_bids=0, top=None, ended=0, updated=None, status="open", lot_type="auction", points=None, cur=None,
            ab_hit=0, with_auction=True, seen=None):
        self.n += 1
        iid = f"da:{self.n}"
        db.upsert_item(self.conn, Item(id=iid, source="deviantart", title=title,
                                       url=f"https://www.deviantart.com/x/art/a-{self.n}",
                                       author=author or f"s{self.n}", lot_type=lot_type, status=status,
                                       price_usd=usd if points is None else None, price_points=points, own=own,
                                       published_ts=published, snippet=snippet, extra={"tags": list(tags)}),
                       now=seen or published)
        if with_auction and lot_type in ("auction", "bta"):
            self.conn.execute(
                "INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, bidders, ended, ab_hit, updated_ts)"
                " VALUES (?,?,?,?,?,?,?,?)", (iid, top, cur or ("usd" if points is None else "points"), n_bids,
                                              min(n_bids, 3), ended, ab_hit,
                                              updated if updated is not None else published + 4 * DAY))
        self.conn.commit()
        return iid


@pytest.fixture
def fresh(tmp_path):
    made = []

    def make():
        c = db.connect(tmp_path / f"g{len(made)}.sqlite3")
        made.append(c)
        return c, Lots(c)
    yield make
    for c in made:
        c.close()


def _survivorship_db(fresh, seed, second_read: bool):
    conn, lots = fresh()
    rng = random.Random(seed)
    for i in range(80):
        pub = NOW - rng.uniform(9, 60) * DAY
        sold = rng.random() < 0.5
        # Со ставками — автор пишет «sold to @x» (ended=1); без ставок — никто ничего не пишет.
        lots.add(f"Dragon adopt {i}", usd=rng.choice([5, 10, 15, 20]), published=pub, n_bids=3 if sold else 0,
                 top=30 if sold else None, ended=1 if sold else 0,
                 updated=pub + (7.2 if second_read else 2.2) * DAY)
    return conn


def test_survivorship_estimate_is_unbiased(fresh):
    rates = []
    for seed in range(5):
        conn = _survivorship_db(fresh, seed, second_read=True)
        outs = forecast.load_outcomes(conn, NOW)
        assert len(outs) == 80
        rates.append(sum(o.sold for o in outs) / len(outs))
    assert abs(sum(rates) / len(rates) - 0.5) <= 0.1


def test_survivorship_single_early_read_refuses(fresh):
    conn = _survivorship_db(fresh, 0, second_read=False)
    known, sampled = forecast.outcome_coverage(conn, NOW)
    assert sampled == 80 and known < 0.7 * sampled
    rep = forecast.niches(conn, now=NOW)
    assert rep.refusal and "нужно ≥70%" in forecast.render_niches(rep)
    assert "нужно ≥70%" in forecast.render_price_plan(forecast.price_plan(conn, now=NOW))


def test_best_time_null_rarely_fires(fresh):
    fired = 0
    for t in range(100):
        rng = random.Random(1000 + t)
        conn, lots = fresh()
        for i in range(40):
            pub = NOW - rng.uniform(8, 59) * DAY
            nb = rng.choice([0, 0, 1, 2, 3, 5])
            lots.add(f"Cat adopt {i}", published=pub, n_bids=nb, top=20 if nb else None, ended=1, status="closed")
        fired += advisor.best_publish_time(conn, NOW) is not None
        conn.close()
    assert fired <= 10


def test_null_niches_rarely_called_sells(fresh):
    species = ["dragon", "cat", "wolf", "fox", "moth", "bird", "demon", "deer"]
    styles = ["chibi", "pastel", "gothic", "fantasy", ""]
    flagged = []
    for t in range(30):
        rng = random.Random(5000 + t)
        conn, lots = fresh()
        for _i in range(120):
            pub = NOW - rng.uniform(8, 170) * DAY
            sold = rng.random() < 0.4
            lots.add(f"{rng.choice(species)} {rng.choice(styles)} adopt", published=pub, n_bids=2 if sold else 0,
                     top=25 if sold else None, ended=1, status="closed", updated=pub + 4 * DAY)
        rep = forecast.niches(conn, now=NOW)
        assert not rep.refusal and rep.tested > 5
        flagged.append(sum(1 for d in rep.niches if d["verdict"] == "sells"))
        conn.close()
    assert sum(flagged) / len(flagged) <= 0.3


def _dragon_db(fresh, seed):
    conn, lots = fresh()
    rng = random.Random(seed)
    for i in range(60):
        pub = NOW - rng.uniform(8, 55) * DAY
        sb = rng.choice([5, 5, 10, 10, 15, 25, 40])
        p = {5: 0.8, 10: 0.7, 15: 0.55, 25: 0.35, 40: 0.2}[sb]
        sold = rng.random() < p
        lots.add(f"Dragon fantasy adopt {i}", usd=sb, published=pub, tags=["dragon"], n_bids=3 if sold else 0,
                 top=sb * rng.choice([1.5, 2, 3]) if sold else None, ended=1, status="closed", updated=pub + 4 * DAY)
    return conn


def test_thin_start_prices_are_never_best(fresh):
    for seed in (3, 4, 5, 6, 7):
        conn = _dragon_db(fresh, seed)
        plan = forecast.price_plan(conn, species=("dragon",), unit="usd", now=NOW)
        assert plan.best is not None, plan.caveats
        assert plan.best["start"] not in (25, 40) and plan.best["n"] >= forecast.MIN_START_LOTS
        text = forecast.render_price_plan(plan, now=NOW)
        assert "по модели" in text and "набирают" not in text
        assert plan.confidence != "высокая" or plan.best["n"] >= 40


def test_mixed_currency_autobuy(fresh):
    conn, lots = fresh()
    for title, usd, pts, top, pub in (("Moth A", 10, None, 15, 20), ("Moth B", 10, None, 14, 18),
                                      ("Moth C", None, 500, 900, 16), ("Moth D", None, 500, 800, 15)):
        snippet = "SB: $10 AB: $60" if usd else "SB: 500 pts AB: 6000 pts"
        lots.add(title, usd=usd or 0, points=pts, own=True, snippet=snippet, n_bids=2, top=top, ended=1,
                 status="closed", published=NOW - pub * DAY)
    for a in advisor._rule_autobuy(conn, NOW):
        assert "$3,030" not in a.detail and "$407.50" not in a.detail and "$735" not in a.detail
    # 2 лота в каждой валюте — меньше 3, правило молчит
    assert advisor._rule_autobuy(conn, NOW) == []


def test_sold_fixed_price_not_stale(fresh):
    conn, lots = fresh()
    iid = lots.add("Fox flatsale $20", usd=20, own=True, lot_type="flatsale", status="open",
                   published=NOW - 20 * DAY, seen=NOW - DAY)
    sales.add_sale(conn, title="Fox", amount=20, currency="usd", platform="deviantart", format="flatsale",
                   item_id=iid, status="done", now=NOW - 15 * DAY)
    assert not [a for a in advisor._rule_stale(conn, Config(), NOW) if a.title.startswith("Не продаются")]


def test_running_auctions_not_closed_without_bids(fresh):
    conn, lots = fresh()
    for i in range(3):
        lots.add(f"Cat auction {i} [OPEN]", usd=15, own=True, published=NOW - 4 * DAY, status="open",
                 n_bids=0, updated=NOW, seen=NOW)
    for a in advisor.advice(conn, Config(), NOW):
        assert "закрылись без ставок" not in a.detail and a.kind != "pricing"


def test_no_seasonal_penalty_without_data(fresh):
    out = {}
    for label, now in (("sep20", datetime(2026, 9, 20, tzinfo=UTC).timestamp()), ("nov1", NOW)):
        conn, lots = fresh()
        rng = random.Random(7)
        for i in range(40):
            pub = now - rng.uniform(8, 40) * DAY
            sold = rng.random() < 0.5
            sp = "demon" if i % 4 == 0 else "cat"
            lots.add(f"{sp} adopt {i}", published=pub, n_bids=2 if sold else 0, top=20 if sold else None, ended=1,
                     status="closed", updated=pub + 4 * DAY)
        plan = forecast.price_plan(conn, species=("demon",), unit="usd", now=now)
        out[label] = (round(plan.p_niche, 6), plan.season)
    assert out["sep20"] == out["nov1"]


def test_weak_calibration_caps_confidence(fresh, monkeypatch):
    conn = _dragon_db(fresh, 3)
    monkeypatch.setattr(forecast, "calibration", lambda *a, **k: {"n": 200, "skill": -0.05})
    plan = forecast.price_plan(conn, species=("dragon",), unit="usd", now=NOW)
    assert plan.best and plan.confidence in ("низкая", "нет вывода")
    assert any("не точнее простого среднего" in c for c in plan.caveats)
    monkeypatch.setattr(forecast, "calibration", lambda *a, **k: {"n": 12, "skill": 0.3})
    plan = forecast.price_plan(conn, species=("dragon",), unit="usd", now=NOW)
    assert plan.confidence in ("низкая", "нет вывода")


def test_no_overconfident_instructions_anywhere(fresh):
    conn = _dragon_db(fresh, 3)
    outputs = [forecast.render_price_plan(forecast.price_plan(conn, species=("dragon",), now=NOW), now=NOW),
               forecast.render_niches(forecast.niches(conn, now=NOW), now=NOW),
               forecast.render_calibration(forecast.calibration(conn, NOW)),
               advisor.launch_plan(conn, Config(), species="dragon", fmt="auction", now=NOW),
               advisor.render_advice(advisor.advice(conn, Config(), NOW), 15),
               advisor.weekly_report(conn, Config(), NOW),
               market.render(market.forecast(conn, ["dragon"], "auction", now=NOW), "auction")]
    for text in outputs:
        low = text.lower()
        assert "окончательн" not in low and "не пересматривай" not in low
        assert "покупают" not in low and "находят покупателя" not in low
    for name in ("forecast.py", "advisor.py", "market.py", "evidence.py", "listing.py", "sales.py", "digest.py"):
        src = (SRC / name).read_text(encoding="utf-8").lower()
        assert "не пересматривай" not in src and "окончательный" not in src
        assert "в разы больше" not in src and "15–25 тегов" not in src and "поднимает клики" not in src


# --- Находки проверки ---

def test_open_title_lots_get_an_outcome_after_two_weeks(fresh):
    """Непроданные лоты в тихих папках весь срок видны в ленте с [OPEN]: без правила 14 дней их итог
    оставался неизвестным, и доля «со ставкой» выходила завышенной (0.57 вместо 0.50)."""
    conn, lots = fresh()
    rng = random.Random(11)
    for i in range(200):
        pub = NOW - rng.uniform(20, 60) * DAY
        sold = i % 2 == 0
        # Проданные закрыты автором; непроданные висят [OPEN] и всё время видны в ленте.
        lots.add(f"Dragon adopt {i}", published=pub, n_bids=3 if sold else 0, top=30 if sold else None,
                 ended=1 if sold else 0, status="closed" if sold else "open", updated=pub + 15 * DAY,
                 seen=NOW - DAY / 2 if not sold else pub)
    outs = forecast.load_outcomes(conn, NOW)
    assert len(outs) == 200
    assert abs(sum(o.sold for o in outs) / len(outs) - 0.5) <= 0.02


def test_launch_plan_says_whole_market_when_niche_is_thin(fresh):
    conn, lots = fresh()
    rng = random.Random(2)
    for i in range(3):
        lots.add(f"Dragon adopt {i}", usd=15, published=NOW - (10 + i) * DAY, tags=["dragon"], n_bids=2, top=30,
                 ended=1, status="closed", updated=NOW - i * DAY)
    for i in range(60):
        pub = NOW - rng.uniform(8, 55) * DAY
        sold = rng.random() < 0.5
        lots.add(f"Cat adopt {i}", usd=rng.choice([10, 15, 20]), published=pub, tags=["cat"], n_bids=3 if sold else 0,
                 top=40 if sold else None, ended=1, status="closed", updated=pub + 4 * DAY)
    text = advisor.launch_plan(conn, Config(), species="dragon", fmt="auction", now=NOW)
    assert "Цена: SB" in text
    assert "похожих аукционов" not in text and "аукционов рынка в целом" in text
    assert "старт по рынку в целом" in text
    plan = forecast.price_plan(conn, species=["dragon"], unit="usd", now=NOW)
    assert not plan.niche_pool


def test_best_time_noise_with_her_lots_and_market(fresh):
    """Её окна и запасной проход по рынку делят один порог: на шуме «лучшее время» — редкость."""
    fired = 0
    for t in range(60):
        rng = random.Random(7000 + t)
        conn, lots = fresh()
        for i in range(200):
            pub = NOW - rng.uniform(8, 59) * DAY
            nb = rng.choice([0, 0, 1, 2, 3, 5])
            lots.add(f"Cat adopt {i}", published=pub, n_bids=nb, top=20 if nb else None, ended=1, status="closed",
                     own=i < 60)
        fired += advisor.best_publish_time(conn, NOW) is not None
        conn.close()
    assert fired <= 7
