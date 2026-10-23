import streamlit as st
import pandas as pd
import psycopg2

# Set Streamlit Page Config
st.set_page_config(page_title="Insider Trading Dashboard", page_icon="📈", layout="wide")

# Fetch data from Supabase
@st.cache_data(ttl=60)
def load_data():
    conn_url = st.secrets["SUPABASE_DB_URL"]
    conn = psycopg2.connect(conn_url)
    
    query = """
        SELECT transaction_date, ticker, sector, insider_name, shares_bought, est_value, activity_type, ai_summary 
        FROM insider_trades 
        ORDER BY transaction_date DESC;
    """
    df = pd.read_sql(query, conn)
    conn.close()
    return df

# Main Title & Subheader
st.title("📈 Insider Trading Intelligence Dashboard")
st.markdown("Market-wide tracking of high-conviction open-market executive purchases ($50,000+).")

# Load dataset
try:
    df = load_data()

    if df.empty:
        st.warning("No transactions found in database.")
    else:
        # Metrics Display
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Trades Tracked", len(df))
        col2.metric("Unique Tickers", df["ticker"].nunique())
        col3.metric("Unique Insiders", df["insider_name"].nunique())
        col4.metric("Total Dollar Volume", f"${df['est_value'].sum():,.0f}")

        st.divider()

        # Display Data Table
        st.subheader("📋 Insider Transactions Database")
        
        # Format columns for display
        df_display = df.copy()
        df_display["shares_bought"] = df_display["shares_bought"].apply(lambda x: f"+{x:,.0f}")
        df_display["est_value"] = df_display["est_value"].apply(lambda x: f"${x:,.0f}")

        st.dataframe(
            df_display.rename(columns={
                "transaction_date": "Transaction Date",
                "ticker": "Ticker",
                "sector": "Sector",
                "insider_name": "Insider Name",
                "shares_bought": "Shares Bought",
                "est_value": "Est. Value ($)",
                "activity_type": "Activity Type",
                "ai_summary": "AI Summary / Executive Takeaway"
            }),
            use_container_width=True,
            hide_index=True
        )

except Exception as e:
    st.error(f"Failed to connect to database: {e}")
