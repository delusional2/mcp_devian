import json
import shutil
import subprocess

import pytest

from mcp_devian import her_chrome, th_reply
from mcp_devian import toyhouse as th

THREAD = "https://toyhou.se/~forums/13.adoption-center/900001.lf-dragons"
MOTH = {"name": "Moth Queen", "url": "https://toyhou.se/123456.moth-queen", "price": "$40",
        "tags": ["moth", "dragon"]}


# --- Отрисовка шаблона ---

def test_render_fills_slots_and_keeps_her_text():
    tpl = "Hi! I have {name} — {price}\n{url}\n\nNo worries if not a fit 💛"
    out = th_reply.render_reply(tpl, MOTH)
    assert out["text"] == "Hi! I have Moth Queen — $40\nhttps://toyhou.se/123456.moth-queen\n\nNo worries if not a fit 💛"
    assert out["html"] == ('<p>Hi! I have Moth Queen — $40<br><a href="https://toyhou.se/123456.moth-queen">'
                           'https://toyhou.se/123456.moth-queen</a></p><p>No worries if not a fit 💛</p>')
    # Маркер — её текст без ссылки (ссылка в посте разбивает строки иначе).
    assert out["marker"] == "Hi! I have Moth Queen — $40"
    assert "toyhou.se" not in out["marker"]


def own_tags_removed(markup):
    import re

    return re.sub(r'</?p>|<br>|<a href="https://toyhou\.se/[^"<>]*">|</a>', "", markup)


@pytest.mark.parametrize("tpl,character,bad", [
    ("Hi <script>alert(1)</script> {name}", MOTH, "<script"),
    ('<img src=x onerror="alert(1)"> {name}', MOTH, "<img"),
    ("{url}", {**MOTH, "url": "javascript:alert(1)"}, "<a href=\"javascript"),
    ("{name}", {**MOTH, "name": '<a href="x" onclick="steal()">x</a>'}, "<a href=\"x\""),
])
def test_render_never_lets_markup_or_scripts_through(tpl, character, bad):
    out = th_reply.render_reply(tpl, character)
    assert bad not in out["html"]
    # Чужая разметка остаётся только текстом (экранирована), живых тегов, кроме наших, нет.
    assert "<" not in own_tags_removed(out["html"])
    assert th_reply.html_to_text(out["html"]) == out["text"]


@pytest.mark.parametrize("tpl,note", [
    ("Hi! one = $15, two = $20\n{url}", ""),
    ("javascript: is not a link here, {name} onload=fine {price}", ""),
    ("Hey <3 {name} — {url}\n\nP.S. a<b & c>d", "  spaced   note\nsecond line "),
    ("{name}\t{price}  double  spaces\n\n\nthree blank lines {note}", "x"),
])
def test_posted_text_equals_preview(tpl, note):
    """Санитайзер не правит её текст: в пост уходит ровно то, что ей показали."""
    out = th_reply.render_reply(tpl, MOTH, note=note)
    assert th_reply.html_to_text(out["html"]) == out["text"]
    assert "one = $15" in out["text"] or "javascript:" in out["text"] or "<3" in out["text"] or "\t" in out["text"]


def test_render_refuses_instead_of_rewriting(monkeypatch):
    monkeypatch.setattr(th_reply, "_link", lambda url: f'<a href="{url}" onclick="x">{url}</a>')
    with pytest.raises(ValueError, match="разметка"):
        th_reply.render_reply("{url}", MOTH)


def squash(text):
    return "".join(text.split())


def test_marker_found_across_line_breaks():
    tpl = "Hello!\nI have {name}\nfor {price}\n{url}"
    out = th_reply.render_reply(tpl, MOTH)
    # На странице <br> превращается в «ничего» в textContent — сравниваем без пробелов с обеих сторон.
    dom_text = th_reply.html_to_text(out["html"]).replace("\n", "")
    assert squash(out["marker"]) in squash(dom_text)
    js = th_reply.verify_post_js(out["marker"], "Moth", MOTH["url"])
    assert json.dumps(squash(out["marker"])) in js and "replace(/\\s+/g, '')" in js


