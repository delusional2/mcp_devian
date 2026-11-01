"""Подключение в чат: Toyhouse (аукционы, спрос, персы), продажи (оплата, пинглист, выгрузка), превью, обновления.

Всё на подделках: ни Chrome, ни DeviantArt, ни сети."""

import asyncio
import json
import time

import pytest
from test_th_characters import CREATE_FORM, EDIT_FORM, TRANSFER_FORM

from mcp_devian import da_api, db, drafts, her_chrome, mcp_server, pinglist, posting, sales, selling
from mcp_devian import th_characters as tc
from mcp_devian import toyhouse as th
from mcp_devian.config import Config, load_config, save_config
from mcp_devian.db import Item

ME = "her"
WORK = f"https://www.deviantart.com/{ME}/art/Moth-5"
THREAD = "https://toyhou.se/~forums/13.adoption-center/123.lumi-auction"
LUMI = "https://toyhou.se/123456.lumi"


class FakeDA:
    """Её DeviantArt для комментариев: пишет, что запостили; падает по заказу на n-м комментарии."""

    def __init__(self):
        self.posted: list[tuple[str, str]] = []
        self.fail_at: int | None = None
        self.fail_with: Exception | None = None
        self.connected = True
        self.scopes_ok = True

    def is_connected(self):
        return self.connected

    def require_scopes(self, *needed):
        if not self.scopes_ok:
            raise da_api.NeedsReconnect(da_api.RECONNECT, "missing_scope")

    def whoami(self):
        return {"username": ME}

    def deviation_uuid_from_url(self, url, max_pages=10, **kw):
        return "U5"

    def deviation_comments(self, uuid):
        return []

    def post_comment(self, uuid, body, reply_to=None):
        if self.fail_at is not None and len(self.posted) == self.fail_at:
            raise self.fail_with
        self.posted.append((uuid, body))
        return {"commentid": f"c{len(self.posted)}"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeDA()
    monkeypatch.setattr(posting, "da_client", lambda cfg: f)
    return f


@pytest.fixture
def server(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)
    monkeypatch.setattr(th, "LOCK_FILE", tmp_path / "browser.lock")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: False)
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "s.sqlite3"))
    cfg = Config(configured=True)
    cfg.deviantart.my_username = ME
    cfg.posting.da_client_id = "1"
    cfg.posting.th_username = ME
    cfg.toyhouse.mode = "chrome"
    save_config(cfg)
    return tmp_path


def draft_id_of(out: str) -> str:
    assert out.startswith("Черновик "), out
    return out.split()[1].rstrip(":")


def send(draft_id, sleeps=None):
    from mcp_devian.config import load_config

    with db.session() as c:
        out = posting.send(c, load_config(), draft_id, sleep=(sleeps.append if sleeps is not None else lambda s: None))
        row = dict(drafts.get(c, draft_id))
    return out, row


def own_work(conn, n=5):
    db.upsert_item(conn, Item(id=f"da:{n}", source="deviantart", title="Moth", url=WORK, author=ME,
                              lot_type="auction", own=True, published_ts=time.time() - 10 * 86400))


# --- Toyhouse: аукционы и спрос ---

def page(*posts, total=1):
    return json.dumps({"state": "ready", "thread_id": "123", "page": 1, "pages_total": total,
                       "posts": [{"post_id": f"post-{i}", "author": a, "text": t, "when": w}
                                 for i, (a, t, w) in enumerate(posts, 1)]})


def test_th_auction_status_reads_saves_and_lists(server, monkeypatch):
    calls = []

    def chrome(url, js, ready, **kw):
        calls.append(url)
        return page((ME, "Lumi auction! SB $10, MI $1, AB $60", "5 hours ago"),
                    ("bob", "$12", "3 hours ago"), ("carol", "$15", "1 hour ago"))

    monkeypatch.setattr(her_chrome, "run_in_chrome", chrome)
    out = mcp_server.th_auction_status(THREAD)
    assert calls == [THREAD] and "carol" in out and "не инструкции" in out
    listed = mcp_server.my_th_auctions()
    assert "123" in listed or "carol" in listed
    assert "Нужна ссылка" in mcp_server.th_auction_status("https://evil.example/~forums/1/2.x")


