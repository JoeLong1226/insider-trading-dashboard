import os
import pandas as pd
import psycopg2
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Insider Trading Dashboard", layout="wide")


@st.cache_resource
def get_db_connection():
    conn_url = os.environ.get("SUPABASE_DB_URL")
    return psycopg2.connect(conn_url)


@st.cache_data(ttl=3600)
def calculate_trade_performance(ticker, tx_date_str, shares, est_value):
    if (
        not ticker
        or ticker.upper() in ["UNKNOWN", "NONE", "NULL", "N/A"]
        or shares <= 0
    ):
        return 0.0, 0.0, 0.0
    cleaned_ticker = ticker.replace(".", "-")
    try:
        stock = yf.Ticker(cleaned_ticker)
        tx_date = pd.to_datetime(tx_date_str).tz_localize(None)
        current_price = stock.fast_info.get(
            "lastPrice"
        ) or stock.info.get("currentPrice")
        hist = stock.history(start=tx_date_str)
        if hist.empty:
            return 0.0, 0.0, 0.0
        if not current_price:
            current_price = hist["Close"].iloc[-1]

        # Calculate 30-day price trend prior to trade date for Bullish Divergence
        start_30d = tx_date - pd.Timedelta(days=30)
        hist_30d = stock.history(
            start=start_30d.strftime("%Y-%m-%d"), end=tx_date_str
        )
        price_30d_change_pct = 0.0
        if not hist_30d.empty and len(hist_30d) >= 2:
            old_p = hist_30d["Close"].iloc[0]
            new_p = hist_30d["Close"].iloc[-1]
            if old_p > 0:
                price_30d_change_pct = ((new_p - old_p) / old_p) * 100.0

        current_equity_val = shares * current_price
        capital_gain = current_equity_val - est_value
        dividends = stock.dividends
        total_div_per_share = 0.0
        if not dividends.empty:
            dividends.index = dividends.index.tz_localize(None)
            total_div_per_share = dividends[dividends.index >= tx_date].sum()

        total_dollar_return = capital_gain + (shares * total_div_per_share)
        percent_return = (
            (total_dollar_return / est_value) * 100 if est_value > 0 else 0.0
        )
        return total_dollar_return, percent_return, price_30d_change_pct
    except Exception:
        return 0.0, 0.0, 0.0


def process_advanced_conviction_metrics(df):
    """Calculates Conviction Score and combines multiple icon signals per trade."""
    df["parsed_date"] = pd.to_datetime(df["transaction_date"])

    sector_volumes = df.groupby("sector")["est_value"].transform("sum")
    avg_sector_vol = df["est_value"].sum() / max(df["sector"].nunique(), 1)

    flags = []
    scores = []

    for idx, row in df.iterrows():
        current_date = row["parsed_date"]
        ticker = row["ticker"]
        insider = row["insider_name"]
        sector = row["sector"]
        title = str(row["insider_title"] or "").upper()

        shares_bought = (
            float(row["shares_bought"])
            if pd.notnull(row["shares_bought"])
            else 0.0
        )
        shares_following = (
            float(row["shares_owned_following"])
            if pd.notnull(row["shares_owned_following"])
            else 0.0
        )
        est_val = (
            float(row["est_value"]) if pd.notnull(row["est_value"]) else 0.0
        )
        price_30d_change = row.get("price_30d_change_pct", 0.0)

        prior_shares = shares_following - shares_bought
        if prior_shares > 0 and shares_bought > 0:
            position_increase_pct = (shares_bought / prior_shares) * 100.0
        else:
            position_increase_pct = 0.0

        window_df = df[
            (df["ticker"] == ticker)
            & (df["parsed_date"] >= current_date - pd.Timedelta(days=60))
            & (df["parsed_date"] <= current_date + pd.Timedelta(days=60))
        ]

        unique_insiders = window_df["insider_name"].nunique()
        repeat_buys_by_insider = len(
            window_df[window_df["insider_name"] == insider]
        )
        is_csuite = any(
            t in title
            for t in ["CEO", "CHIEF EXECUTIVE", "CFO", "CHAIRMAN", "PRESIDENT"]
        )

        # Score calculation
        score = 0
        score += min(int((est_val / 500000) * 30), 30)
        if is_csuite:
            score += 25
        if unique_insiders >= 2:
            score += 25
        elif repeat_buys_by_insider >= 2:
            score += 15
        score += min(int((position_increase_pct / 50) * 20), 20)
        score = min(max(score, 10), 100)
        scores.append(score)

        # Multi-Icon Accumulation
        trade_emojis = []
        if price_30d_change <= -10.0 and is_csuite and est_val >= 50000:
            trade_emojis.append("🎯")
        if unique_insiders >= 2:
            trade_emojis.append("🔥")
        if is_csuite and repeat_buys_by_insider >= 2:
            trade_emojis.append("⚡")
        if position_increase_pct >= 10.0 and est_val >= 50000:
            trade_emojis.append("🚀")
        if sector_volumes.loc[idx] > (
            avg_sector_vol * 1.5
        ) and sector not in ["Unclassified", "Private Credit & Funds"]:
            trade_emojis.append("🌐")

        flags.append(" ".join(trade_emojis) if trade_emojis else "—")

    df["Conviction Score"] = scores
    df["High Conviction Flag"] = flags
    return df


