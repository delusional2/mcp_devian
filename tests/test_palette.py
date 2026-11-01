import io

import pytest
from PIL import Image

from mcp_devian import db, palette
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400
THUMB = "https://images-wixmp-abc.wixmp.com/f/x/{n}.jpg?token=t"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p.sqlite3")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(palette, "DELAY_S", 0)


def solid(color, size=(80, 80), mode="RGB"):
    return Image.new(mode, size, color)


def png(color) -> bytes:
    buf = io.BytesIO()
    solid(color).save(buf, "PNG")
    return buf.getvalue()


def lot(conn, n, title="Moth adopt", own=False, published=NOW - 20 * DAY, thumb=True, sold=None, author=None,
        place="AdoptGroup"):
    extra = {"thumb": THUMB.format(n=n)} if thumb is True else ({"thumb": thumb} if thumb else {})
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=title, url=f"https://www.deviantart.com/a/art/x-{n}",
                              lot_type="auction", status="closed", price_usd=10, own=own, published_ts=published,
                              author=author or ("me" if own else f"seller{n}"), place=place, extra=extra),
                   now=published)
    if sold is not None:
        conn.execute("INSERT OR REPLACE INTO auctions (deviation_id, top_amount, currency, n_bids, ended, updated_ts) "
                     "VALUES (?, ?, 'usd', ?, 1, ?)", (f"da:{n}", 20 if sold else None, 2 if sold else 0, NOW - DAY))
    conn.commit()
    return f"da:{n}"


# --- Цвета и семейства ---

def test_families_of_synthetic_images():
    assert palette.family(palette.dominant_colors(solid((200, 30, 30)))) == "red"
    assert palette.family(palette.dominant_colors(solid((255, 0, 0)))) == "neon red"
    split = solid((255, 255, 255), (100, 100))
    split.paste((30, 60, 200), (0, 0, 50, 100))
    cols = palette.dominant_colors(split)
    assert [c for c, _ in cols] == [(30, 60, 200), (255, 255, 255)] and [s for _, s in cols] == [0.5, 0.5]
    assert palette.family(cols) == "blue"            # белый фон не перебивает персонажа
    assert palette.family(palette.dominant_colors(solid((248, 187, 208)))) == "pastel pink"
    assert palette.label("pastel pink") == "пастельные розовые"
    assert palette.family(palette.dominant_colors(solid((250, 250, 250)))) == "light"
    assert palette.family(palette.dominant_colors(solid((120, 70, 30)))) == "brown"
    assert palette.family(palette.dominant_colors(solid((140, 150, 120)))) == "muted green"
    assert palette.family([]) == "unknown"


def test_transparent_pixels_ignored():
    img = solid((0, 0, 0, 0), (100, 100), "RGBA")
    img.paste((248, 187, 208, 255), (20, 20, 80, 80))
    assert palette.dominant_colors(img) == [((248, 187, 208), 1.0)]
    assert palette.dominant_colors(solid((0, 0, 0, 0), mode="RGBA")) == []


def test_quantization_deterministic():
    import random

    r = random.Random(7)
    img = Image.new("RGB", (120, 90))
    img.putdata([(r.randrange(256), r.randrange(256), r.randrange(256)) for _ in range(120 * 90)])
    a = palette.dominant_colors(img, k=5)
    assert a == palette.dominant_colors(img, k=5) and len(a) == 5
    assert sum(s for _, s in a) == pytest.approx(1.0, abs=0.01)
    assert [s for _, s in a] == sorted((s for _, s in a), reverse=True)


def test_thumb_url_only_small_da_cdn():
    assert palette.thumb_url({"thumb": "https://images-wixmp-1.wixmp.com/a.jpg"})
    assert palette.thumb_url({"thumb": "http://images-wixmp-1.wixmp.com/a.jpg"}) is None
    assert palette.thumb_url({"thumb": "https://evil.example/a.jpg"}) is None
    thumbs = [{"src": "https://x.wixmp.com/150.jpg", "width": 150}, {"src": "https://x.wixmp.com/300.jpg", "width": 300},
              {"src": "https://x.wixmp.com/600.jpg", "width": 600}]
    assert palette.thumb_url({"thumbs": thumbs}).endswith("300.jpg")


