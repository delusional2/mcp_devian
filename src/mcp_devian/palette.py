"""Палитра лотов: какие цвета у превью и связано ли это со ставками и фавами в её нише.

Цвета считаем по маленькой миниатюре (≤64 px), без прозрачных пикселей, медианным разрезом Pillow —
одно и то же изображение всегда даёт одни и те же цвета. Семейство («синие», «пастельные розовые») —
по фиксированным порогам ниже, без машинного обучения.

Миниатюры качаем бережно: только уже известные ссылки на маленькие превью (items.extra["thumb"]),
не больше per_hour за любые 60 минут (не больше 60) и не больше STEP_MAX за проход, с паузой DELAY_S между
файлами, без редиректов. 429 или 403 на двух разных лотах подряд — пауза на час без повторов; одиночный
403 (протухшая подписанная ссылка) — лот откладываем на сутки. Бюджет запросов к API DeviantArt
не тратится: это файлы с CDN. Отчёт — только наблюдение: «вывода нет», пока лотов мало.
"""

from __future__ import annotations

import colorsys
import io
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from . import evidence, forecast

DAY = 86400
HOUR = 3600
SIDE = 64                 # до скольких пикселей уменьшаем картинку перед подсчётом
ALPHA_MIN = 32            # пиксели прозрачнее этого не считаем (фон PNG)
FETCH_TIMEOUT_S = 8
MAX_BYTES = 2_000_000     # миниатюра больше 2 МБ — не миниатюра, не качаем
PAUSE_S = HOUR            # пауза после 429 или 403 подряд на разных лотах
HARD_PER_HOUR = 60        # потолок скачиваний за любые 60 минут, что бы ни стояло в настройках
STEP_MAX = 20             # не больше стольких файлов за один проход фоновой проверки
DELAY_S = 1.0             # пауза между файлами внутри прохода
FORBIDDEN_IN_ROW = 2      # столько 403 подряд на разных лотах — это уже блок, а не одна битая ссылка
MAX_SIDE_PX = 4096        # «миниатюра» больше по стороне — не миниатюра, не раскрываем
MAX_LOCAL_PIXELS = 100_000_000  # её файл больше (≈10000×10000) — просим уменьшенную копию
RETRY_TEMP_S = DAY        # временную ошибку (таймаут, 5xx) пробуем снова через сутки
THUMB_HOSTS = ("wixmp.com", "deviantart.net", "deviantart.com")
MAX_LOCAL_BYTES = 60_000_000

# --- Пороги семейств (HLS из colorsys: h — градусы 0–360, l и s — 0..1; chroma = (max−min)/255) ---
ACHROMATIC_CHROMA = 0.10   # chroma ниже — цвета нет: светлый / тёмный / серый
LIGHT_L = 0.80             # ахроматический и светлее — «светлые»
DARK_L = 0.20              # ахроматический и темнее — «тёмные»
VERY_DARK_L = 0.12         # даже цветной, но темнее — «тёмные»
VERY_LIGHT_L = 0.95        # даже цветной, но светлее — «светлые»
BROWN_HUE = (10.0, 50.0)   # красно-оранжевые оттенки…
BROWN_L = 0.42             # …темнее этого — «коричневые»
PINK_FROM_RED_L = 0.70     # красный оттенок светлее этого — «розовые»
HUES = (("red", 0, 15), ("orange", 15, 40), ("yellow", 40, 70), ("green", 70, 160), ("teal", 160, 195),
        ("blue", 195, 250), ("purple", 250, 290), ("pink", 290, 345), ("red", 345, 361))
CHROMATIC_MIN_SHARE = 0.25  # если цветных пикселей хотя бы столько — семейство по цвету, а не по фону
PASTEL = (0.75, 0.25)      # пастель: средняя l ≥ 0.75 и s ≥ 0.25 (светлый, но цветной)
NEON = (0.90, 0.40, 0.65)  # неон: s ≥ 0.90 и l в 0.40–0.65
MUTED_S = 0.35             # приглушённый: s < 0.35 (при l 0.20–0.75)

