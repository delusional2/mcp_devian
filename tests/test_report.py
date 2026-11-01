import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from mcp_devian import db, report, sales
from mcp_devian.db import Item
from mcp_devian.report import ReportError

TZ = "Europe/Moscow"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo(TZ)).timestamp()
OCT = datetime(2026, 10, 10, 15, 0, tzinfo=ZoneInfo(TZ)).timestamp()
DAY = 86400
NO_NICHES = SimpleNamespace(refusal="x", niches=[])


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "r.sqlite3")
    yield c
    c.close()


def _sale(conn, title, amount, currency, *, fmt="auction", platform="deviantart", net=None, ts=OCT, status="paid",
          buyer="buyer1"):
    sid = sales.add_sale(conn, title=title, amount=amount, currency=currency, platform=platform, format=fmt,
                         buyer=buyer, status=status, now=ts, net_amount=net,
                         pay_method="boosty" if net is not None else None)
    return sid


def _lot(conn, n, *, published=OCT, views=100, favs=10, bids=2):
    iid = f"da:{n}"
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=f"Moth {n}", url=f"https://www.deviantart.com/me/art/m-{n}",
                              lot_type="auction", status="closed", price_usd=10, own=True, published_ts=published,
                              author="me"), now=published)
    conn.execute("INSERT INTO deviation_stats (deviation_id, ts, views, favourites, comments) VALUES (?, ?, ?, ?, 0)",
                 (iid, published + 5 * DAY, views, favs))
    conn.execute("INSERT INTO auctions (deviation_id, top_amount, currency, n_bids, bidders, ended, updated_ts) "
                 "VALUES (?, ?, 'usd', ?, ?, 1, ?)", (iid, 20 if bids else None, bids, 1 if bids else 0, published))
    conn.commit()
    return iid


def _build(conn, tmp_path, **kw):
    kw.setdefault("niche_report", SimpleNamespace(refusal="Вывода нет: мало аукционов — 3 из нужных 8.", niches=[]))
    kw.setdefault("use_earnings", False)
    return report.build(conn, "2026-10", out_dir=tmp_path, tz=TZ, now=NOW, **kw)


# --- месяц ---

def test_month_bounds_in_her_timezone():
    since, until, label = report.month_bounds("2026-10", TZ, NOW)
    assert label == "2026-10"
    assert datetime.fromtimestamp(since, ZoneInfo(TZ)) == datetime(2026, 10, 1, tzinfo=ZoneInfo(TZ))
    assert datetime.fromtimestamp(until, ZoneInfo(TZ)) == datetime(2026, 11, 1, tzinfo=ZoneInfo(TZ))
    assert report.month_bounds("10.2026", TZ, NOW)[2] == "2026-10"
    assert report.month_bounds(None, TZ, NOW)[2] == "2026-11"
    for bad in ("2026-13", "октябрь", "2027-01", "1999-12"):
        with pytest.raises(ReportError):
            report.month_bounds(bad, TZ, NOW)


# --- файл ---

def test_report_file_self_contained_and_named_by_month(conn, tmp_path):
    _sale(conn, "Moth 1", 15, "usd", net=1200)
    res = _build(conn, tmp_path)
    path = tmp_path / "mcp-devian-report-2026-10.html"
    assert res["path"] == str(path) and path.exists()
    html = path.read_text("utf-8")
    assert html.startswith("<!doctype html>")
    assert "<script" not in html and "<link" not in html and "@import" not in html
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "prefers-color-scheme: dark" in html and 'name="viewport"' in html
    assert "<svg" in html
    assert "октябрь 2026" in html
    assert "Отчёт за октябрь 2026" in res["summary"] and "mcp-devian-report-2026-10.html" in res["summary"]


def test_sections_and_every_chart_has_n_and_period(conn, tmp_path):
    _sale(conn, "Moth 1", 15, "usd", net=1200)
    _sale(conn, "Moth 2", 20, "usd", net=1600, fmt="flatsale", platform="toyhouse")
    _lot(conn, 1)
    html = Path(_build(conn, tmp_path)["path"]).read_text("utf-8")
    for section in ('id="income"', 'id="formats"', 'id="hourly"', 'id="niches"', 'id="funnel"', 'id="goal"'):
        assert section in html
    captions = re.findall(r'<p class="cap">(.*?)</p>', html, re.S)
    assert captions
    for cap in captions:
        assert "n=" in cap and re.search(r"\d\d\.\d\d", cap), cap


