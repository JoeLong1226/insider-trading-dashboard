import os
from datetime import datetime, timedelta
import yfinance as yf
import psycopg2

def get_forward_prices(ticker_symbol, tx_date_str):
    cleaned_ticker = ticker_symbol.strip().upper()
    try:
        tx_date = datetime.strptime(tx_date_str.strip()[:10], '%Y-%m-%d').date()
        start_date = tx_date - timedelta(days=5)
        end_date = tx_date + timedelta(days=260)

        t = yf.Ticker(cleaned_ticker)
        hist = t.history(start=str(start_date), end=str(end_date))
        
        if hist.empty or 'Close' not in hist.columns:
            return None, None, None

        hist.index = hist.index.date
        
        # 1. Price at Transaction
        if tx_date in hist.index:
            price_tx = float(hist.loc[tx_date, 'Close'])
        else:
            past_dates = hist[hist.index <= tx_date]
            if past_dates.empty:
                return None, None, None
            price_tx = float(past_dates.iloc[-1]['Close'])

        # 2. Price ~60 days after
        target_60 = tx_date + timedelta(days=85)
        future_60 = hist[(hist.index >= tx_date + timedelta(days=40)) & (hist.index <= target_60)]
        price_60 = float(future_60.iloc[0]['Close']) if not future_60.empty else None

        # 3. Price ~180 days after
        target_180 = tx_date + timedelta(days=260)
        future_180 = hist[(hist.index >= tx_date + timedelta(days=150)) & (hist.index <= target_180)]
        price_180 = float(future_180.iloc[0]['Close']) if not future_180.empty else None

        return price_tx, price_60, price_180
    except Exception:
        return None, None, None

def patch_missing_prices():
    conn_url = os.environ.get("SUPABASE_DB_URL")
    conn = psycopg2.connect(conn_url)
    conn.autocommit = True
    cursor = conn.cursor()

    # Fetch rows that don't have price_at_tx populated yet
    cursor.execute("""
        SELECT accession_number, ticker, transaction_date 
        FROM insider_trades 
        WHERE price_at_tx IS NULL AND accession_number IS NOT NULL;
    """)
    rows = cursor.fetchall()
    print(f"Found {len(rows)} rows to patch with forward prices.")

    for accession_number, ticker, tx_date in rows:
        price_tx, price_60, price_180 = get_forward_prices(ticker, tx_date)
        
        if price_tx is not None:
            cursor.execute("""
                UPDATE insider_trades 
                SET price_at_tx = %s, price_60d_after = %s, price_180d_after = %s
                WHERE accession_number = %s;
            """, (price_tx, price_60, price_180, accession_number))
            print(f"Patched {ticker} on {tx_date} (Accession: {accession_number})")

    cursor.close()
    conn.close()
    print("Patching complete!")

if __name__ == "__main__":
    patch_missing_prices()
