import json

import pytest
from fixtures_th import (
    BUDGETS,
    CLOSED_TITLES,
    CROSS_POST,
    GOLD,
    GOLD_CHARACTERS,
    LONG_FIRST_POST,
    NOW,
)

from mcp_devian import db, drafts, th_match
from mcp_devian.config import Config, Posting

HOUR, DAY = 3600, 86400
BY_NAME = {c["name"]: c for c in GOLD_CHARACTERS}


def offered(title, snippet, chars=GOLD_CHARACTERS):
    prof = th_match.lf_profile(title, snippet)
    return {c["name"] for c in chars
            if (f := th_match.character_fit(prof, c, frozenset({"usd"}), title, snippet)).score > 0 and not f.blockers}


# --- Золотой набор ---

@pytest.mark.parametrize("title,snippet,expected", GOLD, ids=[g[0] for g in GOLD])
def test_gold_thread(title, snippet, expected):
    assert offered(title, snippet) == expected


def test_gold_precision_and_recall():
    tp = fp = fn = 0
    for title, snippet, gold in GOLD:
        got = offered(title, snippet)
        tp, fp, fn = tp + len(got & gold), fp + len(got - gold), fn + len(gold - got)
    assert tp / (tp + fp) >= 0.80
    assert tp / (tp + fn) >= 0.95


def test_budget_phrasings():
    ok = sum(th_match.lf_profile("LF: dragons", text).budget == want for text, want in BUDGETS)
    assert ok >= 10, [(t, th_match.lf_profile("LF", t).budget) for t, _ in BUDGETS]


def test_budget_range_takes_upper_bound_and_keeps_currency():
    assert th_match.lf_profile("LF cats", "budget 1500-3000 pts").budget == (3000.0, "points")
    assert th_match.lf_profile("LF cats", "budget $15-40").budget == (40.0, "usd")
    # «No offers over $50» — это бюджет, а не «без предложений».
    prof = th_match.lf_profile("LF cats", "No offers over $50")
    assert prof.budget == (50.0, "usd") and not prof.no_offers


# --- «Не предлагать» ---

def test_not_looking_for_anthro_blocks_anthro_dragon():
    prof = th_match.lf_profile("LF: dragons", "NOT looking for anthro ones")
    fit = th_match.character_fit(prof, BY_NAME["Vex"])
    assert "anthro" in prof.negated and fit.blockers
    assert "looking" not in prof.negated


def test_negation_with_qualifier_blocks_only_that_combination():
    prof = th_match.lf_profile("LF cats", "no cats with pastel colors")
    assert th_match.character_fit(prof, BY_NAME["Mochi"]).blockers
    plain_cat = {"name": "Tom", "price": "$10", "tags": ["cat", "gothic"]}
    fit = th_match.character_fit(prof, plain_cat)
    assert not fit.blockers and fit.score >= 40


def test_i_dont_want_wolves():
    prof = th_match.lf_profile("LF: OCs", "i don't want wolves")
    assert th_match.character_fit(prof, BY_NAME["Ember"]).blockers
    assert not th_match.character_fit(prof, BY_NAME["Mochi"]).blockers


def test_no_list_negates_every_item():
    prof = th_match.lf_profile("LF: OCs", "no hybrids or customs")
    assert {"hybrid", "custom"} <= prof.negated
    for tag in ("hybrid", "customs"):
        ch = {"name": "X", "price": "$5", "tags": ["dragon", tag]}
        assert th_match.character_fit(prof, ch).blockers


def test_politeness_is_not_a_negation():
    prof = th_match.lf_profile("LF: dragons", "No pressure, no worries if not! no rush. Not picky about colors")
    assert prof.negated == set() and prof.negated_text == []


def test_negation_that_empties_request_blocks():
    prof = th_match.lf_profile("LF: wolves", "anything except canines")
    assert prof.contradictory and not prof.open_call
    assert all(th_match.character_fit(prof, c).blockers for c in GOLD_CHARACTERS)


# --- Виды и общие слова ---

@pytest.mark.parametrize("title,name", [
    ("LF: canines", "Ember"),
    ("LF: Wyverns/drakes", "Vex"),
    ("LF: werewolves", "Ember"),
    ("LF: felines", "Mochi"),
])
def test_hypernyms(title, name):
    assert name in offered(title, None)


