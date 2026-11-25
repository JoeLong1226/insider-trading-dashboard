-- link_buyers.sql  (safe to re-run; run it AFTER "Build Buyer Groups")
-- Gives every visible trade row on the site a buyer_id.

DROP TABLE IF EXISTS _lead; DROP TABLE IF EXISTS _day; DROP TABLE IF EXISTS _match; DROP TABLE IF EXISTS _cand;

UPDATE insider_trades SET buyer_id = NULL;

-- One lead owner per SEC filing: officer > director > 10% owner > other
CREATE TEMP TABLE _lead AS
SELECT DISTINCT ON (fo.accession_number) fo.accession_number, fo.owner_cik
FROM filing_owners fo
ORDER BY fo.accession_number,
         (CASE WHEN fo.is_officer THEN 0 WHEN fo.is_director THEN 1 WHEN fo.is_ten_pct THEN 2 ELSE 3 END),
         fo.owner_cik;

-- Rows that already carry an accession number: direct link
UPDATE insider_trades t SET buyer_id = m.buyer_id
FROM _lead l JOIN buyer_members m ON m.owner_cik = l.owner_cik
WHERE t.accession_number = l.accession_number;

-- Older rows without an accession number: match ticker + date + same total shares or value (within 1%)
CREATE TEMP TABLE _day AS
SELECT f.ticker, pl.transaction_date, pl.accession_number,
       SUM(pl.shares) AS shares, SUM(pl.value) AS value
FROM purchase_lines pl JOIN sec_filings f USING (accession_number)
WHERE f.superseded_by IS NULL AND f.ticker IS NOT NULL
GROUP BY 1, 2, 3;

CREATE TEMP TABLE _match AS
SELECT t.id, MIN(m.buyer_id) AS buyer_id, COUNT(DISTINCT m.buyer_id) AS n_buyers
FROM insider_trades t
JOIN _day d ON d.ticker = t.ticker AND d.transaction_date = t.transaction_date
JOIN _lead l ON l.accession_number = d.accession_number
JOIN buyer_members m ON m.owner_cik = l.owner_cik
WHERE t.buyer_id IS NULL
  AND ( ABS(d.shares - t.shares_bought) <= GREATEST(1, 0.01 * t.shares_bought)
        OR ABS(d.value - t.est_value)   <= 0.01 * t.est_value )
GROUP BY t.id;

-- only when exactly one buyer fits (ambiguous days stay unlinked rather than guessed)
UPDATE insider_trades t SET buyer_id = x.buyer_id
FROM _match x WHERE x.id = t.id AND x.n_buyers = 1;

-- PASS 2: SEC has that ticker + day but the amounts differ (old per-lot rows etc.)
-- (a) only one buyer filed that ticker that day -> safe to link
-- (b) several buyers -> link only if one buyer's total is within 5% of the row and no other is
CREATE TEMP TABLE _cand AS
SELECT t.id, m.buyer_id,
       MIN(ABS(d.value - t.est_value) / NULLIF(t.est_value, 0)) AS rel_diff
FROM insider_trades t
JOIN _day d ON d.ticker = t.ticker AND d.transaction_date = t.transaction_date
JOIN _lead l ON l.accession_number = d.accession_number
JOIN buyer_members m ON m.owner_cik = l.owner_cik
WHERE t.buyer_id IS NULL
GROUP BY t.id, m.buyer_id;

UPDATE insider_trades t SET buyer_id = c.buyer_id
FROM _cand c
WHERE c.id = t.id AND t.buyer_id IS NULL
  AND (SELECT COUNT(*) FROM _cand c2 WHERE c2.id = c.id) = 1;

UPDATE insider_trades t SET buyer_id = c.buyer_id
FROM _cand c
WHERE c.id = t.id AND t.buyer_id IS NULL
  AND c.rel_diff <= 0.05
  AND NOT EXISTS (SELECT 1 FROM _cand c2 WHERE c2.id = c.id AND c2.buyer_id <> c.buyer_id AND c2.rel_diff <= 0.05);

-- PASS 3: still unlinked (e.g. Oct/Nov rows the SEC files don't cover yet): exact owner-name match,
-- only when that exact name belongs to a single buyer
WITH names AS (
  SELECT lower(regexp_replace(trim(fo.owner_name), '\s+', ' ', 'g')) AS nm,
         MIN(m.buyer_id) AS buyer_id, COUNT(DISTINCT m.buyer_id) AS n_buyers
  FROM filing_owners fo JOIN buyer_members m ON m.owner_cik = fo.owner_cik
  GROUP BY 1
)
UPDATE insider_trades t SET buyer_id = n.buyer_id
FROM names n
WHERE t.buyer_id IS NULL AND n.n_buyers = 1
  AND n.nm = lower(regexp_replace(trim(t.insider_name), '\s+', ' ', 'g'));

SELECT COUNT(*)                                     AS visible_rows,
       COUNT(*) FILTER (WHERE t.buyer_id IS NOT NULL) AS linked,
       COUNT(*) FILTER (WHERE t.buyer_id IS NULL)     AS unlinked,
       COUNT(*) FILTER (WHERE t.buyer_id IS NULL AND t.transaction_date < DATE '2026-10-01') AS unlinked_before_oct
FROM insider_trades t
WHERE t.id IN (SELECT id FROM insider_trades_open_market);
