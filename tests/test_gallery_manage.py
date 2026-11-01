"""Закрыть аукционы, папки галереи, переименование: черновик → её «го» → по одной работе, без двойных правок."""

import copy
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from mcp_devian import da_api, db, drafts, mcp_server, posting, sales
from mcp_devian.bids import Comment
from mcp_devian.config import Config, save_config
from mcp_devian.da_api import DAApiError, DAClient, NeedsReconnect, RateLimited, TokenStore, UncertainPublish
from mcp_devian.db import Item

ME = "mothy"


def url_of(n: int) -> str:
    return f"https://www.deviantart.com/{ME}/art/Moth-adopt-{n}"


class FakeGallery:
    """Её DeviantArt в памяти: пишет все вызовы, умеет падать по заказу."""

    WRITES = ("edit_deviation", "create_folder", "rename_folder", "copy_deviations", "move_deviations",
              "post_comment")

    def __init__(self):
        self.calls: list[tuple] = []
        self.folders = [{"folder_id": "F-feat", "name": "Featured", "parent": None, "description": ""},
                        {"folder_id": "F-auc", "name": "Auctions", "parent": "F-feat", "description": "open ones"}]
        self.works: dict[str, dict] = {}
        self.comments: dict[str, list[Comment]] = {}
        self.fail: dict[str, Exception] = {}
        self.fail_on: dict[str, str] = {}  # метод → uuid, на котором падать
        self.missing_scopes: set[str] = set()
        self.wipe_tags = False
        self.lose_comment_response = False

    def add(self, uuid, title, n, galleries=("F-auc",), tags=("moth", "adopt"), author=ME):
        names = {f["folder_id"]: f["name"] for f in self.folders}
        self.works[uuid] = {"deviationid": uuid, "title": title, "is_mature": False, "mature_level": None,
                            "mature_classification": [], "allow_comments": True, "tags": list(tags),
                            "galleries": [{"folder_id": g, "name": names.get(g, g)} for g in galleries],
                            "allow_free_download": False, "license": "No License", "can_post_comment": True,
                            "author": author, "url": url_of(n)}

    def writes(self, name=None):
        return [c for c in self.calls if c[0] in self.WRITES and (name is None or c[0] == name)]

    def _scope(self, scopes):
        if self.missing_scopes & set(scopes):
            raise NeedsReconnect(da_api.RECONNECT, "insufficient_scope")

    def _maybe_fail(self, name, uuid=None):
        if name in self.fail and (name not in self.fail_on or self.fail_on[name] == uuid):
            raise self.fail[name]

    # подключение
    def is_connected(self):
        return True

    def whoami(self):
        return {"username": ME, "userid": "1"}

    def require_scopes(self, *needed):
        self.calls.append(("require_scopes", needed))
        if self.missing_scopes & set(needed):
            raise NeedsReconnect(da_api.RECONNECT, "missing_scope")

    # чтение
    def gallery_folders(self, username):
        self.calls.append(("gallery_folders", username))
        return copy.deepcopy(self.folders)

    def deviation_uuid_from_url(self, url, max_pages=10, **kw):
        return next((u for u, w in self.works.items() if w["url"] == url), None)

    def gallery_all(self, username, max_pages=10):
        return [{"deviationid": u, "title": w["title"], "url": w["url"]} for u, w in self.works.items()]

    def edit_state(self, uuid):
        self.calls.append(("edit_state", uuid))
        self._maybe_fail("edit_state", uuid)
        if uuid not in self.works:
            raise DAApiError("нет такой работы", "edit_unsafe")
        return copy.deepcopy(self.works[uuid])

    def deviations_brief(self, uuids):
        return {u: {"title": self.works[u]["title"], "author": self.works[u]["author"],
                    "galleries": copy.deepcopy(self.works[u]["galleries"])} for u in uuids if u in self.works}

    def deviation_comments(self, uuid):
        self._maybe_fail("deviation_comments", uuid)
        return list(self.comments.get(uuid, []))

    # запись
    def edit_deviation(self, uuid, state, *, title=None, tags=None, noai=True, is_ai_generated=False):
        self._scope(da_api.EDIT_SCOPES)
        self.calls.append(("edit_deviation", uuid, title, noai, is_ai_generated, copy.deepcopy(state)))
        self._maybe_fail("edit_deviation", uuid)
        self.works[uuid]["title"] = title
        if self.wipe_tags:
            self.works[uuid]["tags"] = []
        return {"url": self.works[uuid]["url"], "deviationid": uuid}

    def verify_edit(self, uuid, before, title, tags=None):
        self._maybe_fail("verify_edit", uuid)
        now = self.works[uuid]
        lost = []
        if now["title"] != title:
            lost.append("название")
        if set(before["tags"]) - set(now["tags"]):
            lost.append(f"пропали теги: {', '.join(sorted(set(before['tags']) - set(now['tags'])))}")
        return lost

    def create_folder(self, name, *, username, parent_folderid=None, description=""):
        self._scope(da_api.GALLERY_SCOPES)
        self.calls.append(("create_folder", name))
        self._maybe_fail("create_folder")
        fid = f"F-{len(self.folders)}"
        self.folders.append({"folder_id": fid, "name": name, "parent": None, "description": description})
        return {"folder_id": fid, "name": name}

    def rename_folder(self, folderid, name, description=""):
        self.calls.append(("rename_folder", folderid, name, description))
        for f in self.folders:
            if f["folder_id"] == folderid:
                f["name"] = name

    def _place(self, uuid, fid, drop=None):
        g = self.works[uuid]["galleries"]
        if drop:
            g[:] = [x for x in g if x["folder_id"] != drop]
        if all(x["folder_id"] != fid for x in g):
            g.append({"folder_id": fid, "name": next(f["name"] for f in self.folders if f["folder_id"] == fid)})

    def copy_deviations(self, target, ids, on_chunk=None):
        self._scope(da_api.GALLERY_SCOPES)
        self.calls.append(("copy_deviations", target, list(ids)))
        self._maybe_fail("copy_deviations")
        for u in ids:
            self._place(u, target)
        if on_chunk:
            on_chunk(len(ids))

    def move_deviations(self, source, target, ids, on_chunk=None):
        self.calls.append(("move_deviations", source, target, list(ids)))
        ids = list(ids)
        for i in range(0, len(ids), da_api.GALLERY_CHUNK):  # как настоящий клиент: пачками по 24
            if i:
                self._maybe_fail("move_deviations")
            for u in ids[i:i + da_api.GALLERY_CHUNK]:
                self._place(u, target, drop=source)
            if on_chunk:
                on_chunk(len(ids[i:i + da_api.GALLERY_CHUNK]))

    def post_comment(self, uuid, body, reply_to=None):
        self._scope(da_api.COMMENT_SCOPES)
        self.calls.append(("post_comment", uuid, body))
        self._maybe_fail("post_comment", uuid)
        self.comments.setdefault(uuid, []).append(Comment(f"c{len(self.calls)}", None, ME, time.time(), body))
        if self.lose_comment_response:
            raise UncertainPublish("DeviantArt не ответил (комментарий)", "network")
        return {"commentid": f"c{len(self.calls)}"}


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def fake(monkeypatch):
    f = FakeGallery()
    f.add("U101", "Moth adopt [OPEN]", 101)
    f.add("U102", "(open) Lunar moth", 102)
    f.add("U103", "Silk moth auction", 103)
    monkeypatch.setattr(posting, "da_client", lambda c: f)
    return f


