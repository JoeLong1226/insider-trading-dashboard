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


@st.cache_data(ttl=86400)
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


@st.cache_data(ttl=86400)
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
    """Institutional Conviction Score Architecture v2.2."""
    df["parsed_date"] = pd.to_datetime(df["transaction_date"].astype(str).str[:10])
    df = df.sort_values(by="parsed_date", ascending=True).reset_index(drop=True)

    flags = []
    scores = []
    explanations = []

    for idx, row in df.iterrows():
        current_date = row["parsed_date"]
        ticker = row["ticker"]
        title = str(row["insider_title"] or "").upper()

        shares_bought = float(row["shares_bought"]) if pd.notnull(row["shares_bought"]) else 0.0
        shares_following = float(row["shares_owned_following"]) if pd.notnull(row["shares_owned_following"]) else 0.0
        est_val = float(row["est_value"]) if pd.notnull(row["est_value"]) else 0.0
        market_cap = float(row["market_cap"]) if pd.notnull(row["market_cap"]) else 0.0
        price_30d = float(row.get("price_30d_change_pct", 0.0) or 0.0)

        prior_shares = shares_following - shares_bought
        if prior_shares > 0 and shares_bought > 0:
            position_increase_pct = (shares_bought / prior_shares) * 100.0
        elif shares_bought > 0 and prior_shares <= 0:
            position_increase_pct = 100.0
        else:
            position_increase_pct = 0.0

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

        is_tier1 = any(
            t in title for t in ["CEO", "CFO", "COO", "CHIEF EXECUTIVE", "CHIEF FINANCIAL", "CHIEF OPERATING"]
        )
        is_tier2 = any(t in title for t in ["PRESIDENT", "VP", "VICE PRESIDENT"]) and not is_tier1
        is_admin_or_other = any(t in title for t in ["ACCOUNTING", "SECRETARY", "COUNSEL", "SEE REMARKS"])

        score = 10
        breakdown = ["Base: 10"]

        if 0 < market_cap < 50000000:
            score -= 10
            breakdown.append("Micro-cap penalty: -10")
        elif market_cap >= 500000000:
            score += 10
            breakdown.append("Stability bonus: +10")

        if is_tier1:
            score += 20
            breakdown.append("C-Suite role (CEO/CFO/COO): +20")
        elif is_tier2:
            score += 12
            breakdown.append("Executive role: +12")
        elif is_admin_or_other:
            score += 2
            breakdown.append("Admin/Support role: +2")
        else:
            score += 8
            breakdown.append("Standard Director/Officer: +8")

        if position_increase_pct >= 100.0:
            score += 25
            breakdown.append(f"Position doubled ({position_increase_pct:.1f}%): +25")
        elif position_increase_pct >= 50.0:
            score += 15
            breakdown.append(f"Position increased 50%+ ({position_increase_pct:.1f}%): +15")
        elif position_increase_pct >= 10.0:
            score += 8
            breakdown.append(f"Position increased 10%+ ({position_increase_pct:.1f}%): +8")

        if est_val >= 500000:
            score += 15
            breakdown.append("Large $500k+ purchase: +15")
        elif est_val >= 100000:
            score += 8
            breakdown.append("Substantial $100k+ purchase: +8")

        if distinct_csuite_insiders >= 2:
            score += 30
            breakdown.append("C-Suite Cluster Consensus: +30")
        elif distinct_all_insiders >= 3:
            score += 20
            breakdown.append("Multi-Insider Cluster (3+): +20")
        elif distinct_all_insiders == 2:
            score += 10
            breakdown.append("Multi-Insider Buy (2): +10")

        if price_30d < -30.0:
            score -= 25
            breakdown.append(f"Deep drop value trap filter ({price_30d:.1f}%): -25")
        elif price_30d < -15.0:
            score -= 10
            breakdown.append(f"Moderate drop filter ({price_30d:.1f}%): -10")
        elif -10.0 <= price_30d <= 15.0:
            score += 10
            breakdown.append("Stable consolidation zone: +10")

        final_score = min(max(score, 0), 100)
        scores.append(final_score)
        explanations.append(" | ".join(breakdown))

        trade_emojis = []
        if distinct_csuite_insiders >= 2 or distinct_all_insiders >= 3:
            trade_emojis.append("🔥")
        if is_tier1 and est_val >= 100000:
            trade_emojis.append("⚡")
        if position_increase_pct >= 25.0 and est_val >= 50000:
            trade_emojis.append("🚀")

        flags.append(" ".join(trade_emojis) if trade_emojis else "—")

    df["Conviction Score"] = scores
    df["High Conviction Flag"] = flags
    df["Score Breakdown"] = explanations
    return df.sort_values(by="parsed_date", ascending=False).reset_index(
        drop=True
    )


