import pytest

from mcp_devian import db, pinglist, selling
from mcp_devian.bids import Comment
from mcp_devian.db import Item

T0 = 1_790_000_000.0


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p.sqlite3")
    yield c
    c.close()


@pytest.mark.parametrize("text", [
    "ping me!", "Please add me to your pinglist", "pinglist pls", "can I be on the pinglist?",
    "ping me if you make more dragons", "Could you add me to the ping list? :)", "put me on your pinglist please",
    "пингани меня", "добавь в пинглист пожалуйста", "хочу в пинг-лист", "Добавьте меня в пинглист!",
    "pingueame porfa",
])
def test_opt_in(text):
    assert pinglist.classify(text) == "in"


@pytest.mark.parametrize("text", [
    "remove me from the pinglist", "unping me please", "don't ping me anymore", "please stop pinging me",
    "убери меня из пинглиста", "не пингуй меня больше", "I don't want to be on the pinglist anymore",
    "remove me pls", "take me off your ping list", "удалите меня из пинг-листа",
])
def test_opt_out(text):
    assert pinglist.classify(text) == "out"


@pytest.mark.parametrize("text", [
    "did you ping me?", "thanks for the ping!", "ping me on discord", "SB $10", "remove me from the auction",
    "she pinged me", "I'm already on your pinglist", "Love this!! <3", "do you have a pinglist?",
    "is there a pinglist?", "пинг понг", "remove my bid please",
])
def test_not_a_signal(text):
    assert pinglist.classify(text) is None


def own(conn, iid, mine=True):
    db.upsert_item(conn, Item(id=iid, source="deviantart", title="t", url=f"https://www.deviantart.com/her/art/{iid}",
                              author="her" if mine else "other", lot_type="auction", own=mine, published_ts=T0),
                   now=T0)
    return iid


def c(cid, user, text, ts, parent=None):
    return Comment(comment_id=cid, parent_id=parent, user=user, posted_ts=ts, text=text)


def test_ingest_latest_signal_wins_and_is_idempotent(conn):
    comments = [c("1", "Alice", "ping me!", T0), c("2", "bob", "pinglist pls", T0 + 1),
                c("3", "her", "ping me", T0 + 2), c("4", "Alice", "remove me from the pinglist", T0 + 3),
                c("5", "carol", "SB $10", T0 + 4)]
    got = pinglist.ingest(conn, "da:1", comments, owner="her", now=T0 + 10)
    assert got == {"added": ["bob"], "removed": []}       # Alice вошла и вышла, свои комментарии не считаем
    assert [m["user"] for m in pinglist.members(conn)] == ["bob"]
    assert pinglist.ingest(conn, "da:1", comments, owner="her", now=T0 + 20) == {"added": [], "removed": []}


def test_manual_beats_older_comment(conn):
    pinglist.ingest(conn, "da:1", [c("1", "Alice", "ping me", T0)], owner="her", now=T0 + 1)
    assert pinglist.remove(conn, ["@alice"], now=T0 + 5) == ["Alice"]
    pinglist.ingest(conn, "da:1", [c("1", "Alice", "ping me", T0)], owner="her", now=T0 + 6)
    assert pinglist.members(conn) == []
    # Новый комментарий позже ручного удаления — снова в списке.
    pinglist.ingest(conn, "da:2", [c("9", "Alice", "ping me pls", T0 + 7)], owner="her", now=T0 + 8)
    assert [m["user"] for m in pinglist.members(conn)] == ["Alice"]
    assert pinglist.members(conn)[0]["deviation_id"] == "da:2"


def test_ingest_accepts_api_dicts(conn):
    raw = [{"comment_id": "1", "user": "dan", "text": "add me to the pinglist", "posted_ts": T0}]
    assert pinglist.ingest(conn, "da:1", raw, owner="her", now=T0)["added"] == ["dan"]


def test_manual_add_validates(conn):
    added = pinglist.add(conn, ["@Eve", "https://www.deviantart.com/frank", "bad name!", "eve"], now=T0)
    assert added == ["Eve", "frank"]
    with pytest.raises(pinglist.PinglistError):
        pinglist.add(conn, ["bad name!"], now=T0)


