-- =====================================================================
-- insider_cleanup.sql
-- PART 1: non-destructive cleanup of the CURRENT insider_trades table
-- PART 2: v2 schema (filing-line level + owners + buyers) for the rebuild
-- Run PART 1 step by step in the Supabase SQL editor. Nothing is deleted;
-- bad rows are flagged and hidden behind a view.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. BACKUP (do this first, and pause the GitHub workflow)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS insider_trades_backup_20261120 AS
SELECT * FROM insider_trades;

-- ---------------------------------------------------------------------
-- 1. NORMALIZE
-- ---------------------------------------------------------------------
BEGIN;

-- 29 rows look like '2026-06-02-05:00' (timezone suffix from the SEC XML).
-- Works whether the column is text or already date.
ALTER TABLE insider_trades
  ALTER COLUMN transaction_date TYPE date
  USING LEFT(transaction_date::text, 10)::date;

-- BUY (old backfill) vs Purchase (live ETL): one vocabulary
UPDATE insider_trades SET activity_type = 'Purchase' WHERE activity_type <> 'Purchase';

-- 'HEI, HEI.A' -> 'HEI',  'NYSE:FLG' -> 'FLG'
UPDATE insider_trades
SET ticker = UPPER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(ticker, '^.*:', ''), '[,/ ].*$', '')))
WHERE ticker ~ '[:, /]';

-- Columns that are 100% NULL in the file you sent. Drop once you confirm
-- nothing reads them.
-- ALTER TABLE insider_trades DROP COLUMN ai_summary, DROP COLUMN ret_stock_after,
--   DROP COLUMN ret_benchmark_after, DROP COLUMN alpha_after;

COMMIT;

-- ---------------------------------------------------------------------
-- 2. EVENT KEY + CANONICAL ROW
--    Conservative key: includes shares_owned_following so two different
--    insiders who bought identical lots are NOT merged. This leaves ~1%
--    of joint-filer rows un-collapsed; the filing-line key in PART 2
--    removes that ambiguity for good.
-- ---------------------------------------------------------------------
ALTER TABLE insider_trades
  ADD COLUMN IF NOT EXISTS event_key    text,
  ADD COLUMN IF NOT EXISTS is_canonical boolean DEFAULT true,
  ADD COLUMN IF NOT EXISTS dq_hard      text[]  DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS dq_soft      text[]  DEFAULT '{}';

UPDATE insider_trades
SET event_key = md5(concat_ws('|', ticker, transaction_date, shares_bought,
                              ROUND(est_value::numeric, 2), shares_owned_following));

-- keep one row per event: prefer rows that have an accession, then lowest id
WITH ranked AS (
  SELECT id,
         ROW_NUMBER() OVER (PARTITION BY event_key
                            ORDER BY (accession_number IS NULL), id) AS rn
  FROM insider_trades
)
UPDATE insider_trades t
SET is_canonical = (r.rn = 1)
FROM ranked r WHERE t.id = r.id;

-- sanity check: expect roughly 7,700 canonical rows from ~15,700
SELECT is_canonical, COUNT(*) FROM insider_trades GROUP BY 1;

-- ---------------------------------------------------------------------
-- 3. DATA-QUALITY FLAGS
--    hard = excluded from the public view.  soft = shown, but labelled.
-- ---------------------------------------------------------------------
UPDATE insider_trades SET dq_hard = '{}', dq_soft = '{}';

-- HARD ------------------------------------------------------------------
UPDATE insider_trades SET dq_hard = dq_hard || 'value_ge_1b'       WHERE est_value >= 1e9;
UPDATE insider_trades SET dq_hard = dq_hard || 'value_gt_mktcap'   WHERE est_value > market_cap;
UPDATE insider_trades SET dq_hard = dq_hard || 'weekend_date'      WHERE EXTRACT(ISODOW FROM transaction_date) IN (6,7);
UPDATE insider_trades SET dq_hard = dq_hard || 'stale_date'        WHERE transaction_date < DATE '2025-11-01';
UPDATE insider_trades SET dq_hard = dq_hard || 'no_ticker'         WHERE ticker IS NULL OR ticker = '';
-- insider "owns" >150% of shares outstanding => price or market cap is wrong
UPDATE insider_trades SET dq_hard = dq_hard || 'ownership_gt_float'
WHERE price_at_tx > 0 AND market_cap > 0
  AND shares_owned_following > 1.5 * (market_cap / price_at_tx);
