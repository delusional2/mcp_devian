"""Готовая картинка для поста адопта: её рисунок, цены (SB/MI/AB или одна цена), её ник, плашка OPEN/SOLD
и водяной знак — одним файлом, вместо ручной сборки в редакторе.

Берём только картинки из Загрузок, Рабочего стола и Изображений (как posting.check_image). Оригинал не
трогаем и ничего не перезаписываем: пишем рядом «<имя>-sheet.png», а если такое имя занято — «-sheet-2»
и дальше. Метаданных оригинала (EXIF, GPS) в результате нет. Подписи — на английском (пост для DA и X),
шрифт — системный с кириллицей, если найдётся (название перса может быть по-русски).
"""

from __future__ import annotations

import functools
import io
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

from . import media
from .sales import money

MARKER = "mcp_devian-sheet"
LAYOUTS = {"bottom": "полоса снизу", "side": "панель сбоку"}
BADGES = {"open": "OPEN", "sold": "SOLD", "hold": "ON HOLD", "pending": "PENDING"}
_BADGE_ALIASES = {"on hold": "hold", "on_hold": "hold", "closed": "sold", "открыт": "open", "продан": "sold"}
CURRENCIES = ("usd", "eur", "gbp", "rub", "points")
PRICE_KEYS = ("sb", "mi", "ab")
THEMES = {
    "dark": {"bg": (26, 26, 25), "text": (240, 239, 236), "muted": (195, 194, 183)},
    "light": {"bg": (252, 252, 251), "text": (11, 11, 11), "muted": (82, 81, 78)},
}
# Плашка: цвет + слово, цвет один смысла не несёт.
BADGE_COLORS = {"open": ((12, 163, 12), (255, 255, 255)), "sold": ((208, 59, 59), (255, 255, 255)),
                "hold": ((250, 178, 25), (26, 26, 25)), "pending": ((250, 178, 25), (26, 26, 25))}
# Системные шрифты macOS с кириллицей; первый, который реально её знает, и берём.
FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/HelveticaNeue.ttc",          # знает и «₽»
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
DEFAULT_MAX_SIDE = 1600
MIN_SIDE, MAX_SIDE = 300, 3000
MIN_ART = 200                           # короче — подписи и плашка не читаются
X_IMAGE_LIMIT = 5 * 1024 * 1024        # X не берёт картинки больше 5 МБ — тогда сохраняем JPEG
_NAME_TRIES = 50
_LABEL_MAX = 24                         # подпись цены своими словами («offer», «$15+»)


class SheetError(ValueError):
    """Понятная ей причина, почему картинку не собрать."""


# --- Цены ---

def _price_text(value, currency: str) -> str:
    if isinstance(value, bool):  # True — это не цена (иначе вышло бы «$1»)
        raise SheetError("Цена — число (или короткий текст вроде «offer»), не да/нет.")
    if isinstance(value, str):
        text = " ".join(value.split())
        if not text or len(text) > _LABEL_MAX:
            raise SheetError(f"Цена текстом — коротко, до {_LABEL_MAX} знаков (например «offer»).")
        return text
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise SheetError("Цена — число (или короткий текст вроде «offer»).") from e
    if not math.isfinite(v) or v < 0:
        raise SheetError("Цена — обычное число не меньше нуля.")
    return money(v, currency)


def price_lines(prices: dict | None, price, currency: str = "usd") -> list[str]:
    """["SB $15", "MI $5", "AB $80"] или ["Price $30"]. Порядок всегда SB, MI, AB."""
    currency = (currency or "usd").strip().lower()
    if currency not in CURRENCIES:
        raise SheetError(f"Валюта: {', '.join(CURRENCIES)}.")
    prices = {str(k).strip().lower(): v for k, v in (prices or {}).items() if v not in (None, "")}
    if prices and price not in (None, ""):
        raise SheetError("Либо SB/MI/AB (аукцион), либо одна цена — не всё сразу.")
    unknown = sorted(set(prices) - set(PRICE_KEYS))
    if unknown:
        raise SheetError(f"Не знаю подпись {', '.join(unknown)}: бывают SB, MI, AB.")
    if prices:
        return [f"{k.upper()} {_price_text(prices[k], currency)}" for k in PRICE_KEYS if k in prices]
    if price not in (None, ""):
        return [f"Price {_price_text(price, currency)}"]
    return []


