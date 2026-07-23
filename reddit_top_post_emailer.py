#!/usr/bin/env python3
"""
Reddit Top Posts of the Day -> Email (runs on GitHub Actions, no local computer needed)

Fetches the top posts from all of Reddit (r/all), then (if REDDIT_COOKIE is
set) fetches each post's own JSON data to get its score, thumbnail/preview
image, and full self-text body - and emails you a digest, grouped by
subreddit. Subreddits in BLACKLIST_SUBREDDITS are filtered out before the
email is built.

WHY THIS VERSION USES A COOKIE
---------------------------------
Reddit blocks essentially all per-post requests from GitHub Actions' own
IP range when they look anonymous (confirmed: 0/50 succeeded in testing).
Attaching a logged-in Reddit account's session cookie makes requests look
like a real logged-in user instead of an anonymous bot, which may get past
that block. Without REDDIT_COOKIE set, this script still runs fine - it
just skips score/image/body and sends title/author/link only.

IMPORTANT SECURITY NOTE: REDDIT_COOKIE is a real login credential for a
real Reddit account, not an API key. Store it ONLY as a GitHub Actions
secret (Settings -> Secrets and variables -> Actions) - never paste it
directly into this file or any committed file, even in a private repo.
Using a personal account's cookie for automated access is also outside
Reddit's official API terms - low real-world risk for light personal use,
but worth knowing this isn't officially sanctioned. Cookies also expire
periodically and will need to be refreshed when that happens.

NOTE ON VOTES: Reddit only ever exposes net score (upvotes minus
downvotes) - it does not expose upvote and downvote counts separately, to
anyone, via any method, even to a logged-in account viewing its own feed.

SETUP
-----
1. Install dependencies:
     pip install requests

2. Create a Gmail "App Password" (regular Gmail passwords won't work with SMTP):
     - Go to https://myaccount.google.com/apppasswords
     - You need 2-Step Verification turned on first.
     - Create an app password for "Mail" and copy the 16-character code.

3. Get your Reddit session cookie:
     - Log into reddit.com in your browser
     - Open DevTools (F12) -> Network tab -> reload the page
     - Click the first "reddit.com" request in the list
     - In the Headers panel, find "Cookie:" under Request Headers
     - Copy the ENTIRE value (a long string of name=value pairs)

4. Set these as environment variables (or GitHub Actions secrets):
     export GMAIL_ADDRESS="youraddress@gmail.com"
     export GMAIL_APP_PASSWORD="16-char-app-password"
     export REDDIT_RECIPIENT="where-to-send@example.com"
     export REDDIT_COOKIE="the cookie string from step 3"   # optional but needed for score/image/body
     export POSTS_TOTAL="50"                               # optional, top N posts from r/all
     export TIMEFRAME="day"                                # optional: hour/day/week/month/year/all
     export BLACKLIST_SUBREDDITS=""                         # optional, comma-separated, e.g. "nsfw,gonewild"

SCHEDULING
----------
See README.md / GitHub Actions workflow in this repo for running this daily
in the cloud without needing your own computer on.

USAGE
-----
     python reddit_top_post_emailer.py
"""

import os
import re
import smtplib
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests

# Reddit's public RSS feed for r/all's top posts (Atom format) - used for
# the listing only, since it's confirmed reliable even without a cookie.
REDDIT_RSS_URL = "https://www.reddit.com/r/all/top/.rss"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

REDDIT_COOKIE = os.environ.get("REDDIT_COOKIE", "").strip()

# A browser-like User-Agent tends to fare better against Reddit's bot
# detection than a generic/default one. The Cookie header (if set) is what
# makes per-post requests look like a real logged-in user.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/atom+xml, application/xml, text/xml, */*",
}
if REDDIT_COOKIE:
    HEADERS["Cookie"] = REDDIT_COOKIE

POSTS_TOTAL = int(os.environ.get("POSTS_TOTAL", "50"))
TIMEFRAME = os.environ.get("TIMEFRAME", "day")  # hour, day, week, month, year, all
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
BLACKLIST_SUBREDDITS = {
    s.strip().lower() for s in os.environ.get("BLACKLIST_SUBREDDITS", "").split(",") if s.strip()
}