-- not informative as "insider conviction"
UPDATE insider_trades SET dq_hard = dq_hard || 'market_maker'
WHERE insider_name ~* 'HRT FINANCIAL|JANE STREET|CITADEL SEC|SUSQUEHANNA|VIRTU|JUMP TRADING|TWO SIGMA SEC';
UPDATE insider_trades SET dq_hard = dq_hard || 'spac_sponsor'
WHERE insider_name ~* 'sponsor|acquisition corp';

-- SOFT ------------------------------------------------------------------
-- Form 4 price is far from the market close the return was measured from
-- (private placement, offering, wrong currency/class). base = implied close.
UPDATE insider_trades SET dq_soft = dq_soft || 'price_vs_market'
WHERE price_60d_after > 0 AND return_60d_pct IS NOT NULL AND price_at_tx > 0
  AND ABS((price_60d_after / (1 + return_60d_pct/100.0)) / price_at_tx - 1) > 0.25;

UPDATE insider_trades SET dq_soft = dq_soft || 'unknown_mktcap'  WHERE market_cap IS NULL;
UPDATE insider_trades SET dq_soft = dq_soft || 'nano_cap'        WHERE market_cap < 5e7;
UPDATE insider_trades SET dq_soft = dq_soft || 'no_return_60d'
WHERE return_60d_pct IS NULL AND transaction_date <= CURRENT_DATE - 60;
UPDATE insider_trades SET dq_soft = dq_soft || 'generic_title'
WHERE insider_title IN ('Director/Other', 'See Remarks', 'Insider');
UPDATE insider_trades SET dq_soft = dq_soft || 'round_offering_price'
WHERE price_at_tx IS NOT NULL AND ROUND(price_at_tx::numeric, 2) = price_at_tx
  AND shares_bought >= 100000 AND est_value >= 1e6;   -- heuristic, review manually

-- how many rows each flag catches
SELECT f, COUNT(*) FROM insider_trades, UNNEST(dq_hard || dq_soft) AS f GROUP BY 1 ORDER BY 2 DESC;

-- ---------------------------------------------------------------------
-- 4. PUBLIC VIEW: point the website and every aggregate at this
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW insider_trades_public AS
SELECT *
FROM insider_trades
WHERE is_canonical
  AND COALESCE(CARDINALITY(dq_hard), 0) = 0;

-- Quarantine review list: look at these by hand before ever deleting
SELECT id, ticker, transaction_date, insider_name, shares_bought, est_value, price_at_tx,
       market_cap, dq_hard
FROM insider_trades
WHERE is_canonical AND CARDINALITY(dq_hard) > 0
ORDER BY est_value DESC;

-- Make it impossible to re-create the exact duplicates in the meantime
-- (fails if any canonical duplicates remain, which is a useful check)
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_event_canonical
  ON insider_trades (event_key) WHERE is_canonical;


-- =====================================================================
-- PART 2: v2 SCHEMA (the rebuild). One row per Form 4 table-I line,
-- owners stored separately, buyers resolved on top.
-- =====================================================================

-- one row per SEC filing
CREATE TABLE IF NOT EXISTS sec_filings (
  accession_number     text PRIMARY KEY,
  form_type            text NOT NULL CHECK (form_type IN ('4','4/A')),
  filing_date          date,
  period_of_report     date,
  issuer_cik           text,
  issuer_name          text,
  ticker               text,
  orig_submission_date date,              -- 4/A only (dateOfOriginalSubmission)
  is_10b5_1            boolean,
  superseded_by        text REFERENCES sec_filings(accession_number),
  inserted_at          timestamptz DEFAULT now()
);

-- everyone named on the filing (joint filers live here, NOT as duplicate trades)
CREATE TABLE IF NOT EXISTS filing_owners (
  accession_number text REFERENCES sec_filings(accession_number) ON DELETE CASCADE,
  owner_cik        text NOT NULL,         -- rptOwnerCik: the deterministic identity key
  owner_name       text NOT NULL,
  is_officer       boolean DEFAULT false,
  is_director      boolean DEFAULT false,
  is_ten_pct       boolean DEFAULT false,
  is_other         boolean DEFAULT false,
  officer_title    text,
  other_text       text,
  PRIMARY KEY (accession_number, owner_cik)
);

