"""Отклики в LF-треды Toyhouse: её шаблон, подбор персов, скрипты для формы ответа.

Текст отклика — всегда её собственный шаблон слово в слово (правило 4.8 Toyhouse
запрещает тексты, написанные ИИ). Здесь только подставляются поля и всё
экранируется, чтобы в пост не попал ни один скрипт.
"""

import html as html_lib
import json
import re

from . import th_match
from .th_match import CLOSED_TITLE, LF_PART  # noqa: F401 — CLOSED_TITLE нужен monitor и другим

SLOTS = ("name", "url", "price", "note")
SLOT_RE = re.compile(r"\{(\w*)\}")
TOYHOUSE_URL = re.compile(r"^https://toyhou\.se/[\w\-./~%?=&#]*$")
MARKER_LEN = 40


def _check_slots(template: str) -> list[str]:
    used = SLOT_RE.findall(template)
    unknown = sorted({s for s in used if s not in SLOTS})
    if unknown:
        allowed = " ".join("{" + s + "}" for s in SLOTS)
        raise ValueError(f"В шаблоне неизвестные поля: {', '.join('{' + u + '}' for u in unknown)}. Можно: {allowed}")
    return used


def _link(url: str) -> str:
    safe = html_lib.escape(url, quote=True)
    # Ссылкой делаем только адреса Toyhouse: всё остальное — просто текст.
    if TOYHOUSE_URL.match(url):
        return f'<a href="{safe}">{safe}</a>'
    return safe


_OWN_TAGS = re.compile(r'</?p>|<br>|<a href="https://toyhou\.se/[^"<>]*">|</a>')


def _check_markup(markup: str) -> str:
    """Подстраховка без правки текста: кроме наших <p>, <br> и ссылок на Toyhouse тегов быть не должно.

    Всё, что написала она, уже экранировано — поэтому здесь ничего не вырезаем (иначе отправленный
    текст разошёлся бы с тем, что она одобрила), а только проверяем и падаем, если что-то не так.
    """
    if "<" in _OWN_TAGS.sub("", markup):
        raise ValueError("В отклике оказалась разметка, которой там быть не должно — не отправляю.")
    return markup


def html_to_text(markup: str) -> str:
    """Текст, который увидит человек: обратное к render_reply (для проверки «отправлено = показано»)."""
    out = markup.replace("</p><p>", "\n\n").replace("<br>", "\n")
    return html_lib.unescape(re.sub(r"<[^>]+>", "", out))


def _render_line(line: str, values: dict[str, str]) -> tuple[str, str]:
    """(html, text) одной строки шаблона."""
    out_html, out_text, pos = [], [], 0
    for m in SLOT_RE.finditer(line):
        literal = line[pos:m.start()]
        out_html.append(html_lib.escape(literal))
        out_text.append(literal)
        slot, value = m.group(1), values[m.group(1)]
        out_html.append(_link(value) if slot == "url" else html_lib.escape(value).replace("\n", "<br>"))
        out_text.append(value)
        pos = m.end()
    out_html.append(html_lib.escape(line[pos:]))
    out_text.append(line[pos:])
    return "".join(out_html), "".join(out_text)


def render_reply(template: str, character: dict, note: str = "") -> dict:
    """Её шаблон + поля перса → {"html", "text", "marker"}.

    marker — кусок текста без ссылки, по которому потом проверяем, что пост появился.
    """
    if not (template or "").strip():
        raise ValueError("Шаблон пустой: попроси её прислать текст отклика своими словами.")
    used = _check_slots(template)
    values = {
        "name": str(character.get("name") or "").strip(),
        "url": str(character.get("url") or "").strip(),
        "price": str(character.get("price") or "").strip(),
        "note": (note or "").strip(),
    }
    missing = [s for s in dict.fromkeys(used) if s != "note" and not values[s]]
    if missing:
        raise ValueError(f"Для шаблона не хватает: {', '.join(missing)} — заполни это у перса.")

    paragraphs_html, lines_text = [], []
    for para in re.split(r"\n\s*\n", template.strip()):
        rendered = [_render_line(line, values) for line in para.split("\n")]
        paragraphs_html.append("<p>" + "<br>".join(h for h, _ in rendered) + "</p>")
        lines_text.append("\n".join(t for _, t in rendered))
    text = "\n\n".join(lines_text)
    return {"html": _check_markup("".join(paragraphs_html)), "text": text, "marker": _marker(text, values["url"])}


