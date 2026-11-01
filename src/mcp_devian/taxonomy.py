"""Вид, тип тела, стиль и тема адопта по названию, тегам и описанию.

Нужен аналитике: «драконы дорожают», «кошек переизбыток». Ищем целые слова (и их
формы), а в слитных тегах DeviantArt вроде «adoptdragon», «mothdragon» — известные слова
внутри тега. Ложные совпадения вроде «catch», «dogma», «batch» отсекаются границами слов.
"""

import functools
import json
import re
import unicodedata
from dataclasses import dataclass, field

# канон → синонимы (регистр не важен). Канон сам тоже ищется.
SPECIES: dict[str, tuple[str, ...]] = {
    "dragon": ("dragons", "drake", "draconid", "🐉", "🐲", "wyvern", "wyrm", "дракон", "драконы", "дракончик"),
    "moth": ("moths", "мотылёк", "мотылек", "моль"),
    "butterfly": ("butterflies", "бабочка"),
    "cat": ("cats", "kitty", "kitten", "feline", "кот", "кошка", "котик", "котёнок"),
    # «canine» — это и волк, и лиса, а не только собака: см. HYPERNYMS.
    "dog": ("dogs", "puppy", "pup", "doggo", "собака", "пёс", "щенок"),
    "wolf": ("wolves", "werewolf", "werewolves", "волк", "волчица", "оборотень"),
    "fox": ("foxes", "vixen", "лиса", "лис", "лисичка"),
    "kitsune": ("кицунэ",),
    "rabbit": ("rabbits", "bunny", "bunnies", "hare", "кролик", "заяц", "зайка"),
    "deer": ("stag", "fawn", "reindeer", "олень", "оленёнок"),
    "bird": ("birds", "avian", "owl", "crow", "raven", "птица", "сова", "ворон"),
    "fish": ("shark", "koi", "рыба", "акула"),
    "mermaid": ("merman", "merfolk", "siren", "русалка", "сирена"),
    "demon": ("demons", "devil", "imp", "succubus", "incubus", "демон", "бес"),
    "angel": ("angels", "seraph", "ангел"),
    "elf": ("elves", "elven", "эльф", "эльфийка"),
    "fairy": ("fairies", "faerie", "fae", "pixie", "фея"),
    "vampire": ("vampires", "vamp", "dracula", "вампир"),
    "slime": ("slimes", "слайм"),
    "insect": ("insects", "bug", "bugs", "beetle", "bee", "spider", "насекомое", "жук", "пчела", "паук"),
    "snake": ("snakes", "serpent", "naga", "lamia", "змея", "нага"),
    "horse": ("horses", "pony", "ponies", "equine", "mlp", "my little pony", "🐴", "лошадь", "пони"),
    "unicorn": ("unicorns", "единорог"),
    "griffin": ("gryphon", "griffon", "грифон"),
    "bat": ("bats", "летучая мышь"),
    "bear": ("bears", "медведь"),
    "raccoon": ("raccoons", "racoon", "енот"),
    "kemonomimi": ("neko", "nekomimi", "catgirl", "catboy", "kemono", "inumimi", "kitsunemimi", "кемономими", "неко"),
    "centaur": ("centaurs", "кентавр"),
    "harpy": ("harpies", "гарпия"),
    "robot": ("robots", "android", "cyborg", "mecha", "робот", "андроид"),
    # «spirit» убран: «team spirit», «free spirit» давали призраков.
    "ghost": ("ghosts", "phantom", "призрак"),
    "plant": ("plants", "flower", "dryad", "mushroom", "vine", "растение", "цветок", "гриб"),
    "skeleton": ("skeletons", "skull", "undead", "lich", "скелет", "нежить"),
    "witch": ("witches", "ведьма"),
    "zombie": ("zombies", "зомби"),
    "rat": ("rats", "крыса"),
    "sheep": ("lamb", "lambs", "ram", "овца", "овечка", "ягнёнок"),
    "tiger": ("tigers", "тигр"),
    "lion": ("lions", "lioness", "лев", "львица"),
    "lizard": ("lizards", "gecko", "ящерица"),
    "goat": ("goats", "коза", "козёл"),
    "cow": ("cows", "bull", "корова"),
    "mouse": ("mice", "мышь", "мышка"),
    "axolotl": ("axolotls", "аксолотль"),
    "frog": ("frogs", "toad", "лягушка"),
    "dinosaur": ("dinosaurs", "dino", "dinos", "raptor", "динозавр"),
    "octopus": ("octopi", "octopuses", "осьминог"),
    "jellyfish": ("jelly fish", "медуза"),
    "hyena": ("hyenas", "гиена"),
    "possum": ("possums", "opossum", "opossums", "опоссум"),
    "cryptid": ("cryptids", "криптид"),
    "wendigo": ("wendigos", "вендиго"),
    # Закрытые виды.
    "sergal": ("sergals",),
    "protogen": ("protogens", "протоген"),
    "avali": (),
    "wickerbeast": ("wickerbeasts",),
    "manokit": ("manokits",),
}
BODY: dict[str, tuple[str, ...]] = {
    "feral": ("ferals", "quadruped", "фераль"),
    "anthro": ("anthros", "furry", "furries", "антро", "фурри"),
    "humanoid": ("humanoids", "гуманоид"),
    "human": ("humans", "человек"),
}
STYLES: dict[str, tuple[str, ...]] = {
    "chibi": ("чиби",),
    "pixel": ("pixelart", "pixel art", "пиксель", "пиксельный"),
    "realistic": ("semi-realistic", "semirealistic", "реалистичный"),
    # «cute» убран: им описывают почти любой адопт, это не стиль.
    "kawaii": ("кавай",),
    "gothic": ("goth", "готический", "гот"),
    "pastel": ("pastels", "пастель", "пастельный"),
    "horror": ("creepy", "creepypasta", "gore", "хоррор", "крипи"),
    "fantasy": ("фэнтези",),
    "sci-fi": ("scifi", "alien", "aliens", "outer space", "space themed", "космос", "пришелец"),
    "cyberpunk": ("cyber", "киберпанк"),
    "witchy": ("witchcore", "occult",),
    "cottagecore": ("cottage", "коттеджкор"),
    # king/queen/princess убраны: «Princess Celestia», «Evil Queen» — имена, а не стиль.
    "royal": ("royalty",),
}
THEMES: dict[str, tuple[str, ...]] = {
    "halloween": ("spooky", "spoopy", "pumpkin", "pumpkins", "jack-o-lantern", "trick or treat", "spooktober",
                  "хэллоуин", "хеллоуин", "тыква"),
    "christmas": ("xmas", "winter holiday", "new year", "santa", "новый год", "новогодний", "рождество"),
    "valentine": ("valentines", "valentine's", "день святого валентина"),
    "easter": ("пасха",),
    "summer": ("beach", "лето"),
    "winter": ("snowy", "зима"),
    "spring": ("весна",),
    "autumn": ("fall season", "fall themed", "осень"),
}
# Роль персонажа: горничная, паладин, монахиня…
ARCHETYPES: dict[str, tuple[str, ...]] = {
    "maid": ("maids", "горничная"),
    "knight": ("knights", "paladin", "paladins", "рыцарь", "паладин"),
    "nun": ("nuns", "монахиня"),
    "mage": ("mages", "sorceress", "sorcerer", "wizard", "wizards", "маг", "волшебник", "чародейка"),
    "pirate": ("pirates", "пират"),
    "idol": ("idols", "айдол"),
}
# Фандомы — отдельный рынок со своими покупателями.
FANDOMS: dict[str, tuple[str, ...]] = {
    "mlp": ("my little pony", "mlp fim", "mylittlepony"),
    "pokemon": ("pokémon", "pokemons", "покемон"),
    "sanrio": ("hello kitty", "kuromi", "cinnamoroll", "my melody"),
}
# Широкие слова из LF-тредов: «LF: canines» подходит и волку, и лисе.
HYPERNYMS: dict[str, tuple[str, ...]] = {
    "canine": ("dog", "wolf", "fox", "kitsune"),
    "feline": ("cat", "tiger", "lion"),
    "reptile": ("dragon", "snake", "lizard"),
    "avian": ("bird", "griffin", "harpy"),
    "insect": ("insect", "moth", "butterfly"),
    "bug": ("insect", "moth", "butterfly"),
    "rodent": ("rat", "mouse"),
    "equine": ("horse", "unicorn"),
    "undead": ("zombie", "skeleton", "vampire", "ghost"),
}
_HYPER_RE = {word: re.compile(r"(?<![\wа-яё])" + word + r"(?:e?s)?(?![\wа-яё])", re.I) for word in HYPERNYMS}