def test_income_net_rub_and_currencies_not_mixed(conn, tmp_path):
    _sale(conn, "Moth 1", 15, "usd", net=1200)
    _sale(conn, "Moth 2", 20, "usd", net=1600)
    _sale(conn, "Moth 3", 500, "points")
    _sale(conn, "Old", 99, "usd", net=9000, ts=OCT - 40 * DAY)         # сентябрь — не в отчёте
    _sale(conn, "Unpaid", 50, "usd", status="won")                       # не оплачено — не доход
    data = report.collect(conn, "2026-10", tz=TZ, now=NOW, use_earnings=False, niche_report=NO_NICHES)
    assert data["net"]["rub"]["total"] == 2800 and data["net"]["rub"]["count"] == 2
    assert data["gross"]["usd"]["total"] == 35
    assert data["gross"]["points"]["total"] == 500
    html = report.render_html(data)
    assert "2,800 ₽" in html and "$35" in html and "500 pts" in html
    assert "не выводятся" in html                 # поинты — не деньги
    assert "по курсу не пересчитываю" in html


def test_buyers_never_in_report(conn, tmp_path):
    _sale(conn, "Moth 1", 15, "usd", net=1200, buyer="secret_buyer_42")
    html = Path(_build(conn, tmp_path)["path"]).read_text("utf-8")
    assert "secret_buyer_42" not in html


def test_html_injection_escaped_everywhere(conn, tmp_path):
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    _sale(conn, evil, 15, "usd", net=1200)
    niche = SimpleNamespace(refusal=None, n_lots=40, niches=[
        {"key": evil, "lots": 12, "sold": 6, "p_sold": 0.5, "confidence": "средняя", "verdict": "sells",
         "ev": None, "interval": (0.3, 0.7)}])
    hourly = [{"label": evil, "hours": 20, "net_rub": 12000, "n": 4}]
    goal = {"target": 30000, "currency": "rub", "plan": evil}
    html = Path(_build(conn, tmp_path, niche_report=niche, hourly=hourly, goal=goal)["path"]).read_text("utf-8")
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html


def test_goal_progress(conn, tmp_path):
    _sale(conn, "Moth 1", 100, "usd", net=12000)
    data = report.collect(conn, "2026-10", tz=TZ, now=NOW, use_earnings=False, goal={"target": 30000, "currency": "rub"},
                          niche_report=SimpleNamespace(refusal="x", niches=[]))
    assert data["goal"]["done"] == 12000 and data["goal"]["pct"] == 40
    html = report.render_html(data)
    assert "40%" in html and "30,000 ₽" in html and "12,000 ₽" in html


def test_no_goal_says_so(conn, tmp_path):
    html = Path(_build(conn, tmp_path)["path"]).read_text("utf-8")
    assert "Цель на месяц не задана" in html


def test_hourly_rates_and_refusal_below_threshold(conn, tmp_path):
    hourly = [{"label": "аукционы драконов", "hours": 20, "net_rub": 12000, "n": 10},
              {"label": "наборы по 3", "hours": 1.5, "net_rub": 3000, "n": 1},
              {"label": "рафлы", "hours": 20, "net_rub": 9000, "n": 4}]
    data = report.collect(conn, "2026-10", tz=TZ, now=NOW, use_earnings=False, hourly=hourly,
                          niche_report=SimpleNamespace(refusal="x", niches=[]))
    rows = {r["label"]: r for r in data["hourly"]}
    assert rows["аукционы драконов"]["rate"] == 600 and rows["аукционы драконов"]["level"] == "низкая"
    assert rows["наборы по 3"]["rate"] is None            # 1 работа, 1.5 ч — вывода нет
    assert rows["рафлы"]["rate"] is None                   # 4 работы < evidence.MIN_SIDE (8) — тоже нет
    html = report.render_html(data)
    assert "600 ₽/ч · уверенность низкая" in html and "Вывода нет" in html
    assert "4 работы, 20 ч из нужных 8 работ и 3 ч" in html
    assert "ориентир, не вывод" in html


