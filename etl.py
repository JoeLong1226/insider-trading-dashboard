import osimport os
import re
import datetime
import xml.etree.ElementTree as ET
import pandas as pd
from edgar import get_filings, set_identity
import yfinance as yf
import psycopg2
import random

# Set required SEC EDGAR identity
set_identity("JosephLong1226@gmail.com")

# Explicit mapping for known CIKs (both padded and unpadded string keys handled)
FUND_CIK_MAP = {
    # Blackstone Private Real Estate Credit & Income Fund
    "2049733": "BREC",
    "0002049733": "BREC",
    # Bluerock Private Real Estate Fund
    "1551047": "BPRE",
    "0001551047": "BPRE",
    "1851212": "BPRE",
    "0001851212": "BPRE",
    "1560385": "BPRE",
    "0001560385": "BPRE",
    # Blackstone Private Credit Fund
    "1833538": "BCRED",
    "0001833538": "BCRED",
    # Blackstone Private Multi-Asset Credit & Income
    "2032432": "BMACX",
    "0002032432": "BMACX",
}

def resolve_ticker(raw_ticker, issuer_name, issuer_cik=""):
    """Resolve standard exchange tickers or map known CIK/fund entities while filtering junk strings."""
    clean_cik = str(issuer_cik).strip().lstrip("0")
    
    # 1. Primary Check: Direct CIK match
    if clean_cik in FUND_CIK_MAP:
        return FUND_CIK_MAP[clean_cik]
    if issuer_cik in FUND_CIK_MAP:
        return FUND_CIK_MAP[issuer_cik]

    # 2. Secondary Check: Name-based overrides
    name_upper = issuer_name.upper()
    if "BLUEROCK" in name_upper or "BLUROCK" in name_upper:
        return "BPRE"
    if "BLACKSTONE PRIVATE REAL ESTATE CREDIT" in name_upper:
        return "BREC"

    # 3. Standard Ticker Resolution
    if not raw_ticker or not isinstance(raw_ticker, str):
        return None

    clean = raw_ticker.strip().upper().replace("[", "").replace("]", "")

    # Drop generic uninformative filler strings and unmapped entity noise
    if clean in ["NONE", "NULL", "N/A", "UNKNOWN", "UNKNO", "BLACK", "BLACKS", "VISTA", "5C"]:
        return None

    if bool(re.match(r"^[A-Z0-9.\-]+$", clean)) and len(clean) <= 6:
        return clean

    return None

def get_sector_for_ticker(ticker):
    """Fetch sector or return Private/Interval Fund classification."""
    if ticker in ["BREC", "BPRE", "BCRED", "BMACX", "MXF"]:
        return "Private Credit & Funds"

    cleaned_ticker = ticker.replace(".", "-")
    try:
        stock = yf.Ticker(cleaned_ticker)
        info = stock.info
        sector = info.get("sector")
        if sector:
            return sector

        quote_type = info.get("quoteType", "").upper()
        if quote_type in ["ETF", "MUTUALFUND"]:
            return "Funds & ETFs"

        return "Unclassified"
    except Exception:
        return "Funds & ETFs"

def generate_engaging_summary(cursor, insider, ticker, shares, est_val, tx_date):
    """Generate dynamic narrative takeaway anchored to database history."""
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
        scale_label = f"a substantial ${est_val:,.0f} position"
    else:
        scale_label = f"an open-market transaction valued at ${est_val:,.0f}"

    repeat_phrases = [
        f"{insider} deepens conviction in {ticker}, acquiring another +{int(shares):,} shares ({scale_label}).",
        f"Continuing an accumulation pattern, {insider} adds +{int(shares):,} shares of {ticker} ({scale_label}).",
        f"{insider} expands their stake in {ticker}, securing +{int(shares):,} shares ({scale_label})."
    ]

    fresh_phrases = [
        f"{insider} establishes a fresh position in {ticker}, acquiring +{int(shares):,} shares ({scale_label}).",
        f"Marking a new stake in {ticker}, {insider} purchases +{int(shares):,} shares via {scale_label}.",
        f"{insider} initiates coverage from the inside, buying +{int(shares):,} shares of {ticker} ({scale_label})."
    ]

    return random.choice(repeat_phrases) if prior_count > 0 else random.choice(fresh_phrases)

def run_market_wide_etl():
    start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    print(f"Fetching recent Form 4 filings from {start_date} with CIK fund resolution...")

    filings = get_filings(form="4", filing_date=f"{start_date}:")
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

    for filing in filings:
        try:
            xml_content = filing.xml()
            if not xml_content:
                continue

            root = ET.fromstring(xml_content)

            issuer_ticker_elem = root.find(".//issuer/issuerTradingSymbol")
            raw_ticker = issuer_ticker_elem.text.strip().upper() if (issuer_ticker_elem is not None and issuer_ticker_elem.text) else ""

            issuer_name_elem = root.find(".//issuer/issuerName")
            issuer_name = issuer_name_elem.text.strip() if (issuer_name_elem is not None and issuer_name_elem.text) else ""

            issuer_cik_elem = root.find(".//issuer/issuerCik")
            issuer_cik = issuer_cik_elem.text.strip() if (issuer_cik_elem is not None and issuer_cik_elem.text) else ""

            ticker = resolve_ticker(raw_ticker, issuer_name, issuer_cik)
            if not ticker:
                continue

            if ticker in ["BREC", "BPRE", "BCRED", "BMACX"]:
                print(f"[FOUND FUND MATCH] Ticker: {ticker} | CIK: {issuer_cik} | Name: {issuer_name}")

            rpt_owner_elem = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
            insider = rpt_owner_elem.text if (rpt_owner_elem is not None and rpt_owner_elem.text) else "Insider Trader"

            for tx in root.findall(".//nonDerivativeTransaction"):
                code_elem = tx.find(".//transactionCoding/transactionCode")
                if code_elem is None or code_elem.text != "P":
                    continue

                shares_elem = tx.find(".//transactionAmounts/transactionShares/value")
                price_elem = tx.find(".//transactionAmounts/transactionPricePerShare/value")
                date_elem = tx.find(".//transactionDate/value")

                shares = float(shares_elem.text) if (shares_elem is not None and shares_elem.text) else 0.0
                price = float(price_elem.text) if (price_elem is not None and price_elem.text) else 0.0
                tx_date = date_elem.text if (date_elem is not None and date_elem.text) else str(filing.filing_date)

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
        print("No valid open-market purchases over $50k found in this batch.")
        cursor.close()
        conn.close()
        return

    print(f"Extracted {len(df_new)} validated trades. Upserting into Supabase...")

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
    print("ETL sync completed successfully!")

if __name__ == "__main__":
    run_market_wide_etl()