# --- Бережное скачивание ---

class FakeFetch:
    def __init__(self, data=None, fail=None):
        self.calls: list[str] = []
        self.data = data or png((30, 60, 200))
        self.fail = fail or {}

    def __call__(self, url):
        self.calls.append(url)
        for key, err in self.fail.items():
            if key in url:
                raise err
        return self.data


def test_step_respects_per_hour_and_cache(conn):
    for i in range(5):
        lot(conn, i, published=NOW - (i + 1) * DAY)
    lot(conn, 50, thumb="https://evil.example/x.jpg")       # чужой хост — не качаем
    lot(conn, 51, thumb=None)                              # без превью — пропуск
    f = FakeFetch()
    r = palette.step(conn, f, NOW, per_hour=3, niche={"moth"})
    assert r["fetched"] == 3 and r["cached"] == 3 and len(f.calls) == 3
    assert palette.step(conn, f, NOW + 60, per_hour=3, niche={"moth"})["skipped"] == "лимит в час исчерпан"
    assert len(f.calls) == 3
    r = palette.step(conn, f, NOW + palette.HOUR, per_hour=3, niche={"moth"})
    assert r["cached"] == 2 and len(f.calls) == 5
    palette.step(conn, f, NOW + 2 * palette.HOUR, per_hour=3, niche={"moth"})
    assert len(f.calls) == 5                               # всё в кэше — больше не качаем
    assert not any("evil" in u for u in f.calls)
    assert palette.cached_family(conn, "da:0") == "blue"


def test_step_niche_filter_and_own_first(conn):
    lot(conn, 1, title="Dragon adopt")                     # не её ниша
    lot(conn, 2, title="Moth adopt")
    lot(conn, 3, title="Cat thing", own=True)              # её — всегда
    f = FakeFetch()
    palette.step(conn, f, NOW, per_hour=10, niche={"moth"})
    assert [u.split("/")[-1].split(".")[0] for u in f.calls] == ["3", "2"]


def test_step_429_pauses_for_an_hour(conn):
    for i in range(3):
        lot(conn, i)
    f = FakeFetch(fail={"/0.jpg": palette.FetchError(429)})
    r = palette.step(conn, f, NOW, per_hour=10, niche={"moth"})
    assert r["paused_until"] == NOW + palette.HOUR and len(f.calls) == 1   # без повторов
    assert palette.step(conn, f, NOW + 1800, per_hour=10, niche={"moth"})["skipped"].startswith("пауза")
    assert len(f.calls) == 1
    f.fail = {}
    r = palette.step(conn, f, NOW + palette.HOUR + 1, per_hour=10, niche={"moth"})
    assert r["cached"] == 3


def test_step_404_cached_temp_retried_next_day(conn):
    lot(conn, 1)
    lot(conn, 2)
    f = FakeFetch(fail={"/1.jpg": palette.FetchError(404), "/2.jpg": palette.FetchError(None, "timeout")})
    r = palette.step(conn, f, NOW, per_hour=10, niche={"moth"})
    assert r["errors"] == 2
    palette.step(conn, f, NOW + 2 * palette.HOUR, per_hour=10, niche={"moth"})
    assert len(f.calls) == 2
    f.fail = {}
    palette.step(conn, f, NOW + 2 * DAY, per_hour=10, niche={"moth"})
    assert f.calls[-1].endswith("/2.jpg?token=t") and len(f.calls) == 3   # 404 не повторяем, таймаут — через сутки


def test_step_broken_image(conn):
    lot(conn, 1)
    r = palette.step(conn, FakeFetch(data=b"not an image"), NOW, niche={"moth"})
    assert r["errors"] == 1 and palette.cached_family(conn, "da:1") is None


