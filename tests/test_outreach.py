import json
import re

import pytest

from mcp_devian import buyers, da_api, db, drafts, outreach, pinglist, sales, selling
from mcp_devian.bids import Comment
from mcp_devian.db import Item

DAY = 86400
NOW = 1_790_000_000.0
TEMPLATE = "Hi {name}! New adopt «{title}» — {why}. {link}"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "o.sqlite3")
    yield c
    c.close()


def set_offer(conn, text=TEMPLATE, watched=True):
    selling.ensure(conn)
    conn.execute("INSERT OR REPLACE INTO selling_templates (kind, text, updated_ts) VALUES ('offer', ?, ?)",
                 (text, NOW))
    conn.commit()
    if watched:  # фоновая проверка ответов «stop» недавно прошла
        outreach.note_watch_run(conn, NOW - 3600)


def lot(conn, n, title, own=True, ts=NOW - 3 * DAY):
    iid = f"da:{n}"
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=title, url=f"https://www.deviantart.com/her/art/x-{n}",
                              author="her" if own else "other", lot_type="auction", own=own, published_ts=ts),
                   now=ts)
    return iid


def bids(conn, iid, users, ts):
    extra = {"bidders": [{"user": u, "amount": 10, "currency": "usd", "ts": ts, "n": 1} for u in users]}
    conn.execute("INSERT INTO auctions (deviation_id, owner, updated_ts, extra) VALUES (?, 'her', ?, ?)",
                 (iid, ts, json.dumps(extra)))
    conn.commit()


def world(conn):
    moth = lot(conn, 1, "Moth chibi adopt", ts=NOW - 30 * DAY)
    moth2 = lot(conn, 2, "Moth pastel adopt", ts=NOW - 100 * DAY)
    wolf = lot(conn, 3, "Wolf adopt", ts=NOW - 5 * DAY)
    sales.add_sale(conn, title="Moth chibi adopt", amount=20, currency="usd", platform="deviantart",
                   format="auction", buyer="Alice", item_id=moth, status="paid", now=NOW - 30 * DAY)
    bids(conn, moth2, ["Bob"], NOW - 100 * DAY)        # ставил на мотылька давно
    bids(conn, wolf, ["Wendy"], NOW - 5 * DAY)         # ставила на волка
    buyers.add_event(conn, "Fay", "faver", moth, NOW - 2 * DAY)   # фавнула мотылька недавно
    pinglist.add(conn, ["Pia"], now=NOW - 50 * DAY)
    buyers.sync(conn, NOW, my_username="her")
    return lot(conn, 9, "Moth chibi adopt NEW", ts=NOW)


def test_ranking_by_species_role_and_recency(conn):
    new = world(conn)
    ranked = outreach.rank_for_adopt(conn, new, NOW)
    users = [r["user"] for r in ranked]
    assert users[0] == "Alice"            # купила такого же
    assert "Wendy" not in users           # волки, не мотыльки
    assert "Pia" in users                 # пинглист без совпадения тегов — сама просила
    assert "Bob" in users and "Fay" not in users   # ставка — да (даже старая); только фав — лично не пишем
    alice = ranked[0]
    assert alice["channel"] == "da_mention" and "брал(а) у тебя 1 лот" in alice["why"] and "moth" in alice["why"]
    by_url = outreach.rank_for_adopt(conn, "https://www.deviantart.com/her/art/x-9", NOW)
    assert [r["user"] for r in by_url] == users
    by_tags = outreach.rank_for_adopt(conn, ["wolf"], NOW)
    assert "Wendy" in [r["user"] for r in by_tags]


def test_recency_breaks_ties(conn):
    lot(conn, 1, "Moth adopt", ts=NOW - 150 * DAY)
    lot(conn, 2, "Moth adopt two", ts=NOW - 2 * DAY)
    bids(conn, "da:1", ["Old"], NOW - 150 * DAY)
    bids(conn, "da:2", ["Fresh"], NOW - 2 * DAY)
    buyers.sync(conn, NOW)
    ranked = outreach.rank_for_adopt(conn, ["moth"], NOW)
    assert [r["user"] for r in ranked] == ["Fresh", "Old"]


