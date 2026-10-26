import os
import xml.etree.ElementTree as ET
import pandas as pd
from edgar import get_filings, set_identity
import yfinance as yf
import psycopg2
import random

# Set required SEC EDGAR identity
set_identity("JosephLong1226@gmail.com")

def get_sector_for_ticker(ticker):
    """Fetch the market sector or fund type for a given ticker using yfinance with intelligent fallbacks."""
    if not ticker or ticker in ["UNKNOWN", "NONE", "NULL", "N/A"]:
        return "Unclassified"
    
    cleaned_ticker = ticker.replace(".", "-")
    
    try:
        stock = yf.Ticker(cleaned_ticker)
        info = stock.info
        
        # Check standard sector first
        sector = info.get("sector")
        if sector:
            return sector
            
        # Fallback for ETFs, Mutual Funds, and Closed-End Funds that lack corporate sectors
        quote_type = info.get("quoteType", "").upper()
        if quote_type == "ETF":
            return "Funds & ETFs"
        elif quote_type == "MUTUALFUND":
            return "Mutual Funds"
        elif ticker.endswith("X") or len(ticker) == 5:
            return "Funds & ETFs"
            
        return "Unclassified"
    except Exception:
        if ticker.endswith("X"):
            return "Funds & ETFs"
        return "Unclassified"

def generate_engaging_summary(cursor, insider, ticker, shares, est_val, tx_date):
    """Generate a dynamic, multi-factor narrative executive takeaway strictly anchored to prior history."""
    cursor.execute("""
        SELECT COUNT(*) 
        FROM insider_trades 
        WHERE insider_name = %s 
          AND ticker = %s 
          AND transaction_date < %s
    """, (insider, ticker, tx_date))
    res = cursor.fetchone()
    prior_count = res[0] if res else 0
    
    if est_val >= 5000000:
        scale_label = f"a massive, multi-million dollar block worth ${est_val:,.0f}"
    elif est_val >= 1000000:
        scale_label = f"a heavy seven-figure commitment totaling ${est_val:,.0f}"
    elif est_val >= 250000:
        scale_label = f"a substantial ${est_val:,.0f} open-market stake"
    else:
        scale_label = f"an open-market purchase valued at ${est_val:,.0f}"

    repeat_phrases = [
        f"{insider} deepens their conviction in {ticker}, acquiring another +{int(shares):,} shares ({scale_label}) in an ongoing accumulation phase.",
        f"Continuing a pattern of insider buying, {insider} steps up for another round in {ticker}, picking up +{int(shares):,} shares ({scale_label}).",
        f"{insider} adds further weight to their position in {ticker}, securing +{int(shares):,} shares ({scale_label}) in a follow-on transaction."
    ]

    fresh_phrases = [
        f"{insider} establishes a fresh position in {ticker}, picking up +{int(shares):,} shares ({scale_label}).",
        f"Marking a new stake in {ticker}, {insider} acquires +{int(shares):,} shares via {scale_label}.",
        f"{insider} initiates coverage from the inside, buying +{int(shares):,} shares of {ticker} ({scale_label})."
    ]

    if prior_count > 0:
        return random.choice(repeat_phrases)
    else:
        return random.choice(fresh_phrases)

def run_market_wide_etl():
    print("Fetching recent Form 4 filings market-wide (deep historical scan with advanced fund & ticker parsing)...")
    
    filings = get_filings(form="4")
    recent_filings = filings.head(1000)
    
    parsed_transactions = []

    conn_url = os.environ.get("SUPABASE_DB_URL")
    conn = psycopg2.connect(conn_url)
    cursor = conn.cursor()

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

    for filing in recent_filings:
        try:
            xml_content = filing.xml()
            if not xml_content:
                continue

            root = ET.fromstring(xml_content)

            # Extract Ticker with intelligent fallback to Issuer Name if ticker is NONE/missing
            issuer_ticker_elem = root.find(".//issuer/issuerTradingSymbol")
            raw_ticker = issuer_ticker_elem.text.strip().upper() if issuer_ticker_elem is not None and issuer_ticker_elem.text else ""
            
            issuer_name_elem = root.find(".//issuer/issuerName")
            issuer_name = issuer_name_elem.text.strip() if issuer_name_elem is not None and issuer_name_elem.text else "PRIVATE ENTITY"

            if raw_ticker in ["", "NONE", "NULL", "N/A", "UNKNOWN"]:
                # Use a clean truncated version of the issuer name as identifier if no public ticker exists
                ticker = issuer_name.split()[0].upper()[:10]
            else:
                ticker = raw_ticker

            rpt_owner_elem = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
            insider = rpt_owner_elem.text if rpt_owner_elem is not None and rpt_owner_elem.text else "Insider Trader"

            for tx in root.findall(".//nonDerivativeTransaction"):
                code_elem = tx.find(".//transactionCoding/transactionCode")
                if code_elem is None or code_elem.text != "P":
                    continue 

                shares_elem = tx.find(".//transactionAmounts/transactionShares/value")
                price_elem = tx.find(".//transactionAmounts/transactionPricePerShare/value")
                date_elem = tx.find(".//transactionDate/value")

                shares = float(shares_elem.text) if shares_elem is not None and shares_elem.text else 0.0
                price = float(price_elem.text) if price_elem is not None and price_elem.text else 0.0
                tx_date = date_elem.text if date_elem is not None and date_elem.text else str(filing.filing_date)

                est_val = shares * price

                if est_val >= 50000 and shares > 0:
                    sector = get_sector_for_ticker(ticker)
                    
                    ai_summary = generate_engaging_summary(cursor, insider, ticker, shares, est_val, tx_date)

                    parsed_transactions.append({
                        "transaction_date": tx_date,
                        "ticker": ticker,
                        "sector": sector,
                        "insider_name": insider,
                        "shares_bought": int(shares),
                        "est_value": float(est_val),
                        "activity_type": "Purchase",
                        "ai_summary": ai_summary
                    })

        except Exception:
            continue

    df_new = pd.DataFrame(parsed_transactions)
    
    if df_new.empty:
        print("No open-market purchases over $50k found in this batch.")
        cursor.close()
        conn.close()
        return

    print(f"Extracted {len(df_new)} high-conviction trades. Upserting into Supabase...")

    for _, row in df_new.iterrows():
        cursor.execute("""
            INSERT INTO insider_trades (transaction_date, ticker, sector, insider_name, shares_bought, est_value, activity_type, ai_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (transaction_date, ticker, insider_name, shares_bought, est_value) 
            DO UPDATE SET 
                sector = EXCLUDED.sector,
                ai_summary = EXCLUDED.ai_summary
            WHERE insider_trades.sector = 'Unclassified' 
               OR insider_trades.ai_summary LIKE '%%acquired%%via open-market purchase%%';
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
    print("ETL complete with fund sector classification and entity fallback parsing!")

if __name__ == "__main__":
    run_market_wide_etl()
