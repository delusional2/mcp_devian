"""Публикация без двойных постов и без обхода лимитов: замки, атомарный захват, неизвестный исход."""

import threading
import time

import pytest

from mcp_devian import da_api, db, drafts, posting
from mcp_devian.config import Config, Toyhouse
from mcp_devian.db import Item

NOW = time.time()
TEMPLATE = "Привет! Могу предложить {name} — {url}, цена {price}. {note}"
CHARS = [{"name": "Луна", "url": "https://toyhou.se/123.luna", "price": "$30", "tags": ["dragon"]},
         {"name": "Сол", "url": "https://toyhou.se/124.sol", "price": "$25", "tags": ["dragon"]}]


@pytest.fixture(autouse=True)
def sending_on(monkeypatch):
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "s.sqlite3"
    with db.session(path) as c:
        for i in (1, 2):
            db.upsert_item(c, Item(id=f"th:{i}", source="toyhouse", title=f"LF dragons {i}",
                                   url=f"https://toyhou.se/~forums/13.x/{i}.t", wants_characters=True,
                                   matched=True, match_kind="lf", activity_ts=NOW), now=NOW)
    return path


@pytest.fixture
def cfg():
    c = Config(toyhouse=Toyhouse(mode="chrome"))
    c.posting.reply_templates = {"основной": TEMPLATE}
    c.posting.characters = [dict(x) for x in CHARS]
    c.posting.th_username = "mothy"
    c.posting.th_auto_submit = True
    c.posting.th_min_gap_minutes = 0  # тут проверяем именно гонки, а не паузу
    return c


def slow_reply(calls, delay=0.4, state="posted"):
    lock = threading.Lock()

    def run(thread_url, html, submit, username, marker, **kw):
        with lock:
            calls.append(thread_url)
        time.sleep(delay)
        return {"state": state, "permalink": thread_url + "#p"}
    return run


