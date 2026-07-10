"""
BowmanRSISignalsBacktesterV3.py

Python port of the TradingView Pine Script v6 strategy
"Bowman RSI Signals Backtester v3".

Strategy logic
--------------
Entry (long only):
    * RSI(rsi_length) crosses over 30 on the trading timeframe, AND
    * higher-timeframe RSI(htf_rsi_length) is below htf_rsi_cutoff, AND
    * (optionally) close is above an EMA(ema_length) computed on a
      separate EMA timeframe.

Exit (selectable, mirrors the Pine "Exit Strategy" input):
    * "ATR Trailing Stop"      - trailing stop of atr_mult * ATR(atr_length)
    * "Bear Div & Pivot High"  - close when a bearish RSI divergence and a
                                 confirmed pivot high occur on the same bar
    * "Pivot High Only"        - close on any confirmed pivot high
    * "Fixed Percentages"      - take profit +2%, stop loss -1%

Position sizing mirrors the Pine header: initial capital 25,000 with each
entry sized at 25% of current equity (default_qty_type=strategy.percent_of_equity,
default_qty_value=25).

Data is pulled from the Yahoo Finance chart API (same endpoint used by
Sentiment/StockQuotes.py), so the only third-party dependency is `requests`.

Notes on Pine parity
--------------------
* Pine's request.security() calls, by default, return the value of the
  still-forming higher-timeframe bar. This port instead uses the last
  *completed* higher-timeframe bar, which avoids lookahead bias and gives
  slightly more conservative (realistic) results.
* ta.pivothigh(high, L, R) confirms a pivot R bars after it forms; the
  boolean fires on the confirmation bar, exactly as in Pine.
* Fills happen on the close of the signal bar (Pine fills on next bar's
  open; on liquid intraday data the difference is small).

Usage
-----
    python BowmanRSISignalsBacktesterV3.py [SYMBOL] [options]

    python BowmanRSISignalsBacktesterV3.py SPY --interval 1h --range 2y
    python BowmanRSISignalsBacktesterV3.py AAPL --exit-mode "Fixed Percentages"
"""

import argparse
import math
import sys
from datetime import datetime, timezone

import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
CHART_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
CHART_TIMEOUT = 30

EXIT_MODES = (
    "ATR Trailing Stop",
    "Bear Div & Pivot High",
    "Pivot High Only",
    "Fixed Percentages",
)

