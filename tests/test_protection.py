"""Защита: проверка покупателя перед передачей и поиск украденных дизайнов. Только фейки: без сети и Chrome."""

import json
import subprocess
from datetime import UTC, datetime
from urllib.parse import quote

import pytest

from mcp_devian import db, protection, sales
from mcp_devian.db import Item

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC).timestamp()
DAY = 86400
LUMI = "https://toyhou.se/12345.lumi"
LOT = "https://www.deviantart.com/moth-artist/art/Moth-Dragon-Adopt-OPEN-1234567890"
THUMB = "https://images-wixmp-ed30a86b8c4ca887773594c2.wixmp.com/f/abc/moth.jpg"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p.sqlite3")
    yield c
    c.close()


def sale(conn, buyer, status="done", amount=40.0, currency="usd", ts=NOW - 10 * DAY, character_url=None, title="Lot"):
    sid = sales.add_sale(conn, title=title, amount=amount, currency=currency, platform="deviantart",
                         format="auction", buyer=buyer, status=status, now=ts, character_url=character_url)
    return sid


def no_fetch(name):
    raise AssertionError("сеть трогать нельзя")


# --- Её чёрный список ---

def test_blacklist_add_normalizes_and_needs_reason(conn):
    with pytest.raises(protection.ProtectionError):
        protection.blacklist_add(conn, "Scammy_1", "", now=NOW)
    with pytest.raises(protection.ProtectionError):
        protection.blacklist_add(conn, "a b<script>", "причина", now=NOW)
    protection.blacklist_add(conn, "@Scammy_1", "оспорил платёж в 2025", now=NOW)
    rows = protection.blacklist(conn)
    assert [r["username"] for r in rows] == ["scammy_1"]
    assert rows[0]["display"] == "Scammy_1"
    assert protection.blacklist_remove(conn, "SCAMMY_1") is True
    assert protection.blacklist(conn) == []
    assert protection.blacklist_remove(conn, "scammy_1") is False


def test_blacklisted_buyer_is_stop(conn):
    protection.blacklist_add(conn, "Scammy_1", "оспорил платёж", now=NOW)
    res = protection.check_buyer(conn, "scammy_1", fetch=no_fetch, platform="toyhouse", now=NOW)
    assert res["severity"] == "stop"
    text = protection.render_check(res)
    assert "чёрном списке" in text and "оспорил платёж" in text
    assert "не гарантия" in text


# --- Её отметки и журнал продаж ---

def test_chargeback_mark_is_high_and_names_date(conn):
    protection.mark(conn, "buyer_x", "chargeback", note="через банк", now=NOW - 40 * DAY)
    res = protection.check_buyer(conn, "Buyer_X", fetch=no_fetch, platform="toyhouse", now=NOW)
    assert res["severity"] == "high"
    assert any("возврат через банк" in f and "22.09.2026" in f for f in res["findings"])


def test_mark_kinds_validated_and_sale_buyer_used(conn):
    with pytest.raises(protection.ProtectionError):
        protection.mark(conn, "x_1", "scam", now=NOW)
    sid = sale(conn, "Late_Payer", status="cancelled")
    mid = protection.mark(conn, "", "no_pay", sale_id=sid, now=NOW)
    assert mid
    res = protection.check_buyer(conn, "late_payer", fetch=no_fetch, platform="toyhouse", now=NOW)
    assert res["severity"] == "caution"
    assert protection.unmark(conn, mid) is True


def test_good_history_and_trust_transfer(conn):
    sale(conn, "Regular_1", ts=NOW - 100 * DAY)
    sale(conn, "@regular_1", ts=NOW - 50 * DAY)
    res = protection.check_buyer(conn, "regular_1", fetch=no_fetch, platform="toyhouse", now=NOW)
    assert res["severity"] == "ok"
    assert any("2" in g and "оплатил" in g for g in res["good"])
    # Перс отдан по доверию, оплаты нет — осторожно.
    sid = sale(conn, "regular_1", status="won", ts=NOW - 5 * DAY)
    sales.set_status(conn, sid, "transferred", now=NOW - 4 * DAY)
    res = protection.check_buyer(conn, "regular_1", fetch=no_fetch, platform="toyhouse", now=NOW)
    assert res["severity"] == "caution"
    assert any("без оплаты" in f for f in res["findings"])


