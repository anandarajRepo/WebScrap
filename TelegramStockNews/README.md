# FavouriteStockNews

A Python agent that fetches news for a configurable watchlist of NSE stock
tickers / company names (e.g. `RELIANCE`, `TCS`, `INFY`), saves matched
articles as raw JSON in this repo, and pushes real-time alerts to Telegram.
It is designed to run every 30 minutes on **GitHub Actions**.

## What it does

1. **Sources** – Pulls from free RSS feeds (Economic Times Markets,
   MoneyControl, Business Standard) plus, optionally, the
   [NewsAPI](https://newsapi.org) free tier.
2. **Filter** – Matches article title + summary against the watchlist
   keywords (company name + aliases), case-insensitively with word
   boundaries.
3. **Dedup** – Tracks already-seen articles by URL (or a title hash when no
   URL is present) in `seen_urls.json`, committed to the repo so state
   survives across stateless Action runs.
4. **Storage** – Appends matched articles to a per-day file
   `data/news_YYYY-MM-DD.json`. Each entry includes `title`, `source`,
   `url`, `published_date`, `matched_ticker`, and `fetch_timestamp`.
5. **Alerts** – Sends a formatted Telegram message for each new article,
   **batched into one message per run** when several are found.
6. **Resilience** – A single dead feed or a failed Telegram send is logged
   and skipped; it never fails the whole run.

## Files

| File | Purpose |
| --- | --- |
| `fetch_news.py` | Main agent: fetch, filter, dedupe, store, notify. |
| `telegram_notify.py` | Telegram sending helper (batching, HTML formatting). |
| `../resources/watchlist.json` | Tickers / company names / aliases to track (shared, at the repo root). |
| `requirements.txt` | Python dependencies. |
| `seen_urls.json` | Persisted dedup state (committed by the workflow). |
| `data/` | Per-day output JSON files. |
| `../.github/workflows/fetch-news.yml` | Scheduled GitHub Actions workflow (lives at the repo root). |

## Configuration — `../resources/watchlist.json`

```json
{
  "tickers": [
    {
      "ticker": "RELIANCE",
      "name": "Reliance Industries",
      "aliases": ["Reliance", "RIL", "Reliance Jio", "Mukesh Ambani"]
    }
  ]
}
```

- `ticker` – the NSE symbol, also stored as `matched_ticker`.
- `name` – full company name (also used for NewsAPI queries).
- `aliases` – extra keywords to match (short forms, brands, key people).

Add or remove entries freely; the agent picks up changes on the next run.

## Setup

### 1. Create a Telegram bot (via BotFather)

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts (choose a name and a username
   ending in `bot`).
3. BotFather replies with a **bot token** like
   `123456789:AAExampleTokenStringHere`. This is your `TELEGRAM_BOT_TOKEN`.

### 2. Find your chat ID

1. Send any message to your new bot (e.g. "hi") — the bot must have
   received at least one message from you.
2. Visit this URL in a browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id":<number>...}` in the JSON response. That number is
   your `TELEGRAM_CHAT_ID`.
   - For a **group chat**, add the bot to the group, post a message, then
     read `getUpdates` again — the group id is usually negative
     (e.g. `-1001234567890`).

### 3. (Optional) NewsAPI key

Sign up at <https://newsapi.org> for a free API key and provide it as
`NEWSAPI_KEY` to enable the extra source. Omit it to use RSS feeds only.

### 4. Add GitHub secrets

In the repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `NEWSAPI_KEY` *(optional)*

The workflow also needs write access to push data back; it uses the built-in
`GITHUB_TOKEN` with `contents: write` (already set in the workflow). Ensure
**Settings → Actions → General → Workflow permissions** allows
"Read and write permissions".

## Running locally

```bash
cd TelegramStockNews
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="123456789:AA..."
export TELEGRAM_CHAT_ID="123456789"
export NEWSAPI_KEY="..."   # optional

python fetch_news.py
```

Without the Telegram variables the agent still fetches and stores data; it
just skips (and logs) the alerts.

## Schedule

The workflow runs every 30 minutes (`*/30 * * * *`, UTC) and can also be
triggered manually from the **Actions** tab. GitHub cron runs are
best-effort and may be delayed during high load.
