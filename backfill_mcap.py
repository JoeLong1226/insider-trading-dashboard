import os
import time
import re
from datetime import datetime, timedelta
import psycopg2
import requests
import xml.etree.ElementTree as ET
import yfinance as yf
import pandas as pd

HEADERS = {'User-Agent': 'InsiderTradingApp admin@example.com'}
SUPABASE_URL = os.environ.get("SUPABASE_DB_URL")

def get_db_connection():
    if not SUPABASE_URL:
        raise ValueError("Missing database connection string. Set SUPABASE_DB_URL.")
    return psycopg2.connect(SUPABASE_URL)

TICKER_ALIASES = {
    "LBTY": "LBTYA",
    "GOOG": "GOOGL",
    "BRK.B": "BRK-B",
}

def clean_ticker_for_yf(ticker):
    if not ticker: return ticker
    clean = ticker.strip().upper()
    if ":" in clean: clean = clean.split(":")[-1].strip()
    clean = re.sub(r'[\[\]\s]', '', clean)
    if "," in clean: clean = clean.split(",")[0].strip()
    elif "/" in clean: clean = clean.split("/")[0].strip()
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

def get_point_in_time_metadata(ticker_symbol, tx_date_str):
    cleaned_ticker = clean_ticker_for_yf(ticker_symbol)
    try:
        tx_date = datetime.strptime(tx_date_str.strip(), '%Y-%m-%d').date()
        start_date = tx_date - timedelta(days=7)
        end_date = tx_date + timedelta(days=3)

        t = yf.Ticker(cleaned_ticker)
        hist = t.history(start=str(start_date), end=str(end_date))
        
        if hist.empty or 'Close' not in hist.columns:
            return None, 'Unknown', None
            
        hist.index = hist.index.date
        if tx_date in hist.index:
            price = float(hist.loc[tx_date, 'Close'])
        else:
            hist['diff'] = abs(pd.to_datetime(hist.index) - pd.to_datetime(tx_date))
            price = float(hist.loc[hist['diff'].idxmin(), 'Close'])

        shares = None
        try:
            bs = t.quarterly_balance_sheet
            if bs is not None and not bs.empty:
                share_rows = [r for r in bs.index if 'Share' in str(r) or 'Ordinary' in str(r)]
                if share_rows:
                    valid_cols = [(c, pd.to_datetime(c).date()) for c in bs.columns if pd.to_datetime(c).date() <= tx_date]
                    if valid_cols:
                        valid_cols.sort(key=lambda x: x[1], reverse=True)
                        shares = float(bs.loc[share_rows[0], valid_cols[0][0]])
        except Exception:
            pass

        if not shares or shares <= 0:
            try:
                shares = float(t.fast_info.shares) if t.fast_info.shares else None
            except Exception:
                shares = None

        if not shares or shares <= 0:
            return None, 'Unknown', None

        historical_mcap = price * shares
        category, benchmark = assign_market_cap_tier(historical_mcap)
        return historical_mcap, category, benchmark
    except Exception:
        return None, 'Unknown', None

def fetch_quarterly_index(year, quarter):
    url = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx"
    response = requests.get(url, headers=HEADERS, timeout=15)
    if response.status_code != 200:
        print(f"Failed to fetch index for {year} Q{quarter}: Status {response.status_code}")
        return []

    lines = response.text.split('\n')
    form4_entries = []
    data_started = False
    for line in lines:
        if "CIK|Company Name|Form Type|Date Filed|Filename" in line:
            data_started = True
            continue
        if data_started and line.strip():
            parts = line.split('|')
            if len(parts) == 5 and parts[2] == '4':
                form4_entries.append({
                    'cik': parts[0],
                    'company': parts[1],
                    'date_filed': parts[3],
                    'filename': parts[4].strip()
                })
    return form4_entries