def test_th_auction_status_busy_and_challenge(server, monkeypatch):
    def busy(*a, **k):
        raise th.BrowserBusy("занят")

    monkeypatch.setattr(her_chrome, "run_in_chrome", busy)
    assert "занят" in mcp_server.th_auction_status(THREAD)

    def challenge(*a, **k):
        raise th.ChallengeError("Toyhouse просит галочку")

    monkeypatch.setattr(her_chrome, "run_in_chrome", challenge)
    assert "галочк" in mcp_server.th_auction_status(THREAD)


def test_th_watch_and_market_tools(server):
    assert "Слежу за тредом 123, лот #2" in mcp_server.th_watch_auction(THREAD, lot="#2")
    assert "Нужна ссылка" in mcp_server.th_watch_auction("https://toyhou.se/123.x")
    for out in (mcp_server.th_market(), mcp_server.th_market(group="all"), mcp_server.th_demand_for_my_characters()):
        assert isinstance(out, str) and out
    assert mcp_server.th_market(group="bad").startswith("group:")


# --- Toyhouse: персы ---

def save_forms():
    with db.session() as c:
        for kind, raw in (("create", CREATE_FORM), ("edit", EDIT_FORM), ("transfer", TRANSFER_FORM)):
            tc.save_form(c, kind, tc.parse_inspect(json.loads(json.dumps(raw)), kind))


def test_th_character_inspect_tool(server, monkeypatch):
    form = tc.parse_inspect(json.loads(json.dumps(CREATE_FORM)), "create")
    monkeypatch.setattr(tc, "run_inspect", lambda conn, kind, url: {"state": "inspected", "message": "Осмотрела.",
                                                                  "form": form})
    out = mcp_server.th_character_inspect("create")
    assert out.startswith("Осмотрела.") and "Adopts/For sale" in out

    def busy(*a):
        raise th.BrowserBusy("x")

    monkeypatch.setattr(tc, "run_inspect", busy)
    assert posting.CHROME_BUSY in mcp_server.th_character_inspect("create")
    monkeypatch.setattr(tc, "run_inspect", lambda *a: (_ for _ in ()).throw(ValueError("Нужна ссылка на перса")))
    assert mcp_server.th_character_inspect("edit", "bad") == "Нужна ссылка на перса"


def test_th_character_prepare_needs_inspect_then_drafts_and_fills(server, monkeypatch):
    img = server / "Downloads" / "lumi.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    out = mcp_server.th_character_prepare("Lumi", [str(img)], folder="Adopts/For sale")
    assert not out.startswith("Черновик") and "осмотр" in out.lower()
    save_forms()
    assert "Adopts/For sale" in mcp_server.th_character_folders()
    out = mcp_server.th_character_prepare("Lumi", [str(img)], folder="Adopts/For sale", price=25, status="for sale",
                                          profile="My words")
    draft_id = draft_id_of(out)
    assert "My words" in out and "submit_draft" in out and "жмёт она" in out
    with db.session() as c:
        assert "по расписанию не делаю" in posting.approve_draft(c, Config(), draft_id)
    seen = []
    monkeypatch.setattr(tc, "run_plan", lambda plan: seen.append(plan) or {"state": "filled", "message": "Заполнила."})
    result, row = send(draft_id)
    assert result == "Заполнила." and row["status"] == "sent" and seen[0]["kind"] == "create"
    with db.session() as c:
        assert posting.FILLED_NAME in posting.describe(drafts.get(c, draft_id))
    assert "Заполнено в её Chrome" in mcp_server.draft_status(draft_id)


@pytest.mark.parametrize("exc,status", [(th.BrowserBusy("busy"), "pending"), (th.ToyhouseError("нет вкладки"), "failed")])
def test_th_character_send_errors(server, monkeypatch, exc, status):
    save_forms()
    draft_id = draft_id_of(mcp_server.th_character_listing(LUMI, "not for sale"))

    def boom(plan):
        raise exc

    monkeypatch.setattr(tc, "run_plan", boom)
    _, row = send(draft_id)
    assert row["status"] == status


