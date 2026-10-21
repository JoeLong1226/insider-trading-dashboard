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
        SELECT ticker, sector, insider_name, shares_bought, estimated_value, activity_type, ai_summary 
        FROM insider_buys
        ORDER BY id DESC
    """, conn)
    
    if df.empty:
        st.warning("No insider buys found in the database yet.")
    else:
        # Sidebar Filters
        st.sidebar.header("🔍 Filter Options")
        sectors = ["All"] + list(df["sector"].unique())
        selected_sector = st.sidebar.selectbox("Filter by Sector", sectors)
        
        if selected_sector != "All":
            df_filtered = df[df["sector"] == selected_sector]
        else:
            df_filtered = df
            
        # Top Metrics Row
        total_value = df_filtered["estimated_value"].sum()
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Tracked Buys", len(df_filtered))
        col2.metric("Unique Tickers", df_filtered["ticker"].nunique())
        col3.metric("Sectors Monitored", df_filtered["sector"].nunique())
        col4.metric("Total Capital Invested", f"${total_value:,.2f}" if total_value > 0 else "N/A")
        
        st.divider()
        
        # Format Data Frame for Clean Display
        st.subheader("📋 Insider Transactions Database")
        df_display = df_filtered.copy()
        df_display["shares_bought"] = df_display["shares_bought"].apply(lambda x: f"+{x:,}")
        df_display["estimated_value"] = df_display["estimated_value"].apply(lambda x: f"${x:,.2f}" if x > 0 else "N/A")
        
        st.dataframe(
            df_display, 
            column_config={
                "ticker": "Ticker",
                "sector": "Sector",
                "insider_name": "Insider Name",
                "shares_bought": "Shares Bought",
                "estimated_value": "Est. Value ($)",
                "activity_type": "Activity Type",
                "ai_summary": "AI Summary"
            },
            use_container_width=True, 
            hide_index=True
        )
        
        # AI Takeaways Section
        st.divider()
        st.subheader("💡 AI Takeaways & Executive Insights")
        
        for idx, row in df_filtered.iterrows():
            val_str = f" | Total Value: ${row['estimated_value']:,.2f}" if row['estimated_value'] > 0 else ""
            with st.expander(f"📌 {row['ticker']} — {row['insider_name']} (+{row['shares_bought']:,} shares{val_str})"):
                st.write(f"**Sector:** {row['sector']}")
                st.write(f"**Activity Type:** {row['activity_type']}")
                st.info(f"**AI Takeaway:**\n\n{row['ai_summary']}")

except Exception as e:
    st.error(f"Database connection error: {e}")
