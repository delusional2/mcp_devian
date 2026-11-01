"""История рынка (backfill): листание закрытых аукционов групп и её галереи в пределах бюджета.
Только подставной клиент, без сети."""

import json

import pytest

from mcp_devian import backfill, da_access, db
from mcp_devian.bids import Comment
from mcp_devian.config import Config, DeviantArt, Posting
from mcp_devian.da_api import NeedsReconnect, Throttled

NOW = 1_793_534_400.0  # 01.11.2026 12:00 UTC
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.sqlite3")
    yield c
    c.close()


def dev(n: int, age_days: float, title: str = "", author: str = "seller", comments: int = 3) -> dict:
    return {"deviationid": f"UUID-{n}", "url": f"https://www.deviantart.com/{author}/art/Lot-{n}",
            "title": title or f"Dragon adopt auction #{n} [CLOSED] SB $10",
            "author": {"username": author}, "published_time": str(int(NOW - age_days * DAY)),
            "stats": {"comments": comments, "favourites": 1}}


class FakeClient:
    """Папки → страницы по 24 (сначала новые), метаданные, комментарии. Пишет все вызовы."""

    def __init__(self, galleries: dict[str, list[dict]], fail_at: int | None = None, fail=None):
        self.galleries = galleries
        self.calls: list[str] = []
        self.fail_at, self.fail = fail_at, fail

    def _hit(self, what: str) -> None:
        self.calls.append(what)
        if self.fail_at is not None and len(self.calls) >= self.fail_at:
            raise self.fail

    def _request(self, method, path, params=None, **kw):
        assert method == "GET"
        self._hit(path)
        if path == "/deviation/metadata":
            ids = [v for k, v in params if k == "deviationids[]"]
            return {"metadata": [{"deviationid": u, "title": "", "description": "Starting bid: $10. Ends 48h",
                                  "tags": [{"tag_name": "dragon"}], "author": {"username": "seller"}} for u in ids]}
        key = path.removeprefix("/gallery/")
        devs = self.galleries[key]
        off, lim = int(params["offset"]), int(params["limit"])
        page = devs[off:off + lim]
        return {"results": page, "has_more": off + lim < len(devs), "next_offset": off + lim}

    def deviation_comments(self, uuid, max_comments=500):
        self._hit(f"comments:{uuid}")
        return [Comment("1", None, "buyer1", NOW - 30 * DAY, "$12"),
                Comment("2", None, "buyer2", NOW - 29 * DAY, "$15")]

    def gallery_folders(self, name):
        self._hit(f"folders:{name}")
        return [{"folder_id": "JOURNALS", "name": "Journals"}, {"folder_id": "FOLD-1", "name": "Auctions"}]

    # чего backfill не должен делать никогда: пишем в calls и падаем
    def post_comment(self, *a, **k):
        self.calls.append("post_comment")
        raise AssertionError("backfill только читает")

    def watch(self, *a, **k):
        self.calls.append("watch")
        raise AssertionError("backfill только читает")


def folder(n0: int, count: int, step_days: float = 2.0, start_age: float = 15.0) -> list[dict]:
    return [dev(n0 + i, start_age + i * step_days) for i in range(count)]


def enqueue_one(conn, folderid="FOLD-1", days=75):
    conn.execute("INSERT INTO group_scores (grp, folder, size, lots_per_day, niche_share, n, ts) "
                 "VALUES ('dragonadopts', '555', 100, 3.0, 0.5, 24, ?)", (NOW,))
    conn.execute("INSERT INTO da_folders (grp, folder_id, uuid, name, size, ts) VALUES "
                 "('dragonadopts', '555', ?, 'Auctions', 100, ?)", (folderid, NOW))
    conn.commit()
    return backfill.enqueue(conn, None, "", days, NOW, cfg=Config())


# --- очередь ---

def test_enqueue_auto_picks_groups_and_own_gallery_idempotent(conn):
    keys = enqueue_one(conn)
    assert keys == ["group:dragonadopts/555"]
    added = backfill.enqueue(conn, None, "Me", 75, NOW, cfg=Config())
    assert added == ["own:me"]
    conn.execute("UPDATE backfill_state SET \"offset\" = 48 WHERE key = 'group:dragonadopts/555'")
    assert backfill.enqueue(conn, None, "me", 75, NOW, cfg=Config()) == []
    row = conn.execute("SELECT \"offset\", folderid, days FROM backfill_state WHERE key = 'group:dragonadopts/555'"
                       ).fetchone()
    assert row["offset"] == 48 and row["folderid"] == "FOLD-1" and row["days"] == 75


