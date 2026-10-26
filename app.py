import streamlit as st
import pandas as pd
import psycopg2
import os
import yfinance as yf

st.set_page_config(page_title="Insider Trading Dashboard", layout="wide")

@st.cache_resource
def get_db_connection():
    conn_url = os.environ.get("SUPABASE_DB_URL")
    return psycopg2.connect(conn_url)

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
        if hist.empty: return 0.0, 0.0
        if not current_price: current_price = hist['Close'].iloc[-1]

        current_equity_val = shares * current_price
        capital_gain = current_equity_val - est_value
        dividends = stock.dividends
        total_div_per_share = 0.0
        if not dividends.empty:
            dividends.index = dividends.index.tz_localize(None)
            total_div_per_share = dividends[dividends.index >= tx_date].sum()
        
        total_dollar_return = capital_gain + (shares * total_div_per_share)
        percent_return = (total_dollar_return / est_value) * 100 if est_value > 0 else 0.0
        return total_dollar_return, percent_return
    except Exception:
        return 0.0, 0.0

def apply_high_conviction_flags(df):
    """Evaluates multi-insider clusters, C-suite accumulation, and verified skin-in-the-game."""
    df['parsed_date'] = pd.to_datetime(df['transaction_date'])
    flags = []

    for idx, row in df.iterrows():
        current_date = row['parsed_date']
        ticker = row['ticker']
        insider = row['insider_name']
        title = str(row['insider_title'] or "").upper()
        
        shares_bought = float(row['shares_bought']) if pd.notnull(row['shares_bought']) else 0.0
        shares_following = float(row['shares_owned_following']) if pd.notnull(row['shares_owned_following']) else 0.0
        est_val = float(row['est_value']) if pd.notnull(row['est_value']) else 0.0

        # Calculate position percentage increase strictly when prior shares are known and > 0
        prior_shares = shares_following - shares_bought
        if prior_shares > 0 and shares_bought > 0:
            position_increase_pct = (shares_bought / prior_shares) * 100.0
        else:
            position_increase_pct = 0.0

        # Expanded Lookback Window for Clusters
        window_df = df[
            (df['ticker'] == ticker) & 
            (df['parsed_date'] >= current_date - pd.Timedelta(days=60)) & 
            (df['parsed_date'] <= current_date + pd.Timedelta(days=60))
        ]
        
        unique_insiders = window_df['insider_name'].nunique()
        repeat_buys_by_insider = len(window_df[window_df['insider_name'] == insider])

        is_csuite = any(t in title for t in ["CEO", "CHIEF EXECUTIVE", "CFO", "CHAIRMAN", "PRESIDENT"])

        # Strict Prioritization Rules (Default to Standard Purchase)
        if unique_insiders >= 2:
            flags.append("🔥 Multi-Insider Cluster (60d)")
        elif is_csuite and repeat_buys_by_insider >= 2:
            flags.append("⚡ C-Suite Accumulation (60d)")
        elif position_increase_pct >= 20.0 and est_val >= 50000:
            flags.append("🚀 >20% Skin-in-the-Game")
        else:
            flags.append("Standard Purchase")

    df['High Conviction Flag'] = flags
    return df

st.title("📊 Insider Transactions Dashboard")
st.write("Market-wide tracking of high-conviction open-market executive purchases ($50,000+).")

try:
    conn = get_db_connection()
    query = """
        SELECT transaction_date, ticker, sector, insider_name, insider_title, shares_bought, shares_owned_following, est_value, activity_type 
        FROM insider_trades 
        ORDER BY transaction_date DESC;
    """
    df = pd.read_sql(query, conn)

    if not df.empty:
        df = apply_high_conviction_flags(df)

        perf = [
            calculate_trade_performance(row['ticker'], row['transaction_date'], row['shares_bought'], float(row['est_value'] or 0))
            for _, row in df.iterrows()
        ]
        df['Unrealized Profit ($)'] = [p[0] for p in perf]
        df['Return (%)'] = [p[1] for p in perf]

        df['Unrealized Gain'] = df['Unrealized Profit ($)'].apply(lambda x: f"+${x:,.0f}" if x >= 0 else f"-${abs(x):,.0f}")
        df['Total Return'] = df['Return (%)'].apply(lambda x: f"+{x:.1f}%" if x >= 0 else f"{x:.1f}%")
        df['Shares Bought'] = df['shares_bought'].apply(lambda x: f"+{x:,.0f}" if pd.notnull(x) else "+0")
        df['Est. Value ($)'] = df['est_value'].apply(lambda x: f"${x:,.0f}" if pd.notnull(x) else "$0")

        institutional_sectors = ['Private Credit & Funds', 'Funds & ETFs', 'Mutual Funds', 'Unclassified']
        fund_mask = df['sector'].isin(institutional_sectors)
        df.loc[fund_mask, 'Unrealized Gain'] = "Institutional Allocation"
        df.loc[fund_mask, 'Total Return'] = "N/A"

        high_alert_count = len(df[df['High Conviction Flag'] != "Standard Purchase"])

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Total Trades Tracked", len(df))
        col2.metric("🔥 High Conviction Signals", high_alert_count)
        col3.metric("Unique Tickers", df['ticker'].nunique())
        col4.metric("Unique Insiders", df['insider_name'].nunique())
        col5.metric("Total Dollar Volume", f"${df['est_value'].astype(float).sum():,.0f}")

        st.markdown("---")
        
        st.sidebar.header("Filter Dashboard")
        show_flags_only = st.sidebar.checkbox("Show High Conviction Signals Only", value=False)

        if show_flags_only:
            display_source = df[df['High Conviction Flag'] != "Standard Purchase"]
        else:
            display_source = df

        st.subheader("📋 Insider Transactions Database")

        display_df = display_source[[
            "transaction_date", "High Conviction Flag", "ticker", "sector", "insider_name", "insider_title",
            "Shares Bought", "Est. Value ($)", "Unrealized Gain", "Total Return"
        ]]

        display_df.columns = [
            "Transaction Date", "High Conviction Flag", "Ticker", "Sector", "Insider Name", "Title",
            "Shares Bought", "Est. Value ($)", "Unrealized Gain (Inc. Divs)", "Total Return"
        ]

        st.dataframe(display_df, use_container_width=True)

    else:
        st.info("No insider transactions found in database.")

except Exception as e:
    st.error(f"Error connecting to database or fetching data: {e}")
