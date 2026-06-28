"""
hedgeflow_top_funds_scraper.py

Scrapes https://hedgefollow.com/ for the "Top Searched Hedge Funds" listed on the
home page and, for each of those funds, the NEW stock buys (brand-new positions)
they opened in their most recent 13F filing.

How it works
------------
1. Fetch the home page and locate the "Top Searched Hedge Funds" panel. Every
   fund there links to its own page at  https://hedgefollow.com/funds/<Name>
   (spaces encoded as '+', e.g. /funds/ARK+Investment+Management). We collect the
   fund name + that link.
2. For each fund, fetch its page and read the holdings / recent-trades table. Each
   row carries a "recent activity" cell describing what the fund did with that
   position last quarter: e.g. "Buy", "Add 12%", "Reduce 8%", "Sell", and -- for a
   position the fund did not hold at all the previous quarter -- a NEW marker
   ("New", "New Buy", "Buy New"). We keep only the rows flagged NEW: those are the
   fund's new stock buys.

The exact HTML hedgefollow.com emits could not be inspected from the build
environment (the host is blocked by the egress policy), so the parser is written
defensively: it finds the right table/section by its surrounding heading text and
column headers rather than by brittle fixed ids/classes, and falls back to scanning
every "/funds/" link / every table when a heading is not found. If hedgefollow
changes its markup, the heading/column keywords near the top of this file are the
first place to adjust.

Install deps once:
    pip install requests beautifulsoup4 --break-system-packages

Usage:
    python3 main.py                       # list each top fund + its NEW buys
    python3 main.py --limit 5             # only the first 5 top-searched funds
    python3 main.py --csv buys.csv        # also write the results to a CSV
    python3 main.py --delay 0.5           # pause between requests (default 1.0s)
    python3 main.py --include-all-activity # don't filter -- show every recent trade
"""