def test_th_character_transfer_listing_move(server, monkeypatch):
    assert not mcp_server.th_character_transfer(LUMI, "alice").startswith("Черновик")  # формы ещё нет
    save_forms()
    out = mcp_server.th_character_transfer(LUMI, "alice", name="Lumi")
    assert draft_id_of(out) and "alice" in out and "Transfer" in out and "нет в журнале" in out
    assert draft_id_of(mcp_server.th_character_listing(LUMI, "not for sale"))
    out = mcp_server.th_character_move([LUMI, "https://toyhou.se/2.b"], "OCs")
    assert draft_id_of(out) and "2 перс" in out
    monkeypatch.setattr(tc, "run_plan", lambda plan: {"state": "partial", "message": "1 из 2"})
    _, row = send(draft_id_of(out))
    assert row["status"] == "partial"


def test_th_character_tools_gated(server, monkeypatch):
    monkeypatch.setattr(posting, "SENDING_ENABLED", False)
    for out in (mcp_server.th_character_prepare("Lumi", []), mcp_server.th_character_transfer(LUMI, "alice"),
                mcp_server.th_character_listing(LUMI, "nfs"), mcp_server.th_character_move([LUMI], "OCs"),
                mcp_server.prepare_payment_reminder("1"), mcp_server.prepare_ping(WORK)):
        assert out == posting.NOT_YET


# --- Продажи: непроданное, сроки, оплата ---

def test_selling_reads_on_empty_db(server):
    for out in (mcp_server.unsold_lots(), mcp_server.auction_deadlines(), mcp_server.unpaid_sales()):
        assert isinstance(out, str) and out


def unpaid_sale():
    with db.session() as c:
        own_work(c)
        sale_id = sales.add_sale(c, title="Moth", amount=20, currency="usd", platform="deviantart",
                                 format="auction", buyer="alice", item_id="da:5", now=time.time() - 5 * 86400)
    return sale_id


def test_payment_reminder_draft_send_and_once(server, fake):
    sale_id = unpaid_sale()
    assert "alice" in mcp_server.unpaid_sales()
    assert "шаблона" in mcp_server.prepare_payment_reminder(sale_id)
    assert "Не сохранила" in mcp_server.set_selling_template("payment_reminder", "Hi {nope}")
    assert "сохранён" in mcp_server.set_selling_template("payment_reminder", "@{buyer} reminder: {amount}, {days}d")
    out = mcp_server.prepare_payment_reminder(sale_id)
    draft_id = draft_id_of(out)
    assert "@alice reminder: $20" in out and WORK in out
    assert "уже есть" in mcp_server.prepare_payment_reminder(sale_id)  # второй черновик не нужен
    result, row = send(draft_id)
    assert row["status"] == "sent" and fake.posted == [("U5", "@alice reminder: $20, 5d")]
    with db.session() as c:
        assert selling.reminder_block(c, sale_id)  # три дня тишины
        assert drafts.count_sent(c, "da_comment", 0) == 1


def test_payment_reminder_lost_response_is_unknown(server, fake):
    sale_id = unpaid_sale()
    mcp_server.set_selling_template("payment_reminder", "{buyer}: {amount}")
    draft_id = draft_id_of(mcp_server.prepare_payment_reminder(sale_id))
    fake.fail_at, fake.fail_with = 0, da_api.UncertainPublish("нет ответа", "network")
    _, row = send(draft_id)
    assert row["status"] == "unknown"
    with db.session() as c:
        assert drafts.count_sent(c, "da_comment", 0) == 1  # место занято: вслепую не повторим


def test_comment_drafts_need_connection(server, fake):
    sale_id = unpaid_sale()
    mcp_server.set_selling_template("payment_reminder", "{buyer}: {amount}")
    fake.scopes_ok = False
    assert mcp_server.prepare_payment_reminder(sale_id) == da_api.RECONNECT
    fake.scopes_ok, fake.connected = True, False
    assert "da_connect" in mcp_server.prepare_payment_reminder(sale_id)


# --- Пинглист ---

FANS = [f"fan{i:02d}" for i in range(25)]


def pinglist_ready():
    with db.session() as c:
        own_work(c)
    assert "fan00" in mcp_server.manage_pinglist("add", FANS)
    mcp_server.set_selling_template("pinglist", "New: {title} {url}\n{mentions}")


