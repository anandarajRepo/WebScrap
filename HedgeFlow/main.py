"""
hedgeflow_top_funds_scraper.py

Scrapes https://hedgefollow.com/ for the hedge funds listed in the home-page
funds table and, for each of those funds, the NEW stock buys (brand-new
positions) they opened in their most recent 13F filing.

Why a real browser is required
------------------------------
hedgefollow.com builds its tables with JavaScript (DataTables). The funds table
exists only in the *rendered* DOM (DevTools -> Elements); it is NOT in the raw
HTML the server sends. So a plain `requests` GET receives an empty shell with
zero rows / zero "/funds/" links -- which is exactly why the previous,
heading-based parser reported:

    ! 'Top Searched Hedge Funds' heading not found; falling back to all fund links.
    Found 0 top-searched hedge fund(s).

To see the table at all we have to load each page in a real browser and let its
JavaScript run. This version uses Playwright (Chromium) to render the page, then
parses the rendered HTML with BeautifulSoup.

How it works
------------
1. Render the home page. Locate the funds table directly -- the table whose rows
   link to individual fund pages at  https://hedgefollow.com/funds/<Name>  -- and
   walk EVERY row to collect the fund name + that link. No heading is required.
   DataTables paginates, so we expand the page-length menu to "All" and, as a
   fallback, click through every "Next" page so no rows are missed.
2. For each fund, render its page and read the holdings / recent-trades table.
   Each row carries a "recent activity" cell describing what the fund did last
   quarter: "Buy", "Add 12%", "Reduce 8%", "Sell", and -- for a position the fund
   did not hold at all the previous quarter -- a NEW marker ("New", "New Buy",
   "Buy New"). We keep only the rows whose activity cell contains "new": those
   are the fund's new stock buys.

The parser stays defensive about markup: it finds the right table by the links /
column headers it contains rather than by brittle fixed ids/classes, so minor
template changes don't break it.

Install deps once:
    pip install playwright beautifulsoup4 --break-system-packages
    playwright install chromium      # downloads the browser binary

Usage:
    python3 main.py                       # every fund in the table + its NEW buys
    python3 main.py --limit 5             # only the first 5 funds (for testing)
    python3 main.py --csv buys.csv        # also write the results to a CSV
    python3 main.py --delay 0.5           # pause between fund pages (default 1.0s)
    python3 main.py --include-all-activity # don't filter -- show every recent trade
    python3 main.py --headful             # show the browser window (debugging)
    python3 main.py --limit 3 --debug     # dump each table's headers + activity cells
"""

import argparse
import csv
import glob
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - dependency hint
    print(
        "This scraper needs Playwright because hedgefollow.com renders its "
        "tables with JavaScript.\n"
        "Install it with:\n"
        "    pip install playwright beautifulsoup4 --break-system-packages\n"
        "    playwright install chromium",
        file=sys.stderr,
    )
    raise

BASE_URL = "https://hedgefollow.com/"

# A normal browser UA avoids basic bot-blocking.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Be polite: small pause between fund pages so we don't hammer the server.
REQUEST_DELAY = 1.0
# Milliseconds Playwright waits for a page / selector to appear.
NAV_TIMEOUT_MS = 30000
SELECTOR_TIMEOUT_MS = 20000
# Safety cap so a misbehaving pager can never loop forever.
MAX_PAGES = 500

# Fund pages live at hedgefollow.com/funds/<Name>; this matches such links.
FUND_LINK_RE = re.compile(r"/funds?/[^/?#]+", re.I)

# A holdings/trades row's "recent activity" cell flags a brand-new position with
# some form of the word "new" (e.g. "New", "New Buy", "Buy New", "Add (New)").
NEW_BUY_RE = re.compile(r"\bnew\b", re.I)