def test_channels(conn):
    # только фав / комментарий / LF — личного канала нет: DM только покупателям, ставившим и пинглисту
    assert outreach.channel_for({"roles": ["faver"], "platforms": ["deviantart"]}) is None
    assert outreach.channel_for({"roles": ["commenter"], "platforms": ["deviantart"]}) is None
    assert outreach.channel_for({"roles": ["th_lf"], "platforms": ["toyhouse"]}) is None
    assert outreach.channel_for({"roles": ["bidder"], "platforms": ["deviantart"]}) == "da_message_manual"
    assert outreach.channel_for({"roles": ["buyer"], "platforms": ["deviantart"]}) == "da_mention"
    assert outreach.channel_for({"roles": ["pinglist"], "platforms": ["deviantart"]}) == "da_mention"
    assert outreach.channel_for({"roles": [], "platforms": ["x"]}) == "x_reply"
    assert outreach.channel_for({"roles": ["buyer"], "platforms": ["toyhouse"]}) == "th_message_manual"
    assert outreach.channel_for({"roles": ["faver"], "platforms": ["deviantart", "x"]}) is None
    assert outreach.channel_for({"roles": ["faver", "bidder"], "platforms": ["deviantart"]}) == "da_message_manual"
    # контакт, которому она ответила в X (mark_contacted x_reply), получает канал x_reply из базы
    outreach.mark_contacted(conn, ["XFan"], "x_reply", "lead:1", NOW)
    assert outreach.channel_for(buyers.contact(conn, "xfan")) == "x_reply"


def test_favers_never_get_offers_and_first_manual_has_no_url(conn):
    new = world(conn)
    set_offer(conn, "Hi {name}! «{title}» is up, {why} — https://boosty.to/her {link}")
    ranked = outreach.rank_for_adopt(conn, new, NOW)
    assert "Fay" not in [r["user"] for r in ranked]   # только фавнула — не предлагаем лично
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    res = outreach.drafts_for_adopt(conn, item, ranked, None, NOW)
    assert "Fay" not in res["mention_users"] and "Bob" not in res["mention_users"]
    manual = {m["user"]: m["text"] for m in res["manual"]}
    assert "Fay" not in manual and "Bob" in manual
    for text in manual.values():
        assert not re.search(r"https?://|boosty\.to|www\.", text)
        assert outreach.NO_LINK in text
    assert "you bid on my" in manual["Bob"]
    assert any("убрала ссылку" in w for w in res["warnings"])
    # Даже если фав подсунули вручную (или с каналом), черновика нет.
    forced = outreach.drafts_for_adopt(conn, item, [{"user": "Fay", "channel": "da_message_manual"}], None, NOW + 9)
    assert not forced["manual"] and dict(forced["skipped"])["Fay"] == outreach.NOT_DM_ROLE
    assert "лично не пишем" in buyers.render_crm(conn, NOW)


def test_cap_five_across_draft_manual_pending(conn):
    lot(conn, 1, "Moth adopt", ts=NOW - 5 * DAY)
    users = [f"Buyer{i}" for i in range(3)] + [f"Fan{i}" for i in range(4)]
    for i, u in enumerate(users[:3]):
        sales.add_sale(conn, title="Moth adopt", amount=10, currency="usd", platform="deviantart", format="auction",
                       buyer=u, item_id=f"da:{100 + i}", status="paid", now=NOW - 5 * DAY)
    for u in users[3:]:
        buyers.add_event(conn, u, "bidder", "da:1", NOW - DAY)
    lot(conn, 100, "Moth adopt a", ts=NOW - 5 * DAY)
    lot(conn, 101, "Moth adopt b", ts=NOW - 5 * DAY)
    lot(conn, 102, "Moth adopt c", ts=NOW - 5 * DAY)
    buyers.sync(conn, NOW)
    set_offer(conn)
    # одно место уже занято сегодня (отправила вручную)
    drafts.log_sent(conn, "offer_manual", "someone", "da_message_manual", True, "", NOW - 3600)
    new = lot(conn, 9, "Moth adopt new", ts=NOW)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    ranked = outreach.rank_for_adopt(conn, new, NOW, limit=10)
    assert len(ranked) == 7

    class Client:
        def post_comment(self, *a, **k):
            raise AssertionError("никаких комментариев через API")

    res = outreach.drafts_for_adopt(conn, item, ranked, None, NOW, per_day=9)  # жёсткий потолок всё равно 5
    sent = len(res["mention_users"]) + len(res["manual"])
    assert sent == 4
    assert len(res["skipped"]) == 3
    assert all("лимит" in why for _, why in res["skipped"])
    assert outreach.used_today(conn, NOW) == 5
    # черновик da_offer: ≤5 ников, текст с подсказкой stop, ничего не отправлено
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (res["mention_draft_id"],)).fetchone()
    assert row["kind"] == "da_offer" and row["status"] == "pending"
    payload = json.loads(row["payload"])
    assert 1 <= len(payload["users"]) <= 5 and payload["deviation_id"] == new
    assert "stop" in payload["text"] and "@Buyer0" in payload["text"]
    # шестой и дальше — пропущены; повторный вызов — никого (все заняты или лимит)
    again = outreach.drafts_for_adopt(conn, item, ranked, None, NOW + 60)
    assert not again["manual"] and again["mention_draft_id"] is None
    reasons = dict(again["skipped"])
    assert reasons["Fan0"] in ("черновик для него(неё) уже есть",) or "лимит" in reasons["Fan0"]