def test_manage_pinglist(server):
    assert mcp_server.manage_pinglist() == "Пинглист пуст."
    assert "Добавила: amy" in mcp_server.manage_pinglist("add", ["@amy", "bad nick!"])
    assert "amy" in mcp_server.manage_pinglist("show")
    assert "Убрала: amy" in mcp_server.manage_pinglist("remove", ["amy"])
    assert "Не ники" in mcp_server.manage_pinglist("add", ["!!"])


def test_ping_draft_sends_paced_and_never_twice(server, fake):
    pinglist_ready()
    out = mcp_server.prepare_ping(WORK)
    draft_id = draft_id_of(out)
    assert out.count("Комментарий ") == 3 and "@fan24" in out
    sleeps: list[float] = []
    result, row = send(draft_id, sleeps)
    assert row["status"] == "sent" and len(fake.posted) == 3
    assert len(sleeps) == 2 and all(posting.PING_PACE_S[0] <= s <= posting.PING_PACE_S[1] for s in sleeps)
    assert "все уже упомянуты" in mcp_server.prepare_ping(WORK)


def test_ping_daily_limit_trims_draft(server, fake, monkeypatch):
    monkeypatch.setattr(posting, "PINGS_PER_DAY", 2)
    pinglist_ready()
    out = mcp_server.prepare_ping(WORK)
    assert out.count("Комментарий ") == 2 and "Не вошли" in out
    _, row = send(draft_id_of(out))
    assert row["status"] == "sent"
    assert "лимит" in mcp_server.prepare_ping(WORK)


def test_ping_cancel_frees_users(server):
    pinglist_ready()
    draft_id = draft_id_of(mcp_server.prepare_ping(WORK))
    assert "все уже" in mcp_server.prepare_ping(WORK)  # заняты черновиком
    assert mcp_server.cancel_draft(draft_id) == "Отменила."
    assert draft_id_of(mcp_server.prepare_ping(WORK))


def test_ping_refusal_mid_run_is_partial_and_releases_rest(server, fake):
    pinglist_ready()
    draft_id = draft_id_of(mcp_server.prepare_ping(WORK))
    fake.fail_at, fake.fail_with = 1, da_api.DAApiError("bad request", 400)
    _, row = send(draft_id)
    assert row["status"] == "partial" and len(fake.posted) == 1
    with db.session() as c:
        sent = c.execute("SELECT COUNT(*) FROM pinglist_sent").fetchone()[0]
        held = c.execute("SELECT COUNT(*) FROM pinglist_pending").fetchone()[0]
    assert sent == 10 and held == 0


def test_ping_needs_her_known_work(server):
    pinglist_ready()
    assert "нет в базе" in mcp_server.prepare_ping("https://www.deviantart.com/x/art/Other-99")


def test_running_ping_can_be_stopped(server):
    pinglist_ready()
    draft_id = draft_id_of(mcp_server.prepare_ping(WORK))
    with db.session() as c:
        c.execute("UPDATE drafts SET status = 'sending', sent_ts = ? WHERE id = ?", (time.time(), draft_id))
        c.commit()
        assert posting.request_stop(c, draft_id)


# --- Выгрузка, превью, обновления ---

def test_export_sales_writes_csv(server, monkeypatch):
    from mcp_devian import export

    unpaid_sale()
    monkeypatch.setattr(export, "default_dir", lambda: server / "out")
    out = mcp_server.export_sales(include_lots=True)
    assert "Продажи: 1 строк" in out and "Лоты:" in out
    assert len(list((server / "out").glob("*.csv"))) == 2


def png(path, size=(1600, 900)):
    from PIL import Image

    Image.new("RGB", size, (200, 100, 50)).save(path)
    return path


def test_prepare_preview_tool(server):
    src = png(server / "Downloads" / "moth.png")
    before = src.read_bytes()
    out = mcp_server.prepare_preview(str(src))
    assert "Оригинал не тронут" in out and "«her»" in out and src.read_bytes() == before
    outside = png(server / "elsewhere.png")
    assert not mcp_server.prepare_preview(str(outside)).startswith("Превью")


