"""«Кому он может понравиться»: её тёплые контакты под новый адопт и черновики предложений её словами.

Каналы:
- da_mention — один комментарий под ЕЁ работой с @никами, только покупателям и людям из пинглиста
  (они уже знают её и сами были у неё). Черновик 'da_offer', уходит только по её «го».
- da_message_manual — ставили на ЕЁ лоты: она пишет сама в DA Messages. Первое сообщение без ссылок
  (ссылки от незнакомых в DA Messages некликабельны, и это похоже на спам): «ссылка — в профиле».
Личные предложения (отметка или личка) — только тем, кто у неё покупал, ставил на ЕЁ лоты или сам вступил
в пинглист (DM_ROLES). Кто только фавнул, комментировал или писал LF — им ничего лично не готовим:
DA считает такие сообщения незваной рекламой.
- x_reply — только для LF-постов из X, которые она сама вставила (leads), и контактов только из X.

Отметка под работой обещает «ответь stop — больше не отмечу». Это обещание держит watch_offer_replies:
фоновая проверка читает комментарии под работами, где ушли предложения, и «stop» сразу выключает человека.
Пока эта проверка не работала последние WATCH_FRESH_S, черновиков-отметок не делаем.

Лимиты: не больше per_day человек в сутки (жёстко ≤ 5) на все каналы вместе, одному человеку — не чаще
раза в cooldown дней, «stop» — навсегда. Похожие тексты подряд — предупреждение «поменяй пару слов».
"""

from __future__ import annotations

import difflib
import json
import re
import time
from typing import Any

from . import buyers, drafts, selling, taxonomy

DAY = 86400
HARD_MAX_PER_DAY = 5
MAX_MENTIONS = 5
ROLE_SCORE = {"buyer": 30, "bidder": 20, "pinglist": 15, "faver": 8, "th_lf": 5, "commenter": 5}
OFFER_KINDS = ("da_offer", "offer_manual")
OPT_OUT_HINT = "(reply “stop” and I won't tag you again · ответь stop — больше не отмечу)"
NO_LINK = "the link is in my profile"
# Любая ссылка, в том числе без https: linktr.ee/her, her.carrd.co, boosty.to. Домен верхнего уровня — буквы
# (цены вроде 12.50 и «e.g.» не трогаем).
URL_RE = re.compile(r"(?:https?://|www\.)\S+|(?<![\w@.-])[\w-]+(?:\.[\w-]+)*\.[a-z]{2,24}(?![\w-])(?:/\S*)?", re.I)
MANUAL_CHANNELS = ("da_message_manual", "th_message_manual", "x_reply", "da_lf_reply")
# Кому можно готовить личное предложение: покупал(а), ставил(а) на ЕЁ лоты, сам(а) вступил(а) в пинглист.
DM_ROLES = frozenset(buyers.DM_ROLES)
NOT_DM_ROLE = ("только фавнул(а) или комментировал(а) — личные предложения готовлю лишь покупателям, "
               "ставившим на твои лоты и пинглисту")

WATCH_DAYS = 30            # сколько дней после предложения читаем ответы под работой
WATCH_FRESH_S = 3 * DAY    # фоновая проверка ответов должна была пройти не позже этого
WATCH_RUN = "_run"
WATCH_FAIL = "_fail:"     # работа с отметками, ответы под которой прочитать не вышло (пока есть — отметок не делаем)
MENTION_WHY = "thought you might like it"

SCHEMA = """
-- Чтение ответов под работами с предложениями: deviation_id → когда читали ('_run' — последний полный проход).
CREATE TABLE IF NOT EXISTS outreach_watch (
    deviation_id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outreach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,        -- ник в нижнем регистре
    channel TEXT NOT NULL,         -- da_mention | da_message_manual | th_message_manual | x_reply | da_lf_reply
    ref TEXT,                      -- лот (da:<id>), черновик, лид (lead:<id>)
    text TEXT,
    status TEXT NOT NULL,          -- pending (черновик ей) | sent (она подтвердила) | draft (в черновике da_offer)
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outreach_log_user ON outreach_log(username, ts);
"""


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    buyers.ensure(conn)


