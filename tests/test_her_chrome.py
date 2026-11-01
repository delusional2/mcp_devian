import json
import subprocess
from contextlib import contextmanager

import pytest

from mcp_devian import db, her_chrome, monitor
from mcp_devian import toyhouse as th
from mcp_devian.config import Config, Toyhouse

NOW = 1_800_000_000.0

ROWS = [{"title": "LF: dragon OCs | O: $$", "href": "https://toyhou.se/~forums/13.adoption-center/900001.lf",
         "author": "buyer", "created": "2 days ago", "posts": "4 Posts", "last": "5 hours ago", "last_poster": "x"}]

RU_DISABLED = ("Error: Выполнение JavaScript через AppleScript отключено. Чтобы включить эту функцию, в строке меню "
               "выберите \"Вид > Разработчикам > Разрешить JavaScript из событий Apple\". Подробную информацию можно "
               "найти на этой странице: https://support.google.com/chrome/?p=applescript.")


@pytest.fixture(autouse=True)
def chrome_present(monkeypatch, tmp_path):
    app = tmp_path / "Google Chrome.app"
    app.mkdir()
    monkeypatch.setattr(her_chrome, "CHROME_PATHS", [app])


def fake_osascript(answers):
    """answers — список (returncode, stdout, stderr) по одному на вызов osascript."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        code, out, err = answers[min(len(calls), len(answers)) - 1]
        return subprocess.CompletedProcess(cmd, code, out, err)

    run.calls = calls
    return run


def ok(result):
    return (0, json.dumps({"state": "ready", "result": result}), "")


def state(name, **extra):
    return (0, json.dumps({"state": name, **extra}), "")


def test_board_and_first_post():
    run = fake_osascript([ok(json.dumps(ROWS)), ok("Looking for dragons!")])
    reader = her_chrome.HerChromeReader(runner=run)
    rows = reader.board(13, 1, NOW)
    assert rows[0].thread_id == "900001" and rows[0].activity_ts == NOW - 5 * 3600
    assert reader.first_post("https://toyhou.se/~forums/13.x/900001.lf") == "Looking for dragons!"
    cmd = run.calls[0]
    assert cmd[:3] == ["osascript", "-l", "JavaScript"] and cmd[5] == "https://toyhou.se/~forums/13.adoption-center"
    assert "tr.forum-thread-row" in cmd[6]  # скрипт передаётся аргументом, без экранирования


@pytest.mark.parametrize("stderr,hint", [
    ("execution error: Error: Произошла ошибка. (-1743)", "Автоматизация"),
    ("Executing JavaScript through AppleScript is turned off. Allow JavaScript from Apple Events", "событий Apple"),
    (RU_DISABLED, "событий Apple"),
    ("execution error: Error: Application can't be found. (-2700)", "Не нашла Google Chrome"),
])
def test_setup_errors_explain_what_to_do(stderr, hint):
    reader = her_chrome.HerChromeReader(runner=fake_osascript([(1, "", stderr)]))
    with pytest.raises(her_chrome.ChromeSetupError, match=hint):
        reader.board(13, 1, NOW)


def test_russian_disabled_error_from_inside_script():
    answer = state("error", error=RU_DISABLED)
    with pytest.raises(her_chrome.ChromeSetupError, match="событий Apple"):
        her_chrome.HerChromeReader(runner=fake_osascript([answer])).board(13, 1, NOW)


def test_jxa_breaks_on_translated_error_text():
    # Регулярка внутри JXA должна ловить и русский текст — по ссылке ?p=applescript.
    assert "p=applescript|AppleScript|Apple ?Events|событий Apple" in her_chrome.JXA


def test_setup_hint_names_real_russian_menu_item():
    assert "Разрешить JavaScript из событий Apple" in her_chrome.SETUP_HINT
    assert "Allow JavaScript from Apple Events" in her_chrome.SETUP_HINT


def test_not_installed_checked_without_apple_events(monkeypatch, tmp_path):
    monkeypatch.setattr(her_chrome, "CHROME_PATHS", [tmp_path / "нет.app"])
    run = fake_osascript([ok("[]")])
    assert not her_chrome.chrome_installed()
    with pytest.raises(her_chrome.ChromeSetupError, match="Не нашла Google Chrome"):
        her_chrome.HerChromeReader(runner=run).board(13, 1, NOW)
    assert run.calls == []  # osascript даже не запускали


def test_vanished_object_is_not_not_installed():
    err = her_chrome.classify_error("execution error: Error: Can't get object. (-1728)")
    assert not isinstance(err, her_chrome.ChromeSetupError) and "закрыли" in str(err)


def test_tab_closed_mid_wait():
    reader = her_chrome.HerChromeReader(runner=fake_osascript([state("closed")]))
    with pytest.raises(th.ToyhouseError, match="закрыли"):
        reader.board(13, 1, NOW)


def test_loaded_page_without_rows_is_empty_not_error():
    run = fake_osascript([state("loaded"), state("loaded")])
    reader = her_chrome.HerChromeReader(runner=run)
    assert reader.board(13, 3, NOW) == []
    assert reader.first_post("https://toyhou.se/~forums/13.x/1.gone") is None


def test_interactive_challenge_one_tab_at_a_time():
    asked = []
    run = fake_osascript([state("challenge"), ok(json.dumps(ROWS))])
    reader = her_chrome.HerChromeReader(interactive=True, on_challenge=lambda: asked.append(1), runner=run)
    assert reader.board(13, 1, NOW) and asked == [1]
    first, second = run.calls
    # Первый заход вкладку с проверкой закрывает (keep=0), второй ждёт галочку до 2 минут и вкладку держит.
    assert first[-2:] == ["0", "0"]
    assert second[-2:] == ["120", "1"]


def test_self_solving_challenge_needs_no_human():
    # JXA сам дождался, пока Cloudflare пропустит: Python видит только ready, никого не зовёт.
    asked = []
    run = fake_osascript([ok(json.dumps(ROWS))])
    reader = her_chrome.HerChromeReader(interactive=True, on_challenge=lambda: asked.append(1), runner=run)
    assert reader.board(13, 1, NOW) and asked == [] and len(run.calls) == 1
    assert "!waiting && challengeS > 0" in her_chrome.JXA  # без break при первом же виде проверки


def test_challenge_scheduled_leaves_tab_for_her():
    run = fake_osascript([state("challenge")])
    with pytest.raises(th.ChallengeError, match="вкладка открыта"):
        her_chrome.HerChromeReader(interactive=False, runner=run).board(13, 1, NOW)
    assert len(run.calls) == 1 and run.calls[0][-2:] == ["0", "1"]


def test_window_and_tabs_pinned_by_id():
    jxa = her_chrome.JXA
    assert "chrome.windows.byId(" in jxa and "modes.indexOf('normal')" in jxa  # не инкогнито, не по индексу
    assert "activate(beforeId)" in jxa and "win.activeTab.id()" in jxa       # возвращаем её вкладку по id
    assert "chrome.launch()" in jxa                                           # Chrome не запущен — запускаем


def test_osascript_timeout():
    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    with pytest.raises(th.ToyhouseError, match="не ответил"):
        her_chrome.HerChromeReader(runner=hang).board(13, 1, NOW)


def test_osascript_timeout_is_bounded():
    seen = {}

    def run(cmd, **kw):
        seen["timeout"] = kw["timeout"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "loaded"}), "")

    her_chrome.run_in_chrome("https://toyhou.se/~forums/13.adoption-center", "1", "x", load_s=30,
                             challenge_wait_s=120, runner=run)
    assert seen["timeout"] == her_chrome.LAUNCH_WAIT_S + 30 + 120 + her_chrome.ERROR_EXTRA_S


def test_check_toyhouse_chrome_mode_uses_her_chrome(tmp_path):
    run = fake_osascript([ok(json.dumps(ROWS)), ok("Looking for dragons, $40")])

    @contextmanager
    def session(interactive=False, on_challenge=None):
        yield her_chrome.HerChromeReader(interactive=interactive, runner=run)

    cfg = Config(toyhouse=Toyhouse(mode="chrome", boards=[13], pages_per_board=1))
    with db.session(tmp_path / "t.sqlite3") as conn:
        import mcp_devian.toyhouse as thmod
        orig = thmod.human_pause
        thmod.human_pause = lambda *a, **k: None
        try:
            res = monitor.check_toyhouse(conn, cfg, NOW, interactive=True, her_chrome_session=session)
        finally:
            thmod.human_pause = orig
        assert res.error is None and res.seen == 1
        item = db.get_item(conn, "th:900001")
        assert item["match_kind"] == "lf" and item["snippet"].startswith("Looking for dragons")


def test_check_toyhouse_chrome_mode_setup_error(tmp_path):
    run = fake_osascript([(1, "", "(-1743)")])

    @contextmanager
    def session(interactive=False, on_challenge=None):
        yield her_chrome.HerChromeReader(runner=run)

    cfg = Config(toyhouse=Toyhouse(mode="chrome", boards=[13], pages_per_board=1))
    with db.session(tmp_path / "t.sqlite3") as conn:
        res = monitor.check_toyhouse(conn, cfg, NOW, interactive=True, her_chrome_session=session)
    assert "Автоматизация" in res.error


# --- DeviantArt: комментарии в её Chrome ---

DA_URL = "https://www.deviantart.com/fuvuart/art/Adopt-3-OPEN-1404679584"

# Как их отдаёт скрипт со страницы: порядок как на экране (новые ветки сверху, ответы под родителем).
PAGE_ROWS = [
    {"comment_id": "30", "user": "carol", "posted": "2026-10-31T22:00:00.000Z", "depth": 0, "text": "$25"},
    {"comment_id": "31", "user": "Fuvuart", "posted": "2026-10-31T22:05:00.000Z", "depth": 1, "text": "ok!"},
    {"comment_id": "32", "user": "carol", "posted": "2026-10-31T22:06:00.000Z", "depth": 2, "text": "yay"},
    {"comment_id": "10", "user": "alice", "posted": "2026-10-31T20:00:00.000Z", "depth": 0, "text": "SB"},
    {"comment_id": "11", "user": "bob", "posted": "2026-10-31T21:00:00.000Z", "depth": 1, "text": "+5"},
    {"comment_id": "10", "user": "alice", "posted": "2026-10-31T20:00:00.000Z", "depth": 0, "text": "SB"},  # дубль
]


def test_deviantart_comments_parents_and_order():
    run = fake_osascript([ok(json.dumps(PAGE_ROWS))])
    out = her_chrome.run_deviantart_comments(DA_URL, max_load_more=7, runner=run)
    assert [c.comment_id for c in out] == ["10", "11", "30", "31", "32"]  # по времени, без дубля
    parents = {c.comment_id: c.parent_id for c in out}
    assert parents == {"10": None, "11": "10", "30": None, "31": "30", "32": "31"}
    assert [c.depth for c in out] == [0, 1, 0, 1, 2]
    cmd = run.calls[0]
    assert cmd[:3] == ["osascript", "-l", "JavaScript"]
    assert cmd[5:9] == [DA_URL, "1404679584", "30", "7"]
    assert "data-commentid" in cmd[4] and "Load more" not in cmd[5:]  # селекторы внутри скрипта, не в argv


def test_deviantart_comments_empty_page():
    assert her_chrome.run_deviantart_comments(DA_URL, runner=fake_osascript([ok("[]")])) == []


@pytest.mark.parametrize("url", ["https://toyhou.se/~forums/13.x/1.y", "https://www.deviantart.com/a/gallery/1",
                                 "https://evil.example/a/art/x-1", "https://www.deviantart.com/a/art/no-number"])
def test_deviantart_comments_rejects_other_urls(url):
    run = fake_osascript([ok("[]")])
    with pytest.raises(th.ToyhouseError):
        her_chrome.run_deviantart_comments(url, runner=run)
    assert run.calls == []


@pytest.mark.parametrize("answer,exc,text", [
    (state("challenge"), th.ChallengeError, "человек"),
    (state("closed"), th.ToyhouseError, "закрыли"),
    (state("timeout"), th.ToyhouseError, "не загрузилась"),
    ((0, json.dumps({"state": "error", "error": RU_DISABLED}), ""), her_chrome.ChromeSetupError, "событий Apple"),
    ((1, "", "execution error: Error: Произошла ошибка. (-1743)"), her_chrome.ChromeSetupError, "Автоматизация"),
    ((0, "не json", ""), th.ToyhouseError, "странное"),
])
def test_deviantart_comments_errors(answer, exc, text):
    with pytest.raises(exc, match=text):
        her_chrome.run_deviantart_comments(DA_URL, runner=fake_osascript([answer]))


def test_deviantart_comments_without_chrome(monkeypatch, tmp_path):
    monkeypatch.setattr(her_chrome, "CHROME_PATHS", [tmp_path / "нет.app"])
    run = fake_osascript([ok("[]")])
    with pytest.raises(her_chrome.ChromeSetupError, match="Не нашла Google Chrome"):
        her_chrome.run_deviantart_comments(DA_URL, runner=run)
    assert run.calls == []


def test_deviantart_comments_timeout_bounded():
    seen = {}

    def run(cmd, **kw):
        seen["timeout"] = kw["timeout"]
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    with pytest.raises(th.ToyhouseError, match="не ответил"):
        her_chrome.run_deviantart_comments(DA_URL, max_load_more=5, runner=run, load_s=20)
    assert seen["timeout"] == her_chrome.LAUNCH_WAIT_S + 20 + 5 * 7 + her_chrome.ERROR_EXTRA_S


def test_da_jxa_and_generated_js_are_valid(tmp_path):
    import shutil

    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    src = her_chrome.DA_COMMENTS_JXA.replace("function run(", "function run_(")
    check = tmp_path / "check.js"
    check.write_text(src + """
const pick = (name) => { const m = String(run_).match(new RegExp("const " + name + " = ([\\\\s\\\\S]*?);\\\\n")); return m[1]; };
const itemId = "1404679584";
const cf = eval(pick('cf'));
for (const n of ['probe', 'countJs', 'clickJs', 'extractJs']) { new Function("return (" + eval(pick(n)) + ");"); }
""", encoding="utf-8")
    done = subprocess.run([node, str(check)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
