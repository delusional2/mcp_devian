"""Лимиты публикаций: чтобы её аккаунты выглядели живыми и не попадали под антиспам.

Считаются и уже отправленное (sent_log, включая резервы «попытка» и «неизвестно»), и черновики,
которые прямо сейчас отправляются: иначе две одновременные отправки обе прошли бы проверку.
"""

import time

from . import drafts
from .config import Posting

DAY = 86400


def toyhouse_reply_block(conn, posting: Posting, thread_id: str, character_url: str | None,
                         now: float | None = None, exclude_draft: str | None = None) -> str | None:
    """Почему отклик сейчас нельзя отправить (или None, если можно)."""
    now = now or time.time()
    busy = drafts.inflight(conn, "th_reply", exclude_draft)
    if drafts.count_sent(conn, "th_reply", now - DAY) + len(busy) >= posting.th_replies_per_day:
        return f"на сегодня уже {posting.th_replies_per_day} откликов — это лимит, чтобы Toyhouse не счёл спамом"
    if (drafts.count_sent(conn, "th_reply", now - posting.th_thread_cooldown_days * DAY, target=thread_id)
            or any(p.get("thread_id") == thread_id for _, p in busy)):
        return f"в этот тред уже отвечали за последние {posting.th_thread_cooldown_days} дн"
    if character_url:
        same = drafts.count_sent(conn, "th_reply", now - DAY, ref=character_url)
        same += sum(1 for _, p in busy if p.get("character_url") == character_url)
        if same >= posting.th_same_character_per_day:
            return (f"этого перса сегодня уже предлагали в {posting.th_same_character_per_day} тредах — "
                    "одинаковые предложения Toyhouse считает флудом")
    last = max([drafts.last_sent(conn, "th_reply") or 0, *(ts for ts, _ in busy)])
    if last and now - last < posting.th_min_gap_minutes * 60:
        wait = int(posting.th_min_gap_minutes * 60 - (now - last)) + 1
        return f"слишком быстро после прошлого отклика, подожди ещё {wait} с"
    return None


def deviantart_group_used(conn, group: str, now: float | None = None, exclude_draft: str | None = None) -> int:
    """Сколько мест в группе занято за сутки: отправленное и то, что отправляется прямо сейчас."""
    now = now or time.time()
    key = group.lower()
    used = drafts.count_sent(conn, "da_group", now - DAY, target=key)
    used += sum(1 for _, p in drafts.inflight(conn, "da_publish", exclude_draft)
                for g in p.get("groups", []) if str(g.get("group", "")).lower() == key)
    return used


GROUP_KINDS = ("da_group", "da_group_manual")  # через API и руками (она отметила сама)


def group_limits(posting: Posting, group: str) -> list[tuple[int, int]]:
    """Все правила группы из её наборов: [(сколько работ, за сколько дней)], без повторов. Если группа в
    нескольких наборах с разными правилами — соблюдаем каждое (1 в день И 3 в месяц)."""
    key = group.strip().lower()
    found: set[tuple[int, int]] = set()
    for items in (posting.group_presets or {}).values():
        for g in items or []:
            lim = g.get("limit") if isinstance(g, dict) else None
            if str(g.get("group", "")).lower() == key and isinstance(lim, dict):
                try:
                    count, days = int(lim["count"]), int(lim["days"])
                except (KeyError, TypeError, ValueError):
                    continue
                if count > 0 and days > 0:
                    found.add((count, days))
    return sorted(found, key=lambda cd: (cd[1], cd[0]))


def group_limit(posting: Posting, group: str) -> tuple[int, int] | None:
    """Одно правило для подписи (самое строгое по доле в день) или None. Проверять — group_limits."""
    found = group_limits(posting, group)
    return min(found, key=lambda cd: cd[0] / cd[1]) if found else None


def limit_text(count: int, days: int) -> str:
    return f"{count} за {days} дн"


def limits_text(rules) -> str:
    return " и ".join(limit_text(c, d) for c, d in rules)


def group_window_used(conn, group: str, days: int, now: float | None = None,
                      exclude_draft: str | None = None) -> int:
    """Сколько её работ ушло в группу за окно правила группы: через API, отмеченные руками и в полёте."""
    now = now or time.time()
    key = group.lower()
    used = sum(drafts.count_sent(conn, kind, now - days * DAY, target=key) for kind in GROUP_KINDS)
    used += sum(1 for _, p in drafts.inflight(conn, "da_publish", exclude_draft)
                for g in p.get("groups", []) if str(g.get("group", "")).lower() == key)
    return used


def deviantart_group_block(conn, posting: Posting, group: str, now: float | None = None,
                           exclude_draft: str | None = None) -> str | None:
    if deviantart_group_used(conn, group, now, exclude_draft) >= posting.da_submissions_per_group_per_day:
        return f"в группу {group} сегодня уже отправлено {posting.da_submissions_per_group_per_day}"
    for lim in group_limits(posting, group):
        used = group_window_used(conn, group, lim[1], now, exclude_draft)
        if used >= lim[0]:
            return (f"правило группы {group}: {limit_text(*lim)}, а за эти {lim[1]} дн уже отправлено {used} "
                    "(считаю и то, что она отметила руками)")
    return None
