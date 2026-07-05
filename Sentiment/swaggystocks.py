"""
swaggystocks.py

Fetches all penny stocks (top tickers) from the SwaggyStocks API:
https://api.swaggystocks.com/v1/pennystocks/top-tickers?from-datetime=<ISO8601>

By default the "from-datetime" is the last 7 days (matching the API's own
week-lookback style), but a specific timestamp can be passed on the command
line. Results are printed as a table and optionally saved to CSV/JSON.

Install deps once:
    pip install requests --break-system-packages

Usage:
    python3 swaggystocks.py                                   # last 7 days
    python3 swaggystocks.py --from "2026-06-28T04:29:58-04:00"
    python3 swaggystocks.py --csv pennystocks.csv             # also write CSV
    python3 swaggystocks.py --json pennystocks.json           # also write JSON
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


def default_from_datetime():
    """ISO-8601 timestamp for the start of the default lookback window."""
    dt = datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    return dt.replace(microsecond=0).isoformat()


def fetch_penny_stocks(from_datetime):
    """Return the full list of penny-stock ticker records from the API."""
    resp = requests.get(
        API_URL,
        params={"from-datetime": from_datetime},
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
    print(f"\n{len(rows)} penny stock(s) fetched.")


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
    parser.add_argument("--csv", help="also write results to this CSV file")
    parser.add_argument("--json", help="also write raw results to this JSON file")
    args = parser.parse_args()

    try:
        records = fetch_penny_stocks(args.from_datetime)
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