@pytest.fixture
def cfg():
    c = Config(configured=True)
    c.posting.da_client_id = "123"
    c.deviantart.my_username = ME
    return c


def add_lot(conn, n, title, winner="bob", amount=40.0):
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=title, url=url_of(n), author=ME,
                              lot_type="auction", status="open", own=True, price_usd=10.0,
                              extra={"uuid": f"U{n}"}), now=time.time() - 3600)
    if amount is not None:
        conn.execute("INSERT INTO auctions (deviation_id, owner, top_amount, currency, n_bids, bidders, winner, ended, "
                     "updated_ts, extra) VALUES (?, ?, ?, 'usd', 3, 2, ?, 0, ?, ?)",
                     (f"da:{n}", ME, amount, winner, time.time(), json.dumps({"top_user": winner})))
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.sqlite3")
    add_lot(c, 101, "Moth adopt [OPEN]")
    add_lot(c, 102, "(open) Lunar moth", winner="kate", amount=25.0)
    add_lot(c, 103, "Silk moth auction", amount=None)
    yield c
    c.close()


def close(conn, cfg, **kw):
    kw.setdefault("folder", "Sold")
    kw.setdefault("from_folder", "Auctions")
    kw.setdefault("comment", "Congrats {winner}! Your bid {amount} won. Please send payment within 24h")
    return posting.prepare_da_close(conn, cfg, **kw)


def go(conn, cfg, draft_id, sleeps=None):
    return posting.send(conn, cfg, draft_id, sleep=sleeps if sleeps is not None else Sleeps())


# --- Названия ---

@pytest.mark.parametrize("title,word,expected", [
    ("Moth adopt [OPEN]", "CLOSED", "Moth adopt [CLOSED]"),
    ("(open) Lunar moth", "CLOSED", "(closed) Lunar moth"),
    ("Moth | Open", "SOLD", "Moth | Sold"),
    ("Silk moth auction", "CLOSED", "[CLOSED] Silk moth auction"),
    ("OTA open to offers", "CLOSED", "[CLOSED] OTA open to offers"),
    ("Moth [AVAILABLE]", "SOLD", "Moth [SOLD]"),
    # «open» посреди названия — просто слово, не метка
    ("Eyes Open moth adopt", "CLOSED", "[CLOSED] Eyes Open moth adopt"),
    # «CLOSED 2 left» всё ещё читается открытым — метка в начало, иначе монитор откроет лот снова
    ("Moth auction OPEN 2 left", "CLOSED", "[CLOSED] Moth auction OPEN 2 left"),
    ("OPEN moth auction", "CLOSED", "CLOSED moth auction"),
])
def test_closed_title(title, word, expected):
    assert posting.closed_title(title, word) == (expected, "")


def test_closed_title_already_closed():
    new, why = posting.closed_title("Moth adopt [CLOSED]", "CLOSED")
    assert new is None and "уже" in why
    assert posting.closed_title("Moth [SOLD]", "CLOSED")[0] is None
    # уже закрыта: слово open в названии не трогаем
    assert posting.closed_title("[CLOSED] Eyes open moth", "CLOSED")[0] is None


# --- Превью: всё видно, ничего не отправлено ---

def test_preview_lists_every_change_and_sends_nothing(conn, cfg, fake):
    draft_id, text = close(conn, cfg)
    assert draft_id
    assert "Сначала создам в её галерее папку «Sold» — её пока нет." in text
    assert "«Moth adopt [OPEN]» → «Moth adopt [CLOSED]»" in text
    assert "«(open) Lunar moth» → «(closed) Lunar moth»" in text
    assert "«Silk moth auction» → «[CLOSED] Silk moth auction»" in text
    assert "переложу из «Auctions» в «Sold»" in text
    assert "«Congrats bob! Your bid $40 won. Please send payment within 24h»" in text
    assert "«Congrats kate! Your bid $25 won." in text
    # у третьего лота ставок нет: слоты не заполнить — комментария не будет, продажи тоже
    assert "победитель неизвестен" in text and "ставок не нашла" in text
    assert "Продажа: запишу «ждёт оплаты» — bob, $40" in text
    assert "Размер показа и водяной знак" in text  # честно про то, чего API не показывает
    assert fake.writes() == []
    assert drafts.get(conn, draft_id)["status"] == "pending"


def test_nothing_happens_without_draft_flow(conn, cfg, fake, monkeypatch):
    assert "Такого черновика нет" in go(conn, cfg, "deadbeef")
    draft_id, _ = close(conn, cfg)
    monkeypatch.setattr(posting, "SENDING_ENABLED", False)
    assert go(conn, cfg, draft_id) == posting.NOT_YET
    assert fake.writes() == [] and drafts.get(conn, draft_id)["status"] == "pending"


def test_cannot_be_scheduled(conn, cfg, fake):
    draft_id, _ = close(conn, cfg)
    assert "только сразу" in posting.approve_draft(conn, cfg, draft_id, "завтра 19:00")


# --- Отправка ---

