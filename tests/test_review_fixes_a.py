"""Регрессии после проверки подключения: лимит комментариев при закрытии, напоминания, пинги, чат-чтение треда,
выгрузка. Всё на подделках: ни Chrome, ни DeviantArt, ни сети."""

import os
import time

import pytest
import test_integration_a as ia
from test_gallery_manage import FakeGallery, add_lot, close, go
from test_integration_a import draft_id_of, pinglist_ready, send, unpaid_sale

from mcp_devian import da_api, db, drafts, mcp_server, pinglist, posting, selling
from mcp_devian.config import Config

DAY = 86400
fake, server = ia.fake, ia.server  # общие подделки DA и сервера из test_integration_a


# --- Закрытие аукционов: комментарии тоже под суточным лимитом ---

@pytest.fixture
def gallery(tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setenv("HOME", str(tmp_path))
    f = FakeGallery()
    f.add("U101", "Moth adopt [OPEN]", 101)
    f.add("U102", "(open) Lunar moth", 102)
    monkeypatch.setattr(posting, "da_client", lambda c: f)
    conn = db.connect(tmp_path / "s.sqlite3")
    add_lot(conn, 101, "Moth adopt [OPEN]")
    add_lot(conn, 102, "(open) Lunar moth", winner="kate", amount=25.0)
    cfg = Config(configured=True)
    cfg.posting.da_client_id = "123"
    cfg.deviantart.my_username = "mothy"
    yield conn, cfg, f
    conn.close()


def test_closing_comments_respect_daily_cap(gallery, monkeypatch):
    conn, cfg, f = gallery
    monkeypatch.setattr(posting, "COMMENTS_PER_DAY", 1)
    draft_id, text = close(conn, cfg, folder="", from_folder="")
    assert "Комментариев 2" in text and "позволяет ещё 1" in text
    result = go(conn, cfg, draft_id)
    assert len(f.writes("post_comment")) == 1, result
    assert drafts.get(conn, draft_id)["status"] == "partial"
    assert posting.COMMENT_LIMIT in result
    # названия поменялись у обеих, но вторая без комментария осталась открытой в базе — допишется потом
    assert len(f.writes("edit_deviation")) == 2
    assert db.get_item(conn, "da:101")["status"] == "closed"
    assert db.get_item(conn, "da:102")["status"] != "closed"


def test_closing_comments_stop_when_cap_already_used(gallery, monkeypatch):
    conn, cfg, f = gallery
    monkeypatch.setattr(posting, "COMMENTS_PER_DAY", 1)
    drafts.log_sent(conn, "da_comment", "other", "x", True, "ok", time.time())
    draft_id, _ = close(conn, cfg, folder="", from_folder="")
    go(conn, cfg, draft_id)
    assert f.writes("post_comment") == []


# --- Напоминание об оплате: потерянный ответ — второго не будет ---

def _sent_marks(sale_id):
    with db.session() as c:
        return c.execute("SELECT COUNT(*) FROM payment_reminders WHERE sale_id = ? AND status = 'sent'",
                         (sale_id,)).fetchone()[0]


@pytest.mark.parametrize("exc", [da_api.UncertainPublish("нет ответа", "network"), RuntimeError("boom")])
def test_unknown_reminder_is_not_offered_again(server, fake, exc):
    sale_id = unpaid_sale()
    mcp_server.set_selling_template("payment_reminder", "@{buyer} {amount}, {days}d")
    draft_id = draft_id_of(mcp_server.prepare_payment_reminder(sale_id))
    fake.fail_at, fake.fail_with = 0, exc
    _, row = send(draft_id)
    assert row["status"] == "unknown"
    assert _sent_marks(sale_id) == 1
    with db.session() as c:
        later = selling.payment_reminders(c, now=time.time() + DAY, older_than_days=0)
        assert all(d["sale_id"] != sale_id for d in later["due"])
        assert selling.reminder_block(c, sale_id, time.time() + DAY)


def test_definitely_failed_reminder_unmarks(server, fake):
    sale_id = unpaid_sale()
    mcp_server.set_selling_template("payment_reminder", "@{buyer} {amount}")
    draft_id = draft_id_of(mcp_server.prepare_payment_reminder(sale_id))
    fake.fail_at, fake.fail_with = 0, da_api.DAApiError("bad request", 400)
    _, row = send(draft_id)
    assert row["status"] == "failed" and _sent_marks(sale_id) == 0
    with db.session() as c:
        assert selling.reminder_block(c, sale_id) == ""


def test_reminders_per_sale_are_capped(server, fake):
    sale_id = unpaid_sale()
    mcp_server.set_selling_template("payment_reminder", "@{buyer} {amount}, {days}d")
    with db.session() as c:
        for k in range(selling.REMIND_MAX_PER_SALE):
            selling.mark_reminded(c, sale_id, "sent", time.time() - (10 + k) * DAY)
        assert selling.reminder_block(c, sale_id) == selling.TOO_MANY_REMINDERS
        res = selling.payment_reminders(c, older_than_days=0)
        assert any(s["reason"] == selling.TOO_MANY_REMINDERS for s in res["skipped"])
    assert "Не готовлю" in mcp_server.prepare_payment_reminder(sale_id)


def test_cancelled_reminder_draft_does_not_block_new_one(server, fake):
    sale_id = unpaid_sale()
    mcp_server.set_selling_template("payment_reminder", "@{buyer} {amount}")
    draft_id = draft_id_of(mcp_server.prepare_payment_reminder(sale_id))
    assert "уже есть" in mcp_server.prepare_payment_reminder(sale_id)
    assert mcp_server.cancel_draft(draft_id) == "Отменила."
    mcp_server.set_selling_template("payment_reminder", "@{buyer}, {amount} please")
    assert "please" in mcp_server.prepare_payment_reminder(sale_id)


# --- Пинги: согласие перепроверяется при отправке, ники не застревают ---

def test_ping_skips_comment_when_someone_left_pinglist(server, fake):
    pinglist_ready()
    draft_id = draft_id_of(mcp_server.prepare_ping(posting_work()))
    assert "Убрала: fan00" in mcp_server.manage_pinglist("remove", ["fan00"])
    result, row = send(draft_id)
    assert row["status"] == "sent" and len(fake.posted) == 2, result
    assert all("@fan00" not in body for _, body in fake.posted)
    assert "пропустила" in result and "@fan00" in result
    with db.session() as c:
        held = c.execute("SELECT COUNT(*) FROM pinglist_pending").fetchone()[0]
        assert held == 0 and not c.execute("SELECT 1 FROM pinglist_sent WHERE key = 'fan01'").fetchone()
    # остальные из пропущенного комментария попадут в следующий пинг, ушедшая — нет
    out = mcp_server.prepare_ping(posting_work())
    assert "@fan01" in out and "@fan00" not in out


def test_ping_all_skipped_is_failed(server, fake):
    pinglist_ready()
    draft_id = draft_id_of(mcp_server.prepare_ping(posting_work()))
    mcp_server.manage_pinglist("remove", ["fan00", "fan10", "fan20"])
    _, row = send(draft_id)
    assert row["status"] == "failed" and fake.posted == []


def test_expired_ping_draft_frees_users(server, fake):
    pinglist_ready()
    draft_id = draft_id_of(mcp_server.prepare_ping(posting_work()))
    with db.session() as c:
        # сутки назад: и черновик, и занятые им ники
        c.execute("UPDATE drafts SET created_ts = created_ts - ? WHERE id = ?", (DAY + 60, draft_id))
        c.execute("UPDATE pinglist_pending SET ts = ts - ?", (DAY + 60,))
        c.commit()
        assert pinglist.PENDING_TTL_S > DAY + 60  # без освобождения ники ещё держались бы
        drafts.expire_pending(c)
        assert drafts.get(c, draft_id)["status"] == "cancelled"
    assert draft_id_of(mcp_server.prepare_ping(posting_work()))


def test_old_cancelled_ping_does_not_free_newer_draft(server, fake):
    pinglist_ready()
    first = draft_id_of(mcp_server.prepare_ping(posting_work()))
    mcp_server.cancel_draft(first)
    time.sleep(0.01)
    draft_id_of(mcp_server.prepare_ping(posting_work()))
    assert "все уже" in mcp_server.prepare_ping(posting_work())


def test_pending_ttl_matches_draft_life():
    assert drafts.DRAFT_TTL_S < pinglist.PENDING_TTL_S <= drafts.DRAFT_TTL_S + 3 * 3600


def posting_work():
    from test_integration_a import WORK

    return WORK


# --- Тред Toyhouse в чате: коротко, без минутного ожидания ---

def test_chat_thread_read_is_bounded(server, monkeypatch):
    from mcp_devian import th_auctions as ta

    seen = []

    def fake_read(url, conn=None, **kw):
        seen.append(kw)
        return ta.ThreadRead([], incomplete=True, reason="пусто")

    monkeypatch.setattr(ta, "run_th_thread", fake_read)
    url = "https://toyhou.se/~forums/13.adoption-center/123.lumi-auction"
    mcp_server.th_auction_status(url)
    mcp_server.th_auction_status(url, full=True)
    assert seen[0]["max_pages"] == mcp_server.CHAT_TH_PAGES and seen[0]["lock_wait_s"] == mcp_server.CHAT_CHROME_WAIT_S
    assert seen[1]["max_pages"] is None and seen[1]["full"] is True


# --- Выгрузка: «Загрузки» закрыты — в папку помощника ---

@pytest.mark.skipif(os.geteuid() == 0, reason="root пишет куда угодно")
def test_export_falls_back_when_downloads_is_closed(server, monkeypatch):
    from mcp_devian import export

    unpaid_sale()
    closed = server / "closed"
    closed.mkdir()
    closed.chmod(0o500)
    monkeypatch.setattr(export, "default_dir", lambda: closed)
    monkeypatch.setattr(export, "fallback_dir", lambda: server / "fb")
    try:
        out = mcp_server.export_sales()
    finally:
        closed.chmod(0o700)
    assert "папку помощника" in out and len(list((server / "fb").glob("*.csv"))) == 1
