import os
import pandas as pd
from edgar import get_filings, set_identity
import psycopg2

# Set required SEC EDGAR identity
set_identity("JosephLong1226@gmail.com")

def run_market_wide_etl():
    print("Fetching recent Form 4 filings market-wide...")
    
    # Get recent Form 4 filings from the SEC feed
    filings = get_filings(form="4")
    recent_filings = filings.head(100)
    
    parsed_transactions = []

    for filing in recent_filings:
        try:
            form4 = filing.obj()
            if form4 is None:
                continue

            summary = form4.get_ownership_summary()
            if summary is None or not hasattr(summary, 'transactions') or not summary.transactions:
                continue

            insider = getattr(summary, 'insider_name', None) or getattr(filing, 'company', 'Insider Trader')
            ticker_symbol = getattr(summary, 'issuer', None) or getattr(filing, 'company', 'UNKNOWN')

            for t in summary.transactions:
                # Code 'P' = Open Market Purchase
                code = getattr(t, 'code', '') or ''
                if str(code).upper() == 'P':
                    shares = float(getattr(t, 'shares_numeric', 0) or 0)
                    price = float(getattr(t, 'price_numeric', 0) or 0)
                    est_val = shares * price

                    # Filter for trades $50,000+
                    if est_val >= 50000 and shares > 0:
                        tx_date = str(getattr(t, 'date', None) or getattr(filing, 'filing_date', ''))
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
        except Exception:
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
