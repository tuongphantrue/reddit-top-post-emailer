#!/usr/bin/env python3
"""
Reddit Top Posts of the Day -> Email (runs on GitHub Actions, no local computer needed)

Fetches the top posts from all of Reddit (r/all) via Reddit's public RSS
feed, then fetches each post's own RSS feed to get its thumbnail/preview
image and full self-text body (for text posts) - and emails you a digest,
grouped by subreddit. Subreddits in BLACKLIST_SUBREDDITS are filtered out
before the email is built.

NOTE ON SCORE/VOTES: An earlier version of this script tried to also
include each post's score via Reddit's JSON endpoint, but that endpoint
was confirmed blocked (403) from GitHub Actions IPs in testing - every
post came back empty. So this version sticks to RSS only, which is
confirmed to work for image + body. Score/upvotes aren't included as a
result. Downvotes were never available either way - Reddit doesn't expose
upvote/downvote counts separately to anyone, via any method, ever.

COST OF INCLUDING IMAGES/BODY TEXT
------------------------------------
Getting a post's image and full body requires one extra request per post
(Reddit's subreddit-level RSS only gives title + link). With POSTS_TOTAL=50
that's up to 51 requests per run instead of 1, with a 1-second gap between
each - so a run now takes roughly a minute instead of a few seconds, and is
more likely to hit Reddit's rate limiting (handled with retries, but not
guaranteed to always succeed). If this becomes too flaky on your schedule,
lowering POSTS_TOTAL or the run frequency helps.

NOTE ON RELIABILITY
--------------------
As of 2026, Reddit has largely closed off new API/OAuth app registration for
personal projects, and blocks a lot of unauthenticated traffic from cloud
IPs (like GitHub Actions runners) with a 403. This script uses Reddit's
public RSS feed instead, which historically have been more lenient - but
there's no guarantee Reddit won't tighten this up too. Run the workflow
manually once after setup (Actions tab -> "Run workflow") to confirm it
still works before relying on the schedule.

SETUP
-----
1. Install dependencies:
     pip install requests

2. Create a Gmail "App Password" (regular Gmail passwords won't work with SMTP):
     - Go to https://myaccount.google.com/apppasswords
     - You need 2-Step Verification turned on first.
     - Create an app password for "Mail" and copy the 16-character code.

3. Set these as environment variables:
     export GMAIL_ADDRESS="youraddress@gmail.com"
     export GMAIL_APP_PASSWORD="16-char-app-password"
     export REDDIT_RECIPIENT="where-to-send@example.com"
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

# Reddit's public RSS feed for r/all's top posts (Atom format).
REDDIT_RSS_URL = "https://www.reddit.com/r/all/top/.rss"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# A browser-like User-Agent tends to fare better against Reddit's bot
# detection than a generic/default one.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/atom+xml, application/xml, text/xml, */*",
}

POSTS_TOTAL = int(os.environ.get("POSTS_TOTAL", "50"))
TIMEFRAME = os.environ.get("TIMEFRAME", "day")  # hour, day, week, month, year, all
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
BLACKLIST_SUBREDDITS = {
    s.strip().lower() for s in os.environ.get("BLACKLIST_SUBREDDITS", "").split(",") if s.strip()
}

SUBREDDIT_FROM_URL_RE = re.compile(r"reddit\.com/r/([^/]+)/", re.IGNORECASE)
IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
TAG_STRIP_RE = re.compile(r'<[^>]+>')
# Reddit wraps a post's rendered self-text between these two stable markers -
# used across old and new Reddit for years, more reliable than trying to
# parse the surrounding table layout.
SELFTEXT_RE = re.compile(r'<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->', re.IGNORECASE | re.DOTALL)

MAX_BODY_CHARS = 600

# Domains that host actual post images/thumbnails, as opposed to static UI
# assets (subreddit icons, snoo avatars, etc.) that can also show up as
# <img> tags in a post's RSS content but aren't the post's own image.
IMAGE_CONTENT_DOMAINS = (
    "i.redd.it", "preview.redd.it", "external-preview.redd.it",
    "i.imgur.com", "imgur.com", "a.thumbs.redditmedia.com",
    "b.thumbs.redditmedia.com", "external-i.redd.it",
)


def is_real_post_image(url):
    if not url:
        return False
    return any(domain in url.lower() for domain in IMAGE_CONTENT_DOMAINS)


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


def fetch_post_detail(permalink, retries=3):
    """Fetch a single post's own RSS feed (its comments page) to get its
    thumbnail/preview image and full self-text body, if any. Returns
    (image_url_or_None, body_text_or_empty_string). Link posts (no
    self-text) will return an empty body - that's expected, not a failure.

    Uses RSS rather than JSON: JSON was tried and confirmed blocked (403)
    from GitHub Actions IPs, while this RSS endpoint is confirmed to work.
    """
    if not permalink:
        return None, ""

    url = permalink.rstrip("/") + "/.rss"

    for attempt in range(1, retries + 1):
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 429:
            if attempt < retries:
                time.sleep(3 * attempt)
                continue
            return None, ""
        if resp.status_code >= 400:
            return None, ""
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return None, ""
        break
    else:
        return None, ""

    first_entry = root.find("atom:entry", ATOM_NS)
    if first_entry is None:
        return None, ""

    content_el = first_entry.find("atom:content", ATOM_NS)
    content_html = content_el.text if content_el is not None and content_el.text else ""

    img_matches = IMG_TAG_RE.findall(content_html)
    image_url = next((u for u in img_matches if is_real_post_image(u)), None)

    body_text = ""
    selftext_match = SELFTEXT_RE.search(content_html)
    if selftext_match:
        raw = selftext_match.group(1)
        plain = TAG_STRIP_RE.sub(" ", raw)
        plain = " ".join(plain.split())  # collapse whitespace
        if plain:
            body_text = plain[:MAX_BODY_CHARS] + ("..." if len(plain) > MAX_BODY_CHARS else "")

    return image_url, body_text


def enrich_posts(posts):
    """Fetch image + body text for each post individually. This means one
    extra request per post (on top of the single r/all listing request), so
    it's slower and more rate-limit-prone than the listing fetch alone. If
    several requests in a row come back completely empty, stop trying
    further ones - that's a sign of IP-level blocking rather than a
    per-post issue, so retrying each remaining post would just waste time.
    """
    consecutive_blocked = 0
    for i, p in enumerate(posts):
        if i > 0:
            time.sleep(1)

        if consecutive_blocked >= 5:
            p["image"], p["body"] = None, ""
            continue

        try:
            image_url, body_text = fetch_post_detail(p["url"])
        except requests.RequestException as e:
            print(f"  detail fetch failed for '{p['title'][:40]}...': {e}", file=sys.stderr)
            image_url, body_text = None, ""

        if image_url is None and not body_text:
            consecutive_blocked += 1
        else:
            consecutive_blocked = 0

        p["image"] = image_url
        p["body"] = body_text

    if consecutive_blocked >= 5:
        print("  Per-post detail fetching appears blocked - stopped early to save time.", file=sys.stderr)
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


def build_section_html(subreddit, posts):
    rows = []
    for i, p in enumerate(posts, start=1):
        title_esc = escape(p["title"])

        image_html = ""
        if p.get("image"):
            image_html = f'<img src="{escape(p["image"])}" style="max-width:100%; border-radius:6px; margin-top:8px;">'

        body_html = ""
        if p.get("body"):
            body_html = f'<div style="font-size:13px; color:#333; margin-top:8px; line-height:1.4;">{escape(p["body"])}</div>'

        rows.append(f"""