def test_enqueue_explicit_groups_and_days_clamped(conn):
    backfill.enqueue(conn, ["https://www.deviantart.com/SomeGroup/gallery/1", "other"], "", 400, NOW)
    rows = {r["key"]: r for r in conn.execute("SELECT * FROM backfill_state")}
    assert set(rows) == {"group:somegroup/?", "group:other/?"}
    assert all(r["days"] == 90 for r in rows.values())


# --- бюджет ---

def test_step_stops_at_days_cutoff(conn):
    enqueue_one(conn)
    lots = folder(1, 60, step_days=2.0)  # 15…133 дней назад
    client = FakeClient({"FOLD-1": lots})
    lines = backfill.step(conn, client, NOW, days=75, max_api=500, daily_cap=1000, sleep=lambda s: None)
    pages = [c for c in client.calls if c == "/gallery/FOLD-1"]
    # вторая страница упирается в 75 дней (в основном старше), третья — целиком старше: край
    assert len(pages) == 3
    st = conn.execute("SELECT * FROM backfill_state").fetchone()
    assert st["done"] == 1 and st["offset"] == 72 and st["edge_offset"] == 24
    ages = [(NOW - r["published_ts"]) / DAY for r in conn.execute("SELECT published_ts FROM items")]
    assert ages and max(ages) <= 75
    assert any("история рынка" in line for line in lines)


def test_step_honours_max_api_and_resumes_from_cursor(conn):
    enqueue_one(conn)
    client = FakeClient({"FOLD-1": folder(1, 30, step_days=1.0)})
    backfill.step(conn, client, NOW, days=75, max_api=5, daily_cap=1000, sleep=lambda s: None)
    assert len(client.calls) <= 5
    first = len(client.calls)
    st = conn.execute("SELECT * FROM backfill_state").fetchone()
    assert st["done"] == 0 and st["offset"] == 0  # страница не дочитана — курсор на месте
    assert backfill._used_today(conn, NOW) <= 5
    # следующий проход продолжает: прочитанные лоты не перечитывает
    client2 = FakeClient({"FOLD-1": folder(1, 30, step_days=1.0)})
    backfill.step(conn, client2, NOW + 3600, days=75, max_api=200, daily_cap=1000, sleep=lambda s: None)
    read_twice = {c for c in client.calls if c.startswith("comments:")} & \
        {c for c in client2.calls if c.startswith("comments:")}
    assert not read_twice
    assert first and conn.execute("SELECT done FROM backfill_state").fetchone()["done"] == 1


def test_step_honours_daily_cap(conn):
    enqueue_one(conn)
    conn.execute("INSERT INTO backfill_daily (day, calls) VALUES (?, 297)", (backfill._day(NOW),))
    client = FakeClient({"FOLD-1": folder(1, 30)})
    backfill.step(conn, client, NOW, days=75, max_api=40, daily_cap=300, sleep=lambda s: None)
    # на сутки осталось 3: страница, метаданные, комментарии одного лота
    assert client.calls == ["/gallery/FOLD-1", "/deviation/metadata", "comments:UUID-1"]
    assert backfill._used_today(conn, NOW) == 300
    client2 = FakeClient({"FOLD-1": folder(1, 30)})
    assert backfill.step(conn, client2, NOW + 60, daily_cap=300, sleep=lambda s: None) == []
    assert client2.calls == []
    # на следующие сутки — снова можно
    client3 = FakeClient({"FOLD-1": folder(1, 30)})
    backfill.step(conn, client3, NOW + DAY, daily_cap=300, sleep=lambda s: None)
    assert client3.calls


def test_step_stops_on_block_and_keeps_cursor(conn):
    enqueue_one(conn)
    block = da_access.Block("api", "budget", NOW + 600, 90)
    client = FakeClient({"FOLD-1": folder(1, 50)}, fail_at=1, fail=Throttled(block))
    lines = backfill.step(conn, client, NOW, sleep=lambda s: None)
    assert len(client.calls) == 1
    st = conn.execute("SELECT * FROM backfill_state").fetchone()
    assert st["offset"] == 0 and st["done"] == 0
    assert any("лимит" in line for line in lines)


