"""
tradebrains_orders_scraper.py

Pulls the latest article titles + links from:
https://tradebrains.in/category/indian-markets/orders/

Designed to be run once a day (via cron / Task Scheduler / launchd).
It keeps a small "seen URLs" file so each run only reports NEW articles
since the last run, and appends everything to a running CSV log.

Install deps once:
    pip install requests beautifulsoup4 --break-system-packages

Usage:
    python3 tradebrains_orders_scraper.py
"""

import csv
import json
import os
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

URL = "https://tradebrains.in/category/indian-markets/orders/"
HEADERS = {
    # A normal browser UA avoids basic bot-blocking on some WP sites
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(BASE_DIR, "tradebrains_orders_seen.json")
LOG_FILE = os.path.join(BASE_DIR, "tradebrains_orders_log.csv")


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_urls):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_urls), f, indent=2)


def fetch_articles():
    """Return list of dicts: {title, url, posted}"""
    resp = requests.get(URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    articles = []
    # Each article headline on this WP theme is an <h2> containing a single <a>
    for h2 in soup.find_all("h2"):
        a_tag = h2.find("a", href=True)
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        link = a_tag["href"]
        if not title or not link.startswith("https://tradebrains.in/"):
            continue
        articles.append({"title": title, "url": link})

    # De-dupe within this single fetch while preserving order
    deduped, seen_in_batch = [], set()
    for art in articles:
        if art["url"] not in seen_in_batch:
            seen_in_batch.add(art["url"])
            deduped.append(art)
    return deduped


def append_to_csv(new_articles):
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["scraped_at_utc", "title", "url"])
        scraped_at = datetime.now(timezone.utc).isoformat()
        for art in new_articles:
            writer.writerow([scraped_at, art["title"], art["url"]])


def main():
    seen = load_seen()
    articles = fetch_articles()

    new_articles = [a for a in articles if a["url"] not in seen]

    if new_articles:
        print(f"Found {len(new_articles)} new article(s):\n")
        for art in new_articles:
            print(f"- {art['title']}\n  {art['url']}\n")
        append_to_csv(new_articles)
        seen.update(a["url"] for a in new_articles)
        save_seen(seen)
    else:
        print("No new articles since last run.")


if __name__ == "__main__":
    main()