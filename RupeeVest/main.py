"""
RupeeVest "Stocks attracting mutual funds" scraper
--------------------------------------------------
Scrapes the "Stocks attracting mutual funds" table from
https://www.rupeevest.com/Mutual-Fund-Holdings/ and saves every column of
every row to a CSV, one file per month.

RupeeVest publishes, for each month, the list of stocks that mutual funds
*bought into* on a net basis ("attracting") alongside the ones they sold
("distressing"). The data for a month is usually finalised between the 13th
and 15th of the following month, once all the AMCs disclose their portfolios.

The page is JavaScript-driven and lets you pick a month from a dropdown, so we
drive a real (headless) browser with Playwright rather than parsing static HTML.
The "attracting" table is located by its heading text and read generically --
whatever columns the site shows are what we write -- so a column rename on their
side won't silently drop data.

Install once:
    pip install playwright --break-system-packages
    playwright install chromium

Usage:
    python3 main.py                      # scrape the latest available month
    python3 main.py --month "May 2026"   # scrape a specific month (as shown in the dropdown)
    python3 main.py --all-months         # scrape every month the dropdown offers
    python3 main.py --list-months        # just print the months available, don't scrape
    python3 main.py --inspect            # dump page structure (headings/tables/months)
                                         #   -- use this first if selectors look off
    python3 main.py --headed             # show the browser window (debugging)

Output:
    CSV files in this script's folder, named like
        rupeevest_stocks_attracting_mf_2026-05.csv
    with the site's own columns plus a leading `month` and `scraped_at_utc`.

NOTE: This page is rendered entirely client-side and its markup can change
without notice. If a run reports "could not find the 'attracting' table",
re-run with --inspect: it prints the headings, every table's headers, and the
month options it can see, so the locator constants below can be adjusted.
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime, timezone

try:
    from playwright.sync_api import TimeoutError as PWTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - guidance for a missing dependency
    sys.exit(
        "Playwright is not installed. Run:\n"
        "    pip install playwright --break-system-packages\n"
        "    playwright install chromium"
    )

URL = "https://www.rupeevest.com/Mutual-Fund-Holdings/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The heading that sits above the table we want. Matched case-insensitively and
# loosely ("attracting" + "mutual fund") so minor wording changes still hit.
ATTRACTING_HEADING_RE = re.compile(r"attract.*mutual\s*fund", re.I)

# Month labels in the dropdown look like "May 2026" / "May-2026" / "Jun 2026".
MONTH_LABEL_RE = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-/]*'?\s*(\d{4})",
    re.I,
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Generous default; the page can be slow to populate its tables.
DEFAULT_TIMEOUT_MS = 45_000


# --------------------------------------------------------------------------- #
# In-page extraction helpers (run inside the browser via page.evaluate)
# --------------------------------------------------------------------------- #

# Read every <table> on the page as {heading, headers, rows}. `heading` is the
# nearest preceding text node/heading-ish element, used to tell tables apart.
_JS_READ_TABLES = r"""
() => {
  const cellText = (el) => (el.innerText || el.textContent || '')
      .replace(/\s+/g, ' ').trim();

  // Walk backwards in document order to find a short, label-like bit of text
  // that introduces this table (a heading, a styled div, a tab caption, ...).
  const headingFor = (table) => {
    let node = table;
    for (let hops = 0; hops < 6 && node; hops++) {
      let sib = node.previousElementSibling;
      while (sib) {
        const t = cellText(sib);
        if (t && t.length <= 120 && !sib.querySelector('table')) return t;
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    return '';
  };

  const tables = [];
  for (const table of document.querySelectorAll('table')) {
    const rows = [...table.querySelectorAll('tr')];
    if (!rows.length) continue;

    // Header = the first row that uses <th>, else the first row's cells.
    let headerRow = rows.find(r => r.querySelector('th')) || rows[0];
    const headers = [...headerRow.querySelectorAll('th,td')].map(cellText);

    const bodyRows = [];
    for (const r of rows) {
      if (r === headerRow) continue;
      const cells = [...r.querySelectorAll('th,td')].map(cellText);
      if (cells.some(c => c !== '')) bodyRows.push(cells);
    }
    tables.push({ heading: headingFor(table), headers, rows: bodyRows });
  }
  return tables;
}
"""

# Every <select>'s options, so we can find and drive the month picker.
_JS_READ_SELECTS = r"""
() => [...document.querySelectorAll('select')].map((sel, idx) => ({
  index: idx,
  id: sel.id || '',
  name: sel.name || '',
  options: [...sel.options].map(o => ({ value: o.value, label: (o.textContent || '').trim() })),
}))
"""


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def month_sort_key(label: str):
    """Sort key (year, month) for a 'May 2026'-style label; (0,0) if unparseable."""
    m = MONTH_LABEL_RE.search(label or "")
    if not m:
        return (0, 0)
    return (int(m.group(2)), _MONTHS[m.group(1)[:3].lower()])


def month_file_tag(label: str) -> str:
    """Turn 'May 2026' into '2026-05' for the filename; fall back to a slug."""
    m = MONTH_LABEL_RE.search(label or "")
    if m:
        return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1)[:3].lower()]:02d}"
    return re.sub(r"[^A-Za-z0-9]+", "-", (label or "unknown").strip()).strip("-").lower()


def pick_attracting_table(tables: list) -> dict | None:
    """Return the table whose heading names the 'attracting' list, or None."""
    for t in tables:
        if ATTRACTING_HEADING_RE.search(t.get("heading", "")):
            return t
    return None


def find_month_select(selects: list) -> dict | None:
    """Return the <select> that looks like the month picker (most month-like options)."""
    best, best_hits = None, 0
    for sel in selects:
        hits = sum(1 for o in sel["options"] if MONTH_LABEL_RE.search(o["label"]))
        if hits > best_hits:
            best, best_hits = sel, hits
    return best if best_hits >= 1 else None


# --------------------------------------------------------------------------- #
# Browser driving
# --------------------------------------------------------------------------- #

def _new_page(p, headed: bool, timeout_ms: int):
    browser = p.chromium.launch(headless=not headed)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 900},
    )
    page = context.new_page()
    page.set_default_timeout(timeout_ms)
    return browser, page


def _load(page, timeout_ms: int):
    """Navigate to the holdings page and wait for its tables to populate."""
    page.goto(URL, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_selector("table tr", timeout=timeout_ms)
    except PWTimeoutError:
        pass  # caller will report if nothing usable showed up
    # Give client-side rendering a beat to settle after the first rows appear.
    page.wait_for_timeout(1500)


def _select_month(page, month_label: str, timeout_ms: int) -> bool:
    """Select `month_label` in the month dropdown and wait for a refresh. True on success."""
    selects = page.evaluate(_JS_READ_SELECTS)
    sel = find_month_select(selects)
    if not sel:
        return False
    # Match the option whose (year, month) equals the requested label's.
    want = month_sort_key(month_label)
    option = next(
        (o for o in sel["options"] if month_sort_key(o["label"]) == want and want != (0, 0)),
        None,
    )
    if option is None:
        return False

    locator = page.locator("select").nth(sel["index"])
    try:
        locator.select_option(value=option["value"])
    except Exception:
        locator.select_option(label=option["label"])
    # The table re-renders; wait briefly for it to swap in.
    page.wait_for_timeout(2000)
    return True


def available_months(page) -> list:
    """Month labels from the picker, newest first."""
    sel = find_month_select(page.evaluate(_JS_READ_SELECTS))
    if not sel:
        return []
    labels = [o["label"] for o in sel["options"] if MONTH_LABEL_RE.search(o["label"])]
    return sorted(set(labels), key=month_sort_key, reverse=True)


# --------------------------------------------------------------------------- #
# CSV output
# --------------------------------------------------------------------------- #

def write_csv(month_label: str, table: dict, out_dir: str) -> str:
    """Write one month's 'attracting' table to CSV and return the file path."""
    tag = month_file_tag(month_label)
    path = os.path.join(out_dir, f"rupeevest_stocks_attracting_mf_{tag}.csv")
    scraped_at = datetime.now(timezone.utc).isoformat()

    headers = table["headers"] or [f"col_{i+1}" for i in range(
        max((len(r) for r in table["rows"]), default=0))]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["month", "scraped_at_utc", *headers])
        for row in table["rows"]:
            # Pad/truncate so ragged rows still line up under the headers.
            row = (row + [""] * len(headers))[: len(headers)]
            writer.writerow([month_label, scraped_at, *row])
    return path


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def scrape_month(page, month_label, out_dir: str, timeout_ms: int):
    """Scrape one month (None = whatever's currently shown) and write its CSV."""
    if month_label and not _select_month(page, month_label, timeout_ms):
        print(f"  ! could not select month '{month_label}' in the dropdown; "
              f"scraping the currently displayed month instead.")

    tables = page.evaluate(_JS_READ_TABLES)
    table = pick_attracting_table(tables)
    if table is None:
        print("  ! could not find the 'Stocks attracting mutual funds' table.")
        print("    Re-run with --inspect to see the page's headings/tables.")
        return None
    if not table["rows"]:
        print("  ! found the 'attracting' table but it had no data rows.")
        return None

    label = month_label or "current"
    path = write_csv(label, table, out_dir)
    print(f"  -> {len(table['rows'])} stocks written to {os.path.relpath(path, BASE_DIR)}")
    return path


def run_inspect(page):
    """Print what the scraper can see, to help adjust selectors when markup changes."""
    print("\n=== <select> dropdowns ===")
    for sel in page.evaluate(_JS_READ_SELECTS):
        opts = ", ".join(o["label"] for o in sel["options"][:12])
        more = " ..." if len(sel["options"]) > 12 else ""
        print(f"  [{sel['index']}] id={sel['id']!r} name={sel['name']!r}: {opts}{more}")

    print("\n=== tables ===")
    for i, t in enumerate(page.evaluate(_JS_READ_TABLES)):
        marker = "  <-- matches 'attracting'" if ATTRACTING_HEADING_RE.search(
            t.get("heading", "")) else ""
        print(f"  [{i}] heading={t['heading']!r}{marker}")
        print(f"       headers={t['headers']}")
        print(f"       rows={len(t['rows'])}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape RupeeVest 'Stocks attracting mutual funds' into per-month CSVs."
    )
    parser.add_argument("--month", help="Month to scrape, as shown in the dropdown (e.g. 'May 2026').")
    parser.add_argument("--all-months", action="store_true",
                        help="Scrape every month the dropdown offers.")
    parser.add_argument("--list-months", action="store_true",
                        help="Print the available months and exit (no scraping).")
    parser.add_argument("--inspect", action="store_true",
                        help="Print the page's dropdowns/tables and exit (for fixing selectors).")
    parser.add_argument("--headed", action="store_true",
                        help="Run with a visible browser window (debugging).")
    parser.add_argument("--out-dir", default=BASE_DIR,
                        help="Where to write the CSV files (default: this script's folder).")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS,
                        help=f"Per-step timeout in ms (default: {DEFAULT_TIMEOUT_MS}).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    os.makedirs(args.out_dir, exist_ok=True)

    with sync_playwright() as p:
        browser, page = _new_page(p, args.headed, args.timeout)
        try:
            print(f"Loading {URL} ...")
            _load(page, args.timeout)

            if args.inspect:
                run_inspect(page)
                return

            months = available_months(page)
            if args.list_months:
                if months:
                    print("Available months (newest first):")
                    for m in months:
                        print(f"  {m}")
                else:
                    print("No month dropdown found. Re-run with --inspect.")
                return

            if args.all_months:
                if not months:
                    print("No months found to iterate; falling back to the current view.")
                    scrape_month(page, None, args.out_dir, args.timeout)
                    return
                print(f"Scraping {len(months)} month(s)...")
                for m in months:
                    print(f"[{m}]")
                    scrape_month(page, m, args.out_dir, args.timeout)
                return

            # Single month: the one requested, or the latest the dropdown shows.
            target = args.month or (months[0] if months else None)
            print(f"[{target or 'current view'}]")
            scrape_month(page, target, args.out_dir, args.timeout)

        finally:
            browser.close()


if __name__ == "__main__":
    main()