def test_hourly_threshold_is_project_rule():
    assert report.MIN_HOURLY_N == report.evidence.MIN_SIDE == 8
    rows = report._hourly([{"label": "a", "hours": 30, "net_rub": 30000, "n": 7},
                           {"label": "b", "hours": 30, "net_rub": 30000, "n": 8},
                           {"label": "c", "hours": 30, "net_rub": 30000, "n": 40}])
    got = {r["label"]: (r["rate"], r["level"]) for r in rows}
    assert got == {"a": (None, "нет вывода"), "b": (1000, "низкая"), "c": (1000, "высокая")}


def test_hourly_refusal_from_earnings_quotes_earnings_threshold(conn):
    hourly = [{"label": "аукционы", "rate": 900.0, "level": "нет вывода", "hours": 15.0, "n": 5, "net_rub": 13500}]
    data = report.collect(conn, "2026-10", tz=TZ, now=NOW, use_earnings=False, hourly=hourly, niche_report=NO_NICHES)
    html = report.render_html(data)
    assert "Вывода нет: ₽ в час для «аукционы» — 5 работ из нужных 8 работ." in html
    assert "3 адопта" not in html and "3 ч" not in html


def test_goal_plan_keeps_earnings_confidence(conn):
    goal = {"target": 30000, "currency": "rub", "done": 12000,
            "plan": report._need_text([{"format": "auction", "count": 4, "level": "низкая", "n": 9},
                                       {"format": "ych", "count": 2, "level": "нет вывода", "n": 3}])}
    data = report.collect(conn, "2026-10", tz=TZ, now=NOW, use_earnings=False, goal=goal, niche_report=NO_NICHES)
    html = report.render_html(data)
    assert "ещё ~4 × аукционы (уверенность низкая, n=9)" in html
    assert "YCH" not in data["goal"]["plan"]


def test_hourly_missing_explains(conn, tmp_path):
    html = Path(_build(conn, tmp_path)["path"]).read_text("utf-8")
    assert "часы" in html.lower() and "не отмечены" in html


def test_funnel_from_her_lots_in_month(conn, tmp_path):
    _lot(conn, 1, views=200, favs=20, bids=3)
    _lot(conn, 2, views=100, favs=5, bids=0)
    _lot(conn, 3, published=OCT - 40 * DAY)                    # сентябрьский лот — не в воронке месяца
    sid = _sale(conn, "Moth 1", 15, "usd", net=1200)
    conn.execute("UPDATE sales SET item_id = 'da:1' WHERE id = ?", (sid,))
    conn.commit()
    data = report.collect(conn, "2026-10", tz=TZ, now=NOW, use_earnings=False, niche_report=NO_NICHES)
    f = data["funnel"]
    assert (f["lots"], f["views"], f["favs"], f["with_bids"], f["sold"]) == (2, 300, 25, 1, 1)


def test_niches_from_report_and_refusal(conn, tmp_path):
    niche = SimpleNamespace(refusal=None, n_lots=60, niches=[
        {"key": "вид:dragon", "lots": 20, "sold": 14, "p_sold": 0.7, "confidence": "средняя", "verdict": "sells",
         "ev": None, "interval": (0.55, 0.82)}])
    html = Path(_build(conn, tmp_path, niche_report=niche)["path"]).read_text("utf-8")
    assert "dragon" in html and "70%" in html and "n=60" in html and "14 из 20" in html
    html2 = Path(_build(conn, tmp_path)["path"]).read_text("utf-8")
    assert "Вывода нет" in html2


def test_formats_comparison_refused_when_few_sales(conn, tmp_path):
    _sale(conn, "A", 15, "usd", net=1200)
    _sale(conn, "B", 10, "usd", net=800, fmt="flatsale")
    html = Path(_build(conn, tmp_path)["path"]).read_text("utf-8")
    assert "Вывода нет" in html and "сравнивать форматы" in html.lower()


def test_empty_month_still_builds(conn, tmp_path):
    html = Path(_build(conn, tmp_path)["path"]).read_text("utf-8")
    assert "оплаченных продаж нет" in html.lower()


def test_regenerating_overwrites_own_report_but_not_foreign_file(conn, tmp_path):
    first = _build(conn, tmp_path)["path"]
    again = _build(conn, tmp_path)["path"]
    assert first == again
    foreign = tmp_path / "mcp-devian-report-2026-09.html"
    foreign.write_text("<html>her own file</html>", "utf-8")
    res = report.build(conn, "2026-09", out_dir=tmp_path, tz=TZ, now=NOW, use_earnings=False,
                       niche_report=SimpleNamespace(refusal="x", niches=[]))
    assert res["path"].endswith("mcp-devian-report-2026-09-2.html")
    assert foreign.read_text("utf-8") == "<html>her own file</html>"


