#!/usr/bin/env python3
"""
load_recent_filings.py - fill the gap before the SEC's quarterly bulk file is published.

The site's newest rows (the live feed) have an accession number but are not in the SEC bulk files yet.
For each of them this script downloads the Form 4 straight from EDGAR, converts it into the same
shape as the bulk files and loads it through the SAME code (load_sec_bulk.transform / load_db).
Result: the newest rows get footnote checks (offering / placement), the 10b5-1 flag, owner CIKs and
buyer links, exactly like older rows.

Extra rule: if the same filing also shows a purchase of a WARRANT (pre-funded warrants are how funds join
a private placement / PIPE), every purchase line in it is marked as an offering.

Usage
  python load_recent_filings.py                      # fetch what is missing, load into the database
  python load_recent_filings.py --max 300            # cap the number of filings per run (default 300)
  python load_recent_filings.py --files a.xml b.xml --dry-run --dump out   # test on local XML files
Safe to re-run (same keys as the bulk loader). Never touches insider_trades.
"""
import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import pandas as pd
import requests

import load_sec_bulk as L

HEADERS = L.HEADERS
SYN_ID = "F9001"
SYN_TEXT = ("private placement: the same filing reports a purchase of warrants "
            "(pre-funded warrant), typical of a financing round")


def fmt_acc(a):
    d = re.sub(r"\D", "", str(a))
    return f"{d[:10]}-{d[10:12]}-{d[12:]}" if len(d) == 18 else str(a)


def txt(el, path):
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else ""


def fdate(s):
    try:
        return pd.to_datetime(s).strftime("%d-%b-%Y").upper()
    except Exception:       # noqa: BLE001
        return ""


