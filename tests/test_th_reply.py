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
    assert out["marker"] == "Hi! I have Moth Queen — $40 No worries if"[:40].strip()
    assert "toyhou.se" not in out["marker"]


@pytest.mark.parametrize("tpl,character,bad", [
    ("Hi <script>alert(1)</script> {name}", MOTH, "<script"),
    ('<img src=x onerror="alert(1)"> {name}', MOTH, "<img"),
    ("{url}", {**MOTH, "url": "javascript:alert(1)"}, "javascript:"),
    ("{name}", {**MOTH, "name": '<a href="x" onclick="steal()">x</a>'}, "<a href=\"x\""),
])
def test_render_never_lets_markup_or_scripts_through(tpl, character, bad):
    out = th_reply.render_reply(tpl, character)
    assert bad not in out["html"]
    assert "<script" not in out["html"].lower() and "onerror=" not in out["html"] and "onclick=" not in out["html"]


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
    assert set(found[0][1]) == {"moth", "dragon"}
    assert th_reply.match_characters("LF wolves, no offers please", None, chars)[0][0]["name"] == "Wolfie"


@pytest.mark.parametrize("title,snippet,expected", [
    ("LF: dragon OCs | O: $$", None, ["ищут: dragon OCs"]),
    ("LF wolves, no offers please", None, ["ищут: wolves, no offers please", "просят без предложений (no offers)"]),
    ("[CLOSED] LF: cats", None, ["ищут: cats", "тред, похоже, закрыт"]),
    ("LF: humanoids", "Budget up to $30, USD only. No ferals, no anthros", None),
    ("Seeking customs", "my commissions are closed btw", ["ищут: customs"]),
])
def test_op_restrictions(title, snippet, expected):
    got = th_reply.op_restrictions(title, snippet)
    if expected is None:
        assert "бюджет: до $30" in got and "платят только деньгами" in got
        assert "не хотят: ferals" in got and "не хотят: anthros" in got
    else:
        assert got == expected


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
        "verify.js": th_reply.verify_post_js('Hi! "I" have', "Moth"),
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

    with pytest.raises(th.ToyhouseError, match="неизвестно"):
        reply(hang)


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