BASES = ("red", "orange", "yellow", "green", "teal", "blue", "purple", "pink", "brown", "dark", "light", "grey")
ACHROMATIC = ("dark", "light", "grey")
MODIFIERS = ("pastel", "neon", "muted")
LABEL = {"red": "красные", "orange": "оранжевые", "yellow": "жёлтые", "green": "зелёные", "teal": "бирюзовые",
         "blue": "синие", "purple": "фиолетовые", "pink": "розовые", "brown": "коричневые", "dark": "тёмные",
         "light": "светлые", "grey": "серые", "pastel": "пастельные", "neon": "неоновые", "muted": "приглушённые",
         "plain": "без пастели/неона/приглушённости"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS palette_cache (
    item_id TEXT PRIMARY KEY,      -- da:<id>
    colors TEXT,                   -- JSON [[[r,g,b], доля], …]
    family TEXT,                   -- «pastel pink», «blue», …
    src_url TEXT,
    ts REAL NOT NULL,
    error TEXT                     -- temp:… — попробуем снова через сутки; иначе не трогаем
);
CREATE TABLE IF NOT EXISTS palette_downloads (
    ts REAL NOT NULL               -- момент скачивания: лимит считаем за скользящие 60 минут
);
CREATE TABLE IF NOT EXISTS palette_pause (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    until_ts REAL NOT NULL,
    status INTEGER
);
"""


class FetchError(Exception):
    """Миниатюра не скачалась. status — HTTP-код (403/429 включают паузу), None — сеть/таймаут/размер."""

    def __init__(self, status: int | None, msg: str = ""):
        super().__init__(msg or f"HTTP {status}")
        self.status = status


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


# --- Цвета ---

def _small_rgba(image: Image.Image) -> Image.Image:
    """Уменьшить до ≤SIDE и только потом перевести в RGBA: без полноразмерных копий исходника."""
    img = image
    w, h = img.size
    scale = max(w, h) / SIDE
    if scale > 1:
        size = (max(1, round(w / scale)), max(1, round(h / scale)))
        if img.mode in ("RGB", "L"):
            img = img.resize(size, Image.Resampling.BOX, reducing_gap=2.0)
        else:
            # RGBA/LA Pillow сглаживает через полноразмерную копию, P/CMYK/16 бит — тоже копия при convert:
            # сначала выборка ближайших пикселей до 4×SIDE (без копии), потом как обычно.
            if scale > 4:
                img = img.resize((max(1, round(w / scale * 4)), max(1, round(h / scale * 4))),
                                 Image.Resampling.NEAREST)
            img = img.convert("RGBA").resize(size, Image.Resampling.BOX)
    return img if img.mode == "RGBA" else img.convert("RGBA")


def _rgba_pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    raw = _small_rgba(image).tobytes()
    return [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 4) if raw[i + 3] >= ALPHA_MIN]


def dominant_colors(image: Image.Image, k: int = 5) -> list[tuple[tuple[int, int, int], float]]:
    """До k главных цветов с долями (сумма 1), по убыванию доли. Прозрачное не считаем. Детерминированно."""
    pixels = _rgba_pixels(image)
    if not pixels:
        return []
    strip = Image.new("RGB", (len(pixels), 1))
    strip.frombytes(bytes(c for p in pixels for c in p))
    q = strip.quantize(colors=max(1, min(k, 16)), method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = q.getpalette() or []
    counts = q.getcolors() or []
    total = len(pixels)
    merged: Counter = Counter()
    for count, idx in counts:
        i = int(idx)  # type: ignore[arg-type]  # у P-картинки getcolors() отдаёт номера палитры
        merged[(pal[3 * i], pal[3 * i + 1], pal[3 * i + 2])] += count
    out = [(rgb, round(c / total, 4)) for rgb, c in merged.items()]
    return sorted(out, key=lambda x: (-x[1], x[0]))


def _hls(rgb) -> tuple[float, float, float, float]:
    r, g, b = (v / 255 for v in rgb)
    h, lum, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, lum, s, max(r, g, b) - min(r, g, b)


def base_of(rgb) -> str:
    """Один цвет → базовое семейство (пороги — константы модуля)."""
    h, lum, _s, chroma = _hls(rgb)
    if chroma < ACHROMATIC_CHROMA:
        return "light" if lum >= LIGHT_L else "dark" if lum <= DARK_L else "grey"
    if lum <= VERY_DARK_L:
        return "dark"
    if lum >= VERY_LIGHT_L:
        return "light"
    if BROWN_HUE[0] <= h < BROWN_HUE[1] and lum < BROWN_L:
        return "brown"
    name = next(n for n, lo, hi in HUES if lo <= h < hi)
    if name == "red" and lum >= PINK_FROM_RED_L:
        return "pink"
    return name


def family(colors) -> str:
    """Семейство палитры: «blue», «pastel pink», «neon green», «dark»…

    Если цветные пиксели занимают ≥ CHROMATIC_MIN_SHARE, семейство — самый весомый цвет (белый фон адопта
    не перебивает персонажа); иначе — самое весомое из светлых/тёмных/серых. Модификатор — по средним
    l и s цветов победившего семейства: пастель, неон или приглушённый."""
    by: dict[str, float] = defaultdict(float)
    members: dict[str, list] = defaultdict(list)
    for rgb, share in colors or ():
        b = base_of(rgb)
        by[b] += share
        members[b].append((rgb, share))
    if not by:
        return "unknown"
    chroma_total = sum(v for k, v in by.items() if k not in ACHROMATIC)
    pool = [k for k in by if k not in ACHROMATIC] if chroma_total >= CHROMATIC_MIN_SHARE else list(by)
    base = max(pool, key=lambda k: (by[k], -BASES.index(k)))
    if base in ACHROMATIC:
        return base
    w = sum(s for _, s in members[base]) or 1.0
    lum = sum(_hls(rgb)[1] * s for rgb, s in members[base]) / w
    sat = sum(_hls(rgb)[2] * s for rgb, s in members[base]) / w
    if lum >= PASTEL[0] and sat >= PASTEL[1]:
        return f"pastel {base}"
    if sat >= NEON[0] and NEON[1] <= lum <= NEON[2]:
        return f"neon {base}"
    if sat < MUTED_S and DARK_L < lum < 0.75:
        return f"muted {base}"
    return base


def split(fam: str) -> tuple[str, str]:
    """«pastel pink» → ("pink", "pastel"); «blue» → ("blue", "plain")."""
    parts = (fam or "unknown").split()
    return (parts[-1], parts[0]) if len(parts) == 2 else (parts[0], "plain")


def label(fam: str) -> str:
    base, mod = split(fam)
    word = LABEL.get(base, base)
    return f"{LABEL[mod]} {word}" if mod != "plain" else word


# --- Скачивание миниатюр ---

def thumb_url(extra: dict) -> str | None:
    """Маленькое превью лота: из extra["thumbs"] — самое крупное до 300 px (иначе самое мелкое), иначе extra["thumb"].
    Только https и только CDN DeviantArt."""
    cands = []
    for t in extra.get("thumbs") or []:
        if isinstance(t, dict) and isinstance(t.get("src"), str):
            try:
                width = int(t.get("width") or 0)
            except (TypeError, ValueError):   # «150px» и прочее — считаем размер неизвестным
                width = 0
            cands.append((width, t["src"]))
    url = None
    if cands:
        small = [c for c in cands if 0 < c[0] <= 300]
        url = max(small)[1] if small else min(cands)[1]
    elif isinstance(extra.get("thumb"), str):
        url = extra["thumb"]
    if not url:
        return None
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not any(host == h or host.endswith("." + h) for h in THUMB_HOSTS):
        return None
    return url


def client():
    """httpx-клиент для миниатюр: вежливый UA, таймаут 8 с, без редиректов (хост проверен заранее)."""
    import httpx

    from .deviantart import USER_AGENT

    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT_S, follow_redirects=False)


def make_fetch(http):
    """fetch(url) -> bytes поверх httpx-клиента: таймаут 8 с, не больше MAX_BYTES, ошибки — FetchError."""
    import httpx

    def fetch(url: str) -> bytes:
        try:
            with http.stream("GET", url, timeout=FETCH_TIMEOUT_S) as r:
                if r.status_code != 200:
                    raise FetchError(r.status_code)
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise FetchError(None, "слишком большой файл")
                return bytes(buf)
        except httpx.HTTPError as e:
            raise FetchError(None, type(e).__name__) from e

    return fetch


def _status(e: Exception) -> int | None:
    st = getattr(e, "status", None)
    if st is None:
        resp = getattr(e, "response", None)
        st = getattr(resp, "status_code", None)
    return st if isinstance(st, int) else None


def paused_until(conn, now: float) -> float | None:
    ensure(conn)
    row = conn.execute("SELECT until_ts FROM palette_pause WHERE id = 1").fetchone()
    return row["until_ts"] if row and row["until_ts"] > now else None


def used_this_hour(conn, now: float) -> int:
    """Скачиваний за последние 60 минут (скользящее окно, не по часам на циферблате)."""
    ensure(conn)
    row = conn.execute("SELECT COUNT(*) AS n FROM palette_downloads WHERE ts > ?", (now - HOUR,)).fetchone()
    return int(row["n"]) if row else 0


def _count(conn, now: float) -> None:
    conn.execute("INSERT INTO palette_downloads (ts) VALUES (?)", (now,))
    conn.execute("DELETE FROM palette_downloads WHERE ts < ?", (now - 2 * DAY,))


def _save(conn, item_id: str, url: str | None, now: float, colors=None, fam=None, error=None) -> None:
    conn.execute("INSERT OR REPLACE INTO palette_cache (item_id, colors, family, src_url, ts, error) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (item_id, json.dumps([[list(c), s] for c, s in colors]) if colors else None, fam, url, now, error))


def save(conn, item_id: str, colors, url: str | None = None, now: float | None = None) -> str:
    """Записать готовую палитру лота (например, посчитанную из её файла). Возвращает семейство."""
    ensure(conn)
    fam = family(colors) if colors else "unknown"
    _save(conn, item_id, url, now or time.time(), colors=colors, fam=fam)
    conn.commit()
    return fam


def _json(v) -> dict:
    try:
        d = json.loads(v or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def her_niche(conn) -> set[str]:
    """Виды (до 3) и стили (до 2) её лотов DA — по названиям и тегам. Пусто — ниша неизвестна."""
    from .taxonomy import tag_text

    species: Counter = Counter()
    styles: Counter = Counter()
    for row in conn.execute("SELECT title, extra FROM items WHERE own = 1 AND source = 'deviantart'"):
        extra = _json(row["extra"])
        tags = [str(t) for t in [*(extra.get("tags") or []), *(extra.get("api_tags") or [])] if isinstance(t, str)]
        t = tag_text(row["title"] or "", tags, "")
        species.update(t.species)
        styles.update(t.styles)
    return {s for s, _ in species.most_common(3)} | {s for s, _ in styles.most_common(2)}


def _in_niche(row, niche: set[str]) -> bool:
    if not niche:
        return True
    t = forecast.analytics_tags(row)
    return bool(niche & (set(t.species) | set(t.styles)))


def candidates(conn, now: float, niche: set[str] | None = None, limit: int = 2000) -> list[tuple[str, str]]:
    """(item_id, url) без палитры: сначала её лоты, потом аукционы ниши; свежие первыми, за 90 дн."""
    ensure(conn)
    niche = her_niche(conn) if niche is None else niche
    rows = conn.execute(
        "SELECT i.* FROM items i LEFT JOIN palette_cache p ON p.item_id = i.id "
        "WHERE i.source = 'deviantart' "
        "AND ((i.own = 1 AND i.lot_type NOT IN ('other','ych','commission')) OR i.lot_type IN ('auction','bta')) "
        "AND COALESCE(i.published_ts, i.first_seen_ts) >= ? "
        "AND (p.item_id IS NULL OR (p.error LIKE 'temp:%' AND p.ts < ?)) "
        "ORDER BY i.own DESC, COALESCE(i.published_ts, i.first_seen_ts) DESC, i.id LIMIT ?",
        (now - 90 * DAY, now - RETRY_TEMP_S, limit)).fetchall()
    out = []
    for r in rows:
        if not r["own"] and (forecast.is_search(r["place"]) or not _in_niche(r, niche)):
            continue
        url = thumb_url(_json(r["extra"]))
        if url:
            out.append((r["id"], url))
    return out


def colors_from_bytes(data: bytes, max_side: int = MAX_SIDE_PX) -> list:
    """Цвета миниатюры. Картинку больше max_side по стороне не раскрываем (сжатый PNG бывает огромным)."""
    with Image.open(io.BytesIO(data)) as img:
        if max(img.size) > max_side:
            raise ValueError(f"слишком большая картинка {img.size[0]}×{img.size[1]}")
        img.draft("RGB", (SIDE * 2, SIDE * 2))
        return dominant_colors(img)


def step(conn, fetch, now: float | None = None, per_hour: int = 30, niche: set[str] | None = None,
         sleep=None) -> dict:
    """Досчитать палитры нескольких лотов. fetch(url) -> bytes (таймаут 8 с — на его стороне).
    Не больше per_hour (≤60) за скользящий час и STEP_MAX за проход, с паузой DELAY_S между файлами.
    429 или 403 на FORBIDDEN_IN_ROW разных лотах подряд — пауза на час и выход; одиночный 403 — лот на сутки."""
    now = now or time.time()
    pause = sleep or time.sleep
    ensure(conn)
    res: dict = {"fetched": 0, "cached": 0, "errors": 0, "paused_until": None, "skipped": None}
    until = paused_until(conn, now)
    if until:
        res.update(paused_until=until, skipped="пауза после 403/429")
        return res
    left = max(0, min(int(per_hour), HARD_PER_HOUR) - used_this_hour(conn, now))
    if not left:
        res["skipped"] = "лимит в час исчерпан"
        return res
    forbidden = 0
    for i, (item_id, url) in enumerate(candidates(conn, now, niche)[:min(left, STEP_MAX)]):
        # Запись не держим открытой во время паузы и скачивания: чат ждал бы блокировку базы.
        conn.commit()
        if i and DELAY_S:
            pause(DELAY_S)
        _count(conn, now)
        conn.commit()
        res["fetched"] += 1
        try:
            data = fetch(url)
        except Exception as e:  # noqa: BLE001 — сеть, HTTP, наш FetchError
            st = _status(e)
            forbidden = forbidden + 1 if st == 403 else 0
            if st == 429 or forbidden >= FORBIDDEN_IN_ROW:
                if st == 403:
                    _save(conn, item_id, url, now, error="temp:" + str(e)[:120])
                conn.execute("INSERT OR REPLACE INTO palette_pause (id, until_ts, status) VALUES (1, ?, ?)",
                             (now + PAUSE_S, st))
                conn.commit()
                res.update(paused_until=now + PAUSE_S, skipped=f"HTTP {st}: пауза на час")
                return res
            temp = st is None or st == 403 or st >= 500
            _save(conn, item_id, url, now, error=("temp:" if temp else "") + str(e)[:120])
            res["errors"] += 1
            continue
        forbidden = 0
        try:
            colors = colors_from_bytes(data)
        except Exception as e:  # noqa: BLE001 — битая картинка, не картинка, бомба
            _save(conn, item_id, url, now, error=f"image: {type(e).__name__}")
            res["errors"] += 1
            continue
        _save(conn, item_id, url, now, colors=colors, fam=family(colors) if colors else "unknown")
        res["cached"] += 1
    conn.commit()
    return res


def cached_family(conn, item_id: str) -> str | None:
    ensure(conn)
    row = conn.execute("SELECT family FROM palette_cache WHERE item_id = ? AND error IS NULL", (item_id,)).fetchone()
    return row["family"] if row and row["family"] and row["family"] != "unknown" else None


# --- Отчёт ---

def _families(conn, ids: list[str]) -> dict[str, str]:
    ensure(conn)
    out: dict[str, str] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(f"SELECT item_id, family FROM palette_cache WHERE error IS NULL AND family IS NOT NULL "
                              f"AND family != 'unknown' AND item_id IN ({','.join('?' * len(chunk))})", chunk):
            out[r["item_id"]] = r["family"]
    return out


def _pool(conn, now: float, days: int, niche: set[str] | None):
    outcomes = forecast.load_outcomes(conn, now, days)
    # Только рынок: её лоты в «аукционах её ниши» смешали бы её результаты с рыночными (и без ограничения
    # на продавца перевесили бы).
    pool = forecast.market_pool(outcomes)
    niche = her_niche(conn) if niche is None else niche
    if niche:
        pool = [o for o in pool if niche & (set(o.tags.species) | set(o.tags.styles))]
    return pool, niche, forecast._cutoff(outcomes)


def _groups(conn, now: float, days: int, niche: set[str] | None) -> dict:
    pool, niche, cutoff = _pool(conn, now, days, niche)
    fams = _families(conn, [o.id for o in pool])
    with_pal = [o for o in pool if o.id in fams]
    favs = forecast.fav_snapshots(conn, [{"id": o.id, "published_ts": o.ts, "first_seen_ts": o.ts} for o in with_pal])
    by_base: dict[str, list] = defaultdict(list)
    by_mod: dict[str, list] = defaultdict(list)
    for o in with_pal:
        base, mod = split(fams[o.id])
        by_base[base].append(o)
        by_mod[mod].append(o)
    return {"pool": pool, "with": with_pal, "niche": niche, "cutoff": cutoff, "favs": favs,
            "fams": fams, "base": by_base, "mod": by_mod}


def _ev(obs, cutoff, population: str) -> evidence.Evidence:
    k = sum(1 for o in obs if o.sold)
    n = len(obs)
    return evidence.span([o.ts for o in obs], "palette", population, None, "observed", n=n,
                         interval=evidence.wilson(k, n), measure="rate", value=k / n if n else None,
                         data_as_of=cutoff)


def _group_line(key: str, obs: list, g: dict) -> str:
    n = len(obs)
    name = LABEL.get(key, key)
    if n < evidence.MIN_SIDE:
        return f"• {name}: {n} лот(ов) — вывода нет (нужно ≥{evidence.MIN_SIDE})."
    k = sum(1 for o in obs if o.sold)
    lo, hi = evidence.wilson(k, n)
    ev = _ev(obs, g["cutoff"], f"аукционы её ниши с палитрой «{name}»")
    favs = [g["favs"][o.id]["favourites"] for o in obs
            if o.id in g["favs"] and g["favs"][o.id].get("favourites") is not None]
    line = (f"• {name}: ставку получили {k} из {n} ({evidence.pct(k / n)}, 80%: {evidence.rate_interval(lo, hi)})")
    if len(favs) >= evidence.MIN_SIDE:
        flo, fhi = evidence.median_ci(favs)
        line += f"; медиана фавов на 3–10 день {statistics.median(favs):g} (80%: {flo:g}–{fhi:g}, по {len(favs)})"
    else:
        line += f"; по фавам вывода нет (снимков на 3–10 день {len(favs)} из нужных {evidence.MIN_SIDE})"
    return line + " " + evidence.tag(ev)


def _comparisons(g: dict) -> list[str]:
    with_pal = g["with"]
    total = len(with_pal)
    k_all = sum(1 for o in with_pal if o.sold)
    tests = []
    for kind in ("base", "mod"):
        for key, obs in g[kind].items():
            n = len(obs)
            if n >= evidence.MIN_SIDE and total - n >= evidence.MIN_SIDE:
                tests.append((key, obs))
    if not tests:
        return []
    thr = evidence.family_threshold(len(tests))
    out = []
    for key, obs in tests:
        n = len(obs)
        k = sum(1 for o in obs if o.sold)
        verdict = evidence.compare(k, n, k_all - k, total - n, thr)
        if not verdict:
            continue
        p = evidence.p_greater(k, n, k_all - k, total - n)
        word = "чаще" if verdict == "better" else "реже"
        out.append(f"«{LABEL.get(key, key)}» {word} получают ставку, чем остальные лоты ниши: "
                   f"P≈{min(0.99, p if verdict == 'better' else 1 - p):.2f} при пороге {thr:.2f} "
                   f"({len(tests)} сравнений) " + evidence.tag(_ev(obs, g["cutoff"], "аукционы её ниши с палитрой")))
    return out


def _coverage_refusal(conn, now: float) -> str | None:
    """Как в forecast.niches: итог известен у слишком малой доли аукционов — доля «со ставкой» завышена."""
    return forecast.coverage_refusal(*forecast.outcome_coverage(conn, now))


def report(conn, now: float | None = None, days: int = 90, niche: set[str] | None = None) -> str:
    """Палитры её ниши против ставок и фавов: по семействам, сравнения — только выше порога."""
    now = now or time.time()
    refusal = _coverage_refusal(conn, now)
    if refusal:
        return f"Палитра и ставки: {refusal}"
    g = _groups(conn, now, days, niche)
    pool, with_pal = g["pool"], g["with"]
    where = f"её ниши ({', '.join(sorted(g['niche']))})" if g["niche"] else "рынка (ниша неизвестна)"
    head = (f"Палитра и ставки — аукционы {where} за {days} дн, с известным итогом. "
            f"Палитра посчитана у {len(with_pal)} из {len(pool)}.")
    lines = [head]
    if len(with_pal) < evidence.MIN_SIDE:
        lines.append(evidence.refuse("лотов ниши с посчитанной палитрой", len(with_pal), evidence.MIN_SIDE)
                     + " Миниатюры докачиваются в фоне понемногу.")
        return "\n".join(lines)
    lines.append("По цвету:")
    lines += [_group_line(k, obs, g) for k, obs in sorted(g["base"].items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    mods = {k: v for k, v in g["mod"].items() if k != "plain" or len(g["mod"]) > 1}
    if mods:
        lines.append("По характеру палитры:")
        lines += [_group_line(k, obs, g) for k, obs in sorted(mods.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    comps = _comparisons(g)
    lines += comps or ["Вывода нет: ни одна палитра не отличается от остальных надёжно — по цвету решать рано."]
    lines.append("Это связь, не причина: у разных палитр могут быть разные продавцы, виды и цены.")
    return "\n".join(lines)


def family_line(conn, fam: str, now: float | None = None, days: int = 90, niche: set[str] | None = None) -> str:
    """Строка отчёта для семейства (по базовому цвету) — для разбора её лота."""
    now = now or time.time()
    refusal = _coverage_refusal(conn, now)
    if refusal:
        return refusal
    g = _groups(conn, now, days, niche)
    base, _mod = split(fam)
    line = _group_line(base, g["base"].get(base, []), g).removeprefix("• ")
    comp = next((c for c in _comparisons(g) if c.startswith(f"«{LABEL.get(base, base)}»")), None)
    return line + (f" {comp}" if comp else "")


def of_file(conn, path: str, now: float | None = None, days: int = 90, niche: set[str] | None = None) -> str:
    """Её картинка (локальный файл) → семейство палитры и строка отчёта по нему. Ничего не отправляется."""
    p = Path(path).expanduser()
    if not p.is_file():
        return f"Файл не нашла: {p}"
    if p.stat().st_size > MAX_LOCAL_BYTES:
        return "Файл слишком большой для разбора палитры (больше 60 МБ)."
    try:
        with Image.open(p) as img:
            if img.size[0] * img.size[1] > MAX_LOCAL_PIXELS:
                return (f"Картинка слишком большая для разбора ({img.size[0]}×{img.size[1]}). "
                        "Сохрани уменьшенную копию (например, 2000 px по длинной стороне) и дай путь к ней.")
            img.draft("RGB", (SIDE * 4, SIDE * 4))
            colors = dominant_colors(img)
    except Exception as e:  # noqa: BLE001
        return f"Не смогла открыть как картинку: {type(e).__name__}"
    if not colors:
        return "В картинке нет непрозрачных пикселей — палитру не посчитать."
    fam = family(colors)
    hexes = ", ".join(f"#{r:02x}{g:02x}{b:02x} {round(s * 100)}%" for (r, g, b), s in colors)
    return (f"Палитра «{p.name}»: {label(fam)} ({hexes}).\n"
            f"Как такие лоты идут в нише: {family_line(conn, fam, now, days, niche)}")
