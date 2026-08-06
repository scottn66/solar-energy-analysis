# DuckDB Warehouse

**Path:** `data/warehouse/solar.duckdb`

The warehouse is where every quote, rate lookup, and API response gets saved. It's the **OLAP** side of the project — built for analytical queries across many sites, not for serving individual live requests.

## The 30-second explanation

- **The web app (`app.py`) = OLTP.** Fast individual responses. One quote at a time.
- **The warehouse (`solar.duckdb`) = OLAP.** Shared history. Many quotes, many rates, over time.
- **The ETL (`solar_etl.py`) = the only thing that needs API keys.** It populates the warehouse.

When the app gets a request, it checks the warehouse first. If there's a recent quote for that location, it's served in ~50ms with no API calls. Otherwise the ETL runs, the result is persisted, and everyone benefits (including teammates doing EDA).

## Two tables you'll actually use

### `raw_quote` — one row per solar analysis

Everything a teammate needs for stats across sites. Includes the viability score, payback, NPV, IRR, LCOE, CO2, utility name, rate source, confidence level, and the full JSON result.

```sql
SELECT location_query, state, viability_score, payback_years, utility_name
FROM raw_quote
ORDER BY fetched_at DESC
LIMIT 10;
```

### `raw_eia` — time series of state electricity rates

Each EIA rate lookup is logged here. Use it to see how rates change month over month.

```sql
SELECT state, period, rate_dollars_per_kwh, fetched_at
FROM raw_eia
WHERE state = 'CA'
ORDER BY period DESC;
```

## Two commands

```bash
# Add a new quote (needs API keys)
python3 solar_etl.py --location 94061 --monthly-kwh 650

# Query everything (no keys needed)
duckdb data/warehouse/solar.duckdb
```

## Populating a whole region at once

`--batch` runs the full pipeline for every row of a location CSV and stages
each result — one command to give the warehouse regional coverage. A curated
Oregon file ships in the repo (Central-Oregon-heavy: Bend, Redmond, Sisters,
La Pine, Sunriver, Prineville, Madras, Terrebonne + statewide anchors):

```bash
# Needs API keys, ~1-2s per location; failing rows are skipped, not fatal
python3 solar_etl.py --batch data/oregon_locations.csv
```

Then compare the region in SQL:

```sql
SELECT location_query, viability_score, payback_years,
       utility_name, electricity_rate_used, export_policy
FROM raw_quote
WHERE state = 'OR'
ORDER BY viability_score DESC;
```

To batch a different region, copy the CSV format: `location` (ZIP or
"City, ST"), optional `monthly_kwh` / `system_kw`, and a free-text `note`.

Or from Python:
```python
from solar_warehouse import get_conn
df = get_conn().execute("SELECT * FROM raw_quote").df()
```

## Three audit tables

You probably won't touch these, but they're there for debugging or re-parsing raw responses if a formula changes:

| Table | What it stores |
|---|---|
| `raw_pvwatts` | Every NREL PVWatts call, with the full JSON response |
| `raw_urdb` | Every utility rate lookup (URDB, NREL v3, EIA, bundled TOU) |
| `raw_geocode` | Every address → lat/lon resolution |

## Example queries for your EDA

### How does viability score vary by state?
```sql
SELECT state, count(*) AS n, round(avg(viability_score), 1) AS avg_score
FROM raw_quote
GROUP BY state
ORDER BY avg_score DESC;
```

### Which utilities have we quoted the most?
```sql
SELECT utility_name, count(*) AS n_quotes, round(avg(viability_score), 1) AS avg_score
FROM raw_quote
GROUP BY utility_name
ORDER BY n_quotes DESC;
```

### Cache hit analysis — how many quotes reuse a recent result?
```sql
SELECT location_query, count(*) AS hits,
       min(fetched_at) AS first_seen,
       max(fetched_at) AS last_seen
FROM raw_quote
GROUP BY location_query
HAVING count(*) > 1;
```

### Latest quote per ZIP in California
```sql
SELECT location_query, viability_score, payback_years, utility_name
FROM raw_quote
WHERE state = 'CA'
QUALIFY row_number() OVER (PARTITION BY zip_code ORDER BY fetched_at DESC) = 1;
```

### How have EIA rates moved over the last year?
```sql
SELECT state, period, rate_dollars_per_kwh
FROM raw_eia
WHERE state IN ('CA', 'TX', 'NY', 'HI')
ORDER BY period DESC, state;
```

## Connecting from a notebook

```python
import pandas as pd
from solar_warehouse import get_conn

conn = get_conn()
df = conn.execute("SELECT * FROM raw_quote").df()
df.describe()
```

## Who needs API keys?

Only the person running `solar_etl.py` (or starting the FastAPI app, since it may call ETL on cache miss). Once the `.duckdb` file is populated and shared, **teammates running EDA don't need any keys**. That's the whole point.

## When to prune

Staging tables grow forever. For a semester project, don't prune — the history is the point. If the file gets >500 MB, you can trim old rows:

```python
from solar_warehouse import get_conn
conn = get_conn()
conn.execute("DELETE FROM raw_pvwatts WHERE fetched_at < now() - INTERVAL 90 DAYS")
```
