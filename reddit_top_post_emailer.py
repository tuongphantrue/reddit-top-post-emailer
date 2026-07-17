#!/usr/bin/env python3
"""
Reddit Top Posts of the Day -> Email (runs on GitHub Actions, no local computer needed)

Fetches the top posts from your chosen subreddits via Reddit's public RSS
feeds and emails you a digest, grouped by subreddit.

NOTE ON RELIABILITY
--------------------
As of 2026, Reddit has largely closed off new API/OAuth app registration for
personal projects, and blocks a lot of unauthenticated traffic from cloud
IPs (like GitHub Actions runners) with a 403. This script uses Reddit's
public RSS feeds instead, which historically have been more lenient - but
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
     export SUBREDDITS="programming,python,technology"   # optional, comma-separated
     export POSTS_PER_SUB="5"                             # optional, top N per subreddit
     export TIMEFRAME="day"                                # optional: hour/day/week/month/year/all

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
import xml.etree.ElementTree as ET
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests

# Reddit's public RSS feed for a subreddit's top posts (Atom format).
REDDIT_RSS_URL = "https://www.reddit.com/r/{subreddit}/top/.rss"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# A browser-like User-Agent tends to fare better against Reddit's bot
# detection than a generic/default one.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/atom+xml, application/xml, text/xml, */*",
}

SUBREDDITS = [s.strip() for s in os.environ.get("SUBREDDITS", "programming,python,technology").split(",") if s.strip()]
POSTS_PER_SUB = int(os.environ.get("POSTS_PER_SUB", "5"))
TIMEFRAME = os.environ.get("TIMEFRAME", "day")  # hour, day, week, month, year, all
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")

THUMB_RE = re.compile(r'<img src="([^"]+)"')


def fetch_top_posts(subreddit, limit=5, timeframe="day"):
    """Fetch the top N posts from a subreddit's RSS feed for the given timeframe."""
    url = REDDIT_RSS_URL.format(subreddit=subreddit)
    params = {"t": timeframe, "limit": limit}

    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

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

        content_el = entry.find("atom:content", ATOM_NS)
        content_html = content_el.text if content_el is not None and content_el.text else ""
        thumb_match = THUMB_RE.search(content_html)
        thumbnail = thumb_match.group(1) if thumb_match else None

        posts.append({
            "title": title,
            "author": author,
            "url": link,
            "thumbnail": thumbnail,
        })
    return posts


def collect_all_posts(subreddits, per_sub, timeframe):
    """Fetch top posts for every subreddit, skipping any that fail."""
    sections = {}
    for sub in subreddits:
        try:
            sections[sub] = fetch_top_posts(sub, limit=per_sub, timeframe=timeframe)
            print(f"  r/{sub}: fetched {len(sections[sub])} post(s)")
        except (requests.RequestException, ET.ParseError) as e:
            print(f"  r/{sub}: failed to fetch - {e}", file=sys.stderr)
            sections[sub] = []
    return sections


def build_section_html(subreddit, posts):
    if not posts:
        return f"""
<h2 style="color:#222; font-family:Arial,Helvetica,sans-serif;">r/{escape(subreddit)}</h2>
<p style="color:#999; font-size:13px; font-family:Arial,Helvetica,sans-serif;">No posts found.</p>"""

    rows = []
    for p in posts:
        title_esc = escape(p["title"])
        thumb = (
            f'<img src="{escape(p["thumbnail"])}" width="64" height="64" style="border-radius:6px; object-fit:cover;">'
            if p["thumbnail"]
            else '<div style="width:64px; height:64px; background:#f0f0f0; border-radius:6px;"></div>'
        )
        rows.append(f"""
<tr>
  <td style="padding:10px 0; border-bottom:1px solid #eee;">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="vertical-align:top; padding-right:12px;">{thumb}</td>
      <td style="vertical-align:top; font-family:Arial,Helvetica,sans-serif;">
        <a href="{escape(p['url'] or '#')}" style="font-size:14px; font-weight:600; color:#1a1a1b; text-decoration:none;">{title_esc}</a>
        <div style="font-size:12px; color:#888; margin-top:4px;">u/{escape(p['author'])}</div>
      </td>
    </tr></table>
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
    print(f"Fetching top posts (timeframe={TIMEFRAME}) from: {', '.join(SUBREDDITS)}")
    sections = collect_all_posts(SUBREDDITS, POSTS_PER_SUB, TIMEFRAME)

    total = sum(len(posts) for posts in sections.values())
    if total == 0:
        print("No posts found - not sending an email.")
        return

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
