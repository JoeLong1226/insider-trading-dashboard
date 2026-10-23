import os
import xml.etree.ElementTree as ET
import pandas as pd
from edgar import get_filings, set_identity
import psycopg2

# Set required SEC EDGAR identity
set_identity("JosephLong1226@gmail.com")

def run_market_wide_etl():
    print("Fetching recent Form 4 filings market-wide (deep historical scan)...")
    
    # Fetch a larger batch of Form 4 filings from SEC EDGAR feed (1,000 filings)
    filings = get_filings(form="4")
    recent_filings = filings.head(1000)
    
    parsed_transactions = []

    for filing in recent_filings:
        try:
            # Extract raw XML content directly
            xml_content = filing.xml()
            if not xml_content:
                continue

            root = ET.fromstring(xml_content)

            # Extract Insider & Ticker Metadata
            issuer_ticker_elem = root.find(".//issuer/issuerTradingSymbol")
            ticker = issuer_ticker_elem.text.upper() if issuer_ticker_elem is not None and issuer_ticker_elem.text else "UNKNOWN"

            rpt_owner_elem = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
            insider = rpt_owner_elem.text if rpt_owner_elem is not None and rpt_owner_elem.text else "Insider Trader"

            # Parse non-derivative open-market transactions
            for tx in root.findall(".//nonDerivativeTransaction"):
                code_elem = tx.find(".//transactionCoding/transactionCode")
                if code_elem is None or code_elem.text != "P":
                    continue  # Only keep 'P' (Open Market Purchases)

                shares_elem = tx.find(".//transactionAmounts/transactionShares/value")
                price_elem = tx.find(".//transactionAmounts/transactionPricePerShare/value")
                date_elem = tx.find(".//transactionDate/value")

                shares = float(shares_elem.text) if shares_elem is not None and shares_elem.text else 0.0
                price = float(price_elem.text) if price_elem is not None and price_elem.text else 0.0
                tx_date = date_elem.text if date_elem is not None and date_elem.text else str(filing.filing_date)

                est_val = shares * price

                # Filter for transactions over $50,000
                if est_val >= 50000 and shares > 0:
                    parsed_transactions.append({
                        "transaction_date": tx_date,
                        "ticker": ticker,
                        "sector": "Unclassified",
                        "insider_name": insider,
                        "shares_bought": int(shares),
                        "est_value": float(est_val),
                        "activity_type": "Purchase",
                        "ai_summary": f"{insider} acquired +{int(shares):,} shares of {ticker} via open-market purchase worth ${est_val:,.0f}."
                    })

        except Exception:
            # Skip any malformed single filing without crashing the loop
            continue

    df_new = pd.DataFrame(parsed_transactions)
    
    if df_new.empty:
        print("No open-market purchases over $50k found in this batch.")
        return

    print(f"Extracted {len(df_new)} high-conviction trades from deep scan. Connecting to Supabase...")

    # Connect to Supabase PostgreSQL and Upsert
    conn_url = os.environ.get("SUPABASE_DB_URL")
    conn = psycopg2.connect(conn_url)
    cursor = conn.cursor()

    # Ensure table exists before executing inserts
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS insider_trades (
            id SERIAL PRIMARY KEY,
            transaction_date TEXT,
            ticker TEXT,
            sector TEXT,
            insider_name TEXT,
            shares_bought BIGINT,
            est_value NUMERIC,
            activity_type TEXT,
            ai_summary TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT unique_trade UNIQUE (transaction_date, ticker, insider_name, shares_bought, est_value)
        );
    """)

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
    print("ETL deep historical sync completed successfully!")

if __name__ == "__main__":
    run_market_wide_etl()
