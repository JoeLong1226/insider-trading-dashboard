import os
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

def get_sector_for_ticker(ticker):
    """Fetch the market sector or fund type for a given ticker using yfinance."""
    if not ticker or ticker in ["UNKNOWN", "UNKNO", "NONE"]:
        return "Unclassified"
        
    cleaned_ticker = ticker.replace(".", "-")
    try:
        stock = yf.Ticker(cleaned_ticker)
        info = stock.info
        sector = info.get("sector")
        if sector:
            return sector
            
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
    """Generate a dynamic narrative executive takeaway strictly anchored to prior history."""
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
        scale_label = f"a massive, multi-million dollar block worth \${est_val:,.0f}"
    elif est_val >= 1000000:
        scale_label = f"a heavy seven-figure commitment totaling \${est_val:,.0f}"
    elif est_val >= 250000:
        scale_label = f"a substantial \${est_val:,.0f} open-market stake"
    else:
        scale_label = f"an open-market purchase valued at \${est_val:,.0f}"

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
    print("Fetching recent Form 4 filings by date range to ensure complete coverage...")
    
    # Fetch filings from the last 10 days up to today using correct edgartools syntax
    start_date = (datetime.date.today() - datetime.timedelta(days=10)).strftime('%Y-%m-%d')
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
            raw_ticker = issuer_ticker_elem.text.strip().upper() if issuer_ticker_elem is not None and issuer_ticker_elem.text else ""
            
            issuer_name_elem = root.find(".//issuer/issuerName")
            issuer_name = issuer_name_elem.text.strip() if issuer_name_elem is not None and issuer_name_elem