def test_step_stops_on_cooldown_without_requests(conn):
    enqueue_one(conn)
    conn.execute("INSERT INTO da_cooldown (kind, until_ts, level, since_ts, status) VALUES ('api', ?, 1, ?, 429)",
                 (NOW + 1800, NOW))
    conn.commit()
    client = FakeClient({"FOLD-1": folder(1, 10)})
    lines = backfill.step(conn, client, NOW, sleep=lambda s: None)
    assert client.calls == [] and "притормозил" in lines[0]


def test_step_stops_on_needs_reconnect(conn):
    enqueue_one(conn)
    client = FakeClient({"FOLD-1": folder(1, 10)}, fail_at=1, fail=NeedsReconnect("нет прав"))
    lines = backfill.step(conn, client, NOW, sleep=lambda s: None)
    assert len(client.calls) == 1 and any("подключить" in line for line in lines)
    assert conn.execute("SELECT done FROM backfill_state").fetchone()["done"] == 0


# --- что читаем ---

def test_skips_open_and_known_lots_and_parses_bids(conn):
    enqueue_one(conn)
    lots = [dev(1, 3, title="Dragon adopt auction #1 [OPEN] SB $10"),       # открыт и молодой — нет
            dev(2, 20, title="Dragon adopt auction #2 SB $10"),               # старше 14 дней — итог
            dev(3, 5, title="Dragon adopt auction #3 [CLOSED] SB $10"),       # закрыт по названию
            dev(4, 25, title="Dragon art commission sheet"),                  # не лот
            dev(5, 30, title="Dragon adopt auction #5 [CLOSED] SB $10")]      # итог уже известен
    db.upsert_item(conn, db.Item(id="da:5", source="deviantart", title="Dragon adopt auction #5 [CLOSED] SB $10",
                                 url="u", author="seller", lot_type="auction", status="closed",
                                 published_ts=NOW - 30 * DAY), NOW - 20 * DAY)
    conn.execute("INSERT INTO auctions (deviation_id, n_bids, ended, updated_ts, extra) VALUES ('da:5', 1, 1, ?, '{}')",
                 (NOW - 20 * DAY,))
    conn.commit()
    client = FakeClient({"FOLD-1": lots})
    backfill.step(conn, client, NOW, sleep=lambda s: None)
    read = sorted(c for c in client.calls if c.startswith("comments:"))
    assert read == ["comments:UUID-2", "comments:UUID-3"]
    rows = {r["deviation_id"]: r for r in conn.execute("SELECT * FROM auctions")}
    assert rows["da:2"]["n_bids"] == 2 and rows["da:2"]["top_amount"] == 15
    bidders = json.loads(rows["da:2"]["extra"])["bidders"]
    assert {b["user"] for b in bidders} == {"buyer1", "buyer2"}
    item = db.get_item(conn, "da:2")
    assert item["place"].startswith("dragonadopts") and item["own"] == 0 and item["lot_type"] == "auction"
    assert "backfill" in json.loads(item["extra"])["via"]
    assert db.get_item(conn, "da:1") is None and db.get_item(conn, "da:4") is None
    st = conn.execute("SELECT * FROM backfill_state").fetchone()
    assert st["lots"] == 2 and st["bids_read"] == 2


def test_zero_comments_old_lot_saved_without_reading(conn):
    enqueue_one(conn)
    client = FakeClient({"FOLD-1": [dev(7, 20, comments=0)]})
    backfill.step(conn, client, NOW, sleep=lambda s: None)
    assert not any(c.startswith("comments:") for c in client.calls)
    extra = json.loads(conn.execute("SELECT extra FROM auctions WHERE deviation_id = 'da:7'").fetchone()["extra"])
    assert extra.get("zero_by_stats") is True


def test_own_gallery_and_unknown_folder_resolved(conn):
    backfill.enqueue(conn, ["dragonadopts"], "Me", 75, NOW)
    client = FakeClient({"all": [dev(10, 20, author="Me")], "FOLD-1": [dev(11, 20)]})
    backfill.step(conn, client, NOW, sleep=lambda s: None)
    assert client.calls[0] == "/gallery/all"  # её галерея — первой
    assert "folders:dragonadopts" in client.calls
    assert db.get_item(conn, "da:10")["own"] == 1
    assert db.get_item(conn, "da:11")["own"] == 0
    assert conn.execute("SELECT folderid FROM backfill_state WHERE key = 'group:dragonadopts/?'"
                        ).fetchone()["folderid"] == "FOLD-1"
    assert "post_comment" not in client.calls and "watch" not in client.calls


