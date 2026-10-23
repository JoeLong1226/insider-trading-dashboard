import os
import pandas as pd
from edgar import get_filings, set_identity
import psycopg2

# Set required SEC EDGAR identity
set_identity("JosephLong1226@gmail.com")

def run_market_wide_etl():
    print("Fetching recent Form 4 filings market-wide...")
    
    # Fetch recent Form 4 filings from the SEC feed
    filings = get_filings(form="4")
    recent_filings = filings.head(100)
    
    parsed_transactions = []

    for filing in recent_filings:
        try:
            form4 = filing.obj()
            if form4 is None:
                continue

            # Access market trades directly via edgartools
            trades_df = getattr(form4, 'market_trades', None)
            
            if trades_df is not None and not trades_df.empty:
                # Filter for open-market purchases (Code 'P')
                if 'Code' in trades_df.columns:
                    p_buys = trades_df[trades_df['Code'].str.upper() == 'P']
                else:
                    p_buys = trades_df

                insider = getattr(form4, 'insider_name', None) or getattr(form4, 'reporting_owner_name', 'Insider Trader')
                ticker_symbol = getattr(filing, 'ticker', None) or getattr(filing, 'company', 'UNKNOWN')

                for _, tx in p_buys.iterrows():
                    shares = float(tx.get('Shares', 0) or tx.get('shares', 0) or 0)
                    price = float(tx.get('Price', 0) or tx.get('price', 0) or 0)
                    
                    # Calculate estimated value if not explicitly given
                    est_val = float(tx.get('Value', 0) or tx.get('value', 0) or (shares * price))

                    # Filter for transaction threshold ($50,000+)
                    if est_val >= 50000 and shares > 0:
                        tx_date = str(tx.get('TransactionDate') or tx.get('date') or filing.filing_date)
                        parsed_transactions.append({
                            "transaction_date": tx_date,
                            "ticker": str(ticker_symbol),
                            "sector": "Unclassified",
                            "insider_name": str(insider),
                            "shares_bought": int(shares),
                            "est_value": float(est_val),
                            "activity_type": "Purchase",
                            "ai_summary": f"{insider} acquired +{int(shares):,} shares of {ticker_symbol} via open-market purchase worth ${est_val:,.0f}."
                        })
        except Exception as e:
            # Continue on individual filing parse errors
            continue

    df_new = pd.DataFrame(parsed_transactions)
    
    if df_new.empty:
        print("No new open-market purchases over $50k found in this batch.")
        return

    print(f"Extracted {len(df_new)} high-conviction trades. Connecting to Supabase...")

    # Connect to Supabase PostgreSQL and Upsert
    conn_url = os.environ.get("SUPABASE_DB_URL")
    conn = psycopg2.connect(conn_url)
    cursor = conn.cursor()

    for _, row in df_new.iterrows():
        cursor.execute("""
            INSERT INTO insider_trades (transaction_date, ticker, sector, insider_name, shares_bought, est_value, activity_type, ai_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING;
        """, (
            row["transaction_date"],
            row["ticker"],
            row["sector"],
            row["insider_name"],
            row["shares_bought"],
            row["est_value"],
            row["activity_type"],
            row["ai_summary"]
        ))

    conn.commit()
    cursor.close()
    conn.close()
    print("ETL market-wide sync completed successfully!")

if __name__ == "__main__":
    run_market_wide_etl()
