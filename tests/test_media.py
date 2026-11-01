import hashlib

import pytest
from PIL import Image, ImageCms

from mcp_devian import media
from mcp_devian.media import MediaError, prepare_preview


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jpeg_with_exif(path, size=(2400, 1600), orientation=1, gps=True):
    img = Image.new("RGB", size, (40, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = orientation  # Orientation
    exif[0x010F] = "Apple"  # Make
    if gps:
        exif[0x8825] = {1: "N", 2: (55.0, 45.0, 0.0), 3: "E", 4: (37.0, 37.0, 0.0)}
    img.save(path, "JPEG", exif=exif, quality=95)
    return path


def test_preview_next_to_original_downscaled_and_original_untouched(tmp_path):
    src = _jpeg_with_exif(tmp_path / "fox.jpg")
    before = _digest(src)
    res = prepare_preview(src, text="foxartist")
    out = tmp_path / "fox-preview.jpg"
    assert res["path"] == str(out) and out.exists()
    assert _digest(src) == before
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 1200 and img.size == (1200, 800)
    assert (res["width"], res["height"], res["format"]) == (1200, 800, "JPEG")
    assert "1200×800" in res["summary"] and "оригинал не тронут" in res["summary"].lower()


def test_watermark_changes_pixels(tmp_path):
    src = tmp_path / "flat.png"
    Image.new("RGB", (800, 600), (128, 128, 128)).save(src)
    prepare_preview(src, text="foxartist", position="center", opacity=0.6)
    with Image.open(tmp_path / "flat-preview.jpg") as img:
        colors = img.convert("L").getextrema()
    assert colors[0] < 110 or colors[1] > 150


def test_no_watermark_when_nothing_given(tmp_path):
    src = tmp_path / "flat.png"
    Image.new("RGB", (300, 200), (128, 128, 128)).save(src)
    res = prepare_preview(src)
    with Image.open(res["path"]) as img:
        lo, hi = img.convert("L").getextrema()
    assert hi - lo <= 3
    assert "без водяного знака" in res["summary"]


def test_strips_gps_and_exif_by_default(tmp_path):
    src = _jpeg_with_exif(tmp_path / "a.jpg")
    res = prepare_preview(src, text="x")
    with Image.open(res["path"]) as img:
        exif = img.getexif()
    assert 0x8825 not in exif and 0x010F not in exif
    assert "убраны" in res["summary"]


def test_keep_metadata_still_drops_gps(tmp_path):
    src = _jpeg_with_exif(tmp_path / "a.jpg")
    res = prepare_preview(src, text="x", strip_metadata=False)
    with Image.open(res["path"]) as img:
        exif = img.getexif()
    assert exif.get(0x010F) == "Apple"
    assert 0x8825 not in exif
    assert exif.get(0x0112, 1) == 1


def test_exif_orientation_applied(tmp_path):
    # Orientation 6 = повернуть на 90°: файл 2400×1600 показывается портретом 1600×2400.
    src = _jpeg_with_exif(tmp_path / "phone.jpg", orientation=6)
    res = prepare_preview(src)
    assert (res["width"], res["height"]) == (800, 1200)


def test_png_transparency_kept(tmp_path):
    src = tmp_path / "sticker.png"
    img = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (200, 200, 400, 400))
    img.save(src)
    res = prepare_preview(src, text="foxartist", position="bottom-right")
    assert res["path"].endswith("sticker-preview.png")
    with Image.open(res["path"]) as out:
        assert out.mode == "RGBA"
        assert out.getpixel((0, 0))[3] == 0
        assert out.getpixel((300, 300))[:3] == (255, 0, 0)


def test_png_forced_to_jpeg_flattens_on_white(tmp_path):
    src = tmp_path / "sticker.png"
    Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(src)
    res = prepare_preview(src, fmt="jpeg")
    with Image.open(res["path"]) as out:
        assert out.mode == "RGB" and out.getpixel((5, 5)) >= (250, 250, 250)


def test_palette_png_with_transparency(tmp_path):
    src = tmp_path / "pal.png"
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).convert("P").save(src, transparency=0)
    res = prepare_preview(src, text="z")
    assert res["format"] == "PNG"


def test_cmyk_converted_to_rgb(tmp_path):
    src = tmp_path / "print.jpg"
    Image.new("CMYK", (400, 300), (0, 255, 255, 0)).save(src, "JPEG")
    res = prepare_preview(src, text="foxartist")
    with Image.open(res["path"]) as out:
        assert out.mode == "RGB"
        r, g, b = out.getpixel((2, 2))
        assert r > 200 and g < 80 and b < 80