def test_close_all_end_to_end(conn, cfg, fake):
    draft_id, _ = close(conn, cfg)
    sleeps = Sleeps()
    result = go(conn, cfg, draft_id, sleeps)
    row = drafts.get(conn, draft_id)
    assert row["status"] == "sent", result
    assert [c[2] for c in fake.writes("edit_deviation")] == [
        "Moth adopt [CLOSED]", "(closed) Lunar moth", "[CLOSED] Silk moth auction"]
    assert len(fake.writes("create_folder")) == 1
    sold = next(f["folder_id"] for f in fake.folders if f["name"] == "Sold")
    assert all(any(g["folder_id"] == sold for g in w["galleries"]) for w in fake.works.values())
    assert all(all(g["folder_id"] != "F-auc" for g in w["galleries"]) for w in fake.works.values())
    assert [c[2] for c in fake.writes("post_comment")] == [
        "Congrats bob! Your bid $40 won. Please send payment within 24h",
        "Congrats kate! Your bid $25 won. Please send payment within 24h"]
    # пауза только между работами, «как живой человек»
    assert len(sleeps) == 2 and all(posting.PACE_S[0] <= s <= posting.PACE_S[1] for s in sleeps)
    for n in (101, 102, 103):
        item = db.get_item(conn, f"da:{n}")
        assert item["status"] == "closed" and item["closed_ts"]
    assert db.get_item(conn, "da:101")["title"] == "Moth adopt [CLOSED]"
    progress = posting.describe_progress(conn, draft_id)
    assert "4 из 4 готово" in progress and "создала папку «Sold»" in progress
    # её «го» действует один раз
    assert "уже" in go(conn, cfg, draft_id) and len(fake.writes("edit_deviation")) == 3


def test_edit_keeps_settings_and_her_ai_flags(conn, cfg, fake):
    cfg.posting.default_noai = True
    draft_id, _ = close(conn, cfg, works=["da:101"], folder="", from_folder="", comment="")
    go(conn, cfg, draft_id)
    _, uuid, title, noai, is_ai, state = fake.writes("edit_deviation")[0]
    assert uuid == "U101" and noai is True and is_ai is False
    assert state["tags"] == ["moth", "adopt"] and state["galleries"][0]["folder_id"] == "F-auc"


def test_sale_recorded_once(conn, cfg, fake):
    draft_id, _ = close(conn, cfg)
    go(conn, cfg, draft_id)
    rows = conn.execute("SELECT * FROM sales WHERE item_id = 'da:101'").fetchall()
    assert len(rows) == 1 and rows[0]["buyer"] == "bob" and rows[0]["amount"] == 40.0 and rows[0]["status"] == "won"
    assert sales.sync_from_auctions(conn) == []  # авто-запись не задвоит
    # второй черновик по тем же работам продажу не заводит и честно это пишет
    conn.execute("UPDATE items SET status = 'open' WHERE id = 'da:101'")
    conn.commit()
    _, text = close(conn, cfg, works=["da:101"])
    assert "продажа уже записана" in text
    assert conn.execute("SELECT COUNT(*) FROM sales WHERE item_id = 'da:101'").fetchone()[0] == 1


def test_no_sales_when_she_says_so(conn, cfg, fake):
    draft_id, text = close(conn, cfg, record_sales=False)
    assert "Продажа" not in text
    go(conn, cfg, draft_id)
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0


def test_title_changed_since_preview_skips_that_work(conn, cfg, fake):
    draft_id, _ = close(conn, cfg)
    fake.works["U102"]["title"] = "Lunar moth — ON HOLD"  # она поменяла название на сайте после превью
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "partial"
    assert "U102" not in [c[1] for c in fake.writes("edit_deviation")]
    assert all(c[1] != "U102" for c in fake.writes("post_comment"))
    assert "название поменялось после черновика" in result
    assert db.get_item(conn, "da:102")["status"] == "open"


def test_rerun_never_doubles_comment_or_rename(conn, cfg, fake):
    fake.lose_comment_response = True  # первый комментарий ушёл, а ответ потерялся
    draft_id, _ = close(conn, cfg)
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "unknown"
    assert drafts.UNKNOWN_RESULT in result and "дальше не иду" in result
    assert len(fake.writes("edit_deviation")) == 1  # дальше вслепую не пошли
    fake.lose_comment_response = False
    assert drafts.get(conn, draft_id)["status"] == "unknown"
    draft2, text = close(conn, cfg, works=["da:101", "da:102", "da:103"])
    assert "Название: не меняю (уже помечена закрытой)" in text and "Папка: уже в «Sold»" in text
    assert "Этот комментарий я уже писала под работой — второй раз не напишу" in text
    go(conn, cfg, draft2)
    assert [c[1] for c in fake.writes("post_comment")].count("U101") == 1
    assert [c[1] for c in fake.writes("edit_deviation")].count("U101") == 1
    assert drafts.get(conn, draft2)["status"] == "sent"


def test_comment_already_on_page_is_not_repeated(conn, cfg, fake):
    fake.comments["U101"] = [Comment("x", None, ME.upper(), 1.0,
                                     "Congrats bob!  Your bid $40 won. Please send payment within 24h")]
    draft_id, _ = close(conn, cfg, works=["da:101"])
    result = go(conn, cfg, draft_id)
    assert fake.writes("post_comment") == [] and "такой её комментарий уже есть" in result


def test_comment_is_only_her_template(conn, cfg, fake):
    assert "неизвестные слоты {buyer}" in posting.check_comment_template("Congrats {buyer}!")
    assert "неизвестные слоты" in posting.check_comment_template("Congrats {winner!r}")
    assert "скобки" in posting.check_comment_template("Congrats {winner")
    assert posting.check_comment_template("Congrats {winner}, {amount} {{literally}}") == ""
    assert posting.render_comment("Hi {winner} — {{x}}", "bob", None) == ("Hi bob — {x}", "")
    draft_id, text = close(conn, cfg, comment="Congrats {buyer}!")
    assert draft_id is None and "{buyer}" in text
    # ник победителя — чужой текст: странный ник в её комментарий не попадёт
    conn.execute("UPDATE auctions SET winner = '<b>evil</b>', extra = '{}' WHERE deviation_id = 'da:101'")
    conn.commit()
    _, text = close(conn, cfg, works=["da:101"], comment="Congrats {winner}!")
    assert "evil" not in text and "победитель неизвестен" in text