def run_parallel(fns):
    out = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def go(i, fn):
        barrier.wait()
        out[i] = fn()

    threads = [threading.Thread(target=go, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return out


def test_two_drafts_same_thread_sent_at_once_only_one_posts(db_path, cfg, monkeypatch):
    from mcp_devian import her_chrome

    calls = []
    monkeypatch.setattr(her_chrome, "run_reply", slow_reply(calls))
    with db.session(db_path) as c:
        d1, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")
        d2, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Сол")

    def sender(d):
        def f():
            with db.session(db_path) as c:
                return posting.send(c, cfg, d)
        return f

    results = run_parallel([sender(d1), sender(d2)])
    assert len(calls) == 1
    assert sum("опубликован" in r for r in results) == 1
    assert any("в этот тред уже отвечали" in r for r in results)


def test_same_draft_twice_at_once_posts_once(db_path, cfg, monkeypatch):
    from mcp_devian import her_chrome

    calls = []
    monkeypatch.setattr(her_chrome, "run_reply", slow_reply(calls))
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")

    def f():
        with db.session(db_path) as c:
            return posting.send(c, cfg, d)

    results = run_parallel([f, f])
    assert len(calls) == 1 and sum("опубликован" in r for r in results) == 1


def test_claim_is_atomic_across_connections(db_path):
    with db.session(db_path) as a, db.session(db_path) as b:
        d = drafts.create(a, "th_reply", {"x": 1}, "p")
        assert drafts.take_for_sending(a, d, "th_reply") == {"x": 1}
        with pytest.raises(drafts.DraftError):
            drafts.take_for_sending(b, d, "th_reply")


def test_prepare_counts_drafts_in_flight(db_path, cfg):
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")
        drafts.take_for_sending(c, d, "th_reply")  # сейчас отправляется
        blocked = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Сол")
        assert blocked[0] is None and "в этот тред" in blocked[1]


def test_definite_no_post_releases_limit_slot(db_path, cfg, monkeypatch):
    from mcp_devian import her_chrome

    monkeypatch.setattr(her_chrome, "run_reply", slow_reply([], 0, state="no_form"))
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")
        assert "форму" in posting.send(c, cfg, d)
        assert drafts.count_sent(c, "th_reply", 0) == 0
        assert posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")[0]  # можно попробовать снова


def test_no_submit_button_keeps_limit_slot(db_path, cfg, monkeypatch):
    """Кнопки нет, но текст в форме: если она нажмёт «Post» сама, второй отклик в тред не должен пройти."""
    from mcp_devian import her_chrome

    monkeypatch.setattr(her_chrome, "run_reply", slow_reply([], 0, state="no_submit_button"))
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")
        out = posting.send(c, cfg, d)
        assert "вручную" in out
        assert drafts.get(c, d)["status"] == "failed" and drafts.count_sent(c, "th_reply", 0) == 1
        blocked = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Сол")
        assert blocked[0] is None and "отвечала в этом треде" in blocked[1]


def test_unknown_state_from_chrome_is_counted(db_path, cfg, monkeypatch):
    from mcp_devian import her_chrome

    monkeypatch.setattr(her_chrome, "run_reply", slow_reply([], 0, state="unknown"))
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")
        assert "неизвестно" in posting.send(c, cfg, d)
        assert drafts.get(c, d)["status"] == "unknown" and drafts.count_sent(c, "th_reply", 0) == 1


def test_setup_error_releases_slot(db_path, cfg, monkeypatch):
    from mcp_devian import her_chrome

    def no_js(*a, **k):
        raise her_chrome.ChromeSetupError("включи галочку")

    monkeypatch.setattr(her_chrome, "run_reply", no_js)
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")
        assert "галочку" in posting.send(c, cfg, d)
        assert drafts.get(c, d)["status"] == "failed" and drafts.count_sent(c, "th_reply", 0) == 0


def test_stuck_sending_becomes_unknown(db_path):
    with db.session(db_path) as c:
        d = drafts.create(c, "th_reply", {"thread_title": "LF"}, "p", now=NOW)
        drafts.take_for_sending(c, d, "th_reply", now=NOW)
        assert drafts.get(c, d, now=NOW + 60)["status"] == "sending"
        later = NOW + drafts.STUCK_AFTER_S + 1
        row = drafts.get(c, d, now=later)
        assert row["status"] == "unknown" and "проверь" in row["result"]
        listed = [posting.describe(r) for r in posting.list_open(c, now=later)]
        assert any("неизвестно" in line for line in listed)


def test_wait_for_closes_draft_when_sender_died_before_claim(db_path):
    class Dead:
        def poll(self):
            return 1

    with db.session(db_path) as c:
        d = drafts.create(c, "th_reply", {"x": 1}, "p")
        posting._PROCS[d] = Dead()
        row = posting.wait_for(c, d, timeout_s=5, poll_s=0.01)
        assert row["status"] == "failed" and "не запустилась" in row["result"]
        # И позже сам по себе уже не уйдёт.
        with pytest.raises(drafts.DraftError):
            drafts.take_for_sending(c, d, "th_reply")


def test_send_lock_busy_leaves_draft_pending(db_path, cfg):
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")
        with posting.send_lock():
            got = []
            t = threading.Thread(target=lambda: got.append(posting.send(db.connect(db_path), cfg, d, lock_timeout_s=0.3)))
            t.start()
            t.join()
        assert "другая отправка" in got[0]
        assert drafts.get(c, d)["status"] == "pending"


# --- DeviantArt ---

class FakeDA:
    def __init__(self, publish_error=None):
        self.published = []
        self.publish_error = publish_error

    def is_connected(self):
        return True

    def stash_submit(self, file, title, description, tags):
        self.submitted = (file, title, list(tags))
        return 42

    def stash_publish(self, itemid, **kw):
        if self.publish_error:
            raise self.publish_error
        self.published.append(kw)
        return {"url": "https://www.deviantart.com/me/art/x-1", "deviationid": "u1"}


def image(tmp_path, name="moth.png", data=b"\x89PNG"):
    p = tmp_path / "Downloads" / name
    p.write_bytes(data)
    return p


@pytest.fixture
def da_cfg(cfg):
    cfg.posting.group_presets = {"a": [{"group": "da-adopts", "group_id": "g", "folder_id": "f", "folder_name": "x"}]}
    return cfg


def test_da_group_limit_rechecked_at_send(db_path, da_cfg, tmp_path, monkeypatch):
    fake = FakeDA()
    monkeypatch.setattr(posting, "da_client", lambda c: fake)
    img = image(tmp_path)
    with db.session(db_path) as c:
        for _ in range(2):
            drafts.log_sent(c, "da_group", "da-adopts", "old", True)
        d1, p1 = posting.prepare_da_publish(c, da_cfg, str(img), "Moth 1", preset="a")
        d2, p2 = posting.prepare_da_publish(c, da_cfg, str(img), "Moth 2", preset="a")
        assert "da-adopts" in p1 and "da-adopts" in p2
        assert "опубликовано" in posting.send(c, da_cfg, d1)
        second = posting.send(c, da_cfg, d2)
        assert "не отправила" in second and "новый черновик" in second
        assert len(fake.published) == 1 and drafts.get(c, d2)["status"] == "failed"


@pytest.mark.parametrize("error,status,counted", [
    (da_api.DAApiError("нет связи с DeviantArt: ReadTimeout"), "unknown", 1),
    (da_api.DAApiError("server", code=503), "unknown", 1),
    (da_api.DAApiError("плохой запрос", code=2), "failed", 0),
    # DeviantArt ответил отказом с error_code 0 / 5 — это ответ, а не потерянный запрос
    (da_api.DAApiError("нужно согласие", code=0, status=400), "failed", 0),
    (da_api.DAApiError("неверное разрешение", code=5, status=400), "failed", 0),
    (da_api.DAApiError("server", code="http_502", status=502), "unknown", 1),
    (da_api.RateLimited("429"), "failed", 0),
])
def test_da_publish_errors(db_path, da_cfg, tmp_path, monkeypatch, error, status, counted):
    monkeypatch.setattr(posting, "da_client", lambda c: FakeDA(publish_error=error))
    img = image(tmp_path)
    with db.session(db_path) as c:
        d, _ = posting.prepare_da_publish(c, da_cfg, str(img), "Moth", preset="a")
        result = posting.send(c, da_cfg, d)
        assert drafts.get(c, d)["status"] == status
        assert drafts.count_sent(c, "da_group", 0, target="da-adopts") == counted
        if status == "unknown":
            assert "галерею" in result


def test_da_file_changed_after_draft(db_path, da_cfg, tmp_path, monkeypatch):
    fake = FakeDA()
    monkeypatch.setattr(posting, "da_client", lambda c: fake)
    img = image(tmp_path)
    with db.session(db_path) as c:
        d, _ = posting.prepare_da_publish(c, da_cfg, str(img), "Moth")
        img.write_bytes(b"\x89PNG other picture")
        assert "изменился" in posting.send(c, da_cfg, d)
        assert fake.published == []


def test_da_preview_matches_what_is_sent(db_path, da_cfg, tmp_path, monkeypatch):
    fake = FakeDA()
    monkeypatch.setattr(posting, "da_client", lambda c: fake)
    img = image(tmp_path)
    with db.session(db_path) as c:
        d, preview = posting.prepare_da_publish(c, da_cfg, str(img), "Moth", tags=["адопт", "open adopt", "OTA!"])
        assert "Файл: ~/Downloads/moth.png" in preview
        assert "Теги: open_adopt, OTA" in preview and "не войдут адопт" in preview
        posting.send(c, da_cfg, d)
        assert fake.submitted[1] == "Moth" and fake.submitted[2] == ["open_adopt", "OTA"]
        assert fake.published[0]["tags"] == ["open_adopt", "OTA"]


def test_da_title_too_long_refused(db_path, da_cfg, tmp_path):
    img = image(tmp_path)
    with db.session(db_path) as c:
        d, text = posting.prepare_da_publish(c, da_cfg, str(img), "x" * 51)
        assert d is None and "50" in text


@pytest.mark.parametrize("where,ok", [("Downloads", True), ("Desktop", True), ("Documents", False), ("", False)])
def test_da_only_from_picture_folders(db_path, da_cfg, tmp_path, where, ok):
    folder = tmp_path / where if where else tmp_path
    folder.mkdir(exist_ok=True)
    img = folder / "p.png"
    img.write_bytes(b"x")
    with db.session(db_path) as c:
        d, text = posting.prepare_da_publish(c, da_cfg, str(img), "T")
        assert bool(d) is ok
        if not ok:
            assert "Загрузках" in text


def test_da_symlink_out_of_folders_refused_and_non_image(db_path, da_cfg, tmp_path):
    secret = tmp_path / "Documents"
    secret.mkdir()
    (secret / "passport.jpg").write_bytes(b"x")
    link = tmp_path / "Downloads" / "cute.jpg"
    link.symlink_to(secret / "passport.jpg")
    (tmp_path / "Downloads" / "notes.txt").write_text("x")
    with db.session(db_path) as c:
        assert posting.prepare_da_publish(c, da_cfg, str(link), "T")[0] is None
        assert "только картинки" in posting.prepare_da_publish(c, da_cfg, str(tmp_path / "Downloads" / "notes.txt"), "T")[1]


# --- Отклик в её Chrome: вкладка ушла, Chrome занят, проверка нужного перса ---

def test_wrong_page_releases_slot_and_fails(db_path, cfg, monkeypatch):
    from mcp_devian import her_chrome

    monkeypatch.setattr(her_chrome, "run_reply", slow_reply([], 0, state="wrong_page"))
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")
        assert "вкладка ушла" in posting.send(c, cfg, d)
        assert drafts.get(c, d)["status"] == "failed" and drafts.count_sent(c, "th_reply", 0) == 0


def test_chrome_busy_leaves_draft_retryable(db_path, cfg, monkeypatch):
    from mcp_devian import her_chrome, toyhouse

    def busy(*a, **k):
        raise toyhouse.BrowserBusy("профиль занят")

    monkeypatch.setattr(her_chrome, "run_reply", busy)
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Луна")
        assert "Chrome сейчас занят" in posting.send(c, cfg, d)
        row = drafts.get(c, d)
        assert row["status"] == "pending" and "занят" in row["result"] and row["sent_ts"] is None
        assert drafts.count_sent(c, "th_reply", 0) == 0
        # Её «го» в силе: когда Chrome освободился, тот же черновик уходит.
        monkeypatch.setattr(her_chrome, "run_reply", slow_reply([], 0))
        assert "опубликован" in posting.send(c, cfg, d)
        assert drafts.get(c, d)["status"] == "sent" and drafts.count_sent(c, "th_reply", 0) == 1


def test_wait_for_returns_when_sender_finished_but_draft_waits(db_path):
    class Done:
        def poll(self):
            return 0

    with db.session(db_path) as c:
        d = drafts.create(c, "th_reply", {"x": 1}, "p")
        drafts.note_waiting(c, d, posting.CHROME_BUSY)
        posting._PROCS[d] = Done()
        started = time.time()
        row = posting.wait_for(c, d, timeout_s=5, poll_s=0.01)
        assert time.time() - started < 1
        assert row["status"] == "pending" and "занят" in row["result"]


def test_reply_passes_character_url_for_verification(db_path, cfg, monkeypatch):
    from mcp_devian import her_chrome

    seen = {}

    def run(thread_url, html, submit, username, marker, **kw):
        seen.update(kw)
        return {"state": "posted", "permalink": thread_url + "#p"}

    monkeypatch.setattr(her_chrome, "run_reply", run)
    with db.session(db_path) as c:
        d, _ = posting.prepare_toyhouse_reply(c, cfg, "th:1", "Сол")
        posting.send(c, cfg, d)
    assert seen["character_url"] == "https://toyhou.se/124.sol"


def test_same_image_again_is_warned(db_path, da_cfg, tmp_path, monkeypatch):
    """После «исход неизвестен» или «опубликовано» новый черновик той же картинки предупреждает."""
    fake = FakeDA()
    monkeypatch.setattr(posting, "da_client", lambda c: fake)
    img = image(tmp_path)
    with db.session(db_path) as c:
        d1, p1 = posting.prepare_da_publish(c, da_cfg, str(img), "Moth")
        assert "две одинаковые" not in p1
        posting.send(c, da_cfg, d1)
        assert drafts.get(c, d1)["status"] == "sent"
        d2, p2 = posting.prepare_da_publish(c, da_cfg, str(img), "Moth again")
        assert d2 and f"{d1} — отправлен" in p2 and "две одинаковые работы" in p2
        old = posting.prepare_da_publish(c, da_cfg, str(img), "Moth", now=time.time() + 15 * 86400)[1]
        assert "две одинаковые" not in old  # две недели спустя — уже не про то же


def test_da_preview_says_terms_are_agreed(db_path, da_cfg, tmp_path, monkeypatch):
    """da_api при ошибке 0/1 повторяет публикацию с agree_submission/agree_tos — она должна это видеть."""
    monkeypatch.setattr(posting, "da_client", lambda c: FakeDA())
    with db.session(db_path) as c:
        _, preview = posting.prepare_da_publish(c, da_cfg, str(image(tmp_path)), "Moth")
        assert "Submission Policy" in preview and "её «го» означает и это согласие" in preview
