import pytest

from mcp_devian import db, enrich
from mcp_devian.da_api import DAApiError, NotConnected, RateLimited

NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


def add(conn, n, own=False, lot_type="auction", age_days=1, uuid=None, species=None):
    extra = {}
    if uuid:
        extra["uuid"] = uuid
    if species:
        extra["species"] = species
    db.upsert_item(conn, db.Item(id=f"da:{n}", source="deviantart", title=f"Lot {n}",
                                 url=f"https://www.deviantart.com/a/art/Lot-{n}", author="a", own=own,
                                 lot_type=lot_type, published_ts=NOW - age_days * DAY, extra=extra), now=NOW)
    conn.commit()


def snapshot(conn, n, ts, views, favs, comments=0):
    conn.execute("INSERT INTO deviation_stats (deviation_id, ts, views, favourites, comments) VALUES (?, ?, ?, ?, ?)",
                 (f"da:{n}", ts, views, favs, comments))
    conn.commit()


class FakeClient:
    def __init__(self, found=None, stats=None, lookup_error=None, meta_error=None):
        self.found = found or {}          # url -> uuid
        self.stats = stats or {}          # uuid -> dict
        self.lookup_error = lookup_error
        self.meta_error = meta_error
        self.lookups = []
        self.meta_calls = []

    def deviation_uuid_from_url(self, url, folders=(), max_pages=10, miss_cache=None):
        self.lookups.append((url, max_pages))
        if self.lookup_error:
            raise self.lookup_error
        if miss_cache is not None and url in miss_cache:
            return None
        uuid = self.found.get(url)
        if uuid is None and miss_cache is not None:
            miss_cache[url] = NOW
        return uuid

    def deviation_metadata(self, uuids, ext_stats=True):
        self.meta_calls.append(list(uuids))
        if self.meta_error:
            raise self.meta_error
        return {u: self.stats.get(u, {"views": 10, "favourites": 2, "comments": 1}) for u in uuids}


def url(n):
    return f"https://www.deviantart.com/a/art/Lot-{n}"


def ids(rows):
    return [r["id"] for r in rows]


# --- candidates ---

def test_candidates_own_first_then_market_newest_first(conn):
    add(conn, 1, age_days=3)
    add(conn, 2, age_days=1)
    add(conn, 3, own=True, age_days=5, lot_type=None)
    add(conn, 4, lot_type="other")             # чужое не-лот — не нужен
    add(conn, 5, own=True, lot_type="other")   # её не-лот (просто арт) — тоже не нужен
    add(conn, 6, age_days=40)                  # слишком старый
    assert ids(enrich.candidates(conn, NOW)) == ["da:3", "da:2", "da:1"]


def test_candidates_skip_fresh_snapshot(conn):
    add(conn, 1)
    add(conn, 2)
    snapshot(conn, 1, NOW - 3600, 5, 1)
    snapshot(conn, 2, NOW - 2 * DAY, 5, 1)
    assert ids(enrich.candidates(conn, NOW)) == ["da:2"]


def test_candidates_limit(conn):
    for n in range(5):
        add(conn, n, age_days=n + 1)
    add(conn, 9, own=True)
    assert ids(enrich.candidates(conn, NOW, limit=3)) == ["da:9", "da:0", "da:1"]


# --- enrich_stats ---

def test_enrich_known_uuid_and_lookup(conn):
    add(conn, 1, uuid="U1")
    add(conn, 2, age_days=2)
    sleeps = []
    client = FakeClient(found={url(2): "U2"}, stats={"U1": {"views": 50, "favourites": 7, "comments": 3}})
    s = enrich.enrich_stats(conn, client, now=NOW, sleep=sleeps.append)
    assert s == {"lots": 2, "snapshots": 2, "lookups": 1, "misses": 0, "stopped": None, "authors": 0}
    assert client.lookups == [(url(2), 2)]
    assert sorted(client.meta_calls[0]) == ["U1", "U2"]
    assert sleeps == [1.0]
    row = enrich.latest_stats(conn, "da:1")
    assert (row["ts"], row["views"], row["favourites"], row["comments"]) == (NOW, 50, 7, 3)
    # найденный UUID запомнен — во второй раз искать не надо
    item = conn.execute("SELECT * FROM items WHERE id = 'da:2'").fetchone()
    assert '"uuid": "U2"' in item["extra"]


