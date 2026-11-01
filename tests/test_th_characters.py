import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from mcp_devian import her_chrome
from mcp_devian import th_characters as tc

# Форма создания, как её мог бы вернуть осмотр (поля Toyhouse не подтверждены — это подделка для тестов).
CREATE_FORM = {
    "state": "inspected", "url": "https://toyhou.se/~characters/create", "logged_out": False,
    "links": [{"href": "https://toyhou.se/~characters/create", "text": "Create"}],
    "forms": [{
        "action": "https://toyhou.se/~characters/create", "method": "post", "submits": ["Create Character"],
        "fields": [
            {"tag": "input", "type": "hidden", "name": "_token", "label": ""},
            {"tag": "input", "type": "text", "name": "name", "label": "Name"},
            {"tag": "input", "type": "file", "name": "images[]", "label": "Images", "multiple": True,
             "accept": "image/*"},
            {"tag": "select", "type": "select-one", "name": "folder_id", "label": "Folder", "options": [
                {"value": "", "text": "(no folder)"},
                {"value": "11", "text": "Adopts"},
                {"value": "12", "text": "   For sale"},
                {"value": "13", "text": "   Sold"},
                {"value": "14", "text": "      Archive"},
                {"value": "20", "text": "OCs"},
                {"value": "21", "text": "   Sold"},
            ]},
            {"tag": "select", "type": "select-multiple", "name": "tags[]", "label": "Tags", "multiple": True,
             "options": [], "widget": "select2"},
            {"tag": "select", "type": "select-one", "name": "trade_listing", "label": "Marketplace status", "options": [
                {"value": "0", "text": "Not for sale"}, {"value": "1", "text": "For trade"},
                {"value": "2", "text": "For sale"}, {"value": "3", "text": "For sale or trade"}]},
            {"tag": "input", "type": "text", "name": "price", "label": "Price"},
            {"tag": "select", "type": "select-one", "name": "currency", "label": "Currency", "options": [
                {"value": "usd", "text": "USD ($)"}, {"value": "eur", "text": "EUR (€)"}]},
            {"tag": "input", "type": "text", "name": "creator_name", "label": "Designer"},
            {"tag": "textarea", "type": "textarea", "name": "profile", "label": "Profile", "widget": "froala"},
        ]}],
}
EDIT_FORM = {
    "state": "inspected", "url": "https://toyhou.se/555.other/edit", "logged_out": False, "links": [],
    "forms": [{"action": "https://toyhou.se/555.other/edit", "method": "post", "submits": ["Save"], "fields": [
        {"tag": "input", "type": "text", "name": "name", "label": "Name"},
        CREATE_FORM["forms"][0]["fields"][3],
        CREATE_FORM["forms"][0]["fields"][5],
        CREATE_FORM["forms"][0]["fields"][6],
        CREATE_FORM["forms"][0]["fields"][7],
    ]}],
}
TRANSFER_FORM = {
    "state": "inspected", "url": "https://toyhou.se/555.other/ownership/transfer", "logged_out": False, "links": [],
    "forms": [{"action": "https://toyhou.se/555.other/ownership/transfer", "method": "post", "submits": ["Transfer"],
               "fields": [
                   {"tag": "input", "type": "radio", "name": "transfer_type", "label": "To a user", "value": "user"},
                   {"tag": "input", "type": "radio", "name": "transfer_type", "label": "Off-site", "value": "offsite"},
                   {"tag": "input", "type": "text", "name": "recipient", "label": "Recipient username"},
               ]}],
}
LUMI = "https://toyhou.se/123456.lumi"


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "lumi.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    return p


def ok_check(path):
    p = Path(path)
    return (p, "") if p.exists() else (None, f"Не нашла файл {p}")


def good(img, **over):
    data = {"name": "Lumi", "images": [str(img)], "folder": "Adopts/For sale", "tags": ["wolf", "#Pastel", "wolf"],
            "price": 25, "currency": "usd", "status": "for sale", "designer": "moonmoth",
            "profile": "Sweet <b>wolf</b> & friend\n\nTOS on my page"}
    data.update(over)
    return tc.check_character(data, check_image=ok_check)


def form(raw=CREATE_FORM):
    return tc.parse_inspect(json.loads(json.dumps(raw)))


# --- Проверка ввода ---

def test_check_character_normalizes_and_fingerprints(img):
    out = good(img)
    assert out["errors"] == []
    ch = out["character"]
    assert ch.name == "Lumi" and ch.status == "sale" and ch.currency == "usd" and ch.price == 25
    assert ch.tags == ["wolf", "Pastel"]
    assert ch.profile == "Sweet <b>wolf</b> & friend\n\nTOS on my page"  # её текст не трогаем
    meta = out["images"][0]
    assert meta["name"] == "lumi.png" and meta["type"] == "image/png" and meta["size"] == 108
    assert len(meta["sha256"]) == 64


@pytest.mark.parametrize("over,needle", [
    ({"name": "  "}, "имя"),
    ({"status": "maybe"}, "статус"),
    ({"price": -3}, "цена"),
    ({"price": "abc"}, "цена"),
    ({"currency": "btc"}, "валют"),
    ({"images": []}, "картинк"),
    ({"images": ["/nope/x.png"]}, "Не нашла"),
    ({"designer": "bad name!"}, "ник"),
    ({"folder": "Adopts//"}, "папк"),
])
def test_check_character_errors(img, over, needle):
    errors = good(img, **over)["errors"]
    assert errors and any(needle in e.lower() or needle in e for e in errors), errors


