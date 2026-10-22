import os
from datetime import datetime, timedelta
from edgar import Company, set_identity
import pandas as pd
import psycopg2

DB_URL = os.environ.get("SUPABASE_DB_URL")

WATCHLIST_CONFIG = {
    "MedTech & Healthcare": ["ISRG", "DXCM", "ABT", "SYK", "MDT", "BSX", "LLY", "PFE"],
    "Tech & AI": ["NVDA", "AAPL", "INTC", "PLTR", "MSFT", "AMD", "TSLA", "AMZN", "GOOGL", "META"],
    "Finance & REITs": ["O", "JPM", "BAC", "GS"],
    "Industrials & Consumer": ["DIS", "CAT", "GE"]
}

MIN_VALUE_THRESHOLD = 25000.0

def fetch_and_sync_insider_buys():
    set_identity("Joe Long joseph.long.finance@gmail.com")
    all_buys = []
    print("🚀 Starting Automated Watchlist Scan across all sectors...")

    for sector, tickers in WATCHLIST_CONFIG.items():
        print(f"--- Sector: {sector} ---")
        for ticker in tickers:
            print(f" Scanning {ticker}...")
            try:
                company = Company(ticker)
                filings = company.get_filings(form="4").head(10)
                
                for filing in filings:
                    form4 = filing.obj()
                    if not form4: continue
                    
                    summary = form4.get_ownership_summary()
                    if not summary: continue
                    
                    if getattr(summary, 'net_change', 0) > 0:
                        insider = getattr(summary, 'insider_name', 'Unknown')
                        shares = int(summary.net_change)
                        
                        price = 0.0
                        try:
                            df_tx = form4.to_dataframe()
                            if not df_tx.empty:
                                price_col = None
                                for col in ['Price', 'Price per share', 'price']:
                                    if col in df_tx.columns:
                                        price_col = col
                                        break
                                if price_col:
                                    valid_prices = pd.to_numeric(df_tx[price_col], errors='coerce').dropna()
                                    valid_prices = valid_prices[valid_prices > 0]
                                    if not valid_prices.empty:
                                        price = float(valid_prices.iloc[0])
                        except Exception:
                            price = 0.0
                            
                        est_value = int(round(shares * price))
                        
                        if est_value > 0 and est_value < MIN_VALUE_THRESHOLD:
                            continue
                            
                        all_buys.append({
                            "ticker": ticker,
                            "sector": sector,
                            "insider_name": insider,
                            "shares_bought": shares,
                            "estimated_value": est_value,
                            "activity_type": getattr(summary, 'primary_activity', 'Purchase'),
                            "ai_summary": f"{insider} acquired +{shares:,} shares of {ticker} ({sector}) on the open market (Est. Value: ${est_value:,})."
                        })
                        print(f"  🟢 BUY DETECTED: {insider} bought +{shares:,} shares of {ticker} (${est_value:,})")
            
            except Exception as e:
                print(f"  ❌ Error scanning {ticker}: {e}")
                
    return pd.DataFrame(all_buys)

def load_to_supabase(df):
    if df.empty:
        print("No records to load.")
        return
        
    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM insider_buys WHERE estimated_value = 0 OR estimated_value IS NULL;")
    
    for _, row in df.iterrows():
        try:
            sql = """
                INSERT INTO insider_buys 
                (ticker, sector, insider_name, shares_bought, estimated_value, activity_type, ai_summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, insider_name, shares_bought) 
                DO UPDATE SET 
                    estimated_value = EXCLUDED.estimated_value,
                    ai_summary = EXCLUDED.ai_summary;
            """
            cursor.execute(sql, (
                row['ticker'], row['sector'], row['insider_name'], 
                row['shares_bought'], row['estimated_value'], 
                row['activity_type'], row['ai_summary']
            ))
        except Exception as e:
            print(f"DB Insert Error for {row['ticker']}: {e}")
            
    conn.commit()
    cursor.close()
    conn.close()
    print("✅ Successfully synced automated records to Supabase!")

if __name__ == "__main__":
    df_buys = fetch_and_sync_insider_buys()
    load_to_supabase(df_buys)
