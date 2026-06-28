"""
tradebrains_orders_scraper.py

Pulls article titles + links + publish dates + the stock (company) each article
is about from:
https://tradebrains.in/category/indian-markets/orders/

The stock name is taken from the headline when possible (these headlines lead
with the company, e.g. "Texmaco Rail bags ..."). When a headline hides it behind
a sector word ("Defence stock jumps ..."), the article page is fetched to find
the company; pass --no-stock-fetch to skip that and rely on the headline only.

Designed to be run once a day (via cron / Task Scheduler / launchd).
It keeps a small "seen URLs" file so each run only reports NEW articles
since the last run, and appends everything to a running CSV log.

Both modes walk across *all* listing pages (/page/2/, /page/3/, ...), not just
the first one, so a run is not limited to the ~10 articles on page 1:
  * the default incremental run pages until it reaches articles it has already
    seen, so it picks up every new article since the last run, and
  * --all walks every page to the end and prints everything grouped date-wise
    (newest day first), which is handy for a quick overview / full backfill.

Install deps once:
    pip install requests beautifulsoup4 --break-system-packages

Usage:
    python3 main.py                # incremental run (all new articles, all pages)
    python3 main.py --all          # fetch every page, list everything date-wise
    python3 main.py --all --pages 5  # cap how many pages to walk
    python3 main.py --all --delay 0  # no pause between page requests
"""

import argparse
import csv
import json
import os
import re
import time
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

# Safety ceiling so a misbehaving site (e.g. one that loops back to page 1 for
# out-of-range requests) can never spin us forever. The real stop signal is an
# exhausted/looping listing, detected below; this is just a backstop.
DEFAULT_MAX_PAGES = 500
# Be polite: small pause between page requests so we don't hammer the server
# (which also makes us less likely to get rate-limited / blocked).
REQUEST_DELAY = 1.0

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
UNKNOWN_STOCK = "Unknown stock"

# In these headlines the company name comes first, then an action verb
# ("Texmaco Rail bags ...", "Prostarm Info Systems emerges ..."). We slice the
# title at the first such verb to recover the name without any extra requests.
_HEADLINE_ACTIONS = re.compile(
    r"\b(bags?|secures?|wins?|receives?|received|emerges?|gets?|got|lands?|"
    r"jumps?|surges?|rises?|rose|soars?|rallies|rally|gains?|hits?|signs?|inks?|"
    r"to\s+supply|to\s+set\s+up|to\s+build|posts?|reports?|announces?|approves?|"
    r"declares?|fixes?|completes?|raises?)\b",
    re.I,
)

# When the leading chunk is just a sector/placeholder rather than a real name,
# the company is hidden in the body (e.g. "Defence stock jumps ..."), so we
# treat it as "no name in the title" and let the caller look inside the article.
_GENERIC_SUBJECT = re.compile(
    r"\b(stock|stocks|share|shares|scrip|company|companies|firm|player|giant|"
    r"maker|psu|multibagger|smallcap|midcap|largecap)\b",
    re.I,
)

# A link inside an article pointing at a TradeBrains stock/analysis page is a
# strong signal for the company the piece is about.
_STOCK_LINK_RE = re.compile(
    r"(?:portal\.)?tradebrains\.in/(?:portal/)?(?:stock|share-price|company)/",
    re.I,
)


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


def _company_from_title(title):
    """Best-effort company name from a headline, or None if the title hides it.

    These headlines put the company first, then an action verb
    ("Texmaco Rail bags ...", "Prostarm Info Systems emerges ..."), so the words
    before the first such verb are the company. Some headlines instead lead with
    a sector placeholder ("Defence stock jumps ...") -- there is no real name to
    take, so we return None and let the caller look inside the article.
    """
    if not title:
        return None
    head = _HEADLINE_ACTIONS.split(title, maxsplit=1)[0]
    head = head.strip(" -–—:|")
    if not head or _GENERIC_SUBJECT.search(head):
        return None
    return head


def _extract_stock_from_page(soup):
    """Pull a company name out of a fetched article page, or None.

    Tries an in-article link to a TradeBrains stock page first (its anchor text
    is usually the company name), then falls back to the post's tag links.
    """
    # 1) A link to a stock/analysis page; its anchor text names the company.
    for a in soup.find_all("a", href=True):
        if _STOCK_LINK_RE.search(a["href"]):
            name = a.get_text(" ", strip=True)
            if name and len(name) > 2 and not _GENERIC_SUBJECT.fullmatch(name):
                return name

    # 2) WordPress post tags (rel="tag") often include the company name.
    for a in soup.find_all("a", rel=True):
        rel = [r.lower() for r in (a.get("rel") or [])]
        if "tag" in rel:
            name = a.get_text(" ", strip=True)
            if name and len(name) > 2 and not _GENERIC_SUBJECT.search(name):
                return name

    return None