# In practice hedgefollow does NOT print the word "new" in that column. It uses
# 13F shorthand: a brand-new position reads "Buy", while increasing an existing
# one reads "Add 12%", trimming reads "Reduce 8%", and closing out reads "Sell".
# So the distinguishing signal for a new buy is the bare verb "Buy" (with no
# percentage, since there is no prior-quarter size to compare against). This
# matches "Buy", "Buy New", "New Buy" but NOT "Add"/"Reduce"/"Sell"/"Buy/Sell".
BUY_NEW_RE = re.compile(r"^\s*(?:new\s+)?buy(?:\s+new)?\s*$", re.I)

# hedgefollow also flags a brand-new position *structurally*, not just with text:
# the recent-activity cell renders as e.g.
#     <td class="highlighted_bg" data-val="null" ...>
# There is no prior-quarter value to compute a % change from, so DataTables'
# sort value (data-val) is "null" (or "0") and the cell is highlighted. The
# visible "New" marker is supplied by CSS/markup that get_text() doesn't see, so
# a text-only check misses every new buy. We therefore also treat this
# class+data-val combination as a NEW marker.
NEW_BUY_CLASS = "highlighted_bg"
NEW_BUY_DATA_VALS = {"null", "0"}

# Header keywords that identify the "recent activity" column of a holdings
# table, so we know which cell to test for a NEW marker. hedgefollow's holdings
# table has SEVERAL columns that loosely sound like "change/activity", and they
# sit next to each other, e.g.:
#   ... 'Δ % of Portf' ... 'Trade Value' ... 'Latest Activity' ...
#   * 'Latest Activity' is the verb column whose cells read "Buy", "Add 12%",
#     "Reduce 8%", "Sell" -- this is the ONLY one that tells a new buy apart, and
#     the one we want.
#   * 'Trade Value' is a dollar amount (the size of the quarter's trade), not a
#     verb; matching it makes every new-buy test look at a "$1.2M"-style cell.
#   * 'Δ % of Portf' is a numeric change; a brand-new position's cell there is
#     just its current weight, with nothing to distinguish it from a big add.
# Because the headers appear left-to-right as Trade Value (idx 7) *before* Latest
# Activity (idx 8), a single regex that matches "trade" returns the wrong column
# (first match wins). So we rank candidate headers in tiers and take the first
# tier that matches: the verb "activity/action" column first, other verb-ish
# wording next (but never the literal "Trade Value"), and the numeric change
# column only as a last resort.
ACTIVITY_HEADER_TIERS = (
    re.compile(r"\bactivity\b|\baction\b", re.I),                  # "Latest Activity"
    re.compile(r"buy\s*/\s*sell|recent|trade(?!\s*value)", re.I),  # verb-ish, not "Trade Value"
    re.compile(r"change|Δ|delta", re.I),                      # numeric change, last resort
)
# Kept for backwards compatibility / callers that want a single combined test.
ACTIVITY_HEADER_RE = re.compile(
    r"activity|change|action|recent|trade|buy\s*/\s*sell", re.I
)
# Body-content signal: the activity column's *cells* read as 13F verbs
# ("Buy", "Add 12%", "Reduce 8%", "Sell", ...). We use this to find the column
# directly from the data, because the header position and the body position can
# disagree: a leading control/checkbox cell or DataTables' Responsive plugin
# (which collapses columns at narrow viewports) shifts the body <td>s out of
# step with the <thead>, so the cell at "header index 8" is some other column.
# That is exactly what the 0-NEW-buys debug run showed -- header idx 8 said
# 'Latest Activity' but the body cell there was a sparkline (empty text,
# data-val='[21.11,55.33,...]'), so the verb test never matched.
ACTIVITY_VERB_RE = re.compile(
    r"^\s*(?:buy|sell|sold|add|reduce|trim|new|hold|no\s+change)\b", re.I
)
# Header keywords for the stock name / ticker columns, so we can report what was
# bought rather than just "a new position".
STOCK_HEADER_RE = re.compile(r"stock|company|security|holding|name", re.I)
TICKER_HEADER_RE = re.compile(r"ticker|symbol", re.I)


