# Reddit Top Posts of the Day -> Email (runs on GitHub Actions, no local computer needed)

This repo emails you the top posts from your chosen subreddits every day,
automatically, using GitHub's free scheduled-workflow runners. Nothing needs
to run on your own machine.

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

4. **Create a free Reddit "script" app** (Reddit blocks unauthenticated
   requests from cloud IPs like GitHub Actions, so this uses Reddit's
   official OAuth API instead of scraping):
   - Go to https://www.reddit.com/prefs/apps
   - Click "create app" / "create another app" (bottom of the page)
   - Name: anything, e.g. `reddit-top-post-emailer`
   - Type: select **script**
   - redirect uri: `http://localhost:8080` (required field, but unused)
   - Click "create app"
   - Copy the string under the app name (that's your **client ID**) and the
     "secret" field (that's your **client secret**)

5. **Create a Gmail App Password** (your normal Gmail password won't work):
   - Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/two-step-verification
   - Then create an app password: https://myaccount.google.com/apppasswords
   - Choose "Mail" as the app, copy the 16-character password it gives you.

6. **Add your secrets to the repo** (this keeps your credentials out of the code):
   - In your repo: Settings -> Secrets and variables -> Actions -> "New repository secret"
   - Add five secrets:
     - `REDDIT_CLIENT_ID` = the client ID from step 4
     - `REDDIT_CLIENT_SECRET` = the client secret from step 4
     - `GMAIL_ADDRESS` = your Gmail address
     - `GMAIL_APP_PASSWORD` = the 16-character app password from step 5
     - `REDDIT_RECIPIENT` = the email address that should receive the digest

7. **Test it manually**
   - Go to the "Actions" tab in your repo
   - Click "Send Top Reddit Posts of the Day" on the left
   - Click "Run workflow" -> "Run workflow" (green button)
   - Wait ~10-20 seconds, refresh, click into the run to see logs / confirm success
   - Check the recipient inbox for the email

That's it — from now on it runs automatically every day at the time set in
`.github/workflows/send-digest.yml` (default 09:00 UTC), with no computer of
yours needing to be on.

## Changing the subreddits

Edit the `SUBREDDITS` line in `.github/workflows/send-digest.yml`:
```yaml
SUBREDDITS: "programming,python,technology"
```
Comma-separated, no spaces needed. You can also adjust `POSTS_PER_SUB` (top N
per subreddit) and `TIMEFRAME` (`hour`, `day`, `week`, `month`, `year`, `all`).

## Changing the schedule

Open `.github/workflows/send-digest.yml` and edit this line:
```
- cron: "0 9 * * *"
```
Cron format is `minute hour day month weekday`, always in **UTC**. Examples:
- `0 2 * * *` -> 2:00 AM UTC daily (9:00 AM in Vietnam, UTC+7)
- `30 23 * * *` -> 11:30 PM UTC daily
- `0 9 * * 1-5` -> 9:00 AM UTC, weekdays only

A handy converter: https://crontab.guru (shows what a cron string means, but
you still need to convert your local time to UTC yourself, e.g. via
https://www.timeanddate.com/worldclock/converter.html)

## Notes

- GitHub Actions free tier includes 2,000 minutes/month for private repos —
  this job takes well under a minute a day, so it's effectively free.
- You can also trigger it manually anytime via the "Run workflow" button.
- If the run fails, check the Actions tab -> the failed run -> logs. Common
  causes: a secret is missing/misspelled, or the Gmail app password was
  revoked.
