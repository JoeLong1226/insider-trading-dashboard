#!/usr/bin/env python3
"""
load_sec_bulk.py  -  load SEC "Insider Transactions Data Sets" into the v2 tables.

What it does
  1. Downloads the quarterly zips (2025q4_form345.zip, ...) straight from sec.gov.
  2. Keeps Form 4 / 4/A filings, and only open-market PURCHASE lines (code P, acquired).
  3. Keeps a purchase when its day-total within one filing is >= $50,000 (same rule as the site).
  4. Loads three tables, safely re-runnable (the key is accession_number + line_no):
        sec_filings, filing_owners, purchase_lines
  5. Marks a filing "superseded" when a later 4/A restates it (same issuer, period, owners).

It NEVER touches insider_trades. Nothing the site shows changes.

Usage
  python load_sec_bulk.py                              # default quarters, real database
  python load_sec_bulk.py --quarters 2026q3            # one quarter
  python load_sec_bulk.py --dry-run                    # parse + report only, no database
  python load_sec_bulk.py --dry-run --dump out_dir     # also write the staging CSVs
  python load_sec_bulk.py --zip-dir some_dir           # use zips already on disk
"""
import argparse
import csv
import io
import os
import re
import sys
import time
import zipfile

import pandas as pd
import requests

# SEC requires a descriptive User-Agent with a contact email (same as etl.py)
HEADERS = {"User-Agent": "InsiderTradingApp JosephLong1226@gmail.com"}
# The SEC moved these files between folders (2025q4/2026q1 are under /structureddata/,
# newer quarters under /datastandardsinnovation/). Try each in turn.
URLS = [
    "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/{q}_form345.zip",
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip",
]
DEFAULT_QUARTERS = "2025q4,2026q1,2026q2,2026q3"
MIN_VALUE = 50_000

PLACEHOLDER_SYMBOLS = {"", "NONE", "N/A", "N.A.", "NA", "NULL", "[NONE]", "UNKNOWN", "-"}
OFFERING_RE = re.compile(
    r"public offering|private placement|initial public offering|\bIPO\b|underwritten|"
    r"follow-on|registered direct|\bPIPE\b|subscription agreement|securities purchase agreement|"
    r"rights offering|offering price|purchase agreement|concurrent private",
    re.I)
WEIGHTED_RE = re.compile(r"weighted[- ]average", re.I)

# ---------------------------------------------------------------------------
# Database objects (all IF NOT EXISTS: safe to run any number of times)
# ---------------------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS sec_filings (
  accession_number     text PRIMARY KEY,
  form_type            text NOT NULL CHECK (form_type IN ('4','4/A')),
  filing_date          date,
  period_of_report     date,
  issuer_cik           text,
  issuer_name          text,
  ticker               text,
  orig_submission_date date,
  is_10b5_1            boolean,
  superseded_by        text REFERENCES sec_filings(accession_number),
  inserted_at          timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS filing_owners (
  accession_number text REFERENCES sec_filings(accession_number) ON DELETE CASCADE,
  owner_cik        text NOT NULL,
  owner_name       text NOT NULL,
  is_officer       boolean DEFAULT false,
  is_director      boolean DEFAULT false,
  is_ten_pct       boolean DEFAULT false,
  is_other         boolean DEFAULT false,
  officer_title    text,
  other_text       text,
  PRIMARY KEY (accession_number, owner_cik)
);
CREATE TABLE IF NOT EXISTS purchase_lines (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  accession_number        text NOT NULL REFERENCES sec_filings(accession_number) ON DELETE CASCADE,
  line_no                 int  NOT NULL,
  transaction_date        date NOT NULL,
  security_title          text,
  ownership_nature        text,
  shares                  numeric(24,4) NOT NULL,
  price                   numeric(24,4),
  value                   numeric(28,2),
  shares_owned_following  numeric(24,4),
  footnote_flags          text[] DEFAULT '{}',
  UNIQUE (accession_number, line_no)
);
CREATE INDEX IF NOT EXISTS ix_pl_date ON purchase_lines (transaction_date);
CREATE INDEX IF NOT EXISTS ix_sf_ticker ON sec_filings (ticker);
CREATE INDEX IF NOT EXISTS ix_fo_cik ON filing_owners (owner_cik);