def test_enrich_second_pass_same_day_does_nothing(conn):
    add(conn, 1, uuid="U1")
    client = FakeClient()
    enrich.enrich_stats(conn, client, now=NOW, sleep=lambda s: None)
    s = enrich.enrich_stats(conn, client, now=NOW + 3600, sleep=lambda s: None)
    assert s["lots"] == 0 and len(client.meta_calls) == 1
    s = enrich.enrich_stats(conn, client, now=NOW + DAY + 1, sleep=lambda s: None)
    assert s["snapshots"] == 1
    assert conn.execute("SELECT COUNT(*) FROM deviation_stats").fetchone()[0] == 2


def test_enrich_lookup_budget(conn):
    for n in range(5):
        add(conn, n, age_days=n + 1)
    client = FakeClient(found={url(n): f"U{n}" for n in range(5)})
    s = enrich.enrich_stats(conn, client, now=NOW, lookup_budget=2, sleep=lambda s: None)
    assert s["lookups"] == 2 and s["lots"] == 2 and len(client.lookups) == 2


def test_enrich_budget_limits_lots(conn):
    for n in range(5):
        add(conn, n, uuid=f"U{n}", age_days=n + 1)
    client = FakeClient()
    s = enrich.enrich_stats(conn, client, now=NOW, budget=3, sleep=lambda s: None)
    assert s["lots"] == 3 and len(client.meta_calls[0]) == 3


def test_enrich_miss_cache_persisted(conn):
    add(conn, 1)
    client = FakeClient()
    s = enrich.enrich_stats(conn, client, now=NOW, sleep=lambda s: None)
    assert s["misses"] == 1 and s["lots"] == 0 and client.meta_calls == []
    assert conn.execute("SELECT url FROM uuid_misses").fetchall()[0]["url"] == url(1)
    # новый клиент (новый процесс) — кэш промахов берётся из базы: лот даже не кандидат, поиска нет
    client2 = FakeClient(found={url(1): "U1"})
    s = enrich.enrich_stats(conn, client2, now=NOW + 60, sleep=lambda s: None)
    assert s["lookups"] == 0 and client2.lookups == [] and client2.meta_calls == []


def test_enrich_expired_miss_removed_from_cache(conn):
    add(conn, 1)
    conn.executescript(enrich.MISS_SCHEMA)
    conn.execute("INSERT INTO uuid_misses (url, ts) VALUES (?, ?)", (url(1), NOW - 2 * DAY))
    conn.commit()

    class Expiring(FakeClient):
        def deviation_uuid_from_url(self, url, folders=(), max_pages=10, miss_cache=None):
            miss_cache.pop(url, None)   # так делает настоящий клиент с устаревшей записью
            return super().deviation_uuid_from_url(url, folders, max_pages, miss_cache)
    s = enrich.enrich_stats(conn, Expiring(found={url(1): "U1"}), now=NOW, sleep=lambda s: None)
    assert s["snapshots"] == 1
    assert conn.execute("SELECT COUNT(*) FROM uuid_misses").fetchone()[0] == 0


def test_enrich_lookup_api_error_counts_as_miss(conn):
    add(conn, 1)
    add(conn, 2, uuid="U2", age_days=2)
    s = enrich.enrich_stats(conn, FakeClient(lookup_error=DAApiError("boom", 2)), now=NOW, sleep=lambda s: None)
    assert s["misses"] == 1 and s["snapshots"] == 1 and s["stopped"] is None


@pytest.mark.parametrize("error,reason", [(RateLimited("429", status=429), "притормозить"),
                                          (NotConnected("нет"), "не подключён")])
def test_enrich_stops_on_rate_limit_or_disconnect(conn, error, reason):
    add(conn, 1, uuid="U1")
    add(conn, 2, age_days=2)
    add(conn, 3, age_days=3)
    client = FakeClient(found={url(3): "U3"})
    client.found[url(2)] = None
    # первый поиск — промах (запишется в кэш), потом метаданные падают
    client.meta_error = error
    s = enrich.enrich_stats(conn, client, now=NOW, sleep=lambda s: None)
    assert reason in s["stopped"] and s["snapshots"] == 0
    assert conn.execute("SELECT COUNT(*) FROM deviation_stats").fetchone()[0] == 0
    # промах, найденный до остановки, сохранён
    assert [r["url"] for r in conn.execute("SELECT url FROM uuid_misses")] == [url(2)]


