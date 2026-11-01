"""0.5: статус, журнал и предложение под её работой — только по её «го», с лимитами."""

import pytest

from mcp_devian import da_api, db, drafts, pinglist, posting
from mcp_devian.config import Config
from mcp_devian.db import Item

NOW = 1_800_000_000.0
DAY = 86400
MINE = "https://www.deviantart.com/mothy/art/Moth-adopt-100"


class FakeClient:
    def __init__(self, author="mothy", scopes=None):
        self.author = author
        self.scopes = scopes
        self.comments: list[tuple[str, str]] = []
        self.statuses: list[tuple[str, str | None]] = []
        self.journals: list[dict] = []

    def is_connected(self):
        return True

    def require_scopes(self, *needed):
        if self.scopes is not None and any(s not in self.scopes for s in needed):
            raise da_api.NeedsReconnect(da_api.RECONNECT, "missing_scope")

    def whoami(self):
        return {"username": "mothy"}

    def deviation_comments(self, uuid):
        return []

    def post_comment(self, uuid, text):
        self.require_scopes(*da_api.COMMENT_SCOPES)
        self.comments.append((uuid, text))
        return {"commentid": f"c{len(self.comments)}"}

    def deviations_brief(self, uuids):
        return {u: {"title": "Moth adopt", "author": self.author, "galleries": []} for u in uuids}

    def deviation_uuid_from_url(self, url, max_pages=10):
        return "U-100"

    def post_status(self, body, deviationid=None):
        self.require_scopes(*da_api.MANAGE_SCOPES)
        self.statuses.append((body, deviationid))
        return {"statusid": f"s{len(self.statuses)}"}

    def create_journal(self, title, body, is_mature=False, tags=()):
        self.require_scopes(*da_api.MANAGE_SCOPES)
        self.journals.append({"title": title, "body": body, "tags": list(tags)})
        return {"deviationid": f"J{len(self.journals)}", "url": "https://www.deviantart.com/mothy/journal/x-1"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(posting, "da_client", lambda cfg: client)
    return client


@pytest.fixture
def cfg():
    c = Config()
    c.posting.da_client_id = "CID"
    c.deviantart.my_username = "mothy"
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "promo.sqlite3")
    db.upsert_item(c, Item(id="da:100", source="deviantart", title="Moth adopt", url=MINE, own=True,
                           lot_type="auction", published_ts=NOW - 3 * DAY, extra={"uuid": "U-100"}), now=NOW)
    c.commit()
    pinglist.add(c, ["fan1", "fan2", "fan3", "fan4", "fan5", "fan6", "fan7"], now=NOW - DAY)
    yield c
    c.close()


def send(conn, cfg, draft_id, now=NOW):
    return posting.send(conn, cfg, draft_id, now=now)


# --- предложение (da_offer) ---

def test_offer_refuses_foreign_deviation_at_prepare(conn, cfg, fake):
    d, text = posting.prepare_offer(conn, cfg, "https://www.deviantart.com/other/art/X-5", ["fan1"], "Hi {mentions}!",
                                    now=NOW)
    assert d is None and "не её работа" in text
    assert fake.comments == []


def test_offer_refuses_foreign_deviation_at_send(conn, cfg, fake):
    d, preview = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hi {mentions}! New moth is up", now=NOW)
    assert d and "@fan1" in preview and "Hi @fan1!" in preview
    fake.author = "someone_else"
    assert "не её работа" in send(conn, cfg, d)
    assert fake.comments == [] and drafts.count_sent(conn, "da_offer", 0) == 0
    assert drafts.get(conn, d, NOW)["status"] == "failed"