def test_werewolf_character_fits_werewolves_thread():
    ww = {"name": "Howl", "price": "$20", "tags": ["werewolf"]}
    assert offered("LF: werewolves", None, [ww]) == {"Howl"}


def test_generic_tags_never_match_alone():
    assert "Sky" not in offered("LF: bug ocs", None)
    assert offered("LF: bug ocs", None) == {"Luna"}


def test_body_type_needs_species():
    # «feral cats» не подходит ферал-волку только из-за «feral».
    assert "Ember" not in offered("LF: feral cats", None)


def test_specific_request_without_species_needs_matching_tag():
    pastel = {"name": "Puff", "price": "$10", "tags": ["pastel"]}
    plain = {"name": "Rex", "price": "$10", "tags": ["dino"]}
    assert offered("LF: pastel characters", None, [pastel, plain]) == {"Puff"}
    assert offered("LF sky themed OCs", None, [plain]) == set()


# --- Бюджет и оплата ---

def test_budget_blocks_only_in_same_currency():
    usd = th_match.lf_profile("LF: wolves", "budget $20")
    pts = th_match.lf_profile("LF: wolves", "budget 2k pts")
    fit = th_match.character_fit(usd, BY_NAME["Ember"])  # $40 > $20
    assert any("выше бюджета" in b for b in fit.blockers)
    fit = th_match.character_fit(pts, BY_NAME["Ember"])  # пункты с долларами не сравниваем
    assert not fit.blockers and any("другой валюте" in r for r in fit.reasons)
    cheap = th_match.character_fit(th_match.lf_profile("LF: wolves", "budget $50"), BY_NAME["Ember"])
    assert not cheap.blockers and any("в бюджете" in r for r in cheap.reasons)


def test_pays_only_blocks_what_she_does_not_accept():
    prof = th_match.lf_profile("LF: wolves", "points only!")
    assert prof.pays_only == "points"
    assert th_match.character_fit(prof, BY_NAME["Ember"]).blockers
    assert not th_match.character_fit(prof, BY_NAME["Ember"], frozenset({"usd", "points"})).blockers
    paypal = th_match.lf_profile("LF: wolves", "paying via paypal invoice only")
    assert paypal.pays_only == "paypal" and not th_match.character_fit(paypal, BY_NAME["Ember"]).blockers


def test_long_first_post_is_fully_understood():
    prof = th_match.lf_profile("LF: characters", LONG_FIRST_POST)
    assert prof.budget == (40.0, "usd")
    assert {"anthro", "hybrid", "custom"} <= prof.negated
    assert prof.pays_only == "paypal" and prof.pays == {"usd"}
    assert {"dragon", "wolf"} <= prof.wanted_species


@pytest.mark.parametrize("title", CLOSED_TITLES)
def test_closed_and_paused_titles(title):
    prof = th_match.lf_profile(title, None)
    assert prof.closed or prof.paused
    assert all(th_match.character_fit(prof, c).blockers for c in GOLD_CHARACTERS)


def test_edited_first_post_closes_thread():
    assert th_match.lf_profile("LF: dragons", "FOUND! thank you all").closed
    assert not th_match.lf_profile("LF: dragons", "My commissions are closed, but I'm buying").closed
    assert not th_match.lf_profile("LF: dragons, not looking for anthros", None).closed


def test_profile_dict_is_json():
    data = th_match.profile_dict(th_match.lf_profile("LF: wolves", LONG_FIRST_POST))
    assert json.loads(json.dumps(data))["budget"] == [40.0, "usd"]
    assert data["negated"] == sorted(data["negated"])


# --- Шанс и дубли ---

def test_opportunity_score():
    fresh = {"published_ts": NOW - 2 * HOUR, "posts": 2, "author": "Bo", "extra": json.dumps({"last_poster": "bo"})}
    assert th_match.opportunity_score(fresh, NOW) == 15 + 10 + 5
    mid = {"published_ts": NOW - 2 * DAY, "posts": 8, "author": "Bo", "extra": "{}"}
    assert th_match.opportunity_score(mid, NOW) == 8 + 5
    crowded = {"published_ts": NOW - 10 * DAY, "posts": 41, "author": "Bo", "extra": None}
    assert th_match.opportunity_score(crowded, NOW) == -10