def price_warnings(prices: dict | None) -> list[str]:
    """Подсказки, не запреты: AB ниже SB почти всегда опечатка."""
    p = {str(k).lower(): v for k, v in (prices or {}).items()}
    sb, ab = p.get("sb"), p.get("ab")
    if isinstance(sb, (int, float)) and isinstance(ab, (int, float)) and ab < sb:
        return ["AB меньше SB — проверь, не перепутаны ли"]
    return []


# --- Шрифты ---

def _missing_mask(font) -> bytes:
    return bytes(font.getmask("\U000f0000"))  # символ из частной зоны: такого нет почти ни в одном шрифте


def _covers(font, text: str) -> bool:
    """Знает ли шрифт все не-ASCII буквы текста (иначе вместо них будут квадратики)."""
    chars = {c for c in text if ord(c) > 0x7F and not c.isspace()}
    if not chars:
        return True
    try:
        missing = _missing_mask(font)
        return all(bytes(font.getmask(c)) != missing for c in chars)
    except (OSError, ValueError, TypeError):
        return False


@functools.lru_cache(maxsize=1)
def find_font() -> str | None:
    """Системный шрифт с кириллицей (лучше — ещё и со знаком ₽) или None."""
    fallback = None
    for path in FONT_CANDIDATES:
        if not Path(path).is_file():
            continue
        try:
            font = ImageFont.truetype(path, 40)
        except OSError:
            continue
        if _covers(font, "ЖжЯяЁё"):
            if _covers(font, "₽€£"):
                return path
            fallback = fallback or path
    return fallback


# Шрифт без знака валюты: пишем код, а не квадратик.
_CODES = (("₽", "RUB", False), ("€", "EUR", True), ("£", "GBP", True))


def _plain_currency(label: str, font) -> str:
    for sym, code, prefix in _CODES:
        if sym in label and not _covers(font, sym):
            label = label.replace(sym, f"{code} " if prefix else code)
    return label


def _font(size: int):
    path = find_font()
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _check_glyphs(texts: list[str]) -> None:
    probe = _font(32)
    bad = [t for t in texts if t and not _covers(probe, t)]
    if bad:
        raise SheetError("На этом маке не нашла шрифт, который знает эти буквы, — напиши подписи простой "
                         f"латиницей, A–Z (не получится: «{bad[0][:30]}»).")


def _width(font, text: str) -> float:
    left, _, right, _ = font.getbbox(text)
    return right - left


def _fit(text: str, max_w: int, size: int, min_size: int):
    """(текст, шрифт): уменьшаем шрифт до min_size, дальше обрезаем с «…»."""
    while size > min_size and _width(_font(size), text) > max_w:
        size = max(min_size, int(size * 0.9))
    font = _font(size)
    if _width(font, text) <= max_w:
        return text, font
    while len(text) > 1 and _width(font, text + "…") > max_w:
        text = text[:-1]
    return text.rstrip() + "…", font


def _line_h(font) -> int:
    _, top, _, bottom = font.getbbox("ÁgЙy")
    return int(bottom - top)


# --- Части картинки ---

def _short(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s


def target_side(width: int, height: int, max_side: int) -> int:
    """Длинная сторона рисунка в картинке. Короткая сторона оригинала — не меньше MIN_ART; у вытянутых
    рисунков длинную сторону поднимаем выше max_side (до MAX_SIDE), чтобы короткая не ушла ниже MIN_ART.
    Не увеличиваем: длинная сторона результата не больше оригинальной."""
    long, short = max(width, height), min(width, height)
    if short < MIN_ART:
        raise SheetError(f"Картинка слишком маленькая ({width}×{height}) — нужна хотя бы {MIN_ART} px по короткой "
                         "стороне, иначе подписи не прочитать.")
    side = max(max_side, math.ceil(MIN_ART * long / short))
    if side > MAX_SIDE:
        raise SheetError(f"Картинка слишком вытянутая ({width}×{height}): при длинной стороне {MAX_SIDE} px короткая "
                         f"выйдет меньше {MIN_ART} px, и подписи не прочитать. Обрежь её или возьми для поста часть.")
    return min(side, long)


# Поворот по EXIF без лишней полной копии (ImageOps.exif_transpose копирует всю картинку).
_ORIENT = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
           5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
           8: Image.Transpose.ROTATE_90}
_REDUCIBLE = {"RGB", "RGBA", "RGBX", "L", "LA", "CMYK"}   # reduce усредняет пиксели — палитре (P) нельзя
_STRIP_BLOCKS = 256