def test_make_fetch_maps_status():
    import httpx

    def handler(request):
        return httpx.Response(429 if "slow" in str(request.url) else 200, content=b"img")

    fetch = palette.make_fetch(httpx.Client(transport=httpx.MockTransport(handler)))
    assert fetch("https://a.wixmp.com/ok.jpg") == b"img"
    with pytest.raises(palette.FetchError) as e:
        fetch("https://a.wixmp.com/slow.jpg")
    assert e.value.status == 429


# --- Отчёт ---

def seed_market(conn, spec):
    """spec: [(color, n, sold)] — n лотов ниши «moth» с готовой палитрой и известным итогом."""
    n = 100
    for color, total, sold in spec:
        for j in range(total):
            iid = lot(conn, n, sold=j < sold, published=NOW - (10 + n % 30) * DAY)
            palette.save(conn, iid, palette.dominant_colors(solid(color)), "u", NOW)
            n += 1
    conn.commit()


def test_report_refuses_below_min_side(conn):
    seed_market(conn, [((30, 60, 200), 3, 2), ((200, 30, 30), 2, 0)])
    text = palette.report(conn, NOW, niche={"moth"})
    assert "Палитра посчитана у 5 из 5" in text and "Вывода нет" in text and "из нужных 8" in text


def test_report_comparison_only_above_family_threshold(conn):
    seed_market(conn, [((30, 60, 200), 10, 9), ((200, 30, 30), 10, 1)])
    text = palette.report(conn, NOW, niche={"moth"})
    assert "• синие: ставку получили 9 из 10" in text and "палитры лотов, наблюдение" in text
    assert "«синие» чаще получают ставку" in text and "при пороге 0.95 (2 сравнений)" in text
    assert "«красные» реже получают ставку" in text


def test_report_no_claim_when_unclear(conn):
    seed_market(conn, [((30, 60, 200), 10, 6), ((200, 30, 30), 10, 4)])
    text = palette.report(conn, NOW, niche={"moth"})
    assert "чаще получают" not in text and "реже получают" not in text
    assert "Вывода нет: ни одна палитра" in text


def test_report_small_family_marked(conn):
    seed_market(conn, [((30, 60, 200), 10, 5), ((200, 30, 30), 3, 1)])
    text = palette.report(conn, NOW, niche={"moth"})
    assert "• красные: 3 лот(ов) — вывода нет" in text


def test_of_file(conn, tmp_path):
    seed_market(conn, [((30, 60, 200), 10, 9), ((200, 30, 30), 10, 1)])
    path = tmp_path / "mine.png"
    img = solid((255, 255, 255), (100, 100))
    img.paste((30, 60, 200), (0, 0, 60, 100))
    img.save(path)
    text = palette.of_file(conn, str(path), NOW, niche={"moth"})
    assert "Палитра «mine.png»: синие" in text and "#1e3cc8" in text
    assert "ставку получили 9 из 10" in text and "«синие» чаще" in text
    assert palette.of_file(conn, str(tmp_path / "nope.png"), NOW).startswith("Файл не нашла")
    (tmp_path / "bad.png").write_bytes(b"xx")
    assert palette.of_file(conn, str(tmp_path / "bad.png"), NOW).startswith("Не смогла открыть")


# --- Регрессии после проверки ---

def test_hourly_cap_is_a_sliding_window_not_clock_hour(conn):
    for i in range(10):
        lot(conn, i)
    f = FakeFetch()
    t = NOW - 100                                          # NOW ровно на границе часа: hh:58:20
    assert palette.step(conn, f, t, per_hour=3, niche={"moth"})["fetched"] == 3
    assert palette.step(conn, f, t + 200, per_hour=3, niche={"moth"})["skipped"] == "лимит в час исчерпан"
    assert palette.step(conn, f, t + palette.HOUR + 1, per_hour=3, niche={"moth"})["fetched"] == 3
    assert len(f.calls) == 6


def test_hard_cap_60_and_step_max(conn):
    for i in range(80):
        lot(conn, i)
    f = FakeFetch()
    got = [palette.step(conn, f, NOW + 10 * k, per_hour=500, niche={"moth"})["fetched"] for k in range(5)]
    assert got == [palette.STEP_MAX, palette.STEP_MAX, palette.STEP_MAX, 0, 0] and len(f.calls) == 60
    assert palette.used_this_hour(conn, NOW + 50) == 60


