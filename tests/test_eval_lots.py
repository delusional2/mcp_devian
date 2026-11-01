"""Точность распознавания на реальных лотах DeviantArt (размечено вручную, ноябрь 2026).

146 постов: 120 из папок адоптов da-adopts и 26 «популярных» работ, которые лотами не являются.
Пороги — нижние границы с запасом: падение ниже значит, что правка регулярок что-то сломала
на живых данных. В скобках — сколько было до правок W3 и сколько стало.
"""

import json
from pathlib import Path

from mcp_devian.classify import classify_lot
from mcp_devian.taxonomy import SPECIES, tag_text

LOTS = json.loads((Path(__file__).parent / "eval_lots.json").read_text(encoding="utf-8"))
NOT_LOTS = {"other", "ych", "commission"}


def _share(hits: int, total: int) -> float:
    return hits / total if total else 1.0


def _gold_species(r: dict) -> set[str]:
    # «+x» в разметке — вида не было в словаре на момент разметки. Если словарь его
    # теперь знает (zombie, rat, sheep…), это обычный правильный ответ.
    return {s.lstrip("+") for s in r.get("sp", []) if s.lstrip("+") in SPECIES}


def test_fixture_shape():
    assert len(LOTS) == 146
    assert all({"t", "x", "type", "status"} <= set(r) for r in LOTS)


def test_lot_type_accuracy():
    ok = sum(classify_lot(r["t"], r["x"]).lot_type == r["type"] for r in LOTS)
    assert _share(ok, len(LOTS)) >= 0.93  # было 0.705; стало 0.959


def test_status_accuracy():
    ok = sum(classify_lot(r["t"], r["x"]).status == r["status"] for r in LOTS)
    assert _share(ok, len(LOTS)) >= 0.97  # было 0.973; стало 0.993


def test_headline_usd_price():
    rows = [r for r in LOTS if r["type"] != "other"]
    ok = 0
    for r in rows:
        usd, want = classify_lot(r["t"], r["x"]).usd, r.get("usd")
        ok += (usd is None and want is None) or (usd is not None and want is not None and abs(usd - want) < 0.01)
    assert _share(ok, len(rows)) >= 0.95  # было 0.908; стало 0.983


def test_points_price():
    rows = [r for r in LOTS if "pts" in r]
    ok = sum(classify_lot(r["t"], r["x"]).points == r["pts"] for r in rows)
    assert _share(ok, len(rows)) >= 0.75  # было 3/14; стало 14/14 (тысячи через пробел)


def test_sb_flag_recall():
    rows = [r for r in LOTS if r.get("sb")]
    ok = sum(classify_lot(r["t"], r["x"]).is_sb for r in rows)
    assert _share(ok, len(rows)) >= 0.90  # стало 66/67


def test_multi_flag():
    """Пачки («OPEN 2/3», «$5 each», «1. $6 2. SOLD») — в статистике это не один лот."""
    pred = [(classify_lot(r["t"], r["x"]).multi, bool(r.get("multi"))) for r in LOTS]
    tp = sum(p and g for p, g in pred)
    fp = sum(p and not g for p, g in pred)
    fn = sum(g and not p for p, g in pred)
    assert _share(tp, tp + fp) >= 0.85  # стало 0.95
    assert _share(tp, tp + fn) >= 0.80  # стало 0.87


def _species_counts() -> tuple[int, int, int]:
    tp = fp = fn = 0
    for r in LOTS:
        if r["type"] in NOT_LOTS:
            continue
        gold, pred = _gold_species(r), set(tag_text(r["t"], (), r["x"]).species)
        tp, fp, fn = tp + len(gold & pred), fp + len(pred - gold), fn + len(gold - pred)
    return tp, fp, fn


def test_species_precision():
    tp, fp, _ = _species_counts()
    assert _share(tp, tp + fp) >= 0.92  # было 0.77 на 600 символах; стало 0.943


def test_species_recall():
    tp, _, fn = _species_counts()
    assert _share(tp, tp + fn) >= 0.80  # стало 0.862


def test_species_title_only_like_analytics():
    """Прогноз и ниши берут вид только из названия и тегов (forecast.analytics_tags, без описания) —
    порог на этом пути отдельный: проверка с описанием его не защищает."""
    tp = fp = fn = 0
    for r in LOTS:
        if r["type"] in NOT_LOTS:
            continue
        gold, pred = _gold_species(r), set(tag_text(r["t"], (), "").species)
        tp, fp, fn = tp + len(gold & pred), fp + len(pred - gold), fn + len(gold - pred)
    assert _share(tp, tp + fp) >= 0.92  # стало 0.956
    assert _share(tp, tp + fn) >= 0.70  # стало 0.741: без описания часть видов не видна — это честная цена


def test_chibi_style_is_not_an_add_on():
    """«+$30 chibi version» — это допуслуга, а не стиль адопта."""
    flagged = [r for r in LOTS if r["type"] not in NOT_LOTS and "chibi" in tag_text(r["t"], (), r["x"]).styles]
    assert flagged and all(r.get("chibi") for r in flagged)  # было 2 из 8 верны; стало 2 из 2
