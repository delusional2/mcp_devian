"""Данные Toyhouse для тестов подбора персов и чтения досок (своя разметка, без сети).

Разметка строк доски — как в tests/fixtures.py (снята с реальной страницы до Cloudflare);
всё остальное — синтетика, написанная под конкретные случаи.
"""

NOW = 1_800_000_000.0
HOUR = 3600
DAY = 86400


def _ago(seconds: float) -> str:
    """Относительное время в стиле Toyhouse: «4 hours, 40 minutes ago»."""
    seconds = int(seconds)
    if seconds >= DAY:
        d, h = divmod(seconds, DAY)
        return f"{d} days, {h // HOUR} hours ago"
    if seconds >= HOUR:
        return f"{seconds // HOUR} hours, {(seconds % HOUR) // 60} minutes ago"
    return f"{max(1, seconds // 60)} minutes ago"


def row(thread_id, title, author, created_ago, posts, last_ago=None, last_author=None, board="13.adoption-center",
        css="row forum-thread-row ", locked=False):
    last_html = (f'<a href="/~forums/{board}/{thread_id}.t?page=0"><abbr class="tooltipster datetime" '
                 f'title="x">{_ago(last_ago)}</abbr></a>' if last_ago is not None else "N/A")
    lock = '<i class="fa fa-lock thread-lock"></i>' if locked else ""
    who = (f'<span class="display-user"><a href="https://toyhou.se/{last_author}">{last_author}</a></span>'
           if last_author else "")
    return f"""
<tr class="{css}">
  <td class="col-6">
    <h3 class="forum-thread-title">{lock}<a href="/~forums/{board}/{thread_id}.some-slug"> {title} </a></h3>
    <div class="forum-thread-info small"> Posted by <span class="display-user"><a href="https://toyhou.se/{author}">{author}</a></span>
      <abbr class="tooltipster datetime" title="x">{_ago(created_ago)}</abbr></div>
  </td>
  <td class="col-2"><div class="forum-thread-posts">{posts} Posts</div></td>
  <td class="col-4"><div class="forum-last-update"><div class="forum-last-update-content"><div class="forum-last-update-info">
    <div class="forum-last-update-date">{last_html}</div>
    <div class="forum-last-update-author">{who}</div>
  </div></div></div></td>
</tr>"""


def board_page(*rows: str) -> str:
    return ("<html><head><title>Adoption Center on Toyhouse</title></head><body>"
            '<table class="forum-view-table table"><tr class="row forum-thread-header"><th>Title</th></tr>'
            + "".join(rows) + "</table></body></html>")


# Доска, где с прошлой проверки (2 часа назад) много нового: активность на 1-й и 2-й странице свежее,
# на 3-й уже старше границы. Первая строка 1-й страницы — закреплённый старый тред.
BOARD_PAGE1 = board_page(
    row(900001, "[PINNED] Board rules", "mod", 900 * DAY, 2, 300 * DAY, "mod", css="row forum-thread-row sticky"),
    row(900002, "LF: wolves!! O: $$", "buyerA", 3 * HOUR, 2, 5 * 60, "buyerA"),
    row(900003, "LF dragons | O: usd", "buyerB", 20 * HOUR, 6, 30 * 60, "someone"),
    row(900004, "LF: canines", "buyerC", 2 * DAY, 12, 50 * 60, "x"),
)
BOARD_PAGE2 = board_page(
    row(900005, "LF: cats", "buyerD", 1 * DAY, 3, 70 * 60, "y"),
    row(900006, "LF: moth ocs", "buyerE", 4 * DAY, 50, 100 * 60, "z"),
)
BOARD_PAGE3 = board_page(
    row(900007, "LF: humanoids", "buyerF", 4 * DAY, 9, 5 * HOUR, "w"),
    row(900008, "LF: anything", "buyerG", 4 * DAY, 9, 8 * HOUR, "w"),
)
BUSY_PAGES = {1: BOARD_PAGE1, 2: BOARD_PAGE2, 3: BOARD_PAGE3}
# Тихая доска: уже на первой странице последняя активность старше прошлой проверки.
QUIET_PAGE = board_page(
    row(910001, "LF: dragons", "quiet1", 1 * DAY, 2, 10 * 60, "quiet1"),
    row(910002, "LF: cats", "quiet2", 3 * DAY, 4, 20 * HOUR, "q"),
)

CLOSED_TITLES = [
    "LF dragons [CLOSED]",
    "LF: Characters (Found, thank you!)",
    "LF wolves [ON HOLD]",
    "LF cats [paused]",
]

