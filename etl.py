import os
import time
import re
from datetime import datetime, timedelta
import pandas as pd
import psycopg2
import requests
import xml.etree.ElementTree as ET
import yfinance as yf

# SEC requires a descriptive User-Agent with contact email
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

def fetch_live_sec_rss():
    """Fetches real-time Form 4 filings directly from SEC EDGAR Atom feed with retry logic."""
    url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&output=atom"
    response = None
    
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=45)
            if response.status_code == 200:
                break
            print(f"SEC RSS attempt {attempt + 1} returned status code {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"SEC RSS attempt {attempt + 1} failed ({e}); retrying...")
            time.sleep(3)

    if not response or response.status_code != 200:
        print("Failed to fetch SEC RSS feed after 3 attempts.")
        return []
    
    try:
        root = ET.fromstring(response.content)
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        entries = []

        for entry in root.findall('atom:entry', ns):
            title = entry.findtext('atom:title', default='', namespaces=ns)
            updated = entry.findtext('atom:updated', default='', namespaces=ns)
            link_el = entry.find('atom:link', namespaces=ns)
            href = link_el.get('href') if link_el is not None else ''
            
            if not href or 'edgar/data/' not in href:
                continue

            parts = href.split('/')
            if len(parts) >= 7:
                cik = parts[4]
                accession_dash = parts[5]
                filename = f"edgar/data/{cik}/{accession_dash}.txt"
                
                entries.append({
                    'cik': cik,
                    'company': title.split(' - ')[0] if ' - ' in title else 'UNKNOWN',
                    'form_type': '4',
                    'date_filed': updated[:10],
                    'filename': filename,
                    'accession_number': accession_dash
                })
        return entries
    except Exception as e:
        print(f"Error parsing SEC RSS XML content: {e}")
        return []

def fetch_efts_search_api(target_date_str):
    """Exhaustive search API for audit/nightly reconciliation sweeps."""
    url = f"https://efts.sec.gov/LATEST/search-index?q=*&forms=4,4/A&dateRange=custom&startdt={target_date_str}&enddt={target_date_str}"
    response = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            if response.status_code == 200:
                break
        except requests.exceptions.RequestException:
            time.sleep(2)

    if not response or response.status_code != 200:
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
        accession_dash = filename.split('/')[-1].replace('.txt', '')

        form4_entries.append({
            'cik': cik,
            'company': company,
            'form_type': form_type,
            'date_filed': file_date,
            'filename': filename,
            'accession_number': accession_dash
        })

    return form4_entries

def get_existing_accessions(cursor):
    cursor.execute("SELECT accession_number FROM insider_trades WHERE accession_number IS NOT NULL;")
    return {row[0] for row in cursor.fetchall()}

def process_entries(entries, existing_accessions, cursor, conn):
    insert_query = """
        INSERT INTO insider_trades (
            transaction_date, ticker, sector, insider_name, insider_title, 
            shares_bought, shares_owned_following, est_value, activity_type,
            market_cap, market_cap_category, benchmark_ticker, is_10b51, accession_number, price_at_tx
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (accession_number) DO NOTHING;
    """

    delete_amended_query = """
        DELETE FROM insider_trades
        WHERE ticker = %s
          AND insider_name = %s
          AND transaction_date = %s
          AND accession_number IS DISTINCT FROM %s;
    """

    inserted_count = 0
    for entry in entries:
        acc_num = entry['accession_number']
        if acc_num in existing_accessions and entry.get('form_type') != '4/A':
            continue

        txt_url = f"https://www.sec.gov/Archives/{entry['filename']}"
        time.sleep(0.12)  # Respect SEC rate limits
         
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
                                    ticker, insider, tx_date, acc_num
                                ))

                            cursor.execute(insert_query, (
                                tx_date, ticker, sector, insider, title, shares, owned, est_val, "Purchase",
                                float(mcap) if mcap else None, category, benchmark, is_10b51, acc_num, price
                            ))
                            conn.commit()
                            inserted_count += 1
                            existing_accessions.add(acc_num)
                        except Exception:
                            conn.rollback()
        except Exception:
            continue
            
    return inserted_count

def run_realtime_poll():
    print(f"[{datetime.now()}] Polling live SEC RSS feed...")
    entries = fetch_live_sec_rss()
    print(f"Fetched {len(entries)} entries from live RSS feed.")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    existing_accessions = get_existing_accessions(cursor)
    
    inserted = process_entries(entries, existing_accessions, cursor, conn)
    
    cursor.close()
    conn.close()
    print(f"--- Live Poll complete. Inserted {inserted} new trades. ---")

def run_nightly_audit(target_date_str):
    print(f"[{datetime.now()}] Running nightly reconciliation audit for {target_date_str}...")
    entries = fetch_efts_search_api(target_date_str)
    print(f"Found {len(entries)} total filings via EFTS search index for audit date.")

    conn = get_db_connection()
    cursor = conn.cursor()
    existing_accessions = get_existing_accessions(cursor)

    inserted = process_entries(entries, existing_accessions, cursor, conn)

    cursor.close()
    conn.close()
    print(f"--- Nightly Audit complete. Reconciled and inserted {inserted} missing trades. ---")

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1].strip() == 'audit':
        run_nightly_audit(sys.argv[2].strip())
    else:
        try:
            run_realtime_poll()
        except Exception as e:
            print(f"Error in live poll execution: {e}")