def test_marker_and_verification_differ_per_character():
    wolf = {**MOTH, "name": "Wolfie", "url": "https://toyhou.se/777.wolfie"}
    a = th_reply.render_reply("{url} Hi, {name} is {price}", MOTH)
    b = th_reply.render_reply("{url} Hi, {name} is {price}", wolf)
    assert a["marker"] != b["marker"]
    js_a = th_reply.verify_post_js(a["marker"], "Moth", MOTH["url"])
    assert '"/123456.moth-queen"' in js_a and "charPath" in js_a
    # Опасные символы в пути в селектор не попадают.
    assert 'charPath = ""' in th_reply.verify_post_js("x", "Moth", 'https://toyhou.se/1"]{}')


def test_verify_without_username_never_confirms():
    js = th_reply.verify_post_js("Hi there", "")
    assert "no_username" in js and 'user = ""' in js
    assert js.startswith("(function (skipIds)")
    assert "skip[pid]" in js  # посты, бывшие до отправки, не засчитываются


def test_non_toyhouse_url_is_not_a_link():
    out = th_reply.render_reply("see {url}", {**MOTH, "url": "https://evil.example/x"})
    assert "<a " not in out["html"] and "https://evil.example/x" in out["html"]


def test_unknown_and_missing_slots():
    with pytest.raises(ValueError, match=r"\{species\}"):
        th_reply.render_reply("Hi {species}", MOTH)
    with pytest.raises(ValueError, match="price"):
        th_reply.render_reply("{name} for {price}", {"name": "X", "url": "https://toyhou.se/1.x"})
    with pytest.raises(ValueError, match="пустой"):
        th_reply.render_reply("   ", MOTH)
    # {note} необязателен.
    assert th_reply.render_reply("{name}{note}", MOTH)["text"] == "Moth Queen"


def test_note_is_escaped_and_multiline():
    out = th_reply.render_reply("{name}\n{note}", MOTH, note="a < b\nsecond")
    assert "a &lt; b<br>second" in out["html"]


# --- Подбор персов и условия автора ---

def test_match_characters_by_tags_and_name():
    chars = [MOTH, {"name": "Wolfie", "url": "https://toyhou.se/2.w", "tags": ["wolf"]},
             {"name": "Cat", "tags": []}]
    found = th_reply.match_characters("LF: dragon OCs | O: $$", "moth dragons please", chars)
    assert [c["name"] for c, _ in found] == ["Moth Queen"]
    why = "; ".join(found[0][1])  # «вид: dragon» и уточнение из поста «совпало: moth»
    assert "вид: dragon" in why and "moth" in why
    assert th_reply.match_characters("LF wolves, no offers please", None, chars)[0][0]["name"] == "Wolfie"


@pytest.mark.parametrize("title,snippet", [
    ("LF wolves, no dragons", None),
    ("LF anything but dragons", None),
    ("LF OCs", "Not dragons please. Except dragons? no. Without moths"),
])
def test_match_characters_respects_negation(title, snippet):
    chars = [{"name": "Ember", "url": "https://toyhou.se/1.e", "tags": ["dragon"]},
             {"name": "Wolfie", "url": "https://toyhou.se/2.w", "tags": ["wolf"]}]
    names = [c["name"] for c, _ in th_reply.match_characters(title, snippet, chars)]
    assert "Ember" not in names


def test_match_characters_names_need_case_and_whole_word():
    chars = [{"name": "Sky", "url": "https://toyhou.se/3.s", "tags": []},
             {"name": "Moth Queen", "url": "https://toyhou.se/4.m", "tags": ["moth"]}]
    assert th_reply.match_characters("LF sky themed OCs", "skylines", chars) == []
    found = th_reply.match_characters("LF moths! saw Sky in your gallery", None, chars)
    assert [c["name"] for c, _ in found] == ["Moth Queen", "Sky"]  # совпадения по тегам впереди


@pytest.mark.parametrize("title,snippet,expected", [
    ("LF: dragon OCs | O: $$", None, ["ищут: dragon OCs"]),
    ("LF wolves, no offers please", None, ["ищут: wolves, no offers please", "просят без предложений (no offers)"]),
    ("[CLOSED] LF: cats", None, ["ищут: cats", "тред, похоже, закрыт"]),
    ("LF: humanoids", "Budget up to $30, USD only. No ferals, no anthros", None),
    ("Seeking customs", "my commissions are closed btw", ["ищут: customs"]),
    ("Closed species LF!", None, []),
    ("LF found family OCs", None, ["ищут: found family OCs"]),
    ("[FOUND] LF cats", None, ["ищут: cats", "тред, похоже, закрыт"]),
    ("LF dragons (closed)", None, ["ищут: dragons (closed)", "тред, похоже, закрыт"]),
    ("LF: dragons", "No pressure, no worries if not! no rush", ["ищут: dragons"]),
    ("LF: 123", None, []),
])
def test_op_restrictions(title, snippet, expected):
    got = th_reply.op_restrictions(title, snippet)
    if expected is None:
        assert "бюджет: до $30" in got and "платят только деньгами" in got
        assert "не хотят: ferals" in got and "не хотят: anthros" in got
    else:
        assert got == expected


