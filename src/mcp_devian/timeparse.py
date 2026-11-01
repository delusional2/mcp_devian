import re
from datetime import UTC, datetime

UNITS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}

_PART = re.compile(r"(\d+|\ban?\b)\s*(second|minute|hour|day|week|month|year)s?", re.I)


def relative_seconds(text: str | None) -> int | None:
    """'1 month, 4 days ago', 'an hour ago', 'yesterday' -> секунды. Для 'N/A' и мусора None."""
    if not text:
        return None
    parts = _PART.findall(text)
    if not parts:
        if re.search(r"\bjust now\b|\btoday\b", text, re.I):
            return 0
        if re.search(r"\byesterday\b", text, re.I):
            return UNITS["day"]
        return None
    return sum((1 if n.lower() in ("a", "an") else int(n)) * UNITS[unit.lower()] for n, unit in parts)


def relative_to_ts(text: str | None, now: float) -> float | None:
    seconds = relative_seconds(text)
    return None if seconds is None else now - seconds


# Абсолютное время из атрибута title у <abbr class="datetime">. Формат Toyhouse за Cloudflare
# не проверен, поэтому только запасной вариант, когда относительный текст не разобрался.
_ABS_FORMATS = ("%d %b %Y, %I:%M:%S %p", "%d %b %Y, %I:%M %p", "%b %d, %Y, %I:%M:%S %p", "%b %d, %Y %I:%M %p",
                "%B %d, %Y, %I:%M:%S %p", "%B %d, %Y %I:%M %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d %B %Y, %H:%M", "%d %b %Y %H:%M")


def absolute_to_ts(text: str | None) -> float | None:
    """«2026-10-31T20:30:00Z», «31 Oct 2026, 10:15:02 am» → unix. Без пояса считаем UTC. Иначе None."""
    if not text:
        return None
    raw = " ".join(str(text).split())
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        cleaned = re.sub(r"\s*(?:UTC|GMT)$", "", raw, flags=re.I)
        cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", cleaned)
        for fmt in _ABS_FORMATS:
            try:
                dt = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()
