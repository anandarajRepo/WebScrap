"""Fetch news for watchlist stocks and print headlines with links, grouped by date.

Uses the Google News RSS feed (no API key required).

By default this reads the watchlist at ``resources/watchlist.json`` and fetches
news for every stock in it. You can also pass a single stock name on the command
line to fetch news for just that stock, or launch a browser UI to view all the
news interactively.

Usage:
    python main.py                      # fetch news for every stock in the watchlist
    python main.py "Aequs Ltd"          # fetch news for a single stock
    python main.py --days 30            # only news from the last 30 days
    python main.py --watchlist path.json  # use a custom watchlist file
    python main.py --serve              # open a browser UI to view all stock news
    python main.py --serve --port 8080  # serve the UI on a specific port
"""

import argparse
import json
import os
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote_plus, urlparse
from xml.etree import ElementTree

import requests

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Path to the shared watchlist: <repo root>/resources/watchlist.json
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WATCHLIST = os.path.join(_PROJECT_ROOT, "resources", "watchlist.json")


def load_watchlist(path=DEFAULT_WATCHLIST):
    """Return the list of ticker entries from the watchlist JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("tickers", [])


def fetch_news(stock, days=None):
    """Return a list of dicts: {date, headline, link, source} for the given stock."""
    query = quote_plus(f'"{stock}" stock')
    url = GOOGLE_NEWS_RSS.format(query=query)
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    root = ElementTree.fromstring(response.content)
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    articles = []
    for item in root.iter("item"):
        title = item.findtext("title", default="").strip()
        link = item.findtext("link", default="").strip()
        pub_date_raw = item.findtext("pubDate", default="").strip()
        source = item.findtext("source", default="").strip()

        pub_date = None
        if pub_date_raw:
            try:
                pub_date = parsedate_to_datetime(pub_date_raw)
            except (TypeError, ValueError):
                pass

        if cutoff and pub_date and pub_date < cutoff:
            continue

        articles.append({
            "date": pub_date,
            "headline": title,
            "link": link,
            "source": source,
        })

    articles.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return articles


def print_news(stock, articles):
    if not articles:
        print(f"No news found for '{stock}'.")
        return

    print(f"\nNews for '{stock}' ({len(articles)} articles)\n" + "=" * 60)
    current_day = None
    for article in articles:
        day = article["date"].strftime("%d %b %Y (%A)") if article["date"] else "Unknown date"
        if day != current_day:
            current_day = day
            print(f"\n📅 {day}\n" + "-" * 60)
        source = f" — {article['source']}" if article["source"] else ""
        print(f"• {article['headline']}{source}")
        print(f"  {article['link']}")


def fetch_watchlist_news(days=None, watchlist_path=DEFAULT_WATCHLIST):
    """Fetch and print news for every stock in the watchlist."""
    try:
        tickers = load_watchlist(watchlist_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Failed to load watchlist '{watchlist_path}': {exc}")
        sys.exit(1)

    if not tickers:
        print(f"No tickers found in watchlist '{watchlist_path}'.")
        return

    print(f"Fetching news for {len(tickers)} watchlist stocks from '{watchlist_path}'")
    for entry in tickers:
        # Use the company name for the query; fall back to the ticker symbol.
        stock = entry.get("name") or entry.get("ticker")
        if not stock:
            continue
        ticker = entry.get("ticker", "")
        header = f"{stock} ({ticker})" if ticker else stock
        print("\n" + "#" * 60)
        print(f"# {header}")
        print("#" * 60)
        try:
            articles = fetch_news(stock, days=days)
        except requests.RequestException as exc:
            print(f"Failed to fetch news for '{stock}': {exc}")
            continue
        print_news(stock, articles)


# ---------------------------------------------------------------------------
# Browser UI
# ---------------------------------------------------------------------------

def _article_to_json(article):
    """Convert an article dict (with a datetime) into a JSON-serialisable dict."""
    pub_date = article["date"]
    return {
        "headline": article["headline"],
        "link": article["link"],
        "source": article["source"],
        "date": pub_date.isoformat() if pub_date else None,
        "date_label": pub_date.strftime("%d %b %Y (%A)") if pub_date else "Unknown date",
    }


def collect_watchlist_news(days=None, watchlist_path=DEFAULT_WATCHLIST, max_workers=8):
    """Fetch news for every watchlist stock and return a JSON-serialisable list.

    Each entry is ``{ticker, name, articles, error}``. Fetches run concurrently
    so the browser UI does not stall on a long watchlist.
    """
    tickers = load_watchlist(watchlist_path)

    def _fetch_entry(entry):
        stock = entry.get("name") or entry.get("ticker")
        result = {
            "ticker": entry.get("ticker", ""),
            "name": stock or entry.get("ticker", ""),
            "articles": [],
            "error": None,
        }
        if not stock:
            result["error"] = "Watchlist entry has no name or ticker."
            return result
        try:
            articles = fetch_news(stock, days=days)
            result["articles"] = [_article_to_json(a) for a in articles]
        except requests.RequestException as exc:
            result["error"] = str(exc)
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_fetch_entry, tickers))
    return results


PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock News</title>
<style>
  :root {
    --bg: #0f172a;
    --panel: #1e293b;
    --panel-2: #273449;
    --border: #334155;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --accent-2: #22c55e;
    --danger: #f87171;
    --sidebar-w: 320px;
    --header-h: 69px;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  header {
    position: sticky;
    top: 0;
    z-index: 10;
    background: rgba(15, 23, 42, 0.92);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
  }
  .header-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
  }
  h1 { font-size: 20px; margin: 0; display: flex; align-items: center; gap: 8px; }
  h1 .dot { color: var(--accent); }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; margin-left: auto; align-items: center; }
  input[type="search"], select {
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 14px;
    outline: none;
  }
  input[type="search"] { min-width: 220px; }
  input[type="search"]:focus, select:focus { border-color: var(--accent); }
  button {
    background: var(--accent);
    color: #06263a;
    border: none;
    padding: 8px 16px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
  }
  button:disabled { opacity: 0.6; cursor: default; }

  /* Two-pane master/detail layout */
  .layout {
    display: flex;
    align-items: stretch;
    height: calc(100vh - var(--header-h));
  }
  .sidebar {
    width: var(--sidebar-w);
    flex: 0 0 var(--sidebar-w);
    border-right: 1px solid var(--border);
    background: var(--bg);
    overflow-y: auto;
    display: flex;
    flex-direction: column;
  }
  .sidebar-meta {
    padding: 12px 16px;
    color: var(--muted);
    font-size: 13px;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 1;
  }
  .stock-list { list-style: none; margin: 0; padding: 6px; }
  .stock-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border-radius: 8px;
    cursor: pointer;
    user-select: none;
  }
  .stock-item:hover { background: var(--panel); }
  .stock-item.active { background: var(--panel-2); }
  .stock-item.active .stock-name { color: var(--accent); }
  .stock-item .stock-name { font-weight: 600; font-size: 14px; }
  .stock-item .ticker {
    font-size: 11px;
    color: var(--accent);
    background: rgba(56, 189, 248, 0.12);
    padding: 2px 7px;
    border-radius: 999px;
  }
  .stock-item .count {
    color: var(--muted);
    font-size: 12px;
    margin-left: auto;
    background: var(--panel);
    padding: 1px 8px;
    border-radius: 999px;
  }
  .stock-item.active .count { background: var(--bg); }
  .stock-item.has-error .stock-name { color: var(--danger); }

  .detail {
    flex: 1 1 auto;
    overflow-y: auto;
    padding: 24px 32px 64px;
  }
  .detail-head {
    display: flex;
    align-items: center;
    gap: 12px;
    padding-bottom: 12px;
    margin-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .detail-head .stock-name { font-size: 22px; font-weight: 700; }
  .detail-head .ticker {
    font-size: 13px;
    color: var(--accent);
    background: rgba(56, 189, 248, 0.12);
    padding: 3px 10px;
    border-radius: 999px;
  }
  .detail-head .count { color: var(--muted); font-size: 14px; margin-left: auto; }

  .day-group { margin-top: 18px; }
  .day-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    padding-bottom: 4px;
    margin-bottom: 8px;
  }
  .article { padding: 8px 0; }
  .article a {
    color: var(--text);
    text-decoration: none;
    font-size: 15px;
  }
  .article a:hover { color: var(--accent); text-decoration: underline; }
  .article .source { color: var(--muted); font-size: 12px; margin-left: 6px; }
  .empty { color: var(--muted); font-size: 14px; padding: 8px 0; }
  .error { color: var(--danger); font-size: 14px; padding: 8px 0; }
  .banner {
    text-align: center;
    padding: 60px 20px;
    color: var(--muted);
  }
  .placeholder {
    display: flex;
    height: 100%;
    align-items: center;
    justify-content: center;
    color: var(--muted);
    font-size: 15px;
    text-align: center;
  }
  .spinner {
    width: 28px; height: 28px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    margin: 0 auto 16px;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 720px) {
    .layout { flex-direction: column; height: auto; }
    .sidebar {
      width: 100%;
      flex-basis: auto;
      max-height: 40vh;
      border-right: none;
      border-bottom: 1px solid var(--border);
    }
    .detail { padding: 20px; }
  }
</style>
</head>
<body>
<header>
  <div class="header-row">
    <h1><span class="dot">&#9679;</span> Stock News</h1>
    <div class="controls">
      <input id="search" type="search" placeholder="Filter stocks or headlines...">
      <select id="days">
        <option value="">All time</option>
        <option value="1">Last 24 hours</option>
        <option value="3">Last 3 days</option>
        <option value="7" selected>Last 7 days</option>
        <option value="30">Last 30 days</option>
        <option value="90">Last 90 days</option>
      </select>
      <button id="refresh">Refresh</button>
    </div>
  </div>
</header>
<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-meta" id="meta">Loading watchlist news...</div>
    <ul class="stock-list" id="stock-list"></ul>
  </aside>
  <section class="detail" id="detail">
    <div class="banner"><div class="spinner"></div>Fetching the latest news for your watchlist...</div>
  </section>
</div>

<script>
const listEl = document.getElementById('stock-list');
const detailEl = document.getElementById('detail');
const metaEl = document.getElementById('meta');
const searchEl = document.getElementById('search');
const daysEl = document.getElementById('days');
const refreshBtn = document.getElementById('refresh');
let allData = [];
let selectedKey = null;

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : s;
  return d.innerHTML;
}

function stockKey(stock) {
  return (stock.ticker || '') + '|' + stock.name;
}

function groupByDay(articles) {
  const groups = [];
  const index = {};
  for (const a of articles) {
    const key = a.date_label;
    if (!(key in index)) {
      index[key] = groups.length;
      groups.push({ label: key, items: [] });
    }
    groups[index[key]].items.push(a);
  }
  return groups;
}

// Return the articles for a stock that pass the current headline filter.
function visibleArticles(stock, q) {
  if (!q) return stock.articles;
  const nameMatch = stock.name.toLowerCase().includes(q) ||
    (stock.ticker && stock.ticker.toLowerCase().includes(q));
  if (nameMatch) return stock.articles;
  return stock.articles.filter(a => a.headline.toLowerCase().includes(q) ||
    (a.source && a.source.toLowerCase().includes(q)));
}

function renderDetail(stock, q) {
  if (!stock) {
    detailEl.innerHTML = '<div class="placeholder">Select a stock on the left to see its news.</div>';
    return;
  }
  const articles = visibleArticles(stock, q);
  const tickerHtml = stock.ticker ? '<span class="ticker">' + esc(stock.ticker) + '</span>' : '';

  let bodyHtml;
  if (stock.error) {
    bodyHtml = '<div class="error">Failed to fetch: ' + esc(stock.error) + '</div>';
  } else if (articles.length === 0) {
    bodyHtml = '<div class="empty">No news found' + (q ? ' for this filter.' : '.') + '</div>';
  } else {
    bodyHtml = '';
    for (const g of groupByDay(articles)) {
      bodyHtml += '<div class="day-group"><div class="day-label">' + esc(g.label) + '</div>';
      for (const a of g.items) {
        const src = a.source ? '<span class="source">' + esc(a.source) + '</span>' : '';
        bodyHtml += '<div class="article"><a href="' + esc(a.link) +
          '" target="_blank" rel="noopener">' + esc(a.headline) + '</a>' + src + '</div>';
      }
      bodyHtml += '</div>';
    }
  }

  detailEl.innerHTML =
    '<div class="detail-head">' +
      '<span class="stock-name">' + esc(stock.name) + '</span>' +
      tickerHtml +
      '<span class="count">' + articles.length + ' article' + (articles.length === 1 ? '' : 's') + '</span>' +
    '</div>' + bodyHtml;
  detailEl.scrollTop = 0;
}

function render() {
  const q = searchEl.value.trim().toLowerCase();
  let totalArticles = 0;
  let shownStocks = 0;
  const frag = document.createDocumentFragment();
  const visibleStocks = [];

  for (const stock of allData) {
    const nameMatch = !q || stock.name.toLowerCase().includes(q) ||
      (stock.ticker && stock.ticker.toLowerCase().includes(q));
    const articles = visibleArticles(stock, q);

    if (q && !nameMatch && articles.length === 0) continue;
    shownStocks++;
    totalArticles += articles.length;
    visibleStocks.push(stock);

    const key = stockKey(stock);
    const li = document.createElement('li');
    li.className = 'stock-item' + (stock.error ? ' has-error' : '') +
      (key === selectedKey ? ' active' : '');

    const tickerHtml = stock.ticker ? '<span class="ticker">' + esc(stock.ticker) + '</span>' : '';
    li.innerHTML =
      '<span class="stock-name">' + esc(stock.name) + '</span>' +
      tickerHtml +
      '<span class="count">' + articles.length + '</span>';

    li.addEventListener('click', () => {
      selectedKey = key;
      for (const el of listEl.querySelectorAll('.stock-item')) el.classList.remove('active');
      li.classList.add('active');
      renderDetail(stock, searchEl.value.trim().toLowerCase());
    });
    frag.appendChild(li);
  }

  listEl.innerHTML = '';
  listEl.appendChild(frag);

  metaEl.textContent = shownStocks + ' stock' + (shownStocks === 1 ? '' : 's') +
    ' · ' + totalArticles + ' article' + (totalArticles === 1 ? '' : 's') +
    ' · ' + new Date().toLocaleTimeString();

  if (shownStocks === 0) {
    listEl.innerHTML = '<li class="banner">No stocks match your filter.</li>';
    selectedKey = null;
    renderDetail(null, q);
    return;
  }

  // Keep the current selection if it's still visible; otherwise select the first.
  let selected = visibleStocks.find(s => stockKey(s) === selectedKey);
  if (!selected) {
    selected = visibleStocks[0];
    selectedKey = stockKey(selected);
    for (const el of listEl.querySelectorAll('.stock-item')) el.classList.remove('active');
    listEl.querySelector('.stock-item').classList.add('active');
  }
  renderDetail(selected, q);
}

async function load() {
  refreshBtn.disabled = true;
  listEl.innerHTML = '';
  metaEl.textContent = 'Loading watchlist news...';
  detailEl.innerHTML =
    '<div class="banner"><div class="spinner"></div>Fetching the latest news for your watchlist...</div>';
  try {
    const days = daysEl.value;
    const url = '/api/news' + (days ? ('?days=' + encodeURIComponent(days)) : '');
    const resp = await fetch(url);
    if (!resp.ok) throw new Error('Server responded ' + resp.status);
    const data = await resp.json();
    allData = data.stocks || [];
    render();
  } catch (e) {
    metaEl.textContent = 'Error';
    detailEl.innerHTML = '<div class="banner error">Could not load news: ' + esc(e.message) + '</div>';
  } finally {
    refreshBtn.disabled = false;
  }
}

searchEl.addEventListener('input', render);
daysEl.addEventListener('change', load);
refreshBtn.addEventListener('click', load);
load();
</script>
</body>
</html>
"""