def test_enrich_stops_on_rate_limit_during_lookup(conn):
    add(conn, 1)
    client = FakeClient(lookup_error=RateLimited("429", status=429))
    s = enrich.enrich_stats(conn, client, now=NOW, sleep=lambda s: None)
    assert "притормозить" in s["stopped"] and client.meta_calls == []
    assert "429" in s["stopped"]  # по этой строке da_market._cooling_down ждёт 30 минут


def test_enrich_skips_uuids_missing_from_metadata(conn):
    add(conn, 1, uuid="U1")
    add(conn, 2, uuid="U2", age_days=2)

    class Partial(FakeClient):
        def deviation_metadata(self, uuids, ext_stats=True):
            return {"U1": {"views": 1, "favourites": 1, "comments": 0}}
    s = enrich.enrich_stats(conn, Partial(), now=NOW, sleep=lambda s: None)
    assert s["lots"] == 2 and s["snapshots"] == 1


# --- demand_by ---

def test_demand_by_uses_latest_snapshot_and_groups(conn):
    add(conn, 1, species="dragon")
    add(conn, 2, species="dragon")
    add(conn, 3, species="cat")
    add(conn, 4, own=True, species="cat")
    add(conn, 5)                                 # без вида — пропускаем
    snapshot(conn, 1, NOW - 2 * DAY, 10, 1)
    snapshot(conn, 1, NOW - DAY, 100, 9, 4)      # последний снимок
    snapshot(conn, 2, NOW - DAY, 50, 3)
    snapshot(conn, 3, NOW - DAY, 40, 20)
    snapshot(conn, 4, NOW - DAY, 1000, 500)
    snapshot(conn, 5, NOW - DAY, 1000, 500)

    import json

    def species(row):
        return json.loads(row["extra"] or "{}").get("species")
    out = enrich.demand_by(conn, species, now=NOW)
    assert [d["key"] for d in out] == ["cat", "dragon"]
    cat, dragon = out
    assert cat == {"key": "cat", "lots": 1, "favourites_avg": 20, "views_avg": 40, "comments_avg": 0,
                   "fav_per_view": 0.5}
    assert dragon["lots"] == 2 and dragon["favourites_avg"] == 6 and dragon["views_avg"] == 75
    assert dragon["comments_avg"] == 2 and dragon["fav_per_view"] == 12 / 150

    mine = enrich.demand_by(conn, species, now=NOW, own=True)
    assert [(d["key"], d["favourites_avg"]) for d in mine] == [("cat", 500)]
    everyone = enrich.demand_by(conn, species, now=NOW, own=None)
    assert {d["key"]: d["lots"] for d in everyone} == {"cat": 2, "dragon": 2}


def test_demand_by_list_keys_and_window(conn):
    add(conn, 1)
    add(conn, 2, age_days=60)
    snapshot(conn, 1, NOW - DAY, 10, 0)
    snapshot(conn, 2, NOW - DAY, 10, 5)
    out = enrich.demand_by(conn, lambda row: ["wings", "horns"], now=NOW)
    assert {d["key"] for d in out} == {"wings", "horns"}
    assert all(d["lots"] == 1 and d["fav_per_view"] == 0 for d in out)


def test_demand_by_zero_views(conn):
    add(conn, 1)
    snapshot(conn, 1, NOW - DAY, 0, 0)
    assert enrich.demand_by(conn, lambda row: "x", now=NOW)[0]["fav_per_view"] is None


# --- теги API и размер продавцов ---

class ApiClient(FakeClient):
    """Плюс _request для da_browse.user_stats: ник → watchers, или исключение."""

    def __init__(self, users=None, **kw):
        super().__init__(**kw)
        self.users = users or {}
        self.profile_calls = []

    def _request(self, method, path, params=None, **kw):
        name = path.rsplit("/", 1)[-1]
        self.profile_calls.append(name)
        answer = self.users.get(name, 100)
        if isinstance(answer, Exception):
            raise answer
        return {"user": {"username": name, "type": "regular", "stats": {"watchers": answer, "friends": 1}},
                "stats": {"user_deviations": 40}}