def _per_day(per_day: int | None) -> int:
    return max(0, min(HARD_MAX_PER_DAY, int(HARD_MAX_PER_DAY if per_day is None else per_day)))


def opted_out(conn) -> set[str]:
    """Ники (нижний регистр) всех, кто просил не писать: для проверки прямо перед отправкой da_offer."""
    ensure(conn)
    return {r[0] for r in conn.execute("SELECT username FROM contacts WHERE opted_out_ts IS NOT NULL")}


# --- Лимиты ---

def _pending_mentions(conn, now: float) -> list[str]:
    """Ники во всех живых черновиках da_offer: ждут «го» (живут сутки), запланированы (до недели)
    или отправляются прямо сейчас — все они ещё могут уйти."""
    out: list[str] = []
    for r in conn.execute("SELECT payload FROM drafts WHERE kind = 'da_offer' AND ("
                          "(status = 'pending' AND created_ts >= ?) OR (status = 'scheduled' AND created_ts >= ?) "
                          "OR status = 'sending')", (now - drafts.DRAFT_TTL_S, now - drafts.SCHEDULE_MAX_AGE_S)):
        try:
            out += [buyers.key(u) for u in json.loads(r["payload"]).get("users") or []]
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return out


def _pending_manual(conn, since: float, ignore_ref: str | None = None) -> list[str]:
    return [r["username"] for r in conn.execute(
        "SELECT username FROM outreach_log WHERE status = 'pending' AND ts >= ? AND COALESCE(ref, '') != ?",
        (since, ignore_ref or "\0"))]


def last_drafted(conn, user: str, ignore_ref: str | None = None) -> float | None:
    """Когда ей последний раз готовили личное сообщение/ответ этому человеку (pending — она могла отправить
    и не отметить, sent — отметила). Неподтверждённый черновик считаем «может быть, ушёл»."""
    return conn.execute("SELECT MAX(ts) FROM outreach_log WHERE username = ? AND status IN ('pending', 'sent') "
                        "AND COALESCE(ref, '') != ?", (buyers.key(user), ignore_ref or "\0")).fetchone()[0]


def used_today(conn, now: float | None = None) -> int:
    """Сколько мест из суточного лимита занято: ушедшие предложения (da_offer, offer_manual) за 24 ч +
    черновики, которые ещё ждут её «го» или её отправки."""
    now = now or time.time()
    ensure(conn)
    since = now - DAY
    sent = conn.execute(f"SELECT COUNT(*) FROM sent_log WHERE ok = 1 AND ts >= ? AND kind IN "
                        f"({','.join('?' * len(OFFER_KINDS))})", (since, *OFFER_KINDS)).fetchone()[0]
    return int(sent) + len(_pending_mentions(conn, now)) + len(_pending_manual(conn, since))


def busy_users(conn, now: float) -> set[str]:
    """У кого уже лежит черновик предложения (ещё не ушёл) — второй не готовим."""
    return set(_pending_mentions(conn, now)) | set(_pending_manual(conn, now - DAY))


def person_block(conn, user: str, now: float, cooldown_days: int = buyers.COOLDOWN_DAYS,
                 ignore_ref: str | None = None) -> str | None:
    """Почему этому человеку сейчас нельзя писать: stop, младше 18, пауза после её письма, черновик уже есть.
    ignore_ref — не считать черновик с этим ref (тот же лид, который сейчас показываем снова)."""
    ensure(conn)
    k = buyers.key(user)
    c = buyers.contact(conn, k)
    bad = buyers.bad_deal(conn, k)
    if bad:
        return bad
    if c:
        if c.get("opted_out_ts"):
            return "просил(а) не писать"
        if c.get("is_minor") == 1:
            return "младше 18 по данным DeviantArt"
        lc = c.get("last_contact_ts")
        if lc and now - lc < cooldown_days * DAY:
            return f"ты писала {int((now - lc) // DAY)} дн назад (пауза {cooldown_days} дн)"
    last = conn.execute("SELECT MAX(ts) FROM sent_log WHERE ok = 1 AND target = ? AND kind IN ('da_offer', "
                        "'offer_manual')", (k,)).fetchone()[0]
    if last and now - last < cooldown_days * DAY:
        return f"ты писала {int((now - last) // DAY)} дн назад (пауза {cooldown_days} дн)"
    if k in set(_pending_mentions(conn, now)) or k in set(_pending_manual(conn, now - DAY, ignore_ref)):
        return "черновик для него(неё) уже есть"
    drafted = last_drafted(conn, k, ignore_ref)
    if drafted and now - drafted < cooldown_days * DAY:
        return (f"текст ему(ей) готовила {int((now - drafted) // DAY)} дн назад — могла отправить, не отметив "
                f"(пауза {cooldown_days} дн)")
    return None