# Bump this string whenever the file changes, and check it in the Actions
# log after deploying - the single most reliable way to confirm a push
# actually took effect, since checking the file on GitHub's website has
# repeatedly shown stale/cached content in this project's history.
SCRIPT_VERSION = "2026-07-foldable-sections"

SUBREDDIT_FROM_URL_RE = re.compile(r"reddit\.com/r/([^/]+)/", re.IGNORECASE)
MAX_BODY_CHARS = 600
MAX_COMMENT_CHARS = 300

# Posts scoring at or above this get a "HOT" badge alongside their type badge.
HOT_SCORE_THRESHOLD = 50000

# Video links are hidden for now while focusing on getting image display
# right - the underlying fetch/extraction still runs, this just skips
# rendering it. Flip to True to show "Watch video" links again.
SHOW_VIDEO_LINKS = False

# Reddit doesn't expose a topic/genre field for subreddits - this is a
# best-effort static mapping of commonly-seen subreddits into rough topic
# groups, maintained by hand. It will inevitably miss less-common
# subreddits (those fall into "Other"), and a subreddit could arguably fit
# more than one category - this is a convenience grouping, not an
# authoritative classification.
SUBREDDIT_CATEGORIES = {
    "Wholesome": {
        "mademesmile", "wholesome", "humansbeingbros", "upliftingnews",
        "aww", "rarepuppers", "wholesomememes", "mademecry", "eyebleach",
        "animalsbeingbros", "animalsbeingderps",
    },
    "Funny & Memes": {
        "funny", "memes", "meirl", "me_irl", "unexpected", "murderedbywords",
        "nottheonion", "wellthatsucks", "tiktokcringe", "comedycemetery",
        "dankmemes", "shitposting", "196", "programmerhumor", "onerangebraincell",
        "oneorangebraincell", "facepalm", "therewasanattempt", "cursedcomments",
    },
    "Interesting & Curious": {
        "interestingasfuck", "damnthatsinteresting", "beamazed",
        "oddlysatisfying", "historicalcapsule", "sipstea", "poirstea",
        "interesting", "todayilearned", "educationalgifs", "nextlevel",
        "mildlyinteresting",
    },
    "News & Politics": {
        "news", "worldnews", "politics", "politicalhumor", "moderatepolitics",
        "geopolitics", "upliftingnews",
    },
    "Tech & Science": {
        "technology", "programming", "python", "science", "askscience",
        "space", "futurology", "artificial", "machinelearning",
        "explainlikeimfive", "gadgets", "dataisbeautiful",
    },
    "Gaming": {
        "gaming", "pcgaming", "ps5", "xbox", "nintendoswitch",
        "leagueoflegends", "globaloffensive", "wow", "minecraft", "steam",
    },
    "Sports": {
        "sports", "nba", "nfl", "soccer", "formula1", "baseball", "hockey",
        "mma", "football",
    },
    "Pics & Videos": {
        "pics", "videos", "gifs", "publicfreakout", "mildlyinfuriating",
        "crazyfuckingvideos", "wtf", "damnthatsatisfying",
    },
    "Advice & Discussion": {
        "careeradvice", "relationship_advice", "askreddit", "confession",
        "offmychest", "dating_advice", "amitheasshole", "relationships",
        "trueoffmychest", "legaladvice",
    },
    "Finance": {
        "investing", "stocks", "cryptocurrency", "wallstreetbets",
        "personalfinance", "stockmarket",
    },
    "Food": {
        "food", "cooking", "foodporn", "recipes", "mealprepsunday",
    },
    "Nature & Earth": {
        "earthporn", "natureisfuckinglit", "natureismetal",
    },
}

CATEGORY_ORDER = [
    "Wholesome", "Funny & Memes", "Interesting & Curious", "Pics & Videos",
    "News & Politics", "Tech & Science", "Gaming", "Sports", "Finance",
    "Advice & Discussion", "Food", "Nature & Earth", "Other",
]


def classify_subreddit_category(subreddit):
    """Best-effort topic grouping for a subreddit, based on the static
    SUBREDDIT_CATEGORIES mapping above. Falls back to "Other" for anything
    not in the list.
    """
    name = subreddit.lower()
    for category, names in SUBREDDIT_CATEGORIES.items():
        if name in names:
            return category
    return "Other"