def test_mention_only_under_her_own_work(conn):
    world(conn)
    set_offer(conn)
    other = lot(conn, 50, "Moth adopt by other", own=False, ts=NOW)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (other,)).fetchone()
    ranked = outreach.rank_for_adopt(conn, other, NOW)
    res = outreach.drafts_for_adopt(conn, item, ranked, None, NOW)
    assert res["mention_draft_id"] is None
    assert any("ЕЁ работой" in why for _, why in res["skipped"])


def test_no_template_no_drafts(conn):
    new = world(conn)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    res = outreach.drafts_for_adopt(conn, item, outreach.rank_for_adopt(conn, new, NOW), None, NOW)
    assert res["mention_draft_id"] is None and not res["manual"]
    assert outreach.NO_TEMPLATE in res["warnings"]


def test_too_similar_and_warning(conn):
    assert outreach.too_similar("Hi Bob! New moth adopt", ["Hi Bo! New moth adopt"])
    assert not outreach.too_similar("Totally different words here", ["Hi Bob! New moth adopt"])
    new = world(conn)
    bids(conn, "da:1", ["Ben"], NOW - 2 * DAY)
    buyers.sync(conn, NOW, my_username="her")
    set_offer(conn, "Hi {name}! My new adopt «{title}» is open now, {link}")  # без {why} — тексты почти одинаковые
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    res = outreach.drafts_for_adopt(conn, item, outreach.rank_for_adopt(conn, new, NOW), None, NOW)
    assert len(res["manual"]) >= 2
    assert outreach.SIMILAR_WARNING in res["warnings"]
    assert len(outreach.recent_texts(conn)) >= 2


def test_mark_contacted_starts_cooldown(conn):
    new = world(conn)
    set_offer(conn)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    outreach.drafts_for_adopt(conn, item, outreach.rank_for_adopt(conn, new, NOW), None, NOW)
    assert outreach.mark_contacted(conn, ["Bob"], "da_message_manual", new, NOW + 60) == ["Bob"]
    assert drafts.count_sent(conn, "offer_manual", NOW, target="bob") == 1
    assert outreach.person_block(conn, "Bob", NOW + 2 * DAY).startswith("ты писала")
    assert outreach.person_block(conn, "Bob", NOW + 15 * DAY) is None
    buyers.sync(conn, NOW + 2 * DAY)
    assert "bob" not in {c["username"] for c in buyers.eligible(conn, NOW + 2 * DAY)}


def test_opted_out_and_minor_never_ranked(conn):
    new = world(conn)
    buyers.opt_out(conn, ["Alice"], "stop", NOW)
    conn.execute("UPDATE contacts SET is_minor = 1 WHERE username = 'fay'")
    conn.commit()
    users = [r["user"] for r in outreach.rank_for_adopt(conn, new, NOW)]
    assert "Alice" not in users and "Fay" not in users