def test_partial_failure_reported_per_work(conn, cfg, fake):
    fake.fail["edit_deviation"] = DAApiError("DeviantArt не принял запрос: bad", "invalid_request", status=400)
    fake.fail_on["edit_deviation"] = "U102"
    draft_id, _ = close(conn, cfg)
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "partial"
    assert "«(open) Lunar moth» — не вышло: не вышло: DeviantArt не принял запрос" in result
    assert "«Moth adopt [OPEN]» — готово" in result and "«Silk moth auction» — готово" in result
    assert all(c[1] != "U102" for c in fake.writes("post_comment"))


def test_uncertain_edit_stops_everything(conn, cfg, fake):
    fake.fail["edit_deviation"] = UncertainPublish("DeviantArt не ответил (правка работы)", "network")
    draft_id, _ = close(conn, cfg, folder="", from_folder="")
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "unknown"
    assert len(fake.writes("edit_deviation")) == 1 and "не делала" in result


def test_wipe_after_edit_stops_the_rest(conn, cfg, fake):
    fake.wipe_tags = True
    draft_id, _ = close(conn, cfg)
    result = go(conn, cfg, draft_id)
    assert len(fake.writes("edit_deviation")) == 1
    assert "сбросил" in result and "пропали теги" in result
    assert drafts.get(conn, draft_id)["status"] in ("partial", "failed")
    assert fake.writes("post_comment") == []


def test_wipe_on_single_work_is_partial_not_failed(conn, cfg, fake):
    # Название уже поменялось — «не отправился» было бы неправдой.
    fake.wipe_tags = True
    draft_id, _ = close(conn, cfg, works=["da:101"], folder="", from_folder="", comment="")
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "partial", result
    assert "сделано не всё" in result and "пропали теги" in result


def test_folder_auto_create_shown_in_folders_preview(conn, cfg, fake):
    draft_id, text = posting.prepare_da_folders(conn, cfg, create=["YCH"], rename={"Auctions": "Open auctions"},
                                                move={"Sold": ["da:101", "Lunar moth"]}, remove_from="Auctions")
    assert draft_id and "Создам папки: «YCH», «Sold»" in text
    assert "«Auctions» → «Open auctions»" in text and "переложу из «Auctions»" in text
    assert fake.writes() == []
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "sent", result
    assert [c[1] for c in fake.writes("create_folder")] == ["YCH", "Sold"]
    # описание папки отправлено прежним
    assert fake.writes("rename_folder") == [("rename_folder", "F-auc", "Open auctions", "open ones")]
    assert len(fake.writes("move_deviations")) == 1


def test_folders_rerun_does_not_duplicate(conn, cfg, fake):
    draft_id, _ = posting.prepare_da_folders(conn, cfg, create=["YCH"])
    fake.folders.append({"folder_id": "F-y", "name": "ych", "parent": None, "description": ""})  # появилась сама
    result = go(conn, cfg, draft_id)
    assert fake.writes("create_folder") == [] and "уже есть" in result
    assert drafts.get(conn, draft_id)["status"] == "sent"
    assert posting.prepare_da_folders(conn, cfg, create=["YCH"])[0] is None


def test_rename_works(conn, cfg, fake):
    draft_id, text = posting.prepare_da_rename(conn, cfg, {url_of(103): "Silk moth — SOLD to kate",
                                                           "Lunar": "x" * 51})
    assert draft_id and "«Silk moth auction» → «Silk moth — SOLD to kate»" in text
    assert "длиннее 50" in text
    go(conn, cfg, draft_id)
    assert fake.works["U103"]["title"] == "Silk moth — SOLD to kate"
    assert db.get_item(conn, "da:103")["title"] == "Silk moth — SOLD to kate"
    assert len(fake.writes()) == 1


def test_not_her_work_is_refused(conn, cfg, fake):
    draft_id, text = posting.prepare_da_rename(conn, cfg, {"https://www.deviantart.com/someone/art/x-5": "Mine"})
    assert draft_id is None and "не её работа" in text


def test_batch_limit(conn, cfg, fake):
    draft_id, text = close(conn, cfg, works=[f"da:{i}" for i in range(26)])
    assert draft_id is None and "25" in text


# --- Остановка, сбои чтения, продажа при обрыве ---

class StopOnSleep:
    """Она говорит «стоп», пока помощник ждёт перед следующей работой."""

    def __init__(self, conn, draft_id):
        self.conn, self.draft_id, self.sleeps = conn, draft_id, []

    def __call__(self, s):
        self.sleeps.append(s)
        assert posting.request_stop(self.conn, self.draft_id)


def test_stop_mid_run_leaves_the_rest(conn, cfg, fake):
    draft_id, _ = close(conn, cfg, folder="", from_folder="", comment="")
    result = posting.send(conn, cfg, draft_id, sleep=StopOnSleep(conn, draft_id))
    assert [c[1] for c in fake.writes("edit_deviation")] == ["U101"]
    assert "она попросила остановиться" in result
    assert drafts.get(conn, draft_id)["status"] == "partial"
    steps = {r["label"]: r["status"] for r in posting.draft_steps(conn, draft_id)}
    assert steps["«(open) Lunar moth»"] == "notrun" and steps["«Silk moth auction»"] == "notrun"


def test_stop_before_anything_cancels(conn, cfg, fake, monkeypatch):
    draft_id, _ = close(conn, cfg, folder="", from_folder="", comment="")
    # «стоп» пришёл, когда черновик уже забран на отправку, но до первой работы
    monkeypatch.setattr(fake, "is_connected", lambda: posting.request_stop(conn, draft_id))
    result = go(conn, cfg, draft_id)
    assert fake.writes() == [] and "ничего не меняла" in result
    assert drafts.get(conn, draft_id)["status"] == "cancelled"


def test_stop_only_for_running_gallery_drafts(conn, cfg, fake):
    draft_id, _ = close(conn, cfg)
    assert posting.request_stop(conn, draft_id) is False  # ждёт «го» — это обычная отмена
    assert posting.request_stop(conn, "deadbeef") is False


def test_cancel_tool_stops_running_gallery_draft(server, fake):
    out = mcp_server.prepare_close_auctions(folder="Sold")
    draft_id = out.split()[1].rstrip(":")
    with db.session() as c:
        c.execute("UPDATE drafts SET status = 'sending', sent_ts = ? WHERE id = ?", (time.time(), draft_id))
        c.commit()
    assert "Останавливаю" in mcp_server.cancel_draft(draft_id)
    with db.session() as c:
        assert posting._stop_requested(c, draft_id)


