import pytest
from fixtures_th import GOLD_CHARACTERS, NOW

from mcp_devian import buyers, da_api, db, leads, outreach, selling, th_match
from mcp_devian.config import Config, Posting

HOUR, DAY = 3600, 86400
TEMPLATE = "Hi {name}! {title} might fit — {why}: {link}"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "l.sqlite3")
    yield c
    c.close()


def cfg(**kw):
    return Config(posting=Posting(characters=list(GOLD_CHARACTERS), **kw))


def set_offer(conn, text=TEMPLATE):
    selling.ensure(conn)
    conn.execute("INSERT OR REPLACE INTO selling_templates (kind, text, updated_ts) VALUES ('offer', ?, ?)",
                 (text, NOW))
    conn.commit()


def lf_item(conn, tid, title, author="buyer", seen=NOW, posts=2):
    item = db.Item(id=tid, source="toyhouse", title=title, url=f"https://toyhou.se/~forums/13.x/{tid[3:]}.t",
                   author=author, place="Adoption Center", posts=posts, activity_ts=seen - HOUR,
                   published_ts=seen - 2 * HOUR, matched=True, match_kind="lf", match_reason="ищут персов",
                   wants_characters=True, extra={"last_poster": author})
    db.upsert_item(conn, item, seen)
    conn.commit()


# --- Toyhouse ---

def test_strong_matches_only_new_and_above_threshold(conn):
    lf_item(conn, "th:1", "LF: wolves!!", author="A", seen=NOW - 10 * HOUR)    # старый (до since)
    lf_item(conn, "th:2", "LF: wolves", author="B", seen=NOW - HOUR)
    lf_item(conn, "th:3", "LF: robots", author="C", seen=NOW - HOUR)           # не подходит никто
    out = th_match.strong_matches(conn, cfg(), NOW - 6 * HOUR, min_score=40, now=NOW)
    assert [m["id"] for m in out] == ["th:2"]
    assert out[0]["character"] == "Ember"
    assert th_match.strong_matches(conn, cfg(), NOW - 6 * HOUR, min_score=999, now=NOW) == []


def test_th_alerts_once_with_reply_hint(conn):
    lf_item(conn, "th:2", "LF: wolves", author="B", seen=NOW - HOUR)
    lines = leads.th_alerts(conn, cfg(), NOW, since_ts=NOW - 6 * HOUR)
    assert len(lines) == 1
    assert "prepare_toyhouse_reply(thread_id='th:2', character='Ember')" in lines[0]
    assert leads.th_alerts(conn, cfg(), NOW + 60, since_ts=NOW - 6 * HOUR) == []
    lf_item(conn, "th:4", "LF: wolf adopts", author="D", seen=NOW + 100)
    assert len(leads.th_alerts(conn, cfg(), NOW + 200, since_ts=NOW - 6 * HOUR)) == 1
    n = conn.execute("SELECT COUNT(*) FROM leads WHERE source = 'th_lf'").fetchone()[0]
    assert n == 2


def test_th_lead_reply_points_to_toyhouse_pipeline(conn):
    lf_item(conn, "th:2", "LF: wolves", author="B", seen=NOW - HOUR)
    leads.collect_th(conn, cfg(), NOW)
    lid = conn.execute("SELECT id FROM leads").fetchone()[0]
    set_offer(conn)
    res = leads.reply_draft(conn, lid, now=NOW)
    assert not res["ok"] and "prepare_toyhouse_reply" in res["reason"]


# --- DeviantArt ---

class FakeDA:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _request(self, method, path, params=None, **kw):
        self.calls.append(params["tag"])
        return {"results": self.pages.get(params["tag"], []), "has_more": False}

    def post_comment(self, *a, **k):  # pragma: no cover
        raise AssertionError("никаких комментариев через API на чужих работах")


def dev(n, title, author="stranger", age=DAY):
    return {"deviationid": f"U{n}", "url": f"https://www.deviantart.com/{author}/art/x-{n}", "title": title,
            "author": {"username": author}, "published_time": str(int(NOW - age)), "stats": {}}