def _reduce_in_strips(img: Image.Image, factor: int) -> Image.Image:
    """Image.reduce полосами: у RGBA/LA reduce сначала делает полноразмерную копию (premultiply), а полосой —
    только маленькую. Полосы кратны factor, поэтому результат тот же, что у reduce целиком."""
    w, h = img.size
    out = Image.new(img.mode, (math.ceil(w / factor), math.ceil(h / factor)))
    strip = factor * _STRIP_BLOCKS
    for y in range(0, h, strip):
        out.paste(img.crop((0, y, w, min(h, y + strip))).reduce(factor), (0, y // factor))
    out.info = dict(img.info)          # ICC-профиль и прочее — как у reduce
    return out


def _load_art(src: Path, max_side: int, bg: tuple) -> tuple[Image.Image, bytes | None]:
    """Рисунок, уменьшенный до target_side, в RGB. Огромные файлы сначала грубо уменьшаем (reduce) — до
    поворота и смены режима, чтобы сервер не держал в памяти несколько полноразмерных копий."""
    try:
        with media._open(src) as img:
            side = target_side(img.width, img.height, max_side)
            if img.format == "JPEG":
                img.draft(None, (side, side))
            img.load()
            orientation = img.getexif().get(0x0112)
            work = img
            factor = max(work.size) // (2 * side)        # запас ×2 для чистого LANCZOS, как thumbnail()
            if factor >= 2 and work.mode in _REDUCIBLE:
                work = _reduce_in_strips(work, factor)
            if orientation in _ORIENT:
                work = work.transpose(_ORIENT[orientation])
            work, icc = media._to_working_mode(work)
            if work is img:                               # файл сейчас закроется — нужна своя копия
                work = img.copy()
    except media.MediaError as e:
        raise SheetError(str(e)) from e
    except (OSError, Image.DecompressionBombError) as e:
        raise SheetError(f"Не получилось прочитать {src.name} — файл повреждён или не дописан.") from e
    if max(work.size) > side:
        k = side / max(work.size)
        work = work.resize((max(1, round(work.width * k)), max(1, round(work.height * k))), Image.Resampling.LANCZOS)
    if work.mode == "RGBA":  # прозрачный фон — на цвет полосы
        flat = Image.new("RGB", work.size, bg)
        flat.paste(work, mask=work.getchannel("A"))
        work = flat
    return work, icc


def _text_mark(text: str, width: int, opacity: float) -> Image.Image:
    """Знак для подписи не латиницей (встроенный в media шрифт знает только латиницу)."""
    size = 100
    font = _font(size)
    size = max(8, round(size * width / max(1, _width(font, text))))
    font = _font(size)
    stroke = max(1, size // 16)
    left, top, right, bottom = font.getbbox(text, stroke_width=stroke)
    mark = Image.new("RGBA", (int(right - left), int(bottom - top)), (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((-left, -top), text, font=font, fill=(255, 255, 255, 255), stroke_width=stroke,
                              stroke_fill=(0, 0, 0, 255))
    mark.putalpha(mark.getchannel("A").point(lambda a: round(a * opacity)))
    return mark


def _stack(parts: list[Image.Image], width: int, opacity: float) -> Image.Image:
    """Логотип над подписью (как media._build_mark), прозрачность — один раз на весь знак."""
    gap = max(2, width // 20) if len(parts) > 1 else 0
    mark = Image.new("RGBA", (max(p.width for p in parts), sum(p.height for p in parts) + gap * (len(parts) - 1)),
                     (0, 0, 0, 0))
    y = 0
    for p in parts:
        mark.alpha_composite(p, ((mark.width - p.width) // 2, y))
        y += p.height + gap
    mark.putalpha(mark.getchannel("A").point(lambda a: round(a * opacity)))
    return mark


def _watermark(art: Image.Image, text: str, logo: str | None, opacity, position: str) -> Image.Image:
    """Знак: логотип (если есть) и подпись. Подпись A–Z — встроенным шрифтом media, как в обычных превью;
    любая другая (кириллица, «Zoé») — системным шрифтом, который её знает (проверено в _check_glyphs)."""
    opacity_f = media._fraction(opacity, 0.05, 1.0, media.DEFAULT_OPACITY)
    width = max(8, math.floor(art.width * media.DEFAULT_SCALE))
    parts = []
    try:
        if logo:
            parts.append(media._build_mark("", Path(logo).expanduser(), width, 1.0,
                                           max(8, math.floor(art.height * 0.9))))
        if text:
            parts.append(media._build_mark(text, None, width, 1.0) if text.isascii() else _text_mark(text, width, 1.0))
    except media.MediaError as e:
        raise SheetError(str(e)) from e
    if not parts:
        return art
    return media._apply_mark(art, _stack(parts, width, opacity_f), position)


def _badge(art: Image.Image, status: str) -> None:
    label = BADGES[status]
    fill, ink = BADGE_COLORS[status]
    size = max(14, min(56, round(min(art.size) * 0.05)))
    font = _font(size)
    left, top, right, bottom = font.getbbox(label)
    pad_x, pad_y = round(size * 0.6), round(size * 0.35)
    m = max(8, round(min(art.size) * 0.03))
    w, h = int(right - left) + 2 * pad_x, int(bottom - top) + 2 * pad_y
    draw = ImageDraw.Draw(art)
    draw.rounded_rectangle((m, m, m + w, m + h), radius=max(4, size // 3), fill=fill)
    draw.text((m + pad_x - left, m + pad_y - top), label, font=font, fill=ink)


def _bottom(art: Image.Image, title: str, labels: list[str], byline: str, colors: dict) -> Image.Image:
    w = art.width
    pad = max(12, round(w * 0.03))
    base = max(14, min(64, round(min(w, art.height * 1.2) * 0.045)))
    inner = w - 2 * pad
    prices = "  ·  ".join(labels)
    # Две колонки (слева название и ник, справа цены), если влезают; иначе строками друг под другом.
    title_font, by_font, price_font = _font(round(base * 1.1)), _font(round(base * 0.8)), _font(base)
    left_w = max(_width(title_font, title) if title else 0, _width(by_font, byline))
    right_w = _width(price_font, prices) if prices else 0
    rows: list[tuple[str, Any, tuple, str]] = []
    if not prices or left_w + right_w + pad * 2 <= inner:
        two_col = bool(prices)
        left_rows = ([(title, round(base * 1.1), "text")] if title else []) + [(byline, round(base * 0.8), "muted")]
        for text, size, role in left_rows:
            limit = inner - (int(right_w) + pad * 2 if two_col else 0)
            t, f = _fit(text, max(40, limit), size, 10)
            rows.append((t, f, colors[role], "left"))
    else:
        two_col = False
        for text, size, role in ([(title, round(base * 1.1), "text")] if title else []) + [
                (prices, base, "text"), (byline, round(base * 0.8), "muted")]:
            t, f = _fit(text, inner, size, 10)
            rows.append((t, f, colors[role], "left"))
    gap = max(4, base // 3)
    content_h = sum(_line_h(f) for _, f, _, _ in rows) + gap * (len(rows) - 1)
    price_h = 0
    pt, pf = prices, price_font  # в две колонки цены уже влезли целиком (проверено выше)
    if two_col:
        price_h = _line_h(pf)
    bar_h = max(content_h, price_h) + 2 * pad
    out = Image.new("RGB", (w, art.height + bar_h), colors["bg"])
    out.paste(art, (0, 0))
    draw = ImageDraw.Draw(out)
    y = art.height + pad + (bar_h - 2 * pad - content_h) // 2
    for text, font, color, _ in rows:
        left, top, _, _ = font.getbbox(text)
        draw.text((pad - left, y - top), text, font=font, fill=color)
        y += _line_h(font) + gap
    if two_col:
        left, top, right, _ = pf.getbbox(pt)
        draw.text((w - pad - (right - left) - left, art.height + (bar_h - price_h) // 2 - top), pt, font=pf,
                  fill=colors["text"])
    return out


def _side(art: Image.Image, title: str, labels: list[str], byline: str, colors: dict) -> Image.Image:
    h = art.height
    panel = max(200, min(round(h * 0.42), round(art.width * 0.9)))
    pad = max(12, round(panel * 0.08))
    inner = panel - 2 * pad
    base = max(14, min(64, round(panel * 0.1)))
    while True:
        gap = max(4, base // 3)
        top_rows = ([(title, round(base * 1.1), "text")] if title else []) + [(t, base, "text") for t in labels]
        fitted = [(*_fit(t, inner, s, 10), colors[r]) for t, s, r in top_rows]
        by_t, by_f = _fit(byline, inner, round(base * 0.8), 10)
        need = sum(_line_h(f) + gap for _, f, _ in fitted) + gap * 2 + _line_h(by_f) + 2 * pad
        if need <= h or base <= 10:
            break
        base = max(10, int(base * 0.85))
    out = Image.new("RGB", (art.width + panel, h), colors["bg"])
    out.paste(art, (0, 0))
    draw = ImageDraw.Draw(out)
    x0, y = art.width + pad, pad
    for i, (text, font, color) in enumerate(fitted):
        left, top, _, _ = font.getbbox(text)
        draw.text((x0 - left, y - top), text, font=font, fill=color)
        y += _line_h(font) + gap + (gap * 2 if i == 0 and title else 0)
    left, top, _, _ = by_f.getbbox(by_t)
    draw.text((x0 - left, h - pad - _line_h(by_f) - top), by_t, font=by_f, fill=colors["muted"])
    return out


# --- Запись ---

JPEG_QUALITIES = (90, 82, 75, 68)       # пробуем по очереди, чтобы влезть в предел X
_SHRINK = 0.85
_SHRINK_MIN_SIDE = 600                  # мельче уже не уменьшаем — лучше честно сказать


def _encode(img: Image.Image, fmt: str, icc: bytes | None, quality: int = JPEG_QUALITIES[0]) -> bytes:
    buf = io.BytesIO()
    params: dict = {"optimize": True}
    if fmt == "JPEG":
        params.update(quality=quality, progressive=True, comment=MARKER.encode())
    else:
        meta = PngImagePlugin.PngInfo()
        meta.add_text("Software", MARKER)
        params["pnginfo"] = meta
    if icc:
        params["icc_profile"] = icc
    img.save(buf, fmt, **params)
    return buf.getvalue()


def fit_x_limit(img: Image.Image, fmt: str, icc: bytes | None,
                limit: int = X_IMAGE_LIMIT) -> tuple[Image.Image, bytes, str, list[str]]:
    """(картинка, байты, формат, что пришлось сделать). Больше limit — PNG → JPEG, потом качество ниже,
    потом размер меньше. Если и так не влезло — так и говорим, а не делаем вид, что влезло."""
    data = _encode(img, fmt, icc)
    if len(data) <= limit:
        return img, data, fmt, []
    notes = []
    if fmt == "PNG":
        notes.append("PNG вышел больше 5 МБ (предел X) — сохранила JPEG")
        fmt = "JPEG"
    quality = JPEG_QUALITIES[0]
    for quality in JPEG_QUALITIES:
        data = _encode(img, fmt, icc, quality)
        if len(data) <= limit:
            break
    if quality != JPEG_QUALITIES[0]:
        notes.append(f"качество JPEG снизила до {quality}, чтобы влезть в 5 МБ")
    shrunk = False
    while len(data) > limit and max(img.size) * _SHRINK >= _SHRINK_MIN_SIDE:
        img = img.resize((max(1, round(img.width * _SHRINK)), max(1, round(img.height * _SHRINK))),
                         Image.Resampling.LANCZOS)
        data = _encode(img, fmt, icc, quality)
        shrunk = True
    if shrunk:
        notes.append(f"уменьшила картинку до {img.width}×{img.height}, чтобы влезть в 5 МБ")
    if len(data) > limit:
        notes.append(f"всё равно {len(data) / 1024 / 1024:.1f} МБ — больше 5 МБ, X такую не примет; "
                     "возьми max_side поменьше")
    return img, data, fmt, notes


def _write_new(src: Path, data: bytes, ext: str) -> Path:
    """Создаём файл только если имени ещё нет (open 'xb'): её файлы и прошлые сборки не перезаписываем."""
    for i in range(1, _NAME_TRIES + 1):
        out = src.parent / f"{src.stem}-sheet{'' if i == 1 else f'-{i}'}{ext}"
        try:
            with open(out, "xb") as f:
                f.write(data)
            return out
        except FileExistsError:
            continue
        except OSError as e:
            out.unlink(missing_ok=True)
            raise SheetError(f"Не получилось записать картинку в {_short(src.parent)} — нет доступа или места.") from e
    raise SheetError(f"Рядом уже {_NAME_TRIES} файлов «{src.stem}-sheet…» — разбери папку.")


def make_sheet(file_path: str, *, nick: str, prices: dict | None = None, price=None, currency: str = "usd",
               status: str = "", title: str = "", layout: str = "bottom", theme: str = "dark",
               watermark: bool = True, watermark_text: str = "", logo: str | None = None,
               opacity: float = 35, position: str = "tiled", max_side: int = DEFAULT_MAX_SIDE,
               fmt: str = "png", check=None) -> dict:
    """Собрать картинку для поста. Ничего не публикует.

    prices — {"sb", "mi", "ab"} (числа или короткий текст) или price — одна цена; currency — usd|eur|gbp|rub|points.
    status — open|sold|hold|pending или пусто; layout — bottom|side; theme — dark|light.
    watermark — знак на рисунке (текст — watermark_text или ник; logo — PNG); position — как в media.
    max_side — длинная сторона рисунка, 300–3000. fmt — png|jpeg.
    check — проверка пути (по умолчанию posting.check_image: только Загрузки, Рабочий стол, Изображения).
    Возвращает {"path", "width", "height", "format", "bytes", "labels", "warnings", "summary"}."""
    if check is None:
        from .posting import check_image as check
    nick = " ".join(str(nick or "").split())
    if not nick:
        raise SheetError("Нужен её ник — он подписывает картинку.")
    layout = (layout or "bottom").strip().lower()
    if layout not in LAYOUTS:
        raise SheetError(f"Раскладка: bottom (полоса снизу) или side (панель сбоку), не «{layout}».")
    theme = (theme or "dark").strip().lower()
    if theme not in THEMES:
        raise SheetError("Тема: dark или light.")
    status_key = (status or "").strip().lower()
    status_key = _BADGE_ALIASES.get(status_key, status_key)
    if status_key and status_key not in BADGES:
        raise SheetError(f"Плашка: {', '.join(BADGES)} или без неё.")
    position = (position or "tiled").strip().lower()
    if position not in media.POSITIONS:
        raise SheetError(f"Положение знака: {', '.join(media.POSITIONS)}.")
    out_fmt = {"png": "PNG", "jpeg": "JPEG", "jpg": "JPEG"}.get(str(fmt or "png").strip().lower())
    if not out_fmt:
        raise SheetError("Формат: png или jpeg.")
    try:
        side = int(max_side)
    except (TypeError, ValueError):
        side = DEFAULT_MAX_SIDE
    side = min(max(side, MIN_SIDE), MAX_SIDE)
    probe = _font(32)
    labels = [_plain_currency(t, probe) for t in price_lines(prices, price, currency)]
    warnings = price_warnings(prices)
    title = " ".join(str(title or "").split())
    byline = f"by {nick}"
    mark_text = " ".join(str(watermark_text or "").split()) or nick
    _check_glyphs([title, byline, *labels, mark_text if watermark else ""])

    path, why = check(str(file_path))
    if path is None:
        raise SheetError(why)
    src = Path(path)
    colors = THEMES[theme]
    art, icc = _load_art(src, side, colors["bg"])
    if max(art.size) > side:
        warnings.append(f"рисунок вытянутый — оставила {max(art.size)} px по длинной стороне, чтобы короткая "
                        f"была не меньше {MIN_ART} px")
    if watermark:
        art = _watermark(art, mark_text, logo, opacity, position)
    if status_key:
        _badge(art, status_key)
    sheet = (_bottom if layout == "bottom" else _side)(art, title, labels, byline, colors)

    sheet, data, out_fmt, size_notes = fit_x_limit(sheet, out_fmt, icc)
    warnings += size_notes
    out = _write_new(src, data, ".png" if out_fmt == "PNG" else ".jpg")

    bits = [f"Картинка для поста: {_short(out)} — {sheet.width}×{sheet.height}, {out_fmt}, "
            f"{max(1, round(len(data) / 1024))} КБ", LAYOUTS[layout],
            ("цены " + " · ".join(labels)) if labels else "без цены", f"ник {nick}"]
    if title:
        bits.append(f"название «{title}»")
    if status_key:
        bits.append(f"плашка {BADGES[status_key]}")
    if watermark:
        bits.append(f"водяной знак «{mark_text}»{' и логотип' if logo else ''} {media.POSITIONS[position]}")
    else:
        bits.append("без водяного знака")
    summary = "; ".join(bits) + ". Метаданных оригинала нет. Оригинал не тронут. Посмотри картинку перед постом."
    if warnings:
        summary += "\nПроверь: " + "; ".join(warnings) + "."
    return {"path": str(out), "width": sheet.width, "height": sheet.height, "format": out_fmt, "bytes": len(data),
            "labels": labels, "warnings": warnings, "summary": summary}
