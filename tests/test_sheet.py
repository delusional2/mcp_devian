import hashlib

import pytest
from PIL import Image

from mcp_devian import posting, sheet
from mcp_devian.sheet import SheetError, make_sheet, price_lines


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "image_roots", lambda: [tmp_path])
    return tmp_path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _art(path, size=(1000, 800), color=(90, 160, 220)):
    img = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (55.0, 45.0, 0.0)}
    img.save(path, "JPEG", exif=exif, quality=95)
    return path


# --- подписи цен ---

def test_price_lines_sb_mi_ab_in_order_and_currency():
    assert price_lines({"ab": 80, "SB": 15, "mi": 5}, None, "usd") == ["SB $15", "MI $5", "AB $80"]
    assert price_lines({"sb": 1500}, None, "rub") == ["SB 1,500 ₽"]
    assert price_lines({"sb": 300}, None, "points") == ["SB 300 pts"]
    assert price_lines(None, 30, "eur") == ["Price €30"]
    assert price_lines(None, None, "usd") == []


def test_price_lines_reject_bad_values():
    with pytest.raises(SheetError):
        price_lines({"sb": 10}, 20, "usd")          # и набор, и одна цена — неясно, что писать
    with pytest.raises(SheetError):
        price_lines({"sb": -5}, None, "usd")
    with pytest.raises(SheetError):
        price_lines({"sb": float("nan")}, None, "usd")
    with pytest.raises(SheetError):
        price_lines({"xx": 5}, None, "usd")
    with pytest.raises(SheetError):
        price_lines({"sb": 5}, None, "btc")


def test_price_warnings_ab_below_sb():
    assert any("AB" in w for w in sheet.price_warnings({"sb": 50, "ab": 20}))
    assert sheet.price_warnings({"sb": 15, "ab": 80}) == []


# --- сборка ---

def test_bottom_layout_next_to_original_original_untouched(roots):
    src = _art(roots / "moth.jpg")
    before = _digest(src)
    res = make_sheet(str(src), nick="mothmaker", prices={"sb": 15, "mi": 5, "ab": 80}, currency="usd",
                     title="Moth adopt #3", status="open")
    out = roots / "moth-sheet.png"
    assert res["path"] == str(out) and out.exists()
    assert _digest(src) == before
    with Image.open(out) as img:
        assert img.width == 1000 and img.height > 800        # полоса снизу добавила высоту
        assert "exif" not in img.info                         # GPS оригинала не уезжает в пост
    assert res["labels"] == ["SB $15", "MI $5", "AB $80"]
    assert "оригинал не тронут" in res["summary"].lower()
    assert "полоса снизу" in res["summary"]


def test_side_layout_adds_width(roots):
    src = _art(roots / "fox.jpg", size=(800, 1000))
    res = make_sheet(str(src), nick="foxy", price=25, currency="usd", layout="side")
    with Image.open(res["path"]) as img:
        assert img.height == 1000 and img.width > 800


def test_long_side_limited(roots):
    src = _art(roots / "big.jpg", size=(4000, 3000))
    res = make_sheet(str(src), nick="me", price=10, max_side=1200)
    with Image.open(res["path"]) as img:
        assert img.width == 1200


def test_never_overwrites_existing_file(roots):
    src = _art(roots / "cat.jpg")
    hers = roots / "cat-sheet.png"
    Image.new("RGB", (10, 10), (1, 2, 3)).save(hers)
    before = _digest(hers)
    res = make_sheet(str(src), nick="me", price=10)
    assert res["path"] == str(roots / "cat-sheet-2.png")
    assert _digest(hers) == before
    res2 = make_sheet(str(src), nick="me", price=10)
    assert res2["path"] == str(roots / "cat-sheet-3.png")


def test_refuses_files_outside_her_picture_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "image_roots", lambda: [tmp_path / "Downloads"])
    src = _art(tmp_path / "secret.jpg")
    with pytest.raises(SheetError, match="Загрузках"):
        make_sheet(str(src), nick="me", price=10)


def test_requires_nick(roots):
    src = _art(roots / "a.jpg")
    with pytest.raises(SheetError, match="ник"):
        make_sheet(str(src), nick="  ", price=10)


def test_unknown_layout_and_status(roots):
    src = _art(roots / "a.jpg")
    with pytest.raises(SheetError):
        make_sheet(str(src), nick="me", layout="diagonal")
    with pytest.raises(SheetError):
        make_sheet(str(src), nick="me", status="maybe")


def test_status_badge_changes_top_left(roots):
    src = _art(roots / "b.jpg")
    plain = make_sheet(str(src), nick="me", price=10, watermark=False)
    badged = make_sheet(str(src), nick="me", price=10, watermark=False, status="sold")
    with Image.open(plain["path"]) as a, Image.open(badged["path"]) as b:
        box = (0, 0, 250, 120)
        assert a.crop(box).tobytes() != b.crop(box).tobytes()
    assert "SOLD" in badged["summary"]