-- Supabase exposes public tables through its API. These are internal: lock them.
ALTER TABLE sec_filings    ENABLE ROW LEVEL SECURITY;
ALTER TABLE filing_owners  ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchase_lines ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON sec_filings, filing_owners, purchase_lines FROM anon, authenticated;
"""

STAGING_DDL = """
CREATE TEMP TABLE stg_filings (
  accession_number text, form_type text, filing_date text, period_of_report text,
  issuer_cik text, issuer_name text, ticker text, orig_submission_date text, is_10b5_1 text
) ON COMMIT DROP;
CREATE TEMP TABLE stg_owners (
  accession_number text, owner_cik text, owner_name text, is_officer text, is_director text,
  is_ten_pct text, is_other text, officer_title text, other_text text
) ON COMMIT DROP;
CREATE TEMP TABLE stg_lines (
  accession_number text, line_no text, transaction_date text, security_title text,
  ownership_nature text, shares text, price text, value text, shares_owned_following text,
  footnote_flags text
) ON COMMIT DROP;
"""

UPSERT_SQL = """
INSERT INTO sec_filings (accession_number, form_type, filing_date, period_of_report, issuer_cik,
                         issuer_name, ticker, orig_submission_date, is_10b5_1)
SELECT accession_number, form_type, filing_date::date, period_of_report::date, issuer_cik,
       issuer_name, ticker, orig_submission_date::date, is_10b5_1::boolean
FROM stg_filings
ON CONFLICT (accession_number) DO UPDATE SET
  form_type = EXCLUDED.form_type, filing_date = EXCLUDED.filing_date,
  period_of_report = EXCLUDED.period_of_report, issuer_cik = EXCLUDED.issuer_cik,
  issuer_name = EXCLUDED.issuer_name, ticker = EXCLUDED.ticker,
  orig_submission_date = EXCLUDED.orig_submission_date, is_10b5_1 = EXCLUDED.is_10b5_1;

INSERT INTO filing_owners (accession_number, owner_cik, owner_name, is_officer, is_director,
                           is_ten_pct, is_other, officer_title, other_text)
SELECT accession_number, owner_cik, owner_name, is_officer::boolean, is_director::boolean,
       is_ten_pct::boolean, is_other::boolean, officer_title, other_text
FROM stg_owners
ON CONFLICT (accession_number, owner_cik) DO UPDATE SET
  owner_name = EXCLUDED.owner_name, is_officer = EXCLUDED.is_officer,
  is_director = EXCLUDED.is_director, is_ten_pct = EXCLUDED.is_ten_pct,
  is_other = EXCLUDED.is_other, officer_title = EXCLUDED.officer_title,
  other_text = EXCLUDED.other_text;

INSERT INTO purchase_lines (accession_number, line_no, transaction_date, security_title,
                            ownership_nature, shares, price, value, shares_owned_following,
                            footnote_flags)
SELECT accession_number, line_no::int, transaction_date::date, security_title, ownership_nature,
       shares::numeric, price::numeric, value::numeric, shares_owned_following::numeric,
       COALESCE(string_to_array(NULLIF(footnote_flags, ''), ','), '{}')
FROM stg_lines
ON CONFLICT (accession_number, line_no) DO UPDATE SET
  transaction_date = EXCLUDED.transaction_date, security_title = EXCLUDED.security_title,
  ownership_nature = EXCLUDED.ownership_nature, shares = EXCLUDED.shares,
  price = EXCLUDED.price, value = EXCLUDED.value,
  shares_owned_following = EXCLUDED.shares_owned_following,
  footnote_flags = EXCLUDED.footnote_flags;
