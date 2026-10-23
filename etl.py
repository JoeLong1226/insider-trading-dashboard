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

            # Use edgartools native ownership summary object
            summary = form4.get_ownership_summary()
            if summary is None:
                continue

            # Check if it's an open-market net purchase with a positive value
            net_change = getattr(summary, 'net_change', 0) or 0
            net_value = getattr(summary, 'net_value', 0) or 0

            if net_change > 0 and net_value >= 50000:
                insider = getattr(summary, 'insider_name', 'Insider Trader')
                issuer = getattr(summary, 'issuer', getattr(filing, 'company', 'UNKNOWN'))
                tx_date = str(getattr(filing, 'filing_date', ''))

                parsed_transactions.append({
                    "transaction_date": tx_date,
                    "ticker": str(issuer),
                    "sector": "Unclassified",
                    "insider_name": str(insider),
                    "shares_bought": int(net_change),
                    "est_value": float(net_value),
                    "activity_type": "Purchase",
                    "ai_summary": f"{insider} acquired +{int(net_change):,} shares of {issuer} via open-market purchase worth ${net_value:,.0f}."
                })
        except Exception as e:
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