GENDER: dict[str, tuple[str, ...]] = {
    "male": ("boy", "man", "guy", "masc", "мальчик", "парень", "мужской"),
    "female": ("girl", "woman", "lady", "fem", "девочка", "девушка", "женский"),
    "nb": ("nonbinary", "non-binary", "androgynous", "андрогин"),
}

# Слова, которые слишком общие или опасны для поиска внутри слитных тегов.
NO_COMPOUND = {"cat", "dog", "bat", "bee", "pup", "fae", "imp", "doe", "fish", "man", "boy", "fall", "cute",
               "space", "goth", "cyber", "king", "fem", "masc", "guy", "bug", "owl", "elf", "stag", "fox", "snow",
               "rat", "ram", "lamb", "cow", "bull", "lion", "goat", "toad", "dino", "maid", "nun", "mage", "idol",
               "mlp", "avali", "mice", "gecko"}


@dataclass
class Tags:
    species: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    gender: list[str] = field(default_factory=list)
    # Новые срезы — кортежи, в all() не входят, чтобы не менять ключи старой аналитики.
    archetypes: tuple[str, ...] = ()
    fandoms: tuple[str, ...] = ()

    def all(self) -> list[str]:
        return [*self.species, *self.body, *self.styles, *self.themes, *self.gender]

    def is_empty(self) -> bool:
        return not self.all()


