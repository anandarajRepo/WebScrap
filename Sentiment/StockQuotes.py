"""
StockQuotes.py

Shared helper for enriching ticker records with the full company name and
the current trading price, looked up from the Yahoo Finance chart API:
https://query1.finance.yahoo.com/v8/finance/chart/<symbol>

Used by DailyTrendingPennyStocks.py and WeeklyTrendingPennyStocks.py.
"""

import sys

import requests

QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
QUOTE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
QUOTE_TIMEOUT = 15

TICKER_KEYS = ("ticker", "symbol", "stock")


def record_symbol(rec):
    """Return the ticker symbol of a record, or None if it has none."""
    for key in TICKER_KEYS:
        value = rec.get(key)
        if value:
            return str(value).strip().upper()
    return None


def fetch_quote(symbol, session=None):
    """Return {"name": ..., "price": ...} for a symbol via Yahoo Finance.

    Returns None if the lookup fails (unknown/delisted symbol, network
    error, unexpected response shape).
    """
    getter = session or requests
    try:
        resp = getter.get(
            QUOTE_URL.format(symbol=symbol),
            headers=QUOTE_HEADERS,
            timeout=QUOTE_TIMEOUT,
        )
        resp.raise_for_status()
        meta = resp.json()["chart"]["result"][0]["meta"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None
    name = meta.get("longName") or meta.get("shortName") or ""
    price = meta.get("regularMarketPrice")
    currency = meta.get("currency") or ""
    if price is not None:
        price = f"{price:.4g} {currency}".strip()
    return {"name": name, "price": price if price is not None else ""}


def enrich_with_quotes(records):
    """Add "name" and "price" fields to each record, in place.

    Each unique symbol is looked up once; lookup failures leave the fields
    blank so the table still prints. Returns the same list for convenience.
    """
    symbols = {record_symbol(rec) for rec in records}
    symbols.discard(None)
    if not symbols:
        return records

    quotes = {}
    with requests.Session() as session:
        for symbol in sorted(symbols):
            quote = fetch_quote(symbol, session)
            if quote is None:
                print(f"Quote lookup failed for {symbol}", file=sys.stderr)
                quote = {"name": "", "price": ""}
            quotes[symbol] = quote

    for rec in records:
        quote = quotes.get(record_symbol(rec), {"name": "", "price": ""})
        rec["name"] = quote["name"]
        rec["price"] = quote["price"]
    return records
