"""Fetch news for a specific stock and print headlines with links, grouped by date.

Uses the Google News RSS feed (no API key required).

Usage:
    python main.py "Aequs Ltd"
    python main.py "SentinelOne" --days 30
    python main.py                      # prompts for a stock name
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from xml.etree import ElementTree

import requests

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


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


def main():
    parser = argparse.ArgumentParser(description="Fetch stock news headlines with links, grouped by date.")
    parser.add_argument("stock", nargs="?", help='Stock/company name, e.g. "Aequs Ltd" or "SentinelOne"')
    parser.add_argument("--days", type=int, default=None, help="Only show news from the last N days")
    args = parser.parse_args()

    stock = args.stock or input("Enter stock/company name: ").strip()
    if not stock:
        print("No stock name given.")
        sys.exit(1)

    try:
        articles = fetch_news(stock, days=args.days)
    except requests.RequestException as exc:
        print(f"Failed to fetch news: {exc}")
        sys.exit(1)

    print_news(stock, articles)


if __name__ == "__main__":
    main()