def _plural(word: str) -> str:
    """Английское множественное: -es только после s/x/ch/sh/z («foxes»), иначе -s.
    Иначе «rates» читалось как rat, а «stages» — как stag (олень)."""
    w = re.escape(word)
    return w + ("(?:es)?" if re.search(r"(?:s|x|ch|sh|z)$", word.lower()) else "s?")


def _compile(vocab: dict[str, tuple[str, ...]]) -> list[tuple[str, re.Pattern, list[str]]]:
    out = []
    for canon, syns in vocab.items():
        words = [canon, *syns]
        # Целое слово (латиница и кириллица), допускаем английское множественное.
        pattern = re.compile(r"(?<![\wа-яё])(?:" + "|".join(_plural(w) for w in sorted(words, key=len, reverse=True))
                             + r")(?![\wа-яё])", re.I)
        compound = [w.lower() for w in words if re.fullmatch(r"[a-z]{4,}", w.lower()) and w.lower() not in NO_COMPOUND]
        out.append((canon, pattern, compound))
    return out


_VOCABS = {name: _compile(v) for name, v in
           (("species", SPECIES), ("body", BODY), ("styles", STYLES), ("themes", THEMES), ("gender", GENDER))}
_EXTRA_VOCABS = {"archetypes": _compile(ARCHETYPES), "fandoms": _compile(FANDOMS)}
# Поля «Species: Rat», «Theme: bat/Dracula» — главное, что автор сказал о персонаже.
FIELD = re.compile(r"\b(?:species|theme|concept)\s*:\s*([^:|•✦\n]{1,60})", re.I)
LEAD_CHARS = 300  # дальше в описании — бонусы («+$30 chibi version»), реклама и правила


# Что может стоять рядом со словом в слитном теге DA: «dragonadopt», «wolfoc», «adoptauction».
TAG_AFFIXES = ("adopt", "adopts", "adoptable", "adoptables", "adoption", "oc", "ocs", "auction", "auctions", "open",
               "ota", "bta", "ych", "sale", "flatsale", "design", "designs", "character", "baby", "mini", "girl",
               "boy", "eastern", "western", "art", "custom", "closed", "sold", "wing", "wings", "horn", "horns", "ears",
               "girls", "boys")


def _tag_parts() -> tuple[str, ...]:
    words = {w.lower() for v in (SPECIES, BODY, STYLES, THEMES, GENDER, ARCHETYPES, FANDOMS)
             for canon, syns in v.items() for w in (canon, *syns) if re.fullmatch(r"[a-z]{3,}", w.lower())}
    return tuple(sorted(words | set(TAG_AFFIXES), key=len, reverse=True))


_TAG_PARTS = _tag_parts()


