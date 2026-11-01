"""Что написано в посте лота: оплата, SB/MI/AB, срок окончания, что получает покупатель, TOS.

Читаем полное описание (items.extra["desc"], до 3000 символов), а если его нет — короткий фрагмент из ленты.
Только факты «есть / нет в тексте»: никаких оценок «хорошо/плохо» здесь нет — их делают advisor.listing_review
(по её посту) и advisor.listing_norms (доли по рынку).
"""

import json
import re

_F = re.I

PATTERNS: dict[str, re.Pattern] = {
    "tos": re.compile(r"\b(?:tos|t\.o\.s|terms(?:\s+of\s+service)?|rules)\b", _F),
    "pay_paypal": re.compile(r"\bpay\s?pal\b|\bpp\b(?=\s*(?:only|invoice|$|[,./]))", _F),
    "pay_kofi": re.compile(r"\bko-?\s?fi\b", _F),
    "pay_points": re.compile(r"\b(?:da\s*)?(?:points?|pts)\b|\d\s*(?:pts|points?)\b", _F),
    "pay_boosty": re.compile(r"\bboosty(?:\.to)?\b|бусти", _F),
    # «card issued outside Russia» — подсказка для Boosty, а не отдельный способ оплаты.
    "pay_other": re.compile(r"\b(?:stripe|cash\s?app|venmo|wise|vgen|(?<!foreign\s)card(?!s?\s+(?:issued|outside))|"
                            r"crypto|zelle|revolut|hipolink)\b", _F),
    # Как платить иностранной картой на Boosty: метод «card issued outside Russia», 3-D Secure (FAQ Boosty 12.2025).
    # «switch to USD» подсказкой не считаем: валюту на Boosty не выбирают, конвертация автоматическая.
    "foreign_pay_hint": re.compile(r"\b(?:issued\s+)?outside\s+(?:of\s+)?russia\b|\bforeign\s+cards?\b|"
                                   r"\bnon[\s-]?russian\s+cards?\b|\b3[\s-]?d[\s-]?secure\b|\b3ds\b", _F),
    # Срок оплаты: «pay within 48h», «payment due in 24 hours», «48h to pay», «оплата в течение 48 ч».
    "payment_window": re.compile(r"\bpay(?:ment)?\s+(?:with)?in\s+\d{1,3}\s*(?:h|hrs?|hours?|days?)\b|"
                                 r"\bpayment\s+(?:is\s+)?due\b|\b\d{1,3}\s*(?:h|hrs?|hours?|days?)\s+to\s+pay\b|"
                                 r"\bpay(?:ment)?\s+(?:deadline|window)\b|\bmust\s+(?:be\s+)?pa(?:y|id)\s+"
                                 r"(?:with)?in\b|оплат\w*\s+в\s+течение", _F),
    "sb": re.compile(r"\b(?:sb|starting\s+bid|start(?:ing)?\s+price)\b", _F),
    "mi": re.compile(r"\b(?:mi|mb|min(?:imum)?\.?\s*(?:bid|increase|increment|raise)?)\s*[:=\-–|]|"
                     r"\bmin(?:imum)?\.?\s+(?:bid|increase|increment)\b", _F),
    "ab": re.compile(r"\b(?:ab\d?|auto[\s-]?buy(?:out)?|buy[\s-]?out|insta[\s-]?buy)\b", _F),
    "end_time": re.compile(
        r"\bend(?:s|ing)?\s*(?::|-|–|—|\b(?:in|on|at|after|date|time)\b)|\bends?\s+\d|"
        r"\b\d{1,3}\s*(?:h|hrs?|hours?|days?)\s+(?:after|from)\b|\b\d{1,3}\s*[- ]?(?:h|hrs?|hours?|days?)\s+auction\b|"
        r"\bduration\b|\blasts?\s+(?:for\s+)?\d|\bdeadline\b|\btimer\b|\bclos(?:e|es|ing)\s+(?:in|on|at)\s+\d", _F),
    "timezone": re.compile(r"\b(?:gmt|utc)\s*[+\-−]?\s*\d{0,2}\b|\b(?:est|edt|pst|pdt|cst|cdt|mst|bst|cet|cest|"
                           r"msk|мск|jst|aest|wib|ict)\b", _F),
    "unwatermarked": re.compile(r"\b(?:non|no|un|without)[\s-]?(?:watermark(?:ed)?|wm)\b|"
                                r"\bwatermark[\s-]?free\b|\bwithout\s+(?:a\s+)?watermark", _F),
    "fullres": re.compile(r"\bpng\b|\bfull[\s-]?(?:res(?:olution)?|size|quality)\b|\bhigh[\s-]?res(?:olution)?\b|"
                          r"\bpsd\b|\boriginal\s+(?:image\s+)?file\b", _F),
    "rights": re.compile(r"\bcommercial(?:\s+(?:use|rights?|purposes?))?\b", _F),
    "hold": re.compile(r"\bholds?\b|\bpayment\s+plans?\b|\bon\s+hold\b", _F),
    "toyhouse": re.compile(r"\btoy\s?h(?:ou)?se\b|\btoyhou\.se\b|\bth\s+(?:transfer|link|profile)\b", _F),
}
FACT_KEYS = (*PATTERNS, "desc_len")


def listing_facts(text: str) -> dict[str, bool | int]:
    """{tos, pay_paypal, pay_kofi, pay_points, pay_boosty, pay_other, foreign_pay_hint, payment_window, sb, mi, ab,
    end_time, timezone, unwatermarked, fullres, rights, hold, toyhouse: bool, desc_len: int}."""
    text = text or ""
    out: dict[str, bool | int] = {k: bool(p.search(text)) for k, p in PATTERNS.items()}
    out["desc_len"] = len(text)
    return out


def pays_any(facts: dict) -> bool:
    return any(facts.get(k) for k in ("pay_paypal", "pay_kofi", "pay_points", "pay_boosty", "pay_other"))


def buyer_gets(facts: dict) -> bool:
    """Сказано ли, что получает покупатель (файл без водяного знака, png/полный размер, права, перенос на TH)."""
    return any(facts.get(k) for k in ("unwatermarked", "fullres", "rights", "toyhouse"))


def item_text(row) -> tuple[str, bool]:
    """(текст для проверки, полный ли он): extra["desc"], иначе фрагмент из ленты."""
    try:
        extra = json.loads(row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError, KeyError, IndexError):
        extra = {}
    desc = extra.get("desc") if isinstance(extra, dict) else None
    if isinstance(desc, str) and desc.strip():
        return desc, True
    try:
        snippet = row["snippet"] or ""
    except (KeyError, IndexError):
        snippet = ""
    return snippet, False


def row_facts(row) -> tuple[dict, bool]:
    text, full = item_text(row)
    return listing_facts(text), full