def _marker(text: str, url: str) -> str:
    """Кусок её текста без ссылки для проверки поста: самый длинный отрезок между ссылками.

    Ссылка в посте — тоже текст, поэтому маркер не должен её «перешагивать»."""
    pieces = [" ".join(p.split()) for p in (text.split(url) if url else [text])]
    best = max(pieces, key=len) if pieces else ""
    return best[:MARKER_LEN].strip()


_IRREGULAR = {"wolf": "wolves", "werewolf": "werewolves", "elf": "elves", "calf": "calves", "half": "halves",
              "wolves": "wolf", "werewolves": "werewolf", "elves": "elf"}


def _forms(term: str) -> list[str]:
    """Слово и его неправильное множественное (и обратно): wolf ↔ wolves, elf ↔ elves, puppy → puppies."""
    forms = [term]
    low = term.lower()
    if low in _IRREGULAR:
        forms.append(_IRREGULAR[low])
    elif low.endswith("fe"):
        forms.append(term[:-2] + "ves")
    elif low.endswith("f"):
        forms.append(term[:-1] + "ves")
    elif low.endswith("y") and len(low) > 2 and low[-2] not in "aeiou":
        forms.append(term[:-1] + "ies")
    elif low.endswith("ves") and len(low) > 4:
        forms += [term[:-3] + "f", term[:-3] + "fe"]
    elif low.endswith("ies") and len(low) > 4:
        forms.append(term[:-3] + "y")
    return forms


def match_characters(title: str, snippet: str | None, characters: list[dict]) -> list[tuple[dict, list[str]]]:
    """Какие её персы подходят к треду (лучшие первыми) и почему.

    Подбор — th_match.character_fit: вид с учётом синонимов и общих слов (canines → волк, лиса),
    тип тела, «не предлагать» автора, бюджет в той же валюте, «платят только …», закрытый тред.
    Перс с любой помехой сюда не попадает. Имя перса засчитывается, только если написано с той же
    большой буквы (иначе «Sky» совпало бы с «sky themed»)."""
    chars = [c for c in characters or [] if isinstance(c, dict)]
    if not chars:
        return []
    prof = th_match.lf_profile(title or "", snippet)
    accepts = th_match.accepts_of(chars)
    found = []
    for ch in chars:
        fit = th_match.character_fit(prof, ch, accepts, title or "", snippet)
        if not fit.blockers and fit.score > 0:
            found.append((ch, fit))
    found.sort(key=lambda cf: cf[1].score, reverse=True)
    return [(ch, fit.reasons) for ch, fit in found]


_NO_OFFERS = re.compile(r"\bno\s+offers?\b(?!\s+(?:over|above|higher|more|under|below))"
                        r"|\bnot\s+(?:taking|accepting)\s+offers?\b|\boffers?\s+closed\b", re.I)
_ONLY_LABELS = {"usd": "платят только деньгами", "money": "платят только деньгами",
                "paypal": "платят только через PayPal", "ko-fi": "платят только через Ko-fi",
                "cashapp": "платят только через Cash App", "venmo": "платят только через Venmo",
                "trades": "только обмен", "art": "только за арт", "points": "только за поинты"}


def op_restrictions(title: str, snippet: str | None) -> list[str]:
    """Явные условия автора треда, чтобы показать их ей рядом с черновиком. Осторожно: только очевидное."""
    title = title or ""
    blob = f"{title}\n{snippet or ''}"
    prof = th_match.lf_profile(title, snippet)
    found: list[str] = []
    m = LF_PART.search(title)
    if m and re.search(r"[^\W\d_]{2}", m.group(1)):
        found.append(f"ищут: {' '.join(m.group(1).split())[:80]}")
    if prof.closed:
        found.append("тред, похоже, закрыт")
    if prof.paused:
        found.append("тред на паузе")
    if _NO_OFFERS.search(blob):
        found.append("просят без предложений (no offers)")
    if prof.pays_only:
        found.append(_ONLY_LABELS.get(prof.pays_only, f"платят только: {prof.pays_only}"))
    if prof.budget:
        found.append(f"бюджет: до {th_match._fmt(prof.budget)}")
    if prof.no_ota:
        found.append("не хотят OTA")
    for words in prof.negated_text:
        found.append(f"не хотят: {words}")
    return list(dict.fromkeys(found))