def enrich_dataframe(df):
    """Applies live YFinance performance calculations, conviction scoring, and formatting."""
    if df.empty:
        return df

    group_cols = ["transaction_date", "ticker", "insider_name"]
    agg_rules = {
        "sector": "first",
        "insider_title": "first",
        "shares_bought": "sum",
        "shares_owned_following": "last",
        "est_value": "sum",
        "market_cap": "first",
        "market_cap_category": "first",
        "benchmark_ticker": "first",
        "return_60d_pct": "first",
        "return_180d_pct": "first",
        "bench_return_60d_pct": "first",
        "bench_return_180d_pct": "first",
    }
    # Keep only columns that exist in df
    active_agg = {k: v for k, v in agg_rules.items() if k in df.columns}
    df = df.groupby(group_cols, as_index=False).agg(active_agg)

    df["sector"] = df.apply(
        lambda row: get_stock_sector(row["ticker"])
        if row.get("sector") in ["Unclassified", None, ""]
        else row.get("sector", "Unclassified"),
        axis=1,
    )

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

    def assign_tier_label(score):
        if score >= 70:
            return "⭐ Executive Consensus"
        elif score >= 50:
            return "🟢 High Conviction"
        elif score >= 30:
            return "🟡 Moderate"
        return "⚪ Low"

    df["Conviction Tier"] = df["Conviction Score"].apply(assign_tier_label)

    current_date = pd.Timestamp.now().normalize()

    def format_gain(row):
        profit = row["Unrealized Profit ($)"]
        tx_date = pd.to_datetime(str(row["transaction_date"])[:10])
        if (current_date - tx_date).days <= 2 and profit == 0:
            return "N/A (New)"
        return f"+${profit:,.0f}" if profit >= 0 else f"-${abs(profit):,.0f}"

    def format_return(row):
        ret = row["Return (%)"]
        tx_date = pd.to_datetime(str(row["transaction_date"])[:10])
        if (current_date - tx_date).days <= 2 and ret == 0:
            return "N/A (New)"
        return f"+{ret:.1f}%" if ret >= 0 else f"{ret:.1f}%"

    df["Unrealized Gain"] = df.apply(format_gain, axis=1)
    df["Total Return"] = df.apply(format_return, axis=1)

    def compute_horizon_alpha(row, stock_ret_col, bench_ret_col, days_required):
        s_ret = row.get(stock_ret_col)
        b_ret = row.get(bench_ret_col)
        tx_date = pd.to_datetime(str(row["transaction_date"])[:10])

        if pd.notnull(s_ret) and pd.notnull(b_ret):
            alpha_val = float(s_ret) - float(b_ret)
            return alpha_val, (f"+{alpha_val:.1f}%" if alpha_val >= 0 else f"{alpha_val:.1f}%")

        days_elapsed = (current_date - tx_date).days
        if days_elapsed < days_required:
            return None, "Pending"
        return None, "N/A"

    alpha_60_results = df.apply(
        lambda r: compute_horizon_alpha(r, "return_60d_pct", "bench_return_60d_pct", 60), axis=1
    )
    df["alpha_60d_num"] = [res[0] for res in alpha_60_results]
    df["60D Alpha"] = [res[1] for res in alpha_60_results]

    alpha_180_results = df.apply(
        lambda r: compute_horizon_alpha(r, "return_180d_pct", "bench_return_180d_pct", 180), axis=1
    )
    df["alpha_180d_num"] = [res[0] for res in alpha_180_results]
    df["180D Alpha"] = [res[1] for res in alpha_180_results]

    df["Benchmark Return"] = df["Benchmark Return (%)"].apply(
        lambda x: f"+{x:.1f}%" if x >= 0 else f"{x:.1f}%"
    )
    df["Alpha Return"] = df["Alpha Return (%)"].apply(
        lambda x: f"+{x:.1f}%" if x >= 0 else f"{x:.1f}%"
    )

    return df