def test_da_lf_scan_max_calls_and_matched_only(conn):
    client = FakeDA({
        "lookingforadopts": [dev(1, "LF wolf adopts!"), dev(2, "Wolf adopt OPEN #lookingforadopts"),
                             dev(3, "LF robots"), dev(4, "LF wolves", age=40 * DAY), dev(5, "LF wolves", author="her")],
        "lfadopts": [dev(6, "Looking for wolves pls")],
        "lookingforoc": [dev(7, "LF wolf oc")],
    })
    stats = leads.da_lf_scan(conn, client, NOW, max_calls=2, characters=GOLD_CHARACTERS, my_username="her",
                             sleep=lambda s: None)
    assert stats["calls"] == 2 and client.calls == ["lookingforadopts", "lfadopts"]
    rows = conn.execute("SELECT * FROM leads WHERE source = 'da_lf' ORDER BY id").fetchall()
    assert [r["post_id"] for r in rows] == ["1", "6"]
    assert rows[0]["matched_character"] == "Ember"
    # следующий проход берёт теги, которые давно не смотрели
    leads.da_lf_scan(conn, client, NOW + 60, max_calls=1, characters=GOLD_CHARACTERS, sleep=lambda s: None)
    assert client.calls[-1] == "lookingforoc"


def test_da_lf_scan_stops_on_rate_limit(conn):
    class Limited(FakeDA):
        def _request(self, *a, **k):
            self.calls.append("x")
            raise da_api.RateLimited("429")

    c = Limited({})
    assert leads.da_lf_scan(conn, c, NOW, max_calls=2, sleep=lambda s: None)["stopped"] == "RateLimited"
    assert c.calls == ["x"]


def test_da_lf_reply_is_manual(conn):
    client = FakeDA({"lookingforadopts": [dev(1, "LF wolf adopts!")]})
    leads.da_lf_scan(conn, client, NOW, max_calls=1, characters=GOLD_CHARACTERS, sleep=lambda s: None)
    set_offer(conn)
    lid = conn.execute("SELECT id FROM leads").fetchone()[0]
    res = leads.reply_draft(conn, lid, now=NOW)
    assert res["ok"] and res["intent_url"] is None
    assert any("сама" in w for w in res["warnings"])


# --- X ---

def test_add_manual_parses_x_status():
    assert leads.parse_x_url("https://x.com/MothFan_1/status/1850000000000000001") == ("MothFan_1",
                                                                                     "1850000000000000001")
    assert leads.parse_x_url("https://twitter.com/abc/status/123456?s=20") == ("abc", "123456")
    assert leads.parse_x_url("https://x.com/abc") is None


def test_add_manual_dedupe_and_errors(conn):
    a = leads.add_manual(conn, "https://twitter.com/MothFan/status/1850000000000000001?s=20", "LF wolf adopts", NOW,
                         characters=GOLD_CHARACTERS)
    b = leads.add_manual(conn, "https://x.com/MothFan/status/1850000000000000001", "", NOW)
    assert a == b
    row = leads.get(conn, a)
    assert row["author"] == "MothFan" and row["post_id"] == "1850000000000000001" and row["source"] == "x_manual"
    assert row["matched_character"] == "Ember"
    with pytest.raises(leads.LeadError):
        leads.add_manual(conn, "https://example.com/post/1", "", NOW)


def test_x_queries_no_mentions():
    qs = leads.x_queries(["@moth", "Pastel Dragon", "#chibi", "moth"], now=NOW)
    assert all("@" not in q for q in qs)
    assert all("-filter:replies" in q and "since:" in q for q in qs)
    assert '"lf adopts"' in qs[0]
    assert len(qs) == 4  # общий + moth, pastel dragon, chibi (повтор moth убран)


