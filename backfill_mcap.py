from datetime import datetime, timedelta
import os
import re
import time
import xml.etree.ElementTree as ET
import pandas as pd
import psycopg2
import requests
import yfinance as yf

HEADERS = {"User-Agent": "InsiderTradingApp admin@example.com"}
SUPABASE_URL = os.environ.get("SUPABASE_DB_URL")


def get_db_connection():
    if not SUPABASE_URL:
        raise ValueError(
            "Missing database connection string. Set SUPABASE_DB_URL."
        )
    return psycopg2.connect(SUPABASE_URL)


# --- SECTOR & INDUSTRY MAPPING ---
sector_cache = {
    "AON": "Financial Services",
    "AAPL": "Technology",
    "MSFT": "Technology",
    "AMZN": "Consumer Cyclical",
    "GOOGL": "Communication Services",
}


def infer_sector_from_name(company_name):
    name = company_name.upper()
    if any(
        k in name
        for k in [
            "TOURISM",
            "ENTERTAINMENT",
            "HOTEL",
            "RESORT",
            "RESTAURANT",
            "TRAVEL",
        ]
    ):
        return "Consumer Cyclical"
    if any(
        k in name
        for k in ["ENERGY", "OIL", "GAS", "POWER", "ELECTRIC", "UTILITY"]
    ):
        return "Utilities"
    if any(
        k in name
        for k in [
            "PHARMA",
            "HEALTH",
            "BIOTECH",
            "MEDICAL",
            "THERAPEUTIC",
            "SURGICAL",
            "SPINE",
            "DEVICE",
            "CLINIC",
            "BIOSCIENCES",
        ]
    ):
        return "Healthcare"
    if any(
        k in name
        for k in ["BANK", "FINANCIAL", "CAPITAL", "INVESTMENT", "FUND", "BANCORP"]
    ):
        return "Financial Services"
    if any(
        k in name
        for k in [
            "TECH",
            "SOFTWARE",
            "SYSTEMS",
            "SOLUTIONS",
            "SEMICONDUCTOR",
            "NETWORKS",
        ]
    ):
        return "Technology"
    if any(
        k in name for k in ["GOLD", "MINING", "RESOURCES", "METALS", "STEEL"]
    ):
        return "Basic Materials"
    return "General Equity"


def get_sector_for_ticker(ticker, company_name=""):
    if not ticker or ticker in ("NONE", "UNKNOWN", "N/A", "NULL"):
        return infer_sector_from_name(company_name)

    clean_ticker = clean_ticker_for_yf(ticker)
    if clean_ticker in sector_cache:
        return sector_cache[clean_ticker]

    try:
        t = yf.Ticker(clean_ticker)
        yf_sector = t.info.get("sector")
        if yf_sector and yf_sector.strip():
            sector_cache[clean_ticker] = yf_sector
            return yf_sector
    except Exception:
        pass

    fallback_sector = infer_sector_from_name(company_name)
    sector_cache[clean_ticker] = fallback_sector
    return fallback_sector


# --- MARKET CAP & CLEANING LOGIC ---
TICKER_ALIASES = {
    "LBTY": "LBTYA",
    "GOOG": "GOOGL",
    "BRK.B": "BRK-B",
}


def clean_ticker_for_yf(ticker):
    if not ticker:
        return ""
    # Strip quotes, parentheses, brackets, spaces, and exchange prefixes
    clean = str(ticker).strip().upper()
    clean = (
        clean.replace('"', "")
        .replace("'", "")
        .replace("(", "")
        .replace(")", "")
        .replace("[", "")
        .replace("]", "")
    )
    if ":" in clean:
        clean = clean.split(":")[-1].strip()
    clean = re.sub(r"[\s]", "", clean)
    if "," in clean:
        clean = clean.split(",")[0].strip()
    elif "/" in clean:
        clean = clean.split("/")[0].strip()
    return TICKER_ALIASES.get(clean, clean)


def assign_market_cap_tier(market_cap_val):
    if not market_cap_val or market_cap_val <= 0:
        return "Unknown", None
    if market_cap_val < 2_000_000_000:
        return "Micro/Small", "IWM"
    elif 2_000_000_000 <= market_cap_val <= 10_000_000_000:
        return "Mid", "IJH"
    else:
        return "Large", "SPY"


# In-memory run cache
mcap_cache = {}


def get_point_in_time_metadata(ticker_symbol, tx_date_str):
    cleaned_ticker = clean_ticker_for_yf(ticker_symbol)

    # Reject invalid/non-alphabetic ticker symbols immediately (e.g. pure numbers like '1314152')
    if not cleaned_ticker or not cleaned_ticker.isalpha() or len(cleaned_ticker) > 5:
        return None, "Unknown", None

    cache_key = f"{cleaned_ticker}_{tx_date_str[:7]}"
    if cache_key in mcap_cache:
        return mcap_cache[cache_key]

    try:
        tx_date = datetime.strptime(
            tx_date_str.strip()[:10], "%Y-%m-%d"
        ).date()
        start_date = tx_date - timedelta(days=5)
        end_date = tx_date + timedelta(days=5)

        t = yf.Ticker(cleaned_ticker)

        # 1. Get historical close price
        hist = t.history(start=str(start_date), end=str(end_date))
        if hist.empty or "Close" not in hist.columns:
            mcap_cache[cache_key] = (None, "Unknown", None)
            return None, "Unknown", None

        hist.index = hist.index.date
        if tx_date in hist.index:
            price = float(hist.loc[tx_date, "Close"])
        else:
            hist["diff"] = abs(
                pd.to_datetime(hist.index) - pd.to_datetime(tx_date)
            )
            price = float(hist.loc[hist["diff"].idxmin(), "Close"])

        # 2. Get share count (fast check via fast_info first to save time)
        shares = None
        try:
            shares = float(t.fast_info.shares) if t.fast_info.shares else None
        except Exception:
            pass

        # 3. Fallback to historical shares series if fast_info is unavailable
        if not shares or shares <= 0:
            try:
                shares_df = t.get_shares_full(
                    start=str(tx_date - timedelta(days=180)), end=str(tx_date)
                )
                if shares_df is not None and not shares_df.empty:
                    shares = float(shares_df.iloc[-1])
            except Exception:
                pass

        if not shares or shares <= 0:
            mcap_cache[cache_key] = (None, "Unknown", None)
            return None, "Unknown", None

        historical_mcap = price * shares
        category, benchmark = assign_market_cap_tier(historical_mcap)

        result = (historical_mcap, category, benchmark)
        mcap_cache[cache_key] = result
        return result
    except Exception:
        mcap_cache[cache_key] = (None, "Unknown", None)
        return None, "Unknown", None


def fetch_quarterly_index(year, quarter):
    url = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx"
    response = requests.get(url, headers=HEADERS, timeout=15)
    if response.status_code != 200:
        print(
            f"Failed to fetch index for {year} Q{quarter}: Status {response.status_code}"
        )
        return []

    lines = response.text.split("\n")
    form4_entries = []
    data_started = False
    for line in lines:
        if "CIK|Company Name|Form Type|Date Filed|Filename" in line:
            data_started = True
            continue
        if data_started and line.strip():
            parts = line.split("|")
            if len(parts) == 5 and parts[2] == "4":
                form4_entries.append({
                    "cik": parts[0],
                    "company": parts[1],
                    "date_filed": parts[3],
                    "filename": parts[4].strip(),
                })
    return form4_entries


def parse_and_insert_range(start_date_str, end_date_str, min_value=50000):
    start_dt
