"""Защищённое превью работы перед публикацией: уменьшить, поставить водяной знак, убрать метаданные.

Оригинал никогда не трогаем: превью пишется рядом как «<имя>-preview.jpg|png» (или в подпапку).
Шрифт — встроенный в Pillow (load_default), системные шрифты не нужны; он знает только латиницу.
"""

import io
import math
import os
import tempfile
import warnings
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageOps, PngImagePlugin, UnidentifiedImageError

# Больше — почти наверняка не рисунок, а ошибка или «бомба»: на распаковку уйдут гигабайты памяти.
MAX_PIXELS = 90_000_000
DEFAULT_MAX_SIDE = 1200
DEFAULT_QUALITY = 85
DEFAULT_OPACITY = 0.35
DEFAULT_SCALE = 0.25
POSITIONS = {
    "tiled": "сеткой",
    "center": "по центру",
    "bottom-right": "справа снизу",
    "bottom-left": "слева снизу",
    "top-right": "справа сверху",
    "top-left": "слева сверху",
}
_FORMATS = {"jpeg": "JPEG", "jpg": "JPEG", "png": "PNG"}
_EXT = {"JPEG": ".jpg", "PNG": ".png"}
_ANIMATED_FORMATS = {"GIF", "PNG", "WEBP"}  # MPO с айфона — не анимация, берём первый кадр
_TAG_ORIENTATION, _TAG_GPS = 0x0112, 0x8825
# Метка «это превью сделали мы»: только такой файл можно перезаписать. Её собственный «-preview» не трогаем.
MARKER = "mcp_devian-preview"
_NAME_TRIES = 50


class MediaError(ValueError):
    """Понятная ей причина, почему превью не сделать."""


