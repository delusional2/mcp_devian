import sqlite3

import pytest

from mcp_devian import translate_guard as tg
from mcp_devian.translate_guard import TranslateError

NOW = 1_800_000_000.0
RU = ("Новый мотылёк на аукционе! SB 15$, MI 5$, AB 80$.\n"
      "Ставки в комментариях, оплата через Boosty: https://boosty.to/mothmaker\n"
      "Пинг @friend_one, если интересно")
EN = ("New moth up for auction! SB $15, MI $5, AB $80.\n"
      "Bids in the comments, payment via Boosty: https://boosty.to/mothmaker\n"
      "Ping @friend_one if interested")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_targets_normalized_and_toyhouse_refused():
    assert tg.normalize_target("DA") == "deviantart"
    assert tg.normalize_target("deviantart") == "deviantart"
    assert tg.normalize_target("Twitter") == "x"
    for bad in ("toyhouse", "Toyhou.se", "th"):
        with pytest.raises(TranslateError, match="4.8"):
            tg.normalize_target(bad)
    with pytest.raises(TranslateError):
        tg.normalize_target("tumblr")


def test_instruction_carries_her_text_and_rules():
    text = tg.instruction(RU, "da")
    assert RU in text
    low = text.lower()
    assert "ничего не добавляй" in low and "цены" in low and "ссылки" in low
    assert "оба" in text and "advice:translate с text и translation" in text
    assert len(tg.RULES) < 700           # короткая, уходит в каждый ответ
    with pytest.raises(TranslateError):
        tg.instruction(RU, "toyhouse")
    with pytest.raises(TranslateError):
        tg.instruction("   ", "da")


def test_faithfulness_catches_lost_prices_links_handles():
    assert tg.faithfulness(RU, EN) == []
    lost = EN.replace("https://boosty.to/mothmaker", "Boosty").replace("$80", "$90").replace("@friend_one", "")
    warns = " ".join(tg.faithfulness(RU, lost))
    assert "boosty.to/mothmaker" in warns and "80" in warns and "@friend_one" in warns
    extra = EN + "\nSee more at https://example.com"
    assert any("example.com" in w for w in tg.faithfulness(RU, extra))
    assert any("длинн" in w for w in tg.faithfulness("Привет", "Hello! " * 30))


def test_save_marks_text_and_shows_both_versions(conn):
    res = tg.save(conn, RU, EN, "da", now=NOW)
    assert res["id"] and res["status"] == "shown"
    assert RU in res["text"] and EN in res["text"]
    assert "проверь" in res["text"].lower()
    assert tg.is_translated(conn, EN) == res["id"]


def test_save_rejects_toyhouse_and_empty(conn):
    with pytest.raises(TranslateError, match="4.8"):
        tg.save(conn, RU, EN, "toyhouse", now=NOW)
    with pytest.raises(TranslateError):
        tg.save(conn, RU, "  ", "da", now=NOW)


def test_translation_detected_inside_other_text(conn):
    tg.save(conn, RU, EN, "x", now=NOW)
    template = "Hi! {name} is still here.\n" + EN.splitlines()[1] + "\n{url}"
    assert tg.is_translated(conn, template)
    # регистр, пробелы и пунктуация не прячут перевод
    assert tg.is_translated(conn, EN.upper().replace(",", "").replace("  ", " "))


def test_her_own_texts_not_flagged(conn):
    tg.save(conn, RU, EN, "x", now=NOW)
    assert tg.is_translated(conn, "Hello! Offering my moth {name} for {price}, thanks for looking") is None
    assert tg.is_translated(conn, "SB $15") is None          # короткие совпадения — не повод
    assert tg.is_translated(conn, "") is None


def test_check_toyhouse_blocks_even_approved(conn):
    sid = tg.save(conn, RU, EN, "da", now=NOW)["id"]
    reason = tg.check_toyhouse(conn, EN)
    assert reason and "4.8" in reason and "Toyhouse" in reason
    tg.approve(conn, sid, now=NOW + 60)
    assert tg.check_toyhouse(conn, EN)
    assert tg.check_toyhouse(conn, "My own words for the forum, written by me today") is None