st.title("📊 Insider Transactions Dashboard")
st.write(
    "Market-wide tracking of high-conviction open-market executive purchases (\$50,000+)."
)

try:
    conn = get_db_connection()
    query = """
        SELECT transaction_date, ticker, sector, insider_name, insider_title, shares_bought, shares_owned_following, est_value, activity_type 
        FROM insider_trades 
        ORDER BY transaction_date DESC;
    """
    df = pd.read_sql(query, conn)

    if not df.empty:
        perf = [
            calculate_trade_performance(
                row["ticker"],
                row["transaction_date"],
                row["shares_bought"],
                float(row["est_value"] or 0),
            )
            for _, row in df.iterrows()
        ]
        df["Unrealized Profit (\$)"] = [p[0] for p in perf]
        df["Return (%)"] = [p[1] for p in perf]
        df["price_30d_change_pct"] = [p[2] for p in perf]

        df = process_advanced_conviction_metrics(df)

        df["Unrealized Gain"] = df["Unrealized Profit (\$)"].apply(
            lambda x: f"+\${x:,.0f}" if x >= 0 else f"-\${abs(x):,.0f}"
        )
        df["Total Return"] = df["Return (%)"].apply(
            lambda x: f"+{x:.1f}%" if x >= 0 else f"{x:.1f}%"
        )
        df["Shares Bought"] = df["shares_bought"].apply(
            lambda x: f"+{x:,.0f}" if pd.notnull(x) else "+0"
        )
        df["Est. Value (\$)"] = df["est_value"].apply(
            lambda x: f"\${x:,.0f}" if pd.notnull(x) else "\$0"
        )

        institutional_sectors = [
            "Private Credit & Funds",
            "Funds & ETFs",
            "Mutual Funds",
            "Unclassified",
        ]
        fund_mask = df["sector"].isin(institutional_sectors)
        df.loc[fund_mask, "Unrealized Gain"] = "Institutional Allocation"
        df.loc[fund_mask, "Total Return"] = "N/A"

        # Sidebar Filter Controls
        st.sidebar.header("Filter Dashboard")
        show_flags_only = st.sidebar.checkbox(
            "Show High Conviction Signals Only", value=False
        )
        min_score = st.sidebar.slider(
            "Minimum Conviction Score",
            min_value=10,
            max_value=90,
            value=10,
            step=5,
        )

        # Sidebar Signal Legend
        st.sidebar.markdown("---")
        st.sidebar.markdown("### 📖 Signal Legend")
        st.sidebar.markdown("""
        * **🎯** **Bullish Divergence** (10%+ stock dip with C-suite buying)
        * **🔥** **Cluster** (2+ insiders in 60d)
        * **⚡** **C-Suite Accumulation** (Repeat buying by execs)
        * **🚀** **Skin-in-the-Game** (10%+ position increase, \$50k+)
        * **🌐** **Sector Rotation** (High volume burst in sector)
        """)

        # Main Navigation Tabs
        tab1, tab2 = st.tabs(["📊 Live Transactions Feed", "🏆 Insider Leaderboard"])

        with tab1:
            high_alert_count = len(df[df["High Conviction Flag"] != "—"])

            col1, col2, col3, col4, col5 = st.columns(5)
            col1.metric("Total Trades Tracked", len(df))
            col2.metric("🔥 High Conviction Signals", high_alert_count)
            col3.metric(
                "Avg Conviction Score", f"{int(df['Conviction Score'].mean())}/100"
            )
            col4.metric("Unique Insiders", df["insider_name"].nunique())
            col5.metric(
                "Total Dollar Volume",
                f"\${df['est_value'].astype(float).sum():,.0f}",
            )

            st.markdown("---")

            display_source = df[df["Conviction Score"] >= min_score]
            if show_flags_only:
                display_source = display_source[
                    display_source["High Conviction Flag"] != "—"
                ]

            st.subheader("📋 Insider Transactions Database")

            display_df = display_source[[
                "transaction_date",
                "Conviction Score",
                "High Conviction Flag",
                "ticker",
                "sector",
                "insider_name",
                "insider_title",
                "Shares Bought",
                "Est. Value (\$)",
                "Unrealized Gain",
                "Total Return
