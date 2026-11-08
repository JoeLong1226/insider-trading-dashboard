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
    if not ticker_symbol or ticker_symbol in ['NONE', '[NONE]', 'UNKNOWN', 'N/A']:
        return None, 'Unknown', None
    
    cleaned_ticker = clean_ticker_for_yf(ticker_symbol)
    try:
        tx_date = datetime.strptime(tx_date_str.strip(), '%Y-%m-%d').date()
        start_date = tx_date - timedelta(days=5)
        end_date = tx_date + timedelta(days=5)

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
            shares = float(t.fast_info.shares) if t.fast_info.shares else None

        if not shares or shares <= 0:
            return None, 'Unknown', None

        historical_mcap = price * shares
        category, benchmark = assign_market_cap_tier(historical_mcap)
        return historical_mcap, category, benchmark
    except Exception:
        return None, 'Unknown', None

def backfill_missing_market_caps():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Ensure columns exist in Supabase
    for stmt in [
        "ALTER TABLE insider_trades ALTER COLUMN market_cap TYPE NUMERIC;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS market_cap NUMERIC;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS market_cap_category TEXT;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS benchmark_ticker TEXT;"
    ]:
        try: cursor.execute(stmt); conn.commit()
        except Exception: pass

    # Fetch rows where market cap hasn't been calculated yet
    cursor.execute("""
        SELECT id, transaction_date, ticker 
        FROM insider_trades 
        WHERE market_cap IS NULL AND transaction_date is not null;
    """)
    rows = cursor.fetchall()
    print(f"Found {len(rows)} records needing point-in-time market cap backfill.")

    updated_count = 0
    for row in rows:
        trade_id, tx_date_str, ticker = row
        if not ticker: continue

        mcap, category, benchmark = get_point_in_time_metadata(ticker, tx_date_str)
        if mcap:
            try:
                cursor.execute("""
                    UPDATE insider_trades 
                    SET market_cap = %s, market_cap_category = %s, benchmark_ticker = %s
                    WHERE id = %s;
                """, (float(mcap), category, benchmark, trade_id))
                conn.commit()
                updated_count += 1
                print(f"Updated ID {trade_id} | {ticker} on {tx_date_str}: MCap = ${mcap:,.0f} [{category}]")
            except Exception as db_err:
                conn.rollback()
                print(f"DB Update Error for ID {trade_id}: {db_err}")
        
        time.sleep(0.1)

    cursor.close()
    conn.close()
    print(f"--- Finished Market Cap Backfill. Successfully updated {updated_count} records. ---")

if __name__ == "__main__":
    backfill_missing_market_caps()