# --- Ранжирование ---

def resolve_item(conn, ref: str):
    """da:<id>, номер, ссылка на её работу DeviantArt → строка items (или None)."""
    from . import sales

    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("http"):
        iid = sales.lot_id_for_url(conn, ref)
    elif ref.startswith(("da:", "th:")):
        iid = ref
    elif ref.isdigit():
        iid = f"da:{ref}"
    else:
        return None
    return conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone() if iid else None


_RU_ENDINGS = ("ами", "ями", "ов", "ев", "ей", "ом", "ем", "ам", "ям", "ах", "ях", "а", "я", "у", "ю", "ы", "и", "е")


def _word_tags(words: list[str]) -> list[str]:
    """Теги по словам. Русское слово не в той форме («про дракона») — пробуем без падежного окончания."""
    tags = buyers.tags_flat(taxonomy.tag_text(" ".join(words), words))
    if tags:
        return tags
    stems = []
    for w in words:
        lw = w.lower()
        for end in _RU_ENDINGS:
            if len(lw) > len(end) + 2 and lw.endswith(end) and re.search("[а-яё]", lw):
                stems.append(lw[:-len(end)])
                break
    return buyers.tags_flat(taxonomy.tag_text(" ".join(stems), stems)) if stems else []


def adopt_tags(conn, item_ref_or_tags) -> tuple[list[str], Any]:
    """(плоские теги «species:moth», строка items или None). Можно передать список тегов или строку слов."""
    if isinstance(item_ref_or_tags, (list, tuple)):
        words = [str(t) for t in item_ref_or_tags]
        if all(":" in w for w in words):
            return words, None
        return _word_tags(words), None
    row = resolve_item(conn, str(item_ref_or_tags))
    if row is not None:
        return buyers.tags_flat(taxonomy.tag_item(row, use_snippet=False)), row
    text = str(item_ref_or_tags or "")
    return (_word_tags(text.split()) if text and not text.startswith("http") else []), None


def _spend(conn, username: str) -> int:
    """Сколько сделок он(а) оплатил(а): предоплата и остаток одного заказа — одна сделка."""
    from . import sales

    rows = conn.execute("SELECT id, item_id FROM sales WHERE LTRIM(LOWER(TRIM(buyer)), '@') = ? AND status IN "
                        f"({','.join('?' * len(buyers.PAID_STATUSES))})", (username, *buyers.PAID_STATUSES))
    return len({sales.deal_key(r) for r in rows})


def _why(c: dict, overlap: list[str], paid: int) -> str:
    words = [t.split(":", 1)[-1] for t in overlap[:3]]
    roles = c["roles"]
    like = (" — " + ", ".join(words)) if words else ""
    if "buyer" in roles and paid:
        head = f"брал(а) у тебя {paid} {'лот' if paid == 1 else 'лота' if paid < 5 else 'лотов'}{like}"
    elif "buyer" in roles:
        head = f"брал(а) у тебя{like}"
    elif "bidder" in roles:
        head = f"ставил(а) на твои лоты{like}"
    elif "pinglist" in roles:
        head = f"сам(а) просил(а) пинговать{like}"
    elif "faver" in roles:
        head = f"фавнул(а) твои лоты{like}"
    else:
        head = f"был(а) у тебя{like}"
    return head


def why_for_buyer(c: dict) -> str:
    """{why} в тексте для покупателя — по-английски и только о его действиях у неё."""
    words = ", ".join(t.split(":", 1)[-1] for t in (c.get("overlap") or [])[:2])
    roles = c.get("roles") or []
    if "buyer" in roles:
        return f"you adopted my {words} designs before" if words else "you adopted from me before"
    if "bidder" in roles:
        return f"you bid on my {words} adopts" if words else "you bid on my adopts"
    if "pinglist" in roles:
        return "you asked to be pinged"
    if "faver" in roles:
        return f"you faved my {words} adopts" if words else "you faved my adopts"
    return ""


