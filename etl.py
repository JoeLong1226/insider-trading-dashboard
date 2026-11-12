import os
import re
import datetime
import xml.etree.ElementTree as ET
import pandas as pd
from edgar import get_filings, set_identity
import yfinance as yf
import psycopg2

# Set your SEC identity required for all edgartools calls
set_identity("JosephLong1226@gmail.com")

FUND_CIK_MAP = {
    "2049733": "BREC", "0002049733": "BREC",
    "1551047": "BPRE", "0001551047": "BPRE", "1851212": "BPRE", "0001851212": "BPRE", "1560385": "BPRE", "0001560385": "BPRE",
    "1833538": "BCRED", "0001833538": "BCRED",
    "2032432": "BMACX", "0002032432": "BMACX",
}

TICKER_ALIASES = {
    "LBTY": "LBTYA",
    "GOOG": "GOOGL",
    "BRK.B": "BRK-B",
}

def safe_bigint(val):
    """Safely convert and cap values to fit within PostgreSQL BIGINT limits."""
    try:
        if val is None: return None
        i = int(float(val))
        if -9_223_372_036_854_775_808 <= i <= 9_223_372_036_854_775_807:
            return i
    except Exception:
        pass
    return None

def clean_ticker_for_yf(ticker):
    if not ticker: return ticker
    clean = str(ticker).replace(".", "-").upper()
    return TICKER_ALIASES.get(clean, clean)

def resolve_ticker(raw_ticker, issuer_name, issuer_cik=""):
    clean_cik = str(issuer_cik).strip().lstrip("0")
    if clean_cik in FUND_CIK_MAP: return FUND_CIK_MAP[clean_cik]
    if issuer_cik in FUND_CIK_MAP: return FUND_CIK_MAP[issuer_cik]

    name_upper = issuer_name.upper()
    if "BLUEROCK" in name_upper or "BLUROCK" in name_upper: return "BPRE"
    if "BLACKSTONE PRIVATE REAL ESTATE CREDIT" in name_upper: return "BREC"

    # Fallback parsing for raw ticker
    if not raw_ticker or not isinstance(raw_ticker, str):
        match = re.search(r'\[\s*([A-Z0-9.\-]+)\s*\]', issuer_name)
        if match:
            raw_ticker = match.group(1)
        else:
            return None

    clean = str(raw_ticker).strip().upper().replace("[", "").replace("]", "")
    if clean in ["NONE", "NULL", "N/A", "UNKNOWN", "UNKNO", "BLACK", "BLACKS", "VISTA", "5C"]: return None
    if bool(re.match(r"^[A-Z0-9.\-]+$", clean)) and len(clean) <= 6: return clean
    return None

def infer_sector_from_name(company_name):
    if not company_name:
        return "General Equity"
    name = company_name.upper()
    if any(k in name for k in ["TOURISM", "ENTERTAINMENT", "HOTEL", "RESORT", "RESTAURANT", "TRAVEL"]):
        return "Consumer Cyclical"
    if any(k in name for k in ["ENERGY", "OIL", "GAS", "POWER", "ELECTRIC"]):
        return "Utilities"
    if any(k in name for k in ["PHARMA", "HEALTH", "BIOTECH", "MEDICAL", "THERAPEUTICS", "ONCOLOGY"]):
        return "Healthcare"
    if any(k in name for k in ["BANK", "FINANCIAL", "CAPITAL", "INVESTMENT", "FUND"]):
        return "Financial Services"
    if any(k in name for k in ["TECH", "SOFTWARE", "SYSTEMS", "SOLUTIONS"]):
        return "Technology"
    if any(k in name for k in ["GOLD", "MINING", "RESOURCES", "METALS"]):
        return "Basic Materials"
    return "General Equity"

def get_sector_for_ticker(ticker, issuer_name=""):
    if ticker in ["BREC", "BPRE", "BCRED", "BMACX", "MXF"]: 
        return "Private Credit & Funds"
    
    cleaned_ticker = clean_ticker_for_yf(ticker)
    try:
        stock = yf.Ticker(cleaned_ticker)
        info = stock.info
        sector = info.get("sector") or info.get("industry")
        if sector: 
            return sector
        
        quote_type = info.get("quoteType", "").upper()
        if quote_type in ["ETF", "MUTUALFUND"]: 
            return "Funds & ETFs"
        
        return infer_sector_from_name(issuer_name)
    except Exception:
        return infer_sector_from_name(issuer_name)

def assign_market_cap_tier(market_cap_val):
    if not market_cap_val or market_cap_val <= 0:
        return 'Micro/Small', 'IWM'
    if market_cap_val < 2_000_000_000:
        return 'Micro/Small', 'IWM'
    elif 2_000_000_000 <= market_cap_val <= 10_000_000_000:
        return 'Mid', 'IJH'
    else:
        return 'Large', 'SPY'

def get_ticker_metadata(ticker_symbol):
    if not ticker_symbol or ticker_symbol in ['NONE', '[NONE]']:
        return None, 'Micro/Small', 'IWM'
    cleaned_ticker = clean_ticker_for_yf(ticker_symbol)
    try:
        t = yf.Ticker(cleaned_ticker)
        mcap = None
        if hasattr(t, 'fast_info') and t.fast_info.market_cap:
            mcap = float(t.fast_info.market_cap)
        
        if not mcap or mcap <= 0:
            return None, 'Micro/Small', 'IWM'
            
        category, benchmark = assign_market_cap_tier(mcap)
        return mcap, category, benchmark
    except Exception:
        return None, 'Micro/Small', 'IWM'