def _clean(text):
    """Collapse whitespace in a cell's text."""
    return re.sub(r"\s+", " ", (text or "")).strip()


# --------------------------------------------------------------------------- #
# Browser / rendering layer
# --------------------------------------------------------------------------- #
def _detect_chromium_path():
    """Return an explicit Chromium executable path, or None to let Playwright pick.

    On a normal machine Playwright manages its own browser (after `playwright
    install chromium`) and None is correct. Some managed/CI environments ship a
    pre-downloaded Chromium under /opt/pw-browsers whose build number doesn't
    match the pip package; there we must point at it explicitly. An env var
    override wins so callers can force a specific binary.
    """
    override = os.environ.get("HEDGEFLOW_CHROMIUM") or os.environ.get(
        "PLAYWRIGHT_CHROMIUM_PATH"
    )
    if override and os.path.exists(override):
        return override
    candidates = sorted(
        glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"), reverse=True
    )
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


class Renderer:
    """Loads pages in a headless browser and returns the rendered HTML.

    Used as a context manager so the browser is always closed:

        with Renderer() as render:
            html_pages = render(BASE_URL, "a[href*='/funds/']")
    """

    def __init__(self, headless=True):
        self.headless = headless
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        launch_kwargs = {"headless": self.headless}
        exe = _detect_chromium_path()
        if exe:
            launch_kwargs["executable_path"] = exe
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        # A wide viewport keeps DataTables' Responsive plugin from collapsing
        # columns into child rows; that collapse drops body <td>s and shifts the
        # remaining cells out of step with the <thead>, which is one way the
        # activity column ends up misread.
        context = self._browser.new_context(
            user_agent=USER_AGENT, viewport={"width": 1920, "height": 1080}
        )
        self._page = context.new_page()
        self._page.set_default_timeout(SELECTOR_TIMEOUT_MS)
        self._page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        return self

    def __exit__(self, *exc):
        for closer in (self._browser, self._pw):
            try:
                if closer is self._pw and closer is not None:
                    closer.stop()
                elif closer is not None:
                    closer.close()
            except Exception:
                pass

    def __call__(self, url, wait_selector=None, wait_for_holdings=False):
        """Render `url` and return a list of rendered-HTML strings (one per page).

        Returns a list because DataTables paginates: after maximising the page
        length we still click through any remaining "Next" pages so every row is
        captured. On any navigation error we return [] and log to stderr.

        When `wait_for_holdings` is set we additionally wait for the holdings /
        recent-trades table to actually have body rows (see _wait_for_holdings):
        on a fund page the static summary table is present immediately while the
        holdings rows arrive later via DataTables AJAX, so a generic table wait
        returns too early and we'd capture an empty holdings table.
        """
        page = self._page
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"  ! could not load {url}: {exc}", file=sys.stderr)
            return []

        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=SELECTOR_TIMEOUT_MS)
            except Exception:
                # Table may still be settling; carry on and parse what we have.
                pass

        if wait_for_holdings:
            self._wait_for_holdings(page)

        self._maximise_page_length(page)

        htmls = [page.content()]
        htmls.extend(self._collect_next_pages(page))
        return htmls

    @staticmethod
    def _wait_for_holdings(page):
        """Wait until a holdings-style table (stock + activity columns) has rows.

        hedgefollow populates the holdings / recent-trades tables with DataTables
        AJAX *after* the page's static tables (the fund-summary table, the
        filing-error notes) are already in the DOM. So "wait for any tbody tr"
        is satisfied immediately by the summary table and we snapshot the page
        while the holdings table is still an empty shell -- which is exactly why
        a run reports `0 data row(s)` for the holdings table and 0 NEW buys.

        We identify the holdings table by its headers (a stock/company column
        plus an activity/change column) and poll until that specific table has at
        least one body row, nudging any lazy rendering by scrolling. Best-effort:
        on timeout we carry on and parse whatever is present.
        """
        # A brand-new position is flagged in the verb column ("Latest Activity"),
        # so wait for the table that has both a stock column and such a column.
        predicate = r"""
          () => {
            const norm = s => (s || '').toLowerCase();
            const STOCK = /stock|company|security|holding/;
            const ACTIVITY = /activity|action|change|trade|recent|buy\s*\/\s*sell|Δ/;
            for (const table of document.querySelectorAll('table')) {
              const heads = [...table.querySelectorAll('thead th, thead td')]
                .map(h => norm(h.textContent));
              const hasStock = heads.some(h => STOCK.test(h));
              const hasActivity = heads.some(h => ACTIVITY.test(h));
              if (!hasStock || !hasActivity) continue;
              const body = table.querySelector('tbody') || table;
              if (body.querySelectorAll('tr').length > 0) return true;
            }
            return false;
          }
        """
        # Scroll through the page first: some DataTables setups defer drawing
        # rows until the table scrolls into view.
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(300)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        try:
            page.wait_for_function(predicate, timeout=SELECTOR_TIMEOUT_MS)
        except Exception:
            # Rows never showed up within the timeout; parse what we have.
            pass

    @staticmethod
    def _maximise_page_length(page):
        """Set any DataTables length menu to its largest option ("All"/-1/100...)."""
        try:
            changed = page.evaluate(
                """
                () => {
                  let touched = false;
                  for (const sel of document.querySelectorAll('select')) {
                    const isLen =
                      (sel.name && sel.name.toLowerCase().endsWith('_length')) ||
                      sel.closest('.dataTables_length, .dt-length');
                    if (!isLen) continue;
                    let best = null, bestScore = -Infinity;
                    for (const opt of sel.options) {
                      const v = parseInt(opt.value, 10);
                      if (Number.isNaN(v)) continue;
                      const score = v < 0 ? Infinity : v;  // -1 == "All"
                      if (score > bestScore) { bestScore = score; best = opt; }
                    }
                    if (best && sel.value !== best.value) {
                      sel.value = best.value;
                      sel.dispatchEvent(new Event('change', { bubbles: true }));
                      touched = true;
                    }
                  }
                  return touched;
                }
                """
            )
            if changed:
                page.wait_for_timeout(700)
        except Exception:
            pass

    @staticmethod
    def _collect_next_pages(page):
        """Click through remaining DataTables "Next" pages, returning their HTML.

        If page-length was successfully set to "All" there is nothing left to
        page through and this returns []. The selector covers the common
        DataTables / Bootstrap pager markups.
        """
        next_selector = (
            "a.paginate_button.next:not(.disabled), "
            "li.paginate_button.next:not(.disabled) a, "
            "li.next:not(.disabled) a, a.next:not(.disabled)"
        )
        extra = []
        for _ in range(MAX_PAGES):
            nxt = page.query_selector(next_selector)
            if not nxt:
                break
            try:
                if not nxt.is_enabled() or not nxt.is_visible():
                    break
                nxt.click()
                page.wait_for_timeout(450)
            except Exception:
                break
            extra.append(page.content())
        return extra


