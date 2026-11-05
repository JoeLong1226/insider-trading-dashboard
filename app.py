import os
import pandas as pd
import psycopg2
import streamlit as st
import yfinance as yf

st.set_page_config(
    page_title="Insider Trading Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_db_connection():
    conn_url = os.environ.get("SUPABASE_DB_URL")
    return psycopg2.connect(conn_url)


@st.cache_data(ttl=86400)  # Cache sector lookups for 24 hours
def get_stock_sector(ticker):
    if not ticker or ticker.upper() in ["UNKNOWN", "NONE", "NULL", "N/A"]:
        return "Unclassified"
    
    # Manual fallback overrides for any tickers Yahoo Finance misses
    sector_fallbacks = {
        "PBPB": "Consumer Cyclical",
        "TTSH": "Healthcare",
    }
    cleaned_upper = ticker.upper().strip()
    if cleaned_upper in sector_fallbacks:
        return sector_fallbacks[cleaned_upper]

    try:
        cleaned_ticker = ticker.replace(".", "-")
        stock = yf.Ticker(cleaned_ticker)
        sector = stock.info.get("sector")
        if sector:
            return sector
    except Exception:
        pass
    return "Unclassified"


@st.cache_data(ttl=86400)  # Cache stock performance for 24 hours to prevent lag
def calculate_trade_performance(ticker, tx_date_str, shares, est_value_db):
    if (
        not ticker
        or ticker.upper() in ["UNKNOWN", "NONE", "NULL", "N/A"]
        or shares <= 0
    ):
        return 0.0, 0.0, 0.0, est_value_db
    
    clean_date_str = str(tx_date_str)[:10]
    cleaned_ticker = ticker.replace(".", "-")
    
    try:
        stock = yf.Ticker(cleaned_ticker)
        tx_date = pd.to_datetime(clean_date_str)
        current_price = stock.fast_info.get(
            "lastPrice"
        ) or stock.info.get("currentPrice")
        hist = stock.history(start=clean_date_str)
        if hist.empty:
            return 0.0, 0.0, 0.0, est_value_db
        if not current_price:
            current_price = hist["Close"].iloc[-1]

        tx_price_adj = hist["Close"].iloc[0]
        if tx_price_adj <= 0:
            return 0.0, 0.0, 0.0, est_value_db

        true_est_value = shares * tx_price_adj
        stock_pct_return = (current_price - tx_price_adj) / tx_price_adj

        start_30d = tx_date - pd.Timedelta(days=30)
        hist_30d = stock.history(
            start=start_30d.strftime("%Y-%m-%d"), end=clean_date_str
        )
        price_30d_change_pct = 0.0
        if not hist_30d.empty and len(hist_30d) >= 2:
            old_p = hist_30d["Close"].iloc[0]
            new_p = hist_30d["Close"].iloc[-1]
            if old_p > 0:
                price_30d_change_pct = ((new_p - old_p) / old_p) * 100.0

        capital_gain = true_est_value * stock_pct_return
        dividends = stock.dividends
        total_div_dollar = 0.0
        if not dividends.empty:
            if dividends.index.tz is not None:
                dividends.index = dividends.index.tz_localize(None)
            total_div_per_share = dividends[dividends.index >= tx_date].sum()
            total_div_dollar = shares * total_div_per_share

        total_dollar_return = capital_gain + total_div_dollar
        percent_return = (
            (total_dollar_return / true_est_value) * 100
            if true_est_value > 0
            else 0.0
        )
        return (
            total_dollar_return,
            percent_return,
            price_30d_change_pct,
            true_est_value,
        )
    except Exception:
        return 0.0, 0.0, 0.0, est_value_db


def process_advanced_conviction_metrics(df):
    """Calculates Point-in-Time Conviction Score strictly looking backward."""
    df["parsed_date"] = pd.to_datetime(df["transaction_date"].astype(str).str[:10])
    df = df.sort_values(by="parsed_date", ascending=True).reset_index(drop=True)

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
            & (df["parsed_date"] <= current_date)
        ]

        unique_insiders = window_df["insider_name"].nunique()
        
        # Count distinct buy dates instead of raw rows to accurately measure true repeat buying
        insider_window = window_df[window_df["insider_name"] == insider]
        distinct_buy_days = insider_window["parsed_date"].nunique()

        is_csuite = any(
            t in title
            for t in ["CEO", "CHIEF EXECUTIVE", "CFO", "CHAIRMAN", "PRESIDENT"]
        )

        score = 0
        score += min(int((est_val / 500000) * 30), 30)
        if is_csuite:
            score += 25
        if unique_insiders >= 2:
            score += 25
        elif distinct_buy_days >= 2:
            score += 15
        score += min(int((position_increase_pct / 50) * 20), 20)
        score = min(max(score, 10), 100)
        scores.append(score)

        trade_emojis = []
        if price_30d_change <= -10.0 and is_csuite and est_val >= 50000:
            trade_emojis.append("🎯")
        if unique_insiders >= 2:
            trade_emojis.append("🔥")
        if is_csuite and distinct_buy_days >= 2:
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
    return df.sort_values(by="parsed_date", ascending=False).reset_index(
        drop=True
    )