"""

# A 4/A restates a Form 4. The older filing (same issuer, same period, same set of owners,
# filed earlier) is marked superseded by the LATEST such amendment.
SUPERSEDE_SQL = """
WITH owners AS (
  SELECT accession_number, array_agg(owner_cik ORDER BY owner_cik) AS ciks
  FROM filing_owners GROUP BY 1
), latest AS (
  SELECT DISTINCT ON (o.accession_number)
         o.accession_number AS old_acc, a.accession_number AS new_acc
  FROM sec_filings o
  JOIN sec_filings a
    ON a.form_type = '4/A'
   AND a.issuer_cik = o.issuer_cik
   AND a.period_of_report = o.period_of_report
   AND (o.filing_date, o.accession_number) < (a.filing_date, a.accession_number)
  JOIN owners oo ON oo.accession_number = o.accession_number
  JOIN owners ao ON ao.accession_number = a.accession_number AND ao.ciks = oo.ciks
  ORDER BY o.accession_number, a.filing_date DESC, a.accession_number DESC
)
UPDATE sec_filings s SET superseded_by = l.new_acc
FROM latest l
WHERE s.accession_number = l.old_acc AND s.superseded_by IS DISTINCT FROM l.new_acc;
"""

REPORT_SQL = """
SELECT (SELECT COUNT(*) FROM sec_filings)                          AS filings,
       (SELECT COUNT(*) FROM sec_filings WHERE superseded_by IS NOT NULL) AS superseded,
       (SELECT COUNT(*) FROM filing_owners)                        AS owner_rows,
       (SELECT COUNT(DISTINCT owner_cik) FROM filing_owners)       AS distinct_owner_ciks,
       (SELECT COUNT(*) FROM purchase_lines)                       AS purchase_lines,
       (SELECT MIN(transaction_date) FROM purchase_lines)          AS first_trade,
       (SELECT MAX(transaction_date) FROM purchase_lines)          AS last_trade;
"""


# ---------------------------------------------------------------------------
# Download / read
# ---------------------------------------------------------------------------
def fetch_zip(quarter, zip_dir):
    """Return the path of the quarter's zip, downloading it if it is not already in zip_dir."""
    os.makedirs(zip_dir, exist_ok=True)
    path = os.path.join(zip_dir, f"{quarter}_form345.zip")
    if os.path.exists(path) and zipfile.is_zipfile(path):
        return path
    last = None
    for template in URLS:
        url = template.format(q=quarter)
        for attempt in range(1, 4):
            try:
                r = requests.get(url, headers=HEADERS, timeout=180, stream=True)
                if r.status_code == 404:
                    last = f"{url} -> 404"
                    break                       # wrong folder: try the next address, no retries
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                if not zipfile.is_zipfile(path):
                    raise RuntimeError("download is not a zip (SEC may have blocked the request)")
                print(f"  downloaded from {url}")
                return path
            except Exception as e:              # noqa: BLE001
                last = e
                print(f"  attempt {attempt}/3 for {quarter} failed: {e}")
                time.sleep(5 * attempt)
    raise RuntimeError(f"could not download {quarter}: {last}")