def test_unknown_buyer_ok_but_honest(conn):
    res = protection.check_buyer(conn, "Newbie_9", fetch=no_fetch, platform="toyhouse", now=NOW)
    assert res["severity"] == "ok"
    text = protection.render_check(res)
    assert "раньше у тебя не покупал" in text
    assert "Не проверяла" in text and "не гарантия" in text
    assert "120" in text and "75–90" in text   # оговорка про оспаривание карты и Boosty
    assert "Toyhouse" in text   # аккаунт TH без API — честно не проверен


# --- Публичные списки: только ссылки, открывает и читает она ---

def test_manual_links_default_and_character_ownership(conn):
    links = protection.manual_check_links(conn, "Some_Buyer", character_url=LUMI + "/gallery")
    urls = [lk["url"] for lk in links]
    assert "https://toyhou.se/~forums/5867.service-reviews" in urls
    assert "https://toyhou.se/12345.lumi/ownership" in urls
    assert "https://www.deviantart.com/Some_Buyer" in urls
    assert "https://toyhou.se/Some_Buyer" in urls
    assert all(protection.safe_url(u) == u for u in urls)


def test_scam_lists_editable(conn):
    for bad in ("http://example.com/list", "javascript:alert(1)", "https://127.0.0.1/x", "https://localhost/x",
                "file:///etc/passwd", "https://user:pw@example.com/", "https://exa mple.com/"):
        with pytest.raises(protection.ProtectionError):
            protection.scam_list_add(conn, bad, "список", now=NOW)
    protection.scam_list_add(conn, "https://clicksupdatelog.carrd.co/", "её список", now=NOW)
    protection.scam_list_add(conn, "https://www.deviantart.com/search?q={user}", "поиск DA", now=NOW)
    links = protection.manual_check_links(conn, "a.b-c")
    assert any(lk["url"] == "https://www.deviantart.com/search?q=" + quote("a.b-c", safe="") for lk in links)
    # Встроенный убирается и не возвращается; свой — удаляется.
    assert protection.scam_list_remove(conn, "https://www.deviantart.com/artists-beware") is True
    assert protection.scam_list_remove(conn, "https://clicksupdatelog.carrd.co/") is True
    urls = [lk["url"] for lk in protection.manual_check_links(conn, "x_1")]
    assert "https://www.deviantart.com/artists-beware" not in urls
    assert "https://clicksupdatelog.carrd.co/" not in urls
    protection.ensure(conn)   # повторная настройка не возвращает убранный
    assert "https://www.deviantart.com/artists-beware" not in [
        lk["url"] for lk in protection.manual_check_links(conn, "x_1")]


# --- DeviantArt: возраст аккаунта, бережно и с кэшем ---