def fetch_top_posts(limit=50, timeframe="day", retries=3):
    """Fetch the top N posts from r/all's RSS feed for the given timeframe.
    Retries with backoff on 429 (rate limited).
    """
    params = {"t": timeframe, "limit": limit}

    last_error = None
    for attempt in range(1, retries + 1):
        resp = requests.get(REDDIT_RSS_URL, headers=HEADERS, params=params, timeout=15)
        if resp.status_code == 429:
            last_error = f"429 rate limited (attempt {attempt}/{retries})"
            if attempt < retries:
                time.sleep(5 * attempt)
                continue
            resp.raise_for_status()
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        break
    else:
        raise requests.RequestException(last_error)

    posts = []
    for entry in root.findall("atom:entry", ATOM_NS)[:limit]:
        title_el = entry.find("atom:title", ATOM_NS)
        title = title_el.text if title_el is not None else "Untitled post"

        link = None
        for link_el in entry.findall("atom:link", ATOM_NS):
            href = link_el.get("href")
            if href:
                link = href
                break

        author_el = entry.find("atom:author/atom:name", ATOM_NS)
        author = author_el.text.lstrip("/u/") if author_el is not None and author_el.text else "unknown"

        sub_match = SUBREDDIT_FROM_URL_RE.search(link or "")
        subreddit = sub_match.group(1) if sub_match else "unknown"

        posts.append({
            "title": title,
            "author": author,
            "url": link,
            "subreddit": subreddit,
        })
    return posts


def classify_post_type(post_data):
    """Classify a post as Text/Gallery/Video/Image/Link based on Reddit's
    own post metadata (post_hint, is_self, is_gallery, is_video, domain).
    Best-effort heuristic, not from any single authoritative field, since
    Reddit doesn't expose one clean "type" field for every post shape.
    """
    if post_data.get("is_self"):
        return "Text"
    if post_data.get("is_gallery"):
        return "Gallery"
    if post_data.get("is_video"):
        return "Video"

    hint = post_data.get("post_hint", "")
    if hint == "image":
        return "Image"
    if hint in ("hosted:video", "rich:video"):
        return "Video"
    if hint == "link":
        return "Link"

    domain = post_data.get("domain", "") or ""
    if domain in ("i.redd.it", "i.imgur.com", "imgur.com"):
        return "Image"
    if domain == "v.redd.it":
        return "Video"
    if domain.startswith("self."):
        return "Text"
    return "Link"


def extract_top_comment(comment_listing):
    """Pick the highest-scored real top-level comment from a post's comment
    listing (the second element of Reddit's post JSON response). Skips
    deleted/removed comments, stickied mod-note comments, and AutoModerator
    - none of those represent genuine community reaction. Returns
    {"author": str, "body": str, "score": int} or None if nothing usable.
    """
    try:
        children = comment_listing["data"]["children"]
    except (KeyError, TypeError):
        return None

    best = None
    for child in children:
        if child.get("kind") != "t1":  # skip "more comments" stubs etc.
            continue
        d = child.get("data", {})
        if d.get("stickied"):
            continue
        author = d.get("author")
        body = (d.get("body") or "").strip()
        if not body or body in ("[deleted]", "[removed]"):
            continue
        if author in (None, "[deleted]", "AutoModerator"):
            continue

        score = d.get("score", 0) or 0
        if best is None or score > best["score"]:
            best = {"author": author, "body": body, "score": score}

    if best is None:
        return None
    if len(best["body"]) > MAX_COMMENT_CHARS:
        best["body"] = best["body"][:MAX_COMMENT_CHARS] + "..."
    return best


