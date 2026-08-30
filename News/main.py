"""Fetch news for watchlist stocks and print headlines with links, grouped by date.

Uses the Google News RSS feed (no API key required).

By default this reads the watchlist at ``resources/watchlist.json`` and fetches
news for every stock in it. You can also pass a single stock name on the command
line to fetch news for just that stock.

Usage:
    python main.py                      # fetch news for every stock in the watchlist
    python main.py "Aequs Ltd"          # fetch news for a single stock
    python main.py --days 30            # only news from the last 30 days
    python main.py --watchlist path.json  # use a custom watchlist file
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from xml.etree import ElementTree

import requests

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Path to the shared watchlist: <repo root>/resources/watchlist.json
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WATCHLIST = os.path.join(_PROJECT_ROOT, "resources", "watchlist.json")


def load_watchlist(path=DEFAULT_WATCHLIST):
    """Return the list of ticker entries from the watchlist JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("tickers", [])


def fetch_news(stock, days=None):
    """Return a list of dicts: {date, headline, link, source} for the given stock."""
    query = quote_plus(f'"{stock}" stock')
    url = GOOGLE_NEWS_RSS.format(query=query)
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    root = ElementTree.fromstring(response.content)
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    articles = []
    for item in root.iter("item"):
        title = item.findtext("title", default="").strip()
        link = item.findtext("link", default="").strip()
        pub_date_raw = item.findtext("pubDate", default="").strip()
        source = item.findtext("source", default="").strip()

        pub_date = None
        if pub_date_raw:
            try:
                pub_date = parsedate_to_datetime(pub_date_raw)
            except (TypeError, ValueError):
                pass

        if cutoff and pub_date and pub_date < cutoff:
            continue

        articles.append({
            "date": pub_date,
            "headline": title,
            "link": link,
            "source": source,
        })

    articles.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return articles


def print_news(stock, articles):
    if not articles:
        print(f"No news found for '{stock}'.")
        return

    print(f"\nNews for '{stock}' ({len(articles)} articles)\n" + "=" * 60)
    current_day = None
    for article in articles:
        day = article["date"].strftime("%d %b %Y (%A)") if article["date"] else "Unknown date"
        if day != current_day:
            current_day = day
            print(f"\n📅 {day}\n" + "-" * 60)
        source = f" — {article['source']}" if article["source"] else ""
        print(f"• {article['headline']}{source}")
        print(f"  {article['link']}")


def fetch_watchlist_news(days=None, watchlist_path=DEFAULT_WATCHLIST):
    """Fetch and print news for every stock in the watchlist."""
    try:
        tickers = load_watchlist(watchlist_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Failed to load watchlist '{watchlist_path}': {exc}")
        sys.exit(1)

    if not tickers:
        print(f"No tickers found in watchlist '{watchlist_path}'.")
        return

    print(f"Fetching news for {len(tickers)} watchlist stocks from '{watchlist_path}'")
    for entry in tickers:
        # Use the company name for the query; fall back to the ticker symbol.
        stock = entry.get("name") or entry.get("ticker")
        if not stock:
            continue
        ticker = entry.get("ticker", "")
        header = f"{stock} ({ticker})" if ticker else stock
        print("\n" + "#" * 60)
        print(f"# {header}")
        print("#" * 60)
        try:
            articles = fetch_news(stock, days=days)
        except requests.RequestException as exc:
            print(f"Failed to fetch news for '{stock}': {exc}")
            continue
        print_news(stock, articles)


def main():
    parser = argparse.ArgumentParser(description="Fetch stock news headlines with links, grouped by date.")
    parser.add_argument(
        "stock",
        nargs="?",
        help='Stock/company name, e.g. "Aequs Ltd". If omitted, news is fetched for '
             "every stock in the watchlist.",
    )
    parser.add_argument("--days", type=int, default=None, help="Only show news from the last N days")
    parser.add_argument(
        "--watchlist",
        default=DEFAULT_WATCHLIST,
        help="Path to the watchlist JSON file (default: resources/watchlist.json)",
    )
    args = parser.parse_args()

    if args.stock:
        # Single-stock mode.
        try:
            articles = fetch_news(args.stock, days=args.days)
        except requests.RequestException as exc:
            print(f"Failed to fetch news: {exc}")
            sys.exit(1)
        print_news(args.stock, articles)
    else:
        # Watchlist mode.
        fetch_watchlist_news(days=args.days, watchlist_path=args.watchlist)


if __name__ == "__main__":
    main()
