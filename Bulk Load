"""
Bulk history loader: SEC "Insider Transactions Data Sets" (quarterly Form 3/4/5 files) -> Supabase.

Same rules as etl.py:
  * open-market purchases only (code P, acquired A), summed per insider per transaction date
  * keep rows >= $50,000, first reporting owner on the filing is the insider
  * a Form 4/A replaces earlier rows for the same ticker + insider + transaction date
  * never creates a duplicate: if ticker + insider + transaction date already exists, the original is skipped

Usage (run from the repo folder that contains etl.py):
  python bulk_load.py 2025q1 --dry-run            # parse only, print counts + sample rows, touch nothing
  python bulk_load.py 2025q1 2025q2 2025q3 2025q4 # load for real (quarters are processed oldest-first)
  python bulk_load.py 2024q1 --dir C:\\sec_zips    # use zips you downloaded yourself
  python bulk_load.py 2024q1 --no-market-data     # skip yfinance (sector guessed from name, market cap 'Unknown')

It also adds a filing_date column (date the SEC received the filing) for the rows it inserts.
"""
import argparse
import csv
import os
import re
import sys
import time
import zipfile
from datetime import timedelta

import pandas as pd
import requests
import yfinance as yf
from psycopg2.extras import execute_values

import etl  # reuses your ticker cleaning, sector lookup and market-cap tiers

URLS = [
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip",
    "https://dcm.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip",
]

# canonical column -> other spellings we will accept
ALIASES = {
    "TRANS_ACQUIRED_DISP_CD": ["TRANS_ACQUIRED_DISP_CODE", "TRANS_ACQUIRED_DISP_CD"],
    "SHRS_OWND_FOLWNG_TRANS": ["SHRS_OWND_FOLWNG_TRANS", "SHARES_OWNED_FOLLOWING_TRANS"],
}


# ---------------------------------------------------------------------------
# Getting + reading the SEC files
# ---------------------------------------------------------------------------
def get_zip(q, folder):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{q}_form345.zip")
    if os.path.exists(path) and zipfile.is_zipfile(path):
        return path
    for tmpl in URLS:
        url = tmpl.format(q=q)
        try:
            r = requests.get(url, headers=etl.HEADERS, timeout=120, stream=True)
            if r.status_code == 200:
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                if zipfile.is_zipfile(path):
                    print(f"  downloaded {url}")
                    return path
                os.remove(path)
            print(f"  {url} -> HTTP {r.status_code}")
        except requests.RequestException as e:
            print(f"  {url} -> {e}")
        time.sleep(1)
    return None


def read_table(zf, basename, wanted, required):
    name = next((n for n in zf.namelist() if os.path.basename(n).upper() == basename), None)
    if name is None:
        raise RuntimeError(f"{basename} not found in the zip. Files inside: {zf.namelist()}")
    wanted_set = set(wanted)
    for canon, alts in ALIASES.items():
        if canon in wanted_set:
            wanted_set.update(alts)
    with zf.open(name) as f:
        df = pd.read_csv(f, sep="\t", dtype=str, quoting=csv.QUOTE_NONE, encoding="utf-8",
                         encoding_errors="replace", on_bad_lines="warn",
                         usecols=lambda c: c.strip().upper() in wanted_set)
    df.columns = [c.strip().upper() for c in df.columns]
    for canon, alts in ALIASES.items():
        if canon not in df.columns:
            for alt in alts:
                if alt in df.columns:
                    df = df.rename(columns={alt: canon})
                    break
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"{basename}: missing expected column(s) {missing}. Columns found: {list(df.columns)}")
    for c in wanted:
        if c not in df.columns:
            df[c] = ""
    return df


def parse_dates(s):
    out = pd.to_datetime(s, format="%d-%b-%Y", errors="coerce")
    rest = out.isna() & s.notna()
    if rest.any():
        out[rest] = pd.to_datetime(s[rest], errors="coerce")
    return out


