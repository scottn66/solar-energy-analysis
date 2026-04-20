# DuckDB Warehouse Reference

**Path:** `data/warehouse/solar.duckdb`

This is the project's analytical warehouse. Every time `solar_etl.py` runs a quote, it writes all intermediate results here. Teammates query it directly instead of calling APIs.

## Why this exists

1. **No API keys needed for analysis** — Sayli and Shraddha work entirely from the `.duckdb` file
2. **Historical tracking** — rate changes, policy shifts, quote outcomes preserved over time
3. **Faster quotes** — DB reads are <10ms vs 2-5s for API calls (Phase 3)
4. **Audit trail** — every number in a quote can be traced back to its source API response

## Quick start

```bash
# Make sure it exists / is up to date
python3 solar_etl.py --status

# Populate it with a quote (needs API keys)
python3 solar_etl.py --location 94061 --monthly-kwh 650

# Query it (no keys needed)
duckdb data/warehouse/solar.duckdb
# or from Python:
python3 -c "from solar_warehouse import get_conn; print(get_conn().execute('SELECT * FROM raw_quote').df())"
```

## Schema

### Staging tables (raw_*)

Every API call captured verbatim, with the full JSON response preserved for auditability.

#### `raw_pvwatts`
One row per NREL PVWatts API call.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Auto-increment |
| `fetched_at` | TIMESTAMP | When we called the API |
| `lat`, `lon` | DOUBLE | Site coordinates |
| `system_kw` | DOUBLE | Requested system size |
| `ac_annual_kwh` | DOUBLE | Annual production estimate |
| `capacity_factor` | DOUBLE | % |
| `station_distance_m` | DOUBLE | Nearest weather station |
| `pvwatts_version` | VARCHAR | API version (e.g. "8.5.0") |
| `response_json` | JSON | Full PVWatts response |

#### `raw_urdb`
One row per utility rate lookup (URDB, NREL v3, EIA fallback, or bundled TOU).

| Column | Type | Notes |
|---|---|---|
| `utility_name` | VARCHAR | e.g. "Pacific Gas & Electric Co" |
| `rate_name` | VARCHAR | e.g. "E-TOU-C (bundled 2024 schedule)" |
| `flat_rate` | DOUBLE | Weighted average $/kWh |
| `is_tou` / `is_tiered` | BOOLEAN | Rate structure |
| `source` | VARCHAR | `urdb_full` / `urdb_tiered_avg` / `nrel_v3` / `eia_live` / `bundled_tou` |
| `effective_date` | DATE | When the rate took effect |
| `response_json` | JSON | Full URDB tariff JSON |

#### `raw_eia`
One row per EIA state-level rate lookup.

| Column | Type | Notes |
|---|---|---|
| `state` | VARCHAR | Two-letter code |
| `period` | VARCHAR | e.g. "2026-01" |
| `rate_dollars_per_kwh` | DOUBLE | Residential avg |
| `source` | VARCHAR | `eia_live` or `eia_bundled_2025` |

#### `raw_geocode`
One row per address → lat/lon resolution.

| Column | Type | Notes |
|---|---|---|
| `query` | VARCHAR | What the user typed |
| `source` | VARCHAR | `uszips` / `census` / `nominatim` |
| `confidence` | VARCHAR | `high` / `medium` / `low` |

#### `raw_quote`
One row per end-to-end quote. All the top-line numbers, plus the full `QuoteResult` JSON.

| Column | Type | Notes |
|---|---|---|
| `location_query` | VARCHAR | Original input (e.g. "94061") |
| `viability_score` | DOUBLE | 0-100 |
| `viability_label` | VARCHAR | "Excellent", "Good", etc. |
| `payback_years` | DOUBLE | Simple payback |
| `npv_25yr` | DOUBLE | Net present value |
| `irr` | DOUBLE | Internal rate of return |
| `lcoe` | DOUBLE | Levelized cost of energy |
| `confidence_level` | VARCHAR | `high` / `medium` / `low` |
| `full_result_json` | JSON | The complete `QuoteResult` dict |

### Mart tables (dim_*, fact_*)

**Note:** mart tables exist in the schema but are empty until Phase 2 (the mart build script) lands. See `sql/build_marts.sql` (coming soon).

- `dim_location` — deduplicated sites
- `dim_utility` — utility territories
- `dim_tariff` — rate schedules with SCD Type 2 (`effective_from`, `effective_to`)
- `fact_quote` — quote outcomes joinable to dimensions
- `fact_rate_history` — rate time series for trend analysis

## Example queries for teammates

### What's the latest quote for each CA ZIP?
```sql
SELECT location_query, viability_score, payback_years, utility_name
FROM raw_quote
WHERE state = 'CA'
QUALIFY row_number() OVER (PARTITION BY zip_code ORDER BY fetched_at DESC) = 1;
```

### Have PG&E rates changed in the data we've collected?
```sql
SELECT rate_name, flat_rate, effective_date, fetched_at
FROM raw_urdb
WHERE utility_name LIKE '%Pacific Gas%'
ORDER BY fetched_at DESC;
```

### Distribution of viability scores by state
```sql
SELECT state, count(*) as n, round(avg(viability_score), 1) as avg_score
FROM raw_quote
GROUP BY state
ORDER BY avg_score DESC;
```

### Which API sources produced each quote?
```sql
SELECT location_query, rate_source, confidence_level, viability_score
FROM raw_quote
ORDER BY fetched_at DESC;
```

## Connecting from a notebook

```python
import pandas as pd
from solar_warehouse import get_conn

conn = get_conn()
df = conn.execute("SELECT * FROM raw_quote").df()  # .df() returns a DataFrame
df.head()
```

## When to refresh

Staging tables grow forever (keeping the history). If your `.duckdb` file gets large:

```python
# Keep last 90 days only
from solar_warehouse import get_conn
conn = get_conn()
conn.execute("""
    DELETE FROM raw_pvwatts
    WHERE fetched_at < now() - INTERVAL 90 DAYS
""")
```

For the semester, don't prune. The history is the point.

## Who needs API keys?

Only **the person running `solar_etl.py`**. Once the `.duckdb` file is populated and shared (via git or a file transfer), everyone else can query without keys.
