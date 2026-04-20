-- build_marts.sql
-- Transforms raw_* staging tables into dim_* / fact_* mart tables.
--
-- Run: python3 solar_etl.py --build-marts
-- Or:  duckdb data/warehouse/solar.duckdb < sql/build_marts.sql
--
-- Safe to re-run: each section truncates and rebuilds from staging.

-- ------------------------------------------------------------------
-- dim_location — deduplicated physical sites
-- ------------------------------------------------------------------
DELETE FROM dim_location;
DELETE FROM sqlite_sequence WHERE name = 'seq_location_id'
    ON CONFLICT DO NOTHING;  -- harmless if not SQLite

INSERT INTO dim_location (lat, lon, state, zip_code, city, county, first_seen_at)
SELECT
    lat,
    lon,
    state,
    zip_code,
    -- city/county aren't in raw_geocode directly; leave NULL for now.
    -- A future enrichment step could look them up from data/uszips.csv.
    NULL AS city,
    NULL AS county,
    min(fetched_at) AS first_seen_at
FROM raw_geocode
WHERE lat IS NOT NULL AND lon IS NOT NULL
GROUP BY lat, lon, state, zip_code;


-- ------------------------------------------------------------------
-- dim_utility — one row per unique utility seen in rate lookups
-- ------------------------------------------------------------------
DELETE FROM dim_utility;

INSERT INTO dim_utility (utility_name, state, first_seen_at)
SELECT
    u.utility_name,
    -- Best-guess state: the state of the first quote that used this utility
    (SELECT q.state FROM raw_quote q
     WHERE q.utility_name = u.utility_name
     ORDER BY q.fetched_at ASC LIMIT 1) AS state,
    min(u.fetched_at) AS first_seen_at
FROM raw_urdb u
WHERE u.utility_name IS NOT NULL
GROUP BY u.utility_name;


-- ------------------------------------------------------------------
-- dim_tariff — SCD Type 2
-- Each (utility, rate_name) combination gets a new row each time the
-- flat_rate changes.  effective_to is NULL on the current row.
-- ------------------------------------------------------------------
DELETE FROM dim_tariff;

-- Step 1: collapse staging into one row per (utility, rate_name, flat_rate)
-- We use the earliest fetch date as effective_from.
WITH rate_history AS (
    SELECT
        r.utility_name,
        r.rate_name,
        r.flat_rate,
        r.is_tou,
        r.is_tiered,
        r.source,
        min(r.fetched_at)::DATE AS first_seen,
        max(r.fetched_at)::DATE AS last_seen
    FROM raw_urdb r
    WHERE r.utility_name IS NOT NULL AND r.flat_rate IS NOT NULL
    GROUP BY
        r.utility_name, r.rate_name, r.flat_rate,
        r.is_tou, r.is_tiered, r.source
),
-- Step 2: join to dim_utility for the FK
with_utility AS (
    SELECT
        h.*,
        u.utility_id
    FROM rate_history h
    LEFT JOIN dim_utility u ON u.utility_name = h.utility_name
),
-- Step 3: compute effective_to as the first_seen of the next version
with_dates AS (
    SELECT
        utility_id,
        rate_name,
        flat_rate,
        is_tou,
        is_tiered,
        source,
        first_seen AS effective_from,
        LEAD(first_seen) OVER (
            PARTITION BY utility_id, rate_name
            ORDER BY first_seen
        ) AS effective_to
    FROM with_utility
)
INSERT INTO dim_tariff
    (utility_id, rate_name, flat_rate, is_tou, is_tiered,
     effective_from, effective_to, source)
SELECT
    utility_id, rate_name, flat_rate, is_tou, is_tiered,
    effective_from, effective_to, source
FROM with_dates;


-- ------------------------------------------------------------------
-- fact_quote — one row per quote, joined to dimensions
-- ------------------------------------------------------------------
DELETE FROM fact_quote;

INSERT INTO fact_quote
    (raw_quote_id, created_at, location_id, utility_id, tariff_id,
     system_kw, viability_score, payback_years, npv_25yr, irr, lcoe,
     co2_avoided_tons, confidence_level, geocode_source,
     rate_source, export_policy)
SELECT
    q.id AS raw_quote_id,
    q.fetched_at AS created_at,
    loc.location_id,
    util.utility_id,
    -- tariff: pick the version effective at quote time
    (SELECT t.tariff_id FROM dim_tariff t
     WHERE t.utility_id = util.utility_id
       AND t.rate_name = q.rate_name
       AND t.effective_from <= q.fetched_at::DATE
       AND (t.effective_to IS NULL OR t.effective_to > q.fetched_at::DATE)
     LIMIT 1) AS tariff_id,
    q.system_kw,
    q.viability_score,
    q.payback_years,
    q.npv_25yr,
    q.irr,
    q.lcoe,
    q.co2_avoided_tons,
    q.confidence_level,
    -- geocode source: look up the matching raw_geocode row
    (SELECT g.source FROM raw_geocode g
     WHERE g.lat = q.lat AND g.lon = q.lon
     ORDER BY g.fetched_at DESC LIMIT 1) AS geocode_source,
    q.rate_source,
    q.export_policy
FROM raw_quote q
LEFT JOIN dim_location loc ON loc.lat = q.lat AND loc.lon = q.lon
LEFT JOIN dim_utility util ON util.utility_name = q.utility_name;


-- ------------------------------------------------------------------
-- fact_rate_history — time series of state-level rates from EIA
-- ------------------------------------------------------------------
DELETE FROM fact_rate_history;

INSERT INTO fact_rate_history (state, period, rate_dollars_per_kwh, source)
SELECT
    state,
    -- period is a 'YYYY-MM' string; cast to first of month
    CASE
        WHEN period LIKE '____-__' THEN (period || '-01')::DATE
        WHEN period LIKE '____' THEN (period || '-01-01')::DATE
        ELSE NULL
    END AS period,
    rate_dollars_per_kwh,
    source
FROM raw_eia
WHERE state IS NOT NULL AND rate_dollars_per_kwh IS NOT NULL
  AND period IS NOT NULL;
