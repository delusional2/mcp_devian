"""Раффл: приз не деньги, честные правила, сбор с её работы, проверяемая жеребьёвка."""

import hashlib

import pytest

from mcp_devian import db, raffle, selling
from mcp_devian.bids import Comment

NOW = 1_800_000_000.0
DAY = 86400
TEMPLATE = "RAFFLE: {prize}!\n{rules}\nEnds {ends}. Commitment: {commitment}"
WIN = "Winner of {title}: {winner}! seed {seed}, list {list_hash}, entrants: {entrants}. {verify}"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "raffle.sqlite3")
    yield c
    c.close()


def make(conn, **kw):
    args = {"title": "Moth raffle", "prize": "Moth dragon adopt", "ends": NOW + 3 * DAY, "entry_method": "plus",
            "template": TEMPLATE, "now": NOW}
    args.update(kw)
    return raffle.create(conn, **args)


class FakeClient:
    def __init__(self, comments=(), faves=(), author="mothy"):
        self.comments = list(comments)
        self.faves = list(faves)
        self.author = author
        self.comments_truncated = False
        self.calls = []

    def whoami(self):
        return {"username": "mothy"}

    def deviations_brief(self, uuids):
        return {u: {"author": self.author} for u in uuids}

    def comments_page(self, uuid, offset=0, limit=50):
        self.calls.append(("comments", uuid, offset))
        rows = self.comments[offset:offset + limit]
        return rows, (offset + limit if offset + limit < len(self.comments) else None)

    def whofaved_page(self, uuid, offset=0, limit=50):
        self.calls.append(("faves", uuid, offset))
        rows = self.faves[offset:offset + limit]
        return rows, (offset + limit if offset + limit < len(self.faves) else None)


def c(user, text, ts, cid=None):
    return Comment(comment_id=cid or f"{user}{ts}", parent_id=None, user=user, posted_ts=ts, text=text)


# --- создание ---

@pytest.mark.parametrize("prize", ["500 points", "100 pts", "$20", "20 USD", "500 руб", "money", "Cash prize",
                                   "1000 поинтов"])
def test_cash_or_points_prize_refused(conn, prize):
    res = make(conn, prize=prize)
    assert not res["ok"] and "не деньги" in res["reason"]


def test_adopts_prize_is_fine_and_commitment_is_sha256_of_seed(conn):
    res = make(conn, prize="2 moth adopts + headshot")
    assert res["ok"]
    row = raffle.get(conn, res["id"])
    assert len(row["seed_hex"]) == 32
    assert row["commitment"] == hashlib.sha256(row["seed_hex"].encode()).hexdigest() == res["commitment"]
    assert res["commitment"] in res["text"] and row["seed_hex"] not in res["text"]
    for must in ("Free to enter", "one account per person", "no extra entries", "UTC+03:00", "MSK"):
        assert must in res["text"]


def test_template_rules(conn):
    assert "шаблона" in make(conn, template="")["reason"]
    assert "{rules}" in make(conn, template="Win {prize}! {ends} {commitment}")["reason"]
    assert make(conn, template="Win {prize}! {rules}")["ok"]  # конец и commitment уже в {rules}
    res = make(conn, template=TEMPLATE + " Most reposts wins a bonus!")
    assert not res["ok"] and "одинаковый шанс" in res["reason"]
    assert not make(conn, entry_method="retweet")["ok"]
    assert not make(conn, ends=NOW - 10)["ok"]


def test_watch_required_is_optional_and_flagged(conn):
    res = make(conn, watch_required=True)
    assert "Optional: watching me" in res["text"]
    assert any("правило DA не описано" in f for f in res["flags"])
    assert not any("правило DA не описано" in f for f in make(conn)["flags"])


# --- сбор ---