def test_thread_key_groups_cross_posts():
    a, b = CROSS_POST
    assert th_match.thread_key(a["author"], a["title"]) == th_match.thread_key(b["author"], b["title"])
    assert th_match.thread_key("other", a["title"]) != th_match.thread_key(a["author"], a["title"])


# --- Работа с базой ---

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


def lf_item(conn, tid, title, author="buyer", snippet=None, ago=HOUR, posts=2, last_poster=None,
            match_kind="lf", reason="ищут персов"):
    item = db.Item(id=tid, source="toyhouse", title=title, url=f"https://toyhou.se/~forums/13.x/{tid[3:]}.t",
                   author=author, place="Adoption Center", posts=posts, activity_ts=NOW - ago,
                   published_ts=NOW - ago - HOUR, snippet=snippet, matched=match_kind is not None,
                   match_kind=match_kind, match_reason=reason, wants_characters=True,
                   extra={"last_poster": last_poster or author})
    db.upsert_item(conn, item, NOW)
    conn.commit()


def cfg(**kw):
    return Config(posting=Posting(characters=list(GOLD_CHARACTERS), **kw))


def test_replied_threads_from_log_and_last_poster(conn):
    lf_item(conn, "th:1", "LF: wolves")
    lf_item(conn, "th:2", "LF: cats", last_poster="MeArtist")
    lf_item(conn, "th:3", "LF: dragons")
    drafts.log_sent(conn, "th_reply", "th:1", "https://toyhou.se/1.ember", True, now=NOW - HOUR)
    drafts.log_sent(conn, "th_reply", "th:3", "https://toyhou.se/3.vex", True, now=NOW - 60 * DAY)
    assert th_match.replied_threads(conn, "meartist", NOW - 30 * DAY) == {"th:1", "th:2"}


def test_suggestions_rank_filter_and_dedupe(conn):
    lf_item(conn, "th:10", "LF: wolves!!", author="A", ago=HOUR, posts=1)
    lf_item(conn, "th:11", "LF: wolves", author="B", ago=3 * HOUR, posts=60)
    lf_item(conn, "th:12", "LF dragons [CLOSED]", author="C")  # старый ряд, ещё помеченный lf
    lf_item(conn, "th:13", "LF: cats", author="MeArtist")
    lf_item(conn, "th:14", "LF: felines", author="D", last_poster="meartist")
    lf_item(conn, "th:15", "LF: moths", author="E", snippet="budget $10")  # Luna $30 дороже
    lf_item(conn, "th:16", "[LF] Wolves!", author="a", ago=2 * HOUR)  # кросс-пост th:10
    out = th_match.suggestions(conn, cfg(th_username="MeArtist"), now=NOW)
    assert [s["id"] for s in out] == ["th:10", "th:11"]
    assert [ch["name"] for ch, _ in out[0]["fits"]] == ["Ember"]
    assert out[0]["rank"] > out[1]["rank"]

    text = th_match.render_suggestions(conn, cfg(th_username="MeArtist"), now=NOW)
    assert text.startswith(th_match.UNTRUSTED_HEADER)
    assert "[th:10] LF: wolves!!" in text and "Ember — " in text and "th:12" not in text
    assert "th:13" not in text and "th:14" not in text and "th:15" not in text

    shown = th_match.render_suggestions(conn, cfg(th_username="MeArtist"), show_blocked=True, limit=30, now=NOW)
    for tid in ("th:12", "th:13", "th:14", "th:15"):
        assert tid in shown
    assert "это её тред" in shown and "уже отвечала" in shown and "мешает:" in shown
    assert "выше бюджета" in shown


def test_suggestions_empty(conn):
    lf_item(conn, "th:20", "LF: robots")
    assert th_match.render_suggestions(conn, cfg(), now=NOW) == th_match.EMPTY


# --- Разбор запроса: находки проверки ---

DRAGON = {"name": "Ember", "tags": ["dragon"], "price": "$20"}
CAT = {"name": "Mochi", "tags": ["cat"], "price": "$15"}


def fit(title, snippet, ch, accepts=frozenset({"usd"})):
    return th_match.character_fit(th_match.lf_profile(title, snippet), ch, accepts, title, snippet)