def load_quarter(zip_path):
    """Returns (agg, stats). agg = one row per filing + transaction date with purchase totals."""
    with zipfile.ZipFile(zip_path) as zf:
        sub = read_table(zf, "SUBMISSION.TSV",
                         ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERNAME",
                          "ISSUERTRADINGSYMBOL", "REMARKS", "AFF10B5ONE"],
                         ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERTRADINGSYMBOL"])
        own = read_table(zf, "REPORTINGOWNER.TSV",
                         ["ACCESSION_NUMBER", "RPTOWNERNAME", "RPTOWNER_TITLE"],
                         ["ACCESSION_NUMBER", "RPTOWNERNAME"])
        nd = read_table(zf, "NONDERIV_TRANS.TSV",
                        ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
                         "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD", "SHRS_OWND_FOLWNG_TRANS"],
                        ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
                         "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"])

    stats = {"filings_4": 0, "purchase_lines": 0}
    sub["DOCUMENT_TYPE"] = sub["DOCUMENT_TYPE"].str.strip()
    sub = sub[sub["DOCUMENT_TYPE"].isin(["4", "4/A"])]
    stats["filings_4"] = len(sub)

    nd = nd[(nd["TRANS_CODE"].str.strip().str.upper() == "P")
            & (nd["TRANS_ACQUIRED_DISP_CD"].str.strip().str.upper() == "A")].copy()
    stats["purchase_lines"] = len(nd)
    nd["shares"] = pd.to_numeric(nd["TRANS_SHARES"], errors="coerce").fillna(0.0)
    nd["price"] = pd.to_numeric(nd["TRANS_PRICEPERSHARE"], errors="coerce").fillna(0.0)
    nd["owned"] = pd.to_numeric(nd["SHRS_OWND_FOLWNG_TRANS"], errors="coerce").fillna(0.0)
    nd["value"] = nd["shares"] * nd["price"]
    nd["tx_dt"] = parse_dates(nd["TRANS_DATE"])
    nd = nd[nd["tx_dt"].notna()].copy()
    nd["tx_date"] = nd["tx_dt"].dt.strftime("%Y-%m-%d")

    own = own.drop_duplicates("ACCESSION_NUMBER", keep="first")  # first reporting owner = the insider
    lines = (nd.merge(sub[["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERNAME",
                           "ISSUERTRADINGSYMBOL", "REMARKS", "AFF10B5ONE"]], on="ACCESSION_NUMBER", how="inner")
               .merge(own[["ACCESSION_NUMBER", "RPTOWNERNAME", "RPTOWNER_TITLE"]], on="ACCESSION_NUMBER", how="left"))

    agg = (lines.groupby(["ACCESSION_NUMBER", "tx_date"], sort=False)
           .agg(shares=("shares", "sum"), value=("value", "sum"), owned=("owned", "last"),
                doc_type=("DOCUMENT_TYPE", "first"), filing_raw=("FILING_DATE", "first"),
                company=("ISSUERNAME", "first"), sym=("ISSUERTRADINGSYMBOL", "first"),
                insider=("RPTOWNERNAME", "first"), title=("RPTOWNER_TITLE", "first"),
                remarks=("REMARKS", "first"), aff=("AFF10B5ONE", "first"))
           .reset_index().rename(columns={"ACCESSION_NUMBER": "acc"}))

    agg["ticker"] = agg["sym"].fillna("").map(etl.clean_ticker_for_yf)
    ok = (agg["ticker"].str.match(r"^[A-Z\-\.]{1,6}$")
          & ~agg["ticker"].isin(["NONE", "UNKNOWN", "N/A"])
          & ~((agg["ticker"].str.len() == 5) & agg["ticker"].str.endswith("X")))
    agg = agg[ok].copy()

    agg["insider"] = agg["insider"].fillna("UNKNOWN").str.strip().replace("", "UNKNOWN")
    agg["title"] = agg["title"].fillna("").str.strip().replace("", "Director/Other")
    agg["company"] = agg["company"].fillna("")
    rem = agg["remarks"].fillna("").str.upper()
    aff = agg["aff"].fillna("").str.strip().str.lower()
    agg["is_10b51"] = rem.str.contains("10B5-1", regex=False) | rem.str.contains("10B51", regex=False) | aff.isin(["1", "true"])
    agg["filing_date"] = parse_dates(agg["filing_raw"]).dt.strftime("%Y-%m-%d")
    return agg, stats


