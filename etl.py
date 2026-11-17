"""
Live Form 4 ETL: SEC EDGAR -> Supabase (insider_trades)

Usage:
  python etl.py                      # live mode: poll EDGAR's "latest filings" Atom feed
  python etl.py daily [YYYY-MM-DD]   # catch-up sweep from the daily index (default: yesterday, ET)
"""
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import requests
import yfinance as yf

# SEC requires a descriptive User-Agent with contact email
HEADERS = {'User-Agent': 'InsiderTradingApp JosephLong1226@gmail.com'}
SUPABASE_URL = os.environ.get("SUPABASE_DB_URL")

MIN_VALUE = 50_000
EASTERN = ZoneInfo("America/New_York")


def get_db_connection():
    if not SUPABASE_URL:
        raise ValueError("Missing database connection string. Set SUPABASE_DB_URL.")
    return psycopg2.connect(SUPABASE_URL)


# ---------------------------------------------------------------------------
# Sector / ticker / market-cap helpers (unchanged from your working version)
# ---------------------------------------------------------------------------
sector_cache = {}


def infer_sector_from_name(company_name):
    name = company_name.upper()
    if any(k in name for k in ["TOURISM", "ENTERTAINMENT", "HOTEL", "RESORT", "RESTAURANT", "TRAVEL"]):
        return "Consumer Cyclical"
    if any(k in name for k in ["ENERGY", "OIL", "GAS", "POWER", "ELECTRIC", "UTILITY"]):
        return "Utilities"
    if any(k in name for k in ["PHARMA", "HEALTH", "BIOTECH", "MEDICAL", "THERAPEUTIC", "SURGICAL", "SPINE", "DEVICE", "CLINIC", "BIOSCIENCES"]):
        return "Healthcare"
    if any(k in name for k in ["BANK", "FINANCIAL", "CAPITAL", "INVESTMENT", "FUND", "BANCORP"]):
        return "Financial Services"
    if any(k in name for k in ["TECH", "SOFTWARE", "SYSTEMS", "SOLUTIONS", "SEMICONDUCTOR", "NETWORKS"]):
        return "Technology"
    if any(k in name for k in ["GOLD", "MINING", "RESOURCES", "METALS", "STEEL"]):
        return "Basic Materials"
    return "General Equity"


def get_sector_for_ticker(ticker, company_name=""):
    if not ticker or ticker in ("NONE", "UNKNOWN", "N/A", "NULL"):
        return infer_sector_from_name(company_name)

    clean_ticker = clean_ticker_for_yf(ticker)
    if clean_ticker in sector_cache:
        return sector_cache[clean_ticker]

    try:
        t = yf.Ticker(clean_ticker)
        yf_sector = t.info.get('sector')
        if yf_sector and yf_sector.strip():
            sector_cache[clean_ticker] = yf_sector
            return yf_sector
    except Exception:
        pass

    fallback_sector = infer_sector_from_name(company_name)
    sector_cache[clean_ticker] = fallback_sector
    return fallback_sector


TICKER_ALIASES = {
    "LBTY": "LBTYA",
    "GOOG": "GOOGL",
    "BRK.B": "BRK-B",
}


def clean_ticker_for_yf(ticker):
    if not ticker:
        return ""
    clean = str(ticker).strip().upper()
    clean = clean.replace('"', '').replace("'", "").replace('(', '').replace(')', '').replace('[', '').replace(']', '')
    if ":" in clean:
        clean = clean.split(":")[-1].strip()
    clean = re.sub(r'[\s]', '', clean)
    if "," in clean:
        clean = clean.split(",")[0].strip()
    elif "/" in clean:
        clean = clean.split("/")[0].strip()
    return TICKER_ALIASES.get(clean, clean)


def assign_market_cap_tier(market_cap_val):
    if not market_cap_val or market_cap_val <= 0:
        return 'Unknown', None
    if market_cap_val < 2_000_000_000:
        return 'Micro/Small', 'IWM'
    elif 2_000_000_000 <= market_cap_val <= 10_000_000_000:
        return 'Mid', 'IJH'
    else:
        return 'Large', 'SPY'


mcap_cache = {}


