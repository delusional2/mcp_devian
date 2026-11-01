"""Рынок её ниши: поисковые запросы, сбор лотов поиском RSS и тегами API, кто ставит, карточка групп.
Только подставные http/клиент, без сети."""

import json

import httpx
import pytest
from fixtures import GROUP_GALLERY_PAGE, RSS_LIVE, RSS_POPULAR_FALLBACK

from mcp_devian import da_market, db
from mcp_devian import deviantart as da
from mcp_devian.config import Config, DAGroup, DeviantArt, Filters, Posting
from mcp_devian.da_api import RateLimited
from mcp_devian.db import Item

NOW = 1_793_534_400.0  # 01.11.2026 12:00 UTC — как в RSS_LIVE
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.sqlite3")
    yield c
    c.close()


def cfg_with(characters=(), keywords=(), me="", groups=()):
    return Config(posting=Posting(characters=list(characters)), filters=Filters(keywords=list(keywords)),
                  deviantart=DeviantArt(my_username=me, groups=list(groups)))


def rss_http(pages: dict[str, str], seen: list | None = None):
    """q → XML. Неизвестный запрос — запасная лента «Popular», как у настоящего DeviantArt."""
    def handler(req):
        if seen is not None:
            seen.append(req.url)
        return httpx.Response(200, text=pages.get(req.url.params.get("q"), RSS_POPULAR_FALLBACK))
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- запросы ниши ---

def test_niche_queries_from_characters_and_own_lots(conn):
    cfg = cfg_with(characters=[{"name": "Ember", "tags": ["dragon", "pastel"]},
                               {"name": "Mochi", "tags": ["cat", "kawaii"]},
                               {"name": "Vex", "tags": ["dragon", "gothic"]}],
                   keywords=["moth"])
    db.upsert_item(conn, Item(id="da:1", source="deviantart", title="Moth dragon adopt", url="u", own=True,
                              extra={"api_tags": ["mothadopt"]}), NOW)
    qs = da_market.niche_queries(conn, cfg)
    assert len(qs) <= 6 and len(set(qs)) == len(qs)
    assert qs[0] == "dragon adoptable auction"  # чаще всего — дракон
    assert {"moth adoptable auction", "cat adoptable auction"} <= set(qs)
    assert any(q.endswith(" adopt") for q in qs) or len(qs) == 6
    assert all("tag:" not in q and "sort:" not in q for q in qs)


def test_niche_queries_default_without_niche(conn):
    assert da_market.niche_queries(conn, Config()) == ["adoptable auction", "adopt"]


# --- слой A: поиск RSS ---

def test_sample_market_rss_stores_search_lots(conn):
    seen: list = []
    http = rss_http({"dragon adoptable auction": RSS_LIVE}, seen)
    cfg = cfg_with(me="Linker")
    res = da_market.sample_market(conn, cfg, http=http, now=NOW,
                                  queries=["dragon adoptable auction", "tag:dragonadopt sort:time"], pause=lambda: None)
    assert res.found == 5 and res.too_old == 1 and res.new == 4 and res.priced == 4
    assert res.ignored == ["tag:dragonadopt sort:time"] and not res.rate_limited
    assert len(seen) == 2  # одна страница на запрос
    rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == ["da:1405000001", "da:1405000002", "da:1405000003", "da:1405000004"]
    for r in rows:
        assert r["place"] == "поиск DA: dragon adoptable auction"
        assert r["matched"] == 0 and r["match_kind"] is None  # без уведомлений
        extra = json.loads(r["extra"])
        assert extra["via"] == ["search:dragon adoptable auction"] and extra["places"] == []
    assert db.get_item(conn, "da:1405000004")["own"] == 1  # её лот, найденный поиском
    assert db.get_item(conn, "da:1405000005") is None  # старше 180 дней — не сохраняем
    text = da_market.render(res)
    assert "нашла 5, новых 4, с ценой 4, старше 180 дн отброшено 1" in text
    assert "DeviantArt проигнорировал запросы: «tag:dragonadopt sort:time»" in text
    assert da_market.HONESTY in text