def test_match_characters_keeps_shape_and_drops_blocked():
    chars = [{"name": "Ember", "url": "https://toyhou.se/1.e", "price": "$40", "tags": ["wolf"]},
             {"name": "Fang", "url": "https://toyhou.se/2.f", "price": "$15", "tags": ["wolf", "anthro"]},
             {"name": "Pip", "url": "https://toyhou.se/3.p", "price": "$10", "tags": ["fox"]}]
    found = th_reply.match_characters("LF: canines", "budget $20 max, no anthros", chars)
    assert isinstance(found, list) and all(isinstance(c, dict) and isinstance(w, list) for c, w in found)
    # Ember дороже бюджета, Fang — антро: их нет; Pip подходит.
    assert [c["name"] for c, _ in found] == ["Pip"]
    assert th_reply.match_characters("LF dragons [CLOSED]", None, [MOTH]) == []
    assert th_reply.match_characters("LF: dragons", None, []) == []


@pytest.mark.parametrize("snippet,line", [
    ("Budget: $15-$40", "бюджет: до $40"),
    ("Budget is $10 max", "бюджет: до $10"),
    ("No offers over $50", "бюджет: до $50"),
    ("budget 2k pts", "бюджет: до 2000 pts"),
    ("up to €20", "бюджет: до €20"),
])
def test_op_restrictions_budget_lines(snippet, line):
    got = th_reply.op_restrictions("LF: cats", snippet)
    assert line in got
    assert "просят без предложений (no offers)" not in got


@pytest.mark.parametrize("title,snippet,line", [
    ("LF wolves [ON HOLD]", None, "тред на паузе"),
    ("LF cats [paused]", None, "тред на паузе"),
    ("LF: Characters (Found, thank you!)", None, "тред, похоже, закрыт"),
    ("LF dragons", "paying via paypal invoice only", "платят только через PayPal"),
    ("LF dragons", "ko-fi only", "платят только через Ko-fi"),
    ("LF dragons", "cashapp only pls", "платят только через Cash App"),
    ("LF dragons", "NOT looking for anthro ones", "не хотят: anthro"),
    ("LF dragons", "no hybrids or customs", "не хотят: customs"),
])
def test_op_restrictions_new_lines(title, snippet, line):
    assert line in th_reply.op_restrictions(title, snippet)


def test_price_conflicts():
    cat = {"name": "Mochi", "price": "$35"}
    assert th_reply.price_conflicts(cat, "LF cats", "budget $20") == ["цена перса $35 выше бюджета треда $20"]
    assert th_reply.price_conflicts(cat, "LF cats", "budget $40") == []
    assert th_reply.price_conflicts(cat, "LF cats", "points only") == ["тред платит только поинтами, а цена в $"]
    pts = {"name": "Mochi", "price": "3000 pts"}
    assert th_reply.price_conflicts(pts, "LF cats", "USD only") == ["тред платит только деньгами, а цена в поинтах"]
    other = th_reply.price_conflicts(pts, "LF cats", "budget $20")
    assert len(other) == 1 and "другой валюте" in other[0]
    assert th_reply.price_conflicts({"name": "X"}, "LF cats", "budget $20") == []


def test_template_warnings():
    assert th_reply.template_warnings("Hi! {name} — {url} for {price}") == []
    warns = th_reply.template_warnings("Hi! I have {name}")
    assert any("{url}" in w for w in warns) and any("{price}" in w for w in warns)


@pytest.mark.parametrize("word,form", [("wolf", "wolves"), ("werewolf", "werewolves"), ("elf", "elves"),
                                       ("wolves", "wolf"), ("elves", "elf"), ("puppy", "puppies")])
def test_forms_irregular_plurals(word, form):
    assert form in th_reply._forms(word)


# --- Скрипты страницы ---