def test_collect_comment_plus_dedupe_and_exclusions(conn):
    res = make(conn)
    raffle.attach(conn, res["id"], "J-1", "https://www.deviantart.com/mothy/journal/x-1")
    raffle.withdraw(conn, res["id"], "quitter", now=NOW)
    ends = res["ends_ts"]
    comments = [c("Fan1", "+", NOW + 10), c("fan1", "+ again", NOW + 20), c("mothy", "+", NOW + 30),
                c("Late", "+", ends + 60), c("quitter", "+", NOW + 40), c("Lurker", "so cute!", NOW + 50),
                c("Changed", "+", NOW + 60), c("Changed", "-", NOW + 70), c("Mira22", "+", NOW + 80),
                c("mira_23", "+", NOW + 90), c("blocked", "+", NOW + 95)]
    fake = FakeClient(comments)
    out = raffle.collect(conn, res["id"], fake, now=ends + 120, owner="mothy")
    assert out["ok"] and sorted(out["entrants"]) == ["Fan1", "Mira22", "blocked", "mira_23"]
    excluded = dict(out["excluded"])
    assert "Late" in excluded and "quitter" not in out["entrants"] and "Changed" in excluded
    assert "mothy" not in excluded
    assert {n for n, _ in out["flagged"]} == {"Mira22", "mira_23"}  # похожие ники — только пометка
    assert raffle.get(conn, res["id"])["status"] == "ended"


def test_collect_refuses_foreign_or_unattached(conn):
    res = make(conn)
    assert not raffle.collect(conn, res["id"], FakeClient(), now=NOW, owner="mothy")["ok"]
    raffle.attach(conn, res["id"], "J-1")
    out = raffle.collect(conn, res["id"], FakeClient(author="other"), now=NOW, owner="mothy")
    assert not out["ok"] and "не её" in out["text"]


def test_collect_faves_respects_call_budget_and_watch_flag(conn):
    res = make(conn, entry_method="fave", watch_required=True)
    raffle.attach(conn, res["id"], "D-1")
    faves = [{"username": "w1", "is_watching": True, "ts": NOW + 1}, {"username": "nw", "is_watching": False,
                                                                      "ts": NOW + 2}]
    fake = FakeClient(faves=faves)
    out = raffle.collect(conn, res["id"], fake, now=NOW + DAY, owner="mothy", max_calls=3)
    assert fake.calls == [("faves", "D-1", 0)]  # одна страница: дальше никого (has_more = false)
    assert sorted(out["entrants"]) == ["nw", "w1"] and dict(out["flagged"])["nw"].startswith("не подписан")
    assert raffle.get(conn, res["id"])["status"] == "open"  # ещё идёт


def test_incomplete_read_does_not_end_raffle(conn):
    res = make(conn, entry_method="fave")
    raffle.attach(conn, res["id"], "D-1")
    many = [{"username": f"user{i}", "ts": NOW} for i in range(150)]
    out = raffle.collect(conn, res["id"], FakeClient(faves=many), now=res["ends_ts"] + 10, owner="mothy", max_calls=2)
    assert not out["complete"] and raffle.get(conn, res["id"])["status"] == "open"
    assert not raffle.draw(conn, res["id"])["ok"]


