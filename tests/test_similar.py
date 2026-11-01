"""Похожие продавцы: выбор по уровню и нише, только чтение, отчёт с метками доказательности."""

import json

import pytest

from mcp_devian import db, evidence, similar
from mcp_devian.db import Item

NOW = 1_793_534_400.0
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.sqlite3")
    yield c
    c.close()


def lot(conn, n: int, author: str, title: str, age: float = 10, own: bool = False, usd: float | None = 10.0,
        lot_type: str = "auction"):
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=title, url=f"u{n}", author=author,
                              lot_type=lot_type, status="open", price_usd=usd, own=own,
                              published_ts=NOW - age * DAY), NOW - age * DAY)


def watchers(conn, **by_author):
    for a, w in by_author.items():
        conn.execute("INSERT INTO author_stats (author, watchers, deviations, ts) VALUES (?, ?, 10, ?)", (a, w, NOW))
    conn.commit()


def market(conn):
    n = 1
    for i in range(3):
        lot(conn, n, "me", f"Dragon adopt auction #{i}", own=True)
        n += 1
    for author, count in (("peer_same", 4), ("peer_up", 3), ("bigshot", 5), ("few", 2)):
        for i in range(count):
            lot(conn, n, author, f"Dragon adoptable auction {i}")
            n += 1
    for i in range(5):  # не её ниша
        lot(conn, n, "catperson", f"Cat adopt auction {i}")
        n += 1
    watchers(conn, me=300, peer_same=200, peer_up=1200, bigshot=20000, few=100, catperson=300)


def test_peers_same_or_adjacent_tier_in_niche_never_her(conn):
    market(conn)
    got = similar.peers(conn, NOW, manual=["@Me", "Friend"])
    assert "me" not in got
    assert got[0] == "friend"                         # ручной список — первым
    assert got[1:] == ["peer_same", "peer_up"]        # тот же уровень раньше соседнего
    assert "bigshot" not in got and "few" not in got and "catperson" not in got


def test_peers_only_manual_when_her_tier_unknown(conn):
    market(conn)
    conn.execute("DELETE FROM author_stats WHERE author = 'me'")
    assert similar.peers(conn, NOW, manual=["x"]) == ["x"]


class ReadOnlyClient:
    def __init__(self, devs):
        self.devs = devs
        self.calls: list[str] = []

    def _request(self, method, path, params=None, **kw):
        assert method == "GET"
        self.calls.append(path)
        if path == "/deviation/metadata":
            ids = [v for k, v in params if k == "deviationids[]"]
            return {"metadata": [{"deviationid": u, "description": "" if u == "U101" else "SB: $12",
                                  "tags": [{"tag_name": "dragon"}]} for u in ids]}
        return {"results": self.devs, "has_more": True, "next_offset": 24}

    def gallery_all(self, *a, **k):
        self.calls.append("gallery_all")
        return []

    def post_comment(self, *a, **k):
        raise AssertionError("похожим художникам ничего не пишем")

    def watch(self, *a, **k):
        raise AssertionError("не вотчим")

    def fave(self, *a, **k):
        raise AssertionError("не фавим")


def dev(n: int, author: str, title: str, age: float = 1) -> dict:
    return {"deviationid": f"U{n}", "url": f"https://www.deviantart.com/{author}/art/x-{n}", "title": title,
            "author": {"username": author}, "published_time": str(int(NOW - age * DAY)),
            "stats": {"comments": 0, "favourites": 2}}