def fetch_stock_name(url, delay=REQUEST_DELAY, verbose=False):
    """Fetch one article and return the company name mentioned, or None.

    Network/parse errors are swallowed (returning None) so resolving a stock
    name can never break the main scrape.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        if verbose:
            print(f"  (could not fetch {url} for stock name: {exc})")
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    return _extract_stock_from_page(soup)


def enrich_stock_names(articles, fetch=True, delay=REQUEST_DELAY, verbose=False):
    """Fill in each article's 'stock' field (the company the article is about).

    Tries the headline first (cheap, no request). For articles whose headline
    hides the company behind a sector word, optionally fetches the article to
    find it. Falls back to UNKNOWN_STOCK when nothing usable is found.
    """
    for art in articles:
        name = _company_from_title(art["title"])
        if name is None and fetch:
            name = fetch_stock_name(art["url"], delay=delay, verbose=verbose)
            if delay:
                time.sleep(delay)
        art["stock"] = name or UNKNOWN_STOCK
    return articles


def _page_url(page):
    """Build the URL for category page N (page 1 is the bare category URL)."""
    if page <= 1:
        return URL
    return f"{URL.rstrip('/')}/page/{page}/"


def fetch_articles(max_pages=DEFAULT_MAX_PAGES, stop_when_seen=None,
                   delay=REQUEST_DELAY, verbose=False):
    """Walk listing pages and return list of dicts: {title, url, date}.

    Starts at the bare category URL (page 1) and follows /page/2/, /page/3/, ...
    collecting every article it finds, de-duped by URL while preserving
    discovery order. Walking stops when any of these happen:

      * a page 404s (we've gone past the last page of results), or
      * a page yields no URLs we haven't already collected this run (the listing
        has run dry or looped back to an earlier page), or
      * `stop_when_seen` is provided and a page contains nothing outside that set
        -- an incremental short-circuit: the listing is newest-first, so once a
        whole page is already known there is nothing newer left to find, or
      * `max_pages` is reached (a safety backstop, not the normal terminator).
    """
    collected = []
    seen_in_batch = set()

    for page in range(1, max_pages + 1):
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
        unseen_on_page = 0
        for art in page_articles:
            if art["url"] not in seen_in_batch:
                seen_in_batch.add(art["url"])
                collected.append(art)
                new_on_page += 1
                if stop_when_seen is None or art["url"] not in stop_when_seen:
                    unseen_on_page += 1

        if verbose:
            print(f"  page {page}: {len(page_articles)} listed, "
                  f"{new_on_page} new this run ({len(collected)} total)")

        # No fresh URLs on this page => pagination has run dry / looped, stop.
        if new_on_page == 0:
            break

        # Incremental mode: this whole page is already known, so everything
        # older is too. Nothing newer remains -- stop early.
        if stop_when_seen is not None and unseen_on_page == 0:
            break

        # Polite pause before requesting the next page.
        if delay and page < max_pages:
            time.sleep(delay)

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
            print(f"    {art['url']}  [Stock: {art.get('stock', UNKNOWN_STOCK)}]")
        print()


def append_to_csv(new_articles):
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                ["scraped_at_utc", "published_date", "stock", "title", "url"]
            )
        scraped_at = datetime.now(timezone.utc).isoformat()
        for art in new_articles:
            writer.writerow(
                [
                    scraped_at,
                    art.get("date", UNKNOWN_DATE),
                    art.get("stock", UNKNOWN_STOCK),
                    art["title"],
                    art["url"],
                ]
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
        default=DEFAULT_MAX_PAGES,
        help=(
            "Safety cap on how many category pages to walk (default: "
            f"{DEFAULT_MAX_PAGES}). Walking normally stops on its own when the "
            "listing runs out; raise this only if there are more pages than the cap."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"Seconds to wait between page requests (default: {REQUEST_DELAY}).",
    )
    parser.add_argument(
        "--no-stock-fetch",
        action="store_true",
        help=(
            "Don't open individual articles to resolve stock names hidden behind "
            "generic headlines (e.g. 'Defence stock jumps ...'); rely on the "
            "headline alone. Faster, but such articles show 'Unknown stock'."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.all:
        # Overview mode: walk every page and print everything grouped by date.
        articles = fetch_articles(
            max_pages=args.pages, delay=args.delay, verbose=True
        )
        enrich_stock_names(
            articles, fetch=not args.no_stock_fetch, delay=args.delay, verbose=True
        )
        print_date_wise(articles)
        return

    # Default incremental mode: only report (and log) articles not seen before.
    # We still walk across pages (not just page 1), stopping once we reach a page
    # we've already fully seen -- so a run catches *every* new article, not 10.
    seen = load_seen()
    articles = fetch_articles(
        max_pages=args.pages, stop_when_seen=seen, delay=args.delay
    )

    new_articles = [a for a in articles if a["url"] not in seen]

    if new_articles:
        enrich_stock_names(
            new_articles, fetch=not args.no_stock_fetch, delay=args.delay
        )
        print(f"Found {len(new_articles)} new article(s):\n")
        print_date_wise(new_articles)
        append_to_csv(new_articles)
        seen.update(a["url"] for a in new_articles)
        save_seen(seen)
    else:
        print("No new articles since last run.")


if __name__ == "__main__":
    main()
