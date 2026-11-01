"""Режим chrome: Toyhouse читается в её собственном Google Chrome через Apple Events.

Сервер открывает вкладку в её обычном (не инкогнито) окне Chrome, ждёт страницу,
выполняет скрипт-экстрактор (toyhouse.EXTRACT_*_JS), закрывает вкладку и
возвращает ту, что была открыта. Это её обычный браузер с её логином, поэтому
Cloudflare видит обычного посетителя; если он всё же спросит «я человек»,
галочку нажимает она — вкладка ждёт её.

Нужно один раз:
1. Chrome → «Вид» → «Разработчикам» → «Разрешить JavaScript из событий Apple».
2. Разрешить Claude управлять Google Chrome, когда macOS спросит.
"""

import json
import re
import subprocess
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .toyhouse import (
    EXTRACT_BOARD_JS,
    EXTRACT_POST_JS,
    BrowserBusy,
    ChallengeError,
    ThreadRow,
    ToyhouseError,
    board_id,
    board_url,
    browser_lock,
    rows_from_extracted,
)

SETUP_HINT = (
    "Один раз в Google Chrome: меню «Вид» → «Разработчикам» → «Разрешить JavaScript из событий Apple» "
    "(View → Developer → Allow JavaScript from Apple Events)."
)
PERMISSION_HINT = (
    "macOS не разрешила Claude управлять Google Chrome: Системные настройки → Конфиденциальность и безопасность → "
    "Автоматизация → Claude → включить Google Chrome."
)
NOT_INSTALLED_HINT = "Не нашла Google Chrome. Его нужно поставить с google.com/chrome или выбрать режим bot."
CLOSED_HINT = "Вкладку Toyhouse закрыли раньше, чем страница загрузилась. Можно просто попросить проверить ещё раз."

CHROME_PATHS = [Path("/Applications/Google Chrome.app"), Path.home() / "Applications" / "Google Chrome.app"]
LAUNCH_WAIT_S = 5
ERROR_EXTRA_S = 30
# Сколько ждать, пока Chrome освободит другая задача (проверка по расписанию, другой отклик).
LOCK_WAIT_S = 90

# JXA: аргументы приходят через argv, поэтому скрипт-экстрактор не надо экранировать.
# argv: url, js, ready-селектор, секунд на загрузку, секунд ждать галочку, оставить ли вкладку с проверкой.
JXA = r"""
function run(argv) {
  const url = argv[0], js = argv[1], ready = argv[2];
  const loadS = Number(argv[3]), challengeS = Number(argv[4]), keepOnChallenge = argv[5] === '1';
  const chrome = Application('Google Chrome');
  if (!chrome.running()) { chrome.launch(); }

  // Окно — первое обычное (не инкогнито). После запуска Chrome даём сессии восстановиться.
  let modes = [];
  for (let i = 0; i < 20; i++) {
    try { modes = chrome.windows.mode(); } catch (e) { modes = []; }
    if (modes.indexOf('normal') >= 0) break;
    delay(0.25);
  }
  if (modes.indexOf('normal') < 0) {
    chrome.Window({mode: 'normal'}).make();
    delay(0.5);
    modes = chrome.windows.mode();
  }
  const win = chrome.windows.byId(chrome.windows.id()[modes.indexOf('normal')]);
  let beforeId = null;
  try { beforeId = win.activeTab.id(); } catch (e) {}

  win.tabs.push(chrome.Tab({url: url}));
  const ids = win.tabs.id();
  const id = ids[ids.length - 1];

  // Вкладку ищем по id: она могла переехать в другое окно.
  const find = () => {
    try { const t = win.tabs.byId(id); t.id(); return t; } catch (e) {}
    for (const wid of chrome.windows.id()) {
      try { const t = chrome.windows.byId(wid).tabs.byId(id); t.id(); return t; } catch (e) {}
    }
    return null;
  };
  const activate = (tabId) => {
    try { const i = win.tabs.id().indexOf(tabId); if (i >= 0) win.activeTabIndex = i + 1; } catch (e) {}
  };

  const cf = "(window._cf_chl_opt || document.getElementById('challenge-error-text') || " +
    "document.getElementById('challenge-form'))";
  const probe = "document.querySelector(" + JSON.stringify(ready) + ") ? 'ready' : " + cf + " ? 'challenge' : " +
    "((document.readyState === 'complete' && document.querySelector('.navbar, nav, footer, #main')) ? 'loaded' : 'wait')";

  let state = 'wait', lastError = null, waiting = false;
  let deadline = Date.now() + loadS * 1000;
  try {
    while (Date.now() < deadline) {
      delay(1);
      const tab = find();
      if (!tab) { state = 'closed'; break; }
      try { state = tab.execute({javascript: probe}); lastError = null; }
      catch (e) {
        lastError = String(e);
        if (/p=applescript|AppleScript|Apple ?Events|событий Apple/i.test(lastError)) { state = 'error'; break; }
        if (e.errorNumber === -1728) { state = 'closed'; break; }
        continue;
      }
      if (state === 'ready') {
        return JSON.stringify({state: state, result: tab.execute({javascript: js})});
      }
      if (state === 'loaded') { return JSON.stringify({state: state}); }
      // Проверка Cloudflare часто проходит сама за пару секунд, поэтому просто ждём дальше.
      // Если нам разрешено ждать её — показываем вкладку и продлеваем срок.
      if (state === 'challenge' && !waiting && challengeS > 0) {
        waiting = true;
        activate(id);
        chrome.activate();
        deadline = Date.now() + challengeS * 1000;
      }
    }
    return JSON.stringify({state: state, error: lastError});
  } finally {
    const keep = keepOnChallenge && state === 'challenge';
    if (!keep && state !== 'closed') {
      try { const tab = find(); if (tab) tab.close(); } catch (e) {}
    }
    if (!keep && beforeId !== null) activate(beforeId);
  }
}
"""