def parse_and_insert_range(start_date_str, end_date_str, min_value=50000):
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
    
    qtr = (start_dt.month - 1) // 3 + 1
    print(f"--- Fetching QTR {qtr} index for {start_dt.year} ({start_date_str} to {end_date_str}) ---")
    
    all_entries = fetch_quarterly_index(start_dt.year, qtr)
    filtered_entries = [e for e in all_entries if start_date_str <= e['date_filed'] <= end_date_str]
    print(f"Found {len(filtered_entries)} Form 4 filings for target period. Starting ingestion...")

    conn = get_db_connection()
    cursor = conn.cursor()

    for stmt in [
        "ALTER TABLE insider_trades ALTER COLUMN market_cap TYPE NUMERIC;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS market_cap NUMERIC;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS market_cap_category TEXT;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS benchmark_ticker TEXT;"
    ]:
        try: cursor.execute(stmt); conn.commit()
        except Exception: conn.rollback()
    
    insert_query = """
        INSERT INTO insider_trades (
            transaction_date, ticker, sector, insider_name, insider_title, 
            shares_bought, shares_owned_following, est_value, activity_type,
            market_cap, market_cap_category, benchmark_ticker
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    
    inserted_count = 0
    skipped_count = 0

    for entry in filtered_entries:
        txt_url = f"https://www.sec.gov/Archives/{entry['filename']}"
        time.sleep(0.12)
        
        try:
            res = requests.get(txt_url, headers=HEADERS, timeout=10)
            if res.status_code != 200 or "<xml>" not in res.text.lower():
                continue
                
            xml_start = res.text.find("<XML>")
            xml_end = res.text.find("</XML>")
            if xml_start == -1 or xml_end == -1:
                continue
                
            xml_content = res.text[xml_start+5:xml_end].strip()
            root = ET.fromstring(xml_content)
            
            ticker = "NONE"
            for tag in [".//issuerTradingSymbol", ".//tradingSymbol", ".//symbol"]:
                found = root.findtext(tag)
                if found and found.strip() and found.strip().upper() not in ["NONE", "N/A", "NULL", ""]:
                    ticker = found.strip().upper()
                    break
            
            if not ticker or ticker in ['NONE', '[NONE]', 'UNKNOWN', 'N/A'] or (len(ticker) == 5 and ticker.endswith('X')):
                skipped_count += 1
                continue
            
            insider = root.findtext(".//rptOwnerName") or "UNKNOWN"
            title = root.findtext(".//officerTitle") or "Director/Other"
            
            for tx in root.findall(".//nonDerivativeTransaction"):
                code = tx.findtext(".//transactionCoding/transactionCode")
                acquired = tx.findtext(".//transactionAmounts/transactionAcquiredDisposedCode/value")
                
                if code == 'P' and acquired == 'A':
                    tx_date = tx.findtext(".//transactionDate/value")
                    if not tx_date: continue
                        
                    shares_str = tx.findtext(".//transactionShares/value")
                    shares = float(shares_str) if shares_str else 0.0
                    
                    price_str = tx.findtext(".//transactionPricePerShare/value")
                    price = float(price_str) if price_str else 0.0
                    
                    est_val = shares * price
                    if est_val < min_value: continue
                        
                    owned_str = tx.findtext(".//sharesOwnedFollowingTransaction/value")
                    owned = float(owned_str) if owned_str else 0.0
                    
                    mcap, category, benchmark = get_point_in_time_metadata(ticker, tx_date)
                    
                    try:
                        cursor.execute(insert_query, (
                            tx_date, ticker, "General Equity", insider, title, shares, owned, est_val, "BUY",
                            float(mcap) if mcap else None, category, benchmark
                        ))
                        conn.commit()
                        inserted_count += 1
                    except Exception as db_err:
                        conn.rollback()
                        print(f"DB Insert Error: {db_err}")
        except Exception:
            continue

    cursor.close()
    conn.close()
    print(f"--- Finished backfill. Inserted {inserted_count} valid equity records (Skipped {skipped_count} non-equity/missing tickers). ---")

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        parse_and_insert_range(sys.argv[1], sys.argv[2])
    else:
        parse_and_insert_range("2026-05-01", "2026-05-31")