def test_offer_only_pinglist_or_buyers(conn, cfg, fake):
    d, text = posting.prepare_offer(conn, cfg, MINE, ["stranger1"], "Hi {mentions}", now=NOW)
    assert d is None and "незнакомым" in text
    conn.execute("INSERT INTO sales (id, platform, title, buyer, status, created_ts) "
                 "VALUES ('s1', 'deviantart', 'Old moth', 'Stranger1', 'done', ?)", (NOW - 30 * DAY,))
    conn.commit()
    d, _ = posting.prepare_offer(conn, cfg, MINE, ["stranger1"], "Hi {mentions}", now=NOW)
    assert d  # прошлый покупатель — можно
    pinglist.remove(conn, ["fan1"], now=NOW)
    d, text = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hi {mentions}", now=NOW)
    assert d is None and "не надо" in text


def test_offer_daily_cap_and_one_log_row_per_user(conn, cfg, fake):
    cfg.posting.outreach_per_day = 50  # настройка может только снизить потолок
    users = ["fan1", "fan2", "fan3", "fan4", "fan5", "fan6"]
    d, text = posting.prepare_offer(conn, cfg, MINE, users, "Hi {mentions}", now=NOW)
    assert d is None and "не больше чем 5" in text
    d, _ = posting.prepare_offer(conn, cfg, MINE, users[:5], "Hi {mentions}", now=NOW)
    assert "предложение написано" in send(conn, cfg, d)
    assert len(fake.comments) == 1 and fake.comments[0][0] == "U-100"
    rows = conn.execute("SELECT target, ref FROM sent_log WHERE kind = 'da_offer'").fetchall()
    assert sorted(r["target"] for r in rows) == users[:5] and {r["ref"] for r in rows} == {"da:100"}
    d, text = posting.prepare_offer(conn, cfg, MINE, ["fan6"], "Hi {mentions}", now=NOW + 3600)
    assert d is None and "не больше чем 5" in text
    cfg.posting.outreach_per_day = 2
    d, text = posting.prepare_offer(conn, cfg, MINE, ["fan6"], "Hi {mentions}", now=NOW + 2 * DAY)
    assert d  # новый день


def test_offer_cooldown_14_days_per_user(conn, cfg, fake):
    d, _ = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hi {mentions}", now=NOW)
    send(conn, cfg, d)
    d, text = posting.prepare_offer(conn, cfg, MINE, ["FAN1"], "Hello again {mentions}", now=NOW + 3 * DAY)
    assert d is None and "14 дн" in text
    d, _ = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hello again {mentions}", now=NOW + 15 * DAY)
    assert d


def test_offer_rechecked_at_send(conn, cfg, fake):
    from mcp_devian import outreach

    d1, _ = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hi {mentions}", now=NOW)
    d2, text = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hey {mentions}", now=NOW)
    assert d2 is None and "черновик" in text  # второй черновик тому же человеку не готовим
    outreach.mark_contacted(conn, ["fan1"], "da_message_manual", now=NOW + 10)  # она написала ему сама
    assert "14 дн" in send(conn, cfg, d1, now=NOW + 60)
    assert fake.comments == []


# --- регрессии проверки W4 ---

def test_offer_text_cannot_mention_a_stranger(conn, cfg, fake):
    """Регрессия: @ник, вписанный прямо в текст, уходил без проверки — незнакомому приходило уведомление."""
    d, text = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "@randomstranger look! {mentions}", now=NOW)
    assert d is None and "randomstranger" in text and "незнакомым" in text
    d, _ = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "@fan2 and {mentions}, new moth! by @mothy", now=NOW)
    assert d  # fan2 из пинглиста — проверен и стал получателем; её собственный ник не в счёт
    import json

    users = json.loads(drafts.get(conn, d, NOW)["payload"])["users"]
    assert sorted(u.lower() for u in users) == ["fan1", "fan2"]
    assert "написано" in send(conn, cfg, d)
    assert drafts.count_sent(conn, "da_offer", 0, target="fan2") == 1


def test_offer_send_refuses_unlisted_mention_in_stored_text(conn, cfg, fake):
    d, _ = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hi {mentions}", now=NOW)
    import json

    row = drafts.get(conn, d, NOW)
    payload = json.loads(row["payload"])
    payload["text"] += " cc @randomstranger"
    conn.execute("UPDATE drafts SET payload = ? WHERE id = ?", (json.dumps(payload), d))
    conn.commit()
    assert "randomstranger" in send(conn, cfg, d)
    assert fake.comments == [] and drafts.count_sent(conn, "da_offer", 0) == 0