@functools.lru_cache(maxsize=4096)
def _composed(rest: str) -> bool:
    """«moth», «pasteladopt» — целиком из слов словаря и приставок; «ersday», «tail» — нет."""
    return not rest or any(rest.startswith(w) and _composed(rest[len(w):]) for w in _TAG_PARTS)


def _in_compound(word: str, tag: str) -> bool:
    """Слово в слитном теге — только если всё вокруг него тоже собрано из словаря и приставок:
    «mothdragon», «pastelmothdragon» — да; «mothersday», «crown», «divine», «ponytail», «female» — нет."""
    i = tag.find(word)
    while i >= 0:
        if _composed(tag[i + len(word):]) and _composed(tag[:i]):
            return True
        i = tag.find(word, i + 1)
    return False


def _find(vocab, text: str, tags: list[str]) -> list[str]:
    found = []
    for canon, pattern, compound in vocab:
        if pattern.search(text):
            found.append(canon)
            continue
        # Слитные теги DA: «adoptdragon», «mothdragon», «halloweenadopt».
        if any(_in_compound(word, tag) for tag in tags for word in compound):
            found.append(canon)
    return found


def _clean(s: str) -> str:
    """NFKC, без ссылок и @ников: «@queen_speedy» — это ник, а не стиль royal."""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"https?://\S+|@\S+", " ", s)


def tag_text(title: str, tags=(), text: str = "") -> Tags:
    """Стиль — только из названия и тегов; вид, тема и прочее — ещё из начала описания
    и полей «Species:/Theme:/Concept:». Хвост описания (бонусы за ставки, реклама других
    лотов, списки «рисую кошек, собак, драконов») в аналитику не идёт."""
    clean_tags = [str(t).lower().replace("_", "").replace("-", "") for t in tags or () if t]
    text, title = _clean(text), _clean(title)
    head = " ".join([title, " ".join(str(t) for t in tags or ())]).replace("_", " ")
    lead = " ".join([head, text[:LEAD_CHARS], *FIELD.findall(text)]).replace("_", " ")
    found = {name: _find(vocab, head if name == "styles" else lead, clean_tags) for name, vocab in _VOCABS.items()}
    result = Tags(species=found["species"], body=found["body"], styles=found["styles"], themes=found["themes"],
                  gender=found["gender"])
    result.archetypes = tuple(_find(_EXTRA_VOCABS["archetypes"], lead, clean_tags))
    result.fandoms = tuple(_find(_EXTRA_VOCABS["fandoms"], lead, clean_tags))
    # Кицунэ — тоже лиса, кемономими — гуманоиды: для аналитики полезно оба.
    if "kitsune" in result.species and "fox" not in result.species:
        result.species.append("fox")
    if "kemonomimi" in result.species and "humanoid" not in result.body:
        result.body.append("humanoid")
    if "mlp" in result.fandoms and "horse" not in result.species:
        result.species.append("horse")
    return result


def broader_species(text: str) -> list[str]:
    """Виды, которые подразумевают широкие слова: «canines» → dog, wolf, fox, kitsune."""
    text = _clean(text)
    out: list[str] = []
    for word, pattern in _HYPER_RE.items():
        if pattern.search(text):
            out += [s for s in HYPERNYMS[word] if s not in out]
    return out


NO_SPECIES_TYPES = {"ych", "commission"}


def _field(row, name: str):
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return None


def tag_item(row, use_snippet: bool = True) -> Tags:
    """Теги для строки items (title, extra JSON с tags/api_tags, snippet).

    use_snippet=False — только название и теги: для аналитики, где описание шумит.
    У YCH и заказов вид берём только из названия и тегов: «рисую кошек, собак, драконов» в описании —
    это список умений, не лот.
    """
    try:
        extra = json.loads(_field(row, "extra") or "{}")
    except (json.JSONDecodeError, TypeError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    tags = extra.get("tags")
    if not (isinstance(tags, list) and tags):
        tags = extra.get("api_tags")
    tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
    snippet = (_field(row, "snippet") or "") if use_snippet else ""
    result = tag_text(_field(row, "title") or "", tags, snippet)
    if _field(row, "lot_type") in NO_SPECIES_TYPES and snippet:
        # Описание YCH — список того, что художник рисует; вид — только из названия и тегов.
        result.species = tag_text(_field(row, "title") or "", tags, "").species
    return result