def test_mention_comments_chunks(conn):
    own(conn, "da:5")
    own(conn, "da:6")
    pinglist.add(conn, [f"user{i:02d}" for i in range(23)], now=T0)
    res = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="https://www.deviantart.com/her/art/x-5",
                                    title="New dragon")
    assert not res["ok"] and "шаблон" in res["reason"]
    selling.set_template(conn, "pinglist", "New adopt is up! {title}\n{mentions}")
    res = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="https://www.deviantart.com/her/art/x-5",
                                    title="New dragon", per_comment=10, max_comments=2)
    assert res["ok"]
    assert [len(x["users"]) for x in res["comments"]] == [10, 10]
    assert len(res["left_out"]) == 3
    first = res["comments"][0]["text"]
    assert first.startswith("New adopt is up! New dragon\n@user00 @user01")
    assert first.count("@") == 10
    pinglist.mark_pinged(conn, "da:5", res["comments"][0]["users"], now=T0 + 1)
    again = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", title="t", max_comments=5)
    assert sum(len(x["users"]) for x in again["comments"]) == 13
    assert pinglist.mention_comments(conn, deviation_id="da:6", deviation_url="u", title="t",
                                     max_comments=5)["comments"][0]["users"][0] == "user00"


def test_mentions_appended_when_template_has_no_slot(conn):
    own(conn, "da:5")
    pinglist.add(conn, ["amy"], now=T0)
    selling.set_template(conn, "pinglist", "New one up!")
    res = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", title="t")
    assert res["comments"][0]["text"] == "New one up!\n@amy"


def test_per_comment_is_capped(conn):
    own(conn, "da:5")
    pinglist.add(conn, [f"usr{i}" for i in range(40)], now=T0)
    selling.set_template(conn, "pinglist", "{mentions}")
    res = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", title="t", per_comment=50)
    assert max(len(x["users"]) for x in res["comments"]) == pinglist.MAX_PER_COMMENT


def test_render(conn):
    pinglist.add(conn, ["amy", "bob"], now=T0)
    assert "2" in pinglist.render_members(pinglist.members(conn)) and "amy" in pinglist.render_members(
        pinglist.members(conn))


# --- Регрессии после проверки ---

@pytest.mark.parametrize("text", [
    "ping me if the winner backs out!", "can you ping me when the winner is decided?", "ping me if AB is reached",
    "Ping me if the payment fails", "ping me when it ends", "пингани когда оплатишь",
    "пингани если победитель не оплатит", "ping me when you pick the winner", "ping me if she backs out pls",
    "ping me when it sells", "ping me about the price",
])
def test_one_off_ping_requests_are_not_pinglist(text):
    assert pinglist.classify(text) is None


@pytest.mark.parametrize("text", [
    "ping me pls 🥺", "Ping me for future adopts!", "ping me when you post more", "ping me in the future please",
    "пингани пожалуйста", "пингани на новые", "пингуйте меня когда будут ещё", "ping me for the next batch",
])
def test_explicit_future_pings_still_count(text):
    assert pinglist.classify(text) == "in"


def test_backup_bidder_is_not_added(conn):
    got = pinglist.ingest(conn, "da:1", [c("1", "backup", "ping me if the winner backs out!", T0)], owner="her",
                          now=T0 + 1)
    assert got == {"added": [], "removed": []} and pinglist.members(conn) == []


def test_undated_comment_cannot_undo_her_removal(conn):
    pinglist.ingest(conn, "da:1", [c("1", "Alice", "ping me", T0)], owner="her", now=T0 + 1)
    pinglist.remove(conn, ["alice"], now=T0 + 5)
    pinglist.ingest(conn, "da:1", [c("1", "Alice", "ping me", 0)], owner="her", now=T0 + 6)       # тот же
    pinglist.ingest(conn, "da:3", [c("77", "Alice", "ping me", 0)], owner="her", now=T0 + 7)      # другой, без даты
    assert pinglist.members(conn) == []


