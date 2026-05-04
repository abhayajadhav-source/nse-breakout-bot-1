# NSE 100 Breakout Bot — Free Gmail + Yahoo Pipeline

Scans Nifty 100 every 15 minutes during NSE market hours, flags 52-week
high/low breakouts, fetches volume + news, and emails you via Gmail SMTP.
Sends only when at least one breakout is detected.

## Free stack
- Market data + news: Yahoo Finance (no API key)
- Alerts: Gmail SMTP (app password, free)
- Schedule: GitHub Actions cron (free for public repos)

## Setup
1. Enable 2-Step Verification on your Google account.
2. Create an App Password at https://myaccount.google.com/apppasswords
   ("App name" can be anything, e.g. "NSE Bot"). Save the 16-char password.
3. Repo → **Settings → Secrets and variables → Actions → New secret**.
   Add three secrets:
   - `GMAIL_USER` — your Gmail address (the sender)
   - `GMAIL_APP_PASSWORD` — the 16-char app password
   - `EMAIL_TO` — recipient email(s), comma-separated for multiple
4. Repo → **Actions → Mock Run (manual) → Run workflow** to verify.
5. Live schedule starts automatically — every 15 min, 09:15–15:30 IST, Mon–Fri.

## Tuning
- `PROXIMITY=0.0` — strict (price must touch the extreme).
- `PROXIMITY=0.005` — within 0.5% (catches near-breakouts).
- Edit `NIFTY_100` in `scanner.py` for a different universe.

## Caveats
- GitHub cron can drift 5–20 min during peak load.
- GitHub disables scheduled workflows after 60 days of repo inactivity.
- Yahoo NSE data is delayed ~15 min. For real-time, swap `fetch_ticker_snapshot()`
  to a paid feed (e.g. DhanHQ ₹499/month).
- Gmail SMTP free limit: ~500 outgoing emails/day, well above this bot's needs
  (max ~26 emails/day if every cycle hits).
