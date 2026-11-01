import json

from mcp_devian import taxonomy
from mcp_devian.db import Item, connect, upsert_item


def test_whole_words_and_plurals():
    t = taxonomy.tag_text("Cute pastel dragons adopt — OPEN", ["girl", "kawaii"])
    assert t.species == ["dragon"]
    assert set(t.styles) == {"kawaii", "pastel"}
    assert t.gender == ["female"]


def test_no_false_matches_inside_words():
    t = taxonomy.tag_text("Catch this batch of dogma", ["scatter"])
    assert t.species == [] and t.is_empty()


def test_compound_da_tags():
    t = taxonomy.tag_text("Adopt #12", ["adoptdragon", "mothwings", "halloweenadopt"])
    assert {"dragon", "moth"} <= set(t.species)
    assert t.themes == ["halloween"]


def test_short_words_not_searched_in_compounds():
    # «cat» внутри «catalog», «fox» внутри «firefox» — не виды.
    t = taxonomy.tag_text("Adopt", ["catalog", "firefox"])
    assert t.species == []


def test_russian_and_derived():
    t = taxonomy.tag_text("Кицунэ, чиби, оборотень")
    assert {"kitsune", "fox", "wolf"} <= set(t.species)
    assert t.styles == ["chibi"]
    neko = taxonomy.tag_text("neko girl")
    assert "kemonomimi" in neko.species and "humanoid" in neko.body


def test_tag_item_reads_extra_and_snippet(tmp_path):
    conn = connect(tmp_path / "t.sqlite3")
    upsert_item(conn, Item(id="da:1", source="deviantart", title="OPEN adopt", url="u",
                           snippet="a little moth girl", extra={"tags": ["pixel"]}), now=1.0)
    row = conn.execute("SELECT * FROM items").fetchone()
    t = taxonomy.tag_item(row)
    assert t.species == ["moth"] and t.styles == ["pixel"]
    assert taxonomy.tag_item({"title": "Wolf", "extra": "not json", "snippet": None}).species == ["wolf"]


# --- Точность на живых постах (аудит ноября 2026) ---

def test_does_is_not_deer():
    assert "deer" not in taxonomy.tag_text("Moth adopt", (), "How does it work? Payment first").species


def test_snow_white_is_not_winter():
    assert "winter" not in taxonomy.tag_text("Zombie Snow White adopt").themes
    assert "winter" in taxonomy.tag_text("Snowy fox adopt").themes


def test_reward_lines_do_not_make_chibi_style():
    t = taxonomy.tag_text("Witch femboy adopt", (), "SB: $15 MI: $3 rewards: +$30 — chibi version")
    assert "chibi" not in t.styles
    assert "chibi" in taxonomy.tag_text("Chibi vampire adopt").styles


def test_handles_and_links_are_ignored():
    t = taxonomy.tag_text("Moth adopt", (), "art by @queen_speedy https://example.com/wolf-pack")
    assert "royal" not in t.styles and "wolf" not in t.species


def test_names_are_not_royal_but_fandom_counts():
    t = taxonomy.tag_text("MLP Princess Celestia adopt")
    assert "royal" not in t.styles
    assert t.fandoms == ("mlp",) and "horse" in t.species
    assert taxonomy.tag_text("My Little Pony OC").fandoms == ("mlp",)


def test_new_species_and_plurals():
    assert taxonomy.tag_text("Draconid Adoptable").species == ["dragon"]
    assert "wolf" in taxonomy.tag_text("LF werewolves").species
    for word, canon in [("Zombie girl", "zombie"), ("Lamb girl", "sheep"), ("Rat madam", "rat"),
                        ("Axolotl adopt", "axolotl"), ("Protogen adopt", "protogen"), ("Opossum", "possum")]:
        assert canon in taxonomy.tag_text(word).species, word


def test_canine_is_not_only_dog():
    assert taxonomy.tag_text("LF canines").species == []
    assert {"dog", "wolf", "fox"} <= set(taxonomy.broader_species("LF: canines, felines"))
    assert "cat" in taxonomy.broader_species("LF: canines, felines")


def test_ambiguous_synonyms_removed():
    t = taxonomy.tag_text("Moth adopt", ["cute"], "Fall sale! Team spirit, outer reaches of space")
    assert t.species == ["moth"]
    assert t.styles == [] and t.themes == []


def test_archetypes_and_fandoms():
    t = taxonomy.tag_text("[OPEN] Adoptable Auction - Maid Paladin")
    assert set(t.archetypes) == {"maid", "knight"}
    assert taxonomy.tag_text("Snowy sorceress dragon").archetypes == ("mage",)
    assert taxonomy.tag_text("Pokémon Gardevoir | sold adopt").fandoms == ("pokemon",)
    assert taxonomy.tag_text("Adopt").archetypes == () and taxonomy.Tags().fandoms == ()


def test_species_field_counts_beyond_lead():
    text = "x" * 400 + " Species: Rat Occupation: Witch"
    assert "rat" in taxonomy.tag_text("Adopt", (), text).species
    assert "witch" not in taxonomy.tag_text("Adopt", (), "y" * 400 + " witch hat bonus").species


def test_tag_item_without_snippet_and_api_tags():
    row = {"title": "Adopt #4", "snippet": "a little moth girl",
           "extra": json.dumps({"tags": [], "api_tags": ["dragon", "pastel"]})}
    t = taxonomy.tag_item(row, use_snippet=False)
    assert t.species == ["dragon"] and t.styles == ["pastel"]
    assert "moth" in taxonomy.tag_item(row).species


def test_tag_item_skips_species_for_ych_and_commissions():
    row = {"title": "YCH open", "snippet": "I can draw cats, dogs, dragons", "extra": "{}", "lot_type": "ych"}
    assert taxonomy.tag_item(row).species == []
    row["lot_type"] = "auction"
    assert "cat" in taxonomy.tag_item(row).species


def test_compound_tags_need_vocabulary_around_the_word():
    t = taxonomy.tag_text("Adopt", ["female", "crown", "easterndragon", "divine", "ponytail"])
    assert t.species == ["dragon"] and t.themes == [] and t.gender == ["female"]
    for tag in ("mothersday", "bearded", "harem"):
        assert taxonomy.tag_text("Adopt", [tag]).species == []
    assert set(taxonomy.tag_text("Adopt", ["pastelmothdragon"]).species) == {"moth", "dragon"}
    assert taxonomy.tag_text("Adopt", ["wolfadopt"]).species == ["wolf"]


def test_plural_es_only_after_sibilants():
    assert taxonomy.tag_text("commission rates").species == []
    assert taxonomy.tag_text("growth stages").species == []
    assert set(taxonomy.tag_text("foxes and witches").species) == {"fox", "witch"}


def test_ych_species_from_title_even_without_description():
    row = {"title": "Feral dragon YCH auction", "lot_type": "ych", "extra": "{}", "snippet": "I draw cats, dogs"}
    assert taxonomy.tag_item(row, use_snippet=False).species == ["dragon"]
    assert taxonomy.tag_item(row).species == ["dragon"]  # «рисую кошек, собак» из описания — не вид лота
