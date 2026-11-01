"""Лимиты публикаций: чтобы её аккаунты выглядели живыми и не попадали под антиспам."""

import time

from . import drafts
from .config import Posting

DAY = 86400


def toyhouse_reply_block(conn, posting: Posting, thread_id: str, character_url: str | None,
                         now: float | None = None) -> str | None:
    """Почему отклик сейчас нельзя отправить (или None, если можно)."""
    now = now or time.time()
    if drafts.count_sent(conn, "th_reply", now - DAY) >= posting.th_replies_per_day:
        return f"на сегодня уже {posting.th_replies_per_day} откликов — это лимит, чтобы Toyhouse не счёл спамом"
    if drafts.count_sent(conn, "th_reply", now - posting.th_thread_cooldown_days * DAY, target=thread_id):
        return f"в этот тред уже отвечали за последние {posting.th_thread_cooldown_days} дн"
    if character_url and drafts.count_sent(conn, "th_reply", now - DAY, ref=character_url) >= posting.th_same_character_per_day:
        return (f"этого перса сегодня уже предлагали в {posting.th_same_character_per_day} тредах — "
                "одинаковые предложения Toyhouse считает флудом")
    last = drafts.last_sent(conn, "th_reply")
    if last and now - last < posting.th_min_gap_minutes * 60:
        wait = int(posting.th_min_gap_minutes * 60 - (now - last)) + 1
        return f"слишком быстро после прошлого отклика, подожди ещё {wait} с"
    return None


def deviantart_group_block(conn, posting: Posting, group: str, now: float | None = None) -> str | None:
    now = now or time.time()
    if drafts.count_sent(conn, "da_group", now - DAY, target=group.lower()) >= posting.da_submissions_per_group_per_day:
        return f"в группу {group} сегодня уже отправлено {posting.da_submissions_per_group_per_day}"
    return None