def test_watermark_marks_artwork(roots):
    src = _art(roots / "c.jpg")
    off = make_sheet(str(src), nick="mothmaker", price=10, watermark=False)
    on = make_sheet(str(src), nick="mothmaker", price=10, watermark=True)
    with Image.open(off["path"]) as a, Image.open(on["path"]) as b:
        box = (0, 0, 1000, 800)
        assert a.crop(box).tobytes() != b.crop(box).tobytes()
    assert "водяной знак" in on["summary"]


def test_cyrillic_title_needs_a_cyrillic_font(roots, monkeypatch):
    src = _art(roots / "d.jpg")
    if sheet.find_font():
        res = make_sheet(str(src), nick="me", price=10, title="Мотылёк")
        assert res["path"].endswith("-sheet.png")
    monkeypatch.setattr(sheet, "FONT_CANDIDATES", ())
    sheet.find_font.cache_clear()
    try:
        with pytest.raises(SheetError, match="латиниц"):
            make_sheet(str(src), nick="me", price=10, title="Мотылёк")
        res = make_sheet(str(src), nick="me", price=10, title="Moth")   # латиница — встроенным шрифтом
        assert res["path"]
    finally:
        sheet.find_font.cache_clear()


def test_currency_sign_becomes_code_when_font_lacks_it(roots, monkeypatch):
    src = _art(roots / "r.jpg")
    monkeypatch.setattr(sheet, "FONT_CANDIDATES", ())
    sheet.find_font.cache_clear()
    try:
        res = make_sheet(str(src), nick="me", price=1500, currency="rub")
        assert res["labels"] == ["Price 1,500 RUB"]
        res = make_sheet(str(src), nick="me", prices={"sb": 30}, currency="eur")
        assert res["labels"] == ["SB EUR 30"]
    finally:
        sheet.find_font.cache_clear()


def test_long_texts_fit_and_are_clipped(roots):
    src = _art(roots / "e.jpg", size=(400, 900))
    res = make_sheet(str(src), nick="a_really_long_nickname_for_test", title="Very long title " * 10,
                     prices={"sb": 1500, "mi": 250, "ab": 12000}, currency="points")
    with Image.open(res["path"]) as img:
        assert img.width == 400


def test_jpeg_output(roots):
    src = _art(roots / "f.jpg")
    res = make_sheet(str(src), nick="me", price=10, fmt="jpeg")
    assert res["path"].endswith("-sheet.jpg") and res["format"] == "JPEG"


def test_light_theme_differs(roots):
    src = _art(roots / "g.jpg")
    dark = make_sheet(str(src), nick="me", price=10, watermark=False)
    light = make_sheet(str(src), nick="me", price=10, watermark=False, theme="light")
    with Image.open(dark["path"]) as a, Image.open(light["path"]) as b:
        assert a.getpixel((5, a.height - 5)) != b.getpixel((5, b.height - 5))


def test_tiny_image_refused(roots):
    src = _art(roots / "tiny.jpg", size=(120, 90))
    with pytest.raises(SheetError, match="маленькая"):
        make_sheet(str(src), nick="me", price=10)
    assert not list(roots.glob("tiny-sheet*"))


# --- регрессии после проверки ---

def test_bool_is_not_a_price():
    with pytest.raises(SheetError):
        price_lines({"ab": True}, None, "usd")
    with pytest.raises(SheetError):
        price_lines(None, False, "usd")
    assert price_lines({"sb": 0}, None, "usd") == ["SB $0"]


def _noise(size):
    return Image.merge("RGB", [Image.effect_noise(size, s) for s in (100, 90, 80)])