def parse_form4(xml_text, accession):
    """One Form 4 XML -> dict of the four DataFrames the bulk transform expects."""
    root = ET.fromstring(xml_text)
    acc = fmt_acc(accession)
    issuer = root.find("issuer")
    sigs = [(s.text or "").strip() for s in root.iter("signatureDate")]
    filing_date = max(sigs) if sigs else txt(root, "periodOfReport")
    sub = pd.DataFrame([{
        "ACCESSION_NUMBER": acc,
        "DOCUMENT_TYPE": txt(root, "documentType"),
        "FILING_DATE": fdate(filing_date),
        "PERIOD_OF_REPORT": fdate(txt(root, "periodOfReport")),
        "ISSUERCIK": txt(issuer, "issuerCik"),
        "ISSUERNAME": txt(issuer, "issuerName"),
        "ISSUERTRADINGSYMBOL": txt(issuer, "issuerTradingSymbol"),
        "DATE_OF_ORIG_SUB": fdate(txt(root, "dateOfOriginalSubmission")),
        "AFF10B5ONE": txt(root, "aff10b5One"),
    }])

    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        flags = []
        for tag, word in (("isDirector", "Director"), ("isOfficer", "Officer"),
                          ("isTenPercentOwner", "TenPercentOwner"), ("isOther", "Other")):
            if txt(rel, tag).lower() in ("1", "true"):
                flags.append(word)
        owners.append({
            "ACCESSION_NUMBER": acc,
            "RPTOWNERCIK": txt(ro, "reportingOwnerId/rptOwnerCik"),
            "RPTOWNERNAME": txt(ro, "reportingOwnerId/rptOwnerName"),
            "RPTOWNER_RELATIONSHIP": ",".join(flags),
            "RPTOWNER_TITLE": txt(rel, "officerTitle"),
            "RPTOWNER_TXT": txt(rel, "otherText"),
        })
    owners = pd.DataFrame(owners)

    # a purchase of warrants in the same filing = financing round
    warrant_buy = False
    for dt in root.findall("derivativeTable/derivativeTransaction"):
        if txt(dt, "transactionCoding/transactionCode") == "P" and \
           re.search(r"warrant", txt(dt, "securityTitle/value"), re.I):
            warrant_buy = True

    lines = []
    for i, t in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction"), start=1):
        ids = [x.get("id") for x in t.iter("footnoteId")]
        price_ids = [x.get("id") for x in t.findall("transactionAmounts/transactionPricePerShare/footnoteId")]
        lines.append({
            "ACCESSION_NUMBER": acc,
            "NONDERIV_TRANS_SK": str(i),
            "SECURITY_TITLE": txt(t, "securityTitle/value"),
            "TRANS_DATE": fdate(txt(t, "transactionDate/value")),
            "TRANS_CODE": txt(t, "transactionCoding/transactionCode"),
            "TRANS_ACQUIRED_DISP_CD": txt(t, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "TRANS_SHARES": txt(t, "transactionAmounts/transactionShares/value"),
            "TRANS_PRICEPERSHARE": txt(t, "transactionAmounts/transactionPricePerShare/value"),
            "TRANS_PRICEPERSHARE_FN": ",".join(price_ids),
            "SHRS_OWND_FOLWNG_TRANS": txt(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
            "DIRECT_INDIRECT_OWNERSHIP": txt(t, "ownershipNature/directOrIndirectOwnership/value"),
            "ALL_FN": ",".join(ids + ([SYN_ID] if warrant_buy else [])),
        })
    lines = pd.DataFrame(lines, columns=["ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "SECURITY_TITLE", "TRANS_DATE",
                                         "TRANS_CODE", "TRANS_ACQUIRED_DISP_CD", "TRANS_SHARES",
                                         "TRANS_PRICEPERSHARE", "TRANS_PRICEPERSHARE_FN",
                                         "SHRS_OWND_FOLWNG_TRANS", "DIRECT_INDIRECT_OWNERSHIP", "ALL_FN"])

    fn_rows = [{"ACCESSION_NUMBER": acc, "FOOTNOTE_ID": f.get("id"), "FOOTNOTE_TXT": (f.text or "").strip()}
               for f in root.findall("footnotes/footnote")]
    if warrant_buy:
        fn_rows.append({"ACCESSION_NUMBER": acc, "FOOTNOTE_ID": SYN_ID, "FOOTNOTE_TXT": SYN_TEXT})
    fn = pd.DataFrame(fn_rows, columns=["ACCESSION_NUMBER", "FOOTNOTE_ID", "FOOTNOTE_TXT"])
    return {"SUBMISSION": sub, "REPORTINGOWNER": owners, "NONDERIV_TRANS": lines, "FOOTNOTES": fn}


# ---------------------------------------------------------------------------
def get(url, tries=3):
    for a in range(1, tries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        except Exception as e:      # noqa: BLE001
            print(f"    attempt {a}/{tries} failed: {e}")
            time.sleep(2 * a)
    return None


def ticker_to_cik():
    r = get("https://www.sec.gov/files/company_tickers.json")
    if r is None:
        sys.exit("Could not download the SEC ticker list.")
    return {v["ticker"].upper(): str(v["cik_str"]) for v in r.json().values()}


def fetch_form4_xml(cik, accession):
    nodash = re.sub(r"\D", "", accession)
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/"
    r = get(base + "index.json")
    time.sleep(0.15)
    if r is None:
        return None
    names = [i["name"] for i in r.json().get("directory", {}).get("item", [])]
    xmls = [n for n in names if n.lower().endswith(".xml") and "filingsummary" not in n.lower()
            and not n.lower().startswith("xsl")]
    for n in xmls:
        x = get(base + n)
        time.sleep(0.15)
        if x is not None and b"<ownershipDocument" in x.content[:2000] + x.content:
            return x.text
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max", type=int, default=300)
    ap.add_argument("--files", nargs="*", help="local Form 4 XML files (testing)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump", metavar="DIR")
    a = ap.parse_args()

    raw, skipped = [], 0
    if a.files:
        for i, f in enumerate(a.files):
            raw.append(parse_form4(open(f, encoding="utf-8", errors="replace").read(), f"0000000000-26-{i + 1:06d}"))
    else:
        import psycopg2
        url = os.environ.get("SUPABASE_DB_URL")
        if not url:
            sys.exit("Missing SUPABASE_DB_URL environment variable.")
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute("""
            SELECT t.accession_number, MIN(t.ticker), MAX(t.transaction_date)
            FROM insider_trades t
            WHERE t.accession_number IS NOT NULL AND t.transaction_date >= DATE '2025-11-01'
              AND t.is_canonical
              AND NOT EXISTS (SELECT 1 FROM sec_filings s WHERE s.accession_number = t.accession_number)
            GROUP BY 1 ORDER BY 3 DESC LIMIT %s""", (a.max,))
        todo = cur.fetchall()
        conn.close()
        print(f"{len(todo)} filings on the site are not in the SEC tables yet.")
        if not todo:
            return
        t2c = ticker_to_cik()
        for n, (acc, tk, _d) in enumerate(todo, 1):
            acc = fmt_acc(acc)
            cik = t2c.get((tk or "").upper())
            if not cik:
                skipped += 1
                continue
            xml_text = fetch_form4_xml(cik, acc)
            if not xml_text:
                skipped += 1
                continue
            try:
                raw.append(parse_form4(xml_text, acc))
            except Exception as e:      # noqa: BLE001
                print(f"  could not parse {acc}: {e}")
                skipped += 1
            if n % 50 == 0:
                print(f"  {n}/{len(todo)} fetched")
        print(f"fetched {len(raw)} filings, skipped {skipped} (no CIK / not found / unparseable)")
        if not raw:
            return

    # the column ALL_FN (ends in _FN) holds every footnote id of each line; the bulk transform reads those
    filings, owners, lines, stats = L.transform(raw)
    print("PARSE REPORT", stats)
    if a.dump:
        os.makedirs(a.dump, exist_ok=True)
        filings.to_csv(os.path.join(a.dump, "filings.csv"), index=False)
        owners.to_csv(os.path.join(a.dump, "owners.csv"), index=False)
        lines.to_csv(os.path.join(a.dump, "lines.csv"), index=False)
    if a.dry_run:
        print("DRY RUN: database not touched.")
        return
    L.load_db(filings, owners, lines)


if __name__ == "__main__":
    main()