def test_fill_js_embeds_html_as_json_literal():
    tricky = '<p>"quoted" \\ </script><script>alert(1)</script></p>'
    js = th_reply.fill_reply_js(tricky, submit=True)
    assert json.dumps(tricky) in js and "doSubmit = true" in js
    assert "doSubmit = false" in th_reply.fill_reply_js("<p>x</p>", submit=False)
    assert "__mcpDevianSubmitted" in js


def test_inspect_js_never_reads_hidden_values():
    assert ".value" not in th_reply.INSPECT_REPLY_FORM_JS.split("hidden_inputs")[1].split(";")[0]


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node для проверки синтаксиса")
def test_all_scripts_are_valid_javascript(tmp_path):
    scripts = {
        "inspect.js": th_reply.INSPECT_REPLY_FORM_JS,
        "fill.js": th_reply.fill_reply_js("<p>Привет «мир» \"x\"</p>", True),
        "verify.js": "(" + th_reply.verify_post_js('Hi! "I" have', "Moth", MOTH["url"]) + ")([\"post-1\"]);",
        "reply_jxa.js": her_chrome.REPLY_JXA,
        "read_jxa.js": her_chrome.JXA,
    }
    for name, code in scripts.items():
        path = tmp_path / name
        path.write_text(code, encoding="utf-8")
        done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, check=False)
        assert done.returncode == 0, f"{name}: {done.stderr}"


# --- run_reply / inspect_reply_form на подставном osascript ---

@pytest.fixture(autouse=True)
def chrome_present(monkeypatch, tmp_path):
    app = tmp_path / "Google Chrome.app"
    app.mkdir()
    monkeypatch.setattr(her_chrome, "CHROME_PATHS", [app])
    monkeypatch.setattr(th, "LOCK_FILE", tmp_path / "chrome.lock")  # замок — не в её настоящей папке


def fake(answer=None, code=0, stderr=""):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, code, json.dumps(answer) if answer is not None else "", stderr)

    run.calls = calls
    return run


def reply(runner, submit=True):
    out = th_reply.render_reply("Hi! {name} {price} {url}", MOTH)
    return her_chrome.run_reply(THREAD, out["html"], submit, "MothArtist", out["marker"], runner=runner)


@pytest.mark.parametrize("answer,state,words", [
    ({"state": "posted", "permalink": THREAD + "#post-1"}, "posted", "опубликован"),
    ({"state": "submitted_unverified"}, "submitted_unverified", "второй раз не отправляю"),
    ({"state": "no_form"}, "no_form", "формы ответа"),
    ({"state": "closed"}, "closed", "закрыт"),
    ({"state": "not_logged_in"}, "not_logged_in", "не залогинена"),
    ({"state": "fill_failed", "reason": "x"}, "fill_failed", "Ничего не отправлено"),
    ({"state": "challenge"}, "challenge", "галочку"),
    ({"state": "timeout"}, "timeout", "Ничего не отправлено"),
])
def test_run_reply_states(answer, state, words):
    out = reply(fake(answer))
    assert out["state"] == state and words in out["message"]


def test_run_reply_passes_mode_and_scripts():
    run = fake({"state": "filled"})
    out = reply(run, submit=False)
    assert out["state"] == "filled" and "Post" in out["message"]
    cmd = run.calls[0]
    assert cmd[:3] == ["osascript", "-l", "JavaScript"] and cmd[5] == THREAD and cmd[6] == "fill"
    assert "doSubmit = false" in cmd[7] and "MothArtist".lower() in cmd[8]
    assert reply(fake({"state": "posted"}), submit=True) and fake  # submit режим
    run = fake({"state": "posted", "permalink": "p"})
    reply(run, submit=True)
    assert run.calls[0][6] == "submit" and "doSubmit = true" in run.calls[0][7]


def test_run_reply_setup_and_transport_errors():
    with pytest.raises(her_chrome.ChromeSetupError, match="Автоматизация"):
        reply(fake(code=1, stderr="execution error: Error: Произошла ошибка. (-1743)"))
    with pytest.raises(her_chrome.ChromeSetupError, match="событий Apple"):
        reply(fake({"state": "error", "error": "Error: AppleScript is turned off ?p=applescript"}))
    with pytest.raises(th.ToyhouseError, match="закрыли"):
        reply(fake({"state": "tab_closed"}))
    with pytest.raises(th.ToyhouseError, match="ссылка на тред"):
        her_chrome.run_reply("https://evil.example/", "<p>x</p>", True, "u", "x", runner=fake({}))

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    # После возможного клика таймаут — не ошибка, а «неизвестно, ушло ли».
    out = reply(hang)
    assert out["state"] == "uncertain" and "неизвестно" in out["message"]
    # Без отправки таймаут — обычная ошибка: точно ничего не ушло.
    with pytest.raises(th.ToyhouseError, match="ничего не отправлялось"):
        reply(hang, submit=False)
    # Не JSON после возможного клика — тоже «неизвестно».
    def garbage(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "garbage", "")

    assert her_chrome.run_reply(THREAD, "<p>x</p>", True, "u", "x", runner=garbage)["state"] == "uncertain"