def fetch_post_detail(permalink):
    """Fetch a single post's own JSON data (with the login cookie attached,
    if configured) to get its post type, score, thumbnail/preview image,
    full self-text body, and top comment. Returns {"type": str_or_None,
    "score": int_or_None, "image": url_or_None, "video": url_or_None,
    "body": text_or_"", "top_comment": dict_or_None}. No retry-on-429: a
    single retry rarely helps and just adds runtime - better to move on to
    the next post.

    NOTE ON VIDEO: email clients (Gmail, Outlook, etc.) don't support
    playing video inline - no client executes a <video> tag in a message
    body, for security reasons universal across providers, not something
    fixable from this project's side. As the closest available substitute,
    video posts use Reddit's own animated GIF preview (if available) as
    the "image" shown, since GIFs DO autoplay inline in email - this gives
    real motion, just no audio (GIF format never carries sound). "video"
    is still also returned as a direct link to the raw video file, for
    anyone who wants the real thing.

    NOTE ON CROSSPOSTS/GALLERIES: a crosspost (a repost of another post)
    doesn't carry its own media - it's fetched from the original post via
    crosspost_parent_list. A gallery post (multiple images) stores images
    under media_metadata rather than preview.images - only the first image
    is shown here as a representative thumbnail, not the full gallery.

    NOTE ON TOP COMMENT: comes from the SAME request as the post itself -
    Reddit's post JSON response includes both the post (data[0]) and its
    comment tree (data[1]) in one call, so this adds no extra requests.
    """
    empty = {"type": None, "score": None, "comments": None, "domain": None, "image": None, "video": None, "body": "", "top_comment": None}
    if not permalink:
        return empty

    url = permalink.rstrip("/") + ".json"
    resp = requests.get(url, headers=HEADERS, params={"sort": "top"}, timeout=20)
    if resp.status_code >= 400:
        return empty
    try:
        data = resp.json()
        post_data = data[0]["data"]["children"][0]["data"]
    except (ValueError, KeyError, IndexError, TypeError):
        return empty

    top_comment = None
    if len(data) > 1:
        top_comment = extract_top_comment(data[1])

    post_type = classify_post_type(post_data)
    score = post_data.get("score")
    comments = post_data.get("num_comments")
    domain = post_data.get("domain") if post_type == "Link" else None

    body_text = ""
    selftext = (post_data.get("selftext") or "").strip()
    if selftext:
        body_text = selftext[:MAX_BODY_CHARS] + ("..." if len(selftext) > MAX_BODY_CHARS else "")

    # Crossposts (a repost of another post) don't carry their own media -
    # the actual image/video lives on the original post, nested under
    # crosspost_parent_list. Look there instead when present.
    crosspost_parents = post_data.get("crosspost_parent_list") or []
    media_source = crosspost_parents[0] if crosspost_parents else post_data

    video_url = None
    try:
        if media_source.get("is_video"):
            video_url = media_source["media"]["reddit_video"]["fallback_url"].replace("&amp;", "&")
    except (KeyError, TypeError):
        video_url = None

    # For video posts, prefer an animated GIF preview over a static
    # thumbnail - GIFs actually autoplay inline in email (video tags
    # don't), so this is the closest thing to "playable video" achievable
    # in an email body. No audio though - GIF format never carries sound.
    image_url = None
    if video_url:
        try:
            image_url = media_source["preview"]["images"][0]["variants"]["gif"]["source"]["url"].replace("&amp;", "&")
        except (KeyError, IndexError, TypeError):
            image_url = None

    if not image_url:
        try:
            preview_images = media_source.get("preview", {}).get("images", [])
            if preview_images:
                image_url = preview_images[0]["source"]["url"].replace("&amp;", "&")
        except (KeyError, IndexError, TypeError):
            image_url = None

    # Gallery posts (multiple images) store images under media_metadata
    # rather than preview.images - grab the first one as a representative
    # image rather than showing nothing.
    if not image_url and media_source.get("is_gallery"):
        try:
            gallery_items = media_source["gallery_data"]["items"]
            first_media_id = gallery_items[0]["media_id"]
            image_url = media_source["media_metadata"][first_media_id]["s"]["u"].replace("&amp;", "&")
        except (KeyError, IndexError, TypeError):
            image_url = None

    if not image_url:
        thumb = media_source.get("thumbnail", "")
        if thumb and thumb.startswith("http"):
            image_url = thumb

    return {"type": post_type, "score": score, "comments": comments, "domain": domain, "image": image_url,
            "video": video_url, "body": body_text, "top_comment": top_comment}