def get_point_in_time_metadata(ticker_symbol, tx_date_str):
    cleaned_ticker = clean_ticker_for_yf(ticker_symbol)
    if not cleaned_ticker or not re.match(r'^[A-Z\-\.]{1,6}$', cleaned_ticker):
        return None, 'Unknown', None

    cache_key = f"{cleaned_ticker}_{tx_date_str}"
    if cache_key in mcap_cache:
        return mcap_cache[cache_key]

    try:
        tx_date = datetime.strptime(tx_date_str.strip()[:10], '%Y-%m-%d').date()
        start_date = tx_date - timedelta(days=5)
        end_date = tx_date + timedelta(days=5)

        t = yf.Ticker(cleaned_ticker)
        hist = t.history(start=str(start_date), end=str(end_date))
        if hist.empty or 'Close' not in hist.columns:
            mcap_cache[cache_key] = (None, 'Unknown', None)
            return None, 'Unknown', None

        hist.index = hist.index.date
        if tx_date in hist.index:
            price = float(hist.loc[tx_date, 'Close'])
        else:
            hist['diff'] = abs(pd.to_datetime(hist.index) - pd.to_datetime(tx_date))
            price = float(hist.loc[hist['diff'].idxmin(), 'Close'])

        shares = None
        try:
            shares_df = t.get_shares_full(start=str(tx_date - timedelta(days=180)), end=str(tx_date))
            if shares_df is not None and not shares_df.empty:
                shares = float(shares_df.iloc[-1])
        except Exception:
            pass

        if not shares or shares <= 0:
            try:
                shares = float(t.fast_info['shares']) if 'shares' in t.fast_info else None
            except Exception:
                pass

        if not shares or shares <= 0:
            mcap_cache[cache_key] = (None, 'Unknown', None)
            return None, 'Unknown', None

        historical_mcap = price * shares
        category, benchmark = assign_market_cap_tier(historical_mcap)

        result = (historical_mcap, category, benchmark)
        mcap_cache[cache_key] = result
        return result
    except Exception:
        mcap_cache[cache_key] = (None, 'Unknown', None)
        return None, 'Unknown', None


# ---------------------------------------------------------------------------
# SEC fetching
# ---------------------------------------------------------------------------
ATOM = '{http://www.w3.org/2005/Atom}'
FEED_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4"
            "&company=&dateb=&owner=include&start={start}&count=100&output=atom")


def sec_get(url, retries=3, timeout=20):
    """GET with retries. Returns the Response on HTTP 200, otherwise None (404 is not retried)."""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            print(f"  HTTP {r.status_code} for {url} (attempt {attempt}/{retries})")
            if r.status_code == 404:
                return None
        except requests.exceptions.RequestException as e:
            print(f"  Request error for {url}: {e} (attempt {attempt}/{retries})")
        time.sleep(1.5 * attempt)
    return None


def parse_atom(content):
    """Parse the EDGAR 'current filings' Atom feed into a list of filing dicts."""
    root = ET.fromstring(content)
    entries = []
    for e in root.findall(f'{ATOM}entry'):
        title = e.findtext(f'{ATOM}title') or ''
        cat = e.find(f'{ATOM}category')
        form_type = (cat.get('term') if cat is not None else '') or title.split(' - ')[0]
        link = e.find(f'{ATOM}link')
        href = link.get('href', '') if link is not None else ''

        m_acc = re.search(r'(\d{10}-\d{2}-\d{6})', href)
        m_cik = re.search(r'/data/(\d+)/', href)
        if not (m_acc and m_cik):
            continue
        m_name = re.match(r'^\S+\s*-\s*(.*?)\s*\(\d{10}\)', title)
        acc = m_acc.group(1)
        entries.append({
            'accession_number': acc,
            'form_type': form_type.strip(),
            'company': m_name.group(1) if m_name else '',
            # full submission text file: contains the Form 4 XML, same as the backfill reads
            'url': f"https://www.sec.gov/Archives/edgar/data/{m_cik.group(1)}/{acc}.txt",
        })
    return entries


def fetch_live_entries(seen, max_pages=5):
    """Newest Form 4 / 4/A filings from the Atom feed that we haven't processed yet."""
    picked = {}
    for page in range(max_pages):
        r = sec_get(FEED_URL.format(start=page * 100))
        if r is None:
            if page == 0:
                raise RuntimeError("Could not fetch the EDGAR current-filings feed.")
            break
        page_entries = [e for e in parse_atom(r.content) if e['form_type'] in ('4', '4/A')]
        print(f"  feed page {page + 1}: {len(page_entries)} Form 4 / 4/A entries")
        if not page_entries:
            break
        unknown = [e for e in page_entries if e['accession_number'] not in seen]
        if not unknown:
            break  # whole page already processed; older pages will be too
        for e in unknown:
            picked.setdefault(e['accession_number'], e)  # each filing shows up twice (issuer + owner)
        time.sleep(0.2)
    entries = list(picked.values())
    entries.reverse()  # oldest first
    return entries


