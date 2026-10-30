import os
import time
import pandas as pd
import psycopg2
import requests
import xml.etree.ElementTree as ET

# SEC policy requires a custom User-Agent declaration
HEADERS = {'User-Agent': 'InsiderTradingApp admin@example.com'}
SUPABASE_URL = os.environ.get("SUPABASE_DB_URL")


def get_db_connection():
    return psycopg2.connect(SUPABASE_URL)


def fetch_quarterly_index(year, quarter):
    """Downloads SEC master index file for a given year & quarter."""
    url = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx"
    response = requests.get(url, headers=HEADERS)
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


def parse_and_insert_form4(entries, min_value=50000):
    """Loops through Form 4 entries, parses open-market buys, and inserts into Supabase."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    insert_query = """
        INSERT INTO insider_trades (transaction_date, ticker, sector, insider_name, insider_title, shares_bought, shares_owned_following, est_value, activity_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;
    """
    
    inserted_count = 0
    for entry in entries:
        txt_url = f"https://www.sec.gov/Archives/{entry['filename']}"
        time.sleep(0.12)  # Respect SEC rate limits
        
        try:
            res = requests.get(txt_url, headers=HEADERS)
            if res.status_code != 200 or "<xml>" not in res.text.lower():
                continue
                
            xml_start = res.text.find("<XML>")
            xml_end = res.text.find("</XML>")
            if xml_start == -1 or xml_end == -1:
                continue
                
            xml_content = res.text[xml_start+5:xml_end].strip()
            root = ET.fromstring(xml_content)
            
            ticker = root.findtext(".//issuerTradingSymbol") or "UNKNOWN"
            insider = root.findtext(".//rptOwnerName") or "UNKNOWN"
            title = root.findtext(".//officerTitle") or "Director/Other"
            
            for tx in root.findall(".//nonDerivativeTransaction"):
                code = tx.findtext(".//transactionCoding/transactionCode")
                acquired = tx.findtext(".//transactionAmounts/transactionAcquiredDisposedCode/value")
                
                if code == 'P' and acquired == 'A':
                    tx_date = tx.findtext(".//transactionDate/value") or entry['date_filed']
                    shares = float(tx.findtext(".//transactionShares/value") or 0)
                    price = float(tx.findtext(".//transactionPricePerShare/value") or 0)
                    following = float(tx.findtext(".//sharesOwnedFollowingTransaction/value") or 0)
                    
                    est_val = shares * price
                    if est_val >= min_value:
                        cursor.execute(insert_query, (
                            tx_date, ticker, "Unclassified", insider, title,
                            shares, following, est_val, "Open Market Purchase"
                        ))
                        inserted_count += 1
            conn.commit()
        except Exception:
            continue
            
    print(f"  -> Successfully inserted {inserted_count} transactions.")
    cursor.close()
    conn.close()


if __name__ == "__main__":
    years_to_backfill = [2024, 2025, 2026]
    
    for year in years_to_backfill:
        for qtr in range(1, 5):
            print(f"--- Processing {year} Quarter {qtr} ---")
            entries = fetch_quarterly_index(year, qtr)
            if entries:
                print(f"Found {len(entries)} Form 4 filings. Parsing open-market buys...")
                parse_and_insert_form4(entries)