@pytest.mark.parametrize("value,key", [("for sale", "sale"), ("Продаю", "sale"), ("FT", "trade"), ("обмен", "trade"),
                                       ("NFS", "nfs"), ("not for sale", "nfs"), ("", "nfs")])
def test_status_aliases(value, key):
    assert tc.normalize_status(value) == key


def test_sale_without_price_is_a_warning_not_an_error(img):
    out = good(img, price=None, currency=None)
    assert out["errors"] == [] and any("цен" in w for w in out["warnings"])


# --- Осмотр формы ---

def test_parse_inspect_finds_roles_and_hides_token_values():
    f = form()
    assert f["state"] == "inspected" and f["kind"] == "create"
    roles = f["roles"]
    assert roles["name"] == "name" and roles["folder"] == "folder_id" and roles["tags"] == "tags[]"
    assert roles["status"] == "trade_listing" and roles["price"] == "price" and roles["currency"] == "currency"
    assert roles["designer"] == "creator_name" and roles["profile"] == "profile" and roles["image"] == "images[]"
    assert "world" in f["missing"]
    assert "_token" not in json.dumps(f["roles"])


def test_folder_paths_follow_indentation():
    paths = {p["path"]: p["value"] for p in form()["folders"]}
    assert paths == {"Adopts": "11", "Adopts/For sale": "12", "Adopts/Sold": "13", "Adopts/Sold/Archive": "14",
                     "OCs": "20", "OCs/Sold": "21"}


def test_folder_paths_use_explicit_depth():
    folders = tc.folder_paths([{"value": "1", "text": "A", "depth": 0}, {"value": "2", "text": "B", "depth": 1},
                               {"value": "3", "text": "C", "depth": 0}])
    assert [f["path"] for f in folders] == ["A", "A/B", "C"]


@pytest.mark.parametrize("query,value", [("Adopts/For sale", "12"), ("adopts / for SALE", "12"),
                                         ("Adopts > Sold > Archive", "14"), ("Archive", "14"), ("OCs", "20")])
def test_match_folder(query, value):
    got = tc.match_folder(query, form()["folders"])
    assert got["value"] == value


def test_match_folder_ambiguous_and_unknown():
    amb = tc.match_folder("Sold", form()["folders"])
    assert amb["value"] is None and set(amb["suggestions"]) == {"Adopts/Sold", "OCs/Sold"}
    miss = tc.match_folder("Adopts/For sail", form()["folders"])
    assert miss["value"] is None and "Adopts/For sale" in miss["suggestions"]


def test_parse_inspect_states():
    assert tc.parse_inspect({"state": "needs_login"})["state"] == "needs_login"
    assert tc.parse_inspect("not json")["state"] == "bad_result"
    empty = tc.parse_inspect({"state": "inspected", "url": tc.CREATE_URL, "forms": []})
    assert empty["state"] == "not_found"


# --- План создания ---

def fields_by_role(plan):
    return {f["role"]: f for f in plan["targets"][0]["fields"]}


def test_plan_create_maps_every_field(img):
    checked = good(img)
    plan = tc.plan_create(checked, form())
    assert plan["ok"], plan["problems"]
    t = plan["targets"][0]
    assert t["url"] == tc.CREATE_URL and re.match(t["url_re"], tc.CREATE_URL)
    f = fields_by_role(plan)
    assert f["name"]["value"] == "Lumi" and f["name"]["kind"] == "text"
    assert f["folder"] == {**f["folder"], "name": "folder_id", "value": "12", "kind": "select"}
    assert f["status"]["value"] == "2" and f["price"]["value"] == "25" and f["currency"]["value"] == "usd"
    assert f["tags"]["value"] == ["wolf", "Pastel"] and f["tags"]["kind"] == "tags"
    assert f["designer"]["value"] == "moonmoth"
    assert f["profile"]["kind"] == "html"
    assert f["profile"]["value"] == "<p>Sweet &lt;b&gt;wolf&lt;/b&gt; &amp; friend</p><p>TOS on my page</p>"
    assert t["image_field"] == "images[]" and t["images"][0]["name"] == "lumi.png"
    assert "_token" not in json.dumps(plan)


def test_plan_create_without_inspect_asks_for_it(img):
    plan = tc.plan_create(good(img), None)
    assert not plan["ok"] and "осмотр" in plan["problems"][0]


def test_plan_create_blocks_on_unknown_folder_and_currency(img):
    plan = tc.plan_create(good(img, folder="Nope", currency="points"), form())
    assert not plan["ok"]
    text = " ".join(plan["problems"])
    assert "Nope" in text and "валют" in text.lower()


def test_plan_create_world_missing_is_a_note(img):
    plan = tc.plan_create(good(img, world="Starfall"), form())
    assert plan["ok"] and any("Starfall" in n and "мир" in n for n in plan["notes"])


def test_plan_create_nfs_skips_price(img):
    plan = tc.plan_create(good(img, status="nfs", price=None, currency=None), form())
    f = fields_by_role(plan)
    assert f["status"]["value"] == "0" and "price" not in f and "currency" not in f


def test_plan_create_ambiguous_status_option(img):
    raw = json.loads(json.dumps(CREATE_FORM))
    raw["forms"][0]["fields"][5]["options"] = [{"value": "a", "text": "For sale!"}, {"value": "b", "text": "Sale"}]
    plan = tc.plan_create(good(img), tc.parse_inspect(raw))
    assert not plan["ok"] and any("for sale" in p for p in plan["problems"])