def fetch_daily_entries(day):
    """All Form 4 / 4/A filings from one day's master index."""
    qtr = (day.month - 1) // 3 + 1
    url = f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{qtr}/master.{day:%Y%m%d}.idx"
    r = sec_get(url)
    if r is None:
        print(f"No daily index available for {day} (weekend/holiday, or not published yet).")
        return []
    found = {}
    for line in r.text.splitlines():
        parts = line.split('|')
        if len(parts) == 5 and parts[2] in ('4', '4/A'):
            filename = parts[4].strip()
            acc = filename.split('/')[-1].replace('.txt', '')
            found.setdefault(acc, {
                'accession_number': acc,
                'form_type': parts[2],
                'company': parts[1],
                'url': f"https://www.sec.gov/Archives/{filename}",
            })
    return list(found.values())


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
INSERT_QUERY = """
    INSERT INTO insider_trades (
        transaction_date, ticker, sector, insider_name, insider_title,
        shares_bought, shares_owned_following, est_value, activity_type,
        market_cap, market_cap_category, benchmark_ticker, is_10b51, accession_number, price_at_tx
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING;
"""

# A 4/A replaces earlier rows for the same ticker + insider + transaction date
DELETE_AMENDED_QUERY = """
    DELETE FROM insider_trades
    WHERE ticker = %s
      AND insider_name = %s
      AND transaction_date = %s
      AND accession_number IS DISTINCT FROM %s;
"""


