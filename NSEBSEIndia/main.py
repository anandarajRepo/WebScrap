"""
NSE & BSE Corporate Announcement Scraper
------------------------------------------
Fetches corporate announcements from NSE and BSE public endpoints and
filters for order/contract wins and CEO/management changes.

NOTE: These are unofficial endpoints used by the exchanges' own websites.
Field names can change without notice — if a run returns 0 results, print
the raw JSON for one record and check the field names match below.

Install: pip install requests --break-system-packages
"""

import requests
from datetime import datetime, timedelta

# Keyword fallback (used alongside category filtering)
ORDER_KEYWORDS = ["order", "contract", "won", "awarded", "lic award", "work order"]
CEO_KEYWORDS = ["ceo", "managing director", "appoint", "resign", "step down",
                "chief executive", "key managerial personnel"]

# SEBI LODR disclosure categories — more reliable than keyword matching
NSE_CATEGORY_ORDER = "Award of Order / Receipt of Order"
NSE_CATEGORY_CEO = "Change in Directors, Key Managerial Personnel(KMP), Auditor and Compliance Officer"


# ---------------- NSE ----------------

def get_nse_session() -> requests.Session:
    """NSE requires browser-like cookies before the API will respond."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "*/*",
    })
    # Warm-up requests to set session cookies — order matters
    session.get("https://www.nseindia.com", timeout=10)
    session.get(
        "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        timeout=10,
    )
    return session


def fetch_nse_announcements(session: requests.Session) -> list:
    url = "https://www.nseindia.com/api/corporate-announcements"
    resp = session.get(url, params={"index": "equities"}, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ---------------- BSE ----------------

def fetch_bse_announcements(days_back: int = 3) -> list:
    today = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
    params = {
        "strCat": "-1",
        "strPrevDate": from_date,
        "strScrip": "",
        "strSearch": "P",
        "strToDate": today,
        "strType": "C",
    }
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Referer": "https://www.bseindia.com/corporates/ann.html",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data.get("Table", [])


# ---------------- Filtering ----------------

def text_matches(text: str, keywords: list) -> bool:
    text = (text or "").lower()
    return any(kw in text for kw in keywords)


def classify(desc: str) -> str | None:
    if text_matches(desc, ORDER_KEYWORDS):
        return "ORDER/CONTRACT"
    if text_matches(desc, CEO_KEYWORDS):
        return "CEO/MANAGEMENT"
    return None


# ---------------- Main ----------------

def main():
    print("=" * 60)
    print("NSE announcements")
    print("=" * 60)
    try:
        session = get_nse_session()
        nse_data = fetch_nse_announcements(session)
        for item in nse_data:
            desc = item.get("desc", "") or item.get("attchmntText", "")
            tag = classify(desc)
            if tag:
                print(f"[{tag}] {item.get('symbol')}: {desc} "
                      f"({item.get('an_dt', '')})")
    except Exception as e:
        print(f"NSE fetch failed (likely blocked/changed): {e}")

    print()
    print("=" * 60)
    print("BSE announcements")
    print("=" * 60)
    try:
        bse_rows = fetch_bse_announcements()
        for item in bse_rows:
            desc = item.get("NEWSSUB", "") or item.get("HEADLINE", "")
            tag = classify(desc)
            if tag:
                print(f"[{tag}] {item.get('SCRIP_CD')}: {desc} "
                      f"({item.get('NEWS_DT', '')})")
    except Exception as e:
        print(f"BSE fetch failed: {e}")


if __name__ == "__main__":
    main()