def test_preview_create_is_honest(img):
    checked = good(img)
    text = tc.preview_create(checked, tc.plan_create(checked, form()))
    assert "Lumi" in text and "Adopts/For sale" in text and "25 USD" in text and "wolf, Pastel" in text
    assert "Sweet <b>wolf</b> & friend" in text  # её текст — как есть
    assert "Save" in text and "сама" in text
    assert "lumi.png" in text


# --- Передача, статус продажи, перенос между папками ---

def test_character_urls():
    assert tc.character_id("https://toyhou.se/123456.lumi/gallery") == "123456"
    assert tc.character_base("https://toyhou.se/123456.lumi/gallery?x=1") == LUMI
    for bad in ("https://toyhou.se.evil.com/123.x", "https://evil.com/?u=https://toyhou.se/123.x",
                "http://toyhou.se/123.x", "https://toyhou.se/~forums/13.x/123.y"):
        assert tc.character_id(bad) is None, bad


def test_url_patterns_reject_lookalikes():
    pat = tc.url_pattern("transfer", "123456")
    assert re.match(pat, LUMI + "/ownership/transfer")
    for bad in ("https://toyhou.se/1234567.lumi/ownership/transfer", "https://toyhou.se.evil/123456.x",
                "https://evil.com/https://toyhou.se/123456.x"):
        assert not re.match(pat, bad), bad
    create = tc.url_pattern("create")
    assert re.match(create, tc.CREATE_URL) and not re.match(create, "https://toyhou.se/~characters/created")


def test_plan_transfer(tmp_path):
    plan = tc.plan_transfer(LUMI, "@buyer_1", tc.parse_inspect(TRANSFER_FORM), name="Lumi")
    assert plan["ok"], plan["problems"]
    t = plan["targets"][0]
    assert t["url"] == LUMI + "/ownership/transfer"  # адрес из осмотра чужого перса, но для этого
    f = {x["role"]: x for x in t["fields"]}
    assert f["recipient"]["value"] == "buyer_1" and f["transfer_type"]["value"] == "user"
    text = tc.preview_transfer(plan)
    assert "buyer_1" in text and "Lumi" in text and "сама" in text


@pytest.mark.parametrize("url,buyer", [("https://evil.com/1.x", "b"), (LUMI, ""), (LUMI, "a b")])
def test_plan_transfer_rejects_bad_input(url, buyer):
    plan = tc.plan_transfer(url, buyer, tc.parse_inspect(TRANSFER_FORM))
    assert not plan["ok"]


def test_transfer_warnings_from_sales():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sales (id TEXT, character_url TEXT, buyer TEXT, status TEXT, title TEXT)")
    conn.execute("INSERT INTO sales VALUES ('s1', ?, 'buyer_1', 'won', 'Lumi')", (LUMI,))
    warns = tc.transfer_warnings(conn, LUMI + "/", "buyer_1")
    assert any("оплат" in w for w in warns)
    assert any("other" in w for w in tc.transfer_warnings(conn, LUMI, "other"))
    conn.execute("UPDATE sales SET status = 'paid'")
    assert tc.transfer_warnings(conn, LUMI, "buyer_1") == []


def test_plan_listing_marks_sold():
    plan = tc.plan_listing(LUMI, "nfs", None, None, tc.parse_inspect(EDIT_FORM))
    assert plan["ok"]
    t = plan["targets"][0]
    assert t["url"] == LUMI + "/edit"
    assert [(f["role"], f["value"]) for f in t["fields"]] == [("status", "0")]


def test_plan_move_many_with_limit():
    urls = [f"https://toyhou.se/{100 + i}.c{i}" for i in range(3)]
    plan = tc.plan_move(urls, "OCs/Sold", tc.parse_inspect(EDIT_FORM))
    assert plan["ok"] and len(plan["targets"]) == 3
    assert all(t["fields"] == [{"role": "folder", "name": "folder_id", "kind": "select", "value": "21",
                                "label": "OCs/Sold"}] for t in plan["targets"])
    assert plan["targets"][1]["url"] == "https://toyhou.se/101.c1/edit"
    too_many = tc.plan_move([f"https://toyhou.se/{i}.x" for i in range(1, tc.MOVE_MAX + 2)], "OCs",
                            tc.parse_inspect(EDIT_FORM))
    assert not too_many["ok"]
    assert "OCs/Sold" in tc.preview_move(plan)


# --- Скрипты: защита адреса, никаких кликов и отправок ---

def all_scripts():
    plan = tc.plan_create(good_static(), form())
    fields = plan["targets"][0]["fields"]
    return {
        "inspect_create.js": tc.inspect_js("create"),
        "inspect_transfer.js": tc.inspect_js("transfer", "123456"),
        "fill_create.js": tc.fill_js("create", fields),
        "fill_transfer.js": tc.fill_js("transfer", [{"role": "recipient", "name": "recipient", "kind": "text",
                                                    "value": 'b"</script>', "label": "x"}], "123456"),
        "attach.js": tc.attach_js("create", "images[]", [{"name": 'a"b.png', "type": "image/png", "size": 5}]),
        "jxa.js": tc.TH_FORM_JXA,
    }