def enrich_posts(posts):
    """Fetch score + image + video + body text for each post individually,
    using the login cookie. If several requests in a row come back
    completely empty, stop trying further ones - a sign the cookie isn't
    working (e.g. expired) or is still being blocked, so retrying each
    remaining post would just waste time.
    """
    if not REDDIT_COOKIE:
        print("  REDDIT_COOKIE not set - skipping type/image/video/body/score/comment enrichment (would likely be blocked anyway).")
        for p in posts:
            p["type"], p["score"], p["comments"], p["domain"], p["image"], p["video"], p["body"], p["top_comment"] = None, None, None, None, None, None, "", None
        return posts

    consecutive_blocked = 0
    got_content = 0
    for i, p in enumerate(posts):
        if i > 0:
            time.sleep(1)

        if consecutive_blocked >= 5:
            p["type"], p["score"], p["comments"], p["domain"], p["image"], p["video"], p["body"], p["top_comment"] = None, None, None, None, None, None, "", None
            continue

        try:
            detail = fetch_post_detail(p["url"])
        except requests.RequestException as e:
            print(f"  detail fetch failed for '{p['title'][:40]}...': {e}", file=sys.stderr)
            detail = {"type": None, "score": None, "comments": None, "domain": None, "image": None, "video": None, "body": "", "top_comment": None}

        if detail["score"] is None and detail["image"] is None and detail["video"] is None and not detail["body"]:
            consecutive_blocked += 1
        else:
            consecutive_blocked = 0
            got_content += 1

        p["type"] = detail["type"]
        p["score"] = detail["score"]
        p["comments"] = detail["comments"]
        p["domain"] = detail["domain"]
        p["image"] = detail["image"]
        p["video"] = detail["video"]
        p["top_comment"] = detail["top_comment"]
        p["body"] = detail["body"]

    print(f"  Got score/image/video/body for {got_content}/{len(posts)} post(s)")
    if consecutive_blocked >= 5:
        print("  Per-post detail fetching appears blocked even with the cookie - stopped early. "
              "The cookie may have expired - try grabbing a fresh one from your browser.", file=sys.stderr)
    return posts


def group_by_subreddit(posts, blacklist):
    """Group posts by subreddit, dropping any post whose subreddit is
    blacklisted (case-insensitive). Preserves the order subreddits first
    appear in (i.e. roughly by top post rank).
    """
    sections = {}
    skipped = 0
    for p in posts:
        if p["subreddit"].lower() in blacklist:
            skipped += 1
            continue
        sections.setdefault(p["subreddit"], []).append(p)
    if skipped:
        print(f"  Filtered out {skipped} post(s) from blacklisted subreddit(s)")
    return sections


def group_by_category(sections):
    """Group subreddit sections into topic categories, in CATEGORY_ORDER.
    Returns an ordered dict: category -> {subreddit: [posts]}, omitting
    any category with no subreddits present this run.
    """
    by_category = {cat: {} for cat in CATEGORY_ORDER}
    for subreddit, posts in sections.items():
        category = classify_subreddit_category(subreddit)
        by_category[category][subreddit] = posts
    return {cat: subs for cat, subs in by_category.items() if subs}