def test_pinglist_pinged_not_offered_again(conn):
    new = world(conn)
    set_offer(conn)
    pinglist.mark_pinged(conn, new, ["Pia"], NOW)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    res = outreach.drafts_for_adopt(conn, item, outreach.rank_for_adopt(conn, new, NOW), None, NOW)
    assert "Pia" not in res["mention_users"]
    assert dict(res["skipped"])["Pia"].startswith("уже в пинге")
    text = outreach.render_for_adopt(outreach.rank_for_adopt(conn, new, NOW), res)
    assert "эвристика" in text and "«го»" in text


def test_opted_out_for_send_path_and_payload_uuid(conn):
    new = world(conn)
    conn.execute("UPDATE items SET extra = ? WHERE id = ?", (json.dumps({"uuid": "UUID-9"}), new))
    conn.commit()
    buyers.opt_out(conn, ["Bob"], "stop", NOW)
    assert outreach.opted_out(conn) == {"bob"}
    set_offer(conn)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    res = outreach.drafts_for_adopt(conn, item, outreach.rank_for_adopt(conn, new, NOW), None, NOW)
    payload = json.loads(conn.execute("SELECT payload FROM drafts WHERE id = ?",
                                      (res["mention_draft_id"],)).fetchone()[0])
    assert payload["deviation_uuid"] == "UUID-9" and payload["deviation_url"].endswith("x-9")


def test_flat_whofaved_rows(conn):
    rows = buyers._faver_rows([{"username": "Flat", "is_minor": False, "ts": NOW - 5}])
    assert rows == [{"user": "Flat", "ts": NOW - 5, "is_minor": None}]   # bool(нет поля) ≠ «взрослый»


# --- Регрессии по проверке W3 ---

def test_untagged_adopt_only_pinglist(conn):
    world(conn)
    new = lot(conn, 20, "Adopt #12 OTA", ts=NOW)
    assert outreach.adopt_tags(conn, new)[0] == []
    ranked = outreach.rank_for_adopt(conn, new, NOW)
    assert [r["user"] for r in ranked] == ["Pia"]
    text = outreach.render_for_adopt(ranked, {"skipped": []})
    assert "Теги адопта не распознаны" in text


def test_no_drafts_at_all_for_someone_elses_adopt(conn):
    world(conn)
    set_offer(conn)
    other = lot(conn, 50, "Moth chibi adopt by other", own=False, ts=NOW)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (other,)).fetchone()
    ranked = outreach.rank_for_adopt(conn, other, NOW)
    assert ranked
    res = outreach.drafts_for_adopt(conn, item, ranked, None, NOW)
    assert not res["manual"] and res["mention_draft_id"] is None
    assert outreach.used_today(conn, NOW) == 0
    assert conn.execute("SELECT COUNT(*) FROM outreach_log").fetchone()[0] == 0


def test_won_unpaid_is_not_called_a_buyer(conn):
    lot(conn, 1, "Moth adopt", ts=NOW - 10 * DAY)
    sales.add_sale(conn, title="Moth adopt", amount=20, currency="usd", platform="deviantart", format="auction",
                   buyer="Nopay", item_id="da:1", status="won", now=NOW - 10 * DAY)
    buyers.sync(conn, NOW, "her")
    set_offer(conn)
    new = lot(conn, 9, "Moth adopt new", ts=NOW)
    ranked = outreach.rank_for_adopt(conn, new, NOW)
    assert ranked[0]["user"] == "Nopay" and ranked[0]["channel"] == "da_message_manual"
    assert "брал" not in ranked[0]["why"] and "ставил" in ranked[0]["why"]
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    res = outreach.drafts_for_adopt(conn, item, ranked, None, NOW)
    assert res["mention_draft_id"] is None
    assert "adopted" not in res["manual"][0]["text"] and "bid on my" in res["manual"][0]["text"]


@pytest.mark.parametrize("raw", ["see linktr.ee/her", "her.carrd.co", "boosty.to", "https://x.com/h",
                                 "toyhou.se/her", "ko-fi.com/her", "www.her.art"])
def test_any_link_is_stripped_from_first_message(raw):
    text, had = outreach._strip_urls(f"Hi! {raw} thanks")
    assert had and "." not in text.replace("Hi!", "")


@pytest.mark.parametrize("raw", ["SB 12.50 usd", "e.g. moths", "v2.0", "@her.name"])
def test_prices_and_plain_text_survive_url_strip(raw):
    assert outreach._strip_urls(raw) == (raw, False)