def profile_body(days_ago, user_type="regular", is_minor=None, deviations=5, comments=10):
    join = datetime.fromtimestamp(NOW - days_ago * DAY, UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")
    user = {"username": "Fresh_1", "type": user_type, "details": {"joindate": join}}
    if is_minor is not None:
        user["is_minor"] = is_minor
    return {"user": user, "stats": {"user_deviations": deviations, "user_comments": comments}}


def test_new_da_account_is_caution_and_cached(conn):
    calls = []

    def fetch(name):
        calls.append(name)
        return profile_body(10)

    res = protection.check_buyer(conn, "Fresh_1", fetch=fetch, platform="deviantart", now=NOW)
    assert res["severity"] == "caution"
    assert any("10 дн" in f for f in res["findings"])
    assert any("общая практика" in f for f in res["findings"])
    protection.check_buyer(conn, "fresh_1", fetch=fetch, platform="deviantart", now=NOW + DAY)
    assert calls == ["Fresh_1"]   # второй раз из кэша, без запроса


def test_old_da_account_counts_as_checked(conn):
    res = protection.check_buyer(conn, "Old_1", fetch=lambda n: profile_body(3000), platform="deviantart", now=NOW)
    assert res["severity"] == "ok"
    assert any("аккаунт DeviantArt" in c for c in res["checked"])


def test_banned_and_minor(conn):
    res = protection.check_buyer(conn, "B_1", fetch=lambda n: profile_body(3000, user_type="banned"),
                                 platform="deviantart", now=NOW)
    assert res["severity"] == "high"
    res = protection.check_buyer(conn, "M_1", fetch=lambda n: profile_body(3000, is_minor=True),
                                 platform="deviantart", now=NOW)
    assert res["severity"] == "caution"
    assert any("младше 18" in f for f in res["findings"])


def test_da_errors_become_not_checked(conn):
    from mcp_devian import da_api

    def not_connected(name):
        raise da_api.NotConnected("нет")

    def limited(name):
        raise da_api.RateLimited("429", 429, status=429)

    def missing(name):
        raise da_api.DAApiError("no user", "invalid_request", status=400)

    r1 = protection.check_buyer(conn, "u_1", fetch=not_connected, platform="deviantart", now=NOW)
    assert any("не подключён" in n for n in r1["not_checked"])
    r2 = protection.check_buyer(conn, "u_2", fetch=limited, platform="deviantart", now=NOW)
    assert any("притормозить" in n for n in r2["not_checked"])
    r3 = protection.check_buyer(conn, "u_3", fetch=missing, platform="deviantart", now=NOW)
    assert any("не нашёлся" in f for f in r3["findings"])
    r4 = protection.check_buyer(conn, "u_4", fetch=None, platform="deviantart", now=NOW)
    assert any("аккаунт DeviantArt не проверяла" in n and "не подключён" in n for n in r4["not_checked"])
    assert not any("аккаунт DeviantArt (" in c for c in r4["checked"])


def test_toyhouse_buyer_skips_da(conn):
    res = protection.check_buyer(conn, "th_only", fetch=no_fetch, platform="toyhouse", now=NOW)
    assert res["severity"] == "ok"


# --- Сумма: только с n и уверенностью ---

def test_amount_compare_refuses_below_threshold(conn):
    for i in range(3):
        sale(conn, f"b{i}", amount=40, ts=NOW - (i + 1) * DAY)
    res = protection.check_buyer(conn, "big_1", amount=400, currency="usd", fetch=no_fetch, platform="toyhouse",
                                 now=NOW)
    assert any("Вывода нет" in n for n in res["not_checked"])


def test_amount_compare_with_evidence(conn):
    for i in range(9):
        sale(conn, f"b{i}", amount=40 + i, ts=NOW - (i + 1) * DAY)
    res = protection.check_buyer(conn, "big_1", amount=400, currency="usd", fetch=no_fetch, platform="toyhouse",
                                 now=NOW)
    assert res["severity"] == "caution"
    hit = [f for f in res["findings"] if "обычной" in f]
    assert hit and "n=9" in hit[0] and "уверенность" in hit[0]


# --- Хуки для передачи перса и статусов продаж ---

def test_transfer_warnings_local_only(conn):
    assert protection.transfer_warnings(conn, "clean_1", now=NOW) == []
    protection.blacklist_add(conn, "bad_1", "не заплатил", now=NOW)
    protection.mark(conn, "cb_1", "chargeback", now=NOW)
    assert any("чёрном списке" in w for w in protection.transfer_warnings(conn, "@BAD_1", now=NOW))
    assert any("возврат через банк" in w for w in protection.transfer_warnings(conn, "cb_1", now=NOW))
    # Кэш DA: новый аккаунт виден и без сети — когда передаёт на DA.
    protection.check_buyer(conn, "fresh_1", fetch=lambda n: profile_body(5), platform="deviantart", now=NOW)
    assert any("5 дн" in w for w in protection.transfer_warnings(conn, "fresh_1", platform="deviantart", now=NOW))
    # На Toyhouse (по умолчанию) кэш DA не смотрим: тот же ник на DA может быть другим человеком.
    assert protection.transfer_warnings(conn, "fresh_1", now=NOW) == []


def test_split_status_and_on_sale_status(conn):
    assert protection.split_status("paid") == ("paid", None)
    assert protection.split_status("chargeback") == ("cancelled", "chargeback")
    assert protection.split_status("refunded") == ("cancelled", "refunded")
    assert protection.split_status("disputed") == (None, "disputed")
    sid = sale(conn, "Cb_Buyer", status="transferred")
    text = protection.on_sale_status(conn, sid, "chargeback", now=NOW)
    assert "75–90" in text and "Service Reviews" in text and "сама" in text
    assert protection.check_buyer(conn, "cb_buyer", fetch=no_fetch, platform="toyhouse", now=NOW)["severity"] == "high"
    assert protection.on_sale_status(conn, sid, "paid", now=NOW) is None
    with pytest.raises(protection.ProtectionError):
        protection.on_sale_status(conn, "nope", "chargeback", now=NOW)


# --- Её Chrome: только открыть вкладки, ничего не читать ---

def test_open_tabs_jxa_never_reads_pages():
    js = protection.OPEN_TABS_JXA
    assert "execute" not in js and "javascript" not in js.lower().replace("-l javascript", "")
    assert "^https:" in js


def test_open_tabs_argv_filters_and_caps():
    urls = ["https://yandex.ru/images/search?rpt=imageview&url=x", "http://evil.com/", "javascript:alert(1)",
            "https://localhost/x"] + [f"https://tineye.com/search?url={i}" for i in range(10)]
    argv = protection.open_tabs_argv(urls)
    assert argv[:4] == ["osascript", "-l", "JavaScript", "-e"]
    passed = argv[6:]
    assert len(passed) == protection.MAX_TABS
    assert all(u.startswith("https://") and "localhost" not in u for u in passed)


def test_open_tabs_with_fake_runner(monkeypatch):
    from mcp_devian import her_chrome

    monkeypatch.setattr(her_chrome, "chrome_installed", lambda: True)
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "opened", "opened": 2, "skipped": 0}), "")

    res = protection.open_tabs(["https://tineye.com/search?url=a", "https://yandex.ru/images/"], runner=runner,
                               lock_wait_s=0)
    assert res["opened"] == 2 and "Открыла 2" in res["message"]
    assert seen["cmd"][0] == "osascript"
    assert protection.open_tabs(["http://bad/"], runner=runner, lock_wait_s=0)["state"] == "nothing"