def test_read_timeout_keeps_her_go(conn, cfg, fake):
    draft_id, _ = close(conn, cfg, folder="", from_folder="", comment="")
    fake.fail["edit_state"] = DAApiError("нет связи с DeviantArt: ReadTimeout", "network")
    fake.fail_on["edit_state"] = "U101"
    result = go(conn, cfg, draft_id)
    assert fake.writes() == [] and drafts.UNKNOWN_RESULT not in result
    assert drafts.get(conn, draft_id)["status"] == "pending"  # ничего не менялось — «го» в силе
    del fake.fail["edit_state"]
    go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "sent"


def test_read_timeout_mid_run_is_partial_not_unknown(conn, cfg, fake):
    draft_id, _ = close(conn, cfg, folder="", from_folder="", comment="")
    fake.fail["edit_state"] = DAApiError("нет связи с DeviantArt: ReadTimeout", "network")
    fake.fail_on["edit_state"] = "U102"
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "partial", result
    assert [c[1] for c in fake.writes("edit_deviation")] == ["U101"]  # дальше не пошла


def test_refused_first_write_keeps_her_go(conn, cfg, fake):
    fake.fail["edit_deviation"] = RateLimited("DeviantArt просит притормозить (429).", 429, status=429)
    draft_id, _ = close(conn, cfg, folder="", from_folder="", comment="")
    go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "pending"  # отказ ничего не изменил
    fake.fail["edit_deviation"] = NeedsReconnect(da_api.RECONNECT, "insufficient_scope")
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "pending" and "da_connect" in result


def test_failed_recheck_stops_the_rest(conn, cfg, fake):
    fake.fail["verify_edit"] = DAApiError("нет связи с DeviantArt: ReadTimeout", "network")
    draft_id, _ = close(conn, cfg, folder="", from_folder="")
    result = go(conn, cfg, draft_id)
    assert len(fake.writes("edit_deviation")) == 1 and fake.writes("post_comment") == []
    assert "перепроверить" in result and drafts.get(conn, draft_id)["status"] == "partial"


def test_sale_kept_when_run_stops_after_title(conn, cfg, fake):
    fake.fail["copy_deviations"] = RateLimited("DeviantArt просит притормозить (429).", 429, status=429)
    draft_id, _ = close(conn, cfg, from_folder="")
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "partial", result
    sale = conn.execute("SELECT * FROM sales WHERE item_id = 'da:101'").fetchone()
    assert sale and sale["buyer"] == "bob" and sale["amount"] == 40.0
    # лот не отмечен закрытым: следующий «закрыть всё» его доделает (папка, комментарий)
    assert db.get_item(conn, "da:101")["status"] == "open"
    del fake.fail["copy_deviations"]
    draft2, text = close(conn, cfg, from_folder="")
    assert "Moth adopt [CLOSED]" in text and "продажа уже записана" in text
    go(conn, cfg, draft2)
    assert db.get_item(conn, "da:101")["status"] == "closed"
    assert conn.execute("SELECT COUNT(*) FROM sales WHERE item_id = 'da:101'").fetchone()[0] == 1


def test_open_auctions_skips_old_unmarked_and_keeps_newest(conn, cfg, fake, monkeypatch):
    now = time.time()
    db.upsert_item(conn, Item(id="da:900", source="deviantart", title="Old moth auction", url=url_of(900), author=ME,
                              lot_type="auction", status="unknown", own=True, extra={"uuid": "U900"}),
                   now=now - 90 * 86400)
    conn.execute("UPDATE items SET published_ts = ? WHERE id = 'da:900'", (now - 90 * 86400,))
    for n, age in ((101, 3), (102, 2), (103, 1)):
        conn.execute("UPDATE items SET published_ts = ? WHERE id = ?", (now - age * 86400, f"da:{n}"))
    conn.commit()
    assert [r["id"] for r in posting.open_auctions(conn)] == ["da:103", "da:102", "da:101"]
    # с явной меткой OPEN старый лот остаётся открытым
    conn.execute("UPDATE items SET status = 'open' WHERE id = 'da:900'")
    conn.commit()
    assert "da:900" in [r["id"] for r in posting.open_auctions(conn)]
    monkeypatch.setattr(posting, "GALLERY_MAX", 2)
    draft_id, text = close(conn, cfg, folder="", from_folder="", comment="")
    payload = json.loads(drafts.get(conn, draft_id)["payload"])
    assert [w["uuid"] for w in payload["works"]] == ["U102", "U103"] and "самых свежих" in text


def test_folder_move_reports_chunks_already_done(conn, cfg, fake):
    refs = []
    for n in range(200, 225):
        fake.add(f"U{n}", f"Moth {n}", n)
        refs.append(url_of(n))
    draft_id, _ = posting.prepare_da_folders(conn, cfg, move={"Auctions old": refs}, remove_from="Auctions")
    fake.fail["move_deviations"] = RateLimited("DeviantArt просит притормозить (429).", 429, status=429)
    result = go(conn, cfg, draft_id)
    assert "переложила 24 из 25" in result
    assert drafts.get(conn, draft_id)["status"] == "partial"


def test_folder_rename_without_readable_description(conn, cfg, fake):
    fake.folders[1]["description"] = None  # DeviantArt описание не прислал
    draft_id, text = posting.prepare_da_folders(conn, cfg, rename={"Auctions": "Open auctions"})
    assert draft_id is None and "описание" in text
    fake.folders[1]["description"] = "open ones"
    draft_id, _ = posting.prepare_da_folders(conn, cfg, rename={"Auctions": "Open auctions"})
    fake.folders[1]["description"] = None  # пропало к моменту отправки
    result = go(conn, cfg, draft_id)
    assert fake.writes("rename_folder") == [] and "описание" in result


# --- Права: переподключение ---

def test_missing_scope_asks_to_reconnect(conn, cfg, fake):
    fake.missing_scopes = {"gallery"}
    draft_id, text = close(conn, cfg)
    assert draft_id is None and "da_connect" in text and "Разрешить" in text
    # без папки права на правку есть — черновик готовится
    assert close(conn, cfg, folder="", from_folder="", comment="")[0]


def test_missing_scope_at_send_keeps_her_go(conn, cfg, fake):
    draft_id, _ = close(conn, cfg)
    fake.missing_scopes = {"gallery"}
    result = go(conn, cfg, draft_id)
    assert fake.writes() == [] and "da_connect" in result
    assert drafts.get(conn, draft_id)["status"] == "pending"  # ничего не сделано: «го» в силе
    fake.missing_scopes = set()  # она переподключила DeviantArt
    go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "sent"


