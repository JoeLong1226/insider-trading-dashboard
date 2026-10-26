import os
import re
import datetime
import xml.etree.ElementTree as ET
import pandas as pd
from edgar import get_filings, set_identity
import yfinance as yf
import psycopg2

set_identity("JosephLong1226@gmail.com")

FUND_CIK_MAP = {
    "2049733": "BREC", "0002049733": "BREC",
    "1551047": "BPRE", "0001551047": "BPRE", "1851212": "BPRE", "0001851212": "BPRE", "1560385": "BPRE", "0001560385": "BPRE",
    "1833538": "BCRED", "0001833538": "BCRED",
    "2032432": "BMACX", "0002032432": "BMACX",
}

def resolve_ticker(raw_ticker, issuer_name, issuer_cik=""):
    clean_cik = str(issuer_cik).strip().lstrip("0")
    if clean_cik in FUND_CIK_MAP: return FUND_CIK_MAP[clean_cik]
    if issuer_cik in FUND_CIK_MAP: return FUND_CIK_MAP[issuer_cik]

    name_upper = issuer_name.upper()
    if "BLUEROCK" in name_upper or "BLUROCK" in name_upper: return "BPRE"
    if "BLACKSTONE PRIVATE REAL ESTATE CREDIT" in name_upper: return "BREC"

    if not raw_ticker or not isinstance(raw_ticker, str): return None
    clean = raw_ticker.strip().upper().replace("[", "").replace("]", "")
    if clean in ["NONE", "NULL", "N/A", "UNKNOWN", "UNKNO", "BLACK", "BLACKS", "VISTA", "5C"]: return None
    if bool(re.match(r"^[A-Z0-9.\-]+$", clean)) and len(clean) <= 6: return clean
    return None

def get_sector_for_ticker(ticker):
    if ticker in ["BREC", "BPRE", "BCRED", "BMACX", "MXF"]: return "Private Credit & Funds"
    cleaned_ticker = ticker.replace(".", "-")
    try:
        stock = yf.Ticker(cleaned_ticker)
        info = stock.info
        sector = info.get("sector")
        if sector: return sector
        if info.get("quoteType", "").upper() in ["ETF", "MUTUALFUND"]: return "Funds & ETFs"
        return "Unclassified"
    except Exception:
        return "Funds & ETFs"

def run_market_wide_etl():
    start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    print(f"Fetching recent Form 4 filings from {start_date}...")

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
            insider_title TEXT,
            shares_bought BIGINT,
            shares_owned_following BIGINT,
            est_value NUMERIC,
            activity_type TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT unique_trade UNIQUE (transaction_date, ticker, insider_name, shares_bought, est_value)
        );
    """)

    for filing in filings:
        try:
            xml_content = filing.xml()
            if not xml_content: continue
            root = ET.fromstring(xml_content)

            raw_ticker = root.findtext(".//issuer/issuerTradingSymbol", "").strip().upper()
            issuer_name = root.findtext(".//issuer/issuerName", "").strip()
            issuer_cik = root.findtext(".//issuer/issuerCik", "").strip()

            ticker = resolve_ticker(raw_ticker, issuer_name, issuer_cik)
            if not ticker: continue

            insider = root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName", "Insider Trader")
            
            # Smart Title Parsing
            rel = root.find(".//reportingOwner/reportingOwnerRelationship")
            title = ""
            if rel is not None:
                title = rel.findtext("officerTitle", "").strip()
                if not title:
                    is_dir = rel.findtext("isDirector", "0") == "1"
                    is_off = rel.findtext("isOfficer", "0") == "1"
                    is_10 = rel.findtext("isTenPercentOwner", "0") == "1"
                    
                    roles = []
                    if is_dir: roles.append("Director")
                    if is_off: roles.append("Officer")
                    if is_10: roles.append("10% Owner")
                    title = ", ".join(roles) if roles else "Insider"
            if not title:
                title = "Insider"

            for tx in root.findall(".//nonDerivativeTransaction"):
                code_elem = tx.find(".//transactionCoding/transactionCode")
                if code_elem is None or code_elem.text != "P": continue

                shares = float(tx.findtext(".//transactionAmounts/transactionShares/value", "0"))
                price = float(tx.findtext(".//transactionAmounts/transactionPricePerShare/value", "0"))
                tx_date = tx.findtext(".//transactionDate/value", str(filing.filing_date))
                shares_following = float(tx.findtext(".//postTransactionAmounts/sharesOwnedFollowingTransaction/value", "0"))

                est_val = shares * price

                if est_val >= 50000 and shares > 0:
                    sector = get_sector_for_ticker(ticker)
                    parsed_transactions.append({
                        "transaction_date": tx_date, "ticker": ticker, "sector": sector,
                        "insider_name": insider, "insider_title": title,
                        "shares_bought": int(shares), "shares_owned_following": int(shares_following),
                        "est_value": float(est_val), "activity_type": "Purchase"
                    })
        except Exception:
            continue

    df_new = pd.DataFrame(parsed_transactions)
    if df_new.empty:
        print("No valid trades found.")
        cursor.close(); conn.close(); return

    print(f"Upserting {len(df_new)} validated trades into Supabase...")
    for _, row in df_new.iterrows():
        cursor.execute("""
            INSERT INTO insider_trades (transaction_date, ticker, sector, insider_name, insider_title, shares_bought, shares_owned_following, est_value, activity_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (transaction_date, ticker, insider_name, shares_bought, est_value) 
            DO UPDATE SET sector = EXCLUDED.sector, insider_title = EXCLUDED.insider_title;
        """, (
            row["transaction_date"], row["ticker"], row["sector"], row["insider_name"], row["insider_title"],
            row["shares_bought"], row["shares_owned_following"], row["est_value"], row["activity_type"]
        ))

    conn.commit()
    cursor.close()
    conn.close()
    print("ETL sync completed successfully!")

if __name__ == "__main__":
    run_market_wide_etl()