@st.cache_data(ttl=600)
def load_filtered_feed(selected_year, selected_month, max_rows):
    conn = get_db_connection()
    try:
        where_clauses = []
        if selected_year == "Older":
            where_clauses.append("LEFT(transaction_date, 4) < '2024'")
        elif selected_year != "All Years":
            where_clauses.append(f"LEFT(transaction_date, 4) = '{selected_year}'")

        months_list = [
            "All Months", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"
        ]
        if selected_month != "All Months":
            month_idx = months_list.index(selected_month)
            month_str = f"{month_idx:02d}"
            where_clauses.append(f"SUBSTRING(transaction_date, 6, 2) = '{month_str}'")

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        query = f"""
            SELECT transaction_date, ticker, sector, insider_name, insider_title, 
                   shares_bought, shares_owned_following, est_value,
                   market_cap, market_cap_category, benchmark_ticker,
                   return_60d_pct, return_180d_pct, bench_return_60d_pct, bench_return_180d_pct
            FROM insider_trades 
            {where_sql}
            ORDER BY transaction_date DESC
            LIMIT {max_rows};
        """
        df = pd.read_sql(query, conn)
    finally:
        conn.close()

    return enrich_dataframe(df)


@st.cache_data(ttl=600)
def load_full_history_for_directory(limit_rows=2500):
    """Fast load using ONLY pre-computed Supabase columns (zero live yfinance calls) so Tab 2 loads instantaneously."""
    conn = get_db_connection()
    try:
        query = f"""
            SELECT transaction_date, ticker, sector, insider_name, insider_title, 
                   shares_bought, shares_owned_following, est_value,
                   market_cap, market_cap_category, benchmark_ticker,
                   return_60d_pct, return_180d_pct, bench_return_60d_pct, bench_return_180d_pct
            FROM insider_trades 
            ORDER BY transaction_date DESC
            LIMIT {limit_rows};
        """
        df = pd.read_sql(query, conn)
    finally:
        conn.close()

    if df.empty:
        return df

    df = df.groupby(["transaction_date", "ticker", "insider_name"], as_index=False).agg({
        "sector": "first",
        "insider_title": "first",
        "shares_bought": "sum",
        "shares_owned_following": "last",
        "est_value": "sum",
        "market_cap": "first",
        "market_cap_category": "first",
        "benchmark_ticker": "first",
        "return_60d_pct": "first",
        "return_180d_pct": "first",
        "bench_return_60d_pct": "first",
        "bench_return_180d_pct": "first",
    })

    df["parsed_date"] = pd.to_datetime(df["transaction_date"].astype(str).str[:10])
    current_date = pd.Timestamp.now().normalize()

    def compute_horizon_alpha(row, stock_ret_col, bench_ret_col, days_required):
        s_ret = row.get(stock_ret_col)
        b_ret = row.get(bench_ret_col)
        if pd.notnull(s_ret) and pd.notnull(b_ret):
            alpha_val = float(s_ret) - float(b_ret)
            return alpha_val, (f"+{alpha_val:.1f}%" if alpha_val >= 0 else f"{alpha_val:.1f}%")
        days_elapsed = (current_date - row["parsed_date"]).days
        return None, ("Pending" if days_elapsed < days_required else "N/A")

    a60 = df.apply(lambda r: compute_horizon_alpha(r, "return_60d_pct", "bench_return_60d_pct", 60), axis=1)
    df["alpha_60d_num"] = [x[0] for x in a60]
    df["60D Alpha"] = [x[1] for x in a60]

    a180 = df.apply(lambda r: compute_horizon_alpha(r, "return_180d_pct", "bench_return_180d_pct", 180), axis=1)
    df["alpha_180d_num"] = [x[0] for x in a180]
    df["180D Alpha"] = [x[1] for x in a180]

    return df


def filter_out_institutions(df, hide_flag):
    if not hide_flag or df.empty:
        return df
    fund_keywords = [
        "LLC", "L.P.", "PARTNERS", "FUNDS", "CAPITAL", "INVESTMENT", "MANAGEMENT"
    ]
    pattern = "|".join(fund_keywords)
    return df[
        ~df["insider_name"].str.upper().str.contains(pattern, na=False)
    ].reset_index(drop=True)


st.title("📊 Insider Transactions Dashboard")
st.write(
    "Market-wide tracking of high-conviction open-market executive purchases ($50,000+)."
)

st.sidebar.header("Filter Dashboard")
selected_year = st.sidebar.selectbox(
    "Filter by Year (Live Feed)", ["All Years", "2026", "2025", "2024", "Older"]
)

