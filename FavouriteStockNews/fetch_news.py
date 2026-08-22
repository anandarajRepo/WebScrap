"""Stock news fetch agent.

Pulls news from free RSS feeds (Economic Times Markets, MoneyControl,
Business Standard) plus, optionally, the NewsAPI free tier. Filters
articles against a configurable watchlist of NSE tickers/company names,
dedupes against previously seen articles, saves matched articles to a
per-day JSON file, and pushes real-time alerts to Telegram.

Designed to run on a schedule via GitHub Actions on stateless runners, so
dedup state is persisted to ``seen_urls.json`` committed back to the repo.

Environment variables (all optional except where a feature is used):
    TELEGRAM_BOT_TOKEN  - Telegram bot token (for alerts)
    TELEGRAM_CHAT_ID    - Telegram chat id (for alerts)
    NEWSAPI_KEY         - NewsAPI.org API key (enables the NewsAPI source)
"""

import datetime
import hashlib
import json
import logging
import os
import re

import feedparser
import requests

import telegram_notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("fetch_news")

# --- Paths -----------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
SEEN_URLS_PATH = os.path.join(BASE_DIR, "seen_urls.json")
DATA_DIR = os.path.join(BASE_DIR, "data")

# --- News sources ----------------------------------------------------------
# Free RSS feeds. Each entry is (source_name, feed_url).
RSS_FEEDS = [
    ("Economic Times Markets",
     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("MoneyControl",
     "https://www.moneycontrol.com/rss/marketsnews.xml"),
    ("MoneyControl Business",
     "https://www.moneycontrol.com/rss/business.xml"),
    ("Business Standard Markets",
     "https://www.business-standard.com/rss/markets-106.rss"),
]

# Cap so a large first run doesn't send hundreds of Telegram messages.
MAX_ALERTS_PER_RUN = 30

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; FavouriteStockNewsBot/1.0; "
        "+https://github.com/anandarajRepo/WebScrap)"
    )
}


# --- Config / state helpers -----------------------------------------------
def load_watchlist():
    """Load the watchlist and return a list of ticker config dicts."""
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    tickers = data.get("tickers", [])
    logger.info("Loaded watchlist with %d ticker(s).", len(tickers))
    return tickers


def load_seen_urls():
    """Load the set of previously seen article keys (URL / title hash)."""
    if not os.path.exists(SEEN_URLS_PATH):
        return set()
    try:
        with open(SEEN_URLS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Stored as a list for readable diffs in git.
        return set(data.get("seen", []))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s); starting fresh.",
                       SEEN_URLS_PATH, exc)
        return set()


def save_seen_urls(seen):
    """Persist the seen-keys set back to disk (sorted for stable diffs)."""
    payload = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "count": len(seen),
        "seen": sorted(seen),
    }
    with open(SEEN_URLS_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def article_key(url, title):
    """Return a stable dedup key: the URL if present, else a title hash."""
    if url:
        return url.strip()
    digest = hashlib.sha256(title.strip().lower().encode("utf-8")).hexdigest()
    return "titlehash:" + digest


# --- Matching --------------------------------------------------------------
def build_matchers(tickers):
    """Build (ticker, compiled_regex) matchers from watchlist aliases.

    Matching is case-insensitive and uses word boundaries so that, e.g.,
    "HDFC" does not match inside an unrelated longer token.
    """
    matchers = []
    for entry in tickers:
        ticker = entry.get("ticker", "").strip()
        if not ticker:
            continue
        terms = set(entry.get("aliases", []))
        terms.add(ticker)
        name = entry.get("name")
        if name:
            terms.add(name)
        # Longer terms first so the most specific alias wins.
        patterns = [re.escape(t.strip()) for t in terms if t and t.strip()]
        if not patterns:
            continue
        patterns.sort(key=len, reverse=True)
        regex = re.compile(
            r"(?<![\w])(" + "|".join(patterns) + r")(?![\w])",
            re.IGNORECASE,
        )
        matchers.append((ticker, regex))
    return matchers


def match_ticker(text, matchers):
    """Return the first ticker whose alias appears in text, else None."""
    if not text:
        return None
    for ticker, regex in matchers:
        if regex.search(text):
            return ticker
    return None


# --- Sources ---------------------------------------------------------------
def parse_published(entry):
    """Best-effort ISO 8601 published date from a feed entry."""
    for attr in ("published_parsed", "updated_parsed"):
        tstruct = entry.get(attr)
        if tstruct:
            try:
                return datetime.datetime(*tstruct[:6],
                                         tzinfo=datetime.timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
    return entry.get("published") or entry.get("updated") or ""


def fetch_rss_articles():
    """Fetch and normalise articles from all configured RSS feeds.

    Returns a list of dicts with keys: title, source, url, published_date,
    summary. Never raises on a single feed failure.
    """
    articles = []
    for source_name, url in RSS_FEEDS:
        try:
            logger.info("Fetching RSS: %s", source_name)
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            if feed.bozo:
                logger.warning("Feed %s reported a parse warning: %s",
                               source_name, feed.bozo_exception)
            for entry in feed.entries:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                articles.append({
                    "title": title,
                    "source": source_name,
                    "url": (entry.get("link") or "").strip(),
                    "published_date": parse_published(entry),
                    "summary": (entry.get("summary") or "").strip(),
                })
            logger.info("  -> %d entries", len(feed.entries))
        except (requests.RequestException, Exception) as exc:  # noqa: BLE001
            # Deliberately broad: one bad feed must not sink the run.
            logger.error("Failed to fetch/parse feed %s: %s",
                         source_name, exc)
    return articles


def fetch_newsapi_articles(tickers):
    """Fetch articles from NewsAPI free tier if NEWSAPI_KEY is set.

    Queries the everything endpoint with an OR of the watchlist names.
    Returns [] if no key is configured or on any error.
    """
    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        return []

    query_terms = []
    for entry in tickers:
        name = entry.get("name") or entry.get("ticker")
        if name:
            query_terms.append(f'"{name}"')
    if not query_terms:
        return []

    query = " OR ".join(query_terms)
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 100,
        "apiKey": api_key,
    }

    articles = []
    try:
        logger.info("Fetching NewsAPI (everything endpoint)")
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params=params,
            headers=REQUEST_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            logger.error("NewsAPI returned status=%s message=%s",
                         data.get("status"), data.get("message"))
            return []
        for item in data.get("articles", []):
            title = (item.get("title") or "").strip()
            if not title:
                continue
            source = (item.get("source") or {}).get("name") or "NewsAPI"
            articles.append({
                "title": title,
                "source": f"NewsAPI/{source}",
                "url": (item.get("url") or "").strip(),
                "published_date": item.get("publishedAt") or "",
                "summary": (item.get("description") or "").strip(),
            })
        logger.info("  -> %d NewsAPI articles", len(articles))
    except (requests.RequestException, ValueError, Exception) as exc:  # noqa: BLE001
        logger.error("Failed to fetch NewsAPI: %s", exc)
    return articles


# --- Storage ---------------------------------------------------------------
def daily_data_path(now=None):
    """Return the path to today's per-day JSON data file."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return os.path.join(DATA_DIR, f"news_{now:%Y-%m-%d}.json")


def load_daily_data(path):
    """Load existing entries from a per-day data file (list)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s); starting a new file.",
                       path, exc)
        return []