# Seconds per Yahoo interval, used to bucket base bars into HTF bars.
INTERVAL_SECONDS = {
    "1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800,
    "60m": 3600, "90m": 5400, "1h": 3600,
    "1d": 86400, "5d": 432000, "1wk": 604800, "1mo": 2592000,
}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch_ohlcv(symbol, interval, range_):
    """Return a list of bars {ts, open, high, low, close, volume} from Yahoo."""
    resp = requests.get(
        CHART_URL.format(symbol=symbol),
        params={"interval": interval, "range": range_},
        headers=CHART_HEADERS,
        timeout=CHART_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    ts = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = (quote[k][i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        bars.append({
            "ts": t, "open": o, "high": h, "low": l, "close": c,
            "volume": quote["volume"][i] or 0,
        })
    if not bars:
        raise RuntimeError("Yahoo returned no usable bars for %s" % symbol)
    return bars


def resample(bars, bucket_seconds):
    """Aggregate base bars into higher-timeframe bars of bucket_seconds."""
    htf = []
    for bar in bars:
        bucket = bar["ts"] - bar["ts"] % bucket_seconds
        if htf and htf[-1]["ts"] == bucket:
            last = htf[-1]
            last["high"] = max(last["high"], bar["high"])
            last["low"] = min(last["low"], bar["low"])
            last["close"] = bar["close"]
            last["volume"] += bar["volume"]
        else:
            htf.append({
                "ts": bucket, "open": bar["open"], "high": bar["high"],
                "low": bar["low"], "close": bar["close"],
                "volume": bar["volume"],
            })
    return htf


# ---------------------------------------------------------------------------
# Indicators (Wilder-smoothed, matching Pine's ta.rsi / ta.atr / ta.ema)
# ---------------------------------------------------------------------------

def rsi_series(closes, length):
    """Pine ta.rsi: RMA-smoothed gains/losses."""
    out = [None] * len(closes)
    avg_gain = avg_loss = None
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        if i < length:
            avg_gain = gain if avg_gain is None else avg_gain + gain
            avg_loss = loss if avg_loss is None else avg_loss + loss
            continue
        if i == length:
            avg_gain = (avg_gain + gain) / length
            avg_loss = (avg_loss + loss) / length
        else:
            avg_gain = (avg_gain * (length - 1) + gain) / length
            avg_loss = (avg_loss * (length - 1) + loss) / length
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def ema_series(closes, length):
    out = [None] * len(closes)
    alpha = 2.0 / (length + 1)
    ema = None
    for i, c in enumerate(closes):
        if i == length - 1:
            ema = sum(closes[:length]) / length  # SMA seed, as Pine does
        elif ema is not None:
            ema = alpha * c + (1 - alpha) * ema
        out[i] = ema
    return out


def atr_series(bars, length):
    """Pine ta.atr: RMA of true range."""
    out = [None] * len(bars)
    atr = None
    trs = []
    for i, bar in enumerate(bars):
        if i == 0:
            tr = bar["high"] - bar["low"]
        else:
            prev_close = bars[i - 1]["close"]
            tr = max(bar["high"] - bar["low"],
                     abs(bar["high"] - prev_close),
                     abs(bar["low"] - prev_close))
        if atr is None:
            trs.append(tr)
            if len(trs) == length:
                atr = sum(trs) / length
                out[i] = atr
        else:
            atr = (atr * (length - 1) + tr) / length
            out[i] = atr
    return out


def pivot_high_flags(highs, left, right):
    """True on the bar where ta.pivothigh(high, left, right) confirms."""
    out = [False] * len(highs)
    for i in range(left + right, len(highs)):
        p = i - right  # candidate pivot bar
        window = highs[p - left:p] + highs[p + 1:p + right + 1]
        if all(highs[p] > h for h in window):
            out[i] = True
    return out


def map_htf_values(base_bars, htf_bars, htf_values, bucket_seconds):
    """For each base bar, the indicator value of the last COMPLETED HTF bar."""
    mapped = [None] * len(base_bars)
    j = -1  # index of last completed HTF bar
    for i, bar in enumerate(base_bars):
        bucket = bar["ts"] - bar["ts"] % bucket_seconds
        while j + 1 < len(htf_bars) and htf_bars[j + 1]["ts"] < bucket:
            j += 1
        if j >= 0:
            mapped[i] = htf_values[j]
    return mapped


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest(bars, args):
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]

    rsi = rsi_series(closes, args.rsi_length)
    atr = atr_series(bars, args.atr_length)
    pivoth = pivot_high_flags(highs, args.div_lookback, args.div_lookback)

    htf_bucket = INTERVAL_SECONDS[args.htf_tf]
    htf_bars = resample(bars, htf_bucket)
    htf_rsi_raw = rsi_series([b["close"] for b in htf_bars], args.htf_rsi_length)
    htf_rsi = map_htf_values(bars, htf_bars, htf_rsi_raw, htf_bucket)

    ema_bucket = INTERVAL_SECONDS[args.ema_tf]
    ema_bars = resample(bars, ema_bucket)
    ema_raw = ema_series([b["close"] for b in ema_bars], args.ema_length)
    ema = map_htf_values(bars, ema_bars, ema_raw, ema_bucket)

    equity = args.initial_capital
    position_qty = 0.0
    entry_price = None
    entry_ts = None
    trail_stop = None
    trades = []
    equity_curve = []

    def close_position(i, price, reason):
        nonlocal equity, position_qty, entry_price, entry_ts, trail_stop
        pnl = (price - entry_price) * position_qty
        equity += pnl
        trades.append({
            "entry_ts": entry_ts, "exit_ts": bars[i]["ts"],
            "entry": entry_price, "exit": price, "qty": position_qty,
            "pnl": pnl, "pct": (price / entry_price - 1) * 100,
            "reason": reason,
        })
        position_qty = 0.0
        entry_price = entry_ts = trail_stop = None

    for i, bar in enumerate(bars):
        # ----- manage open position -----
        if position_qty > 0:
            if args.exit_mode == "ATR Trailing Stop":
                if atr[i] is not None:
                    candidate = bar["high"] - atr[i] * args.atr_mult
                    trail_stop = candidate if trail_stop is None else max(trail_stop, candidate)
                if trail_stop is not None and bar["low"] <= trail_stop:
                    close_position(i, min(trail_stop, bar["open"]), "ATR Exit")
            elif args.exit_mode == "Bear Div & Pivot High":
                bear_div = (
                    i >= args.div_lookback
                    and rsi[i] is not None and rsi[i - args.div_lookback] is not None
                    and bar["close"] > bars[i - args.div_lookback]["close"]
                    and rsi[i] < rsi[i - args.div_lookback]
                    and rsi[i] > 70
                )
                if bear_div and pivoth[i]:
                    close_position(i, bar["close"], "Bear Div Pivot Exit")
            elif args.exit_mode == "Pivot High Only":
                if pivoth[i]:
                    close_position(i, bar["close"], "Pivot High Exit")
            elif args.exit_mode == "Fixed Percentages":
                stop = entry_price * (1 - args.fixed_loss_pct / 100)
                target = entry_price * (1 + args.fixed_profit_pct / 100)
                if bar["low"] <= stop:
                    close_position(i, min(stop, bar["open"]), "Fixed Loss")
                elif bar["high"] >= target:
                    close_position(i, max(target, bar["open"]), "Fixed Profit")

        # ----- entries -----
        if position_qty == 0:
            crossover = (
                i > 0 and rsi[i] is not None and rsi[i - 1] is not None
                and rsi[i - 1] <= 30 < rsi[i]
            )
            htf_ok = htf_rsi[i] is not None and htf_rsi[i] < args.htf_rsi_cutoff
            ema_ok = (not args.use_ema_filter) or (
                ema[i] is not None and bar["close"] > ema[i]
            )
            if crossover and htf_ok and ema_ok:
                alloc = equity * args.qty_percent / 100.0
                position_qty = alloc / bar["close"]
                entry_price = bar["close"]
                entry_ts = bar["ts"]
                trail_stop = (
                    bar["close"] - atr[i] * args.atr_mult
                    if args.exit_mode == "ATR Trailing Stop" and atr[i] is not None
                    else None
                )

        mark = equity if position_qty == 0 else equity + (bar["close"] - entry_price) * position_qty
        equity_curve.append(mark)

    # close any open position at the final bar for reporting
    if position_qty > 0:
        close_position(len(bars) - 1, bars[-1]["close"], "End of Data")
        equity_curve[-1] = equity

    return trades, equity_curve


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def print_report(symbol, args, trades, equity_curve):
    print("=" * 78)
    print("Bowman RSI Signals Backtester v3 - %s (%s bars, %s)" %
          (symbol, args.interval, args.range))
    print("Exit mode: %s | RSI %d | HTF %s RSI %d < %d | EMA filter: %s (%s EMA %d)" %
          (args.exit_mode, args.rsi_length, args.htf_tf, args.htf_rsi_length,
           args.htf_rsi_cutoff,
           "on" if args.use_ema_filter else "off", args.ema_tf, args.ema_length))
    print("=" * 78)

    if not trades:
        print("No trades were generated for the given parameters/data.")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    net = gross_profit - gross_loss

    peak = -math.inf
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)

    print("%-17s %-17s %10s %10s %8s  %s" %
          ("Entry (UTC)", "Exit (UTC)", "Entry Px", "Exit Px", "P/L %", "Reason"))
    for t in trades:
        print("%-17s %-17s %10.2f %10.2f %+7.2f%%  %s" %
              (fmt_ts(t["entry_ts"]), fmt_ts(t["exit_ts"]),
               t["entry"], t["exit"], t["pct"], t["reason"]))

    print("-" * 78)
    final_equity = equity_curve[-1]
    print("Trades: %d | Wins: %d | Losses: %d | Win rate: %.1f%%" %
          (len(trades), len(wins), len(losses), 100.0 * len(wins) / len(trades)))
    print("Net P/L: %+.2f (%+.2f%%) | Gross profit: %.2f | Gross loss: %.2f" %
          (net, 100.0 * net / args.initial_capital, gross_profit, gross_loss))
    if gross_loss > 0:
        print("Profit factor: %.2f" % (gross_profit / gross_loss))
    print("Max drawdown: %.2f (%.2f%%)" %
          (max_dd, 100.0 * max_dd / args.initial_capital))
    print("Final equity: %.2f (started with %.2f)" %
          (final_equity, args.initial_capital))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Backtest the Bowman RSI Signals v3 strategy on Yahoo Finance data.")
    p.add_argument("symbol", nargs="?", default="SPY", help="ticker symbol (default SPY)")
    p.add_argument("--interval", default="1h", choices=sorted(INTERVAL_SECONDS),
                   help="trading timeframe (default 1h)")
    p.add_argument("--range", default="1y",
                   help="Yahoo range, e.g. 3mo, 1y, 2y, max (default 1y)")
    p.add_argument("--rsi-length", type=int, default=7)
    p.add_argument("--div-lookback", type=int, default=2)
    p.add_argument("--htf-tf", default="1d", choices=sorted(INTERVAL_SECONDS),
                   help="higher timeframe for the RSI filter (default 1d)")
    p.add_argument("--htf-rsi-length", type=int, default=5)
    p.add_argument("--htf-rsi-cutoff", type=int, default=40)
    p.add_argument("--no-ema-filter", dest="use_ema_filter", action="store_false",
                   help="disable the EMA trend filter")
    p.add_argument("--ema-tf", default="1h", choices=sorted(INTERVAL_SECONDS),
                   help="timeframe for the EMA filter (Pine default 240m; "
                        "default here 1h to match intraday data granularity)")
    p.add_argument("--ema-length", type=int, default=200)
    p.add_argument("--exit-mode", default="ATR Trailing Stop", choices=EXIT_MODES)
    p.add_argument("--atr-length", type=int, default=14)
    p.add_argument("--atr-mult", type=float, default=3.0)
    p.add_argument("--fixed-profit-pct", type=float, default=2.0)
    p.add_argument("--fixed-loss-pct", type=float, default=1.0)
    p.add_argument("--initial-capital", type=float, default=25000.0)
    p.add_argument("--qty-percent", type=float, default=25.0,
                   help="percent of equity per entry (default 25)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if INTERVAL_SECONDS[args.htf_tf] < INTERVAL_SECONDS[args.interval]:
        sys.exit("HTF timeframe must be >= trading interval")
    if INTERVAL_SECONDS[args.ema_tf] < INTERVAL_SECONDS[args.interval]:
        sys.exit("EMA timeframe must be >= trading interval")
    try:
        bars = fetch_ohlcv(args.symbol.upper(), args.interval, args.range)
    except Exception as exc:
        sys.exit("Failed to fetch data for %s: %s" % (args.symbol, exc))
    trades, equity_curve = run_backtest(bars, args)
    print_report(args.symbol.upper(), args, trades, equity_curve)


if __name__ == "__main__":
    main()