# --------------------------------------------------------------------------- #
# Step 1: every fund in the home-page funds table
# --------------------------------------------------------------------------- #
def _fund_link_in_row(row):
    """Return the first <a> in a table row that points at a fund page, or None."""
    return row.find("a", href=FUND_LINK_RE)


def _funds_table(soup):
    """Pick the table that lists funds: the one with the most fund-link rows."""
    best, best_count = None, 0
    for table in soup.find_all("table"):
        count = sum(1 for tr in table.find_all("tr") if _fund_link_in_row(tr))
        if count > best_count:
            best, best_count = table, count
    return best


def extract_funds(soup):
    """Return [{name, url}, ...] for every row of the home-page funds table.

    Walks every row of the funds table (no heading needed). Falls back to every
    fund link on the page if no obvious table is found. De-duplicated by URL,
    discovery order preserved.
    """
    table = _funds_table(soup)
    if table is not None:
        rows = [tr for tr in table.find_all("tr") if _fund_link_in_row(tr)]
    else:
        print(
            "  ! no funds table found in the rendered page; "
            "falling back to all fund links.",
            file=sys.stderr,
        )
        rows = soup.find_all("tr")

    funds = []
    seen = set()
    for row in rows:
        link = _fund_link_in_row(row)
        if link is None:
            continue
        url = urljoin(BASE_URL, link.get("href"))
        # Prefer the link's own text; fall back to the row's first cell.
        name = _clean(link.get_text())
        if not name:
            first_cell = row.find(["td", "th"])
            name = _clean(first_cell.get_text(" ")) if first_cell else ""
        if not name or url in seen:
            continue
        seen.add(url)
        funds.append({"name": name, "url": url})

    # Last-ditch fallback: any fund link anywhere on the page.
    if not funds:
        for a in soup.find_all("a", href=FUND_LINK_RE):
            url = urljoin(BASE_URL, a.get("href"))
            name = _clean(a.get_text())
            if not name or url in seen:
                continue
            seen.add(url)
            funds.append({"name": name, "url": url})
    return funds


