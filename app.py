import streamlit as st
import pandas as pd
import psycopg2
import os
import yfinance as yf

st.set_page_config(page_title="Insider Trading Dashboard", layout="wide")

# Connect to Supabase
@st.cache_resource
def get_db_connection():
    conn_url = os.environ.get("SUPABASE_DB_URL")
    return psycopg2.connect(conn_url)

# Helper function to compute unrealized profit including dividends with ticker guards
@st.cache_data(ttl=3600)
def calculate_trade_performance(ticker, tx_date_str, shares, est_value):
    if not ticker or ticker.upper() in ["UNKNOWN", "NONE", "NULL", "N/A"] or shares <= 0:
        return 0.0, 0.0

    cleaned_ticker = ticker.replace(".", "-")
    
    try:
        stock = yf.Ticker(cleaned_ticker)
        tx_date = pd.to_datetime(tx_date_str).tz_localize(None)
        
        current_price = stock.fast_info.get('lastPrice') or stock.info.get('currentPrice')
        
        hist = stock.history(start=tx_date_str)
        if hist.empty:
            return 0.0, 0.0

        if not current_price:
            current_price = hist['Close'].iloc[-1]

        current_equity_val = shares * current_price
        capital_gain = current_equity_val - est_value
        
        dividends = stock.dividends
        if not dividends.empty:
            dividends.index = dividends.index.tz_localize(None)
            dividends_since_buy = dividends[dividends.index >= tx_date]
            total_div_per_share = dividends_since_buy.sum()
        else:
            total_div_per_share = 0.0
            
        total_dividends_collected = shares * total_div_per_share
        total_dollar_return = capital_gain + total_dividends_collected
        percent_return = (total_dollar_return / est_value) * 100 if est_value > 0 else 0.0
        
        return total_dollar_return, percent_return

    except Exception:
        return 0.0, 0.0

st.title("📊 Insider Transactions Dashboard")
st.write("Market-wide tracking of high-conviction open-market executive purchases ($50,000+).")

# Fetch data from Supabase
try:
    conn = get_db_connection()
    query = """
        SELECT transaction_date, ticker, sector, insider_name, shares_bought, est_value, activity_type 
        FROM insider_trades 
        ORDER BY transaction_date DESC;
    """
    df = pd.read_sql(query, conn)

    if not df.empty:
        # Calculate live gain/loss + dividends for each trade
        perf = [
            calculate_trade_performance(row['ticker'], row['transaction_date'], row['shares_bought'], float(row['est_value']))
            for _, row in df.iterrows()
        ]
        
        df['Unrealized Profit ($)'] = [p[0] for p in perf]
        df['Return (%)'] = [p[1] for p in perf]

        # Format column values for executive table display
        df['Unrealized Gain'] = df['Unrealized Profit ($)'].apply(lambda x: f"+${x:,.0f}" if x >= 0 else f"-${abs(x):,.0f}")
        df['Total Return'] = df['Return (%)'].apply(lambda x: f"+{x:.1f}%" if x >= 0 else f"{x:.1f}%")
        df['Shares Bought'] = df['shares_bought'].apply(lambda x: f"+{x:,.0f}")
        df['Est. Value ($)'] = df['est_value'].apply(lambda x: f"${x:,.0f}")

        # Mask unlisted private credit and fund allocations
        institutional_sectors = ['Private Credit & Funds', 'Funds & ETFs', 'Mutual Funds', 'Unclassified']
        fund_mask = df['sector'].isin(institutional_sectors)
        df.loc[fund_mask, 'Unrealized Gain'] = "Institutional Allocation"
        df.loc[fund_mask, 'Total Return'] = "N/A"

        # Metrics overview row
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Trades Tracked", len(df))
        col2.metric("Unique Tickers", df['ticker'].nunique())
        col3.metric("Unique Insiders", df['insider_name'].nunique())
        col4.metric("Total Dollar Volume", f"${df['est_value'].sum():,.0f}")

        st.markdown("---")
        st.subheader("📋 Insider Transactions Database")

        # Select & reorder columns for final display
        display_df = df[[
            "transaction_date", "ticker", "sector", "insider_name", 
            "Shares Bought", "Est. Value ($)", "Unrealized Gain", "Total Return"
        ]]

        display_df.columns = [
            "Transaction Date", "Ticker", "Sector", "Insider Name", 
            "Shares Bought", "Est. Value ($)", "Unrealized Gain (Inc. Divs)", "Total Return"
        ]

        st.dataframe(display_df, use_container_width=True)

    else:
        st.info("No insider transactions found in database.")

except Exception as e:
    st.error(f"Error connecting to database or fetching data: {e}")