def build_section_html(subreddit, posts):
    rows = []
    for i, p in enumerate(posts, start=1):
        title_esc = escape(p["title"])

        score_html = ""
        if p.get("score") is not None:
            score_html = f"&#11014; {p['score']:,} &nbsp;|&nbsp; "

        comments_html = ""
        if p.get("comments") is not None:
            comments_html = f"&#128172; {p['comments']:,} &nbsp;|&nbsp; "

        image_html = ""
        if p.get("image"):
            image_html = (
                f'<img src="{escape(p["image"])}" '
                f'style="width:100%; max-width:280px; height:auto; '
                f'border-radius:6px; margin-top:8px; display:block;">'
            )

        video_html = ""
        if p.get("video") and SHOW_VIDEO_LINKS:
            video_html = (
                f'<div style="margin-top:6px;">'
                f'<a href="{escape(p["video"])}" style="font-size:12px; color:#0066cc; text-decoration:none;">'
                f'&#9654; Watch video (no audio - Reddit strips it from this link)</a></div>'
            )

        body_html = ""
        if p.get("body"):
            body_html = f'<div style="font-size:13px; color:#333; margin-top:8px; line-height:1.4;">{escape(p["body"])}</div>'

        comment_html = ""
        tc = p.get("top_comment")
        if tc:
            comment_html = (
                f'<div style="font-size:12px; color:#555; margin-top:8px; padding-left:10px; '
                f'border-left:3px solid #ddd; line-height:1.4;">'
                f'&#128172; <b>{tc["score"]:,}</b> u/{escape(tc["author"])}: {escape(tc["body"])}'
                f'</div>'
            )

        type_html = ""
        if p.get("type"):
            type_colors = {
                "Image": "#0066cc", "Video": "#8b5cf6", "Text": "#6b7280",
                "Gallery": "#059669", "Link": "#ea580c",
            }
            color = type_colors.get(p["type"], "#6b7280")
            type_label = p["type"].upper()
            if p["type"] == "Link" and p.get("domain"):
                type_label = f'{type_label} \u2192 {p["domain"]}'
            type_html = (
                f'<span style="font-size:10px; font-weight:600; color:{color}; '
                f'border:1px solid {color}; border-radius:3px; padding:1px 5px; '
                f'margin-left:6px; vertical-align:middle;">{escape(type_label)}</span>'
            )

        hot_html = ""
        if p.get("score") is not None and p["score"] >= HOT_SCORE_THRESHOLD:
            hot_html = (
                '<span style="font-size:10px; font-weight:700; color:#fff; '
                'background:#dc2626; border-radius:3px; padding:1px 5px; '
                'margin-left:4px; vertical-align:middle;">&#128293; HOT</span>'
            )

        # Fold body/image/video/comment behind a native <details> toggle -
        # no JavaScript involved (email clients never run JS, universally,
        # for security reasons), this is a plain HTML5 element some email
        # renderers (Gmail's included) support natively. Clients that don't
        # support it typically just show the content unfolded rather than
        # breaking, so this degrades reasonably safely either way.
        extra_content = f"{body_html}{image_html}{video_html}{comment_html}"
        fold_html = ""
        if extra_content.strip():
            fold_html = f"""
    <details style="margin-top:6px;">
      <summary style="cursor:pointer; font-size:12px; color:#0066cc; font-family:Arial,Helvetica,sans-serif;">Show more</summary>
      <div>{extra_content}</div>
    </details>"""

        rows.append(f"""
<tr>
  <td style="padding:14px 0; border-bottom:1px solid #eee; font-family:Arial,Helvetica,sans-serif;">
    <a href="{escape(p['url'] or '#')}" style="font-size:14px; font-weight:600; color:#1a1a1b; text-decoration:none;">{i}. {title_esc}</a>{type_html}{hot_html}
    <div style="font-size:12px; color:#888; margin-top:4px;">{score_html}{comments_html}u/{escape(p['author'])}</div>
    {fold_html}
  </td>
</tr>""")

    return f"""
<details open style="margin-bottom:6px;">
  <summary style="color:#ff4500; font-family:Arial,Helvetica,sans-serif; font-size:19px; font-weight:bold; cursor:pointer;">r/{escape(subreddit)}</summary>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
{''.join(rows)}
  </table>
</details>"""


def build_category_html(category, subreddit_posts):
    """Build one category's block: a category header followed by each of
    its subreddits' post lists.
    """
    sub_parts = [build_section_html(sub, posts) for sub, posts in subreddit_posts.items()]
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
  <tr><td style="background:#1a1a1b; padding:6px 10px; border-radius:4px;">
    <span style="color:#fff; font-size:13px; font-weight:700; font-family:Arial,Helvetica,sans-serif; letter-spacing:0.5px;">{escape(category.upper())}</span>
  </td></tr>
</table>
{''.join(sub_parts)}"""


def build_html(sections):
    total = sum(len(posts) for posts in sections.values())
    by_category = group_by_category(sections)

    # Two-column layout needs an HTML table (not flexbox/grid - those
    # aren't reliably supported across email clients, especially Outlook).
    # Whole CATEGORIES alternate between columns (not individual
    # subreddits) so a category's subreddits stay together rather than
    # splitting across both columns. This doesn't perfectly balance
    # column height, but keeps each category visually intact.
    left_parts, right_parts = [], []
    for i, (category, subreddit_posts) in enumerate(by_category.items()):
        target = left_parts if i % 2 == 0 else right_parts
        target.append(build_category_html(category, subreddit_posts))

    return f"""\
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  @media only screen and (max-width: 600px) {{
    .digest-col {{
      display: block !important;
      width: 100% !important;
      padding-left: 0 !important;
      padding-right: 0 !important;
    }}
  }}