_FILLER = ("Hi everyone! I'm building a little cast for a long-running story project with my friends and I've been "
           "browsing for a while. I love expressive designs with lots of personality, unique markings and fun "
           "accessories. Bonus points for characters that come with extra art or a fleshed-out profile, but that "
           "isn't required at all. ")
LONG_FIRST_POST = (
    _FILLER * 4
    + "Looking for dragons and wolves mostly! NOT looking for anthro ones, sorry. no hybrids or customs either. "
    + "Budget: $15-$40 per character. I'm paying via paypal invoice only. "
    + _FILLER * 2
    + "Thanks for reading, please post your offers below with a link!"
)
assert LONG_FIRST_POST.index("Budget: $15-$40") > 900 and 1800 < len(LONG_FIRST_POST) < 2500
LONG_THREAD_HTML = (
    '<html><body><div class="forum-post forum-13 thread-1 post-1"><div class="card-block user-content">'
    f"<p>{LONG_FIRST_POST}</p></div></div>"
    '<div class="forum-post"><div class="user-content"><p>offer</p></div></div></body></html>')

# Тот же запрос того же автора на двух досках.
CROSS_POST = [
    {"id": "th:920001", "board": 13, "author": "CrossBuyer", "title": "LF: Dragons!! (USD)", "activity_ago": 2 * HOUR},
    {"id": "th:920002", "board": 5866, "author": "crossbuyer", "title": "[LF] dragons (usd)", "activity_ago": 30 * 60},
]

# 16 тредов × 5 персов: кого предложил бы живой продавец (размечено вручную, синтетика).
GOLD_CHARACTERS = [
    {"name": "Ember", "url": "https://toyhou.se/1.ember", "price": "$40", "tags": ["wolf", "feral", "fire"]},
    {"name": "Mochi", "url": "https://toyhou.se/2.mochi", "price": "$15", "tags": ["cat", "pastel", "cute"]},
    {"name": "Vex", "url": "https://toyhou.se/3.vex", "price": "60 USD", "tags": ["dragon", "anthro", "male"]},
    {"name": "Sky", "url": "https://toyhou.se/4.sky", "price": "$25", "tags": ["humanoid", "demon", "oc"]},
    {"name": "Luna", "url": "https://toyhou.se/5.luna", "price": "$30", "tags": ["moth", "insect"]},
]
ALL = {"Ember", "Mochi", "Vex", "Sky", "Luna"}
GOLD = [
    ("LF: canines! O: $$/pts", None, {"Ember"}),
    ("LF: felines", None, {"Mochi"}),
    ("LF: Wyverns/drakes", None, {"Vex"}),
    ("Looking for: feral cats (USD only)", "Budget is $10 max. No anthros please", set()),
    ("LF: dragons", "Looking for dragons! NOT looking for anthro ones, only ferals. Budget under $50", set()),
    ("LF: OCs", "Hi! I'm looking for any oc, cute ones preferred. Paying through paypal. No humans, no dogs.", ALL),
    ("LF: kitty sona", None, {"Mochi"}),
    ("LF: werewolves", None, {"Ember"}),
    ("LF: humanoids", "Budget: 20$ . I can't pay more than $20", set()),
    ("LF: Anything with wings!", "dragons, moths, birds etc", {"Vex", "Luna"}),
    ("LF: demon girls", None, {"Sky"}),
    ("LF characters for my story", "not picky about species! any budget", ALL),
    ("LF dragons [CLOSED]", None, set()),
    ("LF: wolves", "anything except canines lol jk. Budget: $15-$35", set()),
    ("LF: moth ocs (art only)", None, set()),
    ("LF: cats", "budget $30 max", {"Mochi"}),
]

# Обычные формулировки бюджета в LF-тредах → (верхняя граница, валюта).
BUDGETS = [
    ("Budget: $15-$40", (40.0, "usd")),
    ("No offers over $50 please", (50.0, "usd")),
    ("Budget is $10 max", (10.0, "usd")),
    ("$30 max!", (30.0, "usd")),
    ("budget 2k pts", (2000.0, "points")),
    ("up to €20", (20.0, "eur")),
    ("won't pay over $40", (40.0, "usd")),
    ("Budget up to $30", (30.0, "usd")),
    ("budget under $50", (50.0, "usd")),
    ("I can't pay more than $20", (20.0, "usd")),
    ("budget is around $25", (25.0, "usd")),
]