@pytest.mark.parametrize("n", [100, 150, 230])
def test_big_raffle_resumes_and_can_be_drawn(conn, n):
    """Регрессия: при 100+ участниках «прочитано всё» никогда не наступало — раффл нельзя было разыграть."""
    res = make(conn, entry_method="fave")
    raffle.attach(conn, res["id"], "D-1")
    fake = FakeClient(faves=[{"username": f"user{i}", "ts": NOW + i} for i in range(n)])
    ends = res["ends_ts"]
    for k in range(10):
        out = raffle.collect(conn, res["id"], fake, now=ends + 10 + k, owner="mothy", max_calls=3)
        if out["complete"]:
            break
    assert out["complete"] and len(out["entrants"]) == n
    pages = -(-n // 50)
    assert len(fake.calls) == pages and len({c[2] for c in fake.calls}) == pages  # каждую страницу — один раз
    assert raffle.get(conn, res["id"])["status"] == "ended" and raffle.draw(conn, res["id"])["ok"]
    calls = len(fake.calls)
    raffle.collect(conn, res["id"], fake, now=ends + 100, owner="mothy")
    assert len(fake.calls) == calls  # дочитанный итоговый проход повторно не читаем


def test_pass_started_before_end_is_redone_after_end(conn):
    res = make(conn)
    raffle.attach(conn, res["id"], "J-1")
    fake = FakeClient([c(f"u{i}", "+", NOW + i) for i in range(120)])
    first = raffle.collect(conn, res["id"], fake, now=res["ends_ts"] - 100, owner="mothy", max_calls=2)
    assert not first["complete"]
    fake.comments.append(c("lastminute", "+", res["ends_ts"] - 50))
    outs = [raffle.collect(conn, res["id"], fake, now=res["ends_ts"] + k + 1, owner="mothy") for k in range(3)]
    assert outs[-1]["complete"] and "lastminute" in outs[-1]["entrants"] and len(outs[-1]["entrants"]) == 121
    assert raffle.get(conn, res["id"])["status"] == "ended"


# --- жеребьёвка ---

def _ended(conn, names):
    res = make(conn)
    raffle.attach(conn, res["id"], "J-1")
    comments = [c(n, "+", NOW + i) for i, n in enumerate(names)]
    raffle.collect(conn, res["id"], FakeClient(comments), now=res["ends_ts"] + 1, owner="mothy")
    return res


def test_draw_needs_end(conn):
    res = make(conn)
    assert "не закончен" in raffle.draw(conn, res["id"])["text"]


def test_draw_deterministic_and_verifiable(conn):
    res = _ended(conn, ["Zed", "amy", "Bob", "carl"])
    first = raffle.draw(conn, res["id"], now=NOW + 5 * DAY)
    again = raffle.draw(conn, res["id"], now=NOW + 6 * DAY)
    assert first["ok"] and first["winner"] == again["winner"] and first["entrants"] == ["amy", "bob", "carl", "zed"]
    assert first["list_hash"] == hashlib.sha256(b"amy\nbob\ncarl\nzed").hexdigest()
    assert raffle.verify(first["seed"], res["commitment"], ["Bob", "@zed", "AMY", "carl"], first["winner"])
    others = [n for n in first["entrants"] if n != first["winner"]]
    assert not raffle.verify(first["seed"], res["commitment"], first["entrants"], others[0])
    assert not raffle.verify("0" * 32, res["commitment"], first["entrants"], first["winner"])


def test_changed_list_changes_hash():
    assert raffle.list_hash(["amy", "bob"]) != raffle.list_hash(["amy", "bob", "carl"])
    seed = "ab" * 16
    picks = {raffle.pick_index(seed, raffle.list_hash([f"u{i}" for i in range(n)]), n) for n in range(5, 15)}
    assert len(picks) > 1


def test_announcement_uses_her_template_with_seed_and_recipe(conn):
    res = _ended(conn, ["amy", "bob", "carl"])
    out = raffle.announcement(conn, res["id"], template=WIN)
    assert out["ok"] and out["seed"] in out["text"] and out["list_hash"] in out["text"]
    assert "@" + out["winner"] in out["text"] and "HMAC-SHA256" in out["text"] and "mod 3" in out["text"]
    assert not raffle.announcement(conn, res["id"], template="Winner {winner}!")["ok"]
    partial = raffle.announcement(conn, res["id"], template="Winner {winner} {seed} {list_hash}")
    assert partial["ok"] and any("{entrants}" in n for n in partial["notes"])


def test_announcement_is_a_draft_only(conn, monkeypatch):
    from mcp_devian import posting

    res = _ended(conn, ["amy", "bob"])
    selling.ensure(conn)
    conn.execute("INSERT INTO selling_templates (kind, text, updated_ts) VALUES ('raffle_winner', ?, 0)", (WIN,))
    conn.commit()
    made = []
    monkeypatch.setattr(posting, "prepare_journal",
                        lambda conn, cfg, title, text, tags, now=None, helper_note="":
                        made.append((title, text, helper_note)) or ("d1", text))
    d, text = raffle.prepare_announcement(conn, object(), res["id"])
    assert d == "d1" and made and made[0][0].startswith("Raffle results")
    # Что выбрал помощник, а не она, — помечено для превью.
    assert "{verify}" in made[0][2] and "Raffle results" in made[0][2] and "теги" in made[0][2]
    raffle.prepare_announcement(conn, object(), res["id"], title="Moth raffle winner!")
    assert made[1][0] == "Moth raffle winner!" and "название" not in made[1][2]
    assert conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0] == 0