months = [
    "All Months", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
selected_month = st.sidebar.selectbox("Filter by Month (Live Feed)", months, index=0)

max_rows = st.sidebar.slider(
    "Max Rows to Load (Live Feed)", 50, 1000, 200, step=50
)

hide_institutions = st.sidebar.checkbox(
    "Hide Institutional / Fund Entities", value=False
)

df_feed_raw = load_filtered_feed(selected_year, selected_month, max_rows)
df = filter_out_institutions(df_feed_raw, hide_institutions)

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
st.sidebar.markdown("### 📖 Signal & Tier Legend")
st.sidebar.markdown("""
- **⭐ Executive Consensus (70+)**: Top-tier cluster buying & skin-in-the-game.
- **🟢 High Conviction (50-69)**: Strong standalone executive purchase.
- **🔥** **Cluster Consensus** (2+ distinct C-Suite/execs buying within 30 days)
- **⚡** **Operational Leadership** (CEO, CFO, or COO purchase >= $100k)
- **🚀** **Skin-in-the-Game** (>= 25% personal position expansion, >= $50k)
""")

if not df.empty:
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 💾 Export Data")
    csv_export = df.to_csv(index=False).encode("utf-8")
    st.sidebar.download_button(
        label="📥 Download Current Feed (CSV)",
        data=csv_export,
        file_name="insider_trades_filtered.csv",
        mime="text/csv",
    )

tab1, tab2 = st.tabs([
    "📊 Live Transactions Feed",
    "🕵️ Recent Buyers & Track Record",
])

with tab1:
    if not df.empty:
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
            "Conviction Tier",
            "High Conviction Flag",
            "ticker",
            "sector",
            "insider_name",
            "insider_title",
            "Shares Bought",
            "Est. Value ($)",
            "Unrealized Gain",
            "Total Return",
            "60D Alpha",
            "180D Alpha",
            "Market Cap at Tx",
            "market_cap_category",
            "benchmark_ticker",
            "Benchmark Return",
            "Alpha Return",
        ]]

        display_df.columns = [
            "Transaction Date",
            "Score",
            "Tier",
            "Signals",
            "Ticker",
            "Sector",
            "Insider Name",
            "Title",
            "Shares Bought",
            "Est. Value ($)",
            "Unrealized Gain (Inc. Divs)",
            "Total Return",
            "60D Alpha",
            "180D Alpha",
            "Market Cap at Tx",
            "Market Cap Tier",
            "Benchmark",
            "Benchmark Return",
            "Alpha vs Benchmark",
        ]

        event = st.dataframe(
            display_df,
            use_container_width=True,
            on_select="rerun",
            selection_mode="single-row",
            key="insider_table_selection"
        )

        selected_indices = event.get("selection", {}).get("rows", [])

        st.markdown("### 🔍 Inspect Score Breakdown")
        if len(selected_indices) > 0:
            row_idx = selected_indices[0]
            selected_row = display_source.iloc[row_idx]

            st.write(f"**Selected:** {selected_row['ticker']} — {selected_row['insider_name']} ({str(selected_row['transaction_date'])[:10]} | Score: {selected_row['Conviction Score']})")

            col_a, col_b = st.columns([1, 2])
            with col_a:
                st.metric("Conviction Score", f"{selected_row['Conviction Score']} / 100")
                st.write(f"**Tier:** {selected_row['Conviction Tier']}")
                st.write(f"**Signals:** {selected_row['High Conviction Flag']}")
                st.write(f"**60D Alpha:** {selected_row['60D Alpha']} | **180D Alpha:** {selected_row['180D Alpha']}")
            with col_b:
                st.markdown("**Scoring Audit Trail:**")
                components = selected_row["Score Breakdown"].split(" | ")
                for comp in components:
                    st.markdown(f"- {comp}")
        else:
            st.info("👆 Click any row in the table above to inspect its exact scoring breakdown and audit trail.")
    else:
        st.warning("No transactions found for the selected filters.")