def test_mention_text_has_no_dangling_punctuation(conn):
    new = world(conn)
    set_offer(conn)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    res = outreach.drafts_for_adopt(conn, item, outreach.rank_for_adopt(conn, new, NOW), None, NOW)
    assert res["mention_draft_id"]
    assert " — ." not in res["mention_text"] and outreach.MENTION_WHY in res["mention_text"]


def test_no_mentions_while_stop_replies_are_unread(conn):
    """Регрессия: обещание «ответь stop» без чтения ответов под работой."""
    new = world(conn)
    set_offer(conn, watched=False)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (new,)).fetchone()
    ranked = outreach.rank_for_adopt(conn, new, NOW)
    res = outreach.drafts_for_adopt(conn, item, ranked, None, NOW)
    assert res["mention_draft_id"] is None
    assert dict(res["skipped"])["Alice"] == outreach.REPLIES_UNWATCHED
    assert res["manual"]   # личные сообщения (без обещания) — как раньше
    outreach.note_watch_run(conn, NOW - 4 * DAY)   # проверка давно не проходила — тоже нельзя
    assert not outreach.replies_watched(conn, NOW)


class Watcher:
    def __init__(self, comments, fail=None):
        self.comments = comments
        self.fail = fail or {}
        self.calls = []

    def deviation_comments(self, uuid, max_comments=500):
        self.calls.append(uuid)
        if uuid in self.fail:
            raise self.fail[uuid]
        return self.comments.get(uuid, [])

    def deviation_uuid_from_url(self, url, max_pages=10):
        self.calls.append(f"lookup:{url}")
        return None

    def post_comment(self, *a, **k):  # pragma: no cover
        raise AssertionError("watcher только читает")


def _sent_offer(conn, dev, uuid, users, ts):
    did = drafts.create(conn, "da_offer", {"deviation_id": dev, "deviation_uuid": uuid, "users": users,
                                           "deviation_url": f"https://www.deviantart.com/her/art/{dev}", "text": "x"},
                        "p", ts)
    conn.execute("UPDATE drafts SET status = 'sent', sent_ts = ? WHERE id = ?", (ts, did))
    conn.commit()
    return did


def test_watch_offer_replies_reads_stop_and_rotates(conn):
    world(conn)
    _sent_offer(conn, "da:9", "U9", ["Alice"], NOW - DAY)
    _sent_offer(conn, "da:3", "U3", ["Wendy"], NOW - 2 * DAY)
    _sent_offer(conn, "da:1", "U1", ["Bob"], NOW - 40 * DAY)   # старше WATCH_DAYS — не читаем
    cl = Watcher({"U9": [Comment(comment_id="c1", parent_id="m", user="Alice", posted_ts=NOW - 60,
                                 text="stop please")]})
    stats = outreach.watch_offer_replies(conn, cl, NOW, owner="her", max_works=1)
    assert stats["works"] == 1 and stats["opted_out"] == 1 and buyers.is_opted_out(conn, "alice")
    assert outreach.replies_watched(conn, NOW)
    outreach.watch_offer_replies(conn, cl, NOW + 60, owner="her", max_works=1)
    assert cl.calls == ["U9", "U3"]   # по кругу: сначала свежее предложение, потом ни разу не читанная
    assert "U1" not in cl.calls


def test_watch_offer_replies_limits_and_errors(conn):
    world(conn)
    _sent_offer(conn, "da:9", "U9", ["Alice"], NOW - DAY)
    _sent_offer(conn, "da:3", "U3", ["Wendy"], NOW - DAY)
    broken = Watcher({}, fail={"U9": da_api.DAApiError("gone"), "U3": da_api.DAApiError("gone")})
    st = outreach.watch_offer_replies(conn, broken, NOW, owner="her", max_works=2)
    assert st["errors"] == 2 and st["stopped"] is None and len(broken.calls) == 2
    limited = Watcher({}, fail={"U9": da_api.RateLimited("429"), "U3": da_api.RateLimited("429")})
    conn.execute("DELETE FROM outreach_watch")
    conn.commit()
    st = outreach.watch_offer_replies(conn, limited, NOW, owner="her", max_works=2)
    assert st["stopped"] == "RateLimited" and len(limited.calls) == 1
    assert not outreach.replies_watched(conn, NOW)   # проход не завершён — обещанию пока не верим
    assert outreach.watch_offer_replies(conn, limited, NOW, owner="")["stopped"] == "no_owner"


