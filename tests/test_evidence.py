from datetime import UTC, datetime

import pytest

from mcp_devian import db, evidence, sales
from mcp_devian.db import Item
from mcp_devian.evidence import Evidence

DAY = 86400
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC).timestamp()
SEP1 = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
OCT31 = datetime(2026, 10, 31, tzinfo=UTC).timestamp()


def ev(n=12, n_eff=None, interval=None, measure="rate", value=None, source="market_da", kind="estimate",
       as_of=OCT31):
    return Evidence(n=n, n_eff=n_eff, since_ts=SEP1, until_ts=OCT31, data_as_of=as_of, source=source,
                    population="аукционы DA, $, все продавцы, без её лотов", unit="usd", kind=kind,
                    interval=interval, measure=measure, value=value)


def test_wilson_bounds():
    assert evidence.wilson(0, 0) == (0.0, 1.0)
    lo, hi = evidence.wilson(5, 10)
    assert 0 < lo < 0.5 < hi < 1 and hi - lo == pytest.approx(0.38, abs=0.02)
    lo, hi = evidence.wilson(10, 10)
    assert hi == 1.0 and 0.8 < lo < 1
    lo, hi = evidence.wilson(0, 12)
    assert lo == 0.0 and hi < 0.15
    # шире выборка — уже интервал
    assert (lambda a, b: (a[1] - a[0]) > (b[1] - b[0]))(evidence.wilson(10, 20), evidence.wilson(100, 200))


def test_median_ci_deterministic():
    vals = [5, 8, 10, 10, 12, 15, 20, 40, 9, 11]
    a = evidence.median_ci(vals)
    assert a == evidence.median_ci(vals) == evidence.median_ci(vals, seed=0)
    assert a[0] <= 10.5 <= a[1]
    assert evidence.median_ci([7]) == (7, 7)
    with pytest.raises(ValueError):
        evidence.median_ci([])


def test_p_greater():
    assert evidence.p_greater(5, 10, 5, 10) == pytest.approx(0.5, abs=0.05)
    assert evidence.p_greater(16, 20, 8, 20) > 0.9
    assert evidence.p_greater(8, 20, 16, 20) < 0.1
    assert evidence.p_greater(16, 20, 8, 20) == evidence.p_greater(16, 20, 8, 20)   # зерно фиксировано


def test_compare_needs_eight_each_side():
    assert evidence.compare(7, 7, 0, 20) is None
    assert evidence.compare(8, 8, 0, 20) == "better"
    assert evidence.compare(0, 20, 8, 8) == "worse"
    assert evidence.compare(5, 10, 5, 10) is None


def test_family_threshold():
    assert evidence.family_threshold(1) == 0.9
    assert evidence.family_threshold(10) == pytest.approx(0.99)


def test_level_thresholds_and_downgrade():
    assert evidence.level(ev(n=7)) == "нет вывода"
    assert evidence.level(ev(n=8)) == "низкая"
    assert evidence.level(ev(n=15)) == "средняя"
    assert evidence.level(ev(n=40)) == "высокая"
    assert evidence.level(ev(n=100, n_eff=10)) == "низкая"                      # эффективное n важнее
    assert evidence.level(ev(n=40, interval=(0.3, 0.65))) == "средняя"          # доля: шире 0.3
    assert evidence.level(ev(n=40, interval=(0.3, 0.55))) == "высокая"
    assert evidence.level(ev(n=40, interval=(5, 12), measure="price", value=10)) == "средняя"  # шире 50% медианы
    assert evidence.level(ev(n=40, interval=(8, 12), measure="price", value=10)) == "высокая"
    assert evidence.level(ev(n=10, interval=(0.2, 0.7))) == "нет вывода"
    assert evidence.cap("высокая", "низкая") == "низкая" and evidence.cap("нет вывода", "низкая") == "нет вывода"


def test_tag_refuse_stale_heuristic():
    assert evidence.tag(ev()) == "(n=12, 01.09–31.10.2026, рынок DA, оценка, уверенность низкая)"
    assert evidence.tag(ev(n=30, n_eff=11.6)).startswith("(n=30, эфф. 12, ")
    assert "уверенность" not in evidence.tag(ev(kind="heuristic", source="rule_of_thumb"))
    assert evidence.refuse("старт", 5, 8) == "Вывода нет: старт — 5 из нужных 8."
    assert evidence.stale(ev(as_of=NOW - 10 * DAY), NOW) == "данные старые: по 22.10"
    assert evidence.stale(ev(as_of=NOW - DAY), NOW) is None
    assert evidence.heuristic("AB 3–5× SB", "гайд") == "общая практика, не из твоих данных: AB 3–5× SB (гайд)"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "e.sqlite3")
    yield c
    c.close()


def test_render_health_empty(conn):
    text = evidence.render_health(conn, NOW)
    assert "лента DeviantArt: ни разу" in text and "Toyhouse: ни разу" in text
    assert "итог известен у 0" in text and "Её продаж в базе пока нет" in text
    assert "найдены только поиском: 0" in text


def test_render_health_populated(conn):
    db.record_run(conn, "deviantart", None, NOW - 3600)
    db.record_run(conn, "toyhouse", None, NOW - 5 * DAY)
    for i in range(10):
        iid = f"da:{i}"
        db.upsert_item(conn, Item(id=iid, source="deviantart", title="Cat auction", url="u", lot_type="auction",
                                  status="open", price_usd=10, author=f"s{i}", published_ts=NOW - 20 * DAY,
                                  place="поиск DA: cat" if i == 9 else "grp"), now=NOW - 20 * DAY)
        conn.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, ended, updated_ts, extra) "
                     "VALUES (?, NULL, 'usd', 0, 0, ?, ?)",
                     (iid, NOW - 10 * DAY, '{"read_age_days": 10}' if i < 6 else '{"read_age_days": 2}'))
    conn.commit()
    sales.add_sale(conn, title="A", amount=30, currency="usd", platform="deviantart", format="auction", now=NOW)
    sales.add_sale(conn, title="B", amount=5, currency=None, platform="deviantart", format="auction", now=NOW)
    sales.add_sale(conn, title="C", amount=None, currency="usd", platform="deviantart", format="auction", now=NOW)
    text = evidence.render_health(conn, NOW)
    # Это не доход: честно сказано, что сюда входят и неоплаченные, а продажа без суммы не считается «$».
    assert "включая неоплаченные, за всё время; доход — sales:income" in text and "без суммы: 1" in text
    assert "лента DeviantArt: 1 ч назад" in text
    assert "Toyhouse: 5 дн назад (27.10) — данные старые" in text
    assert "чьи ставки читали: 9; итог известен у 6 (67%) — для прогнозов нужно ≥70%" in text
    assert "Дочитаны в возрасте ≥7 дн: 6 из 9 (67%)" in text
    assert "$30 (1)" in text and "без валюты: 1" in text
    assert "Лоты рынка: 10, из них найдены только поиском: 1" in text


@pytest.mark.parametrize("source, label", [
    ("palette", "палитры лотов"), ("experiment", "её эксперимент"), ("market_th_stated", "заявленные цены TH"),
    ("x_manual", "X — ссылки, которые она принесла"), ("crm", "её покупатели и участники")])
def test_new_source_labels(source, label):
    assert evidence.SOURCE_LABEL[source] == label
    assert f", {label}, оценка, " in evidence.tag(ev(source=source))


def test_unknown_source_falls_back_to_raw_key():
    assert evidence.tag(ev(source="brand_new_source")) == \
        "(n=12, 01.09–31.10.2026, brand_new_source, оценка, уверенность низкая)"
