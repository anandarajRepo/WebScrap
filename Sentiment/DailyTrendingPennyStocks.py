"""
DailyTrendingPennyStocks.py

Fetches trending penny stocks (top tickers) from the SwaggyStocks API on a
daily basis for the last 30 days:
https://api.swaggystocks.com/v1/pennystocks/top-tickers?from-datetime=<ISO8601>

The lookback range is sliced into one-day windows; each day is queried
separately and its trending tickers are printed as a table (same table style
as WeeklyTrendingPennyStocks.py). Results can optionally be saved to
CSV/JSON, with each record tagged with the day it trended on.

Install deps once:
    pip install requests --break-system-packages

Usage:
    python3 DailyTrendingPennyStocks.py                          # last 30 days
    python3 DailyTrendingPennyStocks.py --days 14                # last 14 days
    python3 DailyTrendingPennyStocks.py --csv pennystocks.csv    # also write CSV
    python3 DailyTrendingPennyStocks.py --json pennystocks.json  # also write JSON
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
DEFAULT_LOOKBACK_DAYS = 30
REQUEST_TIMEOUT = 30


def _request_page(from_datetime):
    """Fetch one page of ticker records from the API."""
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


def fetch_daily_penny_stocks(days):
    """Return a list of (day, records) pairs, one per day in the lookback.

    Each day is queried with a from-datetime at the start of that day; the
    API's response for that window is treated as the tickers trending on
    that day. Per-day request failures are reported and skipped so one bad
    day doesn't lose the rest.
    """
    now = datetime.now(timezone.utc)
    daily = []
    for offset in range(days, 0, -1):
        day_start = (now - timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        try:
            records = _request_page(day_start.isoformat())
        except (requests.RequestException, ValueError) as exc:
            print(
                f"Failed to fetch {day_start.date()}: {exc}", file=sys.stderr
            )
            records = []
        daily.append((day_start.date().isoformat(), records))
    return daily


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


def print_daily(daily):
    """Print one table per day, most recent day last."""
    total = 0
    for day, records in daily:
        print(f"\n=== Trending penny stocks for {day} ===")
        keys, rows = normalize(records)
        print_table(keys, rows)
        total += len(rows)
    print(f"\n{total} stock(s) fetched across {len(daily)} day(s).")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Fetch daily trending penny stocks from the SwaggyStocks "
            "top-tickers API for the last N days."
        )
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="number of past days to fetch (default: %(default)s)",
    )
    parser.add_argument("--csv", help="also write results to this CSV file")
    parser.add_argument("--json", help="also write raw results to this JSON file")
    args = parser.parse_args()

    daily = fetch_daily_penny_stocks(args.days)
    print_daily(daily)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(
                [{"day": day, "stocks": records} for day, records in daily],
                f,
                indent=2,
            )
        print(f"Wrote {args.json}")

    if args.csv:
        # Flatten all days into one CSV, tagging each row with its day.
        tagged = []
        for day, records in daily:
            for rec in records:
                tagged.append({"day": day, **rec})
        keys, rows = normalize(tagged)
        if "day" in keys:
            keys.insert(0, keys.pop(keys.index("day")))
        if rows:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(rows)
            print(f"Wrote {args.csv}")


if __name__ == "__main__":
    main()