def test_scheduled_offer_draft_still_counts(conn):
    """Регрессия: запланированный черновик старше суток выпадал из лимита и «занятых»."""
    world(conn)
    drafts.ensure_schema(conn)
    did = drafts.create(conn, "da_offer", {"users": ["Alice"], "text": "x"}, "p", NOW - 3 * DAY)
    conn.execute("UPDATE drafts SET status = 'scheduled', send_at = ? WHERE id = ?", (NOW + DAY, did))
    conn.commit()
    assert outreach.used_today(conn, NOW) == 1
    assert outreach.person_block(conn, "Alice", NOW) == "черновик для него(неё) уже есть"
    old = drafts.create(conn, "da_offer", {"users": ["Bob"], "text": "x"}, "p", NOW - 2 * DAY)
    assert old and outreach.person_block(conn, "Bob", NOW) is None   # «го» не было сутки — черновик мёртв


def test_render_uses_her_cooldown(conn):
    text = outreach.render_for_adopt([{"user": "A", "why": "w", "channel": "da_message_manual"}],
                                     {"manual": [{"user": "A", "channel": "da_message_manual", "text": "t"}]},
                                     cooldown_days=21)
    assert "21 дн" in text and "14" not in text


# --- регрессии ревью 0.5 (платформенная безопасность) ---

def _item(conn, iid):
    return conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()


def test_unconfirmed_manual_dm_still_holds_cooldown(conn):
    """Регрессия: личку по черновику она отправила, но не отметила (buyers:contacted) — назавтра тот же
    человек снова получал черновик. Неподтверждённый черновик держит паузу cooldown дней."""
    a = lot(conn, 1, "Moth adopt", ts=NOW - 10 * DAY)
    bids(conn, a, ["Bob"], NOW - 10 * DAY)
    buyers.sync(conn, NOW, my_username="her")
    set_offer(conn)
    n1 = lot(conn, 2, "Moth adopt two", ts=NOW)
    r1 = outreach.drafts_for_adopt(conn, _item(conn, n1), outreach.rank_for_adopt(conn, n1, NOW), None, NOW)
    assert [m["user"] for m in r1["manual"]] == ["Bob"]
    later = NOW + DAY + 60
    outreach.note_watch_run(conn, later - 60)
    n2 = lot(conn, 3, "Moth adopt three", ts=later)
    assert "Bob" not in [r["user"] for r in outreach.rank_for_adopt(conn, n2, later)]
    r2 = outreach.drafts_for_adopt(conn, _item(conn, n2), [{"user": "Bob", "channel": "da_message_manual",
                                                            "roles": ["bidder"]}], None, later)
    assert not r2["manual"] and "могла отправить" in dict(r2["skipped"])["Bob"]
    assert "могла отправить" in buyers.render_crm(conn, later)
    # После паузы — снова можно.
    after = NOW + 15 * DAY
    assert outreach.person_block(conn, "Bob", after) is None


def test_lead_reply_to_same_author_waits_for_cooldown(conn):
    set_offer(conn)
    from mcp_devian import leads

    l1 = leads.add_manual(conn, "https://x.com/mothfan/status/11111111111", "LF moth adopts", NOW)
    l2 = leads.add_manual(conn, "https://x.com/mothfan/status/22222222222", "LF moth adopts again", NOW + DAY)
    assert leads.reply_draft(conn, l1, None, None, NOW)["ok"]
    r2 = leads.reply_draft(conn, l2, None, None, NOW + DAY + 60)
    assert not r2["ok"] and "пауза" in r2["reason"]