def test_sample_market_keeps_feed_place_and_match(conn):
    db.upsert_item(conn, Item(id="da:1405000003", source="deviantart", title="Dragon adoptable auction (open)",
                              url="u", place="da-adopts: auctions", matched=True, match_kind="lot",
                              match_reason="слова: dragon", extra={"places": ["da-adopts: auctions"]}), NOW - 60)
    res = da_market.sample_market(conn, Config(), http=rss_http({"dragon": RSS_LIVE}), now=NOW, queries=["dragon"],
                                  pause=lambda: None)
    assert res.new == 3
    row = db.get_item(conn, "da:1405000003")
    assert row["place"] == "da-adopts: auctions" and row["matched"] == 1 and row["match_kind"] == "lot"
    extra = json.loads(row["extra"])
    assert extra["places"] == ["da-adopts: auctions"] and extra["via"] == ["search:dragon"]


def test_sample_market_stops_on_429(conn):
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(429)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    res = da_market.sample_market(conn, Config(), http=http, now=NOW, queries=["a", "b", "c"], pause=lambda: None)
    assert res.rate_limited and len(calls) == 1
    assert "притормозить" in da_market.render(res)
    # обычная лента тоже подождёт: пауза общая (da_access), а не ошибка в строке runs
    assert conn.execute("SELECT until_ts FROM da_cooldown WHERE kind = 'rss'").fetchone()
    # и следующий поиск в ближайшие 30 минут ничего не спрашивает
    again = da_market.sample_market(conn, Config(), http=http, now=NOW + 600, queries=["a"], pause=lambda: None)
    assert again.rate_limited and len(calls) == 1


def test_maybe_sample_runs_once_per_six_hours(conn, monkeypatch):
    monkeypatch.setattr(da, "polite_pause", lambda: None)
    import mcp_devian.posting as posting

    monkeypatch.setattr(posting, "da_client", lambda cfg: (_ for _ in ()).throw(RuntimeError("нет client_id")))
    seen: list = []
    http = rss_http({"adoptable auction": RSS_LIVE, "adopt": RSS_LIVE}, seen)
    lines = da_market.maybe_sample(conn, Config(), NOW, http=http)
    assert lines and lines[0].startswith("рынок DA: нашла") and len(seen) == 2
    assert da_market.maybe_sample(conn, Config(), NOW + 5 * 3600, http=http) == []
    assert len(seen) == 2
    da_market.maybe_sample(conn, Config(), NOW + 6 * 3600 + 1, http=http)
    assert len(seen) == 4


def test_maybe_sample_waits_after_429_and_never_raises(conn, monkeypatch):
    monkeypatch.setattr(da, "polite_pause", lambda: None)
    db.record_run(conn, "deviantart", "DeviantArt просит притормозить (429)", NOW - 600)
    seen: list = []
    assert da_market.maybe_sample(conn, Config(), NOW, http=rss_http({}, seen)) == [] and seen == []

    class Boom:
        def get(self, *a, **k):
            raise ValueError("сломалось")
    out = da_market.maybe_sample(conn, Config(), NOW + 3600, http=Boom())
    assert out == ["рынок DA: не получилось (ValueError)"]


def test_maybe_sample_resolves_folder_uuid(conn, monkeypatch):
    monkeypatch.setattr(da, "polite_pause", lambda: None)

    def handler(req):
        if req.url.path.endswith("/gallery"):
            return httpx.Response(200, text=GROUP_GALLERY_PAGE)
        return httpx.Response(200, text=RSS_LIVE)
    cfg = cfg_with(groups=[DAGroup(url="https://www.deviantart.com/da-adopts/gallery/26780440", label="da: auctions")])
    da_market.maybe_sample(conn, cfg, NOW, http=httpx.Client(transport=httpx.MockTransport(handler)))
    row = conn.execute("SELECT * FROM da_folders WHERE grp = 'da-adopts' AND folder_id = '26780440'").fetchone()
    assert row["uuid"] == "A4A80DA9-DEA9-2A1E-26EB-EB72E5F164BC" and row["size"] == 9395
    from mcp_devian import auctions

    assert auctions.folder_targets(conn, cfg) == [("da-adopts", "A4A80DA9-DEA9-2A1E-26EB-EB72E5F164BC")]


# --- слой B: теги API ---