def test_preview_tool_uses_her_watermark_settings(server, monkeypatch):
    """«Сделай превью с водяным знаком» после update_settings(watermark_*) — её настройки, а не ник плиткой 35%."""
    from mcp_devian import media

    cfg = load_config()
    cfg.posting.watermark_text, cfg.posting.watermark_position, cfg.posting.watermark_opacity = "MOTHY", "bottom-right", 50
    save_config(cfg)
    seen = {}
    monkeypatch.setattr(media, "prepare_preview", lambda path, **kw: seen.update(kw) or {"summary": "ok"})
    src = png(server / "Downloads" / "moth.png")
    assert mcp_server.prepare_preview(str(src)) == "ok"
    assert (seen["text"], seen["position"], seen["opacity"]) == ("MOTHY", "bottom-right", 50)
    mcp_server.prepare_preview(str(src), text="other", position="center", opacity=20)
    assert (seen["text"], seen["position"], seen["opacity"]) == ("other", "center", 20)  # её слово в запросе главнее


def test_publish_with_watermark_uses_preview(server, monkeypatch):
    src = png(server / "Downloads" / "moth.png")
    seen = []

    def fake_prepare(c, cfg, file_path, *a, **k):
        seen.append(file_path)
        return "abcd1234", f"Файл: {file_path}"

    monkeypatch.setattr(posting, "prepare_da_publish", fake_prepare)
    out = mcp_server.prepare_deviantart_publish(str(src), "Moth", watermark=True)
    assert out.startswith("Превью:") and "Черновик abcd1234" in out
    assert seen[0] != str(src) and "-preview" in seen[0]
    out = mcp_server.prepare_deviantart_publish(str(src), "Moth")
    assert seen[1] == str(src)


def test_batch_watermark_per_work_and_empty_nick(server, monkeypatch):
    a, b = png(server / "Downloads" / "a.png"), png(server / "Downloads" / "b.png")
    got = []
    monkeypatch.setattr(posting, "prepare_da_batch",
                        lambda c, cfg, items, schedule=None: got.extend(items) or (["x"], "Партия"))
    out = mcp_server.prepare_deviantart_batch([{"file_path": str(a), "title": "A", "watermark": True},
                                               {"file_path": str(b), "title": "B"}])
    assert "1) Превью" in out and "-preview" in got[0]["file_path"] and got[1]["file_path"] == str(b)
    assert "watermark" not in got[0]
    from mcp_devian.config import editing

    with editing() as cfg:
        cfg.deviantart.my_username = ""
        save_config(cfg)
    assert "подпись" in mcp_server.prepare_deviantart_publish(str(a), "A", watermark=True)


def test_get_settings_mentions_update_and_never_fails(server, monkeypatch):
    from mcp_devian import updates

    monkeypatch.setattr(updates, "check_for_update", lambda: "Есть обновление 9.9.9 (у тебя 0.4.0).")
    assert mcp_server.get_settings().endswith("Есть обновление 9.9.9 (у тебя 0.4.0).")

    def broken():
        raise RuntimeError("сеть")

    monkeypatch.setattr(updates, "check_for_update", broken)
    assert "Настройка пройдена" in mcp_server.get_settings()


# --- Пометки инструментов ---

def test_new_tool_annotations():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    read_only = ("get_settings", "findings", "market", "forecast", "advice")
    # th_character и th_auctions открывают её Chrome или окно бота — спрашивать каждый раз.
    external = ("th_auctions", "th_character", "th_thread", "check", "accounts", "set_background_checks")
    safe = ("sales", "pinglist", "set_template", "images", "characters", "drafts", "feedback", "da_groups",
            "prepare_gallery", "prepare_toyhouse_reply", "prepare_deviantart_publish", "update_settings", "dismiss",
            "da_auctions")
    for name in read_only:
        assert tools[name].annotations.read_only_hint is True, name
    for name in external + safe:
        a = tools[name].annotations
        assert a.read_only_hint is False and a.destructive_hint is False, name
    for name in external:
        assert tools[name].annotations.open_world_hint is True, name
    for name in (*read_only, *external, *safe):
        assert len(tools[name].description) < 400, name
    assert tools["submit_draft"].annotations.destructive_hint is True
    assert pinglist.MAX_PER_COMMENT == 10