def channel_for(contact: dict) -> str | None:
    """da_mention — только покупатели и пинглист НА DeviantArt (комментарий под ЕЁ работой); ставили на её лоты
    DA — da_message_manual (пишет сама); контакт только из X (лиды) — x_reply; покупал(а) или ставил(а) на
    Toyhouse — th_message_manual. Роль с другой площадки на DA не переносим: тот же ник там может быть чужим.
    Только фав, комментарий, LF или покупка вне площадок (other) — канала нет (None)."""
    plats = set(contact.get("platforms") or [])
    if plats == {"x"}:
        return "x_reply"
    dm = buyers.dm_roles(contact)
    da = dm.get("deviantart") or set()
    if da & {"buyer", "pinglist"}:
        return "da_mention"
    if "bidder" in da:
        return "da_message_manual"
    if dm.get("toyhouse"):
        return "th_message_manual"
    return None


def dm_allowed(conn, contact: dict) -> bool:
    """Можно ли готовить ему(ей) личное предложение: роль из DM_ROLES (по строке или по базе)."""
    roles = contact.get("roles")
    if roles is None:
        known = buyers.contact(conn, contact.get("user") or contact.get("username") or "")
        roles = (known or {}).get("roles") or []
    return bool(set(roles) & DM_ROLES)


def rank_for_adopt(conn, item_ref_or_tags, now: float | None = None, limit: int = 10,
                   window_days: int = buyers.WINDOW_DAYS, cooldown_days: int = buyers.COOLDOWN_DAYS) -> list[dict]:
    """Кому из её тёплых контактов может понравиться этот адопт. Порядок: совпадение интересов (по тому,
    что брали/ставили/фавнули), свежесть, роль (покупатель > ставил > пинглист > фавнул), прошлые покупки.
    Без совпадений по тегам (и когда теги адопта не распознаны) — только люди из пинглиста (они сами
    просили о новых работах); тогда у строк tags_unknown = True."""
    now = now or time.time()
    ensure(conn)
    tags, row = adopt_tags(conn, item_ref_or_tags)
    tagset = set(tags)
    out = []
    for c in buyers.eligible(conn, now, window_days, cooldown_days):
        overlap = sorted((t for t in c["interests"] if t in tagset), key=lambda t: (-c["interests"][t], t))
        if not overlap and "pinglist" not in c["roles"]:
            continue
        paid = _spend(conn, c["username"])
        age = (now - (c["last_interaction_ts"] or now)) / DAY
        score = (sum(c["interests"][t] for t in overlap) * 5
                 + max((ROLE_SCORE.get(r, 0) for r in c["roles"]), default=0)
                 + max(0.0, 10 * (1 - age / max(1, window_days)))
                 + min(15, 5 * paid))
        out.append({"user": c["display"], "username": c["username"], "score": round(score, 1),
                    "why": _why(c, overlap, paid), "overlap": overlap, "roles": c["roles"],
                    "platforms": c["platforms"], "dm_roles": c.get("dm_roles") or {}, "channel": channel_for(c),
                    "age_unknown": c["age_unknown"],
                    "item_id": row["id"] if row is not None else None, "tags_unknown": not tagset})
    out.sort(key=lambda x: (-x["score"], x["username"]))
    return [x for x in out if x["channel"]][:max(1, limit)]


# --- Тексты ---

def _strip_urls(text: str) -> tuple[str, bool]:
    new = URL_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", new).strip(), new != text


def render_offer(template: str, *, name: str, title: str, link: str, why: str) -> str:
    return selling.render_template(template, {"name": name, "title": title, "link": link, "why": why}).strip()


def recent_texts(conn, limit: int = 20) -> list[str]:
    ensure(conn)
    return [r["text"] for r in conn.execute(
        "SELECT text FROM outreach_log WHERE text IS NOT NULL AND text != '' ORDER BY ts DESC, id DESC LIMIT ?",
        (limit,))]