import argparse
import csv
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://hedgefollow.com/"
HEADERS = {
    # A normal browser UA avoids basic bot-blocking.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Be polite: small pause between requests so we don't hammer the server.
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 25

# The home-page panel we want is titled (roughly) "Top Searched Hedge Funds".
# Match loosely so minor wording/spacing changes still resolve.
TOP_SEARCHED_HEADING = re.compile(r"top\s+searched\s+hedge\s+funds", re.I)

# Fund pages live at hedgefollow.com/funds/<Name>; this matches such links.
FUND_LINK_RE = re.compile(r"/funds/[^/?#]+", re.I)

# A holdings/trades row's "recent activity" cell flags a brand-new position with
# some form of the word "new" (e.g. "New", "New Buy", "Buy New", "Add (New)").
# That is what distinguishes a new buy from merely adding to an existing one.
NEW_BUY_RE = re.compile(r"\bnew\b", re.I)

# Header keywords that identify the "recent activity" / change column of a
# holdings table, so we know which cell to test for a NEW marker.
ACTIVITY_HEADER_RE = re.compile(r"activity|change|action|recent|trade|buy/sell", re.I)
# Header keywords for the stock name / ticker columns, so we can report what was
# bought rather than just "a new position".
STOCK_HEADER_RE = re.compile(r"stock|company|security|holding|name", re.I)
TICKER_HEADER_RE = re.compile(r"ticker|symbol", re.I)


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch_soup(session, url):
    """GET `url` and return a parsed BeautifulSoup, or None on any error."""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! could not fetch {url}: {exc}", file=sys.stderr)
        return None
    return BeautifulSoup(resp.text, "html.parser")


def _clean(text):
    """Collapse whitespace in a cell's text."""
    return re.sub(r"\s+", " ", (text or "")).strip()


# --------------------------------------------------------------------------- #
# Step 1: top-searched funds off the home page
# --------------------------------------------------------------------------- #
def _heading_container(soup):
    """Return the element wrapping the 'Top Searched Hedge Funds' panel, or None.

    We find the heading by its text, then climb to the nearest ancestor that also
    contains fund links -- that ancestor is the panel/card holding the list.
    """
    heading = soup.find(string=TOP_SEARCHED_HEADING)
    if heading is None:
        return None

    node = getattr(heading, "parent", None)
    # Walk up until we find an ancestor that actually contains "/funds/" links;
    # that block is the panel. Stop climbing once we hit one (don't over-reach
    # into the whole page and pull in unrelated fund links elsewhere).
    while node is not None:
        if node.find("a", href=FUND_LINK_RE):
            return node
        node = node.parent
    return None


def extract_top_searched_funds(soup):
    """Return [{name, url}, ...] for the 'Top Searched Hedge Funds' panel.

    De-duplicated by fund URL, discovery order preserved. Falls back to scanning
    every fund link on the page if the heading/panel cannot be located.
    """
    container = _heading_container(soup)
    scope = container if container is not None else soup
    if container is None:
        print(
            "  ! 'Top Searched Hedge Funds' heading not found; "
            "falling back to all fund links on the page.",
            file=sys.stderr,
        )

    funds = []
    seen = set()
    for a in scope.find_all("a", href=FUND_LINK_RE):
        href = a.get("href")
        name = _clean(a.get_text())
        url = urljoin(BASE_URL, href)
        if not name or url in seen:
            continue
        seen.add(url)
        funds.append({"name": name, "url": url})
    return funds


# --------------------------------------------------------------------------- #
# Step 2: NEW buys off each fund page
# --------------------------------------------------------------------------- #
def _header_index(header_cells, pattern):
    """Index of the first header cell whose text matches `pattern`, else None."""
    for i, cell in enumerate(header_cells):
        if pattern.search(_clean(cell.get_text())):
            return i
    return None


def _table_headers(table):
    """Return the list of header cells for `table` (from thead or the first row)."""
    thead = table.find("thead")
    if thead:
        header_row = thead.find("tr")
    else:
        header_row = table.find("tr")
    if header_row is None:
        return []
    return header_row.find_all(["th", "td"])


def _find_holdings_table(soup):
    """Pick the table on a fund page that holds positions + a recent-activity column.

    Returns (table, col_map) where col_map maps {'stock','ticker','activity'} to
    column indices (any of which may be None), or (None, None) if no suitable
    table is found.
    """
    best = None
    for table in soup.find_all("table"):
        headers = _table_headers(table)
        if not headers:
            continue
        activity_idx = _header_index(headers, ACTIVITY_HEADER_RE)
        if activity_idx is None:
            # Without an activity/change column we cannot tell new buys apart.
            continue
        col_map = {
            "stock": _header_index(headers, STOCK_HEADER_RE),
            "ticker": _header_index(headers, TICKER_HEADER_RE),
            "activity": activity_idx,
        }
        # Prefer the table with the most data rows (the main holdings table).
        row_count = len(table.find_all("tr"))
        if best is None or row_count > best[2]:
            best = (table, col_map, row_count)

    if best is None:
        return None, None
    return best[0], best[1]


def _row_cells(table):
    """Yield the data rows (lists of cells) of a table, skipping the header row."""
    body = table.find("tbody") or table
    header_row = None if table.find("tbody") else table.find("tr")
    for tr in body.find_all("tr"):
        if tr is header_row:
            continue
        cells = tr.find_all(["td", "th"])
        if cells:
            yield cells


def _cell_text(cells, idx):
    if idx is None or idx >= len(cells):
        return ""
    return _clean(cells[idx].get_text(" "))


def extract_new_buys(soup, include_all=False):
    """Return [{stock, ticker, activity}, ...] of NEW buys from a fund page.

    With include_all=True, returns every row's recent activity instead of just the
    NEW positions (useful for inspecting/verifying the parse against a fund page).
    """
    table, col_map = _find_holdings_table(soup)
    if table is None:
        return []

    results = []
    for cells in _row_cells(table):
        activity = _cell_text(cells, col_map["activity"])
        if not activity:
            continue
        if not include_all and not NEW_BUY_RE.search(activity):
            continue
        stock = _cell_text(cells, col_map["stock"])
        ticker = _cell_text(cells, col_map["ticker"])
        # If we never resolved a stock-name column, fall back to the first cell,
        # which on these tables is the company/stock.
        if not stock and cells:
            stock = _clean(cells[0].get_text(" "))
        results.append({"stock": stock, "ticker": ticker, "activity": activity})
    return results


# --------------------------------------------------------------------------- #
# Orchestration + output
# --------------------------------------------------------------------------- #
def scrape(session, limit=None, delay=REQUEST_DELAY, include_all=False):
    """Return [{name, url, buys: [...]}, ...] for the top-searched funds."""
    home = fetch_soup(session, BASE_URL)
    if home is None:
        return []

    funds = extract_top_searched_funds(home)
    if limit is not None:
        funds = funds[:limit]

    print(f"Found {len(funds)} top-searched hedge fund(s).\n")

    results = []
    for i, fund in enumerate(funds, 1):
        print(f"[{i}/{len(funds)}] {fund['name']} -> {fund['url']}")
        if delay:
            time.sleep(delay)
        fund_soup = fetch_soup(session, fund["url"])
        buys = extract_new_buys(fund_soup, include_all=include_all) if fund_soup else []
        results.append({**fund, "buys": buys})
    return results


def print_results(results, include_all=False):
    label = "recent trade(s)" if include_all else "NEW buy(s)"
    print()
    for fund in results:
        buys = fund["buys"]
        print(f"=== {fund['name']} ({len(buys)} {label}) ===")
        if not buys:
            print("  (none found)")
        for b in buys:
            ticker = f" [{b['ticker']}]" if b["ticker"] else ""
            print(f"  - {b['stock']}{ticker}  ({b['activity']})")
        print()


def write_csv(results, path):
    scraped_at = datetime.now(timezone.utc).isoformat()
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["scraped_at_utc", "fund", "fund_url", "stock", "ticker", "activity"]
        )
        for fund in results:
            for b in fund["buys"]:
                writer.writerow(
                    [
                        scraped_at,
                        fund["name"],
                        fund["url"],
                        b["stock"],
                        b["ticker"],
                        b["activity"],
                    ]
                )
    print(f"Wrote results to {path}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Scrape hedgefollow.com 'Top Searched Hedge Funds' and the NEW stock "
            "buys (brand-new positions) of each."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N top-searched funds (default: all).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"Seconds to wait between requests (default: {REQUEST_DELAY}).",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="Also write the results to this CSV file.",
    )
    parser.add_argument(
        "--include-all-activity",
        action="store_true",
        help=(
            "Don't filter to NEW positions; report every row's recent activity. "
            "Handy for verifying the parse against a fund page."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    session = make_session()
    results = scrape(
        session,
        limit=args.limit,
        delay=args.delay,
        include_all=args.include_all_activity,
    )
    print_results(results, include_all=args.include_all_activity)
    if args.csv:
        write_csv(results, args.csv)


if __name__ == "__main__":
    main()