st.title("📊 Insider Transactions Dashboard")
st.write(
    "Market-wide tracking of high-conviction open-market executive purchases ($50,000+)."
)

try:
    conn = get_db_connection()

    # Sidebar Controls FIRST
    st.sidebar.header("Filter Dashboard")
    selected_year = st.sidebar.selectbox(
        "Filter by Year", ["All Years", "2026", "2025", "2024", "Older"]
    )
    max_rows = st.sidebar.slider(
        "Max Rows to Load & Analyze", 50, 1000, 200, step=50
    )

    # Toggle to filter out institutional/fund entity names cleanly
    hide_institutions = st.sidebar.checkbox(
        "Hide Institutional / Fund Entities", value=False
    )

    # Dynamic SQL query based on year selection
    if selected_year == "All Years":
        query = f"""
            SELECT transaction_date, ticker, sector, insider_name, insider_title, shares_bought, shares_owned_following, est_value, activity_type 
            FROM insider_trades 
            ORDER BY transaction_date DESC
            LIMIT {max_rows};
        """
    elif selected_year == "Older":
        query = f"""
            SELECT transaction_date, ticker, sector, insider_name, insider_title, shares_bought, shares_owned_following, est_value, activity_type 
            FROM insider_trades 
            WHERE LEFT(transaction_date, 4) < '2024'
            ORDER BY transaction_date DESC
            LIMIT {max_rows};
        """
    else:
        query = f"""
            SELECT transaction_date, ticker, sector, insider_name, insider_title, shares_bought, shares_owned_following, est_value, activity_type 
            FROM insider_trades 
            WHERE LEFT(transaction_date, 4) = '{selected_year}'
            ORDER BY transaction_date DESC
            LIMIT {max_rows};
        """

    df = pd.read_sql(query, conn)

    if not df.empty:
        # Dynamically resolve 'Unclassified' sectors on the fly using yfinance & manual overrides
        df["sector"] = df.apply(
            lambda row: get_stock_sector(row["ticker"]) if row["sector"] in ["Unclassified", None, ""] else row["sector"],
            axis=1
        )

        # Filter out funds/partnerships by name patterns if checked, preserving unclassified corporate executives
        if hide_institutions:
            fund_keywords = ["LLC", "L.P.", "PARTNERS", "FUNDS", "CAPITAL", "INVESTMENT", "MANAGEMENT"]
            pattern = '|'.join(fund_keywords)
            df = df[~df['insider_name'].str.upper().str.contains(pattern, na=False)].reset_index(drop=True)

    if not df.empty:
        perf = [
            calculate_trade_performance(
                row["ticker"],
                row["transaction_date"],
                float(row["shares_bought"] or 0),
                float(row["est_value"] or 0),
            )
            for _, row in df.iterrows()
        ]
        df["Unrealized Profit ($)"] = [p[0] for p in perf]
        df["Return (%)"] = [p[1] for p in perf]
        df["price_30d_change_pct"] = [p[2] for p in perf]
        df["est_value"] = [p[3] for p in perf]

        df = process_advanced_conviction_metrics(df)

        df["Unrealized Gain"] = df["Unrealized Profit ($)"].apply(
            lambda x: f"+${x:,.0f}" if x >= 0 else f"-${abs(x):,.0f}"
        )
        df["Total Return"] = df["Return (%)"].apply(
            lambda x: f"+{x:.1f}%" if x >= 0 else f"{x:.1f}%"
        )
        df["Shares Bought"] = df["shares_bought"].apply(
            lambda x: f"+{x:,.0f}" if pd.notnull(x) else "+0"
        )
        df["Est. Value ($)"] = df["est_value"].apply(
            lambda x: f"${x:,.0f}" if pd.notnull(x) else "$0"
        )

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

        st.sidebar.markdown("---")
        st.sidebar.markdown("### 📖 Signal Legend")
        st.sidebar.markdown("""
        * **🎯** **Bullish Divergence** (10%+ stock dip with C-suite buying)
        * **🔥** **Cluster** (2+ insiders in prior 60d)
        * **⚡** **C-Suite Accumulation** (Repeat buying by execs)
        * **🚀** **Skin-in-the-Game** (10%+ position increase, $50k+)
        * **🌐** **Sector Rotation** (High volume burst in sector)
        """)

        tab1, tab2 = st.tabs([
            "📊 Live Transactions Feed",
            "🏆 Insider Leaderboard",
        ])

        with tab1:
            high_alert_count = len(df[df["High Conviction Flag"] != "—"])

            col1, col2, col3, col4, col5 = st.columns(5)
            col1.metric("Total Trades Tracked", len(df))
            col2.metric("🔥 High Conviction Signals", high_alert_count)
            col3.metric(
                "Avg Conviction Score",
                f"{int(df['Conviction Score'].mean())}/100",
            )
            col4.metric("Unique Insiders", df["insider_name"].nunique())
            col5.metric(
                "Total Dollar Volume",
                f"${df['est_value'].astype(float).sum():,.0f}",
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
                "Est. Value ($)",
                "Unrealized Gain",
                "Total Return",
            ]]

            display_df.columns = [
                "Transaction Date",
                "Score",
                "Signals",
                "Ticker",
                "Sector",
                "Insider Name",
                "Title",
                "Shares Bought",
                "Est. Value ($)",
                "Unrealized Gain (Inc. Divs)",
                "Total Return",
            ]

            st.dataframe(display_df, use_container_width=True)

        with tab2:
            st.subheader("🏆 Aggregate Insider Performance Leaderboard")
            leaderboard_source = df.copy()

            if not leaderboard_source.empty:
                leaderboard = (
                    leaderboard_source.groupby(["insider_name", "ticker"])
                    .agg({
                        "shares_bought": "sum",
                        "est_value": "sum",
                        "Unrealized Profit ($)": "sum",
                    })
                    .reset_index()
                )

                leaderboard["roi_pct"] = (
                    leaderboard["Unrealized Profit ($)"]
                    / leaderboard["est_value"]
                ) * 100.0

                leaderboard["Total Invested"] = leaderboard["est_value"].apply(
                    lambda x: f"${x:,.0f}"
                )
                leaderboard["Net Profit/Loss ($)"] = leaderboard[
                    "Unrealized Profit ($)"
                ].apply(
                    lambda x: f"+${x:,.0f}" if x >= 0 else f"-${abs(x):,.0f}"
                )
                leaderboard["Cumulative ROI (%)"] = leaderboard["roi_pct"].apply(
                    lambda x: f"+{x:.1f}%" if x >= 0 else f"{x:.1f}%"
                )

                col_left, col_right = st.columns(2)

                with col_left:
                    st.markdown("### 🟢 Top Dollar Winners ($)")
                    top_dollar_winners = leaderboard.sort_values(
                        by="Unrealized Profit ($)", ascending=False
                    ).head(10)
                    st.dataframe(
                        top_dollar_winners[[
                            "insider_name",
                            "ticker",
                            "Total Invested",
                            "Net Profit/Loss ($)",
                            "Cumulative ROI (%)",
                        ]],
                        use_container_width=True,
                    )

                    st.markdown("### 🔴 Top Dollar Losers ($)")
                    top_dollar_losers = leaderboard.sort_values(
                        by="Unrealized Profit ($)", ascending=True
                    ).head(10)
                    st.dataframe(
                        top_dollar_losers[[
                            "insider_name",
                            "ticker",
                            "Total Invested",
                            "Net Profit/Loss ($)",
                            "Cumulative ROI (%)",
                        ]],
                        use_container_width=True,
                    )

                with col_right:
                    st.markdown("### 🚀 Top ROI Winners (%)")
                    top_pct_winners = leaderboard.sort_values(
                        by="roi_pct", ascending=False
                    ).head(10)
                    st.dataframe(
                        top_pct_winners[[
                            "insider_name",
                            "ticker",
                            "Total Invested",
                            "Net Profit/Loss ($)",
                            "Cumulative ROI (%)",
                        ]],
                        use_container_width=True,
                    )

                    st.markdown("### 📉 Top ROI Losers (%)")
                    top_pct_losers = leaderboard.sort_values(
                        by="roi_pct", ascending=True
                    ).head(10)
                    st.dataframe(
                        top_pct_losers[[
                            "insider_name",
                            "ticker",
                            "Total Invested",
                            "Net Profit/Loss ($)",
                            "Cumulative ROI (%)",
                        ]],
                        use_container_width=True,
                    )
            else:
                st.info(
                    "No equity insider trades available for leaderboard"
                    " calculation."
                )
    else:
        st.info("No insider transactions found for the selected filter.")

except Exception as e:
    st.error(f"Error connecting to database or fetching data: {e}")