def test_toyhouse_buyer_is_not_a_deviantart_buyer(conn, cfg, fake):
    """Регрессия: покупатель с Toyhouse считался покупателем на DA — на DA этот ник может быть чужим."""
    conn.execute("INSERT INTO sales (id, platform, title, buyer, status, created_ts) "
                 "VALUES ('s9', 'toyhouse', 'Moth', 'thname', 'done', ?)", (NOW - 30 * DAY,))
    conn.commit()
    d, text = posting.prepare_offer(conn, cfg, MINE, ["thname"], "Hi {mentions}", now=NOW)
    assert d is None and "незнакомым" in text


def test_offer_refuses_minor(conn, cfg, fake):
    from mcp_devian import buyers

    buyers.ensure(conn)
    buyers._upsert_contact(conn, "fan1", "deviantart", 1)
    conn.commit()
    d, text = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hi {mentions}", now=NOW)
    assert d is None and "младше 18" in text
    d, _ = posting.prepare_offer(conn, cfg, MINE, ["fan2"], "Hi {mentions}", now=NOW)
    buyers._upsert_contact(conn, "fan2", "deviantart", 1)  # узнали после черновика
    conn.commit()
    assert "младше 18" in send(conn, cfg, d) and fake.comments == []


def test_offer_cap_counts_manual_offers(conn, cfg, fake):
    """Регрессия: 5 предложений руками за сутки + 2 отметки проходили — 7 при лимите 5."""
    from mcp_devian import outreach

    outreach.mark_contacted(conn, ["a1", "a2", "a3", "a4", "a5"], "da_message_manual", now=NOW - 3600)
    d, text = posting.prepare_offer(conn, cfg, MINE, ["fan1", "fan2"], "Hi {mentions}", now=NOW)
    assert d is None and "не больше чем 5" in text and "уже 5" in text


def test_offer_cap_counts_pending_drafts(conn, cfg, fake):
    d1, _ = posting.prepare_offer(conn, cfg, MINE, ["fan1", "fan2", "fan3"], "Hi {mentions}", now=NOW)
    assert d1
    d2, text = posting.prepare_offer(conn, cfg, MINE, ["fan4", "fan5", "fan6"], "Hi {mentions}", now=NOW)
    assert d2 is None and "уже 3" in text


def test_offer_refuses_recently_pinged(conn, cfg, fake):
    pinglist.mark_pinged(conn, "da:777", ["fan1"], now=NOW - 3600)
    d, text = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hi {mentions}", now=NOW)
    assert d is None and "пинг" in text
    pinglist.mark_pinged(conn, "da:100", ["fan2"], now=NOW - 5 * DAY)  # под этой же работой — всегда нет
    d, text = posting.prepare_offer(conn, cfg, MINE, ["fan2"], "Hi {mentions}", now=NOW)
    assert d is None and "пинг" in text
    d, _ = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hi {mentions}", now=NOW + 2 * DAY)
    assert d


def test_offer_never_sent_without_her_go(conn, cfg, fake):
    d, _ = posting.prepare_offer(conn, cfg, MINE, ["fan1"], "Hi {mentions}", now=NOW)
    assert fake.comments == []
    assert "по расписанию не делаю" in posting.approve_draft(conn, cfg, d, now=NOW)
    assert posting.send_due(conn, cfg, now=NOW + 3600) == []
    assert fake.comments == [] and drafts.get(conn, d, NOW)["status"] == "pending"


def test_offer_appends_missing_mentions_and_shows_them(conn, cfg, fake):
    d, preview = posting.prepare_offer(conn, cfg, MINE, "@fan1, fan2", "New moth for you, @fan1!", now=NOW)
    assert d and preview.count("@fan1") >= 1 and "@fan2" in preview
    assert drafts.get(conn, d, NOW)["kind"] == "da_offer"


# --- статус и журнал ---