def too_similar(text: str, recent: list[str], ratio: float = 0.9) -> bool:
    """Текст почти такой же, как одно из последних предложений: X и DA считают повторы спамом."""
    t = (text or "").strip().lower()
    return bool(t) and any(difflib.SequenceMatcher(None, t, (r or "").strip().lower()).ratio() >= ratio
                           for r in recent)


def _log(conn, user: str, channel: str, ref: str | None, text: str, status: str, now: float) -> None:
    conn.execute("INSERT INTO outreach_log (username, channel, ref, text, status, ts) VALUES (?, ?, ?, ?, ?, ?)",
                 (buyers.key(user), channel, ref, text, status, now))


REPLIES_UNWATCHED = ("ответы «stop» под работой сейчас никто не читает (нужна фоновая проверка с подключённым "
                     "DeviantArt) — отметку с обещанием «больше не отмечу» не готовлю")
SIMILAR_WARNING = "тексты почти как прошлые предложения — перед отправкой поменяй пару слов в каждом (повторы = спам)"
TH_TRANSLATED = ("её шаблон offer — перевод от ИИ, а на Toyhouse такие тексты запрещены (правило 4.8): "
                 "для Toyhouse нужен текст её словами")
NO_TEMPLATE = ("нет её шаблона предложения (kind offer, слоты {name} {title} {link} {why}) — "
               "без её текста ничего не готовлю")


def drafts_for_adopt(conn, item, contacts: list[dict], template: str | None = None, now: float | None = None,
                     per_day: int = HARD_MAX_PER_DAY, cooldown_days: int = buyers.COOLDOWN_DAYS) -> dict:
    """Черновики предложений её шаблоном 'offer'. Ничего не отправляет.

    {"mention_draft_id": id черновика da_offer или None, "mention_users": [...], "mention_text": str,
     "manual": [{"user", "channel", "text"}], "skipped": [(user, причина)], "warnings": [...], "left": мест}"""
    now = now or time.time()
    ensure(conn)
    res: dict[str, Any] = {"mention_draft_id": None, "mention_users": [], "mention_text": "", "manual": [],
                           "skipped": [], "warnings": [], "left": 0}
    template = template if template is not None else selling.get_template(conn, "offer")
    if not (template or "").strip():
        res["warnings"].append(NO_TEMPLATE)
        res["skipped"] = [(c["user"], "нет её шаблона") for c in contacts]
        return res
    assert template is not None
    left = max(0, _per_day(per_day) - used_today(conn, now))
    res["left"] = left
    own = bool(item is not None and item["own"])
    if not own:
        # Предлагаем только ЕЁ адопты — ни отметок, ни личных сообщений про чужую работу.
        res["skipped"] = [(c["user"], "предлагаю только под ЕЁ работой — дай ссылку на свою работу")
                          for c in contacts]
        return res
    item_id = item["id"]
    title = item["title"] or ""
    url = item["url"] or ""
    watched = replies_watched(conn, now)
    pinged: set[str] = set()
    if item_id:
        from . import pinglist

        pinglist.ensure(conn)
        pinged = {r["key"] for r in conn.execute(
            "SELECT key FROM pinglist_sent WHERE deviation_id = ? UNION SELECT key FROM pinglist_pending "
            "WHERE deviation_id = ?", (item_id, item_id))}
    mention: list[str] = []
    recent = recent_texts(conn)
    similar = False
    for c in contacts:
        user = c["user"]
        if not dm_allowed(conn, c):
            res["skipped"].append((user, NOT_DM_ROLE))
            continue
        block = person_block(conn, user, now, cooldown_days)
        if block:
            res["skipped"].append((user, block))
            continue
        if buyers.key(user) in pinged:
            res["skipped"].append((user, "уже в пинге пинглиста под этой работой"))
            continue
        if left <= 0:
            res["skipped"].append((user, f"лимит предложений на сутки ({_per_day(per_day)}) исчерпан"))
            continue
        ch = c.get("channel") or channel_for(c)
        if ch == "da_mention":
            if not watched:
                res["skipped"].append((user, REPLIES_UNWATCHED))
                continue
            if len(mention) >= MAX_MENTIONS:
                res["skipped"].append((user, f"в одном комментарии не больше {MAX_MENTIONS} ников"))
                continue
            mention.append(user)
            left -= 1
        elif ch in ("da_message_manual", "th_message_manual"):
            # Первое сообщение — без ссылок: DA Messages не делает их кликабельными, а выглядит как спам.
            # На Toyhouse — только её слова: {why} помощника не вписываем, перевод от ИИ не пускаем (правило 4.8).
            why = "" if ch == "th_message_manual" else why_for_buyer(c)
            text, had_url = _strip_urls(render_offer(template, name=user, title=title, link=NO_LINK, why=why))
            if ch == "th_message_manual":
                from . import translate_guard

                th_block = translate_guard.check_toyhouse(conn, text) or translate_guard.check_toyhouse(conn, template)
                if th_block:
                    res["skipped"].append((user, TH_TRANSLATED))
                    continue
            if had_url:
                res["warnings"].append("из личного сообщения убрала ссылку: в первом сообщении ссылок не надо")
            similar = similar or too_similar(text, recent)
            recent.insert(0, text)
            _log(conn, user, ch, item_id, text, "pending", now)
            res["manual"].append({"user": user, "channel": ch, "text": text, "why": c.get("why", ""),
                                  "age_unknown": c.get("age_unknown", False)})
            left -= 1
        else:
            res["skipped"].append((user, "нет подходящего канала"))
    if mention:
        names = " ".join(f"@{u}" for u in mention)
        body = render_offer(template, name=names, title=title, link=url, why=MENTION_WHY)
        if "{name}" not in template:
            body = f"{body}\n{names}"
        text = f"{body}\n{OPT_OUT_HINT}"
        similar = similar or too_similar(text, recent)
        preview = (f"Комментарий под твоей работой «{title}» ({url}), отметит {len(mention)}: "
                   f"{', '.join(mention)}\n---\n{text}\n---\n"
                   f"Текст: её шаблон; помощник вписал {{why}} = «{MENTION_WHY}» и последнюю строку про stop.")
        payload = {"title": f"предложение: {title or url}", "deviation_id": item_id, "deviation_url": url,
                   "users": mention, "text": text}
        uuid = buyers._json_dict(item["extra"]).get("uuid") if item is not None else None
        if uuid:
            payload["deviation_uuid"] = uuid
        draft_id = drafts.create(conn, "da_offer", payload, preview, now)
        for u in mention:
            _log(conn, u, "da_mention", draft_id, text, "draft", now)
        res.update(mention_draft_id=draft_id, mention_users=mention, mention_text=text)
    if similar:
        res["warnings"].append(SIMILAR_WARNING)
    res["left"] = left
    conn.commit()
    return res