def test_step_one_gallery_call_read_only(conn):
    market(conn)
    client = ReadOnlyClient([dev(100, "peer_same", "Dragon adopt auction OPEN"), dev(101, "peer_same", "Sketch")])
    lines = similar.step(conn, client, NOW, manual=[], sleep=lambda s: None)
    gallery_calls = [c for c in client.calls if c in ("/gallery/all", "gallery_all")]
    assert len(gallery_calls) == 1
    assert set(client.calls) <= {"/gallery/all", "/deviation/metadata"}
    item = db.get_item(conn, "da:100")
    assert item is not None and json.loads(item["extra"])["similar"] == 1 and item["own"] == 0
    assert item["price_usd"] == 12
    assert db.get_item(conn, "da:101") is None  # не лот — не сохраняем
    assert lines == ["похожие художники: peer_same — новых лотов 1"]
    # следующий проход — следующий художник, а этот подождёт every_h
    client2 = ReadOnlyClient([])
    similar.step(conn, client2, NOW + 60, manual=[], sleep=lambda s: None)
    assert client2.calls == ["/gallery/all"]
    row = conn.execute("SELECT author FROM similar_state WHERE last_read_ts = ?", (NOW + 60,)).fetchone()
    assert row["author"] == "peer_up"


def test_step_keeps_known_price_from_description(conn):
    market(conn)
    db.upsert_item(conn, Item(id="da:200", source="deviantart", title="Dragon adopt auction", url="u",
                              author="peer_same", lot_type="auction", status="open", price_usd=40.0,
                              snippet="SB: $40", published_ts=NOW - DAY,
                              extra={"desc": "SB: $40", "desc_ts": NOW - DAY}), NOW - DAY)
    client = ReadOnlyClient([dev(200, "peer_same", "Dragon adopt auction [SOLD]")])
    similar.step(conn, client, NOW, manual=[], sleep=lambda s: None)
    row = db.get_item(conn, "da:200")
    assert row["price_usd"] == 40 and row["status"] == "closed"
    assert "/deviation/metadata" not in client.calls


def test_report_refuses_below_min_side_and_tags(conn):
    market(conn)
    text = similar.report(conn, NOW, days=30)
    assert "вывода нет" in text
    assert "(n=" in text and "рынок DA" in text
    assert "peer_same" in text and "она:" in text


def test_report_numbers_with_enough_data(conn):
    market(conn)
    for i in range(10):
        lot(conn, 300 + i, "peer_same", f"Dragon adopt auction {i}", age=20, usd=20.0 + i)
        conn.execute("INSERT INTO auctions (deviation_id, n_bids, ended, updated_ts, extra) VALUES (?, ?, 1, ?, '{}')",
                     (f"da:{300 + i}", 1 if i % 2 else 0, NOW - 2 * DAY))
    conn.commit()
    text = similar.report(conn, NOW, days=30)
    line = next(ln for ln in text.splitlines() if ln.startswith("• peer_same"))
    assert "медиана старта/цены" in line and "уверенность" in line
    assert "со ставкой 50%" in line


def test_report_without_peers_explains(conn):
    assert "Похожих продавцов пока нет" in similar.report(conn, NOW)


# --- регрессии ---

def test_excerpt_only_lot_gets_description_later(conn):
    """Метаданные однажды не пришли — в базе только отрывок без цены; следующий проход дочитывает."""
    market(conn)
    db.upsert_item(conn, Item(id="da:210", source="deviantart", title="Dragon adopt auction", url="u",
                              author="peer_same", lot_type="auction", status="open", price_usd=None,
                              snippet="look at this boy", published_ts=NOW - DAY,
                              extra={"desc": "look at this boy"}), NOW - DAY)
    client = ReadOnlyClient([dev(210, "peer_same", "Dragon adopt auction")])
    similar.step(conn, client, NOW, manual=[], sleep=lambda s: None)
    assert "/deviation/metadata" in client.calls
    assert db.get_item(conn, "da:210")["price_usd"] == 12


class FullPage(ReadOnlyClient):
    def __init__(self, devs, has_more=True):
        super().__init__(devs)
        self.has_more = has_more

    def _request(self, method, path, params=None, **kw):
        body = super()._request(method, path, params, **kw)
        if path == "/gallery/all":
            body["has_more"] = self.has_more
        return body