def test_delay_between_downloads(conn, monkeypatch):
    monkeypatch.setattr(palette, "DELAY_S", 1.5)
    for i in range(3):
        lot(conn, i)
    slept: list[float] = []
    palette.step(conn, FakeFetch(), NOW, per_hour=10, niche={"moth"}, sleep=slept.append)
    assert slept == [1.5, 1.5]                             # между файлами, не перед первым


def test_single_403_skips_lot_for_a_day_without_pause(conn):
    for i in range(3):
        lot(conn, i)
    f = FakeFetch(fail={"/0.jpg": palette.FetchError(403)})
    r = palette.step(conn, f, NOW, per_hour=10, niche={"moth"})
    assert r["paused_until"] is None and r["errors"] == 1 and r["cached"] == 2
    palette.step(conn, f, NOW + 2 * palette.HOUR, per_hour=10, niche={"moth"})
    assert len(f.calls) == 3                               # тот же 403 не дёргаем каждый час
    f.fail = {}
    palette.step(conn, f, NOW + 2 * DAY, per_hour=10, niche={"moth"})
    assert f.calls[-1].endswith("/0.jpg?token=t") and palette.cached_family(conn, "da:0") == "blue"


def test_403_on_two_lots_in_a_row_pauses(conn):
    for i in range(4):
        lot(conn, i)
    f = FakeFetch(fail={"/0.jpg": palette.FetchError(403), "/1.jpg": palette.FetchError(403)})
    r = palette.step(conn, f, NOW, per_hour=10, niche={"moth"})
    assert r["paused_until"] == NOW + palette.HOUR and len(f.calls) == 2
    f.fail = {}
    r = palette.step(conn, f, NOW + palette.HOUR + 1, per_hour=10, niche={"moth"})
    assert r["cached"] == 2 and len(f.calls) == 4          # оба 403-лота отложены на сутки


def test_odd_thumb_width_does_not_crash(conn):
    thumbs = [{"src": "https://x.wixmp.com/a.jpg", "width": "150px"}, {"src": "https://x.wixmp.com/b.jpg", "width": None}]
    assert palette.thumb_url({"thumbs": thumbs}) in ("https://x.wixmp.com/a.jpg", "https://x.wixmp.com/b.jpg")
    db.upsert_item(conn, Item(id="da:9", source="deviantart", title="Moth adopt", url="u", lot_type="auction",
                              status="closed", own=True, published_ts=NOW - DAY, extra={"thumbs": thumbs}), now=NOW)
    conn.commit()
    assert palette.step(conn, FakeFetch(), NOW, niche={"moth"})["cached"] == 1


def test_huge_dimensions_refused_without_decoding(conn):
    buf = io.BytesIO()
    solid((30, 60, 200), (5000, 5000)).save(buf, "PNG")
    assert len(buf.getvalue()) < palette.MAX_BYTES        # сжатый PNG мал, а пикселей 25 млн
    lot(conn, 1)
    r = palette.step(conn, FakeFetch(data=buf.getvalue()), NOW, niche={"moth"})
    assert r["errors"] == 1 and palette.cached_family(conn, "da:1") is None
    with pytest.raises(ValueError, match="слишком большая"):
        palette.colors_from_bytes(buf.getvalue())


def test_large_image_shrunk_before_rgba_and_input_untouched():
    img = solid((0, 0, 0, 0), (3000, 2000), "RGBA")
    img.paste((248, 187, 208, 255), (0, 0, 1500, 2000))
    assert palette.family(palette.dominant_colors(img)) == "pastel pink" and img.size == (3000, 2000)
    p = solid((30, 60, 200), (1000, 1000)).convert("P")
    assert palette.family(palette.dominant_colors(p)) == "blue"