# --- прогресс ---

def test_progress_empty_and_partial(conn):
    assert backfill.progress(conn, NOW) == ""
    enqueue_one(conn)
    backfill.enqueue(conn, None, "me", 75, NOW, cfg=Config(deviantart=DeviantArt(my_username="me")))
    client = FakeClient({"FOLD-1": folder(1, 30, step_days=1.0), "all": []})
    backfill.step(conn, client, NOW, days=75, max_api=12, daily_cap=1000, sleep=lambda s: None)
    line = backfill.progress(conn, NOW)
    assert line.startswith("история рынка")
    assert "групп" in line and "закрытых аукционов" in line and "%" in line and "назад до" in line


def test_progress_done(conn):
    enqueue_one(conn)
    client = FakeClient({"FOLD-1": folder(1, 5)})
    backfill.step(conn, client, NOW, sleep=lambda s: None)
    line = backfill.progress(conn, NOW)
    assert line.startswith("история рынка прочитана") and "1/1 групп" in line and "5 закрытых" in line


# --- регрессии: повторные проходы, настоящий счёт, край окна, фильтр mature ---

class EndsIn48h(FakeClient):
    """Лоты ещё с [OPEN] в названии, но в описании «ends in 48 hours» — закончились по сроку."""

    def _request(self, method, path, params=None, **kw):
        if path == "/deviation/metadata":
            self._hit(path)
            ids = [v for k, v in params if k == "deviationids[]"]
            return {"metadata": [{"deviationid": u, "description": "SB $10. Auction ends in 48 hours.",
                                  "tags": [{"tag_name": "dragon"}]} for u in ids]}
        return super()._request(method, path, params, **kw)


def test_closed_by_date_lots_read_once_and_counted_once(conn):
    enqueue_one(conn)
    lots = [dev(i, 8 + i * 0.1, title=f"Dragon adopt auction #{i} [OPEN] SB $10") for i in range(24)]
    reads: list[str] = []
    for run in range(4):
        client = EndsIn48h({"FOLD-1": lots})
        backfill.step(conn, client, NOW + run * 3600, days=75, max_api=20, daily_cap=10000, sleep=lambda s: None)
        reads += [c for c in client.calls if c.startswith("comments:")]
    assert len(reads) == len(set(reads)) == 24          # каждый лот — один раз
    st = conn.execute("SELECT * FROM backfill_state").fetchone()
    assert st["lots"] == 24 and st["done"] == 1
    assert "24 закрытых" in backfill.progress(conn, NOW)
    # прогноз согласен, что итог известен: срок из описания записан, ended=1
    a = conn.execute("SELECT ended, extra FROM auctions WHERE deviation_id = 'da:0'").fetchone()
    assert a["ended"] == 1 and json.loads(a["extra"])["ended_by"] == "date"
    assert backfill._known_outcome(conn, "da:0")       # тот же forecast.outcome_known, что у прогноза


class DeepReplies(FakeClient):
    """Настоящий клиент тратит больше оценки: ответвления комментариев — отдельные запросы."""

    def __init__(self, galleries, extra_per_lot: int):
        super().__init__(galleries)
        self.extra = extra_per_lot
        self.http = 0

    def _req(self):
        self.http += 1
        da_access.after(da_access.API, 200)

    def _request(self, method, path, params=None, **kw):
        self._req()
        return super()._request(method, path, params, **kw)

    def deviation_comments(self, uuid, max_comments=500):
        for _ in range(1 + self.extra):
            self._req()
        return super().deviation_comments(uuid, max_comments)


def test_budget_counts_real_requests_not_estimate(conn):
    enqueue_one(conn)
    client = DeepReplies({"FOLD-1": folder(1, 6, step_days=1.0)}, extra_per_lot=6)
    lines = backfill.step(conn, client, NOW, days=75, max_api=20, daily_cap=1000, sleep=lambda s: None)
    used = backfill._used_today(conn, NOW)
    assert used == client.http                     # сутки считают настоящие запросы
    assert used <= 20 + 7                           # перерасход — не больше одного лота
    assert any(f"запросов API {used:g}" in line for line in lines)
    reads = [c for c in client.calls if c.startswith("comments:")]
    assert 1 <= len(reads) < 6                     # оценка (1 на лот) пустила бы все шесть