def api_dev(number, published=NOW - 2 * DAY, comments=2, author="seller", title=None):
    return {"deviationid": f"UUID-{number}", "url": f"https://www.deviantart.com/{author}/art/Dragon-{number}",
            "title": title or f"Dragon adopt auction {number}", "author": {"username": author},
            "published_time": str(int(published)), "stats": {"comments": comments, "favourites": 4}}


class ApiClient:
    def __init__(self, browse, suggest=(), meta=None, fail=None):
        self.browse, self.suggest, self.meta, self.fail = browse, list(suggest), meta or {}, fail
        self.calls: list = []

    def is_connected(self):
        return True

    def _request(self, method, path, params=None, **kw):
        self.calls.append((path, params))
        if self.fail and path in self.fail:
            raise self.fail[path]
        if path == "/browse/tags":
            return {"has_more": False, "results": self.browse}
        if path == "/browse/tags/search":
            return {"results": [{"tag_name": t} for t in self.suggest]}
        if path == "/deviation/metadata":
            ids = [v for k, v in params if k == "deviationids[]"]
            return {"metadata": [{"deviationid": u, "description": self.meta.get(u, "<p>SB: $30 MI: $2</p>"),
                                  "tags": [{"tag_name": "dragonadopt"}], "stats": {}} for u in ids]}
        raise AssertionError(path)


def test_layer_b_maps_ids_and_fetches_metadata_once(conn):
    db.upsert_item(conn, Item(id="da:1405000101", source="deviantart", title="Dragon adopt auction", url="u",
                              place="da-adopts: auctions"), NOW - 60)
    client = ApiClient([api_dev(1405000101), api_dev(1405000102), api_dev(1405000103, comments=0),
                        api_dev(1405000104, published=NOW - 400 * DAY)],
                       suggest=["dragonadopt", "dragonadopts", "dragon"])
    cfg = cfg_with(characters=[{"name": "Ember", "tags": ["dragon"]}])
    res = da_market.sample_market(conn, cfg, http=rss_http({}), client=client, now=NOW, sleep=lambda s: None,
                                  pause=lambda: None)
    paths = [p for p, _ in client.calls]
    assert paths.count("/deviation/metadata") == 1
    assert paths.count("/browse/tags/search") == 2 and paths.count("/browse/tags") <= 2
    assert len([p for p in paths if p.startswith("/browse")]) <= da_market.MAX_API_CALLS
    meta_ids = [v for k, v in client.calls[paths.index("/deviation/metadata")][1] if k == "deviationids[]"]
    assert meta_ids == ["UUID-1405000102", "UUID-1405000103"]  # только новые, старше 180 дн — нет
    new = db.get_item(conn, "da:1405000102")
    extra = json.loads(new["extra"])
    assert new["place"].startswith("поиск DA: тег ") and new["price_usd"] == 30 and new["lot_type"] == "auction"
    assert extra["uuid"] == "UUID-1405000102" and extra["api_tags"] == ["dragonadopt"]
    assert extra["via"][0].startswith("api_tag:")
    old = json.loads(db.get_item(conn, "da:1405000101")["extra"])
    assert old["uuid"] == "UUID-1405000101" and db.get_item(conn, "da:1405000101")["place"] == "da-adopts: auctions"
    snap = conn.execute("SELECT comments FROM deviation_stats WHERE deviation_id = 'da:1405000103'").fetchone()
    assert snap["comments"] == 0
    assert res.via_api >= 4 and res.too_old >= 1
    tags = {r["tag"] for r in conn.execute("SELECT tag FROM da_market_tags")}
    assert {"dragonadopt", "dragonadopts"} <= tags and "dragon" not in tags
    # второй раз в тот же час слой B не ходит
    client.calls.clear()
    da_market.sample_market(conn, cfg, http=rss_http({}), client=client, now=NOW + 600, sleep=lambda s: None,
                            pause=lambda: None)
    assert client.calls == []


def test_layer_b_rate_limit_stops(conn):
    client = ApiClient([], fail={"/browse/tags": RateLimited("429", 429, status=429)})
    res = da_market.sample_market(conn, Config(), http=rss_http({}), client=client, now=NOW, sleep=lambda s: None,
                                  pause=lambda: None)
    assert res.rate_limited and res.api_rate_limited
    assert [p for p, _ in client.calls] == ["/browse/tags"]
    from mcp_devian import auctions

    assert auctions.gate(conn, NOW + 60).startswith("ставки: DeviantArt просил притормозить")