with tab2:
    st.subheader("🕵️ Recent Buyers & Historical Track Record")
    st.write("Sorted by **Most Recent Purchase** so you can vet active buyers against their matured 60D & 180D benchmark alpha.")

    dir_raw = load_full_history_for_directory(limit_rows=2500)
    dir_source = filter_out_institutions(dir_raw, hide_institutions)

    if not dir_source.empty:
        search_query = st.text_input("🔍 Search by Purchaser Name or Ticker:", "", key="dir_search")

        grouped_records = []
        for insider, grp in dir_source.groupby("insider_name"):
            grp_sorted = grp.sort_values(by="parsed_date", ascending=False)
            latest_tx_date = str(grp_sorted["transaction_date"].iloc[0])[:10]
            latest_ticker = grp_sorted["ticker"].iloc[0]
            all_tickers = ", ".join(grp_sorted["ticker"].unique())
            total_trades = len(grp_sorted)
            total_invested = grp_sorted["est_value"].sum()

            matured_60 = grp_sorted["alpha_60d_num"].dropna()
            matured_180 = grp_sorted["alpha_180d_num"].dropna()

            avg_60_alpha = matured_60.mean() if not matured_60.empty else None
            avg_180_alpha = matured_180.mean() if not matured_180.empty else None
            matured_trade_count = len(grp_sorted[grp_sorted["alpha_60d_num"].notnull() | grp_sorted["alpha_180d_num"].notnull()])

            if matured_trade_count < 2:
                rating = "⏳ No Mature Data"
            else:
                valid_horizon_means = [x for x in [avg_60_alpha, avg_180_alpha] if x is not None]
                blended_horizon_alpha = sum(valid_horizon_means) / len(valid_horizon_means)

                if blended_horizon_alpha >= 5.0:
                    rating = "🏆 Strong Performer"
                elif blended_horizon_alpha >= -2.0:
                    rating = "⚖️ Medium Performer"
                else:
                    rating = "📉 Poor Performer"

            grouped_records.append({
                "Latest Purchase": latest_tx_date,
                "Insider Name": insider,
                "Track Record Rating": rating,
                "Latest Ticker": latest_ticker,
                "All Tickers": all_tickers,
                "Total Trades": total_trades,
                "Matured Trades": matured_trade_count,
                "Avg 60D Alpha": f"+{avg_60_alpha:.1f}%" if avg_60_alpha is not None and avg_60_alpha >= 0 else (f"{avg_60_alpha:.1f}%" if avg_60_alpha is not None else "N/A"),
                "Avg 180D Alpha": f"+{avg_180_alpha:.1f}%" if avg_180_alpha is not None and avg_180_alpha >= 0 else (f"{avg_180_alpha:.1f}%" if avg_180_alpha is not None else "N/A"),
                "Total Invested ($)": f"${total_invested:,.0f}",
            })

        agg_df = pd.DataFrame(grouped_records)

        if search_query:
            q = search_query.upper()
            agg_df = agg_df[
                agg_df["Insider Name"].str.upper().str.contains(q, na=False) |
                agg_df["All Tickers"].str.upper().str.contains(q, na=False)
            ]

        agg_df = agg_df.sort_values(by="Latest Purchase", ascending=False).reset_index(drop=True)

        p_event = st.dataframe(
            agg_df,
            use_container_width=True,
            on_select="rerun",
            selection_mode="single-row",
            key="purchaser_table_selection"
        )

        p_selected_indices = p_event.get("selection", {}).get("rows", [])
        if len(p_selected_indices) > 0:
            chosen_row = agg_df.iloc[p_selected_indices[0]]
            chosen_purchaser = chosen_row["Insider Name"]

            st.markdown("---")
            st.markdown(f"### 📄 Full Trade History & Horizon Alpha: {chosen_purchaser}")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Track Record Rating", chosen_row["Track Record Rating"])
            m2.metric("Matured Trades (60D+)", f"{chosen_row['Matured Trades']} of {chosen_row['Total Trades']}")
            m3.metric("Avg 60D Alpha vs Benchmark", chosen_row["Avg 60D Alpha"])
            m4.metric("Avg 180D Alpha vs Benchmark", chosen_row["Avg 180D Alpha"])

            # Enrich ONLY the selected buyer's trades on demand (fast!)
            raw_buyer_df = dir_source[dir_source["insider_name"] == chosen_purchaser].copy()
            enriched_buyer_df = enrich_dataframe(raw_buyer_df).sort_values(by="parsed_date", ascending=False)

            purchaser_trades = enriched_buyer_df[[
                "transaction_date", "ticker", "Conviction Score", "Conviction Tier", 
                "Shares Bought", "Est. Value ($)", "Total Return", "Alpha Return", "60D Alpha", "180D Alpha"
            ]]
            purchaser_trades.columns = [
                "Transaction Date", "Ticker", "Score", "Tier", 
                "Shares Bought", "Est. Value ($)", "Current Return", "Current Alpha", "60D Alpha", "180D Alpha"
            ]
            st.dataframe(purchaser_trades, use_container_width=True)
        else:
            st.info("👆 Click any recent buyer row above to inspect their full trade history, 60D/180D benchmark alpha, and track record scorecard.")
    else:
        st.warning("No historical data available.")