def test_reply_timeout_covers_the_scripts_budget():
    seen = {}

    def run(cmd, **kw):
        seen["timeout"], seen["cmd"] = kw["timeout"], cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "posted"}), "")

    her_chrome.run_reply(THREAD, "<p>x</p>", True, "u", "x", runner=run, load_s=20)
    budget = her_chrome.reply_budget_s(20)
    assert seen["cmd"][9] == "20" and seen["cmd"][11] == str(budget)
    # Скрипт ждёт: страницы (каждая ≤ load_s, но всё вместе ≤ бюджета), пост после клика и его проверку.
    assert budget >= 20 * 2 + her_chrome.POST_WAIT_S + 15
    assert seen["timeout"] >= budget + her_chrome.LAUNCH_WAIT_S + 10


@pytest.mark.parametrize("answer,state,words", [
    ({"state": "uncertain", "error": "x"}, "uncertain", "неизвестно"),
    ({"state": "wrong_page"}, "wrong_page", "ничего не вставляла"),
    ({"state": "no_submit_button"}, "no_submit_button", "вручную"),
    ({"state": "no_form", "reason": "несколько похожих форм"}, "no_form", "формы ответа"),
])
def test_run_reply_new_states(answer, state, words):
    out = reply(fake(answer))
    assert out["state"] == state and words in out["message"]


def test_reply_jxa_guards():
    """Что держит скрипт отклика (сам JXA в тестах не запустить — проверяем, что защиты на месте)."""
    src = her_chrome.REPLY_JXA
    # Только этот тред на toyhou.se: номер треда сверяется до чтения, вставки и проверки.
    assert "tidOf(url)" in src and "onThread()" in src and "'wrong_page'" in src
    assert "location.origin === 'https://toyhou.se'" in src and "m[1] === \" + JSON.stringify(tid)" in src
    # «Next» — не из постов; после перехода ждём новую страницу, а не читаем старую.
    assert "closest('.forum-post, .user-content')" in src and "window.__mcpHop" in src
    # Бюджет и «неизвестно» после возможного клика.
    assert "hardDeadline" in src and "clicked = true" in src and "'uncertain'" in src
    # Её вкладка возвращается сразу после открытия новой (кроме fill).
    assert "mode !== 'fill' && beforeId !== null" in src
    # Проверке передаются посты, которые были до клика.
    assert "JSON.stringify(existing)" in src


def test_form_and_button_choice_is_strict():
    js = th_reply.fill_reply_js("<p>x</p>", True)
    assert "candidates.length === 1" in js and "a.pathname.indexOf('/~forums/') === 0" in js
    assert "toLowerCase() !== 'post'" in js
    assert "preview|cancel|report|delete|draft" in js and "preferred.length === 1" in js
    assert "form.querySelector('button[type=submit], input[type=submit]')" not in js  # не «первая попавшаяся»
    assert "existing: existing" in js
    # Вставленный текст должен совпасть с черновиком целиком.
    assert "text(got) !== text(html)" in js


def test_reply_waits_for_chrome_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(th, "LOCK_FILE", tmp_path / "chrome.lock")
    run = fake({"state": "posted"})
    with th.browser_lock(), pytest.raises(th.BrowserBusy):  # Chrome занят проверкой по расписанию
        her_chrome.run_reply(THREAD, "<p>x</p>", True, "u", "x", runner=run, lock_wait_s=0)
    assert run.calls == []
    assert her_chrome.run_reply(THREAD, "<p>x</p>", True, "u", "x", runner=run, lock_wait_s=0)["state"] == "posted"
    with th.browser_lock(), pytest.raises(th.BrowserBusy):
        her_chrome.inspect_reply_form(THREAD, runner=run, lock_wait_s=0)