# --- Обратный поиск картинки ---

def test_reverse_search_links():
    url = "https://f2.toyhou.se/file/f2-toyhou-se/images/1_abc.png"
    links = protection.reverse_search_links(url)
    by = {lk["engine"]: lk["url"] for lk in links}
    enc = quote(url, safe="")
    assert by["Яндекс"] == f"https://yandex.ru/images/search?rpt=imageview&url={enc}"
    assert by["Google Lens"] == f"https://lens.google.com/uploadbyurl?url={enc}"
    assert by["TinEye"] == f"https://tineye.com/search?url={enc}"
    assert by["Bing"].startswith("https://www.bing.com/images/search?")
    for bad in ("http://x.com/a.png", "data:image/png;base64,AA", "file:///Users/a.png", "https://192.168.0.2/a.png"):
        with pytest.raises(protection.ProtectionError):
            protection.reverse_search_links(bad)


def test_theft_search_local_file_gives_upload_pages(conn):
    res = protection.theft_search(conn, "/Users/her/Desktop/moth.png", now=NOW)
    assert res["local"] is True
    assert all("/Users/her" not in lk["url"] for lk in res["links"])
    text = protection.render_theft_search(res)
    assert "перетащи" in text


def test_theft_search_from_her_lot_uses_thumb_and_dates(conn):
    db.upsert_item(conn, Item(id="da:1234567890", source="deviantart", title="Moth Dragon Adopt OPEN", url=LOT,
                              author="moth-artist", own=True, published_ts=NOW - 20 * DAY,
                              extra={"thumb": THUMB}), now=NOW - 19 * DAY)
    res = protection.theft_search(conn, LOT, now=NOW)
    assert res["image_url"] == THUMB
    assert any("12.10.2026" in f for f in res["facts"])
    assert any("маленькое превью" in n for n in res["notes"])
    assert len(res["open_urls"]) == 4
    text = protection.render_theft_search(res)
    assert "не читаю" in text


def test_theft_search_toyhouse_needs_image(conn):
    res = protection.theft_search(conn, LUMI, now=NOW)
    assert res["links"] == [] and any("адрес изображения" in n for n in res["notes"])
    res = protection.theft_search(conn, LUMI, image_url="https://f2.toyhou.se/file/x.png", now=NOW)
    assert len(res["links"]) == 4


def test_wixmp_token_note():
    res = protection.check_image_url(THUMB + "?token=abc")
    assert any("token" in n for n in res[1])


# --- Набор для жалобы: структура и факты, текст — её ---

