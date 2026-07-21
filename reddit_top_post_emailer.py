#!/usr/bin/env python3
"""
Reddit Top Posts of the Day -> Email (runs on GitHub Actions, no local computer needed)

Fetches the top posts from all of Reddit (r/all), then fetches each post's
own JSON data to get its score (net upvotes), thumbnail/preview image, and
full self-text body (for text posts) - and emails you a digest, grouped by
subreddit. Subreddits in BLACKLIST_SUBREDDITS are filtered out before the
email is built.

WHY THIS VERSION USES A PROXY
------------------------------
Reddit blocks essentially all per-post requests from GitHub Actions' own
IP range (confirmed via testing: 0/50 succeeded across multiple endpoint
types). Routing requests through a residential proxy (PROXY_URL) avoids
that block, since the traffic no longer originates from a flagged
datacenter IP range. Without PROXY_URL set, this script still runs - it
just falls back to direct requests, which reliably get the listing but
will likely fail on per-post enrichment the same way as before.

NOTE ON VOTES: Reddit only ever exposes net score (upvotes minus
downvotes) - it does not expose upvote and downvote counts separately, to
anyone, via any method. That's a Reddit platform limitation, not something
this script (or a proxy) can work around.

SETUP
-----
1. Install dependencies:
     pip install requests

2. Create a Gmail "App Password" (regular Gmail passwords won't work with SMTP):
     - Go to https://myaccount.google.com/apppasswords
     - You need 2-Step Verification turned on first.
     - Create an app password for "Mail" and copy the 16-character code.

3. Get a residential proxy provider's connection URL, in the form:
     http://username:password@proxy-host:port
   (Exact format varies by provider - check their docs. Most residential
   proxy services support this standard format.)

4. Set these as environment variables:
     export GMAIL_ADDRESS="youraddress@gmail.com"
     export GMAIL_APP_PASSWORD="16-char-app-password"
     export REDDIT_RECIPIENT="where-to-send@example.com"
     export PROXY_URL="http://username:password@proxy-host:port"   # optional but needed for images/body/score
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
# the listing only, since it's confirmed reliable even without a proxy.
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

# Residential proxy URL, e.g. "http://user:pass@host:port". Used only for
# the per-post detail requests (score/image/body), since those are the
# ones that get blocked without it. Optional - if unset, falls back to
# direct requests (listing still works, per-post enrichment likely won't).
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

SUBREDDIT_FROM_URL_RE = re.compile(r"reddit\.com/r/([^/]+)/", re.IGNORECASE)
MAX_BODY_CHARS = 600


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


def fetch_post_detail(permalink):
    """Fetch a single post's own JSON data (via proxy, if configured) to
    get its score, thumbnail/preview image, and full self-text body.
    Returns {"score": int_or_None, "image": url_or_None, "body": text_or_""}.
    No retry-on-429: if the proxy itself gets rate limited, retrying a
    single request rarely helps and just adds runtime - better to move on.
    """
    empty = {"score": None, "image": None, "body": ""}
    if not permalink:
        return empty

    url = permalink.rstrip("/") + ".json"
    resp = requests.get(url, headers=HEADERS, proxies=PROXIES, timeout=20)
    if resp.status_code >= 400:
        return empty
    try:
        data = resp.json()
        post_data = data[0]["data"]["children"][0]["data"]
    except (ValueError, KeyError, IndexError, TypeError):
        return empty

    score = post_data.get("score")

    body_text = ""
    selftext = (post_data.get("selftext") or "").strip()
    if selftext:
        body_text = selftext[:MAX_BODY_CHARS] + ("..." if len(selftext) > MAX_BODY_CHARS else "")

    image_url = None
    try:
        preview_images = post_data.get("preview", {}).get("images", [])
        if preview_images:
            image_url = preview_images[0]["source"]["url"].replace("&amp;", "&")
    except (KeyError, IndexError, TypeError):
        image_url = None
    if not image_url:
        thumb = post_data.get("thumbnail", "")
        if thumb and thumb.startswith("http"):
            image_url = thumb

    return {"score": score, "image": image_url, "body": body_text}


def enrich_posts(posts):
    """Fetch score + image + body text for each post individually via the
    proxy. If several requests in a row come back completely empty, stop
    trying further ones - a sign the proxy itself is being blocked or
    misconfigured, so retrying each remaining post would just waste time.
    """
    if not PROXIES:
        print("  PROXY_URL not set - skipping image/body/score enrichment (would likely be blocked anyway).")
        for p in posts:
            p["score"], p["image"], p["body"] = None, None, ""
        return posts

    consecutive_blocked = 0
    got_content = 0
    for i, p in enumerate(posts):
        if i > 0:
            time.sleep(1)

        if consecutive_blocked >= 5:
            p["score"], p["image"], p["body"] = None, None, ""
            continue

        try:
            detail = fetch_post_detail(p["url"])
        except requests.RequestException as e:
            print(f"  detail fetch failed for '{p['title'][:40]}...': {e}", file=sys.stderr)
            detail = {"score": None, "image": None, "body": ""}

        if detail["score"] is None and detail["image"] is None and not detail["body"]:
            consecutive_blocked += 1
        else:
            consecutive_blocked = 0
            got_content += 1

        p["score"] = detail["score"]
        p["image"] = detail["image"]
        p["body"] = detail["body"]

    print(f"  Got score/image/body for {got_content}/{len(posts)} post(s)")
    if consecutive_blocked >= 5:
        print("  Per-post detail fetching appears blocked even through the proxy - stopped early.", file=sys.stderr)
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

        score_html = ""
        if p.get("score") is not None:
            score_html = f"&#11014; {p['score']:,} &nbsp;|&nbsp; "

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
    <div style="font-size:12px; color:#888; margin-top:4px;">{score_html}u/{escape(p['author'])}</div>
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
            score_part = f"[{p['score']:,} pts] " if p.get("score") is not None else ""
            lines.append(f"{score_part}{p['title']} (u/{p['author']}) - {p['url']}")
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
    print(f"Proxy configured: {'yes' if PROXIES else 'no'}")

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

    print(f"Fetching score/image/body text for {total} post(s) individually...")
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