def run_market_wide_etl(start_date=None, end_date=None):
    if start_date and end_date:
        date_query = f"{start_date}:{end_date}"
        print(f"Fetching Form 4 & 4/A filings for date range: {date_query}...")
    elif start_date:
        date_query = f"{start_date}:"
        print(f"Fetching Form 4 & 4/A filings from {start_date} onwards...")
    else:
        start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
        date_query = f"{start_date}:"
        print(f"Fetching recent Form 4 & 4/A filings from {start_date}...")

    filings = get_filings(form=["4", "4/A"], filing_date=date_query)
    parsed_transactions = []

    conn_url = os.environ.get("SUPABASE_DB_URL")
    conn = psycopg2.connect(conn_url)
    conn.autocommit = True
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
            est_value NUMERIC(15,2),
            price_at_tx NUMERIC(15,2),
            activity_type TEXT,
            accession_number TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    alter_statements = [
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS accession_number TEXT;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS price_at_tx NUMERIC(15,2);",
        "ALTER TABLE insider_trades ALTER COLUMN market_cap TYPE NUMERIC;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS market_cap NUMERIC;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS market_cap_category TEXT;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS benchmark_ticker TEXT;",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS ret_stock_after NUMERIC(8,4);",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS ret_benchmark_after NUMERIC(8,4);",
        "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS alpha_after NUMERIC(8,4);"
    ]
    
    for stmt in alter_statements:
        try:
            cursor.execute(stmt)
        except Exception:
            pass

    # Ensure composite unique index exists for multi-line Form 4 parsing
    try:
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS insider_trades_unique_tx_idx 
            ON insider_trades (accession_number, transaction_date, ticker, shares_bought);
        """)
    except Exception:
        pass

    for filing in filings:
        try:
            xml_content = filing.xml()
            if not xml_content: continue
            root = ET.fromstring(xml_content)

            accession_number = getattr(filing, 'accession_number', None) or root.findtext(".//accessionNumber", "")
            if not accession_number:
                accession_number = str(filing.filing_date) + "_" + root.findtext(".//issuer/issuerCik", "")

            raw_ticker = root.findtext(".//issuer/issuerTradingSymbol", "").strip().upper()
            issuer_name = root.findtext(".//issuer/issuerName", "").strip()
            issuer_cik = root.findtext(".//issuer/issuerCik", "").strip()

            ticker = resolve_ticker(raw_ticker, issuer_name, issuer_cik)
            if not ticker: continue

            insider = root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName", "Insider Trader")
            
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

                shares_raw = tx.findtext(".//transactionAmounts/transactionShares/value", "0")
                price_raw = tx.findtext(".//transactionAmounts/transactionPricePerShare/value", "0")
                shares_following_raw = tx.findtext(".//postTransactionAmounts/sharesOwnedFollowingTransaction/value", "0")

                shares = safe_bigint(shares_raw)
                if not shares or shares <= 0: continue

                try:
                    price = round(float(price_raw), 2)
                except Exception:
                    price = 0.0

                shares_following = safe_bigint(shares_following_raw) or 0
                tx_date = tx.findtext(".//transactionDate/value", str(filing.filing_date))

                est_val = round(shares * price, 2)

                if est_val >= 50000:
                    sector = get_sector_for_ticker(ticker, issuer_name)
                    mcap, mcap_category, benchmark = get_ticker_metadata(ticker)

                    parsed_transactions.append({
                        "transaction_date": tx_date, 
                        "ticker": ticker, 
                        "sector": sector,
                        "insider_name": insider, 
                        "insider_title": title,
                        "shares_bought": shares, 
                        "shares_owned_following": shares_following,
                        "price_at_tx": price,
                        "est_value": float(est_val), 
                        "market_cap": float(mcap) if mcap else None,
                        "market_cap_category": mcap_category,
                        "benchmark_ticker": benchmark,
                        "activity_type": "Purchase",
                        "accession_number": accession_number
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
            INSERT INTO insider_trades (
                transaction_date, ticker, sector, insider_name, insider_title, 
                shares_bought, shares_owned_following, price_at_tx, est_value, 
                market_cap, market_cap_category, benchmark_ticker, activity_type, accession_number
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT insider_trades_unique_tx_idx 
            DO UPDATE SET 
                shares_owned_following = EXCLUDED.shares_owned_following,
                price_at_tx = EXCLUDED.price_at_tx,
                est_value = EXCLUDED.est_value,
                market_cap = EXCLUDED.market_cap,
                market_cap_category = EXCLUDED.market_cap_category,
                benchmark_ticker = EXCLUDED.benchmark_ticker,
                sector = EXCLUDED.sector,
                insider_name = EXCLUDED.insider_name,
                insider_title = EXCLUDED.insider_title;
        """, (
            row["transaction_date"], row["ticker"], row["sector"], row["insider_name"], row["insider_title"],
            row["shares_bought"], row["shares_owned_following"], row["price_at_tx"], row["est_value"],
            row["market_cap"], row["market_cap_category"], row["benchmark_ticker"], row["activity_type"], row["accession_number"]
        ))

    cursor.close()
    conn.close()
    print("ETL sync completed successfully!")

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        run_market_wide_etl(start_date=sys.argv[1], end_date=sys.argv[2])
    elif len(sys.argv) == 2:
        run_market_wide_etl(start_date=sys.argv[1])
    else:
        run_market_wide_etl()