def test_report_kit_deviantart(conn):
    db.upsert_item(conn, Item(id="da:1234567890", source="deviantart", title="Moth Dragon", url=LOT,
                              author="moth-artist", own=True, published_ts=NOW - 20 * DAY), now=NOW - 19 * DAY)
    kit = protection.report_kit(conn, LOT, "https://www.deviantart.com/thief/art/My-OC-999", now=NOW)
    assert kit["platform"] == "deviantart"
    text = protection.render_report_kit(kit)
    assert "violations@deviantart.com" in text
    assert "contact-us" in text
    assert "12.10.2026" in text
    assert "[" in text   # места для её слов
    assert "сама" in text   # заявление под присягой — её действие
    assert len(kit["checklist"]) >= 6


def test_report_kit_toyhouse_has_no_ready_text(conn):
    kit = protection.report_kit(conn, LUMI, "https://toyhou.se/999.copy", now=NOW)
    assert kit["platform"] == "toyhouse"
    assert kit["templates"] == {}
    text = protection.render_report_kit(kit)
    assert "4.8" in text and "своими словами" in text
    assert "HelpDesk" in text or "Report" in text


def test_report_kit_warns_when_copy_is_her_buyer(conn):
    sale(conn, "Thief", status="done")
    kit = protection.report_kit(conn, LOT, "https://www.deviantart.com/thief/art/Moth-1", now=NOW)
    assert any("покупал" in w for w in kit["warnings"])


def test_report_kit_rejects_bad_copy_url(conn):
    with pytest.raises(protection.ProtectionError):
        protection.report_kit(conn, LOT, "not a url", now=NOW)


# --- Регрессии по проверке ---

@pytest.mark.parametrize("buyer", [None, "Аня", "some name"])
def test_on_sale_status_without_latin_nick_still_records(conn, buyer):
    # A: продажа без ника DA/TH — статус уже «отменена», отметка всё равно сохраняется, ответ просит ник.
    sid = sale(conn, buyer)
    st, kind = protection.split_status("chargeback")
    sales.set_status(conn, sid, st, now=NOW)
    text = protection.on_sale_status(conn, sid, "chargeback", now=NOW)
    assert "Назови ник" in text and sid in text and "75–90" in text
    rows = conn.execute("SELECT * FROM buyer_marks WHERE sale_id = ?", (sid,)).fetchall()
    assert len(rows) == 1 and rows[0]["username"] == "" and rows[0]["kind"] == "chargeback"
    # Она называет ник — он дописывается в ту же отметку, без второй.
    mid = protection.mark(conn, "Real_Nick", "chargeback", sale_id=sid, now=NOW)
    assert mid == rows[0]["id"]
    assert conn.execute("SELECT count(*) FROM buyer_marks WHERE sale_id = ?", (sid,)).fetchone()[0] == 1
    assert protection.check_buyer(conn, "real_nick", platform="toyhouse", now=NOW)["severity"] == "high"


def test_refund_by_her_is_not_against_buyer(conn):
    # B: она вернула деньги сама — покупателю это не в минус.
    sid = sale(conn, "Nice_1")
    sales.set_status(conn, sid, "cancelled", now=NOW)
    text = protection.on_sale_status(conn, sid, "refunded", note="не успела нарисовать", now=NOW)
    assert "в минус не идёт" in text
    res = protection.check_buyer(conn, "nice_1", platform="toyhouse", now=NOW)
    assert res["severity"] == "ok"
    assert any("не успела" in h for h in res["history"])
    assert "Для сведения" in protection.render_check(res)
    assert protection.transfer_warnings(conn, "nice_1", now=NOW) == []


def banned_fetch(calls):
    def fetch(name):
        calls.append(name)
        return {"user": {"username": name, "type": "banned", "details": {}}, "stats": {}}
    return fetch


@pytest.mark.parametrize("plat", ["Toyhouse", "TH", " toyhou.se ", "тойхаус", "X", "twitter"])
def test_platform_aliases_skip_da(conn, plat):
    # C: «Toyhouse», «TH», X — DeviantArt по тому же нику не смотрим.
    calls = []
    res = protection.check_buyer(conn, "th_person", platform=plat, fetch=banned_fetch(calls), now=NOW)
    assert calls == [] and res["severity"] == "ok"