def good_static():
    return {"character": tc.NewCharacter(name="Lumi", images=[], folder="Adopts/For sale", tags=["wolf"],
                                         price=25.0, currency="usd", status="sale", designer="moonmoth",
                                         profile="Hi"),
            "images": [], "errors": [], "warnings": []}


SUBMIT = re.compile(r"\.\s*(?:click|submit|requestSubmit)\s*\(|new\s+(?:Submit|Mouse|Pointer)?Event\(\s*['\"](?:submit|click)"
                    r"|\.trigger(?:Handler)?\(\s*['\"](?:submit|click)|\[\s*['\"](?:submit|requestSubmit|click)['\"]\s*\]")


def test_scripts_never_click_or_submit():
    for name, code in all_scripts().items():
        assert not SUBMIT.search(code), name


def test_page_scripts_guard_the_address():
    s = all_scripts()
    for name in ("inspect_create.js", "fill_create.js", "attach.js"):
        assert "https://toyhou.se" in s[name] and "~characters" in s[name], name
    for name in ("inspect_transfer.js", "fill_transfer.js"):
        assert '"123456"' in s[name], name
    with pytest.raises(ValueError):
        tc.fill_js("transfer", [], "12a")


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node для проверки синтаксиса")
def test_all_scripts_are_valid_javascript(tmp_path):
    for name, code in all_scripts().items():
        path = tmp_path / name
        path.write_text(code, encoding="utf-8")
        done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, check=False)
        assert done.returncode == 0, f"{name}: {done.stderr}"