class NewsUIHandler(BaseHTTPRequestHandler):
    """Serve the browser UI and a JSON news API."""

    # Injected by the server factory below.
    watchlist_path = DEFAULT_WATCHLIST
    default_days = None

    def _send(self, status, content_type, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE_HTML)
            return

        if parsed.path == "/api/news":
            params = parse_qs(parsed.query)
            days = self.default_days
            if "days" in params:
                try:
                    days = int(params["days"][0])
                except (ValueError, IndexError):
                    days = self.default_days
            try:
                stocks = collect_watchlist_news(days=days, watchlist_path=self.watchlist_path)
                payload = json.dumps({"stocks": stocks})
                self._send(200, "application/json; charset=utf-8", payload)
            except (OSError, json.JSONDecodeError) as exc:
                payload = json.dumps({"error": f"Failed to load watchlist: {exc}"})
                self._send(500, "application/json; charset=utf-8", payload)
            return

        self._send(404, "text/plain; charset=utf-8", "Not found")

    def log_message(self, fmt, *args):  # noqa: A003 - quieten default logging
        # Keep the console readable: only surface API requests.
        if args and str(args[0]).startswith(("GET /api", "POST")):
            super().log_message(fmt, *args)


def serve_ui(host="127.0.0.1", port=8000, days=None, watchlist_path=DEFAULT_WATCHLIST, open_browser=True):
    """Start the browser UI server and (optionally) open a browser tab."""
    handler = type(
        "BoundNewsUIHandler",
        (NewsUIHandler,),
        {"watchlist_path": watchlist_path, "default_days": days},
    )
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Serving stock news UI at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Fetch stock news headlines with links, grouped by date.")
    parser.add_argument(
        "stock",
        nargs="?",
        help='Stock/company name, e.g. "Aequs Ltd". If omitted, news is fetched for '
             "every stock in the watchlist.",
    )
    parser.add_argument("--days", type=int, default=None, help="Only show news from the last N days")
    parser.add_argument(
        "--watchlist",
        default=DEFAULT_WATCHLIST,
        help="Path to the watchlist JSON file (default: resources/watchlist.json)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Launch a browser UI to view all watchlist stock news.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for --serve (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port for --serve (default: 8000)")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="With --serve, do not automatically open a browser tab.",
    )
    args = parser.parse_args()

    if args.serve:
        serve_ui(
            host=args.host,
            port=args.port,
            days=args.days,
            watchlist_path=args.watchlist,
            open_browser=not args.no_browser,
        )
    elif args.stock:
        # Single-stock mode.
        try:
            articles = fetch_news(args.stock, days=args.days)
        except requests.RequestException as exc:
            print(f"Failed to fetch news: {exc}")
            sys.exit(1)
        print_news(args.stock, articles)
    else:
        # Watchlist mode.
        fetch_watchlist_news(days=args.days, watchlist_path=args.watchlist)


if __name__ == "__main__":
    main()