def test_manual_search_does_not_take_over_tag_rotation(conn):
    cfg = cfg_with(characters=[{"name": "A", "tags": ["wolf"]}, {"name": "B", "tags": ["cat"]},
                               {"name": "C", "tags": ["dragon"]}])
    client = ApiClient([])
    da_market.sample_market(conn, cfg, http=rss_http({}), client=client, now=NOW, queries=["mermaid"],
                            sleep=lambda s: None, pause=lambda: None)
    assert [p[1]["tag"] for p in client.calls if p[0] == "/browse/tags"] == ["mermaidadopt"]
    assert conn.execute("SELECT COUNT(*) FROM da_market_tags WHERE tag = 'mermaidadopt'").fetchone()[0] == 0
    browsed = []
    for i in range(1, 4):
        client.calls.clear()
        da_market.sample_market(conn, cfg, http=rss_http({}), client=client, now=NOW + i * 3700,
                                sleep=lambda s: None, pause=lambda: None)
        browsed += [p[1]["tag"] for p in client.calls if p[0] == "/browse/tags"]
    assert "mermaidadopt" not in browsed
    assert {"wolfadopt", "catadopt", "dragonadopt"} <= set(browsed)


def test_tag_suggestions_rotate_across_species(conn):
    conn.executescript(da_market.TAGS_SCHEMA)
    species = ["wolf", "cat", "dragon"]
    asked = []
    for week in range(3):
        client = ApiClient([])
        now = NOW + week * da_market.TAGS_SUGGEST_EVERY_S
        da_market._suggest_tags(conn, client, species, now, lambda s: None, 4)
        asked.append(client.calls[0][1]["tag_name"])
    assert {a.replace("adopt", "") for a in asked} == set(species)


def test_sampler_waits_after_enrich_429(conn):
    seen: list = []
    db.record_run(conn, "enrich", "DeviantArt просит притормозить (429) — продолжу в следующий раз", NOW - 60)
    assert "429" in db.last_run(conn, "enrich")["last_error"]
    res = da_market.sample_market(conn, Config(), http=rss_http({}, seen), client=ApiClient([]), now=NOW,
                                  queries=["a"], pause=lambda: None)
    assert res.rate_limited and seen == []


def test_maybe_sample_pauses_before_first_request(conn, monkeypatch):
    events = []
    monkeypatch.setattr(da, "polite_pause", lambda: events.append("pause"))
    import mcp_devian.posting as posting

    monkeypatch.setattr(posting, "da_client", lambda cfg: (_ for _ in ()).throw(RuntimeError("no token")))

    def handler(req):
        events.append("GET")
        return httpx.Response(200, text=RSS_POPULAR_FALLBACK)
    da_market.maybe_sample(conn, Config(), NOW, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert events and events[0] == "pause" and "GET" in events


def test_explicit_query_tags():
    assert da_market._explicit_tag("dragon") == "dragonadopt"
    assert da_market._explicit_tag("Dragon Adopt") == "dragonadopt"
    assert da_market._explicit_tag("dragon adoptable auction") is None


# --- кто ставит ---

def _auction(conn, n, title, bidders, currency="usd", own=False, place="g: auctions", age=3):
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=title, url=f"u{n}", place=place, own=own,
                              lot_type="auction", published_ts=NOW - age * DAY, extra={"places": [place]}), NOW)
    conn.execute("INSERT INTO auctions (deviation_id, currency, n_bids, updated_ts, extra) VALUES (?, ?, ?, ?, ?)",
                 (f"da:{n}", currency, len(bidders), NOW, json.dumps({"bidders": bidders})))
    conn.commit()


def bid(user, amount, currency="usd", n=1):
    return {"user": user, "amount": amount, "currency": currency, "ts": NOW - DAY, "n": n}