@pytest.mark.parametrize("title,snippet,ch", [
    ("LF: wolves | O: $, art, trades", "or trade my dragon OCs", DRAGON),
    ("LF: wolves", "I already have way too many cats lol, so wolves please!", CAT),
    ("LF: wolves", "My other OC is a dragon...", DRAGON),
])
def test_species_from_first_post_do_not_override_title(title, snippet, ch):
    f = fit(title, snippet, ch)
    assert f.blockers and "вид: " not in "; ".join(f.reasons)


@pytest.mark.parametrize("snippet", [
    "Found a couple I like already but still looking!",
    "EDIT: found one! still looking for more",
    "Update: found some, open for more",
    "Closed species are fine too!",
    "paused offers on art though, still buying",
])
def test_live_threads_not_marked_closed(snippet):
    assert th_match.thread_status("LF: wolves", snippet) == (False, False)


def test_edited_found_still_closes():
    assert th_match.thread_status("LF: wolves", "FOUND, thanks all!")[0]
    assert th_match.thread_status("LF: wolves", "EDIT: closed")[0]


@pytest.mark.parametrize("title,wanted", [
    ("LF: felines", {"cat", "tiger", "lion"}), ("LF big cats", {"tiger", "lion"}),
    ("LF: rodents", {"rat", "mouse"}), ("LF: equines", {"horse", "unicorn"}),
    ("LF: undead", {"zombie", "skeleton", "vampire", "ghost"}),
])
def test_broad_terms_follow_taxonomy(title, wanted):
    assert wanted <= th_match.lf_profile(title, "").wanted_species


@pytest.mark.parametrize("tags", [["elf"], ["vampire"], ["mermaid"], ["witch"]])
def test_humanoid_species_fit_humanoid_request(tags):
    f = fit("LF: humanoids", "", {"name": "Lia", "tags": tags})
    assert f.score > 0 and not f.blockers


def test_unknown_body_is_not_a_blocker():
    f = fit("LF: humanoids", "", {"name": "Nameless", "tags": ["pastel"]})
    assert not f.blockers and "проверь" in "; ".join(f.reasons)
    assert fit("LF: humanoids", "", {"name": "A", "tags": ["anthro", "wolf"]}).blockers  # тип известен и другой


def test_character_with_two_currencies():
    ch = {"name": "X", "tags": ["wolf"], "price": "$20 / 2000 pts"}
    accepts = th_match.accepts_of([ch])
    assert accepts == {"usd", "points"}
    assert th_match.char_prices(ch) == [(20.0, "usd"), (2000.0, "points")]
    assert not fit("LF: wolves, points only please", "", ch, accepts).blockers
    over = fit("LF: wolves", "budget 1500 pts", ch, accepts)
    assert any("2000 pts" in b and "1500 pts" in b for b in over.blockers)


@pytest.mark.parametrize("title,tag", [("LF: dragons", "dragonfly"), ("LF: wolves", "wolfsbane")])
def test_her_tags_match_whole_words_only(title, tag):
    assert fit(title, "", {"name": "Buzz", "tags": [tag]}).blockers


def test_common_word_name_does_not_override_species():
    sky_cat = {"name": "Sky", "tags": ["cat"]}
    assert fit("LF: dragons", "Sky themed ones preferred!", sky_cat).blockers
    named = fit("LF: dragons", "or the cat named Sky from your gallery", sky_cat)
    assert not named.blockers and "по имени" in "; ".join(named.reasons)


def test_head_count_is_not_a_budget():
    assert th_match.lf_profile("LF dogs", "Paying $20 to 3 people who have dogs").budget is None


@pytest.mark.parametrize("snippet,budget", [
    ("$40 budget", (40.0, "usd")), ("My max is $30", (30.0, "usd")),
    ("nothing over 1500 pts", (1500.0, "points")), ("Prices: $10 - $15", (15.0, "usd")),
])
def test_more_budget_phrasings(snippet, budget):
    assert th_match.lf_profile("LF: wolves", snippet).budget == budget


@pytest.mark.parametrize("title,status", [
    ("LF wolves (paused for now)", (False, True)), ("LF wolves [CLOSED FOR NOW, will reopen]", (True, False)),
    ("LF wolves (FOUND, ty!)", (True, False)), ("LF wolves [not closed]", (False, False)),
    ("LF characters with eyes closed", (False, False)), ("LF: wolves (closed species ok)", (False, False)),
])
def test_title_status_gaps(title, status):
    assert th_match.thread_status(title, "") == status