def add_by(conn, n, author, age_days=1, lot_type="auction"):
    db.upsert_item(conn, db.Item(id=f"da:{n}", source="deviantart", title=f"Lot {n}", url=url(n), author=author,
                                 lot_type=lot_type, published_ts=NOW - age_days * DAY), now=NOW)
    conn.commit()


def test_api_tags_merged_and_survive_feed_reread(conn):
    add(conn, 1, uuid="U1")
    client = ApiClient(stats={"U1": {"views": 5, "favourites": 1, "comments": 0, "tags": ["dragonadopt", "moth"]}})
    enrich.enrich_stats(conn, client, now=NOW, sleep=lambda s: None)
    import json

    assert json.loads(db.get_item(conn, "da:1")["extra"])["api_tags"] == ["dragonadopt", "moth"]
    add(conn, 1)  # RSS снова без тегов
    extra = json.loads(db.get_item(conn, "da:1")["extra"])
    assert extra["api_tags"] == ["dragonadopt", "moth"] and extra["uuid"] == "U1"


def test_author_pass_budget_and_cache(conn):
    for i, author in enumerate(["big", "big", "mid", "small", "Other"]):
        add_by(conn, 10 + i, author)
    add_by(conn, 20, "ancient", age_days=90)       # давно не видели — не нужен
    add_by(conn, 21, "artist", lot_type="other")   # не лоты — не продавец
    client = ApiClient(users={"big": 5000})
    assert enrich.author_pass(conn, client, NOW, budget=2, sleep=lambda s: None) == 2
    assert len(client.profile_calls) == 2 and client.profile_calls[0] == "big"  # самые активные — первыми
    row = enrich.author_size(conn, "BIG")
    assert row["watchers"] == 5000 and row["deviations"] == 40
    enrich.author_pass(conn, client, NOW + 60, budget=20, sleep=lambda s: None)
    assert sorted(client.profile_calls) == ["big", "mid", "other", "small"]  # каждый ровно один раз
    client.profile_calls.clear()
    enrich.author_pass(conn, client, NOW + 29 * DAY, budget=20, sleep=lambda s: None)
    assert client.profile_calls == []  # кэш 30 дней
    enrich.author_pass(conn, client, NOW + 31 * DAY, budget=20, sleep=lambda s: None)
    assert sorted(client.profile_calls) == ["big", "mid", "other", "small"]


def test_author_pass_missing_account_cached_as_empty(conn):
    add_by(conn, 1, "gone")
    client = ApiClient(users={"gone": DAApiError("нет такого", 404)})
    assert enrich.author_pass(conn, client, NOW, sleep=lambda s: None) == 1
    assert enrich.author_size(conn, "gone")["watchers"] is None
    assert enrich.author_pass(conn, client, NOW + 60, sleep=lambda s: None) == 0


def test_author_pass_rate_limit_stops_enrich(conn):
    add(conn, 1, uuid="U1")
    add_by(conn, 2, "seller", age_days=2)  # «a» видели позже — его спросим первым
    client = ApiClient(users={"a": RateLimited("429"), "seller": 10})
    s = enrich.enrich_stats(conn, client, now=NOW, sleep=lambda s: None)
    assert s["snapshots"] == 1 and "притормозить" in s["stopped"]
    assert len(client.profile_calls) == 1  # после 429 дальше не спрашиваем
    assert enrich.author_size(conn, "seller") is None


def test_folder_snapshot_without_views_does_not_block_enrich(conn):
    add(conn, 1, uuid="U1")
    snapshot(conn, 1, NOW - 60, None, 3, 0)  # снимок со страницы папки: без просмотров
    assert ids(enrich.candidates(conn, NOW)) == ["da:1"]
    rows = enrich.demand_by(conn, lambda r: "x", now=NOW)
    assert rows[0]["views_avg"] == 0 and rows[0]["fav_per_view"] is None and rows[0]["favourites_avg"] == 3