def test_check_for_target_wants_her_approval_for_da_and_x(conn):
    sid = tg.save(conn, RU, EN, "da", now=NOW)["id"]
    note = tg.check_for_target(conn, EN, "deviantart")
    assert note and "не проверила" in note
    assert "4.8" in tg.check_for_target(conn, EN, "toyhouse")
    tg.approve(conn, sid, now=NOW + 60)
    assert tg.check_for_target(conn, EN, "x") is None
    assert tg.check_for_target(conn, "something unrelated entirely here", "deviantart") is None


def test_approve_unknown_and_forget(conn):
    with pytest.raises(TranslateError):
        tg.approve(conn, "nope")
    sid = tg.save(conn, RU, EN, "da", now=NOW)["id"]
    assert tg.forget(conn, sid) is True
    assert tg.is_translated(conn, EN) is None
    assert tg.forget(conn, sid) is False


def test_get_returns_pair(conn):
    sid = tg.save(conn, RU, EN, "x", now=NOW)["id"]
    row = tg.get(conn, sid)
    assert row["source_text"] == RU and row["translated_text"] == EN and row["target"] == "x"
    assert "RU" in tg.render_pair(row) and "EN" in tg.render_pair(row)


# --- разметка и куски (Toyhouse не должен пропускать перевод из-за тегов) ---

EN2 = ("New moth up for auction today, bids open in the comments below.\n"
       "Payment goes through Boosty only, thanks so much for looking at him!")
RU2 = "Новый мотылёк сегодня на аукционе, ставки в комментариях.\nОплата только через Boosty, спасибо!"


def test_nbsp_between_words_does_not_hide_translation(conn):
    tg.save(conn, RU2, EN2, "da", now=NOW)
    assert tg.check_toyhouse(conn, EN2.replace(" ", "&nbsp;"))
    assert tg.check_toyhouse(conn, EN2.replace(" ", " "))
    assert tg.check_toyhouse(conn, EN2.replace("New", "Ne​w").replace("\n", "<br>"))


def test_inline_markup_does_not_hide_translation(conn):
    tg.save(conn, RU2, EN2, "da", now=NOW)
    marked = ("<p>New <b>moth</b> up for <span style=\"color:#ff0000\">auction</span> today, <i>bids open</i> "
              "in the comments below.</p>")
    assert tg.check_toyhouse(conn, marked)
    assert tg.check_toyhouse(conn, "&lt;b&gt;New moth&lt;/b&gt; up for auction today, bids open in the comments below.")


def test_partial_sentence_from_translation_is_caught(conn):
    tg.save(conn, RU2, EN2, "da", now=NOW)
    # первое предложение без последних 4 слов: 4 из 15 кусков перевода, но почти весь текст — из перевода
    assert tg.check_toyhouse(conn, "New moth up for auction today, bids open")
    # 10 слов подряд из перевода внутри длинного своего текста
    long_own = ("Hi everyone, this is my little bug kid and I adore him a lot. "
                "Payment goes through Boosty only, thanks so much for looking. "
                "I drew him over two evenings with my favourite brushes and tea.")
    assert tg.check_toyhouse(conn, long_own)


def test_her_own_text_with_markup_not_flagged(conn):
    tg.save(conn, RU2, EN2, "da", now=NOW)
    own = ("<p><b>Hello!</b> My moth is <span style='color:red'>still</span> available &nbsp; — offers welcome, "
           "I check the comments every evening.</p>")
    assert tg.check_toyhouse(conn, own) is None
    assert tg.is_translated(conn, "bids open in the comments") is None      # 5 слов совпало — мало


def test_plain_strips_markup():
    assert tg.plain("a&nbsp;b <b>c</b><br>d&amp;e​") == "a b c\nd&e"


def test_faithfulness_number_formats_are_the_same_number():
    assert tg.faithfulness("SB 15 000 ₽ и 2 500 ₽", "SB 15,000 ₽ and 2,500 ₽") == []
    assert tg.faithfulness("AB 15.00$", "AB $15") == []
    assert tg.faithfulness("Цена 1.500 ₽", "Price 1,500 ₽") == []
    warns = " ".join(tg.faithfulness("SB 15 000 ₽", "SB 1,500 ₽"))
    assert "15000" in warns and "1500" in warns
