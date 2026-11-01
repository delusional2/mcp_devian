"""Второй заход для непроданных лотов — в рамках правил: без перезаливов и без лишних пингов.

Что предлагаем по каждому лоту (не чаще раза в 7 дней на лот; пауза считается от её действий по плану —
группа, пост X, статус, пинг, — а не от того, что план просто показали):
* группы из её наборов, куда эта работа ещё не уходила и где правило группы позволяет, — списком;
  отправляет она сама на сайте (у API нет «предложить в группу» для уже опубликованной работы) и потом
  отмечает: group_done → log_manual_group;
* одно предложение статуса (её словами) в ближайшее хорошее время по её данным;
* один пост в X (xpost, её шаблон), вариант — по очереди среди постов второго захода (в сравнение a/b
  такие посты не идут: лот уже не продался с первого раза, это другая выборка);
* пинг пинглисту — только если под этой работой ещё ни разу не пинговали.
Новую копию работы ради подъёма не предлагаем: DeviantArt считает повторы спамом (Etiquette Policy).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, drafts, limits

DAY = 86400
COOLDOWN_DAYS = 7
SAME_PLAN_S = DAY  # в тот же день показываем тот же план, а не новый
WEAK_LEVELS = ("нет вывода", "низкая")
SCHEMA = """
CREATE TABLE IF NOT EXISTS repromote_log (
    item_id TEXT NOT NULL,
    ts REAL NOT NULL,
    plan TEXT NOT NULL,            -- JSON того, что предложили
    PRIMARY KEY (item_id, ts)
);
"""
NO_REUPLOAD = ("Новую копию работы ради подъёма не выкладываю: DeviantArt считает повторы спамом — "
               "вместо этого группы, статус, X и пинг.")


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "Europe/Moscow")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Moscow")


def next_slot(best: dict | None, now: float, tz: str) -> float | None:
    """Ближайший час в лучшем окне (будни/выходные × часть суток) или None, если окна по данным нет."""
    if not best or best.get("level") in WEAK_LEVELS or not best.get("slot"):
        return None
    kind, part = best["slot"][0], int(best["slot"][1])
    zone = _zone(tz)
    start = int(now // 3600 + 1) * 3600
    for h in range(0, 8 * 24):
        local = datetime.fromtimestamp(start + h * 3600, zone)
        if ("weekend" if local.weekday() >= 5 else "weekday") == kind and local.hour // 6 == part:
            return float(start + h * 3600)
    return None


def _number(url: str) -> str:
    m = re.search(r"-(\d+)/?(?:[?#].*)?$", str(url or ""))
    return m.group(1) if m else ""


def used_groups(conn, item) -> set[str]:
    """Группы (в нижнем регистре), куда эта работа уже уходила: через API или отмечено ею вручную."""
    item_id, url = item["id"], item["url"] or ""
    num = _number(url)
    out: set[str] = set()
    for r in conn.execute(f"SELECT target, ref, detail FROM sent_log WHERE ok = 1 AND kind IN "
                          f"({','.join('?' * len(limits.GROUP_KINDS))})", limits.GROUP_KINDS):
        detail = str(r["detail"] or "")
        if r["ref"] == item_id or detail == url or (num and _number(detail) == num):
            out.add(str(r["target"] or "").lower())
    return out


def _preset_groups(posting) -> list[dict]:
    seen: dict[str, dict] = {}
    for items in (posting.group_presets or {}).values():
        for g in items or []:
            key = str(g.get("group") or "").lower()
            if key and key not in seen:
                seen[key] = g
    return list(seen.values())


def funnel_rank(conn, groups: list[dict], now: float) -> list[dict]:
    """Группы по воронке её ниши (funnel.group_targets: сколько разных ставящих), с меткой данных; прочие — после."""
    from . import evidence, funnel

    rows = {r["group"]: (i, r) for i, r in enumerate(funnel.group_targets(conn, now))}
    out = []
    for g in groups:
        hit = rows.get(g["group"].lower())
        if hit and hit[1]["n"]:
            r = hit[1]
            g = {**g, "why": f"ставящих в её нише {r['bidders']}, продано {r['k']} из {r['n']} {evidence.tag(r['ev'])}"}
        out.append((hit[0] if hit else len(rows), g))
    return [g for _, g in sorted(out, key=lambda x: x[0])]


def _rank(conn, groups: list[dict], now: float, rank_groups) -> list[dict]:
    fn = rank_groups or funnel_rank
    try:
        ranked = list(fn(conn, groups, now))
    except Exception:  # noqa: BLE001 — рейтинг групп не должен ломать план: порядок её наборов
        return groups
    return ranked or groups


def group_options(conn, posting, item, now: float, rank_groups=None) -> tuple[list[dict], list[str]]:
    """([{group, folder_name, rule}], [почему пропущены])."""
    used = used_groups(conn, item)
    ok: list[dict] = []
    skipped: list[str] = []
    for g in _preset_groups(posting):
        name = str(g["group"])
        if name.lower() in used:
            skipped.append(f"{name}: эта работа там уже была")
            continue
        why = limits.deviantart_group_block(conn, posting, name, now)
        if why:
            skipped.append(f"{name}: {why}")
            continue
        ok.append({"group": name, "folder_name": g.get("folder_name") or "",
                   "rule": limits.limits_text(limits.group_limits(posting, name))})
    return _rank(conn, ok, now, rank_groups), skipped


def ever_pinged(conn, item_id: str) -> bool:
    """Пинговали ли пинглист под этой работой хоть раз (отправлено, в черновике или неизвестно)."""
    from . import pinglist

    pinglist.ensure(conn)
    if conn.execute("SELECT 1 FROM pinglist_sent WHERE deviation_id = ? LIMIT 1", (item_id,)).fetchone():
        return True
    if drafts.count_sent(conn, "da_ping", 0, target=item_id):
        return True
    return conn.execute("SELECT 1 FROM drafts WHERE kind = 'da_ping' AND status NOT IN ('cancelled', 'failed') "
                        "AND json_extract(payload, '$.deviation_id') = ? LIMIT 1", (item_id,)).fetchone() is not None


def _next_variant(conn) -> str:
    from . import xpost

    return xpost.next_variant(conn, "repromote")


def _last_plan(conn, item_id: str):
    return conn.execute("SELECT ts, plan FROM repromote_log WHERE item_id = ? ORDER BY ts DESC LIMIT 1",
                        (item_id,)).fetchone()


def her_actions(conn, item, since: float) -> list[tuple[float, str]]:
    """Что она сделала по лоту после since: отправила в группу (API или отметила сама), выложила пост X,
    отправила статус с этой работой, пинганула пинглист. [(когда, что)] — свежие первыми."""
    from . import pinglist, xpost

    item_id, url = item["id"], item["url"] or ""
    num = _number(url)
    out: list[tuple[float, str]] = []
    for r in conn.execute(f"SELECT ts, target, ref, detail FROM sent_log WHERE ok = 1 AND ts >= ? AND kind IN "
                          f"({','.join('?' * len(limits.GROUP_KINDS))})", (since, *limits.GROUP_KINDS)):
        detail = str(r["detail"] or "")
        if r["ref"] == item_id or detail == url or (num and _number(detail) == num):
            out.append((r["ts"], f"группа {r['target']}"))
    xpost.ensure(conn)
    out += [(r[0], "пост X") for r in conn.execute(
        "SELECT posted_ts FROM x_posts WHERE item_id = ? AND posted_ts >= ?", (item_id, since))]
    out += [(r[0], "статус") for r in conn.execute(
        "SELECT sent_ts FROM drafts WHERE kind = 'da_status' AND status = 'sent' AND sent_ts >= ? "
        "AND json_extract(payload, '$.deviation_url') = ?", (since, url)) if url]
    pinglist.ensure(conn)
    out += [(r[0], "пинг") for r in conn.execute(
        "SELECT MAX(ts) FROM pinglist_sent WHERE deviation_id = ? AND ts >= ?", (item_id, since)) if r[0]]
    return sorted(out, reverse=True)


def _lot_plan(conn, cfg, lot: dict, item, best: dict | None, now: float, rank_groups) -> dict:
    from . import selling, xpost

    tz = getattr(cfg.posting, "timezone", "") or "Europe/Moscow"
    when = next_slot(best, now, tz)
    groups, skipped = group_options(conn, cfg.posting, item, now, rank_groups)
    plan: dict = {"item_id": item["id"], "title": item["title"], "url": item["url"], "why": lot.get("why") or "",
                  "groups": groups, "skipped_groups": skipped, "slot_ts": when,
                  "slot_text": (f"{datetime.fromtimestamp(when, _zone(tz)).strftime('%d.%m %H:%M')} ({tz}) — "
                                f"{best.get('evidence') or ''}".rstrip(" —") if when and best
                                else "лучшего времени по её данным не видно — когда ей удобно")}
    plan["status_hint"] = (f"статус: её словами про «{item['title']}» — promo:status ref={item['url']} text=…; "
                           "уйдёт по её «го» (можно на время выше)")
    template = selling.get_template(conn, xpost.TEMPLATE_KIND)
    if template:
        default, allowed = xpost.prefs(cfg.posting)
        res = xpost.build(conn, item, template, default, allowed, _next_variant(conn),
                          rt_accounts=getattr(cfg.posting, "x_rt_accounts", None) or [], now=now, push="repromote")
        plan["x_post"] = {k: res.get(k) for k in ("ok", "id", "variant", "text", "intent_url", "warnings", "reason",
                                                  "first_reply_text")}
    else:
        plan["x_post"] = {"ok": False, "reason": "пост в X: нет её шаблона — set_template('x_post', …)"}
    plan["ping"] = (f"пинглисту ещё не писали про эту работу — pinglist:ping url={item['url']} (её шаблон)"
                    if not ever_pinged(conn, item["id"]) else "")
    return plan


def plan(conn, cfg, now: float | None = None, rank_groups=None, limit: int = 5) -> dict:
    """План второго захода для её непроданных лотов: {"lots": [...], "waiting": [...], "text": str}.

    Пишет в repromote_log, что предложено (в тот же день — тот же план). Пауза 7 дней — от её действий
    по плану (группа, пост X, статус, пинг), а не от показа: посмотреть план ещё ничего не значит."""
    from . import selling

    now = now or time.time()
    ensure(conn)
    tz = getattr(cfg.posting, "timezone", "") or "Europe/Moscow"
    res = selling.unsold(conn, now, tz=tz, limit=max(1, limit))
    best = res.get("best_time")
    lots: list[dict] = []
    waiting: list[str] = []
    for lot in res.get("lots") or []:
        item = db.get_item(conn, lot["item_id"])
        if item is None:
            continue
        last = _last_plan(conn, item["id"])
        acted = her_actions(conn, item, last["ts"]) if last else []
        if acted and now - acted[0][0] < COOLDOWN_DAYS * DAY:
            nxt = datetime.fromtimestamp(acted[0][0] + COOLDOWN_DAYS * DAY, _zone(tz)).strftime("%d.%m")
            done = ", ".join(dict.fromkeys(what for _, what in acted))
            waiting.append(f"«{item['title']}»: уже продвигала ({done}), следующий заход не раньше {nxt}")
            continue
        if last and not acted and now - last["ts"] < SAME_PLAN_S:
            lots.append({**json.loads(last["plan"]), "repeat": True})
            continue
        p = _lot_plan(conn, cfg, lot, item, best, now, rank_groups)
        conn.execute("INSERT OR REPLACE INTO repromote_log (item_id, ts, plan) VALUES (?, ?, ?)",
                     (item["id"], now, json.dumps(p, ensure_ascii=False)))
        conn.commit()
        lots.append(p)
    return {"lots": lots, "waiting": waiting, "total": res.get("total", 0), "text": render(lots, waiting)}


def render(lots: list[dict], waiting: list[str]) -> str:
    if not lots and not waiting:
        return "Непроданных лотов, которым нужен второй заход, сейчас нет."
    out: list[str] = []
    for p in lots:
        out.append(f"«{p['title']}» — {p['why']} {p['url']}".strip())
        if p["groups"]:
            out.append("  Группы (отправляет она на сайте, потом promo:group_done): "
                       + "; ".join(g["group"] + (f" → {g['folder_name']}" if g["folder_name"] else "")
                                   + (f" (правило {g['rule']})" if g["rule"] else "")
                                   + (f" — {g['why']}" if g.get("why") else "") for g in p["groups"]))
        else:
            out.append("  Группы: подходящих сейчас нет (её наборы — da_groups).")
        out += [f"  · не предлагаю {s}" for s in p["skipped_groups"][:5]]
        out.append(f"  Когда: {p['slot_text']}")
        out.append(f"  {p['status_hint']}")
        x = p.get("x_post") or {}
        out.append(f"  X [{x.get('id')}], вариант {x.get('variant')}: {x.get('intent_url')}" if x.get("ok")
                   else f"  {x.get('reason')}")
        if p.get("ping"):
            out.append(f"  {p['ping']}")
    out += waiting
    out.append(NO_REUPLOAD)
    return "\n".join(out)


def log_manual_group(conn, deviation_ref: str, group: str, now: float | None = None, cfg=None) -> str:
    """Она сама отправила работу в группу на сайте: записываем в журнал — лимиты групп это учтут."""
    from . import auctions

    now = now or time.time()
    ref = str(deviation_ref or "").strip()
    name = str(group or "").strip().lstrip("@")
    if not ref or not name:
        return "Нужны работа (ссылка или da:123) и группа."
    item = db.get_item(conn, ref) if ref.startswith("da:") else auctions.item_for_url(conn, ref)
    if item is None or not item["own"]:
        return "Такой её работы нет в базе — пришли ссылку на её работу."
    over = limits.deviantart_group_block(conn, cfg.posting, name, now) if cfg is not None else None
    again = name.lower() in used_groups(conn, item)
    drafts.log_sent(conn, "da_group_manual", name.lower(), item["id"], True, detail=item["url"] or "", now=now)
    text = f"Записала: «{item['title']}» → {name}."
    if again:
        text += " Эта работа там уже была раньше."
    if over:
        text += f" Внимание: {over} — следующую в эту группу лучше позже."
    return text