def test_buyers_keep_units_separate(conn):
    _auction(conn, 1, "Dragon adopt", [bid("Anna", 30, n=2), bid("bob", 20)])
    _auction(conn, 2, "Moth dragon adopt", [bid("anna", 45)], place="moth-group: open")
    _auction(conn, 3, "Cat adopt points", [bid("anna", 3000, "points")], currency="points")
    _auction(conn, 4, "Old dragon", [bid("anna", 999)], age=90)
    _auction(conn, 5, "Her lot", [bid("me", 50), bid("carl", 25)], own=True)
    rows = da_market.buyers(conn, days=60, now=NOW, my_username="Me")
    by = {(r["user"].lower(), r["unit"]): r for r in rows}
    usd = by[("anna", "usd")]
    assert usd["lots"] == 2 and usd["bids"] == 3 and usd["max_bid"] == 45
    assert usd["species"][0] == "dragon" and set(usd["places"]) == {"g: auctions", "moth-group: open"}
    assert by[("anna", "points")]["max_bid"] == 3000  # поинты отдельно
    assert ("me", "usd") not in by and by[("carl", "usd")]["on_her_lots"] == 1
    text = da_market.render_buyers(rows, days=60)
    assert "никому не пишем" in text and "$45" in text and "3000 pts" in text and "за 60 дн" in text
    assert "прочитанных ставок нет" in da_market.render_buyers([], days=30)


# --- карточка групп ---

def test_lots_per_day_arithmetic():
    assert da_market.lots_per_day([0, DAY, 2 * DAY], 3 * DAY) == pytest.approx(1.0)
    assert da_market.lots_per_day([DAY / 2] * 8, DAY) == pytest.approx(16.0)
    assert da_market.lots_per_day([5.0], DAY) is None
    # три лота за час полгода назад — мёртвая папка, а не 24 лота в день
    dead = da_market.lots_per_day([NOW - 200 * DAY, NOW - 200 * DAY + 1800, NOW - 200 * DAY + 3600], NOW)
    assert dead < 0.02
    # одна старая работа, недавно добавленная в группу, окно не растягивает
    busy = [NOW - i * 3600 for i in range(1, 20)] + [NOW - 300 * DAY]
    assert da_market.lots_per_day(busy, NOW) > 15


def test_render_scorecard_dead_folder_below_live_one():
    dead = {"grp": "dead", "folder": "1", "name": "old", "size": 3, "niche_share": 1.0, "n": 3,
            "lots_per_day": da_market.lots_per_day([NOW - 200 * DAY + h * 3600 for h in range(3)], NOW)}
    live = {"grp": "da-adopts", "folder": "2", "name": "Auctions", "size": 9000, "niche_share": 0.5, "n": 40,
            "lots_per_day": da_market.lots_per_day([NOW - i * 1800 for i in range(40)], NOW)}
    text = da_market.render_scorecard([dead, live])
    assert text.index("da-adopts") < text.index("dead")


def test_group_scorecard_counts_and_bid_share_threshold(conn):
    seen: list = []

    def handler(req):
        seen.append(req.url)
        if req.url.path.endswith("/gallery"):
            return httpx.Response(200, text=GROUP_GALLERY_PAGE)
        return httpx.Response(200, text=RSS_LIVE)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = cfg_with(characters=[{"name": "Ember", "tags": ["dragon"]}])
    rows = da_market.group_scorecard(conn, http, cfg, "da-adopts", NOW, pause=lambda: None)
    assert len(seen) <= 4
    assert [r["folder"] for r in rows] == ["26780440", "75027420"]  # журналы — не папка лотов
    r = rows[0]
    assert r["size"] == 9395 and r["n"] == 5
    stamps = [e["published_ts"] for e in da.parse_rss(RSS_LIVE, "x")]
    assert r["lots_per_day"] == pytest.approx(5 / ((NOW - min(stamps)) / DAY))
    assert r["niche_share"] == pytest.approx(2 / 5)  # «Dragon adoptable auction», «Moth dragon adopt»
    assert r["median_sb"] == 20 and r["unit"] == "usd"  # старты 150, 25, 15, 20, 20
    assert r["bid_share"] is None and r["known"] == 0  # меньше 10 известных итогов — без доли
    stored = conn.execute("SELECT * FROM group_scores WHERE grp = 'da-adopts' AND folder = '26780440'").fetchone()
    assert stored["n"] == 5 and stored["size"] == 9395 and stored["known"] == 0 and stored["sb_n"] == 5
    # Из базы (через неделю не обновляем) — с теми же n: сколько с известным итогом и по скольким стартам.
    [cached, _] = da_market._cached(conn, "da-adopts", NOW)
    assert cached["known"] == 0 and cached["sb_n"] == 5