def test_default_dir_is_downloads(monkeypatch, tmp_path, conn):
    monkeypatch.setattr(report.Path, "home", classmethod(lambda cls: tmp_path))
    res = report.build(conn, "2026-10", tz=TZ, now=NOW, use_earnings=False,
                       niche_report=SimpleNamespace(refusal="x", niches=[]))
    assert res["path"] == str(tmp_path / "Downloads" / "mcp-devian-report-2026-10.html")


def test_plural_russian():
    words = ("продажа", "продажи", "продаж")
    assert [report.plural(n, *words) for n in (1, 2, 5, 11, 12, 21, 22, 25, 111)] == [
        "продажа", "продажи", "продаж", "продаж", "продаж", "продажа", "продажи", "продаж", "продаж"]


def _fake_earnings(goal_target=30000):
    def hourly(conn, now):
        return SimpleNamespace(formats=[
            SimpleNamespace(key="bundle", rate=1100.0, level="средняя", hours=30.0, n=10, net=33000.0),
            SimpleNamespace(key="auction", rate=600.0, level="средняя", hours=80.0, n=10, net=48000.0),
            SimpleNamespace(key="ych", rate=500.0, level="нет вывода", hours=12.0, n=3, net=6000.0)])

    def goal_status(conn, now, tz):
        return {"target": goal_target, "got": 12000.0, "remaining": 18000.0,
                "need": [{"format": "auction", "count": 3}, {"format": "flatsale", "count": 2}]}

    return SimpleNamespace(hourly=hourly, goal_status=goal_status)


def test_takes_hours_and_goal_from_earnings_when_present(conn, tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "mcp_devian.earnings", _fake_earnings())
    data = report.collect(conn, "2026-11", tz=TZ, now=NOW, niche_report=SimpleNamespace(refusal="x", niches=[]))
    rates = {r["label"]: r["rate"] for r in data["hourly"]}
    assert rates == {"наборы": 1100, "аукционы": 600, "YCH": None}    # «нет вывода» учёта часов уважаем
    assert data["goal"]["target"] == 30000 and data["goal"]["done"] == 12000 and data["goal"]["pct"] == 40
    html = report.render_html(data)
    assert "1,100 ₽/ч" in html and "ещё ~3 × аукционы" in html and "все отмеченные часы" in html


def test_real_earnings_module_shape(conn):
    earnings = pytest.importorskip("mcp_devian.earnings")
    for i in range(10):
        sid = _sale(conn, f"Moth {i}", 15, "usd", net=3000, ts=NOW - (5 + i) * DAY)
        earnings.log_hours(conn, sid, 3, now=NOW - (5 + i) * DAY + 3600)
    data = report.collect(conn, "2026-10", tz=TZ, now=NOW, niche_report=NO_NICHES)
    rows = [r for r in data["hourly"] if r["rate"] is not None]
    assert rows and rows[0]["rate"] == 1000 and rows[0]["n"] == 10
    assert "дн по 01.11.2026" in data["hourly_period"]


def test_past_month_goal_from_earn_goals_table(conn, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "mcp_devian.earnings", _fake_earnings())
    conn.execute("CREATE TABLE IF NOT EXISTS earn_goals (month TEXT PRIMARY KEY, target REAL)")
    conn.execute("INSERT INTO earn_goals VALUES ('2026-10', 25000)")
    _sale(conn, "Moth", 15, "usd", net=5000)
    data = report.collect(conn, "2026-10", tz=TZ, now=NOW, niche_report=SimpleNamespace(refusal="x", niches=[]))
    assert data["goal"]["target"] == 25000 and data["goal"]["done"] == 5000 and data["goal"]["pct"] == 20


def test_broken_earnings_does_not_break_report(conn, tmp_path, monkeypatch):
    import sys

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "mcp_devian.earnings", SimpleNamespace(hourly=boom, goal_status=boom))
    res = report.build(conn, "2026-10", out_dir=tmp_path, tz=TZ, now=NOW,
                       niche_report=SimpleNamespace(refusal="x", niches=[]))
    assert Path(res["path"]).exists()