def test_one_old_resubmitted_lot_does_not_end_target(conn):
    enqueue_one(conn)
    lots = folder(1, 60, step_days=0.5)            # 15…45 дней
    lots.insert(3, dev(999, 400))                   # старый лот, заново выложенный в группу
    client = FakeClient({"FOLD-1": lots})
    backfill.step(conn, client, NOW, days=75, max_api=500, daily_cap=1000, sleep=lambda s: None)
    st = conn.execute("SELECT * FROM backfill_state").fetchone()
    assert st["lots"] == 60 and st["done"] == 1
    assert db.get_item(conn, "da:999") is None


class Mature(FakeClient):
    """DA отсеивает mature на своей стороне: результатов меньше limit, next_offset верный."""

    def _request(self, method, path, params=None, **kw):
        if path.startswith("/gallery/"):
            self._hit(path)
            devs = self.galleries[path.removeprefix("/gallery/")]
            off, lim = int(params["offset"]), int(params["limit"])
            page = [d for d in devs[off:off + lim] if not d.get("is_mature")]
            return {"results": page, "has_more": off + lim < len(devs), "next_offset": off + lim}
        return super()._request(method, path, params, **kw)


def test_mature_filtered_page_is_not_the_end(conn):
    enqueue_one(conn)
    lots = folder(1, 48, step_days=0.5)
    for d in lots[:24]:
        d["is_mature"] = True                       # вся первая страница отсеяна
    client = Mature({"FOLD-1": lots})
    backfill.step(conn, client, NOW, days=75, max_api=500, daily_cap=1000, sleep=lambda s: None)
    st = conn.execute("SELECT * FROM backfill_state").fetchone()
    assert st["lots"] == 24 and st["offset"] == 48 and st["done"] == 1


def test_partially_filtered_pages_use_next_offset(conn):
    enqueue_one(conn)
    lots = folder(1, 72, step_days=0.5)
    for i in range(0, 72, 4):
        lots[i]["is_mature"] = True
    client = Mature({"FOLD-1": lots})
    backfill.step(conn, client, NOW, days=75, max_api=500, daily_cap=1000, sleep=lambda s: None)
    assert [c for c in client.calls if c.startswith("/gallery")] == ["/gallery/FOLD-1"] * 3
    assert conn.execute("SELECT \"offset\" FROM backfill_state").fetchone()["offset"] == 72


def test_larger_window_rereads_edge_page(conn):
    enqueue_one(conn, days=60)
    lots = folder(1, 48, step_days=2.0, start_age=15.0)   # 15…109 дней
    backfill.step(conn, FakeClient({"FOLD-1": lots}), NOW, days=60, max_api=500, daily_cap=1000,
                  sleep=lambda s: None)
    first = conn.execute("SELECT lots FROM backfill_state").fetchone()["lots"]
    assert first == 23                                    # 15…59 дней
    backfill.enqueue(conn, None, "", 90, NOW, cfg=Config())
    client = FakeClient({"FOLD-1": lots})
    backfill.step(conn, client, NOW, days=90, max_api=500, daily_cap=1000, sleep=lambda s: None)
    st = conn.execute("SELECT * FROM backfill_state").fetchone()
    assert st["lots"] == 38 and st["done"] == 1           # 15…89 дней: ничего между краями не пропало
    reread = [c for c in client.calls if c.startswith("comments:")]
    assert len(reread) == 15                              # разобранные раньше не перечитаны


def test_quiet_when_blocked_and_nothing_read(conn):
    enqueue_one(conn)
    conn.execute("INSERT INTO da_cooldown (kind, until_ts, level, since_ts, status) VALUES ('api', ?, 1, ?, 429)",
                 (NOW + 1800, NOW))
    conn.commit()
    assert backfill.step(conn, FakeClient({"FOLD-1": folder(1, 5)}), NOW, sleep=lambda s: None, quiet=True) == []


def test_her_time_zone_used_for_end_dates(conn, monkeypatch):
    enqueue_one(conn)
    seen: list[str] = []
    real = backfill.parse_end

    def spy(text, ts, last, tz="", now=None):
        seen.append(tz)
        return real(text, ts, last, tz=tz, now=now)

    monkeypatch.setattr(backfill, "parse_end", spy)
    cfg = Config(posting=Posting(timezone="America/New_York"))
    client = FakeClient({"FOLD-1": [dev(1, 3, title="Dragon adopt auction #1 [OPEN] SB $10")]})
    backfill.step(conn, client, NOW, sleep=lambda s: None, cfg=cfg)
    assert seen == ["America/New_York"]


