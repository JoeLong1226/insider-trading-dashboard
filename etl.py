import os
from datetime import datetime
import pandas as pd
from edgar import get_filings, set_identity
import psycopg2

# 1. Set required SEC EDGAR identity (use your email)
set_identity("JosephLong1226@gmail.com")

def run_market_wide_etl():
    print("Fetching recent Form 4 filings market-wide...")
    
    # Get all Form 4 filings from the recent SEC feed
    filings = get_filings(form="4")
    
    # Take the most recent batch (e.g., latest 100-200 filings to stay well within GitHub limits)
    recent_filings = filings.head(150)
    
    parsed_transactions = []

    for filing in recent_filings:
        try:
            form4 = filing.obj()
            summary = form4.get_ownership_summary()
            
            # Ensure it's a purchase and has valid value/net change
            if summary and summary.net_change and summary.net_change > 0:
                # Loop through individual transactions in the filing
                for t in summary.transactions:
                    # Filter for open-market purchase code 'P'
                    if t.code == 'P' and t.value and t.value >= 50000:
                        parsed_transactions.append({
                            "transaction_date": t.date or filing.filing_date,
                            "ticker": summary.issuer,
                            "sector": "Unclassified", # Can map sectors dynamically if desired
                            "insider_name": summary.insider_name,
                            "shares_bought": t.shares_numeric,
                            "est_value": t.value,
                            "activity_type": "Purchase",
                            "ai_summary": f"{summary.insider_name} acquired +{t.shares_numeric:,} shares of {summary.issuer} via open-market purchase worth ${t.value:,.0f}."
                        })
        except Exception as e:
            # Skip malformed or unparseable filings silently to keep the job running smoothly
            continue

    df_new = pd.DataFrame(parsed_transactions)
    
    if df_new.empty:
        print("No new high-conviction purchases found in this batch.")
        return

    print(f"Extracted {len(df_new)} high-conviction trades. Connecting to Supabase...")

    # 2. Connect to Supabase PostgreSQL and Upsert
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