def test_reply_draft_one_per_lead_intent_and_dm(conn):
    set_offer(conn)
    calls = []

    def intent(**kw):
        calls.append(kw)
        return "https://x.com/intent/tweet?..."

    lid = leads.add_manual(conn, "https://x.com/Fan/status/111111", "LF wolf adopts, dm me offers!", NOW,
                           characters=GOLD_CHARACTERS)
    res = leads.reply_draft(conn, lid, intent_builder=intent, now=NOW)
    assert res["ok"] and res["dm_ok"] and res["intent_url"]
    assert calls[0]["in_reply_to"] == "111111" and calls[0]["text"] == res["text"]
    assert "Ember" in res["text"] and "Fan" in res["text"]
    again = leads.reply_draft(conn, lid, intent_builder=intent, now=NOW + 60)
    assert again["already"] and again["text"] == res["text"]
    assert outreach.used_today(conn, NOW + 60) == 1   # второй ответ на тот же лид не занял место

    lid2 = leads.add_manual(conn, "https://x.com/Other/status/222222", "LF wolves", NOW)
    res2 = leads.reply_draft(conn, lid2, now=NOW)
    assert res2["ok"] and not res2["dm_ok"]
    assert not any("личку" in w for w in res2["warnings"])


def test_reply_draft_cooldown_cap_and_optout(conn):
    set_offer(conn)
    lid = leads.add_manual(conn, "https://x.com/Fan/status/1", "LF wolves", NOW)
    lid_same = leads.add_manual(conn, "https://x.com/Fan/status/2", "LF wolves again", NOW)
    assert leads.reply_draft(conn, lid, now=NOW)["ok"]
    outreach.mark_contacted(conn, ["Fan"], "x_reply", f"lead:{lid}", NOW + 60)
    assert leads.get(conn, lid)["status"] == "contacted"
    blocked = leads.reply_draft(conn, lid_same, now=NOW + 3 * DAY)
    assert not blocked["ok"] and "пауза 14 дн" in blocked["reason"]
    # дневной лимит общий с outreach
    for i in range(10, 14):
        lead = leads.add_manual(conn, f"https://x.com/u{i}/status/{i}000", "LF wolves", NOW)
        assert leads.reply_draft(conn, lead, now=NOW + 2 * HOUR)["ok"]
    sixth = leads.add_manual(conn, "https://x.com/u99/status/99000", "LF wolves", NOW)
    capped = leads.reply_draft(conn, sixth, now=NOW + 2 * HOUR)
    assert not capped["ok"] and "лимит" in capped["reason"]
    buyers.opt_out(conn, ["u77"], "said no", NOW)
    nope = leads.add_manual(conn, "https://x.com/u77/status/77000", "LF wolves", NOW)
    assert "не писать" in leads.reply_draft(conn, nope, now=NOW + 5 * DAY)["reason"]


def test_reply_draft_needs_her_template(conn):
    lid = leads.add_manual(conn, "https://x.com/Fan/status/1", "LF wolves", NOW)
    res = leads.reply_draft(conn, lid, now=NOW)
    assert not res["ok"] and res["reason"] == outreach.NO_TEMPLATE


def test_render(conn):
    assert "Лидов" in leads.render(conn, NOW)
    lf_item(conn, "th:2", "LF: wolves", author="B", seen=NOW - HOUR)
    leads.add_manual(conn, "https://x.com/Fan/status/1", "LF wolves", NOW)
    text = leads.render(conn, NOW, cfg=cfg())
    assert text.startswith(leads.UNTRUSTED)
    assert "Toyhouse LF" in text and "X (вставила сама)" in text and "n=2" in text
    assert conn.execute("SELECT alerted FROM leads WHERE source = 'th_lf'").fetchone()[0] == 0


# --- Регрессии по проверке W3 ---

@pytest.mark.parametrize("title", ["Wolf adopt OTA (looking for offers)", "Wolf adopt OTA - LF offers",
                                   "Wolf adopt BTA [buying open]", "Wolf adopt - seeking a home",
                                   "LF bids! wolf adopt auction", "wolf adopt for sale, iso buyers",
                                   "Wolf YCH - looking for owners", "LF offers: wolf oc (rehoming)"])
def test_seller_posts_are_not_buyer_leads(title):
    assert not leads.is_buyer_title(title)


@pytest.mark.parametrize("title", ["LF wolf adopts!", "Looking for wolves pls", "ISO wolf adopts",
                                   "buying adopts: wolves", "LF adopts, budget $30", "WTB wolf oc"])