def read_tsv(zf, basename):
    name = next((n for n in zf.namelist() if os.path.basename(n).upper() == basename), None)
    if name is None:
        return None
    with zf.open(name) as f:
        return pd.read_csv(f, sep="\t", dtype=str, keep_default_na=False,
                           quoting=csv.QUOTE_NONE, encoding="utf-8", encoding_errors="replace")


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def clean_symbol(raw):
    """Turn the SEC's free-text symbol into one Yahoo-style ticker, or None.
    'LEN, LEN.B' -> 'LEN'   'N O G' -> 'NOG'   'CLBR U' -> 'CLBRU'   'BRK.B' -> 'BRK-B'
    'BF/B' -> 'BF-B'        'NVRI WI' -> 'NVRI'   'NONE' / 'N/A' / 'N.A.' -> None (never 'N')."""
    s = str(raw or "").strip().upper()
    if s in PLACEHOLDER_SYMBOLS:
        return None
    s = re.sub(r"^.*:", "", s)                      # 'NYSE:FLG' -> 'FLG'
    s = re.split(r"[,;]+", s)[0].strip()            # several tickers: keep the first
    if re.fullmatch(r"[A-Z]{1,5}/[A-Z]", s):        # share class written with a slash
        s = s.replace("/", "-")
    toks = s.split()
    if not toks:
        return None
    if len(toks) > 1:
        if all(len(t) == 1 for t in toks):          # letters spaced out: 'N O G'
            s = "".join(toks)
        elif toks[0] == "OTC":
            s = toks[1]
        elif len(toks[1]) == 1:                     # unit / class suffix: 'CLBR U'
            s = toks[0] + toks[1]
        else:                                       # 'CRDA CRDB', 'NVRI WI': first one
            s = toks[0]
    else:
        s = toks[0]
    s = s.split("/")[0]
    if s in PLACEHOLDER_SYMBOLS:
        return None
    s = s.replace(".", "-")                         # Yahoo style, same as the site
    return None if s in PLACEHOLDER_SYMBOLS or not s else s


def parse_date(series):
    return pd.to_datetime(series, format="%d-%b-%Y", errors="coerce")


def to_bool(x):
    x = str(x).strip().lower()
    return "true" if x in ("1", "true") else ("false" if x in ("0", "false") else "")