def test_bid_share_needs_ten_known_outcomes(conn):
    ids = set()
    for i in range(10):
        n = f"da:{500 + i}"
        ids.add(n)
        conn.execute("INSERT INTO auctions (deviation_id, n_bids, ended, updated_ts, extra) VALUES (?, ?, 0, ?, ?)",
                     (n, 1 if i < 4 else 0, NOW, json.dumps({"read_age_days": 8 if i < 9 else 3})))
    conn.commit()
    assert da_market._bid_share(conn, ids) == (None, 9)  # у одного итог ещё не известен
    conn.execute("UPDATE auctions SET ended = 1 WHERE deviation_id = 'da:509'")
    assert da_market._bid_share(conn, ids) == (pytest.approx(0.4), 10)


def test_bid_share_open_lot_at_ten_days_is_not_an_outcome(conn):
    """Как в прогнозе (outcome_known): в 7–13 дней лот ещё виден открытым — это не «без ставок»."""
    ids = set()
    for i in range(10):
        n = f"da:{600 + i}"
        ids.add(n)
        db.upsert_item(conn, Item(id=n, source="deviantart", title=f"Moth auction [OPEN] {i}", url=f"u{i}",
                                  status="open", lot_type="auction", published_ts=NOW - 10 * DAY), NOW)
        conn.execute("INSERT INTO auctions (deviation_id, n_bids, ended, updated_ts, extra) VALUES (?, 0, 0, ?, ?)",
                     (n, NOW, json.dumps({"read_age_days": 10})))
    conn.commit()
    assert da_market._bid_share(conn, ids) == (None, 0)


def test_scorecard_many_candidates_cache_and_render(conn):
    cfg = cfg_with(characters=[{"name": "Ember", "tags": ["dragon"]}],
                   groups=[DAGroup(url="https://www.deviantart.com/da-adopts/gallery/26780440")])
    cfg.posting.group_presets = {"main": [{"group": "Dragon-Adopts", "folder_id": "F"}]}
    conn.execute("INSERT INTO sent_log (kind, target, ts, ok) VALUES ('da_group', 'moth-hub', ?, 1)", (NOW,))
    db.upsert_item(conn, Item(id="da:9", source="deviantart", title="Dragon adopt", url="u", published_ts=NOW - DAY,
                              extra={"folder_links": ["linked-group/1", "da-adopts/26780440"]}), NOW)
    assert da_market.scorecard_candidates(conn, cfg, NOW) == ["da-adopts", "dragon-adopts", "moth-hub", "linked-group"]
    requests: list = []

    def handler(req):
        requests.append(req.url)
        if req.url.path.startswith("/moth-hub"):
            return httpx.Response(404)
        if req.url.path.endswith("/gallery"):
            return httpx.Response(200, text=GROUP_GALLERY_PAGE.replace("da-adopts", req.url.path.split("/")[1]))
        return httpx.Response(200, text=RSS_LIVE)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    rows = da_market.group_scorecard_many(conn, http, cfg, ["da-adopts", "moth-hub"], NOW, pause=lambda: None)
    assert any(r.get("error") for r in rows if r["grp"] == "moth-hub")
    text = da_market.render_scorecard(rows)
    assert "по рынку, не по твоим продажам" in text and "9395 работ" in text and "n=5" in text
    assert "мало данных" in text and "moth-hub:" in text
    before = len(requests)
    again = da_market.group_scorecard_many(conn, http, cfg, ["da-adopts"], NOW + DAY, pause=lambda: None)
    assert len(requests) == before and all(r.get("cached") for r in again)  # раз в неделю
    da_market.group_scorecard_many(conn, http, cfg, ["da-adopts"], NOW + 8 * DAY, pause=lambda: None)
    assert len(requests) > before


def test_render_scorecard_sorted_by_niche_times_pace():
    rows = [{"grp": "a", "folder": "1", "name": "slow", "size": 10, "lots_per_day": 1, "niche_share": 0.9, "n": 20},
            {"grp": "b", "folder": "2", "name": "busy", "size": 10, "lots_per_day": 10, "niche_share": 0.5, "n": 60}]
    text = da_market.render_scorecard(rows)
    assert text.index("b / busy") < text.index("a / slow")
    assert "Групп для оценки нет" in da_market.render_scorecard([])