def test_journal_send_attaches_raffle(conn, tmp_path, monkeypatch):
    from mcp_devian import posting
    from mcp_devian.config import Config

    class Fake:
        def is_connected(self):
            return True

        def require_scopes(self, *a):
            pass

        def whoami(self):
            return {"username": "mothy"}

        def create_journal(self, title, body, is_mature=False, tags=()):
            return {"deviationid": "J-9", "url": "https://www.deviantart.com/mothy/journal/r-9"}

    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setattr(posting, "da_client", lambda cfg: Fake())
    cfg = Config()
    cfg.deviantart.my_username = "mothy"
    res = make(conn)
    d, _ = posting.prepare_journal(conn, cfg, "Moth raffle", res["text"], ["raffle"], now=NOW, raffle_id=res["id"])
    assert "журнал опубликован" in posting.send(conn, cfg, d, now=NOW)
    row = raffle.get(conn, res["id"])
    assert row["deviation_id"] == "J-9" and row["journal_url"].endswith("r-9")


# --- регрессии проверки W4 ---

@pytest.mark.parametrize("prize", ["Рубиновый дракон", "Point the fox", "Ruby moth adopt", "Pointy-eared cat",
                                   "Cashmere sheep OC"])
def test_prize_words_inside_names_are_not_money(conn, prize):
    assert make(conn, prize=prize)["ok"]


@pytest.mark.parametrize("prize", ["500 рублей", "5k points", "DA points", "Core membership + 100 points"])
def test_more_money_prizes_still_refused(conn, prize):
    assert not make(conn, prize=prize)["ok"]


def test_top_art_is_not_a_ranked_mechanic(conn):
    assert make(conn, template=TEMPLATE + " Top art raffle of the month!")["ok"]
    assert not make(conn, template=TEMPLATE + " Top RT gets a bonus")["ok"]


def test_rules_say_alts_may_be_disqualified(conn):
    text = make(conn)["text"]
    assert "may be disqualified" in text and "are disqualified" not in text


def test_verify_recipe_followed_literally_gives_the_winner(conn):
    """Регрессия: рецепт не говорил, что ключ — hex-текст seed, сообщение — hex list_hash, без перевода строки."""
    import hmac as hm

    res = _ended(conn, ["Zed", "amy", "Bob", "carl", "dora"])
    out = raffle.announcement(conn, res["id"], template=WIN)
    recipe = raffle.VERIFY_RECIPE
    for must in ("as ASCII", "not decoded", "no newline at the end", "lowercase hex", "count from 0"):
        assert must in recipe
    text = "\n".join(sorted(n.lower() for n in ["Zed", "amy", "Bob", "carl", "dora"]))
    lh = hashlib.sha256(text.encode("utf-8")).hexdigest()
    idx = int(hm.new(out["seed"].encode("ascii"), lh.encode("ascii"), hashlib.sha256).hexdigest(), 16) % 5
    assert lh == out["list_hash"] and sorted(n.lower() for n in ["Zed", "amy", "Bob", "carl", "dora"])[idx] == out["winner"]


