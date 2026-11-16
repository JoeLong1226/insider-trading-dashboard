import os
import time
import re
from datetime import datetime, timedelta
import pandas as pd
import psycopg2
import requests
import xml.etree.ElementTree as ET
import yfinance as yf

# Use a valid contact email format required by SEC EDGAR fair-access policy
HEADERS = {'User-Agent': 'InsiderTradingApp JosephLong1226@gmail.com'}
SUPABASE_URL = os.environ.get("SUPABASE_DB_URL")

def get_db_connection():
    if not SUPABASE_URL:
        raise ValueError("Missing database connection string. Set SUPABASE_DB_URL.")
    return psycopg2.connect(SUPABASE_URL)

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
    if not ticker: return ""
    clean = str(ticker).strip().upper()
    clean = clean.replace('"', '').replace("'", "").replace('(', '').replace(')', '').replace('[', '').replace(']', '')
    if ":" in clean: clean = clean.split(":")[-1].strip()
    clean = re.sub(r'[\s]', '', clean)
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

def fetch_quarterly_index(year, quarter):
    url = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx"
    response = requests.get(url, headers=HEADERS, timeout=30)
    if response.status_code != 200:
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
            if len(parts) == 5 and parts[2].strip() in ('4', '4/A'):
                form4_entries.append({
                    'cik': parts[0],
                    'company': parts[1],
                    'form_type': parts[2].strip(),
                    'date_filed': parts[3],
                    'filename': parts[4].strip()
                })
    return form4_entries

def fetch_efts_search_api(target_date_str):
    url = f"https://efts.sec.gov/LATEST/search-index?q=*&forms=4,4/A&dateRange=custom&startdt={target_date_str}&enddt={target_date_str}"
    response = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            if response.status_code == 200:
                break
        except requests.exceptions.RequestException as e:
            print(f"EFTS Search API attempt {attempt + 1} failed ({e}); retrying...")
            time.sleep(2)

    if not response or response.status_code != 200:
        print("Could not reach EFTS Search API after 3 attempts.")
        return []

    data = response.json()
    hits = data.get('hits', {}).get('hits', [])
    form4_entries = []

    for hit in hits:
        src = hit.get('_source', {})
        form_type = src.get('form', '')
        if form_type not in ('4', '4/A'):
            continue

        file_date = src.get('file_date', '')
        if file_date != target_date_str:
            continue

        _id = hit.get('_id', '')
        if ':' in _id:
            filename = _id.split(':', 1)[1]
        else:
            continue

        ciks = src.get('ciks', [])
        cik = str(ciks[0]).zfill(10) if ciks else ''

        display_names = src.get('display_names', [])
        company = display_names[0] if display_names else ''

        form4_entries.append({
            'cik': cik,
            'company': company,
            'form_type': form_type,
            'date_filed': file_date,
            'filename': filename
        })

    return form4_entries

def run_daily_etl(target_date_str=None):
    if not target_date_str:
        target_date_str = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')

    today_str = datetime.today().strftime('%Y-%m-%d')
    if target_date_str == today_str:
        print(f"Fetching live SEC EFTS Search API for today ({target_date_str})...")
        filtered_entries = fetch_efts_search_api(target_date_str)
    else:
        target_dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        qtr = (target_dt.month - 1) // 3 + 1
        print(f"Fetching QTR {qtr} index for {target_dt.year} (Target Date: {target_date_str})...")
        all_entries = fetch_quarterly_index(target_dt.year, qtr)
        filtered_entries = [e for e in all_entries if e['date_filed'] == target_date_str]

    print(f"Found {len(filtered_entries)} Form 4 filings filed on {target_date_str}.")

    conn = get_db_connection()
    cursor = conn.cursor()

    delete_amended_query = """
        DELETE FROM insider_trades
        WHERE ticker = %s
          AND insider_name = %s
          AND transaction_date = %s
          AND accession_number IS DISTINCT FROM %s;
    """

    insert_query = """
        INSERT INTO insider_trades (
            transaction_date, ticker, sector, insider_name, insider_title, 
            shares_bought, shares_owned_following, est_value, activity_type,
            market_cap, market_cap_category, benchmark_ticker, is_10b51, accession_number, price_at_tx
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;
    """

    inserted_count = 0
    for entry in filtered_entries:
        txt_url = f"https://www.sec.gov/Archives/{entry['filename']}"
        time.sleep(0.12)
         
        try:
            res = requests.get(txt_url, headers=HEADERS, timeout=15)
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
                cleaned_found = clean_ticker_for_yf(found)
                if cleaned_found and re.match(r'^[A-Z\-\.]{1,6}$', cleaned_found):
                    ticker = cleaned_found
                    break
             
            if not ticker or ticker in ['NONE', '[NONE]', 'UNKNOWN', 'N/A'] or (len(ticker) == 5 and ticker.endswith('X')):
                continue

            company_name = entry.get('company', '')
            sector = get_sector_for_ticker(ticker, company_name)
             
            owners = root.findall(".//reportingOwner")
            if not owners:
                owners = [root]

            accession_number = entry['filename'].split('/')[-1].replace('.txt', '')

            for owner in owners:
                insider = owner.findtext(".//rptOwnerName") or "UNKNOWN"
                title = owner.findtext(".//officerTitle") or "Director/Other"
                 
                for tx in root.findall(".//nonDerivativeTransaction"):
                    code = tx.findtext(".//transactionCoding/transactionCode")
                    acquired = tx.findtext(".//transactionAmounts/transactionAcquiredDisposedCode/value")
                     
                    if code == 'P' and acquired == 'A':
                        tx_date = tx.findtext(".//transactionDate/value")
                        if not tx_date:
                            continue
                             
                        shares_str = tx.findtext(".//transactionShares/value")
                        shares = float(shares_str) if shares_str else 0.0
                        
                        price_str = tx.findtext(".//transactionPricePerShare/value")
                        price = float(price_str) if price_str else 0.0
                        
                        est_val = shares * price
                        if est_val < 50000:
                            continue
                             
                        owned_str = tx.findtext(".//sharesOwnedFollowingTransaction/value")
                        owned = float(owned_str) if owned_str else 0.0

                        remark_text = (root.findtext(".//remarks") or "").upper()
                        is_10b51 = "10B5-1" in remark_text or "10B51" in remark_text
                         
                        mcap, category, benchmark = get_point_in_time_metadata(ticker, tx_date)
                         
                        try:
                            if entry.get('form_type') == '4/A':
                                cursor.execute(delete_amended_query, (
                                    ticker, insider, tx_date, accession_number
                                ))

                            cursor.execute(insert_query, (
                                tx_date, ticker, sector, insider, title, shares, owned, est_val, "Purchase",
                                float(mcap) if mcap else None, category, benchmark, is_10b51, accession_number, price
                            ))
                            conn.commit()
                            inserted_count += 1
                        except Exception as db_err:
                            conn.rollback()
        except Exception:
            continue

    cursor.close()
    conn.close()
    print(f"--- Daily ETL completed for {target_date_str}. Inserted {inserted_count} new trades. ---")

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1].strip():
        run_daily_etl(sys.argv[1].strip())
    else:
        yesterday = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
        today = datetime.today().strftime('%Y-%m-%d')
        run_daily_etl(yesterday)
        run_daily_etl(today)