def test_rgb_icc_profile_kept(tmp_path):
    src = tmp_path / "p3.jpg"
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (200, 200), (10, 20, 30)).save(src, "JPEG", icc_profile=icc)
    res = prepare_preview(src)
    with Image.open(res["path"]) as out:
        assert out.info.get("icc_profile") == icc


def test_animated_gif_refused_politely(tmp_path):
    src = tmp_path / "blink.gif"
    frames = [Image.new("RGB", (40, 40), c) for c in ("red", "blue")]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(MediaError, match="анимац"):
        prepare_preview(src, text="x")
    assert not (tmp_path / "blink-preview.gif").exists()


def test_static_gif_ok(tmp_path):
    src = tmp_path / "still.gif"
    Image.new("P", (40, 40), 3).save(src)
    res = prepare_preview(src)
    assert res["format"] in ("PNG", "JPEG")


def test_huge_image_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MAX_PIXELS", 1000)
    src = tmp_path / "big.png"
    Image.new("RGB", (100, 100)).save(src)
    with pytest.raises(MediaError, match="огромн|слишком"):
        prepare_preview(src)


def test_not_an_image_and_missing_file(tmp_path):
    bad = tmp_path / "notes.jpg"
    bad.write_text("hello")
    with pytest.raises(MediaError, match="картинк"):
        prepare_preview(bad)
    with pytest.raises(MediaError, match="(?i)не нашла"):
        prepare_preview(tmp_path / "nope.png")


def test_never_upscales_small_images(tmp_path):
    src = tmp_path / "tiny.png"
    Image.new("RGB", (300, 200), "white").save(src)
    res = prepare_preview(src, max_side=1200)
    assert (res["width"], res["height"]) == (300, 200)


def test_max_side_zero_keeps_size(tmp_path):
    src = tmp_path / "w.png"
    Image.new("RGB", (1500, 100), "white").save(src)
    res = prepare_preview(src, max_side=0)
    assert res["width"] == 1500


def test_subfolder_output(tmp_path):
    src = tmp_path / "fox.png"
    Image.new("RGB", (100, 100), "white").save(src)
    res = prepare_preview(src, subfolder="previews")
    assert res["path"] == str(tmp_path / "previews" / "fox-preview.jpg")


def test_subfolder_cannot_escape(tmp_path):
    src = tmp_path / "fox.png"
    Image.new("RGB", (100, 100), "white").save(src)
    with pytest.raises(MediaError):
        prepare_preview(src, subfolder="../../etc")


def test_logo_watermark(tmp_path):
    src = tmp_path / "art.png"
    Image.new("RGB", (1000, 800), (0, 0, 0)).save(src)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (200, 100), (255, 255, 255, 255)).save(logo)
    res = prepare_preview(src, logo=logo, position="center", opacity=0.5, scale=0.3)
    with Image.open(res["path"]) as out:
        r, _, _ = out.convert("RGB").getpixel((500, 400))
    assert 100 < r < 160  # белый логотип на чёрном с прозрачностью 50%
    assert "логотип" in res["summary"]


def test_logo_missing_is_error(tmp_path):
    src = tmp_path / "art.png"
    Image.new("RGB", (100, 100)).save(src)
    with pytest.raises(MediaError, match="логотип"):
        prepare_preview(src, logo=tmp_path / "nope.png")


def test_tiled_covers_whole_image(tmp_path):
    src = tmp_path / "flat.png"
    Image.new("RGB", (1200, 1200), (128, 128, 128)).save(src)
    res = prepare_preview(src, text="foxartist", position="tiled", opacity=0.5)
    with Image.open(res["path"]) as out:
        g = out.convert("L")
        quads = [g.crop(box).getextrema() for box in ((0, 0, 600, 600), (600, 0, 1200, 600),
                                                      (0, 600, 600, 1200), (600, 600, 1200, 1200))]
    assert all(hi - lo > 30 for lo, hi in quads)
    assert "сеткой" in res["summary"]


def test_percent_inputs_and_bad_position(tmp_path):
    src = tmp_path / "flat.png"
    Image.new("RGB", (400, 300), (128, 128, 128)).save(src)
    res = prepare_preview(src, text="x", opacity=35, scale=25)
    assert "35%" in res["summary"]
    with pytest.raises(MediaError, match="position|положени"):
        prepare_preview(src, text="x", position="middle-ish")


def test_preview_of_preview_does_not_overwrite_source(tmp_path):
    src = tmp_path / "fox-preview.jpg"
    Image.new("RGB", (100, 100), "white").save(src)
    before = _digest(src)
    res = prepare_preview(src)
    assert res["path"] != str(src) and _digest(src) == before


