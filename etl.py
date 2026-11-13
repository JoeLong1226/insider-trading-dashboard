import os
import datetime
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

    name_upper = str(issuer_name).upper()
    if "BLUEROCK" in name_upper or "BLUROCK" in name_upper: return "BPRE"
    if "BLACKSTONE PRIVATE REAL ESTATE CREDIT" in name_upper: return "BREC"

    if not raw_ticker or not isinstance(raw_ticker, str):
        return None

    clean = str(raw_ticker).strip().upper().replace("[", "").replace("]", "")
    if clean in ["NONE", "NULL", "N/A", "UNKNOWN", "UNKNO", "BLACK", "BLACKS", "VISTA", "5C"]: return None
    if len(clean) <= 6: return clean
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
        if sector: return sector
        if info.get("quoteType", "").upper() in ["ETF", "MUTUALFUND"]: return "Funds & ETFs"
    except Exception:
        pass
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
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    if start_date and end_date:
        date_query = f"{start_date}:{end_date}"
        print(f"Fetching Form 4 & 4/A filings for date range: {date_query}...")
    elif start_date:
        date_query = f"{start_date}:{today_str}"
        print(f"Fetching Form 4 & 4/A filings from {start_date} to {today_str}...")
    else:
        start_date = (datetime.date.today() - datetime.timedelta(days=10)).strftime('%Y-%m-%d')
        date_query = f"{start_date}:{today_str}"
        print(f"Fetching recent Form 4 & 4/A filings from {start_date} to {today_str}...")

    filings = get_filings(form=["4", "4/A"], filing_date=date_query)
    print(f"Total filings retrieved to process: {len(filings) if filings else 0}")
     
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

    try:
        cursor.execute("ALTER TABLE insider_trades DROP CONSTRAINT IF EXISTS unique_trade;")
        cursor.execute("DROP INDEX IF EXISTS unique_trade;")
    except Exception:
        pass

    try:
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS insider_trades_unique_tx_idx 
            ON insider_trades (accession_number, transaction_date, ticker, shares_bought);
        """)
    except Exception:
        pass

    for filing in filings:
        try:
            ownership = filing.obj()
            if not ownership: continue

            accession_number = getattr(filing, 'accession_number', None) or getattr(ownership, 'accession_number', '')
            filing_date_str = str(getattr(filing, 'filing_date', datetime.date.today()))
            if not accession_number:
                accession_number = filing_date_str + "_" + str(getattr(ownership, 'cik', ''))

            issuer = getattr(ownership, 'issuer', None)
            raw_ticker = getattr(issuer, 'trading_symbol', '') if issuer else ''
            issuer_name = getattr(issuer, 'issuer_name', '') if issuer else ''
            issuer_cik = getattr(issuer, 'cik', '') if issuer else ''

            ticker = resolve_ticker(raw_ticker, issuer_name, issuer_cik)
            if not ticker: continue

            # Robust handling for reporting owners (can be single object or list/DataFrame in edgartools)
            owner_raw = getattr(ownership, 'reporting_owner', None)
            if isinstance(owner_raw, pd.DataFrame) and not owner_raw.empty:
                insider = str(owner_raw.iloc[0].get('rptOwnerName', owner_raw.iloc[0].get('name', 'Insider Trader')))
                title = str(owner_raw.iloc[0].get('officerTitle', '10% Owner'))
            elif isinstance(owner_raw, list) and len(owner_raw) > 0:
                insider = getattr(owner_raw[0], 'rpt_owner_name', getattr(owner_raw[0], 'name', 'Insider Trader'))
                title = getattr(owner_raw[0], 'officer_title', '10% Owner')
            else:
                insider = getattr(owner_raw, 'rpt_owner_name', getattr(owner_raw, 'name', 'Insider Trader')) if owner_raw else 'Insider Trader'
                title = getattr(owner_raw, 'officer_title', '10% Owner') if owner_raw else '10% Owner'

            if not title or title.lower() == 'nan':
                title = "10% Owner"

            tx_df = ownership.non_derivative_transaction_values
            if tx_df is None or tx_df.empty:
                continue

            for _, tx in tx_df.iterrows():
                tx_code = str(tx.get('transactionCode', '')).strip().upper()
                if tx_code != 'P': continue

                shares = safe_bigint(tx.get('shares', 0))
                if not shares or shares <= 0: continue

                try:
                    price = round(float(tx.get('pricePerShare', 0.0)), 2)
                except Exception:
                    price = 0.0

                shares_following = safe_bigint(tx.get('sharesOwnedFollowing', 0)) or 0
                tx_date = str(tx.get('transactionDate', filing_date_str))

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
        except Exception as e:
            print(f"Error parsing filing {getattr(filing, 'accession_number', 'Unknown')}: {e}")
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
            ON CONFLICT (accession_number, transaction_date, ticker, shares_bought) 
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