def mark_contacted(conn, users, channel: str, ref: str = "", now: float | None = None) -> list[str]:
    """Она подтвердила, что написала сама (DA Messages, X, Toyhouse): пауза для этих людей начинается сейчас.
    ref = «lead:<id>» — лид отмечается как contacted."""
    now = now or time.time()
    ensure(conn)
    names = [users] if isinstance(users, str) else list(users or [])
    done = []
    for u in names:
        k = buyers.key(u)
        if not k:
            continue
        buyers._upsert_contact(conn, str(u), "x" if channel == "x_reply" else None)
        conn.execute("UPDATE contacts SET last_contact_ts = ? WHERE username = ?", (now, k))
        cur = conn.execute("UPDATE outreach_log SET status = 'sent', ts = ? WHERE username = ? AND status = 'pending'",
                           (now, k))
        if not cur.rowcount:
            _log(conn, str(u), channel, ref or None, "", "sent", now)
        drafts.log_sent(conn, "offer_manual", k, f"{channel}:{ref}" if ref else channel, True, "", now)
        done.append(str(u).strip().lstrip("@"))
    if ref.startswith("lead:") and _has_table(conn, "leads"):
        conn.execute("UPDATE leads SET status = 'contacted' WHERE id = ? AND status != 'dismissed'",
                     (ref.split(":", 1)[1],))
    conn.commit()
    return done


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def render_for_adopt(ranked: list[dict], res: dict, cooldown_days: int = buyers.COOLDOWN_DAYS) -> str:
    """Ответ для buyers:for_adopt — кто, почему, черновик для «го» и тексты, которые она отправит сама."""
    if not ranked:
        return ("Пока некому предложить: среди тех, кто у неё покупал, ставил на её лоты или в пинглисте, нет "
                "любителей похожего (или всем она уже писала недавно); кто только фавнул — лично не пишем. "
                "Вывода нет.")
    lines = ["Кому может понравиться (порядок — эвристика: интересы, свежесть, роль; не вероятность покупки):"]
    if ranked[0].get("tags_unknown"):
        lines.append("Теги адопта не распознаны (вид, стиль) — предлагаю только людям из пинглиста.")
    for r in ranked:
        flag = " · возраст неизвестен" if r.get("age_unknown") else ""
        lines.append(f"· {r['user']} — {r['why']} [{r['channel']}]{flag}")
    if res.get("mention_draft_id"):
        lines += ["", f"Черновик {res['mention_draft_id']} (комментарий под твоей работой, ждёт «го»):",
                  res["mention_text"]]
    if res.get("manual"):
        lines += ["", "Написать самой (личные сообщения, без ссылок — ссылка в профиле):"]
        for m in res["manual"]:
            lines.append(f"→ {m['user']} ({m['channel']}): {m['text']}")
        lines.append(f"Отправила — скажи, кому: отмечу, чтобы не писать им снова {cooldown_days} дн.")
    if res.get("skipped"):
        lines += ["", "Пропущены: " + "; ".join(f"{u} — {why}" for u, why in res["skipped"][:10])]
    for w in res.get("warnings") or []:
        lines.append(f"! {w}")
    return "\n".join(lines)


