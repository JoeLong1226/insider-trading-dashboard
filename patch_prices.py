"""
Fills 60- and 180-day forward prices and returns for rows in insider_trades.

Safe to run every day:
  * only touches rows whose 60d / 180d horizon has actually passed and is still empty
  * never overwrites an existing value with NULL (a yfinance hiccup can't wipe good data)
  * stock and benchmark returns come from the SAME kind of price series, so they are comparable

Usage:
  python patch_prices.py                         # every row that still has a matured, empty horizon
  python patch_prices.py 2026-01-01 2026-03-31   # limit to a transaction-date window
  python patch_prices.py --force [START [END]]   # recompute even if already filled (use once to fix old values)

New columns it creates (if missing):
  return_60d_pct, return_180d_pct              stock price return after 60 / 180 calendar days
  bench_return_60d_pct, bench_return_180d_pct  benchmark (your benchmark_ticker, SPY if blank) over the same dates
  alpha = return - bench_return  (compute it in SQL / the app)
"""
import os
import re
import sys
import time
from bisect import bisect_left
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import yfinance as yf

SUPABASE_URL = os.environ.get("SUPABASE_DB_URL")
EASTERN = ZoneInfo("America/New_York")
DEFAULT_BENCHMARK = "SPY"  # used when a row has no benchmark_ticker


def get_db_connection():
    if not SUPABASE_URL:
        raise ValueError("Missing database connection string. Set SUPABASE_DB_URL.")
    return psycopg2.connect(SUPABASE_URL)


def clean_ticker_for_yf(ticker):
    if not ticker:
        return ""
    clean = str(ticker).strip().upper()
    clean = clean.replace('"', '').replace("'", "").replace('(', '').replace(')', '').replace('[', '').replace(']', '')
    if ":" in clean:
        clean = clean.split(":")[-1].strip()
    clean = re.sub(r'[\s]', '', clean)
    if "," in clean:
        clean = clean.split(",")[0].strip()
    elif "/" in clean:
        clean = clean.split("/")[0].strip()
    return {"LBTY": "LBTYA", "GOOG": "GOOGL", "BRK.B": "BRK-B"}.get(clean, clean)


