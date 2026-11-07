import os
import psycopg2

conn_url = os.environ.get("SUPABASE_DB_URL")
conn = psycopg2.connect(conn_url)
cursor = conn.cursor()

columns = [
    "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS market_cap BIGINT;",
    "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS market_cap_category TEXT;",
    "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS benchmark_ticker TEXT;",
    "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS ret_stock_after NUMERIC(8,4);",
    "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS ret_benchmark_after NUMERIC(8,4);",
    "ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS alpha_after NUMERIC(8,4);"
]

for col_sql in columns:
    print(f"Executing: {col_sql}")
    cursor.execute(col_sql)
    conn.commit()

cursor.close()
conn.close()
print("All columns added successfully!")