def test_unknown_platform_da_findings_capped_and_hedged(conn):
    # C: площадка неизвестна — забаненный DA-аккаунт с тем же ником лишь «осторожно» и с оговоркой.
    calls = []
    res = protection.check_buyer(conn, "same_nick", fetch=banned_fetch(calls), now=NOW)
    assert calls == ["same_nick"]
    assert res["severity"] == "caution"
    assert any(f.startswith("если это тот же человек на DeviantArt") for f in res["findings"])
    assert any("может быть чужим" in n for n in res["not_checked"])
    # Тот же кэш на передаче через Toyhouse — молчит; при явной DA — серьёзно.
    assert protection.transfer_warnings(conn, "same_nick", now=NOW) == []
    assert protection.check_buyer(conn, "same_nick", platform="DA", now=NOW)["severity"] == "high"


def test_mark_without_history_has_no_clean_history_line(conn):
    # D: отметка есть, продаж нет — не пишем «истории нет».
    protection.mark(conn, "cb_only", "chargeback", now=NOW)
    res = protection.check_buyer(conn, "cb_only", platform="toyhouse", now=NOW)
    assert res["severity"] == "high"
    assert not any("истории нет" in g for g in res["good"])


def test_report_kit_warns_when_original_was_sold(conn):
    # E: перс продан — жалоба на владельца/перепродажу была бы ложной.
    sale(conn, "Buyer_A", character_url=LUMI)
    kit = protection.report_kit(conn, LUMI, LUMI, now=NOW)
    assert any("тот же перс" in w for w in kit["warnings"])
    assert any("продан" in w and "Buyer_A" in w for w in kit["warnings"])
    kit = protection.report_kit(conn, LUMI, "https://www.deviantart.com/reseller_b/art/Lumi-55", now=NOW)
    assert any("продан" in w and "владельца" in w for w in kit["warnings"])
    assert "Внимание" in protection.render_report_kit(kit)


def test_report_kit_unsold_original_has_no_sold_warning(conn):
    kit = protection.report_kit(conn, LUMI, "https://www.deviantart.com/thief/art/Lumi-55", now=NOW)
    assert kit["warnings"] == []


def test_th_profile_regex_skips_character_urls(conn):
    # F: адрес перса — не профиль; профиль — да.
    assert protection._TH_PROFILE.match("https://toyhou.se/999.copy") is None
    assert protection._TH_PROFILE.match("https://toyhou.se/12345") is None
    assert protection._TH_PROFILE.match("https://toyhou.se/Thief_1").group(1) == "Thief_1"
    sale(conn, "Thief_1")
    kit = protection.report_kit(conn, LOT, "https://toyhou.se/Thief_1", now=NOW)
    assert any("покупал" in w for w in kit["warnings"])


def test_en_skeleton_heading_matches_platform(conn):
    # G: заголовок английского каркаса — по площадке копии.
    x = protection.report_kit(conn, LOT, "https://x.com/thief/status/1", now=NOW)["templates"]["en"]
    assert x.startswith("Fields for the X") and "DeviantArt" not in x.splitlines()[0]
    other = protection.report_kit(conn, LOT, "https://example.com/oc", now=NOW)["templates"]["en"]
    assert "DeviantArt" not in other.splitlines()[0]
    da = protection.report_kit(conn, LOT, "https://www.deviantart.com/t/art/x-1", now=NOW)["templates"]["en"]
    assert da.startswith("Fields for the DeviantArt DMCA form")


@pytest.mark.parametrize("ref", ["https://www.deviantart.com/moth-artist/art/1234567890", "https://toyhou.se/12345",
                                 "https://www.deviantart.com/moth-artist/gallery", "https://x.com/me/status/1"])
def test_theft_search_page_url_asks_for_image(conn, ref):
    # H: адрес страницы — не картинка; просим адрес изображения, ссылок поиска не строим.
    res = protection.theft_search(conn, ref, now=NOW)
    assert res["links"] == [] and res["open_urls"] == []
    assert any("адрес страницы" in n for n in res["notes"])
    with pytest.raises(protection.ProtectionError):
        protection.check_image_url(ref)


def test_report_wording_is_hedged():
    # I: непроверенное — с пометкой; про PDF у DA сказано.
    text = " ".join(protection.COMMON_STEPS + protection.PLATFORM_STEPS["deviantart"])
    assert "72 час" not in text and "только загрузки на DA" not in text
    assert "по анонсу" in text and "PDF" in text