def save_daily_data(path, entries):
    """Write the day's entries back to disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# --- Main ------------------------------------------------------------------
def run():
    os.makedirs(DATA_DIR, exist_ok=True)

    tickers = load_watchlist()
    matchers = build_matchers(tickers)
    seen = load_seen_urls()
    initial_seen_count = len(seen)

    raw_articles = fetch_rss_articles()
    raw_articles.extend(fetch_newsapi_articles(tickers))
    logger.info("Collected %d raw article(s) from all sources.",
                len(raw_articles))

    now = datetime.datetime.now(datetime.timezone.utc)
    fetch_timestamp = now.isoformat()

    new_entries = []
    for art in raw_articles:
        key = article_key(art["url"], art["title"])
        if key in seen:
            continue

        # Match against title + summary for better recall.
        haystack = f"{art['title']} {art.get('summary', '')}"
        ticker = match_ticker(haystack, matchers)
        if not ticker:
            # Not on the watchlist; do NOT mark as seen (a later run with an
            # expanded watchlist should still be able to pick it up).
            continue

        seen.add(key)
        entry = {
            "title": art["title"],
            "source": art["source"],
            "url": art["url"],
            "published_date": art["published_date"],
            "matched_ticker": ticker,
            "fetch_timestamp": fetch_timestamp,
        }
        new_entries.append(entry)

    logger.info("Found %d new matching article(s).", len(new_entries))

    if not new_entries:
        logger.info("Nothing new. Exiting without changes.")
        return

    # Append to today's per-day file.
    path = daily_data_path(now)
    entries = load_daily_data(path)
    entries.extend(new_entries)
    save_daily_data(path, entries)
    logger.info("Wrote %d entrie(s) to %s (total %d).",
                len(new_entries), os.path.relpath(path, BASE_DIR),
                len(entries))

    # Persist dedup state (only changed if we added keys).
    if len(seen) != initial_seen_count:
        save_seen_urls(seen)
        logger.info("Updated seen_urls.json (now %d keys).", len(seen))

    # Telegram alerts (batched). Never fails the run.
    to_alert = new_entries[:MAX_ALERTS_PER_RUN]
    if len(new_entries) > MAX_ALERTS_PER_RUN:
        logger.info("Capping alerts at %d (of %d new).",
                    MAX_ALERTS_PER_RUN, len(new_entries))
    try:
        if telegram_notify.is_configured():
            sent = telegram_notify.send_articles(to_alert)
            logger.info("Telegram alert sent: %s", sent)
        else:
            logger.info("Telegram not configured; skipping alerts.")
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error sending Telegram alerts: %s", exc)


if __name__ == "__main__":
    run()
