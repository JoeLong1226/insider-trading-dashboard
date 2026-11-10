from datetime import datetime
import os
import pandas as pd
import psycopg2
import streamlit as st
import yfinance as yf

st.set_page_config(
    page_title="Insider Transactions Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)


def get_db_connection():
    conn_url = os.environ.get("SUPABASE_DB_URL")
    if not conn_url:
        raise ValueError("Missing SUPABASE_DB_URL environment variable.")
    return psycopg2.connect(conn_url)


@st.cache_data(ttl=86400)  # Cache sector lookups for 24 hours
def get_stock_sector(ticker):
    if not ticker or ticker.upper() in ["UNKNOWN", "NONE", "NULL", "N/A"]:
        return "Unclassified"

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


@st.cache_data(ttl=86400)  # Cache benchmark & stock performance
def calculate_trade_performance(
    ticker, benchmark_ticker, tx_date_str, shares, est_value_db
):
    if (
        not ticker
        or ticker.upper() in ["UNKNOWN", "NONE", "NULL", "N/A"]
        or shares <= 0
    ):
        return 0.0, 0.0, 0.0, est_value_db, 0.0, 0.0

    clean_date_str = str(tx_date_str)[:10]
    cleaned_ticker = ticker.replace(".", "-")

    try:
        stock = yf.Ticker(cleaned_ticker)
        tx_date = pd.to_datetime(clean_date_str)
        current_price = stock.fast_info.get("lastPrice") or stock.info.get(
            "currentPrice"
        )
        hist = stock.history(start=clean_date_str)
        if hist.empty:
            return 0.0, 0.0, 0.0, est_value_db, 0.0, 0.0
        if not current_price:
            current_price = hist["Close"].iloc[-1]

        tx_price_adj = hist["Close"].iloc[0]
        if tx_price_adj <= 0:
            return 0.0, 0.0, 0.0, est_value_db, 0.0, 0.0

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

        benchmark_pct_return = 0.0
        if benchmark_ticker and benchmark_ticker not in ["None", "UNKNOWN", ""]:
            try:
                b_stock = yf.Ticker(benchmark_ticker)
                b_hist = b_stock.history(start=clean_date_str)
                if not b_hist.empty and len(b_hist) >= 1:
                    b_start_price = b_hist["Close"].iloc[0]
                    b_current_price = b_stock.fast_info.get(
                        "lastPrice"
                    ) or b_hist["Close"].iloc[-1]
                    if b_start_price > 0:
                        benchmark_pct_return = (
                            (b_current_price - b_start_price) / b_start_price
                        ) * 100.0
            except Exception:
                pass

        alpha_return = percent_return - benchmark_pct_return

        return (
            total_dollar_return,
            percent_return,
            price_30d_change_pct,
            true_est_value,
            benchmark_pct_return,
            alpha_return,
        )
    except Exception:
        return 0.0, 0.0, 0.0, est_value_db, 0.0, 0.0


def process_advanced_conviction_metrics(df):
    """Institutional Conviction Score Architecture v2.1.

    Re-engineered to incorporate relative skin-in-the-game, multi-insider
    consensus, operational role authority, consolidated trade sizing,
    micro-cap noise suppression, and falling-knife trend filters.
    """
    df["parsed_date"] = pd.to_datetime(df["transaction_date"].astype(str).str[:10])
    df = df.sort_values(by="parsed_date", ascending=True).reset_index(drop=True)

    flags = []
    scores = []

    for idx, row in df.iterrows():
        current_date = row["parsed_date"]
        ticker = row["ticker"]
        title = str(row["insider_title"] or "").upper()

        shares_bought = float(row["shares_bought"]) if pd.notnull(row["shares_bought"]) else 0.0
        shares_following = float(row["shares_owned_following"]) if pd.notnull(row["shares_owned_following"]) else 0.0
        est_val = float(row["est_value"]) if pd.notnull(row["est_value"]) else 0.0
        market_cap = float(row["market_cap"]) if pd.notnull(row["market_cap"]) else 0.0
        price_30d = float(row.get("price_30d_change_pct", 0.0) or 0.0)

        # 1. Relative Personal Commitment (% Position Increase)
        prior_shares = shares_following - shares_bought
        if prior_shares > 0 and shares_bought > 0:
            position_increase_pct = (shares_bought / prior_shares) * 100.0
        elif shares_bought > 0 and prior_shares <= 0:
            position_increase_pct = 100.0  # New position initiation
        else:
            position_increase_pct = 0.0

        # 2. Multi-Insider Consensus (Rolling 30-day Window)
        window_df = df[
            (df["ticker"] == ticker)
            & (df["parsed_date"] >= current_date - pd.Timedelta(days=30))
            & (df["parsed_date"] <= current_date)
        ]

        distinct_csuite_insiders = window_df[
            window_df["insider_title"]
            .astype(str)
            .str.upper()
            .str.contains("CEO|CFO|COO|CHIEF EXECUTIVE|CHIEF FINANCIAL|CHIEF OPERATING")
        ]["insider_name"].nunique()

        distinct_all_insiders = window_df["insider_name"].nunique()

        # 3. Role Tiering
        is_tier1 = any(
            t in title for t in ["CEO", "CFO", "COO", "CHIEF EXECUTIVE", "CHIEF FINANCIAL", "CHIEF OPERATING"]
        )
        is_tier2 = any(t in title for t in ["PRESIDENT", "VP", "VICE PRESIDENT"]) and not is_tier1

        score = 15  # Base score

        # Factor A: Market Cap Adjustment (penalize extreme micro-cap distress noise < $50M)
        if 0 < market_cap < 50000000:
            score -= 10
        elif market_cap >= 500000000:  # Mid/Large cap stability bonus
            score += 10

        # Factor B: Operational Role Authority
        if is_tier1:
            score += 25
        elif is_tier2:
            score += 15
        else:
            score += 5

        # Factor C: Relative Personal Stake Expansion
        if position_increase_pct >= 100.0:
            score += 30
        elif position_increase_pct >= 50.0:
            score += 20
        elif position_increase_pct >= 10.0:
            score += 10

        # Factor D: Absolute Dollar Commitment
        if est_val >= 500000:
            score += 20
        elif est_val >= 100000:
            score += 10

        # Factor E: Multi-Insider Consensus Bonus
        if distinct_csuite_insiders >= 2:
            score += 25
        elif distinct_all_insiders >= 3:
            score += 20

        # Factor F: Trend Filter (Avoid Falling Knives)
        if price_30d < -30.0:  # Stock crashed >30% before purchase (value trap risk)
            score -= 20
        elif -15.0 <= price_30d <= 15.0:  # Stable accumulation / consolidation zone
            score += 15

        score = min(max(score, 0), 100)
        scores.append(score)

        # Signal Flags
        trade_emojis = []
        if distinct_csuite_insiders >= 2 or distinct_all_insiders >= 3:
            trade_emojis.append("🔥")  # Cluster Consensus
        if is_tier1 and est_val >= 100000:
            trade_emojis.append("⚡")  # Operational Leadership Buy
        if position_increase_pct >= 25.0 and est_val >= 50000:
            trade_emojis.append("🚀")  # Major Skin-in-the-Game

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

conn = None
try:
    conn = get_db_connection()

    st.sidebar.header("Filter Dashboard")
    selected_year = st.sidebar.selectbox(
        "Filter by Year", ["All Years", "2026", "2025", "2024", "Older"]
    )

    months = [
        "All Months",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    selected_month = st.sidebar.selectbox("Filter by Month", months, index=0)

    max_rows = st.sidebar.slider(
        "Max Rows to Load & Analyze", 50, 1000, 200, step=50
    )

    hide_institutions = st.sidebar.checkbox(
        "Hide Institutional / Fund Entities", value=False
    )

    where_clauses = []
    if selected_year == "Older":
        where_clauses.append("LEFT(transaction_date, 4) < '2024'")
    elif selected_year != "All Years":
        where_clauses.append(f"LEFT(transaction_date, 4) = '{selected_year}'")

    if selected_month != "All Months":
        month_idx = months.index(selected_month)
        month_str = f"{month_idx:02d}"
        where_clauses.append(f"SUBSTRING(transaction_date, 6, 2) = '{month_str}'")

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    query = f"""
        SELECT transaction_date, ticker, sector, insider_name, insider_title, 
               shares_bought, shares_owned_following, est_value, activity_type,
               market_cap, market_cap_category, benchmark_ticker
        FROM insider_trades 
        {where_sql}
        ORDER BY transaction_date DESC
        LIMIT {max_rows};
    """

    df = pd.read_sql(query, conn)

    if not df.empty:
        # Group same-day multi-lot buys by same insider
        group_cols = ["transaction_date", "ticker", "insider_name"]
        agg_rules = {
            "sector": "first",
            "insider_title": "first",
            "shares_bought": "sum",
            "shares_owned_following": "last",
            "est_value": "sum",
            "activity_type": "first",
            "market_cap": "first",
            "market_cap_category": "first",
            "benchmark_ticker": "first",
        }
        df = df.groupby(group_cols, as_index=False).agg(agg_rules)

        df["sector"] = df.apply(
            lambda row: get_stock_sector(row["ticker"])
            if row["sector"] in ["Unclassified", None, ""]
            else row["sector"],
            axis=1,
        )

        if hide_institutions:
            fund_keywords = [
                "LLC",
                "L.P.",
                "PARTNERS",
                "FUNDS",
                "CAPITAL",
                "INVESTMENT",
                "MANAGEMENT",
            ]
            pattern = "|".join(fund_keywords)
            df = df[
                ~df["insider_name"].str.upper().str.contains(pattern, na=False)
            ].reset_index(drop=True)

    if not df.empty:
        perf = [
            calculate_trade_performance(
                row["ticker"],
                row.get("benchmark_ticker"),
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
        df["Benchmark Return (%)"] = [p[4] for p in perf]
        df["Alpha Return (%)"] = [p[5] for p in perf]

        df = process_advanced_conviction_metrics(df)

        def format_market_cap(x):
            if pd.notnull(x) and x > 0:
                if x >= 1e9:
                    return f"${x / 1e9:.1f}B"
                elif x >= 1e6:
                    return f"${x / 1e6:.1f}M"
                return f"${x:,.0f}"
            return "N/A"

        df["Market Cap at Tx"] = df["market_cap"].apply(format_market_cap)
        df["Shares Bought"] = df["shares_bought"].apply(
            lambda x: f"+{x:,.0f}" if pd.notnull(x) else "+0"
        )
        df["Est. Value ($)"] = df["est_value"].apply(
            lambda x: f"${x:,.0f}" if pd.notnull(x) else "$0"
        )

        current_date = pd.to_datetime("2026-11-10")

        def format_gain(row):
            profit = row["Unrealized Profit ($)"]
            tx_date = pd.to_datetime(str(row["transaction_date"])[:10])
            if (current_date - tx_date).days <= 2 and profit == 0:
                return "N/A (New)"
            return (
                f"+${profit:,.0f}" if profit >= 0 else f"-${abs(profit):,.0f}"
            )

        def format_return(row):
            ret = row["Return (%)"]
            tx_date = pd.to_datetime(str(row["transaction_date"])[:10])
            if (current_date - tx_date).days <= 2 and ret == 0:
                return "N/A (New)"
            return f"+{ret:.1f}%" if ret >= 0 else f"{ret:.1f}%"

        df["Unrealized Gain"] = df.apply(format_gain, axis=1)
        df["Total Return"] = df.apply(format_return, axis=1)

        df["Benchmark Return"] = df["Benchmark Return (%)"].apply(
            lambda x: f"+{x:.1f}%" if x >= 0 else f"{x:.1f}%"
        )
        df["Alpha Return"] = df["Alpha Return (%)"].apply(
            lambda x: f"+{x:.1f}%" if x >= 0 else f"{x:.1f}%"
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
        * **🔥** **Cluster Consensus** (2+ distinct C-Suite/execs buying within 30 days)
        * **⚡** **Operational Leadership** (CEO, CFO, or COO purchase $\ge \$100k$)
        * **🚀** **Skin-in-the-Game** ($\ge 25\%$ personal position expansion, $\ge \$50k$)
        """)

        st.sidebar.markdown("---")
        st.sidebar.markdown("### 💾 Export Data")
        csv_export = df.to_csv(index=False).encode("utf-8")
        st.sidebar.download_button(
            label="📥 Download Current Data (CSV)",
            data=csv_export,
            file_name="insider_trades_filtered.csv",
            mime="text/csv",
        )

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
                "Market Cap at Tx",
                "market_cap_category",
                "benchmark_ticker",
                "Benchmark Return",
                "Alpha Return",
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
                "Market Cap at Tx",
                "Market Cap Tier",
                "Benchmark",
                "Benchmark Return",
                "Alpha vs Benchmark",
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
                    "No equity insider trades available for leaderboard calculation."
                )
    else:
        st.info("No insider transactions found for the selected filter.")

except Exception as e:
    st.error(f"Error connecting to database or fetching data: {e}")
finally:
    if conn:
        try:
            conn.close()
        except Exception:
            pass
