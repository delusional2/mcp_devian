"""Чек-лист поста: оплата через Boosty, срок, TOS, что получает покупатель; факты с источниками."""

import re

import pytest

from mcp_devian import checklist
from mcp_devian.config import Posting

NOW = 1_800_000_000.0

FULL = ("Moth dragon AUCTION [OPEN]\n"
        "SB: $15 | MI: $2 | AB: $90\n"
        "Ends: 05.11 at 20:00 MSK (GMT+3)\n"
        "Payment: Boosty (https://boosty.to/mothart; choose the option for cards issued outside Russia, 3-D Secure)\n"
        "Please pay within 48h, otherwise the adopt goes to the next bidder.\n"
        "You get: full-size PNG without watermark + Toyhouse transfer.\n"
        "TOS: https://toyhou.se/mothart/tos")


def settings(**kw):
    p = Posting(boosty_url="https://boosty.to/mothart", payment_methods=["boosty", "points"],
                tos_url="https://toyhou.se/mothart/tos", th_username="mothart")
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_full_listing_passes():
    items = checklist.check(FULL, "auction", settings(), now=NOW)
    assert {i.key for i in items} >= {"payment", "foreign_hint", "payment_window", "tos", "deadline", "timezone",
                                      "sb_mi_ab", "buyer_gets", "toyhouse"}
    assert all(i.ok for i in items), [i for i in items if not i.ok]
    assert "Всё на месте" in checklist.render(items)


def test_missing_foreign_hint_and_timezone_flagged():
    text = FULL.replace("; choose the option for cards issued outside Russia, 3-D Secure", "").replace(" MSK (GMT+3)", "")
    items = {i.key: i for i in checklist.check(text, "auction", settings(), now=NOW)}
    assert items["payment"].ok
    assert not items["foreign_hint"].ok and "outside Russia" in items["foreign_hint"].fix
    assert not items["timezone"].ok and "MSK (GMT+3)" in items["timezone"].fix
    out = checklist.render(list(items.values()))
    assert "нет подсказки иностранцам" in out and "часового пояса" in out and "можно вставить" in out


def test_relative_end_needs_no_timezone():
    items = {i.key: i for i in checklist.check("Ends 48h after the last bid", "auction", settings(), now=NOW)}
    assert items["deadline"].ok and "timezone" not in items


def test_profile_link_counts_as_payment_line():
    items = {i.key: i for i in checklist.check("Payment info in my profile", "flatsale", settings())}
    assert items["payment"].ok


def test_raffle_has_no_payment_items():
    keys = {i.key for i in checklist.check("Free raffle! Ends 05.11 20:00 GMT. TOS in profile", "raffle", settings())}
    assert not keys & {"payment", "foreign_hint", "payment_window", "sb_mi_ab", "buyer_gets"}
    assert {"deadline", "timezone", "tos"} <= keys


def test_holds_item_only_with_her_policy():
    assert "holds" not in {i.key for i in checklist.check(FULL, "auction", settings())}
    items = {i.key: i for i in checklist.check(FULL, "auction", settings(payment_plans=True))}
    assert not items["holds"].ok and "after full payment" in items["holds"].fix
    items = {i.key: i for i in checklist.check(FULL, "auction", settings(holds_policy="Holds: 3 days max"))}
    assert items["holds"].fix == "Holds: 3 days max"


def test_payment_line():
    assert checklist.payment_line(settings()) == ("Payment: Boosty (https://boosty.to/mothart; choose the option for "
                                                  "cards issued outside Russia, 3-D Secure) or DA Points")
    assert "link in my profile" in checklist.payment_line(settings(boosty_url="", payment_methods=["boosty"]))
    assert checklist.payment_line(settings(boosty_url="", payment_methods=["points"])) == "Payment: DA Points"
    assert checklist.payment_line(settings(boosty_url="", payment_methods=[])) == ""


@pytest.mark.parametrize("lot_type", ["auction", "flatsale", "ota", "raffle", "adopt"])
@pytest.mark.parametrize("text", ["", "hi", FULL, "Payment via boosty"])
def test_never_suggests_kofi_paypal_stripe(lot_type, text):
    for s in (settings(), settings(boosty_url="", payment_methods=[]), None):
        items = checklist.check(text, lot_type, s)
        out = checklist.render(items) + "\n" + checklist.payment_line(s)
        for line in out.splitlines():
            if re.search(r"pay\s?pal|ko-?fi|stripe", line, re.I):
                assert re.search(r"isn't available|not available", line), line


def test_every_fact_has_source_and_date():
    for fact in (*checklist.BOOSTY_FACTS, *checklist.POINTS_FACTS):
        assert fact.source.startswith("https://") and re.search(r"\d{2}\.\d{4}", fact.date), fact
    text = checklist.facts_text()
    assert "11.7%" in text and "3,500" in text and "20,000" in text and "не выводятся" in text
    assert text.count("не проверено") == 3
    assert "ko-fi" not in text.lower() and "stripe" not in text.lower()


def test_tz_label():
    assert checklist.tz_label("Europe/Moscow", NOW) == "MSK (GMT+3)"
    assert checklist.tz_label("UTC", NOW) == "GMT"
    assert checklist.tz_label("Asia/Kolkata", NOW) == "GMT+5:30"
    assert checklist.tz_label("America/St_Johns", NOW) in ("GMT-3:30", "GMT-2:30")


def test_no_currency_switch_advice_anywhere():
    """FAQ Boosty (12.2025): конвертация автоматическая, валюту доната не выбрать — «USD/EUR» не советуем."""
    texts = [checklist.FOREIGN_HINT, checklist.payment_line(settings()), checklist.facts_text()]
    texts += [i.fix + " " + i.what for i in checklist.check("Payment via boosty", "auction", settings())]
    for text in texts:
        assert "USD/EUR" not in text and "USD or EUR" not in text and "switch" not in text.lower(), text
    assert "часто не проход" not in checklist.facts_text()


def test_holds_fix_has_no_invented_term():
    items = {i.key: i for i in checklist.check(FULL, "auction", settings(payment_plans=True))}
    assert "30 days" not in items["holds"].fix and "<N> days" in items["holds"].fix


def test_sb_snippet_currency_follows_her_methods():
    pts = {i.key: i for i in checklist.check("hi", "auction", settings(boosty_url="", payment_methods=["points"]))}
    assert "$" not in pts["sb_mi_ab"].fix and "pts" in pts["sb_mi_ab"].fix
    usd = {i.key: i for i in checklist.check("hi", "auction", settings())}
    assert usd["sb_mi_ab"].fix.startswith("SB: $__")
