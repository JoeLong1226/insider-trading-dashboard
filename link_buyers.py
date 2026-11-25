#!/usr/bin/env python3
"""Runs link_buyers.sql (gives every visible trade row a buyer_id). Safe to re-run."""
import os
import sys
import psycopg2

url = os.environ.get("SUPABASE_DB_URL")
if not url:
    sys.exit("Missing SUPABASE_DB_URL environment variable.")
sql = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "link_buyers.sql"), encoding="utf-8").read()
conn = psycopg2.connect(url)
try:
    cur = conn.cursor()
    cur.execute(sql)                      # one transaction: all passes or nothing
    visible, linked, unlinked, unlinked_before_oct = cur.fetchone()
    conn.commit()
    print(f"visible rows {visible}, linked {linked}, unlinked {unlinked} "
          f"(of which before Oct 2026: {unlinked_before_oct})")
finally:
    conn.close()
