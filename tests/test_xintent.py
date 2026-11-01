"""Ссылки X: окно поста, поиск, длина поста, хэштеги."""

from urllib.parse import parse_qs, urlparse

import pytest

from mcp_devian import xintent

NOW = 1_800_000_000.0


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query, keep_blank_values=True).items()}


def test_intent_encodes_cyrillic_hash_and_ampersand():
    url = xintent.tweet_intent("Мотылёк #1 & друзья ? 50%", "https://www.deviantart.com/m/art/x-1",
                               ["#adoptables", "ocadopt"])
    assert url.startswith("https://x.com/intent/tweet?text=")
    raw = urlparse(url).query
    assert "#" not in raw and "&друзья" not in raw and " " not in raw and "%D0%9C" in raw
    q = query(url)
    assert q["text"] == "Мотылёк #1 & друзья ? 50%"
    assert q["url"] == "https://www.deviantart.com/m/art/x-1"
    assert q["hashtags"] == "adoptables,ocadopt"
    assert "in_reply_to" not in q


def test_intent_reply_takes_id_or_link():
    assert query(xintent.tweet_intent("link ↓", in_reply_to="1850000000000000001"))["in_reply_to"] == "1850000000000000001"
    q = query(xintent.tweet_intent("x", in_reply_to="https://x.com/mothy/status/1850000000000000002?s=20"))
    assert q["in_reply_to"] == "1850000000000000002"
    assert "in_reply_to" not in query(xintent.tweet_intent("x", in_reply_to="not a post"))


def test_search_url_is_live_and_encoded():
    url = xintent.search_url('"lf adopts" dragon -filter:replies')
    assert url.startswith("https://x.com/search?q=") and url.endswith("&f=live")
    assert query(url)["q"] == '"lf adopts" dragon -filter:replies'


def test_lf_query_has_since_and_niche():
    q = xintent.lf_query(["moth", "pastel goth"], since_days=7, now=NOW)
    assert '"lf adopts"' in q and "(moth OR \"pastel goth\")" in q and "since:2027-01-08" in q


@pytest.mark.parametrize("text, n", [
    ("", 0),
    ("hello", 5),
    ("привет", 6),                             # кириллица — 1
    ("龍", 2),                                  # CJK — 2
    ("🐉", 2),                                  # эмодзи — 2
    ("see https://www.deviantart.com/a/art/very-long-title-123456789", 4 + 23),
    ("a http://t.co b", 2 + 23 + 2),          # короткая ссылка всё равно 23
    ("“quotes” — dash", 15),                   # типографика в лёгких диапазонах
])
def test_weighted_len(text, n):
    assert xintent.weighted_len(text) == n


def test_sanitize_hashtags_max_four_and_dedupe():
    got = xintent.sanitize_hashtags(["#Adopt", "adopt", "oc adopt", "#123", "furry-adopt", "a", "b", "c"])
    assert got == ["Adopt", "ocadopt", "furryadopt", "a"]
    assert len(xintent.sanitize_hashtags([f"t{i}" for i in range(10)])) == 4
    assert xintent.sanitize_hashtags("#adoptables, #мотылёк") == ["adoptables", "мотылёк"]


@pytest.mark.parametrize("bare", ["boosty.to/mothy", "mothy.carrd.co", "www.deviantart.com/mothy",
                                  "linktr.ee/mothy"])
def test_bare_links_count_as_23(bare):
    """Регрессия: ссылка без https (boosty.to/имя) считалась по своей длине, а X сокращает её до 23."""
    assert xintent.weighted_len(f"Pay via {bare}") == len("Pay via ") + 23


def test_prices_and_abbreviations_are_not_links():
    assert xintent.weighted_len("SB 12.50, e.g. AB") == len("SB 12.50, e.g. AB")