def test_failed_reply_read_keeps_offer_gate_closed(conn):
    """Регрессия: чтение ответов под работой с отметками упало (503), а проход всё равно засчитывался —
    отметки с обещанием «ответь stop» готовились, хотя ответы никто не читал."""
    a = lot(conn, 1, "Moth", ts=NOW - 5 * DAY)
    conn.execute("UPDATE items SET extra = ? WHERE id = ?", ('{"uuid": "u1"}', a))
    did = drafts.create(conn, "da_offer", {"deviation_id": a, "deviation_uuid": "u1", "users": ["Bob"],
                                           "text": "x"}, "p", NOW - DAY)
    conn.execute("UPDATE drafts SET status = 'sent', sent_ts = ? WHERE id = ?", (NOW - DAY, did))
    conn.commit()

    class Client:
        fail: da_api.DAApiError | None = da_api.DAApiError("server error 503", "http_503", status=503)

        def deviation_comments(self, uuid, max_comments=200):
            if self.fail:
                raise self.fail
            return []

    client = Client()
    outreach.note_watch_run(conn, NOW - 3600)  # раньше всё было хорошо
    st = outreach.watch_offer_replies(conn, client, NOW, owner="her")
    assert st["errors"] == 1 and not outreach.replies_watched(conn, NOW)
    # Следующий проход: прочитать вышло — ворота снова открыты.
    client.fail = None
    st = outreach.watch_offer_replies(conn, client, NOW + 600, owner="her")
    assert st["errors"] == 0 and outreach.replies_watched(conn, NOW + 600)
    # Удалённая работа (404) не держит ворота: отвечать под ней некому.
    client.fail = da_api.DAApiError("not found", "http_404", status=404)
    st = outreach.watch_offer_replies(conn, client, NOW + 1200, owner="her")
    assert st["errors"] == 0 and outreach.replies_watched(conn, NOW + 1200)


def test_failing_work_is_retried_first(conn):
    for n in (1, 2, 3):
        a = lot(conn, n, f"Moth {n}", ts=NOW - 5 * DAY)
        conn.execute("UPDATE items SET extra = ? WHERE id = ?", (json.dumps({"uuid": f"u{n}"}), a))
        did = drafts.create(conn, "da_offer", {"deviation_id": a, "deviation_uuid": f"u{n}", "users": ["Bob"],
                                               "text": "x"}, "p", NOW - DAY - n)
        conn.execute("UPDATE drafts SET status = 'sent', sent_ts = ? WHERE id = ?", (NOW - DAY - n, did))
    conn.commit()
    seen: list[str] = []

    class Client:
        def deviation_comments(self, uuid, max_comments=200):
            seen.append(uuid)
            if uuid == "u1":
                raise da_api.DAApiError("server error 503", "http_503", status=503)
            return []

    outreach.watch_offer_replies(conn, Client(), NOW, owner="her", max_works=1)
    outreach.watch_offer_replies(conn, Client(), NOW + 60, owner="her", max_works=1)
    assert seen == ["u1", "u1"] and not outreach.replies_watched(conn, NOW + 60)


def test_old_lead_reply_not_reissued_after_optout_or_contact(conn):
    """Регрессия: повторный buyers:offer по лиду отдавал старый текст и свежую ссылку на ответ в X,
    даже если автор попросил не писать или она уже ответила."""
    from mcp_devian import leads

    set_offer(conn)
    lid = leads.add_manual(conn, "https://x.com/mothfan/status/12345678901", "LF moth adopts", NOW)
    assert leads.reply_draft(conn, lid, None, lambda **k: "intent", NOW)["ok"]
    again = leads.reply_draft(conn, lid, None, lambda **k: "intent", NOW + 10)
    assert again["ok"] and again["already"] and again["intent_url"] == "intent"  # свой же черновик не мешает
    buyers.opt_out(conn, ["mothfan"], "her_choice", NOW + 20)
    r = leads.reply_draft(conn, lid, None, lambda **k: "intent", NOW + 30)
    assert not r["ok"] and r["intent_url"] is None and r["text"] and "не писать" in r["reason"]
    lid2 = leads.add_manual(conn, "https://x.com/otherfan/status/12345678902", "LF moth adopts", NOW)
    assert leads.reply_draft(conn, lid2, None, lambda **k: "intent", NOW + 40)["ok"]
    outreach.mark_contacted(conn, ["otherfan"], "x_reply", f"lead:{lid2}", NOW + 50)
    r = leads.reply_draft(conn, lid2, None, lambda **k: "intent", NOW + 60)
    assert not r["ok"] and r["intent_url"] is None and "уже ответила" in r["reason"]
