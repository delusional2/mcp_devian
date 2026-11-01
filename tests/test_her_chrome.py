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