def extract_funds_multi(html_pages):
    """Merge fund rows across all rendered pages, de-duplicated by URL."""
    funds, seen = [], set()
    for html in html_pages:
        soup = BeautifulSoup(html, "html.parser")
        for fund in extract_funds(soup):
            if fund["url"] in seen:
                continue
            seen.add(fund["url"])
            funds.append(fund)
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
    header_row = thead.find("tr") if thead else table.find("tr")
    if header_row is None:
        return []
    return header_row.find_all(["th", "td"])


def _activity_header_index(headers):
    """Index of the column to test for a NEW marker on a holdings table.

    Prefers the verb-style "Latest Activity" column ("Buy"/"Add"/"Reduce"/"Sell")
    over the dollar "Trade Value" column and the numeric "Δ % of Portf" column,
    since only the verb column distinguishes a brand-new position. Walks the
    header tiers in priority order and returns the first column any tier matches;
    a later tier is consulted only when no earlier tier matched any header.
    Returns None when no tier matches.
    """
    for pattern in ACTIVITY_HEADER_TIERS:
        idx = _header_index(headers, pattern)
        if idx is not None:
            return idx
    return None


def _activity_index_by_content(rows):
    """Return the body column index whose cells read like 13F activity verbs.

    `rows` is a list of cell-lists (the table's data rows). We score each column
    by how many of its cells *start* with an activity verb -- "Buy", "Add 12%",
    "Reduce 8%", "Sell", "New" -- and return the best-scoring column. This finds
    the activity column straight from the data, so it is immune to the
    header/body misalignment that makes a positional ("header index N == body
    cell N") lookup read the wrong column. Returns None when no column looks like
    activity (e.g. the verbs are not rendered as text at all), so the caller can
    fall back to the header-based index.
    """
    if not rows:
        return None
    width = max(len(r) for r in rows)
    best_idx, best_score = None, 0
    for idx in range(width):
        score = sum(
            1
            for cells in rows
            if idx < len(cells)
            and ACTIVITY_VERB_RE.match(_clean(cells[idx].get_text(" ")))
        )
        if score > best_score:
            best_idx, best_score = idx, score
    # Require the column to read as verbs in a meaningful share of rows so a
    # stray "Add"/"New" inside a company name can't win a 1-vote contest.
    if best_idx is not None and best_score >= max(1, len(rows) // 5):
        return best_idx
    return None


def _history_array(data_val):
    """Parse a sparkline `data-val` like '[0,0,0,0,12.3]' into a list of floats.

    hedgefollow renders several history columns (ownership / activity) as
    DataTables sparklines whose sort value is a JSON-ish numeric array. Returns
    [] when the value isn't such an array.
    """
    if not data_val:
        return []
    s = data_val.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return []
    nums = []
    for part in s[1:-1].split(","):
        part = part.strip()
        if part in ("", "null", "None"):
            nums.append(0.0)
            continue
        try:
            nums.append(float(part))
        except ValueError:
            return []
    return nums


def _history_is_new(data_val):
    """True if a sparkline array shows a position that appeared only this period.

    A brand-new buy held nothing in every prior quarter and a non-zero amount in
    the latest one -- i.e. leading zeros followed by a final non-zero value. This
    catches new positions even when the activity column is a sparkline rather
    than a "Buy" verb. Price-history arrays never have leading zeros, so this
    can't misfire on them.
    """
    nums = _history_array(data_val)
    if len(nums) < 2:
        return False
    return nums[-1] != 0 and all(n == 0 for n in nums[:-1])


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
        header_idx = _activity_header_index(headers)
        if header_idx is None:
            # Without an activity/change column we cannot tell new buys apart.
            continue
        # Locate the activity column from the body content first (robust to a
        # header/body column shift), falling back to the header position.
        rows = list(_row_cells(table))
        content_idx = _activity_index_by_content(rows)
        activity_idx = content_idx if content_idx is not None else header_idx
        # When the body is uniformly shifted from the <thead> (a leading control
        # cell, responsive collapse), the verb column moves by `delta`; the stock
        # and ticker columns move by the same amount, so shift their header-based
        # indices too instead of reading the wrong (shifted) cell.
        delta = activity_idx - header_idx if content_idx is not None else 0
        stock_idx = _header_index(headers, STOCK_HEADER_RE)
        ticker_idx = _header_index(headers, TICKER_HEADER_RE)
        col_map = {
            "stock": stock_idx + delta if stock_idx is not None else None,
            "ticker": ticker_idx + delta if ticker_idx is not None else None,
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


def _cell_at(cells, idx):
    """Return the cell element at `idx`, or None if the index is missing/out of range."""
    if idx is None or idx >= len(cells):
        return None
    return cells[idx]


def _cell_text(cells, idx):
    cell = _cell_at(cells, idx)
    return _clean(cell.get_text(" ")) if cell is not None else ""


def _is_new_buy(activity_cell, activity_text):
    """True if the recent-activity cell marks a brand-new position.

    Detects any of the signals hedgefollow uses:
      * the bare verb "Buy" (the column's marker for opening a new position --
        "Add"/"Reduce"/"Sell" are increases/trims/exits and never qualify);
      * the literal word "new" in the cell's text;
      * the structural marker (class="highlighted_bg" with data-val "null"/"0")
        that flags a position with no prior-quarter value; or
      * a sparkline history (data-val='[0,0,0,0,N]') whose value is non-zero only
        in the latest period -- a position the fund did not hold before now.
    """
    if activity_text and (
        BUY_NEW_RE.match(activity_text) or NEW_BUY_RE.search(activity_text)
    ):
        return True
    if activity_cell is None:
        return False
    classes = activity_cell.get("class") or []
    raw_data_val = activity_cell.get("data-val") or ""
    data_val = raw_data_val.strip().lower()
    if NEW_BUY_CLASS in classes and data_val in NEW_BUY_DATA_VALS:
        return True
    return _history_is_new(raw_data_val)


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
        activity_cell = _cell_at(cells, col_map["activity"])
        activity = _clean(activity_cell.get_text(" ")) if activity_cell is not None else ""
        is_new = _is_new_buy(activity_cell, activity)
        # A cell can be flagged NEW structurally (highlighted_bg / data-val
        # null|0) while rendering no text get_text() can see; surface it as
        # "New" so the row isn't dropped by the empty-activity guard below.
        if not activity and is_new:
            activity = "New"
        if not activity:
            continue
        if not include_all and not is_new:
            continue
        stock = _cell_text(cells, col_map["stock"])
        ticker = _cell_text(cells, col_map["ticker"])
        # If we never resolved a stock-name column, fall back to the first cell
        # that carries real text -- skipping empty/control cells (a leading "+"
        # toggle from a shifted body) rather than blindly taking cells[0].
        if not stock:
            for cell in cells:
                text = _clean(cell.get_text(" "))
                if len(text) > 1 and re.search(r"[A-Za-z]", text):
                    stock = text
                    break
        results.append({"stock": stock, "ticker": ticker, "activity": activity})
    return results


def debug_dump_fund_page(html_pages, fund_name):
    """Print what the parser actually sees on a fund page, for diagnosing 0-result runs.

    For each rendered table it lists the header cells, which column was chosen as
    the activity column (header-based vs the body-content detector, so a
    header/body misalignment is obvious), and the first few data rows' chosen
    activity cell plus a per-column text/data-val breakdown of the first row.
    This reveals the real markup (which differs between environments) so the
    NEW-buy matcher can be aimed correctly.
    """
    print(f"\n----- DEBUG: {fund_name} -----", file=sys.stderr)
    if not html_pages:
        print("  (no HTML rendered for this fund)", file=sys.stderr)
        return
    soup = BeautifulSoup(html_pages[0], "html.parser")
    tables = soup.find_all("table")
    print(f"  rendered tables on page: {len(tables)}", file=sys.stderr)
    for t_i, table in enumerate(tables):
        headers = _table_headers(table)
        header_text = [_clean(h.get_text()) for h in headers]
        header_idx = _activity_header_index(headers)
        rows = list(_row_cells(table))
        print(
            f"  [table {t_i}] {len(rows)} data row(s); headers={header_text}",
            file=sys.stderr,
        )
        if header_idx is None:
            print("    no activity/change column detected", file=sys.stderr)
            continue
        content_idx = _activity_index_by_content(rows)
        activity_idx = content_idx if content_idx is not None else header_idx
        chosen = header_text[activity_idx] if activity_idx < len(header_text) else "?"
        print(
            f"    activity column -> idx {activity_idx} ({chosen!r}); "
            f"header-based idx={header_idx}, content-based idx={content_idx}",
            file=sys.stderr,
        )
        # Per-column breakdown of the first data row: the surest way to see where
        # the verb column actually lives when header and body disagree.
        if rows:
            print("    first row, cell-by-cell:", file=sys.stderr)
            for c_i, cell in enumerate(rows[0]):
                txt = _clean(cell.get_text(" "))
                print(
                    f"      [{c_i}] text={txt!r} data-val={cell.get('data-val')!r}",
                    file=sys.stderr,
                )
        for cells in rows[:8]:
            cell = _cell_at(cells, activity_idx)
            if cell is None:
                continue
            print(
                f"      activity={_clean(cell.get_text(' '))!r} "
                f"class={cell.get('class')} data-val={cell.get('data-val')!r} "
                f"-> new={_is_new_buy(cell, _clean(cell.get_text(' ')))}",
                file=sys.stderr,
            )
    print("----- END DEBUG -----\n", file=sys.stderr)


def extract_new_buys_multi(html_pages, include_all=False):
    """Merge NEW buys across all rendered pages of a fund, de-duplicated."""
    results, seen = [], set()
    for html in html_pages:
        soup = BeautifulSoup(html, "html.parser")
        for buy in extract_new_buys(soup, include_all=include_all):
            key = (buy["stock"], buy["ticker"], buy["activity"])
            if key in seen:
                continue
            seen.add(key)
            results.append(buy)
    return results


# --------------------------------------------------------------------------- #
# Orchestration + output
# --------------------------------------------------------------------------- #
def scrape(render, limit=None, delay=REQUEST_DELAY, include_all=False, debug=False):
    """Return [{name, url, buys: [...]}, ...] for every fund in the table."""
    home_pages = render(BASE_URL, "a[href*='/funds/']")
    if not home_pages:
        return []

    funds = extract_funds_multi(home_pages)
    if limit is not None:
        funds = funds[:limit]

    print(f"Found {len(funds)} hedge fund(s) in the funds table.\n")

    results = []
    for i, fund in enumerate(funds, 1):
        print(f"[{i}/{len(funds)}] {fund['name']} -> {fund['url']}")
        if delay:
            time.sleep(delay)
        # Wait for the holdings table's own rows, not just any tbody row -- the
        # static summary table is present immediately while the holdings rows
        # arrive later via DataTables AJAX (see Renderer._wait_for_holdings).
        fund_pages = render(fund["url"], "table", wait_for_holdings=True)
        if debug:
            debug_dump_fund_page(fund_pages, fund["name"])
        buys = (
            extract_new_buys_multi(fund_pages, include_all=include_all)
            if fund_pages
            else []
        )
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


def aggregate_new_buys(results):
    """Collapse every fund's NEW buys into one consensus-ranked list.

    Returns [{stock, ticker, funds: [name, ...], count}, ...] where each entry is
    a distinct stock that at least one fund opened as a brand-new position, with
    the funds that bought it. Sorted by number of funds (the "situational
    awareness" signal -- the more funds opening the same name, the stronger the
    crowd conviction), then alphabetically by stock.
    """
    by_stock = {}
    for fund in results:
        for b in fund["buys"]:
            ticker = (b.get("ticker") or "").strip()
            stock = (b.get("stock") or "").strip()
            # Group by ticker when we have one (most reliable), else by name.
            key = ticker.upper() if ticker else stock.lower()
            if not key:
                continue
            entry = by_stock.setdefault(
                key, {"stock": stock, "ticker": ticker, "funds": []}
            )
            if not entry["stock"]:
                entry["stock"] = stock
            if not entry["ticker"]:
                entry["ticker"] = ticker
            if fund["name"] not in entry["funds"]:
                entry["funds"].append(fund["name"])

    aggregated = [
        {**entry, "count": len(entry["funds"])} for entry in by_stock.values()
    ]
    aggregated.sort(key=lambda e: (-e["count"], (e["stock"] or e["ticker"]).lower()))
    return aggregated


def print_situational_awareness(results):
    """Print the cross-fund summary of brand-new buys (consensus first)."""
    aggregated = aggregate_new_buys(results)
    total = sum(e["count"] for e in aggregated)
    print(f"=== Situational Awareness ({total} NEW buy(s)) ===")
    if not aggregated:
        print("  (none found)")
        print()
        return
    for e in aggregated:
        ticker = f" [{e['ticker']}]" if e["ticker"] else ""
        funds = ", ".join(e["funds"])
        plural = "fund" if e["count"] == 1 else "funds"
        print(f"  - {e['stock']}{ticker}  ({e['count']} {plural}: {funds})")
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
            "Scrape hedgefollow.com's funds table and the NEW stock buys "
            "(brand-new positions) of each fund. Renders the JS tables with a "
            "headless browser."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N funds (default: all rows in the table).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"Seconds to wait between fund pages (default: {REQUEST_DELAY}).",
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
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show the browser window instead of running headless (debugging).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Print, to stderr, every table's headers and a sample of the chosen "
            "activity column's cells for each fund. Use this to see the real "
            "markup when a run reports 0 NEW buys."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with Renderer(headless=not args.headful) as render:
        results = scrape(
            render,
            limit=args.limit,
            delay=args.delay,
            include_all=args.include_all_activity,
            debug=args.debug,
        )
    print_results(results, include_all=args.include_all_activity)
    if not args.include_all_activity:
        print_situational_awareness(results)
    if args.csv:
        write_csv(results, args.csv)


if __name__ == "__main__":
    main()