def test_groups_added_later_when_scorecard_appears(conn):
    cfg = Config(deviantart=DeviantArt(my_username="me"))
    assert backfill.enqueue(conn, None, "me", 75, NOW, cfg=cfg) == ["own:me"]   # при подключении карточек нет
    conn.execute("INSERT INTO group_scores (grp, folder, size, lots_per_day, niche_share, n, ts) "
                 "VALUES ('dragonadopts', '555', 100, 3.0, 0.5, 24, ?)", (NOW,))
    conn.commit()
    client = FakeClient({"all": [], "FOLD-1": []})
    backfill.step(conn, client, NOW + 600, sleep=lambda s: None, cfg=cfg)
    assert conn.execute("SELECT COUNT(*) FROM backfill_state WHERE key LIKE 'group:%'").fetchone()[0] == 0
    backfill.step(conn, client, NOW + backfill.REENQUEUE_S + 1, sleep=lambda s: None, cfg=cfg)
    keys = {r["key"] for r in conn.execute("SELECT key FROM backfill_state")}
    assert "group:dragonadopts/555" in keys


# --- регрессии ревью 0.5 ---

class MetaFailClient(FakeClient):
    """Метаданные падают (503), пока fail_meta = True."""

    fail_meta = True

    def _request(self, method, path, params=None, **kw):
        if path == "/deviation/metadata" and self.fail_meta:
            from mcp_devian.da_api import DAApiError

            self.calls.append(path)
            raise DAApiError("server error", "http_503", status=503)
        return super()._request(method, path, params, **kw)


def test_failed_metadata_does_not_lose_lots(conn):
    """Регрессия: метаданные не пришли (сеть/5xx) — лоты, у которых аукцион виден только в описании,
    навсегда помечались «не лот», курсор уходил дальше."""
    enqueue_one(conn)
    lots = [dev(i, 20 + i, title=f"Dragon design #{i}") for i in range(1, 6)]  # «auction» только в описании
    client = MetaFailClient({"FOLD-1": lots})
    lines = backfill.step(conn, client, NOW, sleep=lambda s: None)
    assert any("описания" in line for line in lines)
    st = conn.execute("SELECT * FROM backfill_state").fetchone()
    assert st["offset"] == 0 and st["done"] == 0 and "описания" in st["last_error"]
    assert conn.execute("SELECT COUNT(*) FROM backfill_seen").fetchone()[0] == 0
    client.fail_meta = False
    backfill.step(conn, client, NOW + 3600, sleep=lambda s: None)
    assert conn.execute("SELECT COUNT(*) FROM auctions").fetchone()[0] == 5


def test_no_write_lock_held_during_pauses(conn, tmp_path):
    """Регрессия: проход держал запись в базе открытой во время пауз 1,5 с и запросов — чат (черновики,
    «го», продажи) ждал 30 с и падал с «database is locked»."""
    import sqlite3

    enqueue_one(conn)
    other = sqlite3.connect(tmp_path / "m.sqlite3", timeout=0.05)
    locked = []

    def sleep(_s):
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        except sqlite3.OperationalError:
            locked.append(1)

    backfill.step(conn, FakeClient({"FOLD-1": folder(1, 10)}), NOW, sleep=sleep)
    other.close()
    assert not locked


def test_backfilled_lots_are_marked_in_basis_and_health(conn):
    """Регрессия: лоты истории рынка (дочитаны через 60–90 дней, удалённые туда не попали) шли в прогноз,
    ниши и палитры без пометки."""
    from mcp_devian import evidence, forecast

    enqueue_one(conn)
    backfill.step(conn, FakeClient({"FOLD-1": folder(1, 10)}), NOW, sleep=lambda s: None)
    outs = [o for o in forecast.load_outcomes(conn, NOW) if not o.own]
    assert outs and all(o.backfill for o in outs)
    line = forecast._basis_line(len(outs), NOW, NOW, sum(o.backfill for o in outs))
    assert "задним числом" in line and "удалили" in line
    assert "задним числом (история рынка) " in evidence.render_health(conn, NOW)
