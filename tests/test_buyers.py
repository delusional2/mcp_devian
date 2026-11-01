import json

import pytest

from mcp_devian import buyers, da_api, db, drafts, pinglist, sales
from mcp_devian.db import Item

DAY = 86400
NOW = 1_790_000_000.0


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "b.sqlite3")
    yield c
    c.close()


def lot(conn, n, title, own=True, ts=NOW - 3 * DAY, uuid=None):
    iid = f"da:{n}"
    db.upsert_item(conn, Item(id=iid, source="deviantart", title=title, url=f"https://www.deviantart.com/her/art/x-{n}",
                              author="her" if own else "other", lot_type="auction", own=own, published_ts=ts,
                              extra={"uuid": uuid} if uuid else {}), now=ts)
    return iid


def bids(conn, iid, users, ts):
    extra = {"bidders": [{"user": u, "amount": 10, "currency": "usd", "ts": ts, "n": 1} for u in users]}
    conn.execute("INSERT INTO auctions (deviation_id, owner, updated_ts, extra) VALUES (?, 'her', ?, ?)",
                 (iid, ts, json.dumps(extra)))
    conn.commit()


class FakeClient:
    def __init__(self, favers):
        self.favers = favers
        self.calls = []

    def whofaved(self, deviationid, max_users=100):
        self.calls.append(deviationid)
        self.max_users = max_users
        return {"results": self.favers.get(deviationid, [])}

    def post_comment(self, *a, **k):  # pragma: no cover - никогда не должен вызываться
        raise AssertionError("post_comment")


def fixture_world(conn):
    a = lot(conn, 1, "Moth chibi adopt")
    b = lot(conn, 2, "Moth pastel auction", uuid="U2", ts=NOW - 2 * DAY)
    c = lot(conn, 3, "Dragon adopt", ts=NOW - 100 * DAY)
    sales.add_sale(conn, title="Moth chibi adopt", amount=20, currency="usd", platform="deviantart", format="auction",
                   buyer="Alice", item_id=a, status="paid", now=NOW - 3 * DAY)
    bids(conn, b, ["Alice", "Bob", "her"], NOW - 2 * DAY)
    bids(conn, c, ["Carol"], NOW - 200 * DAY)
    pinglist.add(conn, ["Dana"], now=NOW - 10 * DAY)
    return a, b, c


def test_sync_sources_interests_and_idempotent(conn):
    fixture_world(conn)
    r1 = buyers.sync(conn, NOW, my_username="her")
    r2 = buyers.sync(conn, NOW, my_username="her")
    assert r1["new_events"] > 0 and r2["new_events"] == 0
    assert buyers.contact(conn, "her") is None
    alice = buyers.contact(conn, "alice")
    assert set(alice["roles"]) == {"buyer", "bidder"}
    # покупка (3) + ставка (2) на мотыльков
    assert alice["interests"]["species:moth"] == 5
    assert alice["interests"]["styles:chibi"] == 3
    assert buyers.contact(conn, "dana")["roles"] == ["pinglist"]
    rows = conn.execute("SELECT COUNT(*) FROM contact_events").fetchone()[0]
    buyers.sync(conn, NOW, my_username="her")
    assert conn.execute("SELECT COUNT(*) FROM contact_events").fetchone()[0] == rows


def test_window_180_days(conn):
    fixture_world(conn)
    buyers.sync(conn, NOW, my_username="her")
    names = {c["username"] for c in buyers.eligible(conn, NOW)}
    assert {"alice", "bob", "dana"} <= names
    assert "carol" not in names  # ставила 200 дней назад
    crm = buyers.render_crm(conn, NOW)
    assert "carol" in crm.lower() and "не было дел с тобой 180 дн" in crm
    assert "n=" in crm and "crm" in crm


def test_favers_and_minor_always_excluded(conn):
    fixture_world(conn)
    client = FakeClient({"U2": [{"user": {"username": "Kid", "is_minor": True}, "time": NOW - DAY},
                                {"user": {"username": "Grown", "is_minor": False}, "time": NOW - DAY},
                                {"user": {"username": "her"}, "time": NOW - DAY}]})
    stats = buyers.refresh_favers(conn, client, NOW, max_calls=3, my_username="her")
    assert client.calls == ["U2"] and stats["favers"] == 2
    assert client.max_users == buyers.FAVERS_PER_CALL  # одна страница = один запрос
    buyers.sync(conn, NOW, my_username="her")
    names = {c["username"]: c for c in buyers.eligible(conn, NOW)}
    assert "kid" not in names
    assert names["grown"]["age_unknown"] is False   # ответ API как есть: поле пришло и сказало «нет»
    assert names["alice"]["age_unknown"] is True
    assert buyers.contact(conn, "kid")["is_minor"] == 1
    assert "младше 18" in buyers.render_crm(conn, NOW)