def test_output_quality_affects_size(tmp_path):
    src = tmp_path / "noise.png"
    Image.effect_noise((800, 800), 60).convert("RGB").save(src)
    hi = prepare_preview(src, quality=95, subfolder="hi")
    lo = prepare_preview(src, quality=40, subfolder="lo")
    assert lo["bytes"] < hi["bytes"]


# --- Регрессии после проверки ---

def test_her_own_preview_file_is_never_overwritten(tmp_path):
    src = _jpeg_with_exif(tmp_path / "adopt1.jpg", size=(800, 600))
    hers = tmp_path / "adopt1-preview.jpg"
    Image.new("RGB", (300, 200), (200, 10, 10)).save(hers, "JPEG")
    before = _digest(hers)
    res = prepare_preview(src, text="foxartist")
    assert res["path"] == str(tmp_path / "adopt1-preview-2.jpg")
    assert _digest(hers) == before
    again = prepare_preview(src, text="foxartist", opacity=0.6)     # наше прошлое превью — можно заменить
    assert again["path"] == res["path"] and _digest(hers) == before


def test_our_png_preview_is_marked_and_reused(tmp_path):
    src = tmp_path / "a.png"
    Image.new("RGBA", (300, 200), (0, 0, 0, 0)).save(src)
    first = prepare_preview(src, text="nick")
    assert media._is_ours(media.Path(first["path"]))
    assert prepare_preview(src, text="nick")["path"] == first["path"]


def test_write_error_is_a_media_error(tmp_path):
    folder = tmp_path / "ro"
    folder.mkdir()
    src = _jpeg_with_exif(folder / "fox.jpg", size=(400, 300))
    folder.chmod(0o500)
    try:
        with pytest.raises(MediaError, match="записать превью"):
            prepare_preview(src, text="nick")
    finally:
        folder.chmod(0o700)


def test_nan_and_negative_inputs_are_safe(tmp_path):
    src = _jpeg_with_exif(tmp_path / "fox.jpg", size=(600, 400))
    nan = float("nan")
    res = prepare_preview(src, text="nick", opacity=nan, scale=nan, quality=nan)
    assert "35%" in res["summary"] and "q85" in res["summary"]
    res = prepare_preview(src, max_side=-1)
    assert (res["width"], res["height"]) == (600, 400)


def test_huge_logo_is_a_media_error(tmp_path, monkeypatch):
    src = _jpeg_with_exif(tmp_path / "fox.jpg", size=(400, 300))
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (200, 200), (255, 255, 255, 255)).save(logo)
    monkeypatch.setattr(media, "MAX_PIXELS", 1000)
    with pytest.raises(MediaError, match="огромный"):
        media._load_logo(logo, 100, 100)
    monkeypatch.undo()
    big = tmp_path / "big.png"
    Image.new("RGBA", (1000, 1000), (255, 255, 255, 255)).save(big)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 200_000)   # картинка 400×300 проходит, логотип — «бомба»
    with pytest.raises(MediaError, match="Логотип big.png слишком огромный"):
        prepare_preview(src, logo=big)


def test_tall_logo_fits_inside_the_picture(tmp_path):
    src = _jpeg_with_exif(tmp_path / "fox.jpg", size=(800, 400))
    logo = tmp_path / "tall.png"
    Image.new("RGBA", (1, 5000), (255, 0, 0, 255)).save(logo)
    mark = media._build_mark("", logo, 200, 0.5, 360)
    assert mark.height <= 360 and mark.width <= 200
    res = prepare_preview(src, logo=logo, position="center")
    assert (res["width"], res["height"]) == (800, 400)


def test_gray_or_lab_profile_not_embedded_in_rgb(tmp_path):
    lab = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    assert media._rgb_profile(srgb) and not media._rgb_profile(lab) and not media._rgb_profile(b"junk")
    src = tmp_path / "gray.png"
    Image.new("L", (300, 200), 128).save(src, icc_profile=lab)
    res = prepare_preview(src, fmt="jpeg")
    with Image.open(res["path"]) as img:
        assert img.mode == "RGB" and not img.info.get("icc_profile")


def test_require_mark_refuses_empty_watermark(tmp_path):
    src = _jpeg_with_exif(tmp_path / "fox.jpg", size=(400, 300))
    with pytest.raises(MediaError, match="ник DA"):
        prepare_preview(src, text="  ", require_mark=True)
    assert not (tmp_path / "fox-preview.jpg").exists()