def test_da_age_marks_minor():
    # J: возраст DA — в user.details.age.
    body = {"user": {"username": "kid_1", "type": "regular", "details": {"age": 15}}, "stats": {}}
    assert protection.parse_da_profile(body)["is_minor"] == 1
    body["user"]["details"]["age"] = 25
    assert protection.parse_da_profile(body)["is_minor"] == 0
    body["user"]["details"] = {}
    assert protection.parse_da_profile(body)["is_minor"] is None


@pytest.mark.parametrize("url", ["https://127.1/", "https://0x7f.1/", "https://127.0.0.1./", "https://localhost./",
                                 "https://localhost.localdomain/", "https://example.com./", "https://[::1]/",
                                 "https://2130706433/"])
def test_safe_url_rejects_local_tricks(url):
    # K: то же правило, что в OPEN_TABS_JXA.
    assert protection.safe_url(url) is None
    assert protection.open_tabs_argv([url])[6:] == []


def test_safe_url_accepts_real_hosts():
    for url in ("https://f2.toyhou.se/file/x.png", THUMB, "https://www.deviantart.com/a/art/b-1"):
        assert protection.safe_url(url) == url


def test_stale_da_cache_says_date(conn):
    # L: старше недели — с датой проверки.
    protection.check_buyer(conn, "old_1", fetch=lambda n: profile_body(3000), platform="deviantart", now=NOW)
    later = NOW + 30 * DAY
    res = protection.check_buyer(conn, "old_1", fetch=None, platform="deviantart", now=later)
    assert any("01.11.2026" in c and "старше недели" in c for c in res["checked"])
    protection.check_buyer(conn, "new_2", fetch=lambda n: profile_body(3), platform="deviantart", now=NOW)
    warns = protection.transfer_warnings(conn, "new_2", platform="deviantart", now=NOW + 8 * DAY)
    assert any("данные DA от 01.11.2026" in w for w in warns)


def test_cancelled_sale_with_mark_not_counted_again(conn):
    # M: отменённая продажа с её отметкой не просит отметить ещё раз.
    sid = sale(conn, "Cb_2")
    sales.set_status(conn, sid, "cancelled", now=NOW)
    protection.on_sale_status(conn, sid, "chargeback", now=NOW)
    res = protection.check_buyer(conn, "cb_2", platform="toyhouse", now=NOW)
    assert not any("отменённых" in f for f in res["findings"])
    sid2 = sale(conn, "Cb_2")
    sales.set_status(conn, sid2, "cancelled", now=NOW)
    res = protection.check_buyer(conn, "cb_2", platform="toyhouse", now=NOW)
    assert any("без твоей отметки: 1" in f for f in res["findings"])


def test_open_tabs_oserror_is_friendly(monkeypatch):
    # N: osascript не найден — понятная ошибка her_chrome, а не OSError.
    from mcp_devian import her_chrome

    monkeypatch.setattr(her_chrome, "chrome_installed", lambda: True)

    def runner(cmd, **kw):
        raise FileNotFoundError("osascript")

    with pytest.raises(her_chrome.ToyhouseError):
        protection.open_tabs(["https://tineye.com/"], runner=runner, lock_wait_s=0)


def test_amount_baseline_paid_only_and_excludes_current(conn):
    # O: обычная цена — только оплаченные продажи, без проверяемой.
    for i in range(7):
        sale(conn, f"b{i}", amount=40, ts=NOW - (i + 1) * DAY)
    for i in range(5):
        sale(conn, f"w{i}", status="won", amount=40, ts=NOW - (i + 1) * DAY)
    cur = sale(conn, "big_1", status="won", amount=400, ts=NOW)
    res = protection.check_buyer(conn, "big_1", sale_id=cur, platform="toyhouse", now=NOW)
    assert any("Вывода нет" in n and "7 из" in n for n in res["not_checked"])
    sale(conn, "b8", amount=40, ts=NOW - 9 * DAY)
    res = protection.check_buyer(conn, "big_1", sale_id=cur, platform="toyhouse", now=NOW)
    hit = [f for f in res["findings"] if "обычной" in f]
    assert hit and "n=8" in hit[0]