def test_fit_x_limit_png_to_jpeg_then_quality_then_size():
    img = _noise((600, 600))
    png = len(sheet._encode(img, "PNG", None))
    q90 = len(sheet._encode(img, "JPEG", None, 90))
    q68 = len(sheet._encode(img, "JPEG", None, 68))
    out, data, fmt, notes = sheet.fit_x_limit(img, "PNG", None, limit=png + 1)
    assert (fmt, notes, out.size) == ("PNG", [], (600, 600))
    out, data, fmt, notes = sheet.fit_x_limit(img, "PNG", None, limit=(q90 + q68) // 2)
    assert fmt == "JPEG" and len(data) <= (q90 + q68) // 2 and out.size == (600, 600)
    assert any("JPEG" in n for n in notes) and any("качество" in n for n in notes)
    out, data, fmt, notes = sheet.fit_x_limit(img, "JPEG", None, limit=q68 // 2)
    assert len(data) <= q68 // 2 or any("всё равно" in n for n in notes)
    assert out.size != (600, 600) or any("всё равно" in n for n in notes)


def test_fit_x_limit_says_honestly_when_it_cannot():
    img = _noise((700, 700))
    out, data, fmt, notes = sheet.fit_x_limit(img, "PNG", None, limit=1000)
    assert len(data) > 1000
    assert any("всё равно" in n and "X такую не примет" in n for n in notes)
    assert max(out.size) >= sheet._SHRINK_MIN_SIDE * sheet._SHRINK


def test_big_noisy_sheet_fits_x_limit(roots):
    src = roots / "noise.png"
    _noise((1800, 1800)).save(src)
    res = make_sheet(str(src), nick="me", price=10, watermark=False, max_side=3000)
    assert res["bytes"] <= sheet.X_IMAGE_LIMIT or "всё равно" in res["summary"]


def test_reduce_in_strips_same_as_reduce(monkeypatch):
    im = _noise((1003, 777)).convert("RGBA")
    im.putalpha(Image.effect_noise((1003, 777), 100))
    monkeypatch.setattr(sheet, "_STRIP_BLOCKS", 7)          # много полос, последняя неполная
    assert sheet._reduce_in_strips(im, 3).tobytes() == im.reduce(3).tobytes()


def test_huge_rgba_png_reduced_before_conversion(roots, monkeypatch):
    src = roots / "huge.png"
    Image.new("RGBA", (4000, 3000), (10, 20, 30, 255)).save(src)
    seen = []
    real = sheet.media._to_working_mode
    monkeypatch.setattr(sheet.media, "_to_working_mode", lambda img: (seen.append(img.size), real(img))[1])
    res = make_sheet(str(src), nick="me", price=10, watermark=False, max_side=800)
    assert seen and max(seen[0]) <= 2000                     # смена режима — уже на уменьшенной копии
    assert res["width"] == 800


def test_exif_orientation_still_applied(roots):
    src = roots / "rot.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (1000, 600), (200, 50, 50)).save(src, "JPEG", exif=exif)
    res = make_sheet(str(src), nick="me", price=10, watermark=False, layout="side")
    with Image.open(res["path"]) as img:
        assert img.height == 1000                            # повернули: портрет 600×1000 + панель справа


def test_wide_and_tall_art_kept_readable(roots):
    for name, size in (("wide.png", (5000, 400)), ("tall.png", (300, 3000))):
        src = roots / name
        Image.new("RGB", size, (90, 160, 220)).save(src)
        res = make_sheet(str(src), nick="me", price=10, watermark=False)
        assert res["path"]
        assert any("вытянут" in w for w in res["warnings"])
    src = roots / "strip.png"
    Image.new("RGB", (10000, 210), (90, 160, 220)).save(src)
    with pytest.raises(SheetError, match="вытянутая \\(10000×210\\)"):
        make_sheet(str(src), nick="me", price=10)


def test_target_side():
    assert sheet.target_side(1000, 800, 1600) == 1000          # не увеличиваем
    assert sheet.target_side(5000, 400, 1600) == 2500          # короткая сторона — 200 px
    assert sheet.target_side(4000, 4000, 1600) == 1600
    with pytest.raises(SheetError, match="маленькая \\(150×900\\)"):
        sheet.target_side(150, 900, 1600)


needs_font = pytest.mark.skipif(not sheet.find_font(), reason="нет системного шрифта с кириллицей")


@needs_font
def test_accented_latin_watermark_uses_system_font(roots, monkeypatch):
    calls = []
    real = sheet._text_mark
    monkeypatch.setattr(sheet, "_text_mark", lambda t, w, o: (calls.append(t), real(t, w, o))[1])
    src = _art(roots / "z.jpg")
    make_sheet(str(src), nick="Zoe", price=10)
    assert calls == []                                      # A–Z — встроенным шрифтом media
    make_sheet(str(src), nick="Zoé", price=10)
    assert calls == ["Zoé"]


@needs_font
def test_logo_and_cyrillic_watermark_both_kept(roots, monkeypatch):
    stacks = []
    real = sheet._stack
    monkeypatch.setattr(sheet, "_stack", lambda parts, w, o: (stacks.append(len(parts)), real(parts, w, o))[1])
    logo = roots / "logo.png"
    Image.new("RGBA", (100, 100), (255, 0, 0, 255)).save(logo)
    src = _art(roots / "l.jpg")
    res = make_sheet(str(src), nick="me", price=10, watermark_text="Мотылёк", logo=str(logo))
    assert stacks == [2]
    assert "«Мотылёк» и логотип" in res["summary"]


def test_fit_clips_with_ellipsis_inside_width():
    text, font = sheet._fit("Very long title " * 10, 200, 40, 10)
    assert text.endswith("…") and sheet._width(font, text) <= 200


def test_long_texts_stay_inside_the_bar(roots):
    src = _art(roots / "e2.jpg", size=(400, 900))
    res = make_sheet(str(src), nick="a_really_long_nickname_for_test", title="Very long title " * 10,
                     prices={"sb": 1500, "mi": 250, "ab": 12000}, currency="points", watermark=False)
    bg = sheet.THEMES["dark"]["bg"]
    with Image.open(res["path"]) as img:
        rgb = img.convert("RGB")
        pad = max(12, round(400 * 0.03))
        for x in range(400 - pad + 2, 400):              # правый отступ полосы пуст: текст не вылез
            for y in range(900, rgb.height):
                assert rgb.getpixel((x, y)) == bg
