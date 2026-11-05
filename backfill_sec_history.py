import os
import time
import re
from datetime import datetime
import psycopg2
import requests
import xml.etree.ElementTree as ET
import yfinance as yf

HEADERS = {'User-Agent': 'InsiderTradingApp admin@example.com'}
SUPABASE_URL = os.environ.get("SUPABASE_DB_URL")


def get_db_connection():
    if not SUPABASE_URL:
        raise ValueError("Missing database connection string. Set SUPABASE_DB_URL.")
    return psycopg2.connect(SUPABASE_URL)


sector_cache = {
    "AON": "Financial Services",
    "AAPL": "Technology",
    "MSFT": "Technology",
    "AMZN": "Consumer Cyclical",
    "GOOGL": "Communication Services"
}

def get_sector_for_ticker(ticker):
    if not ticker or ticker in ("NONE", "UNKNOWN", "N/A", "NULL"):
        return "General Equity"
    
    clean_ticker = ticker.strip().upper()
    
    # Clean up exchange prefixes like "NYSE: VTEX" -> "VTEX"
    if ":" in clean_ticker:
        clean_ticker = clean_ticker.split(":")[-1].strip()
        
    # Strip brackets, unwanted spaces, or weird formatting
    clean_ticker = re.sub(r'[\[\]\s]', '', clean_ticker)
    
    # Handle dual-class / multi-ticker formatting like "HEI, HEI.A" -> "HEI"
    if "," in clean_ticker:
        clean_ticker = clean_ticker.split(",")[0].strip()
    elif "/" in clean_ticker:
        clean_ticker = clean_ticker.split("/")[0].strip()

    if clean_ticker in sector_cache:
        return sector_cache[clean_ticker]
    
    try:
        tk = yf.Ticker(clean_ticker)
        sector = tk.info.get("sector")
        if sector:
            sector_cache[clean_ticker] = sector
            return sector
    except Exception:
        pass
        
    sector_cache[clean_ticker] = "General Equity"
    return "General Equity"


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
    
    filtered_entries = [
        e for e in all_entries 
        if start_date_str <= e['date_filed'] <= end_date_str
    ]
    print(f"Found {len(filtered_entries)} Form 4 filings for target period. Starting ingestion...")

    conn = get_db_connection()
    cursor = conn.cursor()
    
    insert_query = """
        INSERT INTO insider_trades (transaction_date, ticker, sector, insider_name, insider_title, shares_bought, shares_owned_following, est_value, activity_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;
    """
    
    inserted_count = 0
    for i, entry in enumerate(filtered_entries):
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
            
            if ticker == "NONE":
                continue

            insider = root.findtext(".//rptOwnerName") or "UNKNOWN"
            title = root.findtext(".//officerTitle") or "Director/Other"
            sector = get_sector_for_ticker(ticker)
            
            for tx in root.findall(".//nonDerivativeTransaction"):
                code = tx.findtext(".//transactionCoding/transactionCode")
                acquired = tx.findtext(".//transactionAmounts/transactionAcquiredDisposedCode/value")
                
                if code == 'P' and acquired == 'A':
                    tx_date = tx.findtext(".//transactionDate/value") or entry['date_filed']
                    
                    # STRICT DATE BOUNDS FILTER
                    if not (start_date_str <= tx_date <= end_date_str):
                        continue
                        
                    shares = float(tx.findtext(".//transactionShares/value") or 0)
                    price = float(tx.findtext(".//transactionPricePerShare/value") or 0)
                    following = float(tx.findtext(".//sharesOwnedFollowingTransaction/value") or 0)
                    
                    est_val = shares * price
                    if est_val >= min_value:
                        cursor.execute(insert_query, (
                            tx_date, ticker, sector, insider, title,
                            shares, following, est_val, "Open Market Purchase"
                        ))
                        inserted_count += 1
            conn.commit()
        except Exception:
            continue
            
        if (i + 1) % 200 == 0:
            print(f"Processed {i + 1}/{len(filtered_entries)} filings... Inserted {inserted_count} so far.")

    print(f"-> Completed period {start_date_str} to {end_date_str}! Successfully inserted {inserted_count} transactions.")
    cursor.close()
    conn.close()


if __name__ == "__main__":
    start_date_str = os.environ.get("START_DATE", "2026-10-01")
    end_date_str = os.environ.get("END_DATE", "2026-10-06")
    
    print(f"=== Starting SEC Master Index Ingestion Pipeline ===")
    print(f"Target Range: {start_date_str} to {end_date_str}")

    parse_and_insert_range(start_date_str, end_date_str)
