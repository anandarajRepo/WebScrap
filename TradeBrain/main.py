"""
tradebrains_orders_scraper.py

Pulls article titles + links + publish dates from:
https://tradebrains.in/category/indian-markets/orders/

Designed to be run once a day (via cron / Task Scheduler / launchd).
It keeps a small "seen URLs" file so each run only reports NEW articles
since the last run, and appends everything to a running CSV log.

It can also fetch *all* articles across every category page and print them
grouped date-wise (newest day first), which is handy for a quick overview.

Install deps once:
    pip install requests beautifulsoup4 --break-system-packages

Usage:
    python3 main.py                # incremental run (only new articles)
    python3 main.py --all          # fetch every page, list everything date-wise
    python3 main.py --all --pages 5  # cap how many pages to walk
"""

import argparse
import csv
import json
import os
import re
from collections import defaultdict
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

# Date formats commonly emitted by WordPress themes in the listing markup.
_DATE_FORMATS = (
    "%Y-%m-%d",            # 2026-06-27 (from <time datetime="...">)
    "%Y-%m-%dT%H:%M:%S",   # ISO datetime without offset
    "%B %d, %Y",           # June 27, 2026
    "%b %d, %Y",           # Jun 27, 2026
    "%d %B %Y",            # 27 June 2026
    "%d %b %Y",            # 27 Jun 2026
    "%d-%m-%Y",            # 27-06-2026
    "%m/%d/%Y",            # 06/27/2026
)

UNKNOWN_DATE = "Unknown date"


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_urls):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_urls), f, indent=2)


def _parse_date_string(raw):
    """Try hard to turn an arbitrary date string into 'YYYY-MM-DD'. Return None on failure."""
    if not raw:
        return None
    raw = raw.strip()
    # An ISO datetime attribute may carry a timezone/offset suffix; keep the date part.
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date().isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    # Last resort: pull a "Month DD, YYYY" style date out of a longer blob of text.
    m = re.search(r"([A-Z][a-z]+ \d{1,2},? \d{4})", raw)
    if m:
        return _parse_date_string(m.group(1).replace(",", ", ").replace("  ", " "))
    return None


def _extract_date(h2_tag):
    """Find the publish date for the article whose headline is `h2_tag`.

    Walks up from the headline to the enclosing article container and probes a
    few common WordPress patterns: a <time datetime> attribute, then any element
    classed like a date, then a date-shaped substring of the container's text.
    Returns 'YYYY-MM-DD' or None if nothing usable is found.
    """
    # Determine the block that belongs to *this* article. Prefer an <article>
    # ancestor; otherwise climb upward only while the block still contains a
    # single headline, so we never wander into a neighbouring article's date.
    container = h2_tag.find_parent("article")
    if container is None:
        container = h2_tag.parent
        probe = container
        while probe is not None and probe.parent is not None:
            parent = probe.parent
            if len(parent.find_all("h2")) > 1:
                break
            probe = parent
        container = probe or container

    if container is None:
        return None

    # 1) <time datetime="..."> is the most reliable signal.
    for time_tag in container.find_all("time"):
        parsed = _parse_date_string(time_tag.get("datetime")) or _parse_date_string(
            time_tag.get_text(strip=True)
        )
        if parsed:
            return parsed

    # 2) Elements whose class/id hints at a date (e.g. "entry-date", "posted-on").
    for node in container.find_all(attrs={"class": re.compile(r"date|posted|time", re.I)}):
        parsed = _parse_date_string(node.get("datetime")) or _parse_date_string(
            node.get_text(" ", strip=True)
        )
        if parsed:
            return parsed

    # 3) Fall back to scanning the block's text for a date-shaped string.
    return _parse_date_string(container.get_text(" ", strip=True))


def _parse_articles(soup):
    """Extract {title, url, date} dicts from one listing page's soup."""
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
        articles.append(
            {"title": title, "url": link, "date": _extract_date(h2) or UNKNOWN_DATE}
        )
    return articles


def _page_url(page):
    """Build the URL for category page N (page 1 is the bare category URL)."""
    if page <= 1:
        return URL
    return f"{URL.rstrip('/')}/page/{page}/"


def fetch_articles(all_pages=False, max_pages=20):
    """Return list of dicts: {title, url, date}.

    By default fetches only the first listing page. With all_pages=True it walks
    /page/2/, /page/3/, ... until a page yields no new articles (or 404s), up to
    max_pages. Results are de-duped by URL while preserving discovery order.
    """
    collected = []
    seen_in_batch = set()
    last_page = max_pages if all_pages else 1

    for page in range(1, last_page + 1):
        try:
            resp = requests.get(_page_url(page), headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            # A 404 just means we've walked past the last page of results.
            if exc.response is not None and exc.response.status_code == 404:
                break
            raise

        soup = BeautifulSoup(resp.text, "html.parser")
        page_articles = _parse_articles(soup)

        new_on_page = 0
        for art in page_articles:
            if art["url"] not in seen_in_batch:
                seen_in_batch.add(art["url"])
                collected.append(art)
                new_on_page += 1

        # No fresh URLs on this page => pagination has run dry, stop early.
        if all_pages and new_on_page == 0:
            break

    return collected


def group_by_date(articles):
    """Group articles into [(date, [articles]), ...] sorted newest day first.

    The "unknown date" bucket, if any, always comes last.
    """
    buckets = defaultdict(list)
    for art in articles:
        buckets[art["date"]].append(art)

    real_dates = sorted((d for d in buckets if d != UNKNOWN_DATE), reverse=True)
    grouped = [(d, buckets[d]) for d in real_dates]
    if UNKNOWN_DATE in buckets:
        grouped.append((UNKNOWN_DATE, buckets[UNKNOWN_DATE]))
    return grouped


def print_date_wise(articles):
    """Pretty-print articles grouped under their publish date, newest day first."""
    if not articles:
        print("No articles found.")
        return

    grouped = group_by_date(articles)
    total = len(articles)
    print(f"Found {total} article(s) across {len(grouped)} date(s):\n")
    for date_str, items in grouped:
        print(f"=== {date_str} ({len(items)}) ===")
        for art in items:
            print(f"  - {art['title']}")
            print(f"    {art['url']}")
        print()


def append_to_csv(new_articles):
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["scraped_at_utc", "published_date", "title", "url"])
        scraped_at = datetime.now(timezone.utc).isoformat()
        for art in new_articles:
            writer.writerow(
                [scraped_at, art.get("date", UNKNOWN_DATE), art["title"], art["url"]]
            )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape TradeBrains 'orders' articles and list them date-wise."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch every category page and list all articles date-wise (no 'seen' filtering).",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=20,
        help="Max number of category pages to walk when using --all (default: 20).",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.all:
        # Overview mode: pull everything and print it grouped by date.
        articles = fetch_articles(all_pages=True, max_pages=args.pages)
        print_date_wise(articles)
        return

    # Default incremental mode: only report (and log) articles not seen before.
    seen = load_seen()
    articles = fetch_articles()

    new_articles = [a for a in articles if a["url"] not in seen]

    if new_articles:
        print(f"Found {len(new_articles)} new article(s):\n")
        print_date_wise(new_articles)
        append_to_csv(new_articles)
        seen.update(a["url"] for a in new_articles)
        save_seen(seen)
    else:
        print("No new articles since last run.")


if __name__ == "__main__":
    main()