def _finite(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _fraction(value: float, lo: float, hi: float, default: float) -> float:
    v = _finite(value, default)
    if v > 1:  # «35» — это проценты
        v /= 100
    return min(max(v, lo), hi)


def _short(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s


def _open(src: Path) -> Image.Image:
    if not src.is_file():
        raise MediaError(f"Не нашла файл {_short(src)}.")
    if src.suffix.lower() in (".heic", ".heif"):
        raise MediaError("HEIC пока не умею — экспортируй картинку в JPEG или PNG.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            img = Image.open(src)
    except Image.DecompressionBombError as e:
        raise MediaError("Картинка слишком огромная для превью — сначала уменьши её в редакторе.") from e
    except (UnidentifiedImageError, OSError) as e:
        raise MediaError(f"{src.name} не похож на картинку (нужен JPEG, PNG, WebP или GIF).") from e
    w, h = img.size
    if w * h > MAX_PIXELS:
        img.close()
        raise MediaError(f"Картинка слишком огромная ({w}×{h}) — сначала уменьши её в редакторе.")
    if getattr(img, "is_animated", False) and img.format in _ANIMATED_FORMATS:
        img.close()
        raise MediaError("Это анимация — превью с водяным знаком делаю только для статичных картинок. "
                         "Сохрани нужный кадр в PNG, и я сделаю.")
    return img


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in img.info


def _to_working_mode(img: Image.Image) -> tuple[Image.Image, bytes | None]:
    """RGB или RGBA + ICC-профиль, который стоит сохранить в превью."""
    icc = img.info.get("icc_profile")
    if img.mode == "CMYK":
        if icc:
            try:
                srgb = ImageCms.createProfile("sRGB")
                rgb = ImageCms.profileToProfile(img, ImageCms.ImageCmsProfile(io.BytesIO(icc)), srgb, outputMode="RGB")
                if rgb is not None:  # None только при inPlace=True, которого тут нет
                    return rgb, ImageCms.ImageCmsProfile(srgb).tobytes()
            except (ImageCms.PyCMSError, OSError, TypeError):
                pass
        return img.convert("RGB"), None
    icc = icc if _rgb_profile(icc) else None  # серый профиль в RGB-файле — недопустимое сочетание
    if _has_alpha(img):
        return img.convert("RGBA"), icc
    if img.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        img = img.convert("I").point(lambda v: v / 256).convert("L")
    return (img if img.mode == "RGB" else img.convert("RGB")), icc


def _rgb_profile(icc: bytes | None) -> bool:
    if not icc:
        return False
    try:
        return ImageCms.ImageCmsProfile(io.BytesIO(icc)).profile.xcolor_space.strip() == "RGB"
    except (ImageCms.PyCMSError, OSError, TypeError, ValueError, AttributeError):
        return False


def _render_text(text: str, width: int) -> Image.Image:
    if any(ord(c) > 0x24F for c in text):
        raise MediaError("Встроенный шрифт знает только латиницу — напиши подпись латиницей или дай логотип PNG.")
    probe = ImageFont.load_default(size=100)
    left, top, right, bottom = probe.getbbox(text)
    size = max(8, round(100 * width / max(1, right - left)))
    font = ImageFont.load_default(size=size)
    stroke = max(1, size // 16)
    left, top, right, bottom = font.getbbox(text, stroke_width=stroke)
    canvas = Image.new("RGBA", (int(right - left), int(bottom - top)), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((-left, -top), text, font=font, fill=(255, 255, 255, 255),
                                stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    if abs(canvas.width - width) > max(2, width // 20):  # растровый шрифт без FreeType размер не меняет
        canvas = canvas.resize((width, max(1, round(canvas.height * width / canvas.width))), Image.Resampling.LANCZOS)
    return canvas


def _load_logo(logo: Path, width: int, max_height: int) -> Image.Image:
    """Логотип, вписанный в width × max_height с сохранением пропорций (высокий узкий — не растягиваем)."""
    logo = Path(logo).expanduser()
    if not logo.is_file():
        raise MediaError(f"Не нашла логотип {_short(logo)}.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(logo) as raw:
                if raw.width * raw.height > MAX_PIXELS:
                    raise MediaError(f"Логотип {logo.name} слишком огромный — уменьши его в редакторе.")
                img = raw.convert("RGBA")
    except Image.DecompressionBombError as e:
        raise MediaError(f"Логотип {logo.name} слишком огромный — уменьши его в редакторе.") from e
    except (UnidentifiedImageError, OSError) as e:
        raise MediaError(f"Логотип {logo.name} не открывается — нужен PNG.") from e
    k = min(width / img.width, max(1, max_height) / img.height)
    return img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.Resampling.LANCZOS)


def _build_mark(text: str, logo: Path | None, width: int, opacity: float, max_height: int | None = None) -> Image.Image:
    parts = []
    if logo:
        parts.append(_load_logo(logo, width, max_height or width * 4))
    if text:
        parts.append(_render_text(text, width))
    gap = max(2, width // 20) if len(parts) > 1 else 0
    mark = Image.new("RGBA", (max(p.width for p in parts), sum(p.height for p in parts) + gap), (0, 0, 0, 0))
    y = 0
    for p in parts:
        mark.alpha_composite(p, ((mark.width - p.width) // 2, y))
        y += p.height + gap
    alpha = mark.getchannel("A").point(lambda a: round(a * opacity))
    mark.putalpha(alpha)
    return mark


def _composite(layer: Image.Image, mark: Image.Image, x: int, y: int) -> None:
    """alpha_composite, которому можно отрицательные координаты (знак у края, наполовину за кадром)."""
    sx, sy = max(0, -x), max(0, -y)
    if sx < mark.width and sy < mark.height:
        layer.alpha_composite(mark, (max(0, x), max(0, y)), (sx, sy))


def _apply_mark(img: Image.Image, mark: Image.Image, position: str) -> Image.Image:
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    w, h = base.size
    if position == "tiled":
        tile = mark.rotate(30, expand=True, resample=Image.Resampling.BICUBIC)
        gap = max(mark.width // 2, 8)
        step_x, step_y = tile.width + gap, tile.height + gap // 2
        for row, y in enumerate(range(-tile.height // 2, h, step_y)):
            shift = (step_x // 2) * (row % 2)
            for x in range(-tile.width // 2 - shift, w, step_x):
                _composite(layer, tile, x, y)
    else:
        margin = round(0.03 * min(w, h))
        x = {"left": margin, "right": w - mark.width - margin}.get(position.split("-")[-1], (w - mark.width) // 2)
        y = {"top": margin, "bottom": h - mark.height - margin}.get(position.split("-")[0], (h - mark.height) // 2)
        _composite(layer, mark, x, y)
    out = Image.alpha_composite(base, layer)
    return out if img.mode == "RGBA" else out.convert("RGB")


def _is_ours(path: Path) -> bool:
    """Файл — наше прошлое превью (с меткой MARKER)? Её собственные файлы без метки не перезаписываем."""
    try:
        with Image.open(path) as img:
            info = img.info
            comment = info.get("comment") or b""
            comment = comment.decode("latin-1", "ignore") if isinstance(comment, bytes) else str(comment)
            return MARKER in comment or MARKER in str(info.get("Software") or "")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError):
        return False


def _out_path(src: Path, subfolder: str | None, fmt: str) -> Path:
    folder = src.parent
    if subfolder:
        sub = Path(subfolder)
        if sub.is_absolute() or ".." in sub.parts:
            raise MediaError("Подпапка для превью — только простое имя внутри папки с работой, например «previews».")
        folder = folder / sub
    for i in range(1, _NAME_TRIES + 1):
        out = folder / f"{src.stem}-preview{'' if i == 1 else f'-{i}'}{_EXT[fmt]}"
        if out.exists() and out.resolve() == src.resolve():
            continue
        if not out.exists() or (out.is_file() and _is_ours(out)):
            return out
    raise MediaError(f"Рядом уже {_NAME_TRIES} файлов «{src.stem}-preview…» — разбери папку или укажи подпапку.")


def _clean_exif(exif: Image.Exif) -> Image.Exif:
    exif[_TAG_ORIENTATION] = 1  # поворот уже применён к пикселям
    exif.pop(_TAG_GPS, None)  # место съёмки не публикуем никогда
    return exif


def _save(img: Image.Image, out: Path, fmt: str, quality: int, icc: bytes | None, exif: Image.Exif | None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    img.info = {}  # ни текстовых чанков PNG, ни XMP — только то, что передаём явно
    params: dict = {"optimize": True}
    if fmt == "JPEG":
        params.update(quality=quality, progressive=True, comment=MARKER.encode())
    else:
        meta = PngImagePlugin.PngInfo()
        meta.add_text("Software", MARKER)
        params["pnginfo"] = meta
    if icc:
        params["icc_profile"] = icc
    if exif is not None:
        params["exif"] = exif.tobytes()
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".preview-", suffix=_EXT[fmt])
    os.close(fd)
    try:
        img.save(tmp, fmt, **params)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def prepare_preview(path: str | Path, *, text: str = "", logo: str | Path | None = None,
                    opacity: float = DEFAULT_OPACITY, position: str = "tiled", scale: float = DEFAULT_SCALE,
                    max_side: int = DEFAULT_MAX_SIDE, quality: int = DEFAULT_QUALITY, fmt: str | None = None,
                    strip_metadata: bool = True, subfolder: str | None = None, require_mark: bool = False) -> dict:
    """Сделать превью для публикации. Оригинал не меняется.

    text/logo — подпись (латиницей) и/или PNG-логотип; без них — только уменьшение и чистка метаданных.
    opacity и scale (ширина знака относительно ширины картинки) — доли 0..1 или проценты.
    position — tiled | center | bottom-right | bottom-left | top-right | top-left.
    max_side — длинная сторона в px (0 — не уменьшать, увеличивать не будем никогда).
    fmt — jpeg | png | None (PNG, если есть прозрачность, иначе JPEG).
    strip_metadata=False оставляет EXIF, но GPS всё равно убирается.
    require_mark=True — без подписи и логотипа не делаем (для публикации «с водяным знаком»).
    Рядом уже лежит её собственный «<имя>-preview» — не трогаем, пишем «-preview-2» и т.д.
    Возвращает {"path", "width", "height", "format", "bytes", "summary"}; ошибки — MediaError с текстом для неё.
    """
    src = Path(path).expanduser()
    position = (position or "tiled").strip().lower()
    if position not in POSITIONS:
        raise MediaError(f"Не знаю положение «{position}». Можно: {', '.join(POSITIONS)}.")
    out_fmt = _FORMATS.get(str(fmt).lower()) if fmt else None
    if fmt and not out_fmt:
        raise MediaError("Формат превью — jpeg или png.")
    opacity = _fraction(opacity, 0.05, 1.0, DEFAULT_OPACITY)
    scale = _fraction(scale, 0.05, 0.9, DEFAULT_SCALE)
    quality = int(min(max(_finite(quality, DEFAULT_QUALITY), 30), 95))
    max_side = max(0, int(_finite(max_side, DEFAULT_MAX_SIDE)))
    text = " ".join(str(text or "").split())
    if require_mark and not (text or logo):
        raise MediaError("Для водяного знака нужна подпись (её ник DA) или логотип — без них превью не делаю.")

    with _open(src) as img:
        exif = None if strip_metadata else _clean_exif(img.getexif())
        if max_side and img.format == "JPEG":
            img.draft(None, (max_side, max_side))  # большие JPEG сразу читаем уменьшенными — меньше памяти
        try:
            img.load()
            work = ImageOps.exif_transpose(img)
        except (OSError, Image.DecompressionBombError) as e:
            raise MediaError(f"Не получилось прочитать {src.name} — файл повреждён или не дописан.") from e
        work, icc = _to_working_mode(work)

    if max_side and max(work.size) > max_side:
        k = max_side / max(work.size)
        work = work.resize((max(1, round(work.width * k)), max(1, round(work.height * k))), Image.Resampling.LANCZOS)

    mark_parts = []
    if text or logo:
        mark = _build_mark(text, Path(logo) if logo else None, max(8, math.floor(work.width * scale)), opacity,
                           max(8, math.floor(work.height * 0.9)))
        work = _apply_mark(work, mark, position)
        if text:
            mark_parts.append(f"«{text}»")
        if logo:
            mark_parts.append("логотип")

    out_fmt = out_fmt or ("PNG" if work.mode == "RGBA" else "JPEG")
    if out_fmt == "JPEG" and work.mode == "RGBA":
        flat = Image.new("RGB", work.size, (255, 255, 255))
        flat.paste(work, mask=work.getchannel("A"))
        work = flat

    out = _out_path(src, subfolder, out_fmt)
    try:
        _save(work, out, out_fmt, quality, icc, exif)
        size = out.stat().st_size
    except OSError as e:
        raise MediaError(f"Не получилось записать превью в {_short(out.parent)} — нет доступа или места.") from e

    kind = f"JPEG q{quality}" if out_fmt == "JPEG" else "PNG"
    mark_txt = (f"знак {' + '.join(mark_parts)} {POSITIONS[position]}, {round(opacity * 100)}%"
                if mark_parts else "без водяного знака")
    meta_txt = "метаданные (EXIF, GPS) убраны" if strip_metadata else "EXIF оставлен, GPS убран"
    summary = (f"Превью: {_short(out)} — {work.width}×{work.height}, {kind}, {max(1, round(size / 1024))} КБ; "
               f"{mark_txt}; {meta_txt}. Оригинал не тронут.")
    return {"path": str(out), "width": work.width, "height": work.height, "format": out_fmt,
            "bytes": size, "summary": summary}
