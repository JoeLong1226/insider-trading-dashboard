#!/usr/bin/env python3
"""
enrich_new_rows.py - fill market cap, size tier, benchmark and sector on rows that were added by
sec_backfill.sql (they arrive with market_cap empty and tier 'Unknown').

Same method as etl.py: price on the transaction date x shares outstanding, so rows added here are
comparable with every other row. Nothing is inserted, and nothing already filled is overwritten.

Usage
  python enrich_new_rows.py          # only rows added by sec_backfill.sql that are still empty
  python enrich_new_rows.py --all    # ANY visible row since 2025-11-01 whose market cap is empty

Safe to re-run: it picks up exactly the rows that are still empty (so lookups that failed
because of a Yahoo hiccup are retried, and finished rows are skipped). Progress is saved every
25 rows, so a stopped run loses nothing.
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta

BACKFILL_MARK = "2026-11-24 00:00:00+00"
yf = None          # imported in main() so the logic can be tested without network


def assign_market_cap_tier(v):
    if not v or v <= 0:
        return "Unknown", None
    if v < 2_000_000_000:
        return "Micro/Small", "IWM"
    if v <= 10_000_000_000:
        return "Mid", "IJH"
    return "Large", "SPY"


def point_in_time_market_cap(ticker, tx_date_str):
    """(market_cap, tier, benchmark) as of the transaction date, or (None, 'Unknown', None)."""
    import pandas as pd
    if not ticker or not re.match(r"^[A-Z\-\.]{1,6}$", ticker):
        return None, "Unknown", None
    try:
        tx = datetime.strptime(tx_date_str[:10], "%Y-%m-%d").date()
        t = yf.Ticker(ticker)
        hist = t.history(start=str(tx - timedelta(days=5)), end=str(tx + timedelta(days=5)))
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None, "Unknown", None
        hist.index = hist.index.date
        if tx in hist.index:
            price = float(hist.loc[tx, "Close"])
        else:
            diff = abs(pd.to_datetime(hist.index) - pd.to_datetime(tx))
            price = float(hist["Close"].iloc[list(diff).index(min(diff))])

        shares = None
        try:   # historical share count first (no look-ahead)
            sf = t.get_shares_full(start=str(tx - timedelta(days=180)), end=str(tx))
            if sf is not None and not sf.empty:
                shares = float(sf.iloc[-1])
        except Exception:
            pass
        if not shares or shares <= 0:   # fallback: current share count (same as etl.py)
            try:
                shares = float(t.fast_info["shares"])
            except Exception:
                shares = None
        if not shares or shares <= 0:
            return None, "Unknown", None
        mcap = price * shares
        cat, bench = assign_market_cap_tier(mcap)
        return mcap, cat, bench
    except Exception:
        return None, "Unknown", None


def lookup_sector(ticker):
    try:
        s = yf.Ticker(ticker).info.get("sector")
        return s.strip() if s and s.strip() else None
    except Exception:
        return None


SELECT_SQL = """
    SELECT id, ticker, transaction_date::text, sector
    FROM insider_trades
    WHERE is_canonical AND cardinality(dq_hard) = 0
      AND market_cap IS NULL
      AND transaction_date >= DATE '2025-11-01'
      AND (%s OR created_at = %s::timestamptz)
    ORDER BY ticker, transaction_date
"""

UPDATE_SQL = """
    UPDATE insider_trades SET
        market_cap          = COALESCE(market_cap, %s),
        market_cap_category = COALESCE(%s, market_cap_category),
        benchmark_ticker    = COALESCE(benchmark_ticker, %s),
        sector              = CASE WHEN COALESCE(sector, '') IN ('', 'Unclassified') AND %s IS NOT NULL
                                   THEN %s ELSE sector END
    WHERE id = %s
"""


def enrich(cur, conn, all_rows=False, sleep=0.2, log=print):
    cur.execute(SELECT_SQL, (all_rows, BACKFILL_MARK))
    rows = cur.fetchall()
    log(f"{len(rows)} rows to enrich ({'all visible rows' if all_rows else 'added by sec_backfill.sql'}).")
    cap_cache, sector_cache = {}, {}
    done = filled = 0
    for rid, ticker, tx, sector in rows:
        key = (ticker, tx[:10])
        if key not in cap_cache:
            cap_cache[key] = point_in_time_market_cap(ticker, tx)
            time.sleep(sleep)
        mcap, cat, bench = cap_cache[key]
        sec = None
        if (sector or "") in ("", "Unclassified"):
            if ticker not in sector_cache:
                sector_cache[ticker] = lookup_sector(ticker)
            sec = sector_cache[ticker]
        cur.execute(UPDATE_SQL, (mcap, cat if mcap else None, bench, sec, sec, rid))
        done += 1
        filled += 1 if mcap else 0
        if done % 25 == 0:
            conn.commit()
        if done % 100 == 0:
            log(f"  {done}/{len(rows)} rows done, {filled} with a market cap")
    conn.commit()
    log(f"Finished: {done} rows processed, {filled} got a market cap, {done - filled} still unknown "
        f"(no Yahoo data: delisted, foreign, or no share count).")
    return done, filled


def main():
    global yf
    import psycopg2
    import yfinance
    yf = yfinance
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        sys.exit("Missing SUPABASE_DB_URL environment variable.")
    conn = psycopg2.connect(url)
    try:
        enrich(conn.cursor(), conn, all_rows="--all" in sys.argv)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
