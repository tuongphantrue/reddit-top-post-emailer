# Reddit Top Posts of the Day -> Email (runs on GitHub Actions, no local computer needed)

This repo emails you the top posts across all of Reddit (r/all) on a
schedule, automatically, using GitHub's free scheduled-workflow runners.
Nothing needs to run on your own machine. You can blacklist subreddits you
don't want included.

## One-time setup (~5 minutes)

1. **Create a GitHub account** if you don't have one: https://github.com/join

2. **Create a new repository**
   - Click "+" (top right) -> "New repository"
   - Name it anything, e.g. `reddit-top-post-emailer`
   - Set it to **Private** (recommended, keeps your workflow config private)
   - Click "Create repository"

3. **Upload these files** to the repo (drag-and-drop works fine via the GitHub
   web UI: "Add file" -> "Upload files"), keeping the folder structure:
   - `reddit_top_post_emailer.py`
   - `requirements.txt`
   - `.github/workflows/send-digest.yml`

4. **Create a Gmail App Password** (your normal Gmail password won't work):
   - Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/two-step-verification
   - Then create an app password: https://myaccount.google.com/apppasswords
   - Choose "Mail" as the app, copy the 16-character password it gives you.

5. **Add your secrets to the repo** (this keeps your email/password out of the code):
   - In your repo: Settings -> Secrets and variables -> Actions -> "New repository secret"
   - Add three secrets:
     - `GMAIL_ADDRESS` = your Gmail address
     - `GMAIL_APP_PASSWORD` = the 16-character app password from step 4
     - `REDDIT_RECIPIENT` = the email address that should receive the digest

6. **Test it manually**
   - Go to the "Actions" tab in your repo
   - Click "Send Top Reddit Posts of the Day" on the left
   - Click "Run workflow" -> "Run workflow" (green button)
   - Wait ~10-20 seconds, refresh, click into the run to see logs / confirm success
   - Check the recipient inbox for the email

That's it — from now on it runs automatically every day at the time set in
`.github/workflows/send-digest.yml` (default 09:00 UTC), with no computer of
yours needing to be on.

## Blacklisting subreddits

Edit the `BLACKLIST_SUBREDDITS` line in `.github/workflows/send-digest.yml`:
```yaml
BLACKLIST_SUBREDDITS: "nsfw,gonewild,AskReddit"
```
Comma-separated subreddit names (no `r/` prefix needed), case-insensitive.
Any post from a blacklisted subreddit is dropped before the email is built.
It starts empty — add subreddits to it any time.

You can also adjust `POSTS_TOTAL` (how many top posts to pull from r/all
before filtering — the blacklist is applied *after* this, so a small
`POSTS_TOTAL` with a big blacklist might leave few posts) and `TIMEFRAME`
(`hour`, `day`, `week`, `month`, `year`, `all`).

## Changing the schedule

Open `.github/workflows/send-digest.yml` and edit this line:
```
- cron: "*/30 * * * *"
```
Cron format is `minute hour day month weekday`, always in **UTC**. Examples:
- `*/30 * * * *` -> every 30 minutes (current setting)
- `0 2 * * *` -> 2:00 AM UTC daily (9:00 AM in Vietnam, UTC+7)
- `0 9 * * 1-5` -> 9:00 AM UTC, weekdays only

A handy converter: https://crontab.guru (shows what a cron string means, but
you still need to convert your local time to UTC yourself, e.g. via
https://www.timeanddate.com/worldclock/converter.html)

## A note on reliability

This fetches posts via Reddit's public RSS feeds rather than the official
API, because as of 2026 Reddit has largely closed off new personal API app
registration. RSS has historically been more lenient toward unauthenticated
requests than the JSON scrape endpoint, but Reddit could tighten this up at
any time without notice. If a run starts failing with 403s again, check the
Actions tab logs first - there's currently no fully "official" workaround
available for new personal projects.

Also note: since this runs every 30 minutes with no de-duplication, you'll
get repeat emails of the same top posts throughout the day. Let me know if
you'd like a "don't re-send the same post" tracker added.

## Notes

- GitHub Actions free tier includes 2,000 minutes/month for private repos —
  this job takes well under a minute a day, so it's effectively free.
- You can also trigger it manually anytime via the "Run workflow" button.
- If the run fails, check the Actions tab -> the failed run -> logs. Common
  causes: a secret is missing/misspelled, or the Gmail app password was
  revoked.