# --- Ответы под работами с предложениями: «stop» → больше не предлагаем ---

def note_watch_run(conn, now: float | None = None) -> None:
    ensure(conn)
    conn.execute("INSERT INTO outreach_watch (deviation_id, ts) VALUES (?, ?) "
                 "ON CONFLICT(deviation_id) DO UPDATE SET ts = excluded.ts", (WATCH_RUN, now or time.time()))
    conn.commit()


def replies_watched(conn, now: float | None = None) -> bool:
    """Фоновая проверка ответов недавно прошла без ошибок и ни одну работу с отметками сейчас не прочитать
    не может — обещанию «ответь stop» можно верить."""
    now = now or time.time()
    ensure(conn)
    row = conn.execute("SELECT ts FROM outreach_watch WHERE deviation_id = ?", (WATCH_RUN,)).fetchone()
    failing = conn.execute("SELECT 1 FROM outreach_watch WHERE deviation_id LIKE ? LIMIT 1",
                           (WATCH_FAIL + "%",)).fetchone()
    return bool(row and now - row["ts"] <= WATCH_FRESH_S and not failing)


def offer_reply_targets(conn, now: float | None = None, days: int = WATCH_DAYS) -> list[dict]:
    """Её работы, под которыми за days дней ушли (или, может быть, ушли) отметки da_offer.
    Сначала те, что дольше всего не читали (среди ни разу не читанных — самые свежие предложения).
    [{"deviation_id", "uuid", "url", "users", "sent_ts"}]"""
    now = now or time.time()
    ensure(conn)
    seen = {r["deviation_id"]: r["ts"] for r in conn.execute("SELECT deviation_id, ts FROM outreach_watch")}
    failing = {k[len(WATCH_FAIL):] for k in seen if k.startswith(WATCH_FAIL)}
    by_dev: dict[str, dict] = {}
    for r in conn.execute("SELECT payload, COALESCE(sent_ts, created_ts) AS ts FROM drafts WHERE kind = 'da_offer' "
                          "AND status IN ('sent', 'unknown', 'sending') AND COALESCE(sent_ts, created_ts) >= ?",
                          (now - days * DAY,)):
        try:
            p = json.loads(r["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(p, dict):
            continue
        dev = str(p.get("deviation_id") or p.get("deviation_uuid") or p.get("deviation_url") or "")
        if not dev:
            continue
        t = by_dev.setdefault(dev, {"deviation_id": dev, "uuid": p.get("deviation_uuid"),
                                    "url": p.get("deviation_url") or "", "users": [], "sent_ts": r["ts"]})
        t["uuid"] = t["uuid"] or p.get("deviation_uuid")
        t["sent_ts"] = max(t["sent_ts"], r["ts"])
        t["users"] += [u for u in p.get("users") or [] if u not in t["users"]]
    for t in by_dev.values():
        if not t["uuid"]:
            row = conn.execute("SELECT extra, url FROM items WHERE id = ?", (t["deviation_id"],)).fetchone()
            if row:
                t["uuid"] = buyers._json_dict(row["extra"]).get("uuid")
                t["url"] = t["url"] or row["url"] or ""
    # Сначала те, что в прошлый раз прочитать не вышло (пока они не прочитаны, отметок не делаем).
    return sorted(by_dev.values(), key=lambda t: (t["deviation_id"] not in failing, seen.get(t["deviation_id"], 0),
                                                  -t["sent_ts"], t["deviation_id"]))


def watch_offer_replies(conn, client, now: float | None = None, owner: str = "", max_works: int = 2) -> dict:
    """Фон: комментарии под её работами с отметками-предложениями (только чтение, низкий приоритет DA).
    Любой «stop» / «no thanks» / «не тегай» под её работой → buyers.opt_out (через pinglist.ingest).
    Лимит или нет входа — стоп без отметки о проходе. Ошибка одной работы — дальше по кругу, но проход
    не засчитывается и работа помечается WATCH_FAIL: пока её ответы не прочитаны, новых отметок не готовим.
    Удалённая работа (404) не держит: отвечать под ней некому."""
    from . import da_access, da_api, pinglist

    now = now or time.time()
    ensure(conn)
    stats: dict[str, Any] = {"works": 0, "opted_out": 0, "errors": 0, "stopped": None}
    if not buyers.key(owner):
        stats["stopped"] = "no_owner"
        return stats
    before = opted_out(conn)
    targets = offer_reply_targets(conn, now)
    # Работы, ушедшие из окна чтения (старше WATCH_DAYS), больше не держат отметки.
    live = {WATCH_FAIL + t["deviation_id"] for t in targets}
    for r in conn.execute("SELECT deviation_id FROM outreach_watch WHERE deviation_id LIKE ?",
                          (WATCH_FAIL + "%",)).fetchall():
        if r["deviation_id"] not in live:
            conn.execute("DELETE FROM outreach_watch WHERE deviation_id = ?", (r["deviation_id"],))
    conn.commit()
    with da_access.task("buyers", low=True):
        for t in targets[:max(0, int(max_works))]:
            stats["works"] += 1
            gone = False
            try:
                uuid = t["uuid"] or (client.deviation_uuid_from_url(t["url"], max_pages=2) if t["url"] else None)
                comments = client.deviation_comments(uuid, max_comments=200) if uuid else None
            except (da_api.RateLimited, da_api.NotConnected) as e:
                stats["stopped"] = type(e).__name__
                break
            except da_api.DAApiError as e:
                comments = None
                gone = getattr(e, "status", None) == 404  # работу удалили: отвечать под ней уже некому
            fail_key = WATCH_FAIL + t["deviation_id"]
            if comments is None and not gone:
                stats["errors"] += 1
                conn.execute("INSERT INTO outreach_watch (deviation_id, ts) VALUES (?, ?) "
                             "ON CONFLICT(deviation_id) DO UPDATE SET ts = excluded.ts", (fail_key, now))
            else:
                if comments is not None:
                    pinglist.ingest(conn, t["deviation_id"], list(comments), owner=owner, now=now)
                conn.execute("DELETE FROM outreach_watch WHERE deviation_id = ?", (fail_key,))
            # Отметка о чтении — и для неудачной работы, чтобы очередь шла по кругу; неудача держит WATCH_FAIL.
            conn.execute("INSERT INTO outreach_watch (deviation_id, ts) VALUES (?, ?) "
                         "ON CONFLICT(deviation_id) DO UPDATE SET ts = excluded.ts", (t["deviation_id"], now))
            conn.commit()
    stats["opted_out"] = len(opted_out(conn) - before)
    if not stats["stopped"] and not stats["errors"]:
        note_watch_run(conn, now)
    return stats