class ChromeSetupError(ToyhouseError):
    """Chrome не даёт выполнять скрипты или его нет: нужна разовая настройка."""


def chrome_installed() -> bool:
    """Без Apple Events: просто смотрим, есть ли приложение."""
    return any(p.exists() for p in CHROME_PATHS)


def classify_error(text: str) -> ToyhouseError:
    low = text.lower()
    if "-1743" in text or "not authorized" in low or "не разрешено" in low:
        return ChromeSetupError(PERMISSION_HINT)
    # Текст ошибки переводится на язык Chrome, а ссылка ?p=applescript и слово AppleScript — нет.
    if ("p=applescript" in low or "applescript" in low or "apple events" in low or "appleevents" in low
            or "событий apple" in low or "apple-событ" in low):
        return ChromeSetupError(SETUP_HINT)
    if "-2700" in text or "can't be found" in low or "не удается найти" in low or "-10814" in text:
        return ChromeSetupError(NOT_INSTALLED_HINT)
    if "-1728" in text:
        return ToyhouseError(CLOSED_HINT)
    return ToyhouseError(f"Chrome: {text.strip()[:200]}")


def run_in_chrome(url: str, js: str, ready_selector: str, load_s: int = 30, challenge_wait_s: int = 0,
                  keep_on_challenge: bool = True, runner=None) -> str | None:
    """Открыть url во вкладке её Chrome, дождаться ready_selector, выполнить js.

    Возвращает результат js или None, если страница загрузилась, но нужного на ней нет
    (пустая доска, удалённый тред).
    """
    if not chrome_installed():
        raise ChromeSetupError(NOT_INSTALLED_HINT)
    timeout = LAUNCH_WAIT_S + load_s + challenge_wait_s + ERROR_EXTRA_S
    try:
        done = (runner or subprocess.run)(
            ["osascript", "-l", "JavaScript", "-e", JXA, url, js, ready_selector, str(load_s),
             str(challenge_wait_s), "1" if keep_on_challenge else "0"],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise ToyhouseError("Chrome не ответил вовремя (вкладка Toyhouse могла остаться открытой)") from e
    except OSError as e:
        raise ToyhouseError(f"не получилось запустить osascript: {e}") from e
    if done.returncode != 0:
        raise classify_error(done.stderr or done.stdout)
    try:
        answer = json.loads(done.stdout.strip() or "{}")
    except json.JSONDecodeError as e:
        raise ToyhouseError(f"Chrome вернул что-то странное: {done.stdout[:200]}") from e
    state = answer.get("state")
    if state == "ready":
        return answer.get("result") or ""
    if state == "loaded":
        return None
    if state == "closed":
        raise ToyhouseError(CLOSED_HINT)
    if answer.get("error"):
        raise classify_error(answer["error"])
    if state == "challenge":
        where = "вкладка открыта, нажми галочку" if keep_on_challenge else "нужно нажать галочку"
        raise ChallengeError(f"Toyhouse в Chrome просит подтвердить, что ты человек: {where}")
    raise ToyhouseError(f"Страница не загрузилась: {url}")


class HerChromeReader:
    """То же, что toyhouse.Reader, но читает в её Google Chrome."""

    def __init__(self, interactive: bool = False, on_challenge=None, challenge_wait_s: int = 120, runner=None):
        self.interactive = interactive
        self.on_challenge = on_challenge
        self.challenge_wait_s = challenge_wait_s
        self.runner = runner

    def _run(self, url: str, js: str, ready: str) -> str | None:
        wait = self.challenge_wait_s if self.interactive else 0
        if not wait:
            # По расписанию никого рядом нет: вкладку с проверкой оставляем ей.
            return run_in_chrome(url, js, ready, keep_on_challenge=True, runner=self.runner)
        try:
            # Первый заход без лишних вкладок: если проверка сама не прошла, вкладку закрываем...
            return run_in_chrome(url, js, ready, keep_on_challenge=False, runner=self.runner)
        except ChallengeError:
            # ...зовём её и открываем одну вкладку, которая ждёт галочку.
            if self.on_challenge:
                self.on_challenge()
            return run_in_chrome(url, js, ready, challenge_wait_s=wait, keep_on_challenge=True, runner=self.runner)

    def board(self, board, page: int, now: float) -> list[ThreadRow]:
        raw = self._run(board_url(board, page), EXTRACT_BOARD_JS, "tr.forum-thread-row")
        if raw is None:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ToyhouseError("скрипт вернул не JSON") from e
        return rows_from_extracted(items if isinstance(items, list) else [], board_id(board), now)

    def first_post(self, url: str) -> str | None:
        raw = self._run(url, EXTRACT_POST_JS, ".forum-post")
        return (raw or "").strip() or None


# --- Отклик в тред: один запуск osascript на всё ---
#
# argv: url треда, режим (inspect | fill | submit), скрипт формы, функция проверки поста,
# секунд на загрузку страницы, сколько раз можно перейти по «Next», общий бюджет в секундах.
# Правила, которые держит скрипт:
# - всё происходит только на https://toyhou.se в этом же треде (номер треда сверяется перед
#   каждым чтением и перед вставкой текста); чужая ссылка «Next» или переход в другой тред — wrong_page;
# - после перехода по «Next» ждём новую страницу (старая помечена window.__mcpHop), а не читаем старую;
# - кнопку отправки жмём не больше одного раза; если после возможного клика что-то сломалось —
#   uncertain («неизвестно, ушло ли»), а не ошибка;
# - вкладка открывается в фоне: её текущая вкладка остаётся на месте (кроме fill и проверки «я человек»);
# - весь запуск укладывается в бюджет: на клик идём, только если осталось время дождаться поста.
REPLY_JXA = r"""
function run(argv) {
  const url = argv[0], mode = argv[1], formJs = argv[2], verifyJs = argv[3];
  const loadS = Number(argv[4]), maxHops = Number(argv[5]), budgetS = Number(argv[6]);
  const hardDeadline = Date.now() + budgetS * 1000;
  const POST_WAIT_S = 30, VERIFY_RESERVE_S = 15;
  const tidOf = (u) => { const m = /^https:\/\/toyhou\.se\/~forums\/[^\/?#]+\/(\d+)\./.exec(u || ''); return m ? m[1] : ''; };
  const tid = tidOf(url);
  if (!tid) return JSON.stringify({state: 'wrong_page', error: 'not a toyhou.se thread'});
  const chrome = Application('Google Chrome');
  if (!chrome.running()) { chrome.launch(); }
  let modes = [];
  for (let i = 0; i < 20; i++) {
    try { modes = chrome.windows.mode(); } catch (e) { modes = []; }
    if (modes.indexOf('normal') >= 0) break;
    delay(0.25);
  }
  if (modes.indexOf('normal') < 0) {
    chrome.Window({mode: 'normal'}).make();
    delay(0.5);
    modes = chrome.windows.mode();
  }
  const win = chrome.windows.byId(chrome.windows.id()[modes.indexOf('normal')]);
  let beforeId = null;
  try { beforeId = win.activeTab.id(); } catch (e) {}
  const made = chrome.Tab({url: url});
  win.tabs.push(made);
  let id = null;
  try { id = made.id(); } catch (e) {}
  if (id === null) { const ids = win.tabs.id(); id = ids[ids.length - 1]; }
  const find = () => {
    try { const t = win.tabs.byId(id); t.id(); return t; } catch (e) {}
    for (const wid of chrome.windows.id()) {
      try { const t = chrome.windows.byId(wid).tabs.byId(id); t.id(); return t; } catch (e) {}
    }
    return null;
  };
  const activate = (tabId) => {
    try { const i = win.tabs.id().indexOf(tabId); if (i >= 0) win.activeTabIndex = i + 1; } catch (e) {}
  };
  // Новая вкладка становится активной — сразу возвращаем ей её вкладку (кроме fill: там форму покажем).
  if (mode !== 'fill' && beforeId !== null) activate(beforeId);
  const left = () => (hardDeadline - Date.now()) / 1000;
  const cf = "(window._cf_chl_opt || document.getElementById('challenge-error-text') || " +
    "document.getElementById('challenge-form'))";
  const probe = "window.__mcpHop ? 'wait' : document.querySelector('.forum-post') ? " +
    "(document.readyState === 'complete' ? 'ready' : 'wait') : " +
    cf + " ? 'challenge' : ((document.readyState === 'complete' && " +
    "document.querySelector('.navbar, nav, footer, #main')) ? 'loaded' : 'wait')";
  // Номер треда открытой страницы: только https://toyhou.se/~forums/<доска>/<номер>.<...>
  const pageTid = "(function () { var m = /^\\/~forums\\/[^\\/]+\\/(\\d+)\\./.exec(location.pathname); " +
    "return (location.origin === 'https://toyhou.se' && m) ? m[1] : ''; })()";
  const onThread = () => { const t = find(); return !!t && t.execute({javascript: pageTid}) === tid; };
  let lastError = null;
  // Ждать, пока страница во вкладке догрузится. Возвращает ready | loaded | challenge | tab_closed | error | timeout.
  const waitPage = () => {
    const deadline = Math.min(Date.now() + loadS * 1000, hardDeadline);
    while (Date.now() < deadline) {
      delay(1);
      const tab = find();
      if (!tab) return 'tab_closed';
      let st;
      try { st = tab.execute({javascript: probe}); lastError = null; }
      catch (e) {
        lastError = String(e);
        if (/p=applescript|AppleScript|Apple ?Events|событий Apple/i.test(lastError)) return 'error';
        if (e.errorNumber === -1728) return 'tab_closed';
        continue;
      }
      if (st === 'ready' || st === 'loaded') return st;
      if (st === 'challenge') { /* часто проходит само — ждём до конца срока */ }
    }
    try { const tab = find(); if (tab && tab.execute({javascript: cf + " ? 1 : 0"}) === 1) return 'challenge'; } catch (e) {}
    return 'timeout';
  };
  const run_js = (js) => JSON.parse(find().execute({javascript: js}));
  // Форма ответа на странице — та же проверка, что в скрипте формы: POST на toyhou.se в форум, с полем текста.
  const hasForm = "(Array.prototype.slice.call(document.querySelectorAll('form')).some(function (f) {" +
    " if (!f.querySelector('.fr-element, textarea')) return false;" +
    " if ((f.getAttribute('method') || 'get').toLowerCase() !== 'post') return false;" +
    " try { var a = new URL(f.getAttribute('action') || location.href, location.href);" +
    " return a.origin === 'https://toyhou.se' && a.pathname.indexOf('/~forums/') === 0; } catch (e) { return false; }" +
    " })) ? 1 : 0";
  // «Next» — только ссылка пагинации вне постов, на тот же тред на toyhou.se.
  const nextHref = "(function () { var links = Array.prototype.slice.call(document.querySelectorAll('.pagination a[rel=next]'))" +
    ".filter(function (a) { return !a.closest('.forum-post, .user-content'); });" +
    " if (links.length < 1) return ''; var u;" +
    " try { u = new URL(links[0].getAttribute('href'), location.href); } catch (e) { return ''; }" +
    " var m = /^\\/~forums\\/[^\\/]+\\/(\\d+)\\./.exec(u.pathname);" +
    " return (u.origin === 'https://toyhou.se' && m && m[1] === " + JSON.stringify(tid) + ") ? u.href : ''; })()";
  let result = {state: 'timeout'}, keep = false, clicked = false;
  const fail = (st) => {
    if (st === 'challenge') { keep = true; activate(id); chrome.activate(); }
    result = {state: st === 'loaded' ? 'no_form' : st, error: lastError};
    return JSON.stringify(result);
  };
  try {
    let st = waitPage();
    if (st !== 'ready') return fail(st);
    if (!onThread()) { result = {state: 'wrong_page'}; return JSON.stringify(result); }
    // Формы нет на этой странице — идём к последней по «Next» этого же треда.
    for (let hop = 0; hop < maxHops && find().execute({javascript: hasForm}) !== 1; hop++) {
      const next = find().execute({javascript: nextHref});
      if (!next || tidOf(next) !== tid) break;
      find().execute({javascript: "window.__mcpHop = 1; 1"});  // старая страница больше не «готова»
      find().url = next;
      st = waitPage();
      if (st !== 'ready') return fail(st);
      if (!onThread()) { result = {state: 'wrong_page'}; return JSON.stringify(result); }
    }
    if (mode === 'submit' && left() < POST_WAIT_S + VERIFY_RESERVE_S) {
      result = {state: 'timeout'}; return JSON.stringify(result);  // не успеем проверить — не жмём
    }
    if (!onThread()) { result = {state: 'wrong_page'}; return JSON.stringify(result); }
    if (mode === 'submit') clicked = true;  // с этого момента клик мог случиться
    result = run_js(formJs);
    if (mode === 'inspect') { result = {state: 'inspected', form: result}; return JSON.stringify(result); }
    if (result.state !== 'submitted') clicked = false;
    if (result.state === 'filled' || result.state === 'no_submit_button') {
      keep = true; activate(id); chrome.activate(); return JSON.stringify(result);  // текст в форме — покажем ей
    }
    if (result.state !== 'submitted') return JSON.stringify(result);
    // Отправили один раз. Ждём новую страницу (у старой стоит __mcpDevianSubmitted) и ищем пост. Повторно не жмём.
    keep = true;
    const existing = result.existing || [];
    const deadline = Math.min(Date.now() + POST_WAIT_S * 1000, hardDeadline - VERIFY_RESERVE_S * 1000);
    while (Date.now() < deadline) {
      delay(1);
      const tab = find();
      if (!tab) break;
      let fresh = false;
      try {
        fresh = tab.execute({javascript: "(!window.__mcpDevianSubmitted && document.readyState === 'complete') ? 1 : 0"}) === 1;
      } catch (e) {}
      if (fresh) break;
    }
    try {
      if (!onThread()) { result = {state: 'submitted_unverified', error: 'после отправки открылась другая страница'}; }
      else {
        const found = run_js("(" + verifyJs + ")(" + JSON.stringify(existing) + ")");
        if (found.found) { result = {state: 'posted', permalink: found.permalink}; keep = false; }
        else { result = {state: 'submitted_unverified', reason: found.reason || null}; }
      }
    } catch (e) { result = {state: 'submitted_unverified', error: String(e)}; }
    return JSON.stringify(result);
  } catch (e) {
    if (clicked) { keep = true; result = {state: 'uncertain', error: String(e)}; }
    else { result = {state: 'error', error: String(e)}; }
    return JSON.stringify(result);
  } finally {
    if (!keep && result.state !== 'tab_closed') {
      try { const tab = find(); if (tab) tab.close(); } catch (e) {}
      if (beforeId !== null) activate(beforeId);
    } else if (keep && ['filled', 'no_submit_button', 'challenge'].indexOf(result.state) >= 0) {
      activate(id);
    }
  }
}
"""

MAX_HOPS = 60
# Ожидание поста после клика (как POST_WAIT_S в скрипте) и запас на его проверку.
POST_WAIT_S = 30
REPLY_MESSAGES = {
    "posted": "Отклик опубликован.",
    "submitted_unverified": ("Отклик отправлен, но я не нашла его на странице. Вкладка открыта — "
                             "пусть она глянет; второй раз не отправляю."),
    "uncertain": ("Кнопку отправки, возможно, уже нажали, но дальше Chrome не ответил. Ушёл ли отклик — неизвестно: "
                  "посмотрите тред, прежде чем пробовать снова."),
    "filled": "Текст вставлен в форму ответа в её Chrome. Проверь и нажми «Post» сама.",
    "no_form": "На странице треда не нашлось формы ответа: возможно, тред закрыт или нужен вход.",
    "closed": "Тред закрыт для ответов.",
    "not_logged_in": "В её Chrome она не залогинена на Toyhouse: пусть войдёт и попросит ещё раз.",
    "fill_failed": "Не получилось вставить текст в форму ответа. Ничего не отправлено.",
    "no_submit_button": "Не нашла кнопку отправки. Текст в форме — можно отправить вручную.",
    "already_submitted": "На этой странице отклик уже отправлялся — второй раз не отправляю.",
    "wrong_page": "Вкладка ушла с этого треда Toyhouse — ничего не вставляла и не отправляла.",
    "challenge": "Toyhouse просит подтвердить, что она человек: вкладка открыта, нужно нажать галочку и попросить ещё раз.",
    "timeout": "Страница треда не загрузилась вовремя. Ничего не отправлено.",
    "inspected": "Форма ответа осмотрена.",
}
THREAD_URL = re.compile(r"^https://toyhou\.se/~forums/[^/?#]+/\d+\.")


def reply_budget_s(load_s: int) -> int:
    """Весь запуск: первая страница и несколько переходов к последней, ожидание поста и его проверка."""
    return load_s * 6 + POST_WAIT_S + 15


@contextmanager
def chrome_turn(wait_s: float = LOCK_WAIT_S, sleep=time.sleep):
    """Дождаться своей очереди к её Chrome (тот же замок, что у проверок Toyhouse). Не дождались — BrowserBusy."""
    give_up = time.monotonic() + wait_s
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(browser_lock())
                break
            except BrowserBusy:
                if time.monotonic() >= give_up:
                    raise
                sleep(1)
        yield


def _reply_call(thread_url: str, mode: str, form_js: str, verify_js: str, runner, load_s: int) -> dict:
    if not THREAD_URL.match(thread_url):
        raise ToyhouseError("Нужна ссылка на тред форума Toyhouse.")
    if not chrome_installed():
        raise ChromeSetupError(NOT_INSTALLED_HINT)
    budget = reply_budget_s(load_s)
    # Скрипт сам укладывается в бюджет; сверху — запуск Chrome и запас на медленный ответ.
    timeout = LAUNCH_WAIT_S + budget + ERROR_EXTRA_S
    try:
        done = (runner or subprocess.run)(
            ["osascript", "-l", "JavaScript", "-e", REPLY_JXA, thread_url, mode, form_js, verify_js,
             str(load_s), str(MAX_HOPS), str(budget)],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        if mode == "submit":
            # Клик мог случиться: это не «ошибка», а «неизвестно».
            return {"state": "uncertain", "error": "osascript timeout", "message": REPLY_MESSAGES["uncertain"]}
        raise ToyhouseError("Chrome не ответил вовремя — ничего не отправлялось (вкладка могла остаться открытой).") from e
    except OSError as e:
        raise ToyhouseError(f"не получилось запустить osascript: {e}") from e
    if done.returncode != 0:
        raise classify_error(done.stderr or done.stdout)
    try:
        answer = json.loads(done.stdout.strip() or "{}")
    except json.JSONDecodeError as e:
        if mode == "submit":
            return {"state": "uncertain", "error": done.stdout[:200], "message": REPLY_MESSAGES["uncertain"]}
        raise ToyhouseError(f"Chrome вернул что-то странное: {done.stdout[:200]}") from e
    state = answer.get("state")
    if state == "tab_closed":
        raise ToyhouseError(CLOSED_HINT)
    if state == "error":
        raise classify_error(answer.get("error") or "")
    answer["message"] = REPLY_MESSAGES.get(state, f"Неожиданный ответ: {state}")
    return answer


_TH_LINK = re.compile(r'href="(https://toyhou\.se/[^"]+)"')


def run_reply(thread_url: str, html: str, submit: bool, username: str, marker: str, runner=None,
              load_s: int = 30, character_url: str = "", lock_wait_s: float = LOCK_WAIT_S) -> dict:
    """Вставить её отклик в форму ответа треда в её Chrome и, если submit, отправить один раз.

    Возвращает {"state", "message", ...}: posted (+permalink), submitted_unverified,
    uncertain (клик мог случиться, исход неизвестен — повторять нельзя), filled, no_form (+reason),
    closed, not_logged_in, fill_failed, no_submit_button, already_submitted,
    wrong_page (вкладка ушла с треда, ничего не делали), challenge, timeout.
    Ошибки настройки Chrome бросаются как ChromeSetupError; занятый Chrome — BrowserBusy (до клика).
    character_url — ссылка на перса из черновика: пост засчитывается, только если она в нём есть
    (по умолчанию берётся первая ссылка на toyhou.se из html).
    """
    from .th_reply import fill_reply_js, verify_post_js

    if not character_url:
        m = _TH_LINK.search(html)
        character_url = m.group(1) if m else ""
    with chrome_turn(lock_wait_s):
        return _reply_call(thread_url, "submit" if submit else "fill", fill_reply_js(html, submit),
                           verify_post_js(marker, username, character_url), runner, load_s)


def inspect_reply_form(thread_url: str, runner=None, load_s: int = 30, lock_wait_s: float = LOCK_WAIT_S) -> dict:
    """Только посмотреть форму ответа (ничего не вводит и не отправляет), вкладку закрыть."""
    from .th_reply import INSPECT_REPLY_FORM_JS

    with chrome_turn(lock_wait_s):
        answer = _reply_call(thread_url, "inspect", INSPECT_REPLY_FORM_JS, "", runner, load_s)
    if answer.get("state") == "inspected" and isinstance(answer.get("form"), str):
        answer["form"] = json.loads(answer["form"])
    return answer


@contextmanager
def open_her_chrome(interactive: bool = False, on_challenge=None):
    """Сериализуем с ботом: одновременно читает Toyhouse только кто-то один (иначе BrowserBusy)."""
    with browser_lock():
        yield HerChromeReader(interactive=interactive, on_challenge=on_challenge)


# --- DeviantArt: комментарии (ставки) под работой ---
#
# Разметка DeviantArt (снято с публичной страницы 01.11.2026). Классы CSS там случайные,
# поэтому опираемся только на атрибуты:
# - у тела комментария есть data-commentid (и data-itemid = номер работы), текст — в [data-editor-viewer];
# - блок комментария — ближайший предок с data-indent="<глубина>";
# - автор — a[data-username] внутри блока, время — <time datetime="ISO">;
# - ещё комментарии подгружает кнопка «Load more» внутри #comments. Кликаем её, пока число комментариев растёт;
#   если кнопка осталась или комментариев меньше, чем в заголовке «Comments N», — список неполный.
# argv: url, номер работы, секунд на загрузку, сколько раз жать «Load more», сколько ждать галочку «я человек».
DA_COMMENTS_JXA = r"""
function run(argv) {
  const url = argv[0], itemId = argv[1], loadS = Number(argv[2]), maxMore = Number(argv[3]);
  const challengeS = Number(argv[4] || 0);
  const chrome = Application('Google Chrome');
  if (!chrome.running()) { chrome.launch(); }
  let modes = [];
  for (let i = 0; i < 20; i++) {
    try { modes = chrome.windows.mode(); } catch (e) { modes = []; }
    if (modes.indexOf('normal') >= 0) break;
    delay(0.25);
  }
  if (modes.indexOf('normal') < 0) {
    chrome.Window({mode: 'normal'}).make();
    delay(0.5);
    modes = chrome.windows.mode();
  }
  const win = chrome.windows.byId(chrome.windows.id()[modes.indexOf('normal')]);
  let beforeId = null;
  try { beforeId = win.activeTab.id(); } catch (e) {}
  win.tabs.push(chrome.Tab({url: url}));
  const ids = win.tabs.id();
  const id = ids[ids.length - 1];
  const find = () => {
    try { const t = win.tabs.byId(id); t.id(); return t; } catch (e) {}
    for (const wid of chrome.windows.id()) {
      try { const t = chrome.windows.byId(wid).tabs.byId(id); t.id(); return t; } catch (e) {}
    }
    return null;
  };
  const activate = (tabId) => {
    try { const i = win.tabs.id().indexOf(tabId); if (i >= 0) win.activeTabIndex = i + 1; } catch (e) {}
  };
  const cf = "(window._cf_chl_opt || document.getElementById('challenge-error-text') || " +
    "document.getElementById('challenge-form'))";
  const probe = "(document.readyState === 'complete' && document.getElementById('comments')) ? 'ready' : " +
    cf + " ? 'challenge' : 'wait'";
  const countJs = "document.querySelectorAll('[data-commentid][data-itemid=" + JSON.stringify(itemId) + "]').length";
  const moreBtn = "const re = /^(load more|show more|загрузить ещё|загрузить еще|показать ещё|показать еще)$/i;" +
    " const box = document.getElementById('comments');" +
    " const b = box ? [...box.querySelectorAll('button')].find(x => re.test(x.textContent.trim()) && !x.disabled) : null;";
  const clickJs = "(() => { " + moreBtn + " if (!b) return 'none'; b.click(); return 'clicked'; })()";
  const moreLeftJs = "(() => { " + moreBtn + " return b ? 1 : 0; })()";
  // Сколько комментариев обещает заголовок («Comments 123»); -1 — не нашли.
  const totalJs = "(() => { const box = document.getElementById('comments'); if (!box) return -1;" +
    " const scope = box.parentElement || box; const h = [...scope.querySelectorAll('h1, h2, h3, h4, span, div')]" +
    ".map(n => n.childElementCount < 3 ? (n.textContent || '').trim() : '')" +
    ".map(t => /^(?:comments|комментари[а-я]*)\\s*([\\d,.\\s]+)$/i.exec(t)).find(Boolean);" +
    " return h ? Number(h[1].replace(/[^\\d]/g, '')) : -1; })()";
  const extractJs = "JSON.stringify([...document.querySelectorAll('[data-commentid][data-itemid=" +
    JSON.stringify(itemId) + "]')].map(el => {" +
    " const box = el.closest('[data-indent]') || el;" +
    " const own = (sel) => [...box.querySelectorAll(sel)].find(n => (n.closest('[data-indent]') || box) === box);" +
    " const u = own('a[data-username]'); const t = own('time[datetime]');" +
    " const v = el.querySelector('[data-editor-viewer]');" +
    " return {comment_id: el.getAttribute('data-commentid'), user: u ? u.getAttribute('data-username') : ''," +
    " posted: t ? t.getAttribute('datetime') : '', depth: Number(box.getAttribute('data-indent') || 0)," +
    " text: (v ? v.innerText : el.innerText || '').trim()}; }))";

  let state = 'wait', lastError = null, waiting = false;
  let deadline = Date.now() + loadS * 1000;
  try {
    while (Date.now() < deadline) {
      delay(1);
      const tab = find();
      if (!tab) { state = 'closed'; break; }
      try { state = tab.execute({javascript: probe}); lastError = null; }
      catch (e) {
        lastError = String(e);
        if (/p=applescript|AppleScript|Apple ?Events|событий Apple/i.test(lastError)) { state = 'error'; break; }
        if (e.errorNumber === -1728) { state = 'closed'; break; }
        continue;
      }
      if (state === 'ready') break;
      // Проверка «я человек»: показываем ей вкладку и ждём галочку (если разрешено ждать).
      if (state === 'challenge' && !waiting && challengeS > 0) {
        waiting = true;
        activate(id);
        chrome.activate();
        deadline = Date.now() + challengeS * 1000;
      }
    }
    if (state !== 'ready') return JSON.stringify({state: state === 'wait' ? 'timeout' : state, error: lastError});
    delay(1.5);  // комментарии дорисовываются после загрузки страницы
    let clicks = 0, stalled = false;
    for (; clicks < maxMore; clicks++) {
      const tab = find();
      if (!tab) return JSON.stringify({state: 'closed'});
      const before = Number(tab.execute({javascript: countJs}) || 0);
      if (tab.execute({javascript: clickJs}) !== 'clicked') break;
      let grown = false;
      for (let w = 0; w < 12 && !grown; w++) {  // до 6 секунд на подгрузку
        delay(0.5);
        grown = Number(tab.execute({javascript: countJs}) || 0) > before;
      }
      if (!grown) { stalled = true; break; }
    }
    const tab = find();
    if (!tab) return JSON.stringify({state: 'closed'});
    let moreLeft = 0, total = -1;
    try { moreLeft = Number(tab.execute({javascript: moreLeftJs}) || 0); } catch (e) {}
    try { total = Number(tab.execute({javascript: totalJs})); } catch (e) {}
    return JSON.stringify({state: 'ready', clicks: clicks, stalled: stalled, more_left: moreLeft === 1,
                           total: isNaN(total) ? -1 : total, result: tab.execute({javascript: extractJs})});
  } finally {
    if (state !== 'challenge' && state !== 'closed') {
      try { const tab = find(); if (tab) tab.close(); } catch (e) {}
    }
    if (state === 'challenge') { activate(id); try { chrome.activate(); } catch (e) {} }
    else if (beforeId !== null) activate(beforeId);
  }
}
"""

DA_COMMENTS_HINT_CLOSED = "Вкладку DeviantArt закрыли раньше, чем комментарии загрузились. Можно попросить ещё раз."


class CommentsPage(list):
    """Список комментариев (обычный list) + честная пометка, что прочитано не всё.

    incomplete — True, если «Load more» осталась, перестала подгружать или комментариев меньше,
    чем в заголовке страницы; reason — почему (по-русски, для ответа ей).
    """

    def __init__(self, items=(), incomplete: bool = False, reason: str = "", total: int | None = None):
        super().__init__(items)
        self.incomplete = incomplete
        self.reason = reason
        self.total = total


def run_deviantart_comments(deviation_url: str, max_load_more: int = 20, runner=None, load_s: int = 30,
                            challenge_wait_s: int = 120, lock_wait_s: float = LOCK_WAIT_S) -> CommentsPage:
    """Комментарии под работой DeviantArt, прочитанные в её Chrome (если DA не подключён через API).

    Возвращает CommentsPage (это list[bids.Comment] по времени, старые первыми; parent_id — по вложенности)
    с .incomplete/.reason, если прочитаны не все комментарии. Если DeviantArt спросит «я человек»,
    вкладка выходит на передний план и ждёт галочку до challenge_wait_s секунд.
    """
    from .bids import Comment
    from .da_api import DEVIATION_URL

    m = DEVIATION_URL.match(deviation_url.strip())
    if not m:
        raise ToyhouseError("Нужна ссылка на работу DeviantArt вида https://www.deviantart.com/автор/art/название-12345.")
    if not chrome_installed():
        raise ChromeSetupError(NOT_INSTALLED_HINT)
    item_id = m.group(2)
    # Загрузка + ожидание галочки + до 7 с на каждую подгрузку + запас.
    timeout = LAUNCH_WAIT_S + load_s + challenge_wait_s + max_load_more * 7 + ERROR_EXTRA_S
    try:
        with chrome_turn(lock_wait_s):
            done = (runner or subprocess.run)(
                ["osascript", "-l", "JavaScript", "-e", DA_COMMENTS_JXA, deviation_url.strip(), item_id,
                 str(load_s), str(max_load_more), str(challenge_wait_s)],
                capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise ToyhouseError("Chrome не ответил вовремя (вкладка DeviantArt могла остаться открытой)") from e
    except OSError as e:
        raise ToyhouseError(f"не получилось запустить osascript: {e}") from e
    if done.returncode != 0:
        raise classify_error(done.stderr or done.stdout)
    try:
        answer = json.loads(done.stdout.strip() or "{}")
    except json.JSONDecodeError as e:
        raise ToyhouseError(f"Chrome вернул что-то странное: {done.stdout[:200]}") from e
    state = answer.get("state")
    if state == "closed":
        raise ToyhouseError(DA_COMMENTS_HINT_CLOSED)
    if state == "error" or (state != "ready" and answer.get("error")):
        raise classify_error(answer.get("error") or "")
    if state == "challenge":
        raise ChallengeError("DeviantArt в Chrome просит подтвердить, что ты человек: вкладка открыта, нажми галочку")
    if state != "ready":
        raise ToyhouseError(f"Страница работы не загрузилась: {deviation_url}")
    try:
        items = json.loads(answer.get("result") or "[]")
    except json.JSONDecodeError as e:
        raise ToyhouseError("скрипт вернул не JSON") from e
    comments = comments_from_page(items if isinstance(items, list) else [], Comment)
    return _with_completeness(comments, answer, max_load_more)


def _with_completeness(comments: list, answer: dict, max_load_more: int) -> CommentsPage:
    total = answer.get("total")
    total = total if isinstance(total, int) and total >= 0 else None
    reasons = []
    if answer.get("stalled"):
        reasons.append("кнопка «Load more» перестала подгружать комментарии")
    elif answer.get("more_left"):
        reasons.append(f"после {max_load_more} нажатий «Load more» под работой ещё есть комментарии")
    if total is not None and len(comments) < total:
        reasons.append(f"прочитано {len(comments)} из {total} комментариев")
    return CommentsPage(comments, incomplete=bool(reasons), reason="; ".join(reasons), total=total)


def comments_from_page(items: list, comment_cls) -> list:
    """Строки со страницы → Comment. Родитель — ближайший выше комментарий с меньшей глубиной."""
    from .da_api import _posted_ts

    out: list = []
    stack: list[tuple[int, str]] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict) or not it.get("comment_id") or it["comment_id"] in seen:
            continue
        seen.add(it["comment_id"])
        depth = max(0, int(it.get("depth") or 0))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if (stack and depth > 0) else None
        stack.append((depth, it["comment_id"]))
        out.append(comment_cls(comment_id=str(it["comment_id"]), parent_id=parent, user=str(it.get("user") or ""),
                               posted_ts=_posted_ts(it.get("posted")), text=str(it.get("text") or "").strip(),
                               depth=depth))
    return sorted(out, key=lambda c: (c.posted_ts, c.comment_id))