def test_refresh_favers_respects_max_calls_and_stops_on_limit(conn):
    lot(conn, 11, "a", uuid="A", ts=NOW - DAY)
    lot(conn, 12, "b", uuid="B", ts=NOW - 2 * DAY)
    lot(conn, 13, "c", uuid="C", ts=NOW - 3 * DAY)
    lot(conn, 14, "old", uuid="D", ts=NOW - 30 * DAY)
    client = FakeClient({})
    buyers.refresh_favers(conn, client, NOW, max_calls=2)
    assert client.calls == ["A", "B"]

    class Limited(FakeClient):
        def whofaved(self, deviationid, max_users=100):
            self.calls.append(deviationid)
            raise da_api.RateLimited("429")

    lim = Limited({})
    assert buyers.refresh_favers(conn, lim, NOW, max_calls=3)["stopped"] == "RateLimited"
    assert lim.calls == ["C"]   # по кругу: C ещё ни разу не читали
    # следующий проход — снова с давно не читанных, а не всегда с самых свежих
    client2 = FakeClient({})
    buyers.refresh_favers(conn, client2, NOW + 60, max_calls=2)
    assert client2.calls == ["C", "A"]


def test_refresh_favers_skips_broken_lot_and_moves_on(conn):
    """Регрессия: удалённый лот (DAApiError) раньше ронял проход на каждом запуске."""
    lot(conn, 11, "a", uuid="A", ts=NOW - DAY)
    lot(conn, 12, "b", uuid="B", ts=NOW - 2 * DAY)

    class Broken(FakeClient):
        def whofaved(self, deviationid, max_users=100):
            self.calls.append(deviationid)
            if deviationid == "A":
                raise da_api.DAApiError("not found")
            return {"results": [{"user": {"username": "Fan"}, "time": NOW - DAY}]}

    cl = Broken({})
    stats = buyers.refresh_favers(conn, cl, NOW, max_calls=2)
    assert cl.calls == ["A", "B"] and stats["errors"] == 1 and stats["favers"] == 1 and stats["stopped"] is None
    cl2 = Broken({})
    buyers.refresh_favers(conn, cl2, NOW + 60, max_calls=1)
    assert cl2.calls == ["A"]   # оба прочитаны в NOW; A — первым по свежести, но и B не застрял


def test_minor_flag_is_never_erased(conn):
    """Регрессия: плоская строка whofaved с is_minor=False стирала известное «младше 18»."""
    lot(conn, 1, "Moth adopt", uuid="U1", ts=NOW - 2 * DAY)
    buyers.add_event(conn, "Kid", "faver", "da:1", NOW - DAY, is_minor=1)
    flat = [{"username": "Kid", "is_minor": False, "is_watching": False, "ts": NOW - DAY},
            {"username": "New", "is_minor": False, "is_watching": False, "ts": NOW - DAY}]

    class Flat(FakeClient):
        def whofaved(self, deviationid, max_users=100):
            return flat

    buyers.refresh_favers(conn, Flat({}), NOW, max_calls=1)
    assert buyers.contact(conn, "kid")["is_minor"] == 1
    assert buyers.contact(conn, "new")["is_minor"] is None   # «False» из bool(...) — это «неизвестно»
    buyers.add_event(conn, "Kid", "bidder", "da:1", NOW, is_minor=0)
    assert buyers.contact(conn, "kid")["is_minor"] == 1
    buyers.sync(conn, NOW)
    assert "kid" not in {c["username"] for c in buyers.eligible(conn, NOW)}
    assert buyers._faver_rows([{"username": "Flat", "is_minor": True}])[0]["is_minor"] == 1
    assert buyers._faver_rows([{"user": {"username": "Api"}}])[0]["is_minor"] is None


def test_won_unpaid_is_bidder_not_buyer(conn):
    lot(conn, 1, "Moth adopt")
    sales.add_sale(conn, title="Moth adopt", amount=20, currency="usd", platform="deviantart", format="auction",
                   buyer="Nopay", item_id="da:1", status="won", now=NOW - DAY)
    buyers.sync(conn, NOW)
    c = buyers.contact(conn, "nopay")
    assert c["roles"] == ["bidder"] and c["interests"]["species:moth"] == 2
    sale_id = conn.execute("SELECT id FROM sales").fetchone()[0]
    sales.set_status(conn, sale_id, "paid", NOW)
    buyers.sync(conn, NOW)
    assert "buyer" in buyers.contact(conn, "nopay")["roles"]