class Prices:
    """Daily closes for one symbol with 'first close on/after a date' lookups."""

    def __init__(self, hist):
        hist = hist.dropna(subset=["Close"])
        idx = pd.to_datetime(hist.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        self.dates = [d.date() for d in idx]
        self.closes = [float(c) for c in hist["Close"]]

    def on_or_after(self, target, max_gap_days=7):
        """Close of the first trading day on/after `target`; None if no data within `max_gap_days`
        (e.g. the stock stopped trading, or the date hasn't happened yet)."""
        i = bisect_left(self.dates, target)
        if i >= len(self.dates):
            return None
        if (self.dates[i] - target).days > max_gap_days:
            return None
        return self.closes[i]


def download_prices(symbol, start, end):
    """One yfinance call per symbol. auto_adjust=False -> 'Close' is split-adjusted but NOT dividend-adjusted,
    so it doesn't shift every time a new dividend is paid. Returns Prices or None."""
    for attempt in range(2):
        try:
            hist = yf.Ticker(symbol).history(start=str(start), end=str(end), auto_adjust=False)
            if hist is not None and not hist.empty and "Close" in hist.columns:
                return Prices(hist)
        except Exception as e:
            print(f"  yfinance error for {symbol}: {e}")
        time.sleep(2)
    return None


def pct(new, old):
    if new is None or old is None or old <= 0:
        return None
    return round((new / old - 1.0) * 100.0, 2)


def rnd(x, n=4):
    return round(x, n) if x is not None else None


def ensure_columns(cursor, conn):
    for col in ("price_at_tx", "price_60d_after", "price_180d_after",
                "return_60d_pct", "return_180d_pct",
                "bench_return_60d_pct", "bench_return_180d_pct"):
        cursor.execute(f"ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS {col} NUMERIC;")
    conn.commit()


UPDATE_QUERY = """
    UPDATE insider_trades SET
        price_at_tx           = COALESCE(price_at_tx, %s),
        price_60d_after       = COALESCE(%s, price_60d_after),
        price_180d_after      = COALESCE(%s, price_180d_after),
        return_60d_pct        = COALESCE(%s, return_60d_pct),
        return_180d_pct       = COALESCE(%s, return_180d_pct),
        bench_return_60d_pct  = COALESCE(%s, bench_return_60d_pct),
        bench_return_180d_pct = COALESCE(%s, bench_return_180d_pct)
    WHERE id = %s;
"""


def main():
    argv = sys.argv[1:]
    force = "--force" in argv
    args = [a for a in argv if not a.startswith("--")]

    today = datetime.now(EASTERN).date()
    start = args[0] if args else "2000-01-01"
    end = args[1] if len(args) > 1 else today.isoformat()
    cutoff60 = (today - timedelta(days=61)).isoformat()    # 60-day mark must be safely in the past
    cutoff180 = (today - timedelta(days=181)).isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    ensure_columns(cursor, conn)

    tx_col = "LEFT(transaction_date::text, 10)"
    if force:
        need = f"{tx_col} <= %s"
        params = [start, end, cutoff60]
    else:
        need = (f"(return_60d_pct IS NULL AND {tx_col} <= %s) "
                f"OR (return_180d_pct IS NULL AND {tx_col} <= %s)")
        params = [start, end, cutoff60, cutoff180]

    cursor.execute(f"""
        SELECT id, ticker, {tx_col}, est_value, shares_bought, benchmark_ticker
        FROM insider_trades
        WHERE {tx_col} BETWEEN %s AND %s AND ({need});
    """, params)
    rows = cursor.fetchall()
    print(f"{len(rows)} rows need 60d/180d data (window {start} to {end}{', FORCE' if force else ''}).")
    if not rows:
        return

    # group rows by symbol so each ticker is downloaded once
    by_ticker, skipped = {}, 0
    for row_id, ticker, tx_str, est_value, shares, bench in rows:
        sym = clean_ticker_for_yf(ticker)
        if not sym or not re.match(r'^[A-Z0-9\.\-]{1,8}$', sym):
            skipped += 1
            continue
        by_ticker.setdefault(sym, []).append((row_id, tx_str, est_value, shares, bench))

    min_tx = min(date.fromisoformat(r[1]) for rs in by_ticker.values() for r in rs) if by_ticker else today
    window_start = min_tx - timedelta(days=10)
    window_end = today + timedelta(days=1)  # yfinance end date is exclusive

    bench_prices = {}

    def get_bench(sym):
        if sym not in bench_prices:
            bench_prices[sym] = download_prices(sym, window_start, window_end)
        return bench_prices[sym]

    updated = no_data = 0
    for n, (sym, items) in enumerate(sorted(by_ticker.items()), 1):
        tmin = min(date.fromisoformat(i[1]) for i in items) - timedelta(days=10)
        px = download_prices(sym, tmin, window_end)
        if px is None:
            no_data += 1
            print(f"[{n}/{len(by_ticker)}] {sym}: no price data (delisted / bad symbol / rate-limited)")
            time.sleep(0.5)
            continue

        for row_id, tx_str, est_value, shares, bench in items:
            tx = date.fromisoformat(tx_str)
            p0 = px.on_or_after(tx)
            if p0 is None:
                continue
            t60, t180 = tx + timedelta(days=60), tx + timedelta(days=180)
            p60 = px.on_or_after(t60) if t60 < today else None
            p180 = px.on_or_after(t180) if t180 < today else None

            bpx = get_bench((bench or DEFAULT_BENCHMARK).strip().upper()) if (bench or DEFAULT_BENCHMARK) else None
            b0 = bpx.on_or_after(tx) if bpx else None
            b60 = bpx.on_or_after(t60) if (bpx and t60 < today) else None
            b180 = bpx.on_or_after(t180) if (bpx and t180 < today) else None

            official = None
            if est_value and shares and float(shares) > 0:
                official = round(float(est_value) / float(shares), 4)  # actual Form 4 price, kept for display

            try:
                cursor.execute(UPDATE_QUERY, (
                    official, rnd(p60), rnd(p180),
                    pct(p60, p0), pct(p180, p0),
                    pct(b60, b0), pct(b180, b0),
                    row_id,
                ))
                updated += 1
            except Exception as db_err:
                conn.rollback()
                print(f"DB update error for id {row_id}: {db_err}")
        conn.commit()
        print(f"[{n}/{len(by_ticker)}] {sym}: {len(items)} row(s)")
        time.sleep(0.3)

    cursor.close()
    conn.close()
    print(f"--- Done. Updated {updated} rows; {no_data} tickers with no price data; {skipped} rows skipped (bad ticker). ---")
    if by_ticker and no_data == len(by_ticker):
        sys.exit(1)  # nothing worked at all -> make the run turn red


if __name__ == "__main__":
    main()
