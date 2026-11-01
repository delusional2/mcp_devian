"""Публикация 0.4: папки её галереи, партии работ с паузами и отправка по расписанию."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_devian import da_api, db, drafts, posting
from mcp_devian.config import Config, Toyhouse
from mcp_devian.db import Item

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=MSK).timestamp()  # воскресенье, полдень по Москве
HOUR = 3600
GROUP = {"group": "da-adopts", "group_id": "g-1", "folder_id": "f-1", "folder_name": "auctions"}


class FakeDA:
    def __init__(self, folders=None, username="mothy"):
        self.folders = folders if folders is not None else [
            {"folder_id": "F-feat", "name": "Featured", "parent": None},
            {"folder_id": "F-open", "name": "Adopts — Open", "parent": None},
            {"folder_id": "F-sold", "name": "Adopts  Sold", "parent": "F-open"},
        ]
        self.username = username
        self.asked_folders_for = []
        self.whoami_calls = 0
        self.published = []

    def is_connected(self):
        return True

    def whoami(self):
        self.whoami_calls += 1
        return {"username": self.username, "userid": "1"}

    def gallery_folders(self, username):
        self.asked_folders_for.append(username)
        return self.folders

    def stash_submit(self, file, title, description, tags):
        return 42

    def stash_publish(self, itemid, **kw):
        self.published.append(kw)
        return {"url": f"https://www.deviantart.com/mothy/art/x-{len(self.published)}", "deviationid": "u"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()


@pytest.fixture
def fake(monkeypatch):
    f = FakeDA()
    monkeypatch.setattr(posting, "da_client", lambda c: f)
    return f


@pytest.fixture
def cfg():
    c = Config(toyhouse=Toyhouse(mode="chrome"))
    c.posting.da_client_id = "123"
    c.posting.group_presets = {"a": [dict(GROUP)]}
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.sqlite3")
    yield c
    c.close()


def image(tmp_path, name="moth.png", data=b"\x89PNG"):
    p = tmp_path / "Downloads" / name
    p.write_bytes(data + name.encode())
    return p


def batch_of(conn, draft_id):
    return json.loads(drafts.get(conn, draft_id)["payload"])["batch_id"]


def item(tmp_path, n, **kw):
    return {"file_path": str(image(tmp_path, f"m{n}.png")), "title": f"Moth {n}", "tags": ["moth"], **kw}


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


# --- Папки галереи ---

def test_gallery_folders_resolved_and_sent(conn, cfg, fake, tmp_path):
    d, preview = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW,
                                            gallery_folders=["  adopts —   OPEN ", "featured"])
    assert d and "В галерею: Adopts — Open, Featured" in preview
    assert fake.asked_folders_for == ["mothy"] and fake.whoami_calls == 1  # ник не указан — спросили у DA
    assert "опубликовано" in posting.send(conn, cfg, d, now=NOW)
    assert fake.published[0]["galleryids"] == ["F-open", "F-feat"]


def test_gallery_folders_use_her_username_from_settings(conn, cfg, fake, tmp_path):
    cfg.deviantart.my_username = "MothyArt"
    posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW, gallery_folders=["adopts sold"])
    assert fake.asked_folders_for == ["MothyArt"] and fake.whoami_calls == 0


def test_unknown_gallery_folder_lists_what_she_has(conn, cfg, fake, tmp_path):
    d, text = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW, gallery_folders=["YCH"])
    assert d is None and "нет папки «YCH»" in text and "Adopts — Open" in text and "Featured" in text


def test_default_gallery_folders_from_settings(conn, cfg, fake, tmp_path):
    cfg.posting.default_gallery_folders = ["Adopts — Open"]
    _, preview = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    assert "В галерею: Adopts — Open" in preview
    # Явный пустой список — только в общую галерею, без папок.
    d, preview = posting.prepare_da_publish(conn, cfg, str(image(tmp_path, "b.png")), "Moth", now=NOW,
                                            gallery_folders=[])
    assert "В галерею" not in preview
    posting.send(conn, cfg, d, now=NOW)
    assert fake.published[-1]["galleryids"] == ()


def test_gallery_folders_need_deviantart_connected(conn, cfg, tmp_path, monkeypatch):
    class Offline(FakeDA):
        def is_connected(self):
            return False

    monkeypatch.setattr(posting, "da_client", lambda c: Offline())
    d, text = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW, gallery_folders=["x"])
    assert d is None and "da_connect" in text


def test_gallery_folders_api_error_is_explained(conn, cfg, tmp_path, monkeypatch):
    class Broken(FakeDA):
        def gallery_folders(self, username):
            raise da_api.DAApiError("нет связи с DeviantArt")

    monkeypatch.setattr(posting, "da_client", lambda c: Broken())
    d, text = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW, gallery_folders=["x"])
    assert d is None and "папки её галереи" in text


# --- Партии ---

def test_batch_one_preview_group_limit_across_batch(conn, cfg, fake, tmp_path):
    items = [item(tmp_path, n, preset="a", gallery_folders=["Adopts — Open"]) for n in range(1, 5)]
    ids, preview = posting.prepare_da_batch(conn, cfg, items, now=NOW)
    assert len(ids) == 4 and len(set(ids)) == 4
    assert fake.asked_folders_for == ["mothy"]  # папки читаем один раз на партию
    for n, d in enumerate(ids, 1):
        assert f"{n}) черновик {d}" in preview
    payloads = [drafts.get(conn, d) for d in ids]
    loaded = [json.loads(r["payload"]) for r in payloads]
    batch_id = loaded[0]["batch_id"]
    assert all(p["batch_id"] == batch_id for p in loaded) and [p["batch_index"] for p in loaded] == [1, 2, 3, 4]
    assert posting.is_batch_id(batch_id) and batch_id in preview
    # Лимит 3 в сутки на группу: четвёртая работа в группу уже не идёт, и превью это говорит.
    assert [len(p["groups"]) for p in loaded] == [1, 1, 1, 0]
    assert preview.count("пропущу da-adopts") == 1 and "в этой партии" in preview
    assert "паузой 1–2 минуты" in preview and preview.count("DeviantArt не сообщает") == 1


def test_batch_counts_what_was_already_sent_today(conn, cfg, fake, tmp_path):
    for _ in range(2):
        drafts.log_sent(conn, "da_group", "da-adopts", "old", True, now=NOW - HOUR)
    ids, preview = posting.prepare_da_batch(conn, cfg, [item(tmp_path, n, preset="a") for n in (1, 2)], now=NOW)
    assert [len(json.loads(drafts.get(conn, d)["payload"])["groups"]) for d in ids] == [1, 0]


def test_batch_with_bad_item_creates_nothing(conn, cfg, fake, tmp_path):
    items = [item(tmp_path, 1), {"file_path": str(image(tmp_path, "x.png")), "title": "x" * 51}]
    ids, text = posting.prepare_da_batch(conn, cfg, items, now=NOW)
    assert ids == [] and "Работа 2" in text and "50" in text
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


def test_batch_size_limited(conn, cfg, fake, tmp_path):
    ids, text = posting.prepare_da_batch(conn, cfg, [item(tmp_path, n) for n in range(posting.BATCH_MAX + 1)], now=NOW)
    assert ids == [] and "не больше" in text


def test_batch_sent_in_order_with_pauses(conn, cfg, fake, tmp_path):
    ids, _ = posting.prepare_da_batch(conn, cfg, [item(tmp_path, n, preset="a") for n in (1, 2, 3)], now=NOW)
    batch_id = batch_of(conn, ids[0])
    sleeps = Sleeps()
    results = posting.send_batch(conn, cfg, batch_id, now=NOW, sleep=sleeps)
    assert [r["draft_id"] for r in results] == ids and all(r["status"] == "sent" for r in results)
    assert len(fake.published) == 3
    # Пауза перед каждой работой, кроме первой: 60–120 секунд с разбросом.
    assert len(sleeps) == 2 and all(60 <= s <= 120 for s in sleeps)
    assert drafts.count_sent(conn, "da_group", NOW - 1, target="da-adopts") == 3


def test_batch_send_any_and_cancel_midway(conn, cfg, fake, tmp_path):
    ids, _ = posting.prepare_da_batch(conn, cfg, [item(tmp_path, n) for n in (1, 2, 3)], now=NOW)
    batch_id = batch_of(conn, ids[0])

    def sleep_and_she_cancels(s):
        posting.cancel_any(conn, batch_id)  # пока шла пауза, она передумала насчёт остальных

    text = posting.send_any(conn, cfg, batch_id, now=NOW, sleep=sleep_and_she_cancels)
    assert len(fake.published) == 1 and "опубликовано" in text
    assert [drafts.get(conn, d)["status"] for d in ids] == ["sent", "cancelled", "cancelled"]
    assert "нечего отправлять" in posting.send_any(conn, cfg, batch_id, now=NOW)


# --- Расписание ---

def test_parse_when_in_her_timezone():
    tz = "Europe/Moscow"
    at = lambda *a: datetime(*a, tzinfo=MSK).timestamp()  # noqa: E731
    assert posting.parse_when("2026-11-03 19:00", tz, NOW) == at(2026, 11, 3, 19, 0)
    assert posting.parse_when("03.11 19:00", tz, NOW) == at(2026, 11, 3, 19, 0)
    assert posting.parse_when("03.11.2026 19:00", tz, NOW) == at(2026, 11, 3, 19, 0)
    assert posting.parse_when("завтра 19:00", tz, NOW) == at(2026, 11, 2, 19, 0)
    assert posting.parse_when("19:00", tz, NOW) == at(2026, 11, 1, 19, 0)
    assert posting.parse_when("11:00", tz, NOW) == at(2026, 11, 2, 11, 0)  # уже прошло — значит, завтра
    assert posting.parse_when("2026-11-03T16:00+00:00", tz, NOW) == at(2026, 11, 3, 19, 0)
    assert posting.parse_when("сейчас", tz, NOW) == NOW
    assert posting.fmt_when(at(2026, 11, 3, 19, 0), tz) == "03.11 19:00 MSK"
    with pytest.raises(ValueError, match="Не поняла"):
        posting.parse_when("когда-нибудь", tz, NOW)


def test_scheduled_draft_waits_then_goes(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", preset="a", now=NOW)
    msg = posting.approve_draft(conn, cfg, d, "завтра 19:00", now=NOW)
    when = datetime(2026, 11, 2, 19, 0, tzinfo=MSK).timestamp()
    assert "Запланировано" in msg and "02.11 19:00 MSK" in msg
    row = drafts.get(conn, d, now=NOW)
    assert row["status"] == "scheduled" and row["send_at"] == when
    listed = [posting.describe(r) for r in posting.list_open(conn, now=NOW)]
    assert any("запланирован на 02.11 19:00 MSK" in line for line in listed)
    # Раньше времени не уходит: ни фоновой проверкой, ни прямой отправкой.
    assert posting.send_due(conn, cfg, now=when - 60, sleep=Sleeps()) == []
    assert "запланирован на потом" in posting.send(conn, cfg, d, now=when - 60)
    assert fake.published == []
    # Время пришло — уходит (позже суток от черновика, это нормально для расписания).
    results = posting.send_due(conn, cfg, now=when + 30, sleep=Sleeps())
    assert [(r["draft_id"], r["status"]) for r in results] == [(d, "sent")]
    assert len(fake.published) == 1 and posting.send_due(conn, cfg, now=when + 60, sleep=Sleeps()) == []


def test_schedule_set_when_preparing_is_used_on_go(conn, cfg, fake, tmp_path):
    d, preview = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW, schedule="03.11 19:00")
    assert "Когда: 03.11 19:00 MSK" in preview
    posting.approve_draft(conn, cfg, d, now=NOW)
    assert drafts.get(conn, d, now=NOW)["send_at"] == datetime(2026, 11, 3, 19, 0, tzinfo=MSK).timestamp()


def test_scheduled_batch_goes_with_pauses(conn, cfg, fake, tmp_path):
    ids, preview = posting.prepare_da_batch(conn, cfg, [item(tmp_path, n) for n in (1, 2)], schedule="20:00",
                                            now=NOW)
    batch_id = batch_of(conn, ids[0])
    assert "Когда: 01.11 20:00 MSK" in preview
    assert "2 из 2 работ партии" in posting.approve_draft(conn, cfg, batch_id, now=NOW)
    sleeps = Sleeps()
    results = posting.send_due(conn, cfg, now=NOW + 9 * HOUR, sleep=sleeps)
    assert [r["draft_id"] for r in results] == ids and len(sleeps) == 1 and 60 <= sleeps[0] <= 120


def test_scheduled_limits_rechecked_at_send_time(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", preset="a", now=NOW)
    posting.approve_draft(conn, cfg, d, "20:00", now=NOW)
    for _ in range(cfg.posting.da_submissions_per_group_per_day):  # группу заполнили вручную днём
        drafts.log_sent(conn, "da_group", "da-adopts", "manual", True, now=NOW + HOUR)
    results = posting.send_due(conn, cfg, now=NOW + 9 * HOUR, sleep=Sleeps())
    assert results[0]["status"] == "failed" and "не отправила" in results[0]["result"]
    assert fake.published == []


def test_scheduled_older_than_a_week_is_cancelled(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    posting.approve_draft(conn, cfg, d, "20:00", now=NOW)
    later = NOW + drafts.SCHEDULE_MAX_AGE_S + HOUR  # мак был выключен всю неделю
    results = posting.send_due(conn, cfg, now=later, sleep=Sleeps())
    assert results == [{"draft_id": d, "status": "cancelled", "result": drafts.EXPIRED_SCHEDULE}]
    assert drafts.get(conn, d, now=later)["status"] == "cancelled" and fake.published == []


def test_schedule_not_beyond_a_week(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    assert "на неделю вперёд" in posting.approve_draft(conn, cfg, d, "20.11 19:00", now=NOW)
    assert drafts.get(conn, d, now=NOW)["status"] == "pending"
    d2, text = posting.prepare_da_publish(conn, cfg, str(image(tmp_path, "b.png")), "Moth", now=NOW,
                                          schedule="20.11 19:00")
    assert d2 is None and "неделю" in text


def test_cancel_and_reschedule_scheduled(conn, cfg, fake, tmp_path):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    posting.approve_draft(conn, cfg, d, "20:00", now=NOW)
    posting.approve_draft(conn, cfg, d, "21:00", now=NOW)  # перенесла
    assert drafts.get(conn, d, now=NOW)["send_at"] == datetime(2026, 11, 1, 21, 0, tzinfo=MSK).timestamp()
    assert drafts.cancel(conn, d)
    assert posting.send_due(conn, cfg, now=NOW + 12 * HOUR, sleep=Sleeps()) == [] and fake.published == []


def test_toyhouse_replies_are_not_scheduled(conn, cfg, tmp_path):
    db.upsert_item(conn, Item(id="th:1", source="toyhouse", title="LF dragons", url="https://toyhou.se/~forums/1.t",
                              wants_characters=True, matched=True, match_kind="lf", activity_ts=NOW), now=NOW)
    cfg.posting.reply_templates = {"основной": "Привет! {name} — {url}, {price}. {note}"}
    cfg.posting.characters = [{"name": "Луна", "url": "https://toyhou.se/123.luna", "price": "$30", "tags": []}]
    d, _ = posting.prepare_toyhouse_reply(conn, cfg, "th:1", "Луна", now=NOW)
    assert "только сразу" in posting.approve_draft(conn, cfg, d, "20:00", now=NOW)
    assert drafts.get(conn, d, now=NOW)["status"] == "pending"


def test_send_due_is_off_while_sending_is_off(conn, cfg, fake, tmp_path, monkeypatch):
    d, _ = posting.prepare_da_publish(conn, cfg, str(image(tmp_path)), "Moth", now=NOW)
    posting.approve_draft(conn, cfg, d, now=NOW)
    monkeypatch.setattr(posting, "SENDING_ENABLED", False)
    assert posting.send_due(conn, cfg, now=NOW + 60, sleep=Sleeps()) == [] and fake.published == []


def test_old_database_gets_send_at_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE drafts (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, "
                "created_ts REAL NOT NULL, payload TEXT NOT NULL, preview TEXT NOT NULL, result TEXT, sent_ts REAL)")
    raw.execute("INSERT INTO drafts VALUES ('abcd1234', 'da_publish', 'pending', ?, '{}', 'p', NULL, NULL)", (NOW,))
    raw.commit()
    raw.close()
    with db.session(path) as c:
        assert drafts.get(c, "abcd1234", now=NOW)["send_at"] is None
        drafts.ensure_schema(c)  # второй раз — без ошибок