def test_rate_only_over_visible_span(conn):
    market(conn)
    devs = [dev(400 + i, "peer_same", f"Dragon adopt auction {i}", age=i / 6) for i in range(24)]  # 24 лота за 4 дня
    similar.step(conn, FullPage(devs), NOW, manual=[], sleep=lambda s: None)
    line = next(ln for ln in similar.report(conn, NOW, days=30).splitlines() if ln.startswith("• peer_same"))
    assert "темп: вывода нет (галерея видна за 4 дн из нужных 14)" in line
    assert "лота/нед" not in line
    # через 20 дней видно 24 дня галереи: темп считается за них, а не за 30
    later = NOW + 20 * DAY
    line = next(ln for ln in similar.report(conn, later, days=30).splitlines() if ln.startswith("• peer_same"))
    assert "лота/нед (за 24 дн)" in line


def test_rate_whole_gallery_when_no_more_pages(conn):
    market(conn)
    devs = [dev(500 + i, "peer_same", f"Dragon adopt auction {i}", age=2 * i) for i in range(10)]
    similar.step(conn, FullPage(devs, has_more=False), NOW, manual=[], sleep=lambda s: None)
    line = next(ln for ln in similar.report(conn, NOW, days=30).splitlines() if ln.startswith("• peer_same"))
    assert "лота/нед (за 30 дн)" in line


def test_comparison_refuses_below_min_side_never_claims_parity(conn):
    market(conn)
    line = next(ln for ln in similar.report(conn, NOW, days=30).splitlines() if ln.startswith("• peer_same"))
    assert "сравнение с ней: вывода нет" in line and "как у неё" not in line


def test_comparison_uses_family_threshold(conn, monkeypatch):
    market(conn)
    for i in range(10):
        lot(conn, 600 + i, "me", f"Dragon adopt auction mine {i}", age=20, own=True)
        lot(conn, 700 + i, "peer_same", f"Dragon adopt auction {i}", age=20)
        for n in (600 + i, 700 + i):
            conn.execute("INSERT INTO auctions (deviation_id, n_bids, ended, updated_ts, extra) VALUES (?, 1, 1, ?, '{}')",
                         (f"da:{n}", NOW - 2 * DAY))
    conn.commit()
    seen: list[float] = []
    real = evidence.compare

    def spy(k1, n1, k0, n0, threshold=evidence.COMPARE_P):
        seen.append(threshold)
        return real(k1, n1, k0, n0, threshold)

    monkeypatch.setattr(similar.evidence, "compare", spy)
    text = similar.report(conn, NOW, days=30, manual=["a", "b"])
    shown = len([ln for ln in text.splitlines() if ln.startswith("• ") and not ln.startswith("• она")])
    assert seen and set(seen) == {evidence.family_threshold(shown)} and shown > 1
    line = next(ln for ln in text.splitlines() if ln.startswith("• peer_same"))
    assert "разницы с ней по доле со ставкой не видно" in line


def test_report_refuses_rate_when_peer_coverage_low(conn):
    """Регрессия: доля «со ставкой» у похожих продавцов считалась только по лотам с известным итогом —
    без проверки покрытия (forecast.niches её делает); чужие лоты читаются реже, и сравнение с ней врало."""
    market(conn)
    for i in range(10):   # 10 закончившихся аукционов с известным итогом (все со ставкой)
        lot(conn, 300 + i, "peer_same", f"Dragon adopt auction {i}", age=20, usd=20.0 + i)
        conn.execute("INSERT INTO auctions (deviation_id, n_bids, ended, updated_ts, extra) VALUES (?, 1, 1, ?, '{}')",
                     (f"da:{300 + i}", NOW - 2 * DAY))
    for i in range(30):   # и 30 таких же, чьи ставки после окончания не дочитаны
        lot(conn, 400 + i, "peer_same", f"Dragon adopt auction {i}", age=20, usd=20.0)
    conn.commit()
    text = similar.report(conn, NOW, days=30)
    line = next(ln for ln in text.splitlines() if ln.startswith("• peer_same"))
    assert "итог известен у" in line and "нужно ≥70%" in line and "со ставкой 100%" not in line
    assert "ставка бывает" not in line
