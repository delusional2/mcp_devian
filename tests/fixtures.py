"""Синтетические страницы с той же разметкой, что у настоящих (снято 01.11.2026)."""

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
<title>DeviantArt: test-group/123's gallery</title>
<item>
  <title>Moth dragon adopt AUCTION [OPEN] SB: $10 AB: $60</title>
  <link>https://www.deviantart.com/someartist/art/Moth-dragon-adopt-AUCTION-OPEN-1404000001</link>
  <guid isPermaLink="true">https://www.deviantart.com/someartist/art/Moth-dragon-adopt-AUCTION-OPEN-1404000001</guid>
  <pubDate>Sun, 01 Nov 2026 01:57:41 PST</pubDate>
  <media:title type="plain">Moth dragon adopt AUCTION [OPEN] SB: $10 AB: $60</media:title>
  <media:keywords>adopt, dragon, auction</media:keywords>
  <media:rating>nonadult</media:rating>
  <media:credit role="author" scheme="urn:ebu">SomeArtist</media:credit>
  <media:credit role="author" scheme="urn:ebu">https://a.deviantart.net/avatars/s/o/someartist.jpg</media:credit>
  <media:description type="html">&lt;p&gt;Ends 24h after the last bid. Min inc $1&lt;/p&gt;</media:description>
  <media:thumbnail url="https://example.com/t1.jpg" height="150" width="150"/>
</item>
<item>
  <title>YCH commission [SLOTS OPEN]</title>
  <link>https://www.deviantart.com/other/art/YCH-commission-SLOTS-OPEN-1404000002</link>
  <pubDate>Sat, 31 Oct 2026 10:00:00 PDT</pubDate>
  <media:keywords></media:keywords>
  <media:credit role="author" scheme="urn:ebu">Other</media:credit>
  <media:description type="html">&lt;p&gt;slots open&lt;/p&gt;</media:description>
</item>
<item>
  <title>Cat OTA (CLOSED)</title>
  <link>https://www.deviantart.com/third/art/Cat-OTA-CLOSED-1404000003</link>
  <pubDate>Fri, 30 Oct 2026 10:00:00 PDT</pubDate>
  <media:credit role="author" scheme="urn:ebu">Third</media:credit>
</item>
</channel>
</rss>
"""


def _row(thread_id, title, author, created, posts, last=None, last_author=None):
    last_html = (
        f'<a href="/~forums/13.adoption-center/{thread_id}.t?page=0"><abbr class="tooltipster datetime" title="x">{last}</abbr></a>'
        if last else "N/A"
    )
    return f"""
<tr class="row forum-thread-row ">
  <td class="col-6">
    <h3 class="forum-thread-title"><a href="/~forums/13.adoption-center/{thread_id}.some-slug"> {title} </a></h3>
    <div class="forum-thread-info small"> Posted by <span class="display-user"><a href="https://toyhou.se/{author}"><i class="fi-torso user-name-icon"></i>{author}</a></span>
      <abbr class="tooltipster datetime" title="x">{created}</abbr></div>
  </td>
  <td class="col-2"><div class="forum-thread-posts">{posts} Posts</div></td>
  <td class="col-4"><div class="forum-last-update"><div class="forum-last-update-content"><div class="forum-last-update-info">
    <div class="forum-last-update-date">{last_html}</div>
    <div class="forum-last-update-author">{f'<span class="display-user"><a href="https://toyhou.se/{last_author}">{last_author}</a></span>' if last_author else ''}</div>
  </div></div></div></td>
</tr>"""


BOARD = f"""<html><head><title>Adoption Center on Toyhouse</title></head><body>
<table class="forum-view-table table">
<tr class="row forum-thread-header"><th>Title</th><th>Posts</th><th>Last Updated</th></tr>
{_row(865492, "I AM SEEKING DRAGONS !!! O: Trades/Art/$$", "buyer1", "10 days, 15 hours ago", 35, "4 hours, 40 minutes ago", "someone")}
{_row(422432, "LF: $$$, O: CHARACTERS", "seller1", "3 years, 3 months ago", 69, "1 day, 1 hour ago", "seller1")}
{_row(864957, "LF: OCS/CUSTOMS/$$ | OF: OCs/ART", "buyer2", "11 days, 20 hours ago", 0)}
{_row(175858, "Character Purge! HUGELY DISCOUNTED", "old", "5 years, 11 months ago", 16, "5 years, 6 months ago", "old")}
{_row(869446, "Adoptable Dragon Hybrid LF NEW HOME", "seller2", "2 hours ago", 1, "20 minutes ago", "x")}
</table></body></html>"""


THREAD = """<html><body><h1 class="thread-title">I AM SEEKING DRAGONS</h1>
<div class="thread-wrapper">
<div class="forum-post forum-13 thread-865492 post-1"><div class="forum-post-body">
<div class="card forum-post-content"><div class="card-block user-content fr-view post-body-1">
<p>Looking for dragon or wyvern characters!</p><p>Budget up to $40, can also do art.</p>
</div></div></div></div>
<div class="forum-post forum-13 thread-865492 post-2"><div class="user-content"><p>offer here</p></div></div>
</div></body></html>"""

CHALLENGE = "<html><head><title>Just a moment...</title></head><body><div id='challenge-platform'></div></body></html>"

# Локализованная страница проверки: узнаётся по разметке, а не по заголовку.
CHALLENGE_RU = """<html><head><title>Один момент…</title></head><body>
<p id="challenge-error-text"></p><script>window._cf_chl_opt = {cType: 'managed'};</script>
</body></html>"""

# Обычная страница тоже тянет скрипт challenge-platform — это не проверка.
NORMAL_WITH_CF_SCRIPT = BOARD.replace("</body>", '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></body>')