def transform(raw):
    """raw: list of dicts with DataFrames SUBMISSION / REPORTINGOWNER / NONDERIV_TRANS / FOOTNOTES."""
    sub = pd.concat([r["SUBMISSION"] for r in raw], ignore_index=True)
    own = pd.concat([r["REPORTINGOWNER"] for r in raw], ignore_index=True)
    trn = pd.concat([r["NONDERIV_TRANS"] for r in raw], ignore_index=True)
    fns = [r["FOOTNOTES"] for r in raw if r.get("FOOTNOTES") is not None]
    fn = pd.concat(fns, ignore_index=True) if fns else None

    stats = {"submissions": len(sub), "trans_lines": len(trn)}

    sub = sub[sub.DOCUMENT_TYPE.isin(["4", "4/A"])].drop_duplicates("ACCESSION_NUMBER")
    stats["form4_filings"] = len(sub)

    # line_no = position of the line in the filing's table I (all lines, not only purchases),
    # so it stays the same if the filter rules change later.
    trn = trn.copy()
    trn["sk"] = pd.to_numeric(trn.NONDERIV_TRANS_SK, errors="coerce")
    trn = trn.sort_values(["ACCESSION_NUMBER", "sk"], kind="stable")
    trn["line_no"] = trn.groupby("ACCESSION_NUMBER").cumcount() + 1

    p = trn[(trn.TRANS_CODE == "P") & (trn.TRANS_ACQUIRED_DISP_CD == "A")
            & trn.ACCESSION_NUMBER.isin(sub.ACCESSION_NUMBER)].copy()
    stats["purchase_lines_all"] = len(p)
    p["shares"] = pd.to_numeric(p.TRANS_SHARES, errors="coerce")
    p["price"] = pd.to_numeric(p.TRANS_PRICEPERSHARE, errors="coerce")
    p = p[p.shares > 0]
    # physically impossible numbers (SEC data-entry errors) would overflow the columns
    absurd = (p.shares >= 1e15) | (p.price >= 1e12)
    stats["lines_dropped_absurd"] = int(absurd.sum())
    p = p[~absurd]
    p["value"] = p.shares * p.price
    p["transaction_date"] = parse_date(p.TRANS_DATE)
    stats["lines_without_price"] = int((p.price.isna() | (p.price <= 0)).sum())
    p = p[p.transaction_date.notna()]

    # keep a purchase when the filing's day-total is >= $50k
    day_total = p.groupby(["ACCESSION_NUMBER", "transaction_date"]).value.transform("sum")
    p = p[day_total >= MIN_VALUE].copy()

    # footnote-derived flags
    fn_text = {}
    if fn is not None and {"ACCESSION_NUMBER", "FOOTNOTE_ID", "FOOTNOTE_TXT"} <= set(fn.columns):
        fn_text = {(a, i): t for a, i, t in zip(fn.ACCESSION_NUMBER, fn.FOOTNOTE_ID, fn.FOOTNOTE_TXT)}
    fn_cols = [c for c in p.columns if c.endswith("_FN")]

    def flags(row):
        out = []
        if row.price >= 10000:
            out.append("price_outlier")
        if row.transaction_date.weekday() >= 5:
            out.append("weekend_date")
        if str(row.TRANS_PRICEPERSHARE_FN).strip():
            out.append("price_footnote")
        ids = set()
        for c in fn_cols:
            ids.update(re.findall(r"F\d+", str(row[c])))
        texts = " ".join(str(fn_text.get((row.ACCESSION_NUMBER, i), "")) for i in ids)
        if texts:
            if OFFERING_RE.search(texts):
                out.append("offering_footnote")
            if WEIGHTED_RE.search(texts):
                out.append("weighted_avg_price")
        return ",".join(out)

    p["footnote_flags"] = p.apply(flags, axis=1) if len(p) else ""
    stats["has_footnote_text"] = bool(fn_text)

    keep_acc = set(p.ACCESSION_NUMBER)
    f = sub[sub.ACCESSION_NUMBER.isin(keep_acc)].copy()
    filings = pd.DataFrame({
        "accession_number": f.ACCESSION_NUMBER,
        "form_type": f.DOCUMENT_TYPE,
        "filing_date": parse_date(f.FILING_DATE).dt.strftime("%Y-%m-%d"),
        "period_of_report": parse_date(f.PERIOD_OF_REPORT).dt.strftime("%Y-%m-%d"),
        "issuer_cik": f.ISSUERCIK,
        "issuer_name": f.ISSUERNAME,
        "ticker": f.ISSUERTRADINGSYMBOL.map(clean_symbol),
        "orig_submission_date": parse_date(f.DATE_OF_ORIG_SUB).dt.strftime("%Y-%m-%d"),
        "is_10b5_1": f.AFF10B5ONE.map(to_bool),
    })

    o = own[own.ACCESSION_NUMBER.isin(keep_acc)].copy()
    o = o.drop_duplicates(["ACCESSION_NUMBER", "RPTOWNERCIK"])
    rel = o.RPTOWNER_RELATIONSHIP.str.lower()
    owners = pd.DataFrame({
        "accession_number": o.ACCESSION_NUMBER,
        "owner_cik": o.RPTOWNERCIK,
        "owner_name": o.RPTOWNERNAME.str.strip(),
        "is_officer": rel.str.contains("officer").map({True: "true", False: "false"}),
        "is_director": rel.str.contains("director").map({True: "true", False: "false"}),
        "is_ten_pct": rel.str.contains("tenpercent").map({True: "true", False: "false"}),
        "is_other": rel.str.contains("other").map({True: "true", False: "false"}),
        "officer_title": o.RPTOWNER_TITLE,
        "other_text": o.RPTOWNER_TXT,
    })
    owners = owners[owners.owner_cik != ""]

    lines = pd.DataFrame({
        "accession_number": p.ACCESSION_NUMBER,
        "line_no": p.line_no,
        "transaction_date": p.transaction_date.dt.strftime("%Y-%m-%d"),
        "security_title": p.SECURITY_TITLE,
        "ownership_nature": p.DIRECT_INDIRECT_OWNERSHIP,
        "shares": p.shares,
        "price": p.price,
        "value": p.value.round(2),
        "shares_owned_following": pd.to_numeric(p.SHRS_OWND_FOLWNG_TRANS, errors="coerce"),
        "footnote_flags": p.footnote_flags,
    })
    # a filing with no named owner can't be attributed to anyone: drop it
    ok = set(owners.accession_number)
    filings = filings[filings.accession_number.isin(ok)]
    lines = lines[lines.accession_number.isin(ok)]
    stats.update(filings_kept=len(filings), owners_kept=len(owners), lines_kept=len(lines),
                 lines_price_outlier=int(lines.footnote_flags.str.contains("price_outlier").sum()),
                 lines_weekend=int(lines.footnote_flags.str.contains("weekend_date").sum()),
                 lines_offering_fn=int(lines.footnote_flags.str.contains("offering_footnote").sum()),
                 tickers_missing=int(filings.ticker.isna().sum()))
    return filings, owners, lines, stats