def price_conflicts(character: dict, title: str, snippet: str | None) -> list[str]:
    """Чем цена перса не сходится с тредом: бюджет ниже, другая валюта, «платят только …».

    Валюты не пересчитываем: $ и поинты сравниваем только сами с собой."""
    prof = th_match.lf_profile(title or "", snippet)
    price = th_match.char_price(character or {})
    out: list[str] = []
    fmt = th_match._fmt
    if prof.budget and price:
        if prof.budget[1] == price[1]:
            if price[0] > prof.budget[0]:
                out.append(f"цена перса {fmt(price)} выше бюджета треда {fmt(prof.budget)}")
        else:
            out.append(f"бюджет треда {fmt(prof.budget)}, а цена перса {fmt(price)} — в другой валюте, "
                       "пересчитывать не буду: уточни у автора")
    only = th_match.PAY_GROUP.get(prof.pays_only or "", prof.pays_only)
    cur_name = {"usd": "в $", "points": "в поинтах", "eur": "в €", "gbp": "в £"}
    if only and price and only != price[1]:
        what = {"usd": "деньгами", "points": "поинтами", "art": "артом", "trades": "обменом"}.get(only, only)
        out.append(f"тред платит только {what}, а цена {cur_name.get(price[1], price[1])}")
    elif (not only and not out and price and prof.pays and price[1] in ("usd", "points")
          and price[1] not in prof.pays):
        offered = ", ".join({"usd": "деньги", "points": "поинты", "art": "арт", "trades": "обмен"}.get(x, x)
                            for x in sorted(prof.pays))
        out.append(f"автор предлагает {offered}, а цена перса {cur_name.get(price[1], price[1])}")
    return out


def template_warnings(template: str) -> list[str]:
    """Чего не хватает в шаблоне отклика, чтобы автор треда мог купить."""
    used = set(SLOT_RE.findall(template or ""))
    out = []
    if "url" not in used:
        out.append("в шаблоне нет {url} — автор треда не сможет открыть перса")
    if "price" not in used:
        out.append("в шаблоне нет {price} — автор не увидит цену и может не написать")
    return out


# --- Скрипты для страницы треда (выполняются в её Chrome) ---

# Общие куски: как найти форму ответа и понять, залогинена ли она.
# Форма ответа — POST-форма с полем для текста, которая отправляется на toyhou.se в раздел форума.
# Если таких несколько, берём ту, в адресе которой есть номер треда; не ровно одна — не трогаем ничего.
_FIND_FORM = r"""
  var threadId = (location.pathname.match(/^\/~forums\/[^\/]+\/(\d+)\./) || [])[1] || null;
  function formAction(f) {
    try { return new URL(f.getAttribute('action') || location.href, location.href); } catch (e) { return null; }
  }
  var candidates = Array.prototype.slice.call(document.querySelectorAll('form')).filter(function (f) {
    if (!f.querySelector('.fr-element, textarea')) return false;
    if ((f.getAttribute('method') || 'get').toLowerCase() !== 'post') return false;
    var a = formAction(f);
    return !!a && a.origin === location.origin && a.pathname.indexOf('/~forums/') === 0;
  });
  if (candidates.length > 1 && threadId) {
    candidates = candidates.filter(function (f) {
      var a = formAction(f);
      return a && a.pathname.indexOf('/' + threadId + '.') >= 0;
    });
  }
  var form = (candidates.length === 1 && location.origin === 'https://toyhou.se') ? candidates[0] : null;
  var formAmbiguous = candidates.length > 1;
  var loggedOut = !!document.querySelector('a[href$="/~account/login"], a[href*="/~account/login?"]');
  var closed = !!document.querySelector('.thread-locked, .forum-thread-locked, .locked-thread');
  // Кнопка отправки — ровно одна «настоящая» в этой форме: без preview/cancel/report.
  function submitButton(f) {
    var bad = /preview|cancel|report|delete|draft|предпросмотр|отмен/i, good = /post|reply|submit|ответ|отправ/i;
    var label = function (b) { return (b.textContent || '') + ' ' + (b.value || '') + ' ' + (b.name || ''); };
    var all = Array.prototype.slice.call(f.querySelectorAll('button[type=submit], input[type=submit], button:not([type])'))
      .filter(function (b) { return !b.disabled && !bad.test(label(b)); });
    if (all.length === 1) return all[0];
    var preferred = all.filter(function (b) { return good.test(label(b)); });
    return preferred.length === 1 ? preferred[0] : null;
  }
  function postIds() {
    return Array.prototype.slice.call(document.querySelectorAll('.forum-post')).map(function (p) {
      var a = p.querySelector('a[id^="post-"]');
      var m = (p.className || '').match(/\bpost-(\d+)\b/);
      return a ? a.id : (m ? 'post-' + m[1] : null);
    }).filter(Boolean);
  }
"""