def test_buyer_posts_still_leads(title):
    assert leads.is_buyer_title(title)


def test_da_lf_scan_skips_sellers_and_survives_bad_tag(conn):
    class Mixed(FakeDA):
        def _request(self, method, path, params=None, **kw):
            if params["tag"] == "lookingforadopts":
                self.calls.append("bad")
                raise da_api.DAApiError("bad tag")
            return super()._request(method, path, params, **kw)

    client = Mixed({"lfadopts": [dev(1, "Wolf adopt OTA - LF offers"), dev(2, "Wolf adopt - seeking a home"),
                                 dev(3, "LF wolf adopts!")]})
    stats = leads.da_lf_scan(conn, client, NOW, max_calls=2, characters=GOLD_CHARACTERS, sleep=lambda s: None)
    assert stats["errors"] == 1 and stats["stopped"] is None and client.calls == ["bad", "lfadopts"]
    assert [r["post_id"] for r in conn.execute("SELECT post_id FROM leads")] == ["3"]
    # плохой тег отмечен прочитанным — следующий проход не застревает на нём
    leads.da_lf_scan(conn, client, NOW + 60, max_calls=1, characters=GOLD_CHARACTERS, sleep=lambda s: None)
    assert client.calls[-1] == "lookingforoc"


@pytest.mark.parametrize("text", ["LF wolves, please don't dm me, comment below", "LF wolves, do not PM me",
                                  "no dms, comment only", "LF wolves! dms closed, offers in comments",
                                  "dm me? no — comments only"])
def test_dont_dm_is_not_dm_ok(text):
    assert not leads.dm_ok(text)


@pytest.mark.parametrize("text", ["dm me offers", "LF wolves, dms open!", "note me your wolves", "DM your offers"])
def test_dm_ok(text):
    assert leads.dm_ok(text)


def test_reply_draft_respects_no_dm(conn):
    set_offer(conn)
    lid = leads.add_manual(conn, "https://x.com/Fan/status/5", "LF wolf adopts, please don't dm me", NOW,
                           characters=GOLD_CHARACTERS)
    res = leads.reply_draft(conn, lid, now=NOW)
    assert res["ok"] and not res["dm_ok"] and not any("личку" in w for w in res["warnings"])


def test_x_handle_case_and_reserved_paths(conn):
    a = leads.add_manual(conn, "https://x.com/MothFan/status/1850000000000000001", "", NOW)
    b = leads.add_manual(conn, "https://x.com/mothfan/status/1850000000000000001", "", NOW)
    assert a == b and conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
    assert leads.parse_x_url("https://x.com/i/status/123") is None
    assert leads.parse_x_url("https://x.com/i/web/status/123") is None
    with pytest.raises(leads.LeadError):
        leads.add_manual(conn, "https://x.com/i/status/123", "", NOW)


def test_render_shows_her_cap(conn):
    leads.add_manual(conn, "https://x.com/Fan/status/1", "LF wolves", NOW)
    assert "из 3" in leads.render(conn, NOW, per_day=3)
    assert "из 2" in leads.render(conn, NOW, cfg=cfg(outreach_per_day=2))
    assert "из 5" in leads.render(conn, NOW, per_day=9)   # жёсткий потолок


def test_da_lf_scan_holds_no_write_lock_between_requests(conn, tmp_path):
    """Регрессия: отметка о прочитанном теге (leads_scan) открывала запись, и она висела до следующего
    запроса и паузы — записи из чата ждали блокировку базы."""
    import sqlite3

    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    other = sqlite3.connect(path, timeout=0.05)
    locked = []

    def sleep(_s):
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        except sqlite3.OperationalError:
            locked.append(1)

    client = FakeDA({"lookingforadopts": [dev(1, "LF wolf adopts!")], "lfadopts": [dev(6, "LF wolves pls")]})
    leads.da_lf_scan(conn, client, NOW, max_calls=2, characters=GOLD_CHARACTERS, my_username="her", sleep=sleep)
    other.close()
    assert not locked
