from datetime import datetime, timedelta
import os
import re
import time
import pandas as pd
import psycopg2
import yfinance as yf

SUPABASE_URL = os.environ.get("SUPABASE_DB_URL")


def get_db_connection():
    if not SUPABASE_URL:
        raise ValueError("Missing database connection string. Set SUPABASE_DB_URL.")
    return psycopg2.connect(SUPABASE_URL)


def clean_ticker_for_yf(ticker):
    if not ticker: return ""
    clean = str(ticker).strip().upper()
    clean = clean.replace('"', '').replace("'", "").replace('(', '').replace(')', '').replace('[', '').replace(']', '')
    if ":" in clean: clean = clean.split(":")[-1].strip()
    clean = re.sub(r'[\s]', '', clean)
    if "," in clean: clean = clean.split(",")[0].strip()
    elif "/" in clean: clean = clean.split("/")[0].strip()
    return {"LBTY": "LBTYA", "GOOG": "GOOGL", "BRK.B": "BRK-B"}.get(clean, clean)


def fetch_prices_for_date(ticker_symbol, tx_date_str):
    cleaned_ticker = clean_ticker_for_yf(ticker_symbol)
    if not cleaned_ticker or len(cleaned_ticker) > 6 or not re.match(r'^[A-Z0-9\.\-]+$', cleaned_ticker):
        return None, None, None

    try:
        tx_date = datetime.strptime(tx_date_str.strip()[:10], '%Y-%m-%d').date()
        start_date = tx_date - timedelta(days=5)
        end_date_forward = tx_date + timedelta(days=190)

        t = yf.Ticker(cleaned_ticker)
        hist = t.history(start=str(start_date), end=str(end_date_forward))
        if hist.empty or 'Close' not in hist.columns:
            return None, None, None
             
        hist.index = hist.index.date

        # 1. Price at transaction date (fallback close proxy if needed, though Form 4 takes precedence if available)
        if tx_date in hist.index:
            price_at_tx = float(hist.loc[tx_date, 'Close'])
        else:
            hist['diff'] = abs(pd.to_datetime(hist.index) - pd.to_datetime(tx_date))
            price_at_tx = float(hist.loc[hist['diff'].idxmin(), 'Close'])

        # 2. Forward Prices (60d and 180d after)
        price_60d = None
        price_180d = None
        target_60d = tx_date + timedelta(days=60)
        target_180d = tx_date + timedelta(days=180)

        future_hist = hist[hist.index >= tx_date]
        if not future_hist.empty:
            future_60 = future_hist[future_hist.index >= target_60d]
            if not future_60.empty:
                price_60d = float(future_60.iloc[0]['Close'])

            future_180 = future_hist[future_hist.index >= target_180d]
            if not future_180.empty:
                price_180d = float(future_180.iloc[0]['Close'])

        return round(price_at_tx, 2) if price_at_tx else None, \
               round(price_60d, 2) if price_60d else None, \
               round(price_180d, 2) if price_180d else None
    except Exception:
        return None, None, None


def update_missing_prices(start_date_filter="2026-04-01", end_date_filter="2026-04-30"):
    conn = get_db_connection()
    cursor = conn.cursor()

    # AUDITOR FIX: Target all records in the window so we can reconcile/refresh them cleanly
    select_query = """
        SELECT id, ticker, transaction_date, est_value, shares_bought 
        FROM insider_trades 
        WHERE transaction_date >= %s 
          AND transaction_date <= %s
    """
    
    cursor.execute(select_query, (start_date_filter, end_date_filter))
    rows = cursor.fetchall()
    print(f"Found {len(rows)} records to evaluate/patch between {start_date_filter} and {end_date_filter}.")

    update_query = """
        UPDATE insider_trades 
        SET price_at_tx = %s, price_60d_after = %s, price_180d_after = %s
        WHERE id = %s
    """

    updated_count = 0
    for row_id, ticker, tx_date, est_value, shares_bought in rows:
        # 1. Prioritize official Form 4 execution price if available (est_value / shares)
        official_price = None
        if est_value and shares_bought and shares_bought > 0:
            official_price = round(float(est_value) / float(shares_bought), 2)

        # 2. Fetch forward prices via yfinance
        _, price_60d, price_180d = fetch_prices_for_date(ticker, str(tx_date))
        
        # Use official Form 4 price for transaction date, falling back to yfinance if needed
        final_tx_price = official_price if official_price else _

        if final_tx_price is not None:
            try:
                cursor.execute(update_query, (final_tx_price, price_60d, price_180d, row_id))
                conn.commit()
                updated_count += 1
                print(f"Patched ID {row_id} ({ticker} on {tx_date}): Tx=${final_tx_price:.2f}")
            except Exception as db_err:
                conn.rollback()
                print(f"DB Update Error for ID {row_id}: {db_err}")
        
        time.sleep(0.2) 

    cursor.close()
    conn.close()
    print(f"--- Finished price patch & reconciliation. Successfully updated {updated_count} records. ---")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        update_missing_prices(sys.argv[1], sys.argv[2])
    else:
        update_missing_prices("2026-04-01", "2026-04-30")