</style>
</head>
<body style="margin:0; padding:20px; background:#f4f4f4;">
  <h1 style="color:#222; font-family:Arial,Helvetica,sans-serif;">&#128293; {total} Top Reddit Posts Today</h1>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:1000px;">
    <tr>
      <td class="digest-col" valign="top" width="50%" style="padding-right:16px;">
        {''.join(left_parts)}
      </td>
      <td class="digest-col" valign="top" width="50%" style="padding-left:16px;">
        {''.join(right_parts)}
      </td>
    </tr>
  </table>
  <p style="color:#999; font-size:12px; font-family:Arial,Helvetica,sans-serif; margin-top:20px;">
    Sent automatically by reddit-top-post-emailer via GitHub Actions.
  </p>
</body>
</html>"""


def build_plain_text(sections):
    lines = []
    by_category = group_by_category(sections)
    for category, subreddit_posts in by_category.items():
        lines.append(f"=== {category.upper()} ===")
        for sub, posts in subreddit_posts.items():
            lines.append(f"--- r/{sub} ---")
            for p in posts:
                score_part = f"[{p['score']:,} pts] " if p.get("score") is not None else ""
                comments_part = f"[{p['comments']:,} comments] " if p.get("comments") is not None else ""
                type_part = f"[{p['type']}] " if p.get("type") else ""
                lines.append(f"{type_part}{score_part}{comments_part}{p['title']} (u/{p['author']}) - {p['url']}")
                if p.get("body"):
                    lines.append(f"  {p['body']}")
                if p.get("video") and SHOW_VIDEO_LINKS:
                    lines.append(f"  Video: {p['video']}")
                if p.get("top_comment"):
                    tc = p["top_comment"]
                    lines.append(f"  Top comment ({tc['score']:,} pts, u/{tc['author']}): {tc['body']}")
            lines.append("")
    return "\n".join(lines)


def send_email(subject, html, text):
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("REDDIT_RECIPIENT")

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender),
        ("GMAIL_APP_PASSWORD", app_password),
        ("REDDIT_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)

    print(f"Sent to {recipient}!")


def main():
    print(f"=== Script version: {SCRIPT_VERSION} ===")
    print(f"Fetching top {POSTS_TOTAL} posts from r/all (timeframe={TIMEFRAME})...")
    if BLACKLIST_SUBREDDITS:
        print(f"Blacklisted subreddits: {', '.join(sorted(BLACKLIST_SUBREDDITS))}")
    print(f"Cookie configured: {'yes' if REDDIT_COOKIE else 'no'}")

    try:
        posts = fetch_top_posts(limit=POSTS_TOTAL, timeframe=TIMEFRAME)
        print(f"  fetched {len(posts)} post(s)")
    except (requests.RequestException, ET.ParseError) as e:
        print(f"  failed to fetch - {e}", file=sys.stderr)
        posts = []

    sections = group_by_subreddit(posts, BLACKLIST_SUBREDDITS)

    total = sum(len(v) for v in sections.values())
    if total == 0:
        print("No posts found - not sending an email.")
        return

    print(f"Fetching score/comments/image/body text for {total} post(s) individually...")
    for sub in sections:
        sections[sub] = enrich_posts(sections[sub])

    all_posts = [p for posts in sections.values() for p in posts]
    image_typed = [p for p in all_posts if p.get("type") in ("Image", "Gallery")]
    image_typed_with_image = [p for p in image_typed if p.get("image")]
    print(f"Image-hit-rate check: {len(image_typed_with_image)}/{len(image_typed)} "
          f"posts tagged Image/Gallery actually got an image URL")
    if image_typed and not image_typed_with_image:
        print("  ALL Image/Gallery posts failed image extraction - likely a bug in the extraction "
              "logic itself, not a per-post fluke.", file=sys.stderr)
    elif len(image_typed_with_image) < len(image_typed):
        missed_titles = [p["title"][:50] for p in image_typed if not p.get("image")]
        print(f"  Missed: {missed_titles}", file=sys.stderr)

    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(TIMEZONE))
    except Exception:
        now = datetime.now()
    timestamp = now.strftime("%b %d, %Y %I:%M %p")

    other_subs = [s for s in sections if classify_subreddit_category(s) == "Other"]
    if other_subs:
        print(f"Category coverage: {len(other_subs)}/{len(sections)} subreddit(s) uncategorized "
              f"(shown under Other): {', '.join(sorted(other_subs))}")

    subject = f"{total} top Reddit posts - {timestamp}"

    html = build_html(sections)
    text = build_plain_text(sections)

    send_email(subject, html, text)


if __name__ == "__main__":
    main()