def ensure_tables(cursor, conn):
    # Remembers EVERY filing we've looked at (including ones with no qualifying purchase),
    # so we never re-download the same filing every 5 minutes.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS etl_processed_filings (
            accession_number TEXT PRIMARY KEY,
            form_type TEXT,
            processed_at TIMESTAMPTZ DEFAULT now()
        );
    """)
    # Keeps the table private from Supabase's public API (your direct DB connection is unaffected)
    cursor.execute("ALTER TABLE etl_processed_filings ENABLE ROW LEVEL SECURITY;")
    conn.commit()


def get_seen_accessions(cursor):
    cursor.execute("SELECT accession_number FROM insider_trades WHERE accession_number IS NOT NULL;")
    seen = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT accession_number FROM etl_processed_filings;")
    seen |= {row[0] for row in cursor.fetchall()}
    print(f"Loaded {len(seen)} already-processed accession numbers.")
    return seen


def mark_processed(cursor, acc, form_type):
    cursor.execute(
        "INSERT INTO etl_processed_filings (accession_number, form_type) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING;", (acc, form_type))


# ---------------------------------------------------------------------------
# Parsing + processing
# ---------------------------------------------------------------------------
def safe_float(s):
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def extract_purchases(root):
    """Open-market purchases (code P, acquired A), combined per transaction date.

    Insiders often buy in many small lots on one day; each lot is its own line on the form.
    Summing per date means a $400k day isn't thrown away because every lot was under $50k.
    """
    agg = {}
    for tx in root.findall(".//nonDerivativeTransaction"):
        code = tx.findtext(".//transactionCoding/transactionCode")
        acquired = tx.findtext(".//transactionAmounts/transactionAcquiredDisposedCode/value")
        if code != 'P' or acquired != 'A':
            continue
        tx_date = (tx.findtext(".//transactionDate/value") or "").strip()[:10]
        if not tx_date:
            continue
        shares = safe_float(tx.findtext(".//transactionShares/value"))
        price = safe_float(tx.findtext(".//transactionPricePerShare/value"))
        owned = safe_float(tx.findtext(".//sharesOwnedFollowingTransaction/value"))
        a = agg.setdefault(tx_date, {'shares': 0.0, 'value': 0.0, 'owned': 0.0})
        a['shares'] += shares
        a['value'] += shares * price
        a['owned'] = owned  # holdings after the last lot of the day
    return agg


def process_filing(entry, seen, cursor, conn, stats):
    acc = entry['accession_number']
    form_type = entry['form_type']
    is_amendment = form_type == '4/A'

    time.sleep(0.12)  # stay under SEC's 10 requests/sec limit
    res = sec_get(entry['url'], retries=2)
    if res is None:
        stats['retry'] += 1  # not marked processed -> tried again next run
        return

    def done(stat):
        mark_processed(cursor, acc, form_type)
        conn.commit()
        seen.add(acc)
        stats[stat] += 1

    text = res.text
    low = text.lower()
    s, e = low.find("<xml>"), low.find("</xml>")
    if s == -1 or e == -1:
        return done('no_xml')

    xml_content = re.sub(r'^\s*<\?xml[^>]*\?>', '', text[s + 5:e].strip())
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as pe:
        print(f"  bad XML in {acc}: {pe}")
        return done('bad_xml')

    ticker = "NONE"
    for tag in [".//issuerTradingSymbol", ".//tradingSymbol", ".//symbol"]:
        cleaned = clean_ticker_for_yf(root.findtext(tag))
        if cleaned and re.match(r'^[A-Z\-\.]{1,6}$', cleaned):
            ticker = cleaned
            break
    if ticker in ('NONE', '[NONE]', 'UNKNOWN', 'N/A') or (len(ticker) == 5 and ticker.endswith('X')):
        return done('no_ticker')

    purchases = extract_purchases(root)
    if not purchases:
        return done('no_purchase')

    owner = root.find(".//reportingOwner")
    insider = ((owner.findtext(".//rptOwnerName") if owner is not None else None) or "UNKNOWN").strip()
    title = ((owner.findtext(".//officerTitle") if owner is not None else None) or "Director/Other").strip()

    remarks = (root.findtext(".//remarks") or "").upper()
    is_10b51 = ("10B5-1" in remarks or "10B51" in remarks
                or (root.findtext(".//aff10b5One") or "").strip().lower() in ("1", "true"))

    qualifying = {d: a for d, a in purchases.items() if a['value'] >= MIN_VALUE}
    rows = []
    if qualifying:
        sector = get_sector_for_ticker(ticker, entry['company'])  # slow yfinance call: only when needed
        for tx_date, a in sorted(qualifying.items()):
            mcap, category, benchmark = get_point_in_time_metadata(ticker, tx_date)
            avg_price = a['value'] / a['shares'] if a['shares'] else 0.0
            rows.append((tx_date, ticker, sector, insider, title, a['shares'], a['owned'], a['value'],
                         "Purchase", float(mcap) if mcap else None, category, benchmark, is_10b51,
                         acc, avg_price))

    if not rows and not is_amendment:
        return done('below_threshold')

    try:
        replaced = 0
        if is_amendment:
            # delete + insert happen in ONE transaction, so a failure can't leave the row deleted
            for tx_date in purchases:
                cursor.execute(DELETE_AMENDED_QUERY, (ticker, insider, tx_date, acc))
                replaced += cursor.rowcount
        inserted = 0
        for row in rows:
            cursor.execute(INSERT_QUERY, row)
            inserted += cursor.rowcount
        mark_processed(cursor, acc, form_type)
        conn.commit()
    except Exception as db_err:
        conn.rollback()
        stats['db_errors'] += 1
        print(f"*** DATABASE ERROR on {ticker} ({acc}): {db_err}")
        return

    seen.add(acc)
    stats['inserted'] += inserted
    stats['replaced'] += replaced
    stats['examined'] += 1
    for row in rows:
        print(f"*** {form_type} {ticker}: {insider} bought ${row[7]:,.0f} on {row[0]} ({acc})")
    if is_amendment and replaced:
        print(f"    amendment replaced {replaced} earlier row(s)")


def main():
    args = sys.argv[1:]
    mode = args[0].lower() if args else 'live'
    now_et = datetime.now(EASTERN)
    print(f"[{now_et:%Y-%m-%d %H:%M:%S} ET] ETL starting, mode={mode}")

    conn = get_db_connection()
    cursor = conn.cursor()
    ensure_tables(cursor, conn)
    seen = get_seen_accessions(cursor)

    if mode == 'daily':
        day = (datetime.strptime(args[1], '%Y-%m-%d').date() if len(args) > 1
               else (now_et - timedelta(days=1)).date())
        print(f"Daily sweep for {day}")
        entries = [e for e in fetch_daily_entries(day) if e['accession_number'] not in seen]
    elif mode == 'live':
        entries = fetch_live_entries(seen)
    else:
        sys.exit("Usage: python etl.py [live | daily [YYYY-MM-DD]]")

    entries.sort(key=lambda e: e['form_type'] == '4/A')  # originals before amendments
    print(f"{len(entries)} new filings to examine.")

    stats = {k: 0 for k in ('examined', 'inserted', 'replaced', 'retry', 'no_xml', 'bad_xml', 'no_ticker',
                            'no_purchase', 'below_threshold', 'db_errors', 'errors')}
    for entry in entries:
        try:
            process_filing(entry, seen, cursor, conn, stats)
        except Exception as ex:
            conn.rollback()
            stats['errors'] += 1
            print(f"  error on {entry['accession_number']}: {type(ex).__name__}: {ex}")

    cursor.close()
    conn.close()
    print(f"--- Done. {stats} ---")
    if stats['db_errors']:
        sys.exit(1)  # make the GitHub run turn red instead of silently 'succeeding'


if __name__ == "__main__":
    main()
