"""Ссылки X (Twitter) для неё: окно нового поста и поиск. Никакого API и чтения x.com — она жмёт сама.

Документировано (docs.x.com, Web Intent): https://x.com/intent/tweet с параметрами text, url, hashtags
(через запятую, без #), in_reply_to (номер поста). Картинку параметром не приложить — файл она добавит сама.
Длина — по правилам twitter-text v3, но с запасом: всё, что не латиница/кириллица и т.п., считаем за 2,
каждая ссылка — 23 (t.co). Предел поста 280; предупреждаем раньше (WARN_AT).
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

INTENT_URL = "https://x.com/intent/tweet"
SEARCH_URL = "https://x.com/search"
# f=live (вкладка «Latest») — общепринятое соглашение, в документации X не описано; без него откроется «Top».
SEARCH_LIVE = "live"
MAX_LEN = 280
WARN_AT = 270
URL_LEN = 23
MAX_HASHTAGS = 4

# Любая ссылка, и без https: boosty.to/her, her.carrd.co — X их тоже сокращает до t.co (23). Как в outreach.URL_RE:
# домен верхнего уровня — буквы, так что цены «12.50» и «e.g.» ссылками не считаются.
URL_RE = re.compile(r"(?:https?://|www\.)\S+|(?<![\w@.-])[\w-]+(?:\.[\w-]+)*\.[a-z]{2,24}(?![\w-])(?:/\S*)?", re.I)
# Диапазоны с весом 1 в twitter-text v3 (остальное — 2).
_LIGHT = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))
STATUS_ID = re.compile(r"(?:^|/status(?:es)?/)(\d{5,25})(?:[/?#]|$)")


def _weight(ch: str) -> int:
    cp = ord(ch)
    return 1 if any(lo <= cp <= hi for lo, hi in _LIGHT) else 2


def weighted_len(text: str) -> int:
    """Длина поста по X с запасом: ссылка — 23, CJK и эмодзи — 2 за каждый символ, остальное — 1."""
    text = str(text or "")
    total = 0
    pos = 0
    for m in URL_RE.finditer(text):
        total += sum(_weight(c) for c in text[pos:m.start()]) + URL_LEN
        pos = m.end()
    return total + sum(_weight(c) for c in text[pos:])


def sanitize_hashtags(tags, max_n: int = MAX_HASHTAGS) -> list[str]:
    """Хэштеги без #, только буквы/цифры/_, без повторов (регистр не важен), не больше max_n."""
    if isinstance(tags, str):
        tags = re.split(r"[\s,]+", tags)
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or ():
        clean = re.sub(r"[^\w]", "", str(t).strip().lstrip("#"))
        if not clean or clean.isdigit() or clean.casefold() in seen:
            continue
        seen.add(clean.casefold())
        out.append(clean)
        if len(out) >= max(0, max_n):
            break
    return out


def status_id(value: str) -> str:
    """Номер поста из ссылки x.com/…/status/123 или самого номера; '' — не похоже."""
    m = STATUS_ID.search(str(value or "").strip())
    return m.group(1) if m else ""


def tweet_intent(text: str, url: str = "", hashtags=(), in_reply_to: str = "") -> str:
    """Ссылка на окно нового поста (или ответа) с её текстом. Отправляет она сама кнопкой Post."""
    params: list[tuple[str, str]] = [("text", str(text or ""))]
    if url:
        params.append(("url", str(url)))
    tags = sanitize_hashtags(hashtags)
    if tags:
        params.append(("hashtags", ",".join(tags)))
    reply = status_id(in_reply_to)
    if reply:
        params.append(("in_reply_to", reply))
    return f"{INTENT_URL}?{urlencode(params, quote_via=quote, safe='')}"


def composed(text: str, url: str = "", hashtags=()) -> str:
    """Как пост примерно сложится в окне X: текст, ссылка, хэштеги — для подсчёта длины."""
    tags = " ".join("#" + t for t in sanitize_hashtags(hashtags))
    return " ".join(p for p in (str(text or "").strip(), url, tags) if p)


def search_url(query: str) -> str:
    """Поиск по X (свежие посты). Открывает она; помощник x.com не читает (правила X запрещают сбор)."""
    return f"{SEARCH_URL}?{urlencode([('q', query), ('f', SEARCH_LIVE)], quote_via=quote, safe='')}"


LF_PHRASES = ('"lf adopts"', '"looking for adopts"', '"lf oc"', '"buying adopts"')


def lf_query(keywords=(), since_days: int = 7, now: float | None = None) -> str:
    """Запрос для поиска «кто ищет адопты» за последние дни, с её нишей (вид, стиль) — по желанию."""
    since = datetime.fromtimestamp((now or time.time()) - since_days * 86400, UTC).strftime("%Y-%m-%d")
    words = [w.strip() for w in keywords or () if str(w).strip()]
    niche = (" (" + " OR ".join(f'"{w}"' if " " in w else w for w in words) + ")") if words else ""
    return f"({' OR '.join(LF_PHRASES)}){niche} -filter:replies since:{since}"