@pytest.mark.parametrize("text", ["stop", "STOP!", "please stop", "no thanks", "No thank you", "don't tag me",
                                  "please stop tagging me", "unsubscribe", "не тегай меня", "не пиши мне больше",
                                  "отпишите меня", "стоп", "stop please", "stop pls", "stop it", "please remove me",
                                  "не надо мне писать", "больше не пишите"])
def test_detect_opt_out(text):
    assert buyers.detect_opt_out(text)


@pytest.mark.parametrize("text", ["I can't stop staring at this!", "so cute", "SB $10", "thanks!", "wow no way",
                                  "пиши ещё таких", "no thanks to the lag lol, missed it", "не пиши так мелко",
                                  "Не надо писать цену в комментах", "stop motion would be cool"])
def test_not_opt_out(text):
    assert not buyers.detect_opt_out(text)


def test_opt_out_persists_until_opt_in(conn):
    fixture_world(conn)
    buyers.sync(conn, NOW, my_username="her")
    assert buyers.opt_out(conn, ["@Alice"], "said stop", NOW) == ["Alice"]
    buyers.sync(conn, NOW + 1, my_username="her")
    assert "alice" not in {c["username"] for c in buyers.eligible(conn, NOW + 1)}
    assert buyers.is_opted_out(conn, "alice")
    assert buyers.opt_in(conn, ["alice"], NOW + 10) == ["alice"]
    # старый «stop» (до её opt_in) при повторном чтении комментариев снова не выключает
    assert buyers.opt_out(conn, ["alice"], "comment_stop", NOW + 20, signal_ts=NOW + 5) == []
    assert buyers.opt_out(conn, ["alice"], "comment_stop", NOW + 20, signal_ts=0.0) == []
    assert "alice" in {c["username"] for c in buyers.eligible(conn, NOW + 20)}
    # новый — выключает
    assert buyers.opt_out(conn, ["alice"], "comment_stop", NOW + 30, signal_ts=NOW + 25) == ["alice"]


def test_cooldown_14_days(conn):
    fixture_world(conn)
    buyers.sync(conn, NOW, my_username="her")
    drafts.log_sent(conn, "offer_manual", "bob", "da_message_manual", True, "", NOW - 3 * DAY)
    buyers.sync(conn, NOW, my_username="her")
    assert "bob" not in {c["username"] for c in buyers.eligible(conn, NOW)}
    assert "ты писала 3 дн назад" in buyers.render_crm(conn, NOW)
    assert "bob" in {c["username"] for c in buyers.eligible(conn, NOW + 12 * DAY)}


def test_th_lf_authors_she_replied_to(conn):
    db.upsert_item(conn, Item(id="th:55", source="toyhouse", title="LF moth", url="https://toyhou.se/~forums/13/55",
                              author="Thea", wants_characters=True, published_ts=NOW - DAY), now=NOW - DAY)
    drafts.log_sent(conn, "th_reply", "th:55", "Luna", True, "", NOW - DAY)
    buyers.sync(conn, NOW, my_username="her")
    c = buyers.contact(conn, "thea")
    assert c["roles"] == ["th_lf"] and c["platforms"] == ["toyhouse"]


def test_empty_crm_says_no_conclusion(conn):
    assert "Вывода нет" in buyers.render_crm(conn, NOW)


def test_sync_recomputes_only_changed_contacts(conn, monkeypatch):
    """Регрессия: каждый фоновый проход пересчитывал ВСЕ контакты под блокировкой базы (секунды при тысячах)."""
    a = lot(conn, 1, "Moth adopt", ts=NOW - 10 * DAY)
    bids(conn, a, ["Bob", "Ann"], NOW - 10 * DAY)
    buyers.sync(conn, NOW, my_username="her")
    seen: list[str] = []
    real = buyers._recompute
    monkeypatch.setattr(buyers, "_recompute", lambda c, u, now: seen.append(u) or real(c, u, now))
    buyers.sync(conn, NOW + 60, my_username="her")
    assert seen == []                                        # ничего нового — никого не пересчитываем
    buyers.add_event(conn, "Cat", "faver", a, NOW)           # фав из refresh_favers (вне sync)
    drafts.log_sent(conn, "offer_manual", "bob", "da_message_manual", True, "", NOW + 100)
    buyers.sync(conn, NOW + 120, my_username="her")
    assert sorted(seen) == ["bob", "cat"]
    assert buyers.contact(conn, "bob")["last_contact_ts"] == NOW + 100
    assert buyers.contact(conn, "cat")["roles"] == ["faver"]