<tr>
  <td style="padding:14px 0; border-bottom:1px solid #eee; font-family:Arial,Helvetica,sans-serif;">
    <a href="{escape(p['url'] or '#')}" style="font-size:14px; font-weight:600; color:#1a1a1b; text-decoration:none;">{i}. {title_esc}</a>
    <div style="font-size:12px; color:#888; margin-top:4px;">u/{escape(p['author'])}</div>
    {body_html}
    {image_html}
  </td>
</tr>""")

    return f"""
<h2 style="color:#ff4500; font-family:Arial,Helvetica,sans-serif;">r/{escape(subreddit)}</h2>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;">
{''.join(rows)}
</table>"""


def build_html(sections):
    body_parts = [build_section_html(sub, posts) for sub, posts in sections.items()]
    total = sum(len(posts) for posts in sections.values())
    return f"""\
<html>
<body style="margin:0; padding:20px; background:#f4f4f4;">
  <h1 style="color:#222; font-family:Arial,Helvetica,sans-serif;">&#128293; {total} Top Reddit Posts Today</h1>
  {''.join(body_parts)}
  <p style="color:#999; font-size:12px; font-family:Arial,Helvetica,sans-serif; margin-top:20px;">
    Sent automatically by reddit-top-post-emailer via GitHub Actions.
  </p>
</body>
</html>"""


def build_plain_text(sections):
    lines = []
    for sub, posts in sections.items():
        lines.append(f"--- r/{sub} ---")
        for p in posts:
            lines.append(f"{p['title']} (u/{p['author']}) - {p['url']}")
            if p.get("body"):
                lines.append(f"  {p['body']}")
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
    print(f"Fetching top {POSTS_TOTAL} posts from r/all (timeframe={TIMEFRAME})...")
    if BLACKLIST_SUBREDDITS:
        print(f"Blacklisted subreddits: {', '.join(sorted(BLACKLIST_SUBREDDITS))}")

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

    print(f"Fetching image + body text for {total} post(s) individually (1 request per post)...")
    for sub in sections:
        sections[sub] = enrich_posts(sections[sub])

    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(TIMEZONE))
    except Exception:
        now = datetime.now()
    timestamp = now.strftime("%b %d, %Y %I:%M %p")

    subject = f"{total} top Reddit posts - {timestamp}"
    html = build_html(sections)
    text = build_plain_text(sections)

    send_email(subject, html, text)


if __name__ == "__main__":
    main()