-- one row per purchase line on the form (code P, acquired)
CREATE TABLE IF NOT EXISTS purchase_lines (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  accession_number        text NOT NULL REFERENCES sec_filings(accession_number) ON DELETE CASCADE,
  line_no                 int  NOT NULL,   -- position in the nonDerivativeTable
  transaction_date        date NOT NULL,
  security_title          text,            -- separates Class A from Class B (BH, LEN)
  ownership_nature        text,            -- 'D' direct / 'I' indirect (separates RM, STWD lots)
  shares                  numeric(20,4) NOT NULL,
  price                   numeric(20,4),
  value                   numeric(20,2),
  shares_owned_following  numeric(20,4),
  footnote_flags          text[] DEFAULT '{}',   -- offering / placement / etc. from footnotes
  UNIQUE (accession_number, line_no)       -- THE dedupe key: re-runs become harmless
);
CREATE INDEX IF NOT EXISTS ix_pl_date ON purchase_lines (transaction_date);

-- market data kept separate so re-running the ETL never overwrites it with NULLs
CREATE TABLE IF NOT EXISTS line_market_data (
  line_id          bigint PRIMARY KEY REFERENCES purchase_lines(id) ON DELETE CASCADE,
  close_on_tx      numeric(20,4),
  close_on_filing  numeric(20,4),          -- what a follower could actually have bought at
  market_cap       numeric(24,2),
  market_cap_tier  text,
  benchmark        text,
  ret_60d_tx       numeric(10,4),          -- from transaction-date close
  ret_180d_tx      numeric(10,4),
  ret_60d_filing   numeric(10,4),          -- from filing-date close (actionable)
  ret_180d_filing  numeric(10,4),
  bench_60d        numeric(10,4),
  bench_180d       numeric(10,4),
  status_60d       text,                   -- 'ok' | 'immature' | 'delisted' | 'no_data'
  status_180d      text,
  updated_at       timestamptz DEFAULT now()
);

-- Buyer identity layer (populated by build_buyer_groups.py)
CREATE TABLE IF NOT EXISTS buyers (
  buyer_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  display_name text NOT NULL,
  buyer_type   text CHECK (buyer_type IN ('person','group','institution','market_maker')),
  n_people     int,
  created_at   timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS buyer_members (
  owner_cik   text PRIMARY KEY,            -- every CIK belongs to exactly one buyer
  buyer_id    bigint NOT NULL REFERENCES buyers(buyer_id),
  link_method text CHECK (link_method IN ('cik','cofiling','manual','fuzzy_confirmed')),
  confidence  numeric(3,2)
);

-- name-only matches are NEVER auto-merged; they land here for review
CREATE TABLE IF NOT EXISTS buyer_merge_suggestions (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  cik_a       text, name_a text,
  cik_b       text, name_b text,
  reason      text,                        -- e.g. 'same_name_diff_suffix'
  status      text DEFAULT 'pending' CHECK (status IN ('pending','merge','reject')),
  UNIQUE (cik_a, cik_b)
);

-- The one view the site's buyer pages read: one row per purchase line,
-- attributed to exactly one buyer (no joint-filer double counting).
CREATE OR REPLACE VIEW buyer_events AS
SELECT pl.id AS line_id, f.accession_number, f.ticker, pl.transaction_date, f.filing_date,
       pl.shares, pl.price, pl.value, pl.security_title,
       b.buyer_id, b.display_name AS buyer_name,
       md.market_cap_tier, md.benchmark,
       md.ret_60d_tx, md.bench_60d, md.ret_180d_tx, md.bench_180d,
       md.ret_60d_filing, md.ret_180d_filing
FROM purchase_lines pl
JOIN sec_filings f ON f.accession_number = pl.accession_number AND f.superseded_by IS NULL
LEFT JOIN LATERAL (
    -- pick ONE owner per filing as the attributed buyer: officer > director > 10% > other
    SELECT fo.owner_cik
    FROM filing_owners fo
    WHERE fo.accession_number = pl.accession_number
    ORDER BY (CASE WHEN fo.is_officer THEN 0 WHEN fo.is_director THEN 1
                   WHEN fo.is_ten_pct THEN 2 ELSE 3 END), fo.owner_cik
    LIMIT 1
) lead ON true
LEFT JOIN buyer_members bm ON bm.owner_cik = lead.owner_cik
LEFT JOIN buyers b ON b.buyer_id = bm.buyer_id
LEFT JOIN line_market_data md ON md.line_id = pl.id;