def test_undated_requests_work_both_ways_once(conn):
    assert pinglist.ingest(conn, "da:1", [c("5", "zed", "ping me", 0)], owner="her", now=T0)["added"] == ["zed"]
    assert pinglist.members(conn)[0]["since"] == T0
    assert pinglist.ingest(conn, "da:1", [c("6", "zed", "remove me from the pinglist", 0)], owner="her",
                           now=T0 + 1)["removed"] == ["zed"]
    pinglist.add(conn, ["zed"], now=T0 + 2)
    pinglist.ingest(conn, "da:1", [c("6", "zed", "remove me from the pinglist", 0)], owner="her", now=T0 + 3)
    assert [m["user"] for m in pinglist.members(conn)] == ["zed"]   # её ручное добавление не перебито повтором


def test_mentions_cap_is_per_deviation_not_per_call(conn):
    own(conn, "da:5")
    pinglist.add(conn, [f"user{i:02d}" for i in range(80)], now=T0)
    selling.set_template(conn, "pinglist", "{mentions}")
    first = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", now=T0)
    users = [u for x in first["comments"] for u in x["users"]]
    assert len(users) == 50
    pinglist.mark_pinged(conn, "da:5", users, now=T0 + 1)
    again = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", now=T0 + 2)
    assert not again["ok"] and "спам" in again["reason"] and len(again["left_out"]) == 30


def test_reserved_users_are_not_offered_twice(conn):
    own(conn, "da:5")
    pinglist.add(conn, [f"user{i:02d}" for i in range(15)], now=T0)
    selling.set_template(conn, "pinglist", "{mentions}")
    a = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", max_comments=1, reserve_users=True,
                                  now=T0)
    b = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", reserve_users=True, now=T0 + 1)
    ua = {u for x in a["comments"] for u in x["users"]}
    ub = {u for x in b["comments"] for u in x["users"]}
    assert len(ua) == 10 and len(ub) == 5 and not ua & ub
    pinglist.release(conn, "da:5", sorted(ua))
    c3 = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", now=T0 + 2)
    assert {u for x in c3["comments"] for u in x["users"]} == ua
    # Черновик старше недели больше не держит места.
    pinglist.reserve(conn, "da:5", sorted(ua), now=T0)
    late = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", now=T0 + pinglist.PENDING_TTL_S + 9)
    assert ua <= {u for x in late["comments"] for u in x["users"]}


def test_repeated_mentions_slot_rejected(conn):
    with pytest.raises(selling.SellingError):
        selling.set_template(conn, "pinglist", "{mentions} and again {mentions}")


def test_long_template_respects_comment_limit(conn):
    from mcp_devian.posting import COMMENT_MAX
    own(conn, "da:5")
    pinglist.add(conn, [f"user{i:02d}" for i in range(10)], now=T0)
    selling.set_template(conn, "pinglist", "x" * (COMMENT_MAX - 30) + " {mentions}")
    res = pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", now=T0)
    assert res["ok"] and all(len(x["text"]) <= COMMENT_MAX for x in res["comments"])
    assert sum(len(x["users"]) for x in res["comments"]) == 10 and len(res["comments"]) > 1
    selling.set_template(conn, "pinglist", "x" * COMMENT_MAX + " {mentions}")
    assert not pinglist.mention_comments(conn, deviation_id="da:5", deviation_url="u", now=T0)["ok"]


def test_ping_only_under_her_own_known_work(conn):
    pinglist.add(conn, ["amy"], now=T0)
    selling.set_template(conn, "pinglist", "{mentions}")
    missing = pinglist.mention_comments(conn, deviation_id="da:404", deviation_url="u")
    assert not missing["ok"] and "нет в базе" in missing["reason"]
    own(conn, "da:7", mine=False)
    other = pinglist.mention_comments(conn, deviation_id="da:7", deviation_url="u")
    assert not other["ok"] and "не её" in other["reason"]