INSPECT_REPLY_FORM_JS = r"""(function () {""" + _FIND_FORM + r"""
  var out = {present: !!form, ambiguous: formAmbiguous, candidates: candidates.length, logged_out: loggedOut,
             closed_marker: closed, thread_id: threadId};
  var next = document.querySelector('.pagination a[rel=next]');
  out.on_last_page = !next;
  var user = document.querySelector('.navbar .display-user a, .navbar .user-name-badge, .navbar a[href*="/~account/settings"]');
  out.username_guess = user ? user.textContent.replace(/\s+/g, ' ').trim() : null;
  if (form) {
    var fr = form.querySelector('.fr-element');
    var ta = form.querySelector('textarea');
    out.action = form.getAttribute('action');
    out.method = (form.getAttribute('method') || 'get').toLowerCase();
    out.textarea = ta ? {name: ta.name || null, id: ta.id || null} : null;
    out.editor = fr ? 'froala' : (ta ? 'textarea' : null);
    out.froala_api = (window.FroalaEditor && window.FroalaEditor.INSTANCES) ? 'FroalaEditor' :
      ((window.jQuery && window.jQuery.fn && window.jQuery.fn.froalaEditor) ? 'jquery' : null);
    var btn = submitButton(form);
    out.submit = btn ? {tag: btn.tagName.toLowerCase(), text: (btn.textContent || btn.value || '').trim(),
                        name: btn.name || null} : null;
    // Только имена скрытых полей, без значений: в значениях может быть токен.
    out.hidden_inputs = Array.prototype.slice.call(form.querySelectorAll('input[type=hidden]'))
      .map(function (i) { return i.name; });
    out.ic_ooc = /\bOOC\b|\bIC\b/.test(form.textContent) ||
      !!form.querySelector('[name*=character], [name*=ic_], select[name*=character]');
  }
  return JSON.stringify(out);
})()"""