def test_chrome_turn_retries_until_free(monkeypatch, tmp_path):
    monkeypatch.setattr(th, "LOCK_FILE", tmp_path / "chrome.lock")
    attempts = []
    real = her_chrome.browser_lock

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise th.BrowserBusy("busy")
        return real()

    monkeypatch.setattr(her_chrome, "browser_lock", flaky)
    with her_chrome.chrome_turn(wait_s=60, sleep=lambda s: None):
        pass
    assert len(attempts) == 3


def test_run_reply_passes_character_url_to_verification():
    run = fake({"state": "posted"})
    out = th_reply.render_reply("Hi {name} {url}", MOTH)
    her_chrome.run_reply(THREAD, out["html"], True, "Moth", out["marker"], runner=run)
    assert '"/123456.moth-queen"' in run.calls[0][8]
    run = fake({"state": "posted"})
    her_chrome.run_reply(THREAD, out["html"], True, "Moth", out["marker"], runner=run,
                         character_url="https://toyhou.se/999.other")
    assert '"/999.other"' in run.calls[0][8]


@pytest.mark.parametrize("url", ["https://toyhou.se/~forums/", "https://toyhou.se/~forums/13.x/abc",
                                 "https://toyhou.se.evil.example/~forums/13.x/1.y", "http://toyhou.se/~forums/13.x/1.y"])
def test_run_reply_rejects_non_thread_urls(url):
    run = fake({"state": "posted"})
    with pytest.raises(th.ToyhouseError, match="ссылка на тред"):
        her_chrome.run_reply(url, "<p>x</p>", True, "u", "x", runner=run)
    assert run.calls == []


def test_run_reply_without_chrome(monkeypatch, tmp_path):
    monkeypatch.setattr(her_chrome, "CHROME_PATHS", [tmp_path / "nope.app"])
    run = fake({"state": "posted"})
    with pytest.raises(her_chrome.ChromeSetupError):
        reply(run)
    assert run.calls == []


def test_inspect_reply_form():
    form = {"present": True, "editor": "froala", "hidden_inputs": ["_token"], "on_last_page": True}
    run = fake({"state": "inspected", "form": form})
    out = her_chrome.inspect_reply_form(THREAD, runner=run)
    assert out["form"]["editor"] == "froala" and run.calls[0][6] == "inspect"
    assert "INSPECT" not in run.calls[0][7] and "hidden_inputs" in run.calls[0][7]


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node для проверки")
def test_reply_jxa_page_scripts_stay_on_thread(tmp_path):
    """Строки, которые JXA выполняет на странице: синтаксис и то, что «Next» не уводит с треда."""
    src = her_chrome.REPLY_JXA.replace("function run(", "function run_(")
    check = tmp_path / "check.js"
    check.write_text(src + r"""
const pick = (name) => { const m = String(run_).match(new RegExp("const " + name + " = ([\\s\\S]*?);\\n")); return m[1]; };
const tid = "900001";
const cf = eval(pick('cf'));
for (const n of ['probe', 'pageTid', 'hasForm', 'nextHref']) { new Function("return (" + eval(pick(n)) + ");"); }
const tidOf = eval(pick('tidOf'));
if (tidOf("https://toyhou.se/~forums/13.adoption-center/900001.lf") !== "900001") throw new Error("tidOf");
if (tidOf("https://toyhou.se.evil/~forums/13.x/900001.lf") !== "") throw new Error("tidOf origin");
const pageTid = new Function("location", "return " + eval(pick('pageTid')));
if (pageTid({origin: "https://evil.example", pathname: "/~forums/13.x/900001.y"}) !== "") throw new Error("origin");
const next = new Function("document", "location", "return " + eval(pick('nextHref')));
const loc = {href: "https://toyhou.se/~forums/13.x/900001.y", origin: "https://toyhou.se"};
const link = (href, inPost) => ({getAttribute: () => href, closest: () => (inPost ? {} : null)});
const doc = (l) => ({querySelectorAll: () => [l]});
const cases = [["/~forums/13.x/900001.y?page=2", false, true], ["/~forums/13.x/900002.y?page=2", false, false],
               ["https://evil.example/~forums/13.x/900001.y", false, false], ["/~forums/13.x/900001.y?page=2", true, false]];
for (const [href, inPost, allowed] of cases) {
  const got = next(doc(link(href, inPost)), loc);
  if (!!got !== allowed) throw new Error("next " + href + " " + inPost + " -> " + got);
}
""", encoding="utf-8")
    done = subprocess.run(["node", str(check)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