def test_server_refuses_scope_mid_run(conn, cfg, fake):
    fake.fail["post_comment"] = NeedsReconnect(da_api.RECONNECT, "insufficient_scope")
    draft_id, _ = close(conn, cfg)
    result = go(conn, cfg, draft_id)
    assert drafts.get(conn, draft_id)["status"] == "partial"
    assert "da_connect" in result and len(fake.writes("edit_deviation")) == 1
    assert "«Moth adopt [OPEN]» — сделано не всё: название «Moth adopt [CLOSED]»" in result


# --- Настоящий клиент: что уходит в API ---

API = "/api/v1/oauth2"


def real_client(tmp_path, routes, scope="basic user browse stash publish gallery comment.post"):
    store = TokenStore(tmp_path / "da-token.json")
    store.save({"access_token": "AT", "refresh_token": "RT", "expires_at": time.time() + 3600, "scope": scope})
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        fn = routes.get((request.method, request.url.path))
        return fn(request) if fn else httpx.Response(404, json={"error": "not_found"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return DAClient("CID", store, http=http, sleep=lambda s: None), calls


def form(request):
    return parse_qs(request.content.decode(), keep_blank_values=True)


META = {"deviationid": "U1", "title": "Moth [OPEN]", "license": "No License", "allows_comments": False,
        "is_mature": True, "mature_level": "moderate", "mature_classification": ["gore"],
        "tags": [{"tag_name": "moth", "sponsored": False, "sponsor": ""},
                 {"tag_name": "Adopt_Auction", "sponsored": False, "sponsor": ""}],
        "galleries": [{"folderid": "F-feat", "name": "Featured"}, {"folderid": "F-auc", "name": "Auctions"}],
        "can_post_comment": True, "author": {"username": ME}}


def meta_routes(meta, downloadable=True):
    return {("GET", f"{API}/deviation/metadata"): lambda r: httpx.Response(200, json={"metadata": [meta]}),
            ("GET", f"{API}/deviation/U1"): lambda r: httpx.Response(
                200, json={"deviationid": "U1", "is_downloadable": downloadable, "url": url_of(1)})}


def test_edit_resends_every_readable_field(tmp_path):
    routes = meta_routes(META)
    routes[("POST", f"{API}/deviation/edit/U1")] = lambda r: httpx.Response(
        200, json={"status": "success", "url": url_of(1), "deviationid": "U1"})
    client, calls = real_client(tmp_path, routes)
    state = client.edit_state("U1")
    meta_q = parse_qs(calls[0].url.query.decode())
    assert meta_q["ext_gallery"] == ["true"] and meta_q["deviationids[]"] == ["U1"]
    client.edit_deviation("U1", state, title="Moth [CLOSED]", noai=True, is_ai_generated=False)
    sent = form(calls[-1])
    assert sent["title"] == ["Moth [CLOSED]"]
    assert sent["tags[]"] == ["moth", "Adopt_Auction"]
    assert sent["galleryids[]"] == ["F-feat", "F-auc"]
    assert sent["is_mature"] == ["true"] and sent["mature_level"] == ["moderate"]
    assert sent["mature_classification[]"] == ["gore"]
    assert sent["allow_comments"] == ["false"] and sent["allow_free_download"] == ["true"]
    assert sent["noai"] == ["true"] and sent["is_ai_generated"] == ["false"]
    assert "description" not in sent  # описание API не меняет — и мы его не шлём


def test_edit_state_refuses_when_something_unreadable(tmp_path):
    no_folders = {k: v for k, v in META.items() if k != "galleries"}
    client, _ = real_client(tmp_path, meta_routes(no_folders))
    with pytest.raises(DAApiError, match="папки галереи") as e:
        client.edit_state("U1")
    assert e.value.code == "edit_unsafe"
    client, _ = real_client(tmp_path, meta_routes({**META, "license": "Creative Commons Attribution 3.0 License"}))
    with pytest.raises(DAApiError, match="лицензия"):
        client.edit_state("U1")
    client, _ = real_client(tmp_path, {**meta_routes(META), ("GET", f"{API}/deviation/U1"): lambda r: httpx.Response(
        200, json={"deviationid": "U1"})})
    with pytest.raises(DAApiError, match="скачивание"):
        client.edit_state("U1")


def test_verify_edit_notices_wiped_fields(tmp_path):
    wiped = {**META, "title": "Moth [CLOSED]", "tags": [], "galleries": [{"folderid": "F-feat", "name": "Featured"}]}
    client, _ = real_client(tmp_path, meta_routes(META))
    before = client.edit_state("U1")
    client2, _ = real_client(tmp_path, meta_routes(wiped))
    lost = client2.verify_edit("U1", before, "Moth [CLOSED]")
    assert any("теги" in x for x in lost) and any("Auctions" in x for x in lost)
    assert client2.verify_edit("U1", before, "Moth [CLOSED]", tags=[]) != []  # папки всё равно пропали


def test_edit_5xx_is_uncertain_and_not_retried(tmp_path):
    hits = []
    routes = meta_routes(META)
    routes[("POST", f"{API}/deviation/edit/U1")] = lambda r: hits.append(1) or httpx.Response(502)
    client, _ = real_client(tmp_path, routes)
    state = client.edit_state("U1")
    with pytest.raises(UncertainPublish):
        client.edit_deviation("U1", state, title="Moth [CLOSED]")
    assert len(hits) == 1


def test_stored_scope_without_gallery_needs_reconnect(tmp_path):
    client, calls = real_client(tmp_path, {}, scope="basic user browse stash publish")
    with pytest.raises(NeedsReconnect, match="da_connect"):
        client.create_folder("Sold", username=ME)
    with pytest.raises(NeedsReconnect):
        client.post_comment("U1", "hi")
    assert calls == []  # до DeviantArt даже не дошло
    client.require_scopes("stash", "publish")  # правка названий работает и со старым подключением


def test_server_scope_error_is_reconnect(tmp_path):
    routes = {("POST", f"{API}/gallery/folders/create"): lambda r: httpx.Response(
        403, json={"error": "insufficient_scope", "error_description": "gallery scope required"})}
    client, _ = real_client(tmp_path, routes, scope="")  # DeviantArt не назвал права — проверяет сам сервер
    with pytest.raises(NeedsReconnect, match="Разрешить"):
        client.create_folder("Sold", username=ME)
    routes = {("POST", f"{API}/gallery/folders/create"): lambda r: httpx.Response(
        400, json={"error": "unauthorized", "error_description": "Missing scope: gallery"})}
    client, _ = real_client(tmp_path, routes, scope="")
    with pytest.raises(NeedsReconnect):
        client.create_folder("Sold", username=ME)


def test_token_record_does_not_invent_scopes():
    record = da_api._token_record({"access_token": "A", "refresh_token": "R"}, 0.0)
    assert record["scope"] == ""
    assert da_api._token_record({"access_token": "B"}, 0.0, {"scope": "basic", "refresh_token": "R"})["scope"] == "basic"


def test_create_folder_lost_response_does_not_duplicate(tmp_path):
    creates = []
    routes = {("POST", f"{API}/gallery/folders/create"): lambda r: creates.append(1) or httpx.Response(503),
              ("GET", f"{API}/gallery/folders"): lambda r: httpx.Response(200, json={
                  "results": [{"folderid": "F-new", "name": "Sold", "parent": None}], "has_more": False})}
    client, _ = real_client(tmp_path, routes)
    assert client.create_folder("Sold", username=ME) == {"folder_id": "F-new", "name": "Sold"}
    assert len(creates) == 1


def test_create_folder_lost_and_absent_is_uncertain(tmp_path):
    # Ответ потерялся, папки пока не видно: DeviantArt мог ещё её создавать — «точно нет» сказать нельзя,
    # иначе повтор сделал бы вторую такую же.
    lists = []
    routes = {("POST", f"{API}/gallery/folders/create"): lambda r: httpx.Response(503),
              ("GET", f"{API}/gallery/folders"): lambda r: lists.append(1) or httpx.Response(
                  200, json={"results": [], "has_more": False})}
    client, _ = real_client(tmp_path, routes)
    with pytest.raises(UncertainPublish):
        client.create_folder("Sold", username=ME)
    assert len(lists) == 2  # перечитала дважды, с паузой


def test_folder_moves_go_in_chunks_of_24(tmp_path):
    seen = []
    routes = {("POST", f"{API}/gallery/folders/move_deviations"): lambda r: seen.append(form(r)) or httpx.Response(
        200, json={"success": True})}
    client, _ = real_client(tmp_path, routes)
    client.move_deviations("F-a", "F-b", [f"U{i}" for i in range(30)])
    assert [len(s["deviationids[]"]) for s in seen] == [24, 6]
    assert seen[0]["source_folderid"] == ["F-a"] and seen[0]["target_folderid"] == ["F-b"]


def test_rename_folder_resends_description(tmp_path):
    seen = []
    routes = {("POST", f"{API}/gallery/folders/update"): lambda r: seen.append(form(r)) or httpx.Response(
        200, json={"success": True})}
    client, _ = real_client(tmp_path, routes)
    client.rename_folder("F-a", "Sold  adopts", "old text")
    assert seen == [{"folderid": ["F-a"], "name": ["Sold adopts"], "description": ["old text"]}]


def test_edit_state_refuses_unreadable_18_and_license(tmp_path):
    for broken, what in (({k: v for k, v in META.items() if k != "mature_classification"}, "пометки 18+"),
                         ({**META, "mature_classification": ["gore", "Violence"]}, "пометки 18+"),
                         ({k: v for k, v in META.items() if k != "license"}, "лицензию")):
        client, _ = real_client(tmp_path, meta_routes(broken))
        with pytest.raises(DAApiError, match=what) as e:
            client.edit_state("U1")
        assert e.value.code == "edit_unsafe"
    # не 18+: классификация не нужна
    plain = {k: v for k, v in META.items() if k not in ("mature_classification", "mature_level")}
    client, _ = real_client(tmp_path, meta_routes({**plain, "is_mature": False}))
    assert client.edit_state("U1")["mature_classification"] == []


def test_verify_edit_notices_changed_18_details(tmp_path):
    client, _ = real_client(tmp_path, meta_routes(META))
    before = client.edit_state("U1")
    for after in ({**META, "title": "Moth [CLOSED]", "mature_level": "strict"},
                  {**META, "title": "Moth [CLOSED]", "mature_classification": []}):
        client2, _ = real_client(tmp_path, meta_routes(after))
        assert any("18+" in x for x in client2.verify_edit("U1", before, "Moth [CLOSED]"))
    client3, _ = real_client(tmp_path, meta_routes({**META, "title": "Moth [CLOSED]"}))
    assert client3.verify_edit("U1", before, "Moth [CLOSED]") == []


def test_create_folder_lost_response_never_adopts_a_similar_folder(tmp_path):
    # «Sold-Out» — другая папка, не та «Sold Out», которую она одобрила
    routes = {("POST", f"{API}/gallery/folders/create"): lambda r: httpx.Response(503),
              ("GET", f"{API}/gallery/folders"): lambda r: httpx.Response(200, json={
                  "results": [{"folderid": "F-x", "name": "Sold-Out", "parent": None}], "has_more": False})}
    client, _ = real_client(tmp_path, routes)
    with pytest.raises(UncertainPublish):
        client.create_folder("Sold Out", username=ME)
    routes[("GET", f"{API}/gallery/folders")] = lambda r: httpx.Response(200, json={
        "results": [{"folderid": "F-y", "name": "sold  out", "parent": None}], "has_more": False})
    client, _ = real_client(tmp_path, routes)
    assert client.create_folder("Sold Out", username=ME)["folder_id"] == "F-y"


def test_unreadable_answer_to_a_write_is_uncertain(tmp_path):
    routes = {("POST", f"{API}/comments/post/deviation/U1"): lambda r: httpx.Response(200, text="<html>oops")}
    client, _ = real_client(tmp_path, routes)
    with pytest.raises(UncertainPublish):
        client.post_comment("U1", "Congrats!")
    assert posting._uncertain(DAApiError("DeviantArt вернул что-то странное.", "bad_response", status=200))


def test_folder_listing_without_description_is_none(tmp_path):
    routes = {("GET", f"{API}/gallery/folders"): lambda r: httpx.Response(200, json={
        "results": [{"folderid": "F-a", "name": "A"}, {"folderid": "F-b", "name": "B", "description": ""}],
        "has_more": False})}
    client, _ = real_client(tmp_path, routes)
    assert [f["description"] for f in client.gallery_folders(ME)] == [None, ""]


def test_refused_folder_rename_is_an_error(tmp_path):
    routes = {("POST", f"{API}/gallery/folders/update"): lambda r: httpx.Response(200, json={"success": False})}
    client, _ = real_client(tmp_path, routes)
    with pytest.raises(DAApiError, match="не переименовал"):
        client.rename_folder("F-a", "Sold", "")


def test_comment_post_lost_is_uncertain(tmp_path):
    routes = {("POST", f"{API}/comments/post/deviation/U1"): lambda r: httpx.Response(500)}
    client, _ = real_client(tmp_path, routes)
    with pytest.raises(UncertainPublish):
        client.post_comment("U1", "Congrats!")


def test_comment_refused_with_code_0_frees_the_journal(conn, fake):
    """Отказ DeviantArt с error_code 0 — ответ, а не потеря: журнал не держит «unknown», повтор возможен."""
    fake.fail["post_comment"] = DAApiError("DeviantArt не принял запрос", 0, "", 400)
    with pytest.raises(DAApiError):
        posting.comment_once(conn, fake, ME, "U101", "Please pay")
    assert not posting._comment_journaled(conn, "U101", "Please pay")
    del fake.fail["post_comment"]
    assert posting.comment_once(conn, fake, ME, "U101", "Please pay")[0] == "posted"


# --- Инструменты для Claude ---

@pytest.fixture
def server(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "srv.sqlite3"))
    cfg = Config(configured=True)
    cfg.posting.da_client_id = "123"
    cfg.deviantart.my_username = ME
    save_config(cfg)
    with db.session() as c:
        add_lot(c, 101, "Moth adopt [OPEN]")
        add_lot(c, 102, "(open) Lunar moth", winner="kate", amount=25.0)
    return tmp_path


def test_tools_end_to_end(server, fake, monkeypatch):
    listing = mcp_server.list_my_open_auctions()
    assert "Moth adopt [OPEN]" in listing and "максимум $40" in listing and "prepare_gallery:close" in listing
    out = mcp_server.prepare_close_auctions(folder="Sold", comment="Congrats {winner}!")
    assert out.startswith("Черновик ") and "«го»" in out and "submit_draft" in out
    draft_id = out.split()[1].rstrip(":")
    started = []
    monkeypatch.setattr(posting, "start_sending", lambda d: started.append(d))
    monkeypatch.setattr(posting, "wait_for", lambda conn, d, t: dict(drafts.get(conn, d)))
    assert "drafts:status" in mcp_server.submit_draft(draft_id) and started == [draft_id]
    with db.session() as c:  # то, что сделал бы процесс send-draft
        posting.send(c, Config(posting=mcp_server.load_config().posting, deviantart=mcp_server.load_config().deviantart),
                     draft_id, sleep=Sleeps())
    status = mcp_server.draft_status(draft_id)
    assert "Отправлен" in status and "Moth adopt [CLOSED]" in status
    assert "Открытых аукционов" in mcp_server.list_my_open_auctions()


def test_rename_and_folder_tools(server, fake):
    out = mcp_server.prepare_rename_works({url_of(101): "Moth adopt — SOLD"})
    assert out.startswith("Черновик ") and "→ «Moth adopt — SOLD»" in out
    out = mcp_server.prepare_gallery_folders(create=["Sold"], move={"Sold": ["da:102"]})
    assert out.startswith("Черновик ") and "Создам папки: «Sold»" in out
    assert fake.writes() == []


def test_tools_gated_when_sending_off(server, monkeypatch):
    monkeypatch.setattr(posting, "SENDING_ENABLED", False)
    assert mcp_server.prepare_close_auctions() == posting.NOT_YET
    assert mcp_server.prepare_gallery_folders(create=["Sold"]) == posting.NOT_YET
    assert mcp_server.prepare_rename_works({"da:101": "x"}) == posting.NOT_YET
    assert mcp_server.submit_draft("deadbeef") == posting.NOT_YET


def test_settings_tell_claude_it_can(server):
    text = mcp_server.get_settings()
    assert "prepare_gallery:close" in text and "без браузера" in text


def test_tool_descriptions_and_annotations():
    import asyncio

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    gallery = tools["prepare_gallery"]
    assert gallery.annotations.destructive_hint is False  # только черновик
    assert "браузер" in gallery.description and "«го»" in gallery.description
    assert set(gallery.input_schema["properties"]["action"]["enum"]) == {"close", "folders", "rename"}
    assert tools["da_auctions"].annotations.destructive_hint is False
    assert tools["submit_draft"].annotations.destructive_hint is True


# --- Идущий аукцион не закрываем молча ---

def _running_lot(conn, fake, n=104, title="Moth adopt [OPEN] 104"):
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title=title, url=url_of(n), author=ME,
                              lot_type="auction", status="open", own=True, snippet="Auction ends in 3 days",
                              extra={"uuid": f"U{n}"}), now=time.time() - 3600)
    conn.execute("INSERT INTO auctions (deviation_id, owner, top_amount, currency, n_bids, bidders, winner, ended, "
                 "updated_ts, extra) VALUES (?, ?, 25, 'usd', 1, 1, 'kate', 0, ?, ?)",
                 (f"da:{n}", ME, time.time(), json.dumps({"top_user": "kate"})))
    conn.commit()
    fake.add(f"U{n}", title, n)


def test_close_all_leaves_running_auction_alone(conn, cfg, fake):
    _running_lot(conn, fake)
    draft_id, text = close(conn, cfg)
    assert draft_id
    assert "Ещё идут, их не трогаю" in text and "«Moth adopt [OPEN] 104» — до " in text
    payload = json.loads(drafts.get(conn, draft_id)["payload"])
    assert "da:104" not in {w["item_id"] for w in payload["works"]}


def test_close_named_running_auction_warns(conn, cfg, fake):
    _running_lot(conn, fake)
    draft_id, text = close(conn, cfg, works=["da:104"])
    assert draft_id
    assert "⚠ аукцион ещё идёт до " in text and "победителем будет тот, кто лидирует сейчас" in text


def test_close_all_only_running_makes_no_draft(tmp_path, cfg, fake):
    c = db.connect(tmp_path / "only.sqlite3")
    _running_lot(c, fake)
    draft_id, text = close(c, cfg)
    assert draft_id is None and "Закрывать пока нечего" in text
    c.close()