def test_of_file_refuses_too_many_pixels(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(palette, "MAX_LOCAL_PIXELS", 100 * 100)
    path = tmp_path / "big.png"
    solid((30, 60, 200), (200, 200)).save(path)
    assert "уменьшенную копию" in palette.of_file(conn, str(path), NOW)


def test_make_fetch_size_cap_and_network_errors():
    import httpx

    def handler(request):
        if "boom" in str(request.url):
            raise httpx.ConnectTimeout("t", request=request)
        return httpx.Response(200, content=b"x" * (palette.MAX_BYTES + 1))

    fetch = palette.make_fetch(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(palette.FetchError, match="слишком большой") as e:
        fetch("https://a.wixmp.com/big.jpg")
    assert e.value.status is None
    with pytest.raises(palette.FetchError) as e:
        fetch("https://a.wixmp.com/boom.jpg")
    assert e.value.status is None and "ConnectTimeout" in str(e.value)


def test_client_does_not_follow_redirects():
    with palette.client() as h:
        assert h.follow_redirects is False


def test_redirect_is_an_error_not_followed():
    import httpx

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/x.jpg"})

    fetch = palette.make_fetch(httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))
    with pytest.raises(palette.FetchError) as e:
        fetch("https://a.wixmp.com/r.jpg")
    assert e.value.status == 302 and seen == ["https://a.wixmp.com/r.jpg"]


def test_her_non_lots_not_downloaded(conn):
    for n, kind in ((1, "ych"), (2, "commission"), (3, "other"), (4, "auction"), (5, "flatsale")):
        db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title="Moth", url="u", lot_type=kind,
                                  status="open", own=True, published_ts=NOW - DAY, extra={"thumb": THUMB.format(n=n)}),
                       now=NOW)
    conn.commit()
    assert sorted(i for i, _u in palette.candidates(conn, NOW, {"moth"})) == ["da:4", "da:5"]


def test_no_write_lock_held_during_download(conn, tmp_path):
    """Регрессия: счётчик скачиваний открывал запись до fetch и держал её весь проход (до ~3 мин) —
    записи из чата падали с «database is locked»."""
    import sqlite3

    for i in range(3):
        lot(conn, i, published=NOW - (i + 1) * DAY)
    other = sqlite3.connect(tmp_path / "p.sqlite3", timeout=0.05)
    locked = []
    data = png((30, 60, 200))

    def fetch(url):
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        except sqlite3.OperationalError:
            locked.append(url)
        return data

    r = palette.step(conn, fetch, NOW, per_hour=3, niche={"moth"})
    other.close()
    assert r["fetched"] == 3 and not locked


def test_report_refuses_when_outcome_coverage_low(conn):
    """Регрессия: palette.report не проверял, у какой доли аукционов итог известен (forecast.niches проверяет) —
    доля «со ставкой» по одним дочитанным лотам выходила завышенной."""
    seed_market(conn, [((30, 60, 200), 10, 9), ((200, 30, 30), 10, 1)])
    for i in range(60):  # 60 старых аукционов рынка, ставки читали рано — итог неизвестен
        n = 500 + i
        lot(conn, n)
        conn.execute("INSERT OR REPLACE INTO auctions (deviation_id, top_amount, currency, n_bids, ended, updated_ts) "
                     "VALUES (?, NULL, 'usd', 0, 0, ?)", (f"da:{n}", NOW - 19 * DAY))
        conn.execute("UPDATE items SET status = 'open' WHERE id = ?", (f"da:{n}",))
    conn.commit()
    text = palette.report(conn, NOW, niche={"moth"})
    assert "Вывода нет: итог известен только у" in text and "чаще получают" not in text


def test_her_own_lots_not_in_market_pool(conn):
    seed_market(conn, [((30, 60, 200), 10, 9), ((200, 30, 30), 10, 1)])
    for i in range(30):
        lot(conn, 900 + i, own=True, sold=True)
        palette.save(conn, f"da:{900 + i}", [((200, 30, 30), 1.0)], now=NOW)
    text = palette.report(conn, NOW, niche={"moth"})
    assert "• красные: ставку получили 1 из 10" in text
