"""Пост в X про её лот: её шаблон, ссылка на окно поста, путь к картинке — нажимает Post она сама.

Два варианта (влияние ссылки на охват спорно: разборы кода X говорят «режет», X это отрицает):
  a — картинка и текст, ссылка на лот — в её первом ответе под постом;
  b — ссылка прямо в посте.
Какой вариант она выложила, отмечает она сама (confirm_posted) — и variant_report сравнивает их по её
продажам, а пока данных мало, честно говорит «вывода нет».

Хэштеги: её 2–3 основных плюс нишевые только из её же списка разрешённых, всего не больше 4
(правила X: «excessive, unrelated hashtags» — манипуляция). RT-аккаунты @упоминаем, только если она
записала, что аккаунт сам просит отмечать (tag_ok); иначе показываем её заметку, как он просит.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from . import evidence, taxonomy, xintent

SCHEMA = """
CREATE TABLE IF NOT EXISTS x_posts (
    id TEXT PRIMARY KEY,
    item_id TEXT,
    variant TEXT NOT NULL,         -- a (ссылка в ответе) | b (ссылка в посте)
    text TEXT NOT NULL,            -- текст поста, как в окне X
    created_ts REAL NOT NULL,
    posted_ts REAL,                -- она отметила, что выложила (confirm_posted)
    post_url TEXT,
    reply_text TEXT,               -- вариант a: что написать первым ответом (ссылка)
    push TEXT                      -- first (первый пост о лоте) | repromote (второй заход: лот уже не продался)
);
"""
TEMPLATE_KIND = "x_post"
TEMPLATE_SLOTS = {"x_post": ("title", "url", "price", "species")}
VARIANTS = ("a", "b")
DEFAULT_TAGS_MAX = 3
REPORT_DAYS = 180
OUTCOME_DAYS = 14          # лот без продажи и без известного итога через столько дней после поста — «не продан»
SLOT_RE = re.compile(r"\{(\w*)\}")
POST_URL = re.compile(r"^https://(?:www\.|mobile\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/(\d{5,25})(?:[/?#].*)?$")
NICHE_NOTE = "нишевые хэштеги подобраны по виду/стилю из названия и тегов лота — эвристика, не данные"
LINK_NOTE = ("Где ссылка лучше — в посте (b) или в первом ответе (a), — спорно: X цифр не публикует. "
             "Отметь, какой вариант выложила, и по её продажам будет видно (promo:x_posted).")


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    if "push" not in {r[1] for r in conn.execute("PRAGMA table_info(x_posts)")}:
        conn.execute("ALTER TABLE x_posts ADD COLUMN push TEXT")


def next_variant(conn, push: str = "first") -> str:
    """Вариант по очереди внутри одного вида захода (первый пост / второй заход): тот, что выкладывали реже.
    Так варианты не путаются с тем, продавался ли лот раньше."""
    ensure(conn)
    counts = dict(conn.execute("SELECT variant, COUNT(*) FROM x_posts WHERE posted_ts IS NOT NULL "
                               "AND COALESCE(push, 'first') = ? GROUP BY variant", (push,)).fetchall())
    return "b" if counts.get("b", 0) < counts.get("a", 0) else "a"


def _get(item, key: str, default=None):
    try:
        value = item[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _niche_words() -> list[str]:
    return [w for w in (*taxonomy.SPECIES, *taxonomy.STYLES) if len(w) >= 3]


def prefs(posting) -> tuple[list[str], list[str]]:
    """(её основные хэштеги, её нишевые) из настройки x_hashtags (список, ≤4, или {default, allowed}).

    В списке нишевые — те, где есть вид или стиль («dragonadopt»): они идут, только если лот такой."""
    raw = getattr(posting, "x_hashtags", None) or []
    if isinstance(raw, dict):
        return list(raw.get("default") or []), list(raw.get("allowed") or [])
    words = _niche_words()
    tags = xintent.sanitize_hashtags(raw, 50)
    niche = [t for t in tags if any(w in t.lower() for w in words)]
    return [t for t in tags if t not in niche][:DEFAULT_TAGS_MAX], niche


def pick_hashtags(item, hashtags_pref, allowed_tags) -> tuple[list[str], list[str]]:
    """(хэштеги, заметки): её основные (до 3) + нишевые из её разрешённых по виду/стилю лота, всего ≤ 4."""
    base = xintent.sanitize_hashtags(hashtags_pref, DEFAULT_TAGS_MAX)
    words = []
    if item is not None:
        t = taxonomy.tag_item(item)
        words = [w.casefold() for w in [*t.species, *t.styles] if w]
    picked: list[str] = []
    for tag in xintent.sanitize_hashtags(allowed_tags, 50):
        low = tag.casefold()
        if any(w in low for w in words) and low not in {b.casefold() for b in base}:
            picked.append(tag)
    tags = xintent.sanitize_hashtags([*base, *picked], xintent.MAX_HASHTAGS)
    notes = [NICHE_NOTE] if any(t in picked for t in tags) else []
    return tags, notes


def _render(template: str, values: dict) -> str:
    text = SLOT_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.strip().split("\n"))


def _last_text(conn) -> str | None:
    row = conn.execute("SELECT text FROM x_posts ORDER BY (posted_ts IS NULL), COALESCE(posted_ts, created_ts) DESC "
                       "LIMIT 1").fetchone()
    return row[0] if row else None


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _species(item) -> str:
    if item is None:
        return ""
    return ", ".join(taxonomy.tag_item(item).species[:2])


def build(conn, item, template: str, hashtags_pref=(), allowed_tags=(), variant: str = "a", image_path: str = "",
          rt_accounts=(), now: float | None = None, push: str = "first") -> dict:
    """Пост для X: {ok, id, text, intent_url, image_path, length, warnings, notes, hashtags, first_reply_text}.

    Ничего не публикует: ссылку она открывает сама, картинку прикладывает сама, Post жмёт сама."""
    ensure(conn)
    if item is None or not _get(item, "own", False):
        return {"ok": False, "reason": "Пост в X готовлю только про ЕЁ лот — это не её работа (или её нет в базе)."}
    variant = (variant or "a").strip().lower()
    if variant not in VARIANTS:
        return {"ok": False, "reason": "Вариант: a (ссылка в первом ответе) или b (ссылка в посте)."}
    if not str(template or "").strip():
        return {"ok": False, "reason": "Нет её шаблона поста для X: попроси текст её словами и сохрани "
                                       "set_template('x_post', …), слоты {title} {url} {price} {species}."}
    unknown = sorted({s for s in SLOT_RE.findall(template) if s not in TEMPLATE_SLOTS[TEMPLATE_KIND]})
    if unknown:
        return {"ok": False, "reason": f"В шаблоне неизвестные поля: {', '.join('{' + u + '}' for u in unknown)}."}
    url = str(_get(item, "url", "") or "")
    values = {"title": _get(item, "title", ""), "price": _get(item, "price_text", ""), "species": _species(item),
              "url": url if variant == "b" else ""}
    text = _render(template, values)
    warnings: list[str] = []
    notes: list[str] = []
    tags, tag_notes = pick_hashtags(item, hashtags_pref, allowed_tags)
    notes += tag_notes
    mentions = []
    for acc in rt_accounts or ():
        handle = str((acc or {}).get("handle") or "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            continue
        if (acc or {}).get("tag_ok") is True:
            mentions.append("@" + handle)
        else:
            how = str((acc or {}).get("note") or (acc or {}).get("method") or "как он просит — не записано")
            notes.append(f"RT-аккаунт {handle}: не отмечаю (она не записала, что он просит отмечать). Её заметка: {how}")
    if mentions:
        text = f"{text}\n{' '.join(mentions)}"
    link_param = url if variant == "b" and "{url}" not in template else ""
    if variant == "a" and "{url}" in template:
        notes.append("в шаблоне есть {url}: в варианте a ссылка уходит в первый ответ, в посте её нет")
    full = xintent.composed(text, link_param, tags)
    length = xintent.weighted_len(full)
    if length > xintent.WARN_AT:
        warnings.append(f"длина ~{length} из {xintent.MAX_LEN} (считаю с запасом) — X может не дать отправить, "
                        "пусть сократит")
    last = _last_text(conn)
    if last is not None and _norm(last) == _norm(text):
        warnings.append("такой же текст, как в её прошлом посте X: одинаковые посты X считает спамом — "
                        "пусть скажет иначе")
    if not url:
        warnings.append("у лота нет ссылки — некуда вести покупателя")
    image = str(image_path or "")
    if image and not Path(image).expanduser().is_file():
        warnings.append(f"картинки {image} нет — пусть выберет файл заново")
    notes.append(LINK_NOTE)
    reply = url if variant == "a" and url else ""
    post_id = "x" + uuid.uuid4().hex[:7]
    conn.execute("INSERT INTO x_posts (id, item_id, variant, text, created_ts, reply_text, push) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (post_id, _get(item, "id"), variant, text, now or time.time(), reply or None,
                  "repromote" if push == "repromote" else "first"))
    conn.commit()
    return {"ok": True, "id": post_id, "variant": variant, "text": text,
            "intent_url": xintent.tweet_intent(text, link_param, tags), "image_path": image, "length": length,
            "hashtags": tags, "warnings": warnings, "notes": notes, "first_reply_text": reply}


def render(res: dict) -> str:
    """Ответ ей словами."""
    if not res.get("ok"):
        return res.get("reason") or "Не получилось."
    lines = [f"Пост для X [{res['id']}], вариант {res['variant']}:", res["text"],
             f"Хэштеги: {' '.join('#' + t for t in res['hashtags']) or '—'} · длина ~{res['length']} из 280",
             f"Открыть окно поста: {res['intent_url']}"]
    lines.append(f"Картинка — приложить в окне X самой: {res['image_path']}" if res["image_path"]
                 else "Картинку она приложит в окне X сама.")
    if res["first_reply_text"]:
        lines.append(f"Первым ответом под постом — ссылка: {res['first_reply_text']}")
    lines += [f"⚠ {w}" for w in res["warnings"]] + [f"· {n}" for n in res["notes"]]
    lines.append(f"Выложила — пришли ссылку на пост: promo:x_posted ref={res['id']}.")
    return "\n".join(lines)


def confirm_posted(conn, post_id: str, post_url: str, now: float | None = None) -> dict:
    """Она выложила пост и прислала ссылку: {ok, text, reply_intent}."""
    ensure(conn)
    ref = str(post_id or "").strip()
    row = conn.execute("SELECT * FROM x_posts WHERE id = ?", (ref,)).fetchone()
    if not row:
        # Она прислала лот, а не номер поста: берём последний неотмеченный пост X про этот лот.
        item = _lot(conn, ref)
        if item is not None:
            row = conn.execute("SELECT * FROM x_posts WHERE item_id = ? ORDER BY (posted_ts IS NOT NULL), "
                               "created_ts DESC LIMIT 1", (item["id"],)).fetchone()
    if not row:
        return {"ok": False, "text": "Такого поста X нет: нужен номер вида x1234abc из promo:x_post или ссылка на лот, "
                                     "про который его готовили."}
    m = POST_URL.match(str(post_url or "").strip())
    if not m:
        return {"ok": False, "text": "Нужна ссылка на её пост вида https://x.com/ник/status/123…"}
    conn.execute("UPDATE x_posts SET posted_ts = ?, post_url = ? WHERE id = ?",
                 (row["posted_ts"] or now or time.time(), post_url.strip(), row["id"]))
    conn.commit()
    reply = ""
    if row["variant"] == "a" and row["reply_text"]:
        reply = xintent.tweet_intent(row["reply_text"], in_reply_to=m.group(1))
    text = f"Отметила: пост {row['id']} (вариант {row['variant']}) выложен."
    if reply:
        text += f" Ссылку на лот — первым ответом: {reply}"
    return {"ok": True, "text": text, "reply_intent": reply}


def variant_report(conn, now: float | None = None) -> dict:
    """Доля проданных лотов после первого поста X с вариантом a и b.

    Считаем только первые посты о лоте (второй заход — про лоты, которые уже не продались, это другая
    выборка) и только лоты с известным итогом: продан, итог лота известен или прошло OUTCOME_DAYS дней.
    Меньше MIN_SIDE с каждой стороны или «нет вывода» у любой стороны — вывода нет."""
    from . import experiments

    ensure(conn)
    now = now or time.time()
    rows = conn.execute("SELECT item_id, variant, MIN(posted_ts) AS ts FROM x_posts WHERE posted_ts IS NOT NULL "
                        "AND posted_ts >= ? AND item_id IS NOT NULL AND COALESCE(push, 'first') = 'first' "
                        "GROUP BY item_id, variant ORDER BY ts", (now - REPORT_DAYS * evidence.DAY,)).fetchall()
    first: dict[str, tuple[str, float]] = {}
    for r in rows:
        first.setdefault(r["item_id"], (r["variant"], r["ts"]))  # лот считаем за первый выложенный вариант
    stats: dict[str, dict] = {v: {"k": 0, "n": 0, "ts": [], "open": 0} for v in VARIANTS}
    for item_id, (variant, ts) in first.items():
        sold = conn.execute("SELECT 1 FROM sales WHERE item_id = ? AND status != 'cancelled' AND created_ts >= ? "
                            "LIMIT 1", (item_id, ts)).fetchone()
        s = stats[variant]
        if not sold and now - ts < OUTCOME_DAYS * evidence.DAY and not experiments.lot_outcome(conn, item_id,
                                                                                               now)["finished"]:
            s["open"] += 1   # лот ещё продаётся: «не продан» пока не итог
            continue
        s["n"] += 1
        s["k"] += 1 if sold else 0
        s["ts"].append(ts)
    a, b = stats["a"], stats["b"]
    head = "Ссылка в первом ответе (a) против ссылки в посте (b), по её продажам после первого поста о лоте"
    pending = (f" Ещё продаются (не считаю): a {a['open']}, b {b['open']}." if a["open"] or b["open"] else "")
    confound = ("Не чистый опыт: варианты выкладывались в разные дни и про разные лоты (вид, цена, время) — "
                "это могло повлиять сильнее, чем место ссылки.")
    if a["n"] < evidence.MIN_SIDE or b["n"] < evidence.MIN_SIDE:
        text = (f"{head}: вывода нет — лотов с итогом a {a['n']}, b {b['n']}, нужно хотя бы {evidence.MIN_SIDE} "
                f"каждого.{pending} Отмечай выложенные посты (promo:x_posted).")
        return {"text": text, "verdict": None, "a": {"k": a["k"], "n": a["n"]}, "b": {"k": b["k"], "n": b["n"]}}

    def ev_of(name: str, s: dict):
        return evidence.span(s["ts"], "her_sales", f"её первые посты X о лоте, вариант {name} (отмечены ею)", None,
                             "observed", n=s["n"], interval=evidence.wilson(s["k"], s["n"]), measure="rate",
                             value=s["k"] / s["n"])

    evs = {"a": ev_of("a", a), "b": ev_of("b", b)}
    weak = any(evidence.level(e) == "нет вывода" for e in evs.values())
    verdict = None if weak else evidence.compare(a["k"], a["n"], b["k"], b["n"])

    def line(name: str, s: dict) -> str:
        return f"{name}: продано {s['k']} из {s['n']} ({evidence.pct(s['k'] / s['n'])}) {evidence.tag(evs[name])}"

    said = {"better": "вариант a (ссылка в ответе), вероятно, продаёт лучше",
            "worse": "вариант b (ссылка в посте), вероятно, продаёт лучше",
            None: "разницы не видно — вывода нет"}[verdict]
    return {"text": "\n".join([f"{head}: {said}.", line("a", a), line("b", b), confound + pending]),
            "verdict": verdict, "a": {"k": a["k"], "n": a["n"]}, "b": {"k": b["k"], "n": b["n"]}}


def _lot(conn, ref: str):
    from . import auctions, db

    ref = str(ref or "").strip()
    if ref.startswith(("da:", "th:")):
        return db.get_item(conn, ref)
    if ref.isdigit():
        return db.get_item(conn, f"da:{ref}")
    return auctions.item_for_url(conn, ref) if ref.startswith("http") else None


def not_da_reason(ref: str) -> str | None:
    """Почему по этой ссылке/словам её лот DeviantArt не найти — или None, если это похоже на DA."""
    ref = str(ref or "").strip()
    if "toyhou.se" in ref.lower():
        return ("Это Toyhouse: пост X и подбор покупателей готовлю по её работе на DeviantArt (ссылка или da:123). "
                "Покупателей для персов Toyhouse ищи в findings:replies (LF-треды под её персов) и buyers:leads.")
    if not ref.startswith(("http", "da:", "th:")) and not ref.isdigit():
        return "По названию работу не ищу — пришли ссылку на её работу DeviantArt или da:123."
    return None


def build_for(conn, cfg, item_ref: str, variant: str = "", image_path: str = "", now: float | None = None) -> dict:
    """Для инструмента promo:x_post: лот по da:123 / ссылке, её шаблон x_post и её настройки хэштегов."""
    from . import selling

    ref = str(item_ref or "").strip()
    item = _lot(conn, ref)
    if item is None:
        return {"ok": False, "reason": not_da_reason(ref) or "Этой работы нет в базе — обнови её работы "
                                                             "(check:run source=deviantart) и пришли ссылку ещё раз."}
    default, allowed = prefs(cfg.posting)
    rt = getattr(cfg.posting, "x_rt_accounts", None) or []
    # Без её выбора — варианты по очереди среди первых постов: сравнение a/b не зависит от её привычки.
    return build(conn, item, selling.get_template(conn, TEMPLATE_KIND) or "", default, allowed,
                 variant or next_variant(conn, "first"), image_path, rt, now)