FAKE_DOM = r"""
function el(tag, type, name, value, extra) {
  const e = Object.assign({tagName: tag.toUpperCase(), type: type, name: name, value: value || '', checked: false,
    multiple: false, options: [], events: [], getAttribute(a) { return this[a] || null; },
    dispatchEvent(ev) { this.events.push(ev.type); return true; }, closest() { return null; }}, extra || {});
  if (tag === 'select') {
    Object.defineProperty(e, 'value', {get() { const o = this.options.find(o => o.selected); return o ? o.value : ''; },
      set(v) { this.options.forEach(o => { o.selected = (o.value === v); }); }});
    Object.defineProperty(e, 'selectedOptions', {get() { return this.options.filter(o => o.selected); }});
    e.appendChild = function (o) { this.options.push(o); };
  }
  return e;
}
function opt(value, text) { return {value: value, text: text, selected: false}; }
global.Option = function (text, value, a, b) { return {value: value, text: text, selected: !!b}; };
global.Event = function (type) { this.type = type; };
function makeForm(fields, action) {
  return {method: 'post', action: action, fields: fields,
    getAttribute(a) { return this[a]; },
    querySelectorAll(sel) { return sel === '[name]' ? this.fields : (sel.indexOf('submit') >= 0 ? [] : this.fields); },
    querySelector() { return null; }, scrollIntoView() {}};
}
function makeDoc(forms, loggedOut) {
  return {readyState: 'complete', forms: forms,
    querySelectorAll(sel) { return sel === 'form' ? this.forms : []; },
    querySelector(sel) { return (loggedOut && sel.indexOf('login') >= 0) ? {} : null; },
    getElementById() { return null; }};
}
function runPage(js, href, doc) {
  const u = new URL(href);
  const location = {href: href, origin: u.origin, pathname: u.pathname};
  const window = {};
  return JSON.parse(new Function('location', 'document', 'window', 'return ' + js)(location, doc, window));
}
"""


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node")
def test_fill_script_on_fake_page(tmp_path):
    """Заполнение на подставной странице: адрес, вход, поля и проверка значений — без единого клика."""
    plan = tc.plan_create(good_static(), form())
    js = tc.fill_js("create", plan["targets"][0]["fields"])
    check = tmp_path / "check.js"
    check.write_text(FAKE_DOM + "const JS = " + json.dumps(js) + r""";
function freshForm() {
  const folder = el('select', 'select-one', 'folder_id');
  folder.options = [opt('', '-'), opt('11', 'Adopts'), opt('12', 'For sale')];
  const status = el('select', 'select-one', 'trade_listing'); status.options = [opt('0', 'NFS'), opt('2', 'For sale')];
  const cur = el('select', 'select-one', 'currency'); cur.options = [opt('usd', 'USD')];
  const tags = el('select', 'select-multiple', 'tags[]', '', {multiple: true});
  const fields = [el('input', 'hidden', '_token', 'SECRET'), el('input', 'text', 'name'), folder, tags, status,
    el('input', 'text', 'price'), cur, el('input', 'text', 'creator_name'), el('textarea', 'textarea', 'profile')];
  return makeForm(fields, 'https://toyhou.se/~characters/create');
}
let f = freshForm();
let r = runPage(JS, 'https://toyhou.se/~characters/create', makeDoc([f]));
if (r.state !== 'filled') throw new Error('state ' + JSON.stringify(r));
const by = {}; f.fields.forEach(x => { by[x.name] = x; });
if (by['name'].value !== 'Lumi' || by['folder_id'].value !== '12' || by['trade_listing'].value !== '2') throw new Error('values');
if (by['price'].value !== '25' || by['_token'].value !== 'SECRET') throw new Error('price/token');
if (by['tags[]'].selectedOptions.map(o => o.text).join() !== 'wolf') throw new Error('tags');
if (by['profile'].value !== '<p>Hi</p>') throw new Error('profile ' + by['profile'].value);
f = freshForm();
r = runPage(JS, 'https://toyhou.se.evil.com/~characters/create', makeDoc([f]));
if (r.state !== 'wrong_page' || f.fields[1].value !== '') throw new Error('evil origin ' + JSON.stringify(r));
r = runPage(JS, 'https://toyhou.se/~account/login', makeDoc([], true));
if (r.state !== 'needs_login') throw new Error('login ' + JSON.stringify(r));
const partial = freshForm(); partial.fields = partial.fields.filter(x => x.name !== 'folder_id');
r = runPage(JS, 'https://toyhou.se/~characters/create', makeDoc([partial]));
if (r.state !== 'not_found' || r.missing.indexOf('folder_id') < 0) throw new Error('missing ' + JSON.stringify(r));
if (partial.fields[1].value !== '') throw new Error('touched a form that is not the right one');
""", encoding="utf-8")
    done = subprocess.run(["node", str(check)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


# --- Запуск на подставном osascript ---

class FakeRun:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        out = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


@pytest.fixture(autouse=True)
def chrome_here(monkeypatch):
    monkeypatch.setattr(her_chrome, "chrome_installed", lambda: True)


def test_run_inspect_caches_form():
    conn = sqlite3.connect(":memory:")
    run = FakeRun(CREATE_FORM)
    out = tc.run_inspect(conn, "create", runner=run, lock_wait_s=1)
    assert out["state"] == "inspected" and out["form"]["roles"]["folder"] == "folder_id"
    cmd = run.calls[0]
    assert cmd[:4] == ["osascript", "-l", "JavaScript", "-e"] and cmd[5] == tc.CREATE_URL and cmd[8] == "inspect"
    assert tc.load_form(conn, "create")["roles"]["name"] == "name"
    assert tc.load_form(conn, "create", now=1e12) is None  # устарел


def test_run_inspect_needs_character_for_transfer():
    with pytest.raises(ValueError):
        tc.run_inspect(sqlite3.connect(":memory:"), "transfer", url="https://evil.com/1.x", runner=FakeRun({}))


def test_run_plan_fills_and_attaches(img):
    checked = good(img)
    plan = tc.plan_create(checked, form())
    run = FakeRun({"state": "filled", "results": [], "image": {"state": "attached", "count": 1}})
    out = tc.run_plan(plan, runner=run, lock_wait_s=1)
    assert out["state"] == "filled" and "Save" in out["message"] and "картинк" in out["message"].lower()
    cmd = run.calls[0]
    assert cmd[8] == "fill" and json.loads(cmd[10])[0]["path"] == str(img.resolve())
    assert re.match(cmd[6], tc.CREATE_URL)


def test_run_plan_image_not_attached_is_said_plainly(img):
    plan = tc.plan_create(good(img), form())
    out = tc.run_plan(plan, runner=FakeRun({"state": "filled", "image": {"state": "not_attached"}}), lock_wait_s=1)
    assert "осталось выбрать картинку и нажать Save" in out["message"]


def test_run_plan_refuses_changed_image(img):
    plan = tc.plan_create(good(img), form())
    img.write_bytes(b"other")
    run = FakeRun({"state": "filled"})
    out = tc.run_plan(plan, runner=run, lock_wait_s=1)
    assert out["state"] == "image_changed" and run.calls == []


@pytest.mark.parametrize("state,needle", [("needs_login", "войд"), ("challenge", "галочк"),
                                          ("wrong_page", "ничего не"), ("not_found", "осмотр")])
def test_run_plan_states(img, state, needle):
    plan = tc.plan_create(good(img), form())
    out = tc.run_plan(plan, runner=FakeRun({"state": state, "missing": ["folder_id"]}), lock_wait_s=1)
    assert out["state"] == state and needle in out["message"]


def test_run_plan_refuses_not_ok_plan(img):
    plan = tc.plan_create(good(img, folder="Nope"), form())
    with pytest.raises(ValueError):
        tc.run_plan(plan, runner=FakeRun({}))


def test_run_plan_move_runs_each_target():
    plan = tc.plan_move(["https://toyhou.se/101.a", "https://toyhou.se/102.b"], "OCs", tc.parse_inspect(EDIT_FORM))
    run = FakeRun({"state": "filled", "results": []})
    out = tc.run_plan(plan, runner=run, lock_wait_s=1)
    assert len(run.calls) == 2 and out["state"] == "filled" and len(out["results"]) == 2
    assert run.calls[1][5] == "https://toyhou.se/102.b/edit"


# --- Регрессии после проверки ---

def test_submit_regex_catches_sneaky_forms():
    for bad in ("$(f).trigger('submit')", "new SubmitEvent('submit')", "f['submit']()", "b.click()",
                "new MouseEvent('click')", "f.requestSubmit()"):
        assert SUBMIT.search(bad), bad


NODE = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def run_node(tmp_path, body):
    check = tmp_path / "check.js"
    check.write_text(FAKE_DOM + body, encoding="utf-8")
    done = subprocess.run(["node", str(check)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


@NODE
def test_fill_stops_a_form_that_submits_itself(tmp_path):
    """onchange с отправкой формы: отправка гасится, итог autosubmit, всё возвращено как было."""
    js = tc.fill_js("transfer", [{"name": "recipient", "kind": "text", "value": "bob", "label": "кому"}], "123456")
    run_node(tmp_path, "const JS = " + json.dumps(js) + r""";
global.HTMLFormElement = function () {};
let real = 0;
HTMLFormElement.prototype.submit = function () { real++; };
HTMLFormElement.prototype.requestSubmit = function () { real++; };
const orig = [HTMLFormElement.prototype.submit, HTMLFormElement.prototype.requestSubmit];
const wait = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
  for (const how of ['submit', 'requestSubmit']) {
    const f = makeForm([], 'https://toyhou.se/123456.lumi/ownership/transfer');
    Object.setPrototypeOf(f, HTMLFormElement.prototype);
    const inp = el('input', 'text', 'recipient', '', {dispatchEvent(ev) { if (ev.type === 'change') f[how](); return true; }});
    f.fields = [inp];
    const r = runPage(JS, 'https://toyhou.se/123456.lumi/ownership/transfer', makeDoc([f]));
    if (r.state !== 'autosubmit') throw new Error(how + ' ' + JSON.stringify(r));
    await wait(""" + str(tc.SUBMIT_HOLD_MS + 100) + r""");
  }
  if (real !== 0) throw new Error('form really submitted');
  if (HTMLFormElement.prototype.submit !== orig[0] || HTMLFormElement.prototype.requestSubmit !== orig[1]) {
    throw new Error('not restored');
  }
})().catch(e => { console.error(e); process.exit(1); });
""")


@NODE
def test_fill_blocks_a_delayed_self_submit(tmp_path):
    """Форма уходит сама с задержкой (таймер после change): отправка всё равно гасится и видна в
    window.__mcpThTried, который JXA читает перед показом вкладки; потом всё возвращается как было."""
    js = tc.fill_js("transfer", [{"name": "recipient", "kind": "text", "value": "bob", "label": "кому"}], "123456")
    hold = tc.SUBMIT_HOLD_MS
    run_node(tmp_path, "const JS = " + json.dumps(js) + r""";
global.HTMLFormElement = function () {};
let real = 0;
HTMLFormElement.prototype.submit = function () { real++; };
const orig = HTMLFormElement.prototype.submit;
const href = 'https://toyhou.se/123456.lumi/ownership/transfer';
const f = makeForm([], href);
Object.setPrototypeOf(f, HTMLFormElement.prototype);
f.fields = [el('input', 'text', 'recipient', '', {dispatchEvent(ev) {
  if (ev.type === 'change') setTimeout(() => f.submit(), """ + str(hold // 3) + r""");
  return true;
}})];
const u = new URL(href), win = {};
const r = JSON.parse(new Function('location', 'document', 'window', 'return ' + JS)(
  {href: href, origin: u.origin, pathname: u.pathname}, makeDoc([f]), win));
if (r.state !== 'filled') throw new Error('state ' + JSON.stringify(r));
setTimeout(() => {
  if (real !== 0) throw new Error('form really submitted');
  if (!(win.__mcpThTried || []).length) throw new Error('late attempt not recorded');
}, """ + str(hold * 2 // 3) + r""");
setTimeout(() => {
  if (HTMLFormElement.prototype.submit !== orig) throw new Error('not restored');
  f.submit();
  if (real !== 1) throw new Error('her own submit must work after the hold');
}, """ + str(hold + 200) + r""");
""")


def test_fill_jxa_rereads_late_attempts_before_showing_tab():
    jxa = tc.TH_FORM_JXA
    assert "__HOLD_S__" not in jxa and "window.__mcpThTried" in jxa
    assert jxa.index("window.__mcpThTried") < jxa.index("keep = true;\n      activate(id)")
    assert tc._budget(30, []) >= 30 * 2 + 15 + tc.SUBMIT_HOLD_MS // 1000


def test_inspect_autosubmit_field_blocks_the_plan():
    raw = json.loads(json.dumps(EDIT_FORM))
    raw["forms"][0]["fields"][1]["autosubmit"] = True  # folder_id
    f = tc.parse_inspect(raw)
    assert f["autosubmit"] == ["folder_id"]
    plan = tc.plan_move([LUMI], "OCs", f)
    assert not plan["ok"] and any("само отправляет" in p for p in plan["problems"])
    assert "folder_id" in tc._inspect_message(f)


def test_inspect_script_records_autosubmit_and_truncation():
    js = tc.inspect_js("create")
    assert "onchange" in js and "autosubmit" in js and "truncated" in js and str(tc.INSPECT_FIELDS_MAX) in js
    raw = json.loads(json.dumps(CREATE_FORM))
    raw["forms"][0]["truncated"] = True
    f = tc.parse_inspect(raw)
    assert f["truncated"] and "видела не все" in tc._inspect_message(f)


@NODE
def test_hidden_profile_without_editor_is_not_reported_filled(tmp_path):
    plan = tc.plan_create(good_static(), form())
    js = tc.fill_js("create", plan["targets"][0]["fields"])
    run_node(tmp_path, "const JS = " + json.dumps(js) + r""";
const folder = el('select', 'select-one', 'folder_id');
folder.options = [opt('12', 'For sale')];
const status = el('select', 'select-one', 'trade_listing'); status.options = [opt('2', 'For sale')];
const cur = el('select', 'select-one', 'currency'); cur.options = [opt('usd', 'USD')];
const tags = el('select', 'select-multiple', 'tags[]', '', {multiple: true});
const profile = el('textarea', 'textarea', 'profile', '', {getClientRects() { return []; }});
const f = makeForm([el('input', 'text', 'name'), folder, tags, status, el('input', 'text', 'price'), cur,
  el('input', 'text', 'creator_name'), profile], 'https://toyhou.se/~characters/create');
const r = runPage(JS, 'https://toyhou.se/~characters/create', makeDoc([f]));
const p = r.results.filter(x => x.name === 'profile')[0];
if (r.state !== 'partial' || p.ok || p.reason.indexOf('редактор') < 0 || profile.value !== '') {
  throw new Error(JSON.stringify(r));
}
""")


@NODE
def test_edit_script_refuses_another_character(tmp_path):
    js = tc.fill_js("edit", [{"name": "folder_id", "kind": "select", "value": "12", "label": "x"}], "123456")
    run_node(tmp_path, "const JS = " + json.dumps(js) + r""";
function page() {
  const s = el('select', 'select-one', 'folder_id'); s.options = [opt('12', 'A')];
  return makeForm([s], 'https://toyhou.se/123456.lumi/edit');
}
let f = page();
let r = runPage(JS, 'https://toyhou.se/1234567.lumi/edit', makeDoc([f]));
if (r.state !== 'wrong_page' || f.fields[0].value !== '') throw new Error('other id ' + JSON.stringify(r));
f = page();
r = runPage(JS, 'https://toyhou.se/123456.lumi/edit', makeDoc([f]));
if (r.state !== 'filled' || f.fields[0].value !== '12') throw new Error('own id ' + JSON.stringify(r));
""")


@pytest.mark.parametrize("stored", ["toyhou.se/123456.lumi", "http://toyhou.se/123456.lumi",
                                    "https://www.toyhou.se/123456.lumi", "https://toyhou.se/123456.lumi-old",
                                    "https://toyhou.se/123456.", "https://toyhou.se/123456.lumi/gallery"])
def test_transfer_warnings_match_by_character_id(stored):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sales (id TEXT, character_url TEXT, buyer TEXT, status TEXT, title TEXT)")
    conn.execute("INSERT INTO sales VALUES ('s1', ?, 'buyer_1', 'won', 'Lumi')", (stored,))
    conn.execute("INSERT INTO sales VALUES ('s2', 'https://toyhou.se/1234567.x', 'z', 'paid', 'X')")
    warns = tc.transfer_warnings(conn, LUMI, "buyer_1")
    assert any("оплат" in w for w in warns), warns
    assert not any("нет в журнале" in w for w in warns)


def test_transfer_warnings_ignore_other_ids():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sales (id TEXT, character_url TEXT, buyer TEXT, status TEXT, title TEXT)")
    conn.execute("INSERT INTO sales VALUES ('s1', 'https://toyhou.se/1123456.lumi', 'b', 'won', 'L')")
    assert any("нет в журнале" in w for w in tc.transfer_warnings(conn, LUMI, "b"))


def move_plan(n=3):
    return tc.plan_move([f"https://toyhou.se/{100 + i}.c{i}" for i in range(n)], "OCs", tc.parse_inspect(EDIT_FORM))


class SeqRun:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(a))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(a), stderr="")


def test_run_plan_mixed_tabs_say_partial_honestly():
    run = SeqRun([{"state": "filled"}, {"state": "not_found", "missing": ["folder_id"]}, {"state": "filled"}])
    out = tc.run_plan(move_plan(), runner=run, lock_wait_s=1)
    assert out["state"] == "partial"
    assert "Заполнено вкладок: 2 из 3" in out["message"] and "ничего не заполняла" not in out["message"]
    assert "https://toyhou.se/101.c1/edit: формы нет" in out["message"]


def test_run_plan_error_on_later_tab_keeps_filled_ones():
    run = SeqRun([{"state": "filled"}, RuntimeError("boom"), {"state": "filled"}])
    out = tc.run_plan(move_plan(), runner=run, lock_wait_s=1)
    assert out["state"] == "partial" and len(run.calls) == 2
    assert "1 из 3" in out["message"] and "boom" in out["message"] and "102.c2/edit: не открывала" in out["message"]


def test_run_plan_error_on_first_tab_still_raises():
    with pytest.raises(tc.ToyhouseError):
        tc.run_plan(move_plan(), runner=SeqRun([RuntimeError("boom")]), lock_wait_s=1)


def test_run_plan_autosubmit_state(img):
    out = tc.run_plan(tc.plan_create(good(img), form()), runner=FakeRun({"state": "autosubmit"}), lock_wait_s=1)
    assert out["state"] == "autosubmit" and "отправиться сама" in out["message"]


def test_single_file_field_gets_only_the_first_image(tmp_path):
    imgs = []
    for i in range(3):
        p = tmp_path / f"a{i}.png"
        p.write_bytes(b"x" * 10)
        imgs.append(str(p))
    raw = json.loads(json.dumps(CREATE_FORM))
    raw["forms"][0]["fields"][2]["multiple"] = False
    checked = tc.check_character({"name": "A", "images": imgs}, check_image=ok_check)
    plan = tc.plan_create(checked, tc.parse_inspect(raw))
    t = plan["targets"][0]
    assert [i["name"] for i in t["images"]] == ["a0.png"] and any("один файл" in n for n in plan["notes"])


def test_images_over_the_size_cap_are_left_to_her(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "ATTACH_MAX_BYTES", 25)
    imgs = []
    for i in range(3):
        p = tmp_path / f"b{i}.png"
        p.write_bytes(b"x" * 10)
        imgs.append(str(p))
    plan = tc.plan_create(tc.check_character({"name": "A", "images": imgs}, check_image=ok_check), form())
    t = plan["targets"][0]
    assert [i["name"] for i in t["images"]] == ["b0.png", "b1.png"] and any("b2.png" in n for n in plan["notes"])


def test_inspect_not_found_shows_links_and_own_text():
    f = tc.parse_inspect({"state": "inspected", "url": tc.CREATE_URL, "forms": [],
                          "links": [{"href": "https://toyhou.se/~characters/new", "text": "New character"}]})
    msg = tc._inspect_message(f)
    assert "только смотрела" in msg and "https://toyhou.se/~characters/new" in msg
    assert "th_character_inspect" not in msg


def test_create_form_on_another_address(img):
    with pytest.raises(ValueError):
        tc.run_inspect(sqlite3.connect(":memory:"), "create", url="https://toyhou.se/~account/settings",
                       runner=FakeRun({}))
    with pytest.raises(ValueError):
        tc.run_inspect(sqlite3.connect(":memory:"), "create", url="https://evil.com/~characters/new",
                       runner=FakeRun({}))
    raw = json.loads(json.dumps(CREATE_FORM))
    raw["url"] = "https://toyhou.se/~characters/new"
    conn = sqlite3.connect(":memory:")
    run = FakeRun(raw)
    out = tc.run_inspect(conn, "create", url="https://toyhou.se/~characters/new?x=1", runner=run, lock_wait_s=1)
    assert out["state"] == "inspected" and run.calls[0][5] == "https://toyhou.se/~characters/new"
    assert '"/~characters/new"' in run.calls[0][7]
    plan = tc.plan_create(good(img), tc.load_form(conn, "create"))
    t = plan["targets"][0]
    assert t["url"] == "https://toyhou.se/~characters/new" and re.match(t["url_re"], t["url"])
    assert not re.match(t["url_re"], tc.CREATE_URL)
    fill = FakeRun({"state": "filled"})
    tc.run_plan(plan, runner=fill, lock_wait_s=1)
    assert '"/~characters/new"' in fill.calls[0][7]


def test_plans_have_titles(img):
    assert tc.plan_create(good(img), form())["title"] == "Lumi"
    assert "buyer_1" in tc.plan_transfer(LUMI, "buyer_1", tc.parse_inspect(TRANSFER_FORM), name="Lumi")["title"]
    assert "not for sale" in tc.plan_listing(LUMI, "nfs", None, None, tc.parse_inspect(EDIT_FORM))["title"]
    assert "OCs" in move_plan(1)["title"]


@pytest.mark.parametrize("opts,value,warn", [
    ([("user", "To a Toyhouse user"), ("offsite", "To an off-site user")], "user", False),
    ([("a", "Toyhouse user"), ("b", "Another user")], None, True),
])
def test_transfer_type_must_be_unambiguous(opts, value, warn):
    raw = json.loads(json.dumps(TRANSFER_FORM))
    raw["forms"][0]["fields"] = [{"tag": "input", "type": "radio", "name": "transfer_type", "label": t, "value": v}
                                 for v, t in opts] + [raw["forms"][0]["fields"][2]]
    plan = tc.plan_transfer(LUMI, "buyer_1", tc.parse_inspect(raw))
    f = {x["role"]: x for x in plan["targets"][0]["fields"]}
    assert (f.get("transfer_type") or {}).get("value") == value
    assert any("вариант передачи" in w for w in plan["warnings"]) is warn
    assert warn is ("вариант передачи" in tc.preview_transfer(plan))


def test_duplicate_folder_paths_are_ambiguous():
    folders = tc.folder_paths([{"value": "1", "text": "Adopts"}, {"value": "2", "text": "  Sold"},
                               {"value": "3", "text": "  Sold"}])
    got = tc.match_folder("Adopts/Sold", folders)
    assert got["value"] is None and len(got["suggestions"]) == 2


def test_folder_names_starting_with_dots_or_dashes_are_names():
    folders = tc.folder_paths([{"value": "1", "text": "Adopts"}, {"value": "2", "text": ".hidden"},
                               {"value": "3", "text": "-wip"}, {"value": "4", "text": "— Sold"},
                               {"value": "5", "text": "\u00a0\u00a0Old"}])
    assert [f["path"] for f in folders] == ["Adopts", ".hidden", "-wip", "-wip/Sold", "-wip/Old"]


@pytest.mark.parametrize("raw,price", [("1,500", 1500.0), ("1,500.50", 1500.5), ("12,5", 12.5), ("$25", 25.0),
                                       ("1 500", 1500.0)])
def test_price_with_commas(raw, price):
    assert tc._price(raw) == price


@pytest.mark.parametrize("raw", ["1,5000", "1,2,3", "nan", "inf"])
def test_price_rejects_unclear(raw):
    with pytest.raises(ValueError):
        tc._price(raw)


def test_default_image_check_speaks_about_toyhouse(tmp_path, monkeypatch):
    from mcp_devian import posting
    big = tmp_path / "big.png"
    big.write_bytes(b"x")
    monkeypatch.setattr(posting, "check_image",
                        lambda p: (None, "big.png больше 30 МБ — DeviantArt такой не примет."))
    out = tc.check_character({"name": "A", "images": [str(big)]})
    assert any("слишком большой" in e for e in out["errors"]) and not any("DeviantArt" in e for e in out["errors"])