# ---------------------------------------------------------------------------
# Database load
# ---------------------------------------------------------------------------
def copy_df(cur, df, table):
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="", quoting=csv.QUOTE_MINIMAL)
    buf.seek(0)
    cols = ", ".join(df.columns)
    cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv)", buf)


def load_db(filings, owners, lines):
    import psycopg2
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        sys.exit("Missing SUPABASE_DB_URL environment variable.")
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(STAGING_DDL)
            copy_df(cur, filings, "stg_filings")
            copy_df(cur, owners, "stg_owners")
            copy_df(cur, lines, "stg_lines")
            cur.execute(UPSERT_SQL)
            cur.execute(SUPERSEDE_SQL)
            cur.execute(REPORT_SQL)
            names = [d[0] for d in cur.description]
            row = cur.fetchone()
        conn.commit()           # one transaction: all or nothing
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print("\nDATABASE NOW HOLDS")
    for n, v in zip(names, row):
        print(f"  {n:22s} {v}")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quarters", default=DEFAULT_QUARTERS)
    ap.add_argument("--zip-dir", default="sec_zips")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, do not touch the database")
    ap.add_argument("--dump", metavar="DIR", help="write the staging CSVs here")
    a = ap.parse_args()

    raw, failed = [], []
    for q in [x.strip().lower() for x in a.quarters.split(",") if x.strip()]:
        print(f"== {q}")
        try:
            path = fetch_zip(q, a.zip_dir)
            with zipfile.ZipFile(path) as zf:
                d = {n: read_tsv(zf, n + ".TSV") for n in
                     ("SUBMISSION", "REPORTINGOWNER", "NONDERIV_TRANS", "FOOTNOTES")}
            missing = [k for k in ("SUBMISSION", "REPORTINGOWNER", "NONDERIV_TRANS") if d[k] is None]
            if missing:
                raise RuntimeError(f"zip is missing {missing}")
            raw.append(d)
            print(f"  read {len(d['SUBMISSION']):,} submissions, {len(d['NONDERIV_TRANS']):,} lines")
        except Exception as e:                      # noqa: BLE001
            print(f"  SKIPPED {q}: {e}")
            failed.append(q)

    if not raw:
        sys.exit("No quarter could be read. Nothing loaded.")

    filings, owners, lines, stats = transform(raw)
    print("\nPARSE REPORT")
    for k, v in stats.items():
        print(f"  {k:22s} {v}")

    if a.dump:
        os.makedirs(a.dump, exist_ok=True)
        filings.to_csv(os.path.join(a.dump, "filings.csv"), index=False, na_rep="")
        owners.to_csv(os.path.join(a.dump, "owners.csv"), index=False, na_rep="")
        lines.to_csv(os.path.join(a.dump, "lines.csv"), index=False, na_rep="")
        print(f"  staging CSVs written to {a.dump}")

    if a.dry_run:
        print("\nDRY RUN: database not touched.")
    else:
        load_db(filings, owners, lines)

    if failed:
        sys.exit(f"\nFinished, but these quarters failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
