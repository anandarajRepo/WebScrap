"""
WeeklyTrendingPennyStocks.py

Fetches all penny stocks (top tickers) from the SwaggyStocks API:
https://api.swaggystocks.com/v1/pennystocks/top-tickers?from-datetime=<ISO8601>

By default the "from-datetime" is the last 7 days (matching the API's own
week-lookback style), but a specific timestamp can be passed on the command
line. Results are printed as a table and optionally saved to CSV/JSON.

Install deps once:
    pip install requests --break-system-packages

Usage:
    python3 WeeklyTrendingPennyStocks.py                                   # last 7 days
    python3 WeeklyTrendingPennyStocks.py --from "2026-06-28T04:29:58-04:00"
    python3 WeeklyTrendingPennyStocks.py --csv pennystocks.csv             # also write CSV
    python3 WeeklyTrendingPennyStocks.py --json pennystocks.json           # also write JSON
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone

import requests

API_URL = "https://api.swaggystocks.com/v1/pennystocks/top-tickers"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://swaggystocks.com",
    "Referer": "https://swaggystocks.com/",
}
DEFAULT_LOOKBACK_DAYS = 7
REQUEST_TIMEOUT = 30
MAX_STOCKS = 300  # total number of stocks to fetch
PAGE_SIZE = 100   # per-request page size when paginating


def default_from_datetime():
    """ISO-8601 timestamp for the start of the default lookback window."""
    dt = datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    return dt.replace(microsecond=0).isoformat()


def _parse_iso(value):
    """Parse an ISO-8601 string into an aware datetime (UTC if naive)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _request_page(from_datetime, extra_params):
    """Fetch one page of ticker records from the API."""
    params = {"from-datetime": from_datetime}
    params.update(extra_params)
    resp = requests.get(
        API_URL,
        params=params,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    # The endpoint returns either a bare list of ticker objects or a wrapper
    # object with the list under a key such as "data"/"tickers"/"results".
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "tickers", "results", "top_tickers"):
            if isinstance(data.get(key), list):
                return data[key]
    raise ValueError(f"Unexpected API response shape: {type(data).__name__}")


def _record_id(rec):
    """Stable identity for a record, used to de-duplicate across pages."""
    for key in ("ticker", "symbol", "stock", "name"):
        if key in rec:
            return rec[key]
    return json.dumps(rec, sort_keys=True)


def _paginate(from_datetime, max_stocks, records, seen, make_params):
    """Walk pages built by ``make_params(page_index, offset)`` until done.

    Returns True if this strategy made progress (added at least one record
    beyond what was already collected). The API caps every response at ~15
    records no matter what ``limit`` is asked for, so a short page does NOT
    mean the data ran out — we only stop on an empty page, a page with no
    new records (pagination params ignored / data exhausted), or reaching
    ``max_stocks``.
    """
    progressed = False
    page_index = 0
    while len(records) < max_stocks:
        page = _request_page(from_datetime, make_params(page_index, len(records)))
        if not page:
            break
        new = [r for r in page if _record_id(r) not in seen]
        if not new:
            break
        progressed = True
        for rec in new:
            seen.add(_record_id(rec))
            records.append(rec)
            if len(records) >= max_stocks:
                break
        page_index += 1
    return progressed


def fetch_penny_stocks(from_datetime, max_stocks=MAX_STOCKS):
    """Return up to ``max_stocks`` ticker records, paginating as needed.

    Tries offset-based pagination first; if the API ignores ``offset``
    (second page repeats the first), falls back to page-number pagination,
    then to sweeping day-sized ``from-datetime`` windows across the lookback
    range (the API caps every response at ~15 records regardless of
    pagination params, but different windows surface different tickers).
    """
    records = []
    seen = set()

    _paginate(
        from_datetime, max_stocks, records, seen,
        lambda page_index, offset: {"limit": PAGE_SIZE, "offset": offset},
    )
    if 0 < len(records) < max_stocks:
        # Offset may have been ignored; try page-number pagination, starting
        # from page 2 (page 1 is what we already have).
        _paginate(
            from_datetime, max_stocks, records, seen,
            lambda page_index, offset: {"limit": PAGE_SIZE, "page": page_index + 2},
        )
    if 0 < len(records) < max_stocks:
        # The API ignores every pagination parameter and hard-caps each
        # response at ~15 records for a given window. The only lever left is
        # the window itself: slice the lookback range into per-day windows,
        # query each one, and merge the (deduplicated) results.
        _sweep_windows(from_datetime, max_stocks, records, seen)
    return records


def _sweep_windows(from_datetime, max_stocks, records, seen):
    """Fetch each day-sized slice of the lookback window and merge results.

    Each request still returns at most ~15 records, but different windows
    surface different tickers, so sweeping day by day accumulates far more
    than a single call can. Duplicates across windows are dropped via
    ``seen``. Per-window request failures are skipped so one bad slice
    doesn't lose the rest.
    """
    try:
        start = _parse_iso(from_datetime)
    except ValueError:
        return
    now = datetime.now(timezone.utc)
    window_start = start
    while window_start < now and len(records) < max_stocks:
        try:
            page = _request_page(
                window_start.replace(microsecond=0).isoformat(), {}
            )
        except (requests.RequestException, ValueError):
            page = []
        for rec in page:
            rid = _record_id(rec)
            if rid in seen:
                continue
            seen.add(rid)
            records.append(rec)
            if len(records) >= max_stocks:
                return
        window_start += timedelta(days=1)


EXCLUDED_COLUMNS = {"date", "timestamp", "starting_date", "ending_date"}


def normalize(records):
    """Flatten records into rows of plain scalar values, union of all keys."""
    keys = []
    for rec in records:
        for k in rec:
            if k not in keys and k not in EXCLUDED_COLUMNS:
                keys.append(k)
    rows = []
    for rec in records:
        rows.append({k: rec.get(k, "") for k in keys})
    return keys, rows


def print_table(keys, rows):
    if not rows:
        print("No penny stocks returned for the given window.")
        return
    widths = {k: max(len(k), *(len(str(r[k])) for r in rows)) for k in keys}
    header = "  ".join(k.ljust(widths[k]) for k in keys)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r[k]).ljust(widths[k]) for k in keys))
    print(f"\n{len(rows)} stock(s) fetched.")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch penny stocks from the SwaggyStocks top-tickers API."
    )
    parser.add_argument(
        "--from",
        dest="from_datetime",
        default=default_from_datetime(),
        help="ISO-8601 from-datetime (default: %(default)s)",
    )
    parser.add_argument(
        "--max",
        dest="max_stocks",
        type=int,
        default=MAX_STOCKS,
        help="maximum number of stocks to fetch (default: %(default)s)",
    )
    parser.add_argument("--csv", help="also write results to this CSV file")
    parser.add_argument("--json", help="also write raw results to this JSON file")
    args = parser.parse_args()

    try:
        records = fetch_penny_stocks(args.from_datetime, args.max_stocks)
    except (requests.RequestException, ValueError) as exc:
        print(f"Failed to fetch penny stocks: {exc}", file=sys.stderr)
        sys.exit(1)

    keys, rows = normalize(records)
    print_table(keys, rows)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
        print(f"Wrote {args.json}")

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {args.csv}")


if __name__ == "__main__":
    main()