def test_status_cap_and_identical_refused(conn, cfg, fake):
    d, preview = posting.prepare_status(conn, cfg, "New moth batch is open!", MINE, now=NOW)
    assert d and "New moth batch is open!" in preview and MINE in preview
    assert "опубликован" in send(conn, cfg, d)
    assert fake.statuses == [("New moth batch is open!", "U-100")]
    d, text = posting.prepare_status(conn, cfg, "new  moth batch is OPEN!", now=NOW + 60)
    assert d is None and "такой же текст" in text
    d, _ = posting.prepare_status(conn, cfg, "Two moths left", now=NOW + 60)
    send(conn, cfg, d, now=NOW + 60)
    d, text = posting.prepare_status(conn, cfg, "Last call", now=NOW + 120)
    assert d is None and "статусов за сутки уже 2" in text
    assert posting.prepare_status(conn, cfg, "Last call", now=NOW + DAY + 200)[0]


def test_status_for_foreign_work_refused(conn, cfg, fake):
    d, text = posting.prepare_status(conn, cfg, "Look", "https://www.deviantart.com/other/art/X-5", now=NOW)
    assert d is None and "не её" in text


def test_journal_cap_one_per_day(conn, cfg, fake):
    d, preview = posting.prepare_journal(conn, cfg, "Adopt sale", "Line 1\nLine <2>", ["adopts", "распродажа"], now=NOW)
    assert d and "Line <2>" in preview and "adopts" in preview
    assert "журнал опубликован" in send(conn, cfg, d)
    assert fake.journals[0]["body"] == "Line 1<br />Line &lt;2&gt;" and fake.journals[0]["tags"] == ["adopts"]
    d, text = posting.prepare_journal(conn, cfg, "Another", "x", now=NOW + 3600)
    assert d is None and "журнал за сутки" in text


def test_journal_long_title_note(conn, cfg, fake):
    d, text = posting.prepare_journal(conn, cfg, "x" * 51, "body", now=NOW)
    assert d is None and "в описании API предел не назван" in text


def test_status_without_permission_asks_reconnect(conn, cfg, fake):
    fake.scopes = set(da_api.SCOPES.split())
    d, text = posting.prepare_status(conn, cfg, "Hi", now=NOW)
    assert d is None and posting.MANAGE_RECONNECT in text


def test_status_permission_lost_after_draft_keeps_her_go(conn, cfg, fake):
    d, _ = posting.prepare_status(conn, cfg, "Hi", now=NOW)
    fake.scopes = set(da_api.SCOPES.split())
    assert posting.MANAGE_RECONNECT in send(conn, cfg, d)
    assert drafts.get(conn, d, NOW)["status"] == "pending"
    assert drafts.count_sent(conn, "da_status", 0) == 0
    fake.scopes = None
    assert "опубликован" in send(conn, cfg, d, now=NOW + 60)


def test_status_lost_response_is_unknown_and_counted(conn, cfg, fake, monkeypatch):
    def lost(body, deviationid=None):
        raise da_api.UncertainPublish("нет ответа", "network")

    monkeypatch.setattr(fake, "post_status", lost)
    d, _ = posting.prepare_status(conn, cfg, "Hi", now=NOW)
    assert "неизвестно" in send(conn, cfg, d)
    assert drafts.get(conn, d, NOW)["status"] == "unknown"
    assert drafts.count_sent(conn, "da_status", NOW - 1) == 1


def test_status_can_be_scheduled_and_goes_on_time(conn, cfg, fake):
    d, _ = posting.prepare_status(conn, cfg, "Evening batch", now=NOW)
    assert posting.send_due(conn, cfg, now=NOW + 60) == [] and fake.statuses == []
    assert "Запланировано" in posting.approve_draft(conn, cfg, d, send_at=NOW + 7200, now=NOW)
    assert posting.send_due(conn, cfg, now=NOW + 3600) == []
    out = posting.send_due(conn, cfg, now=NOW + 7300)
    assert [r["draft_id"] for r in out] == [d] and fake.statuses == [("Evening batch", None)]
