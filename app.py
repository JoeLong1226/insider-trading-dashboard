import streamlit as st
import psycopg2
import pandas as pd

st.set_page_config(page_title="Insider Trading Dashboard", page_icon="📈", layout="wide")

st.title("🚀 Insider Trading Intelligence Platform")
st.markdown("Real-time tracking and AI-powered analysis of high-conviction open-market insider cash purchases.")

@st.cache_resource
def init_connection():
    return psycopg2.connect(st.secrets["SUPABASE_DB_URL"])

try:
    conn = init_connection()
    df = pd.read_sql("""
        SELECT transaction_date, ticker, sector, insider_name, shares_bought, estimated_value, activity_type, ai_summary 
        FROM insider_buys 
        ORDER BY transaction_date DESC NULLS LAST, estimated_value DESC
    """, conn)

    if df.empty:
        st.warning("No insider buys found in the database yet.")
    else:
        df["estimated_value"] = df["estimated_value"].fillna(0).astype(float)
        df["shares_bought"] = df["shares_bought"].fillna(0).astype(int)

        st.sidebar.header("🔍 Filter Options")
        sectors = ["All"] + list(df["sector"].unique())
        selected_sector = st.sidebar.selectbox("Filter by Sector", sectors)
        min_value = st.sidebar.slider("Min Transaction Value ($)", min_value=0, max_value=10000000, value=25000, step=25000)

        df_filtered = df[df["estimated_value"] >= min_value]
        if selected_sector != "All":
            df_filtered = df_filtered[df_filtered["sector"] == selected_sector]

        total_value = int(round(df_filtered["estimated_value"].sum()))
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Tracked Buys", len(df_filtered))
        col2.metric("Unique Tickers", df_filtered["ticker"].nunique())
        col3.metric("Sectors Monitored", df_filtered["sector"].nunique())
        col4.metric("Total Capital Invested", f"${total_value:,}")

        st.divider()

        st.subheader("📋 Insider Transactions Database")
        
        df_display = df_filtered.copy()
        df_display["shares_bought"] = df_display["shares_bought"].apply(lambda x: f"+{x:,}")
        df_display["estimated_value"] = df_display["estimated_value"].apply(lambda x: f"${int(round(x)):,}")

        st.dataframe(
            df_display,
            column_config={
                "transaction_date": "Transaction Date",
                "ticker": "Ticker",
                "sector": "Sector",
                "insider_name": "Insider Name",
                "shares_bought": "Shares Bought",
                "estimated_value": "Est. Value ($)",
                "activity_type": "Activity Type",
                "ai_summary": "AI Summary / Executive Takeaway"
            },
            use_container_width=True,
            hide_index=True
        )

        st.divider()
        st.subheader("💡 AI Takeaways & Executive Insights")
        for idx, row in df_filtered.iterrows():
            val_int = int(round(row['estimated_value']))
            shares_str = row['shares_bought']
            tx_date_str = row['transaction_date']
            with st.expander(f"📌 {row['ticker']} — {row['insider_name']} ({shares_str} shares on {tx_date_str} | ${val_int:,})"):
                st.write(f"**Transaction Date:** {tx_date_str}")
                st.write(f"**Sector:** {row['sector']}")
                st.write(f"**Activity Type:** {row['activity_type']}")
                st.info(f"**AI Takeaway:**\n\n{row['ai_summary']}")

except Exception as e:
    st.error(f"Database connection error: {e}")