def fill_reply_js(html: str, submit: bool) -> str:
    """Скрипт: вставить её отклик в форму ответа и (если submit) один раз нажать «отправить»."""
    content = json.dumps(html)  # JSON-строка — безопасный JS-литерал, без подстановки в код
    return r"""(function () {
  var html = """ + content + """, doSubmit = """ + ("true" if submit else "false") + """;
""" + _FIND_FORM + r"""
  function text(s) {
    var doc = new DOMParser().parseFromString(s, 'text/html');
    return (doc.body.textContent || '').replace(/\s+/g, '');
  }
  if (!form) {
    if (loggedOut) return JSON.stringify({state: 'not_logged_in'});
    if (closed) return JSON.stringify({state: 'closed'});
    return JSON.stringify({state: 'no_form', reason: formAmbiguous ? 'несколько похожих форм — не угадываю' : null});
  }
  if (window.__mcpDevianSubmitted) return JSON.stringify({state: 'already_submitted'});
  var fr = form.querySelector('.fr-element'), ta = form.querySelector('textarea'), inst = null;
  if (fr && window.FroalaEditor && window.FroalaEditor.INSTANCES) {
    inst = window.FroalaEditor.INSTANCES.filter(function (e) {
      return e.el === fr || (e.$el && e.$el[0] === fr) || (ta && e.$oel && e.$oel[0] === ta);
    })[0] || null;
  }
  if (!inst && fr && ta && window.jQuery) { inst = window.jQuery(ta).data('froala.editor') || null; }
  try {
    if (inst) {
      inst.html.set(html);
      inst.undo.saveStep();
      inst.events.trigger('contentChanged');
      if (ta) ta.value = inst.html.get();
    } else if (fr) {
      fr.innerHTML = html;
      fr.dispatchEvent(new Event('input', {bubbles: true}));
      if (ta) { ta.value = html; ta.dispatchEvent(new Event('change', {bubbles: true})); }
    } else if (ta) {
      ta.value = html;
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      ta.dispatchEvent(new Event('change', {bubbles: true}));
    } else {
      return JSON.stringify({state: 'fill_failed', reason: 'нет поля для текста'});
    }
  } catch (e) {
    return JSON.stringify({state: 'fill_failed', reason: String(e)});
  }
  var got = inst ? inst.html.get() : (fr ? fr.innerHTML : ta.value);
  // В поле должен оказаться ровно её текст (пробелы и переносы редактор может переставить — их не сравниваем).
  if (text(got) !== text(html)) return JSON.stringify({state: 'fill_failed', reason: 'текст в поле не совпал с черновиком'});
  form.scrollIntoView({block: 'center'});
  if (!doSubmit) return JSON.stringify({state: 'filled', editor: inst ? 'froala' : (fr ? 'froala-dom' : 'textarea')});
  var btn = submitButton(form);
  if (!btn) return JSON.stringify({state: 'no_submit_button'});
  var existing = postIds();  // посты, которые были до отправки: их при проверке не считаем
  window.__mcpDevianSubmitted = true;  // второй вызов на этой странице ничего не отправит
  btn.click();
  return JSON.stringify({state: 'submitted', existing: existing});
})()"""


def verify_post_js(marker: str, username: str, character_url: str = "") -> str:
    """Функция-скрипт `(function (skipIds) {...})`: есть ли на странице НОВЫЙ пост от неё с этим текстом.

    Сравнение без пробелов: <br> и абзацы в textContent склеивают строки. Посты, которые были до
    отправки (skipIds), не считаются. Если известна ссылка на перса — пост должен её содержать.
    Без её ника ничего не подтверждаем: «чей-то пост с похожим текстом» — не доказательство.
    """
    path = ""
    m = re.match(r"^https://toyhou\.se(/[^?#]*)", character_url or "")
    if m and re.fullmatch(r"[\w./%~-]+", m.group(1)):  # путь идёт в CSS-селектор: только безопасные символы
        path = m.group(1).rstrip("/")
    return r"""(function (skipIds) {
  var marker = """ + json.dumps("".join((marker or "").split())) + """, user = """ + json.dumps(
        (username or "").strip().lower()) + """, charPath = """ + json.dumps(path) + r""";
  if (!user) return JSON.stringify({found: false, reason: 'no_username'});
  var skip = {};
  (skipIds || []).forEach(function (id) { skip[id] = true; });
  var posts = Array.prototype.slice.call(document.querySelectorAll('.forum-post'));
  for (var i = posts.length - 1; i >= 0; i--) {
    var p = posts[i];
    var anchor = p.querySelector('a[id^="post-"]');
    var m = (p.className || '').match(/\bpost-(\d+)\b/);
    var pid = anchor ? anchor.id : (m ? 'post-' + m[1] : null);
    if (pid && skip[pid]) continue;
    var author = p.querySelector('.forum-post-user-badge a, .display-user a');
    var name = author ? author.textContent.replace(/\s+/g, '').toLowerCase() : '';
    var body = p.querySelector('.user-content');
    var content = body ? body.textContent.replace(/\s+/g, '') : '';
    if (name !== user.replace(/\s+/g, '') || !marker || content.indexOf(marker) < 0) continue;
    if (charPath && !(body.querySelector('a[href*="' + charPath + '"]') || content.indexOf(charPath) >= 0)) continue;
    var link = p.querySelector('a.forum-post-permalink');
    var permalink = link ? link.href : (pid ? location.href.split('#')[0] + '#' + pid : location.href);
    return JSON.stringify({found: true, permalink: permalink});
  }
  return JSON.stringify({found: false, permalink: null});
})"""