def resolve_amendments(agg):
    """A 4/A replaces everything else for the same ticker + insider + transaction date (latest 4/A wins).
    Amendments are kept even if now under $50k so the old row can still be deleted."""
    key = ["ticker", "insider", "tx_date"]
    agg = agg.sort_values(["filing_date", "acc"]).reset_index(drop=True)
    is_a = agg["doc_type"] == "4/A"
    latest_a = agg[is_a].drop_duplicates(key, keep="last").copy()
    latest_a["is_amendment"] = True
    orig = agg[~is_a].merge(latest_a[key].assign(_hit=1), on=key, how="left")
    orig = orig[orig["_hit"].isna()].drop(columns="_hit").copy()
    orig["is_amendment"] = False
    final = pd.concat([orig, latest_a], ignore_index=True)
    final["qualifies"] = final["value"] >= etl.MIN_VALUE
    return final[final["is_amendment"] | final["qualifies"]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Market data (one set of yfinance calls per ticker, not per row)
# ---------------------------------------------------------------------------
def _naive_index(idx):
    idx = pd.to_datetime(idx)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def market_data_for_ticker(sym, tx_dates):
    """{tx_date_str: (market_cap, category, benchmark)} using the same approach as etl.py."""
    unknown = {d: (None, "Unknown", None) for d in tx_dates}
    try:
        dts = [pd.Timestamp(d) for d in tx_dates]
        dmin, dmax = min(dts), max(dts)
        t = yf.Ticker(sym)
        hist = t.history(start=str((dmin - timedelta(days=7)).date()), end=str((dmax + timedelta(days=8)).date()))
        if hist is None or hist.empty or "Close" not in hist.columns:
            return unknown
        closes = pd.Series(hist["Close"].values, index=_naive_index(hist.index)).dropna()
        closes = closes[~closes.index.duplicated(keep="last")].sort_index()

        shares_s = None
        try:
            sf = t.get_shares_full(start=str((dmin - timedelta(days=180)).date()), end=str((dmax + timedelta(days=1)).date()))
            if sf is not None and len(sf) > 0:
                shares_s = pd.Series(sf.values, index=_naive_index(sf.index)).sort_index()
        except Exception:
            pass
        fallback = None
        fallback_loaded = False

        out = {}
        for d, ts in zip(tx_dates, dts):
            i = closes.index.get_indexer([ts], method="nearest")[0]
            if i < 0 or abs((closes.index[i] - ts).days) > 5:
                out[d] = (None, "Unknown", None)
                continue
            price = float(closes.iloc[i])
            shares = None
            if shares_s is not None:
                win = shares_s[(shares_s.index <= ts) & (shares_s.index >= ts - pd.Timedelta(days=180))]
                if len(win):
                    shares = float(win.iloc[-1])
            if not shares or shares <= 0:
                if not fallback_loaded:
                    fallback_loaded = True
                    try:
                        fallback = float(t.fast_info["shares"])
                    except Exception:
                        fallback = None
                shares = fallback
            if not shares or shares <= 0:
                out[d] = (None, "Unknown", None)
                continue
            mcap = price * shares
            cat, bench = etl.assign_market_cap_tier(mcap)
            out[d] = (mcap, cat, bench)
        return out
    except Exception:
        return unknown


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
INSERT_SQL = """
    INSERT INTO insider_trades (
        transaction_date, ticker, sector, insider_name, insider_title,
        shares_bought, shares_owned_following, est_value, activity_type,
        market_cap, market_cap_category, benchmark_ticker, is_10b51, accession_number, price_at_tx, filing_date
    ) VALUES %s
    ON CONFLICT DO NOTHING;
"""
DELETE_SQL = """
    DELETE FROM insider_trades
    WHERE ticker = %s AND insider_name = %s AND LEFT(transaction_date::text, 10) = %s
      AND accession_number IS DISTINCT FROM %s;
"""


def write_quarter(final, conn, args, stats):
    cur = conn.cursor()
    cur.execute("ALTER TABLE insider_trades ADD COLUMN IF NOT EXISTS filing_date DATE;")
    conn.commit()

    cur.execute("""SELECT ticker, insider_name, LEFT(transaction_date::text, 10)
                   FROM insider_trades WHERE LEFT(transaction_date::text, 10) BETWEEN %s AND %s;""",
                (final["tx_date"].min(), final["tx_date"].max()))
    existing = {(r[0], r[1], r[2]) for r in cur.fetchall()}
    print(f"  {len(existing)} rows already in the table for this date span.")

    tickers = sorted(final["ticker"].unique())
    for n, sym in enumerate(tickers, 1):
        grp = final[final["ticker"] == sym]
        todo = []  # rows we may insert
        for r in grp.itertuples():
            k = (r.ticker, r.insider, r.tx_date)
            if not r.is_amendment and k in existing:
                stats["skipped_existing"] += 1
                continue
            todo.append(r)
        need_meta = [r for r in todo if r.qualifies]
        meta, sector = {}, None
        if need_meta:
            company = need_meta[0].company
            if args.no_market_data:
                sector = etl.infer_sector_from_name(company)
            else:
                sector = etl.get_sector_for_ticker(sym, company)
                meta = market_data_for_ticker(sym, sorted({r.tx_date for r in need_meta}))
                time.sleep(0.25)

        rows, deletes = [], []
        for r in todo:
            if r.is_amendment:
                deletes.append(r)
            if r.qualifies:
                mcap, cat, bench = meta.get(r.tx_date, (None, "Unknown", None))
                avg_price = r.value / r.shares if r.shares else 0.0
                rows.append((r.tx_date, r.ticker, sector, r.insider, r.title, r.shares, r.owned, r.value,
                             "Purchase", float(mcap) if mcap else None, cat, bench, bool(r.is_10b51),
                             r.acc, avg_price, r.filing_date or None))
        try:
            for r in deletes:
                cur.execute(DELETE_SQL, (r.ticker, r.insider, r.tx_date, r.acc))
                stats["replaced"] += cur.rowcount
                existing.discard((r.ticker, r.insider, r.tx_date))
            if rows:
                execute_values(cur, INSERT_SQL, rows, page_size=1000)
                stats["inserted"] += cur.rowcount
                for row in rows:
                    existing.add((row[1], row[3], row[0]))
            conn.commit()
        except Exception as e:
            conn.rollback()
            stats["db_errors"] += 1
            print(f"  DB error on {sym}: {e}")
        if n % 100 == 0:
            print(f"  ...{n}/{len(tickers)} tickers (inserted so far: {stats['inserted']})")
    cur.close()


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("quarters", nargs="+", help="e.g. 2025q1 2025q2")
    ap.add_argument("--dir", default="sec_bulk", help="folder for the SEC zip files")
    ap.add_argument("--dry-run", action="store_true", help="parse and report only; no database, no yfinance")
    ap.add_argument("--no-market-data", action="store_true", help="skip yfinance (market cap 'Unknown')")
    args = ap.parse_args()

    quarters = sorted({q.lower() for q in args.quarters})
    for q in quarters:
        if not re.match(r"^\d{4}q[1-4]$", q):
            sys.exit(f"Bad quarter '{q}'. Use a form like 2025q1.")

    conn = None if args.dry_run else etl.get_db_connection()
    total = {"inserted": 0, "replaced": 0, "skipped_existing": 0, "db_errors": 0}

    for q in quarters:
        print(f"=== {q} ===")
        path = get_zip(q, args.dir)
        if not path:
            sys.exit(f"Could not download {q}. Download {q}_form345.zip from the SEC's 'Insider Transactions "
                     f"Data Sets' page, put it in '{args.dir}', and run again.")
        agg, pstats = load_quarter(path)
        final = resolve_amendments(agg)
        n_amend = int(final["is_amendment"].sum())
        print(f"  Form 4/4A filings: {pstats['filings_4']:,} | purchase lines: {pstats['purchase_lines']:,} | "
              f"rows to consider: {len(final):,} (amendments: {n_amend})")

        if args.dry_run:
            q_ok = final[final["qualifies"]]
            print(f"  qualifying (>= ${etl.MIN_VALUE:,}) rows: {len(q_ok):,}, "
                  f"dollar volume ${q_ok['value'].sum():,.0f}")
            print(q_ok.sort_values("value", ascending=False)
                  [["tx_date", "ticker", "insider", "title", "shares", "value", "doc_type", "acc"]]
                  .head(10).to_string(index=False))
            continue

        stats = {"inserted": 0, "replaced": 0, "skipped_existing": 0, "db_errors": 0}
        if len(final):
            write_quarter(final, conn, args, stats)
        print(f"  --- {q} done: {stats} ---")
        for k in total:
            total[k] += stats[k]

    if conn:
        conn.close()
        print(f"=== All done: {total} ===")
        if total["db_errors"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