def test_raffle_journal_preview_marks_helper_words(conn, tmp_path, monkeypatch):
    from mcp_devian import posting
    from mcp_devian.config import Config

    class Fake:
        def is_connected(self):
            return True

        def require_scopes(self, *a):
            pass

        def whoami(self):
            return {"username": "mothy"}

    monkeypatch.setattr(posting, "da_client", lambda cfg: Fake())
    cfg = Config()
    cfg.deviantart.my_username = "mothy"
    res = make(conn)
    _, preview = posting.prepare_journal(conn, cfg, "Moth raffle", res["text"], ["raffle"], now=NOW,
                                         raffle_id=res["id"])
    assert "помощник вписал" in preview and "{rules}" in preview and "её, без изменений" not in preview
    _, plain = posting.prepare_journal(conn, cfg, "Sale", "My sale!", now=NOW + 2 * DAY)
    assert "её, без изменений" in plain


def test_journal_without_url_says_so(conn, tmp_path, monkeypatch):
    from mcp_devian import posting
    from mcp_devian.config import Config

    class Fake:
        def is_connected(self):
            return True

        def require_scopes(self, *a):
            pass

        def whoami(self):
            return {"username": "mothy"}

        def create_journal(self, title, body, is_mature=False, tags=()):
            return {"deviationid": "J-9", "url": ""}  # по документации DA ссылки в ответе нет

    monkeypatch.setattr(posting, "DATA_DIR", tmp_path)
    monkeypatch.setattr(posting, "SENDING_ENABLED", True)
    monkeypatch.setattr(posting, "da_client", lambda cfg: Fake())
    cfg = Config()
    cfg.deviantart.my_username = "mothy"
    res = make(conn)
    d, _ = posting.prepare_journal(conn, cfg, "Moth raffle", res["text"], ["raffle"], now=NOW, raffle_id=res["id"])
    msg = posting.send(conn, cfg, d, now=NOW)
    assert "ссылку DeviantArt не назвал" in msg and "J-9" in msg
    assert raffle.get(conn, res["id"])["deviation_id"] == "J-9"


# --- регрессии ревью 0.5 ---

def test_template_with_own_ends_and_commitment_does_not_repeat_them(conn):
    """Регрессия: {ends}, {rules} и {commitment} были обязательны, а {rules} уже содержал конец и хеш —
    в каждом журнале время и 64-значный хеш стояли дважды."""
    res = make(conn)
    assert res["text"].count(res["commitment"]) == 1 and res["text"].count(res["ends_text"]) == 1
    assert "seed behind the commitment is revealed" in res["text"]
    only_rules = make(conn, template="RAFFLE: {prize}!\n{rules}")
    assert only_rules["text"].count(only_rules["commitment"]) == 1
    assert only_rules["text"].count(only_rules["ends_text"]) == 1
    # В базе правила — полные (с концом и commitment).
    row = raffle.get(conn, res["id"])
    assert res["commitment"] in row["rules"] and res["ends_text"] in row["rules"]


def test_offer_opt_out_does_not_drop_raffle_entrant(conn):
    """Регрессия: «no thanks» на предложение или выход из пинглиста исключали из раффла с пометкой
    «вышел(ла) из раффла» — против опубликованных правил «Everyone has the same chance»."""
    from mcp_devian import buyers

    buyers.opt_out(conn, ["Zed"], "no thanks", now=NOW)
    res = make(conn, entry_method="fave")
    raffle.attach(conn, res["id"], "D-1")
    fake = FakeClient(faves=[{"username": "Ann", "ts": NOW + 1}, {"username": "Zed", "ts": NOW + 2}])
    out = raffle.collect(conn, res["id"], fake, now=res["ends_ts"] + 10, owner="mothy")
    assert sorted(out["entrants"]) == ["Ann", "Zed"] and not out["excluded"]


def test_opted_out_winner_is_not_tagged(conn):
    from mcp_devian import buyers

    res = _ended(conn, ["amy"])
    buyers.opt_out(conn, ["amy"], "stop", now=NOW)
    out = raffle.announcement(conn, res["id"], template=WIN)
    assert out["ok"] and "@amy" not in out["text"] and "amy" in out["text"]
    assert any("не отмечать" in n for n in out["notes"])
