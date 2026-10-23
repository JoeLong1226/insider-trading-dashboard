import os
import pandas as pd
from edgar import get_filings, set_identity
import psycopg2

# Set required SEC EDGAR identity
set_identity("JosephLong1226@gmail.com")

def run_market_wide_etl():
    print("Fetching recent Form 4 filings market-wide...")
    
    # Fetch recent Form 4 filings
    filings = get_filings(form="4")
    recent_filings = filings.head(100)
    
    parsed_transactions = []

    for filing in recent_filings:
        try:
            form4 = filing.obj()
            if form4 is None:
                continue
                
            # Extract transactions dataframe from the Form 4 object
            df_tx = form4.to_dataframe()
            if df_tx is None or df_tx.empty:
                continue

            # Check for open market purchase code 'P'
            # (edgartools dataframes include 'AcquiredDisposed', 'Code', 'Shares', 'Price', 'TransactionDate')
            p_buys = df_tx[df_tx['Code'].str.upper() == 'P'] if 'Code' in df_tx.columns else pd.DataFrame()

            if not p_buys.empty:
                insider_name = getattr(form4, 'reporting_owner_name', 'Insider Trader')
                issuer = getattr(form4, 'issuer', filing.company)

                for _, tx in p_buys.iterrows():
                    shares = float(tx.get('Shares', 0) or 0)
                    price = float(tx.get('Price', 0) or 0)
                    est_val = shares * price if price > 0 else float(tx.get('Value', 0) or 0)

                    # Filter for trades over $50k (or lower if testing)
                    if est_val >= 50000:
                        tx_date = str(tx.get('TransactionDate') or filing.filing_date)
                        parsed_transactions.append({
                            "transaction_date": tx_date,
                            "ticker": str(issuer),
                            "sector": "Unclassified",
                            "insider_name": str(insider_name),
                            "shares_bought": int(shares),
                            "est_value": float(est_val),
                            "activity_type": "Purchase",
                            "ai_summary": f"{insider_name} acquired +{int(shares):,} shares of {issuer} via open-market purchase worth ${est_val:,.0f}."
                        })
        except Exception:
            continue

    df_new = pd.DataFrame(parsed_transactions)
    
    if df_new.empty:
        print("No new high-conviction open market purchases over $50k found in this batch.")
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
