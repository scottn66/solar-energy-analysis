# Architecture Overview

This document explains how the modules connect and where to look when something breaks.

## Module Map

```
User Input ("94061" or "San Jose, CA")
        │
        v
┌─────────────────────────────────────────────────────┐
│  solar_fetch.py  (ORCHESTRATOR)                     │
│  quote_from_location() ties everything together     │
└───────┬──────────┬──────────┬──────────┬────────────┘
        │          │          │          │
        v          v          v          v
  solar_geocode  solar_pvwatts  solar_urdb  solar_nem
  .py            .py            .py         .py
  ZIP/Census/    NREL PVWatts   URDB rate   NEM export
  Nominatim      production     lookup      policy
        │          │          │          │
        └──────────┴──────────┴──────────┘
                       │
                       v
              solar_economics.py
              score_site() → SiteResult
                       │
    ┌──────────────────┼──────────────────┐
    v                  v                  v
solar_viz.py       app.py          solar_etl.py
HTML report       FastAPI web UI  Persists to DuckDB
(Plotly charts)   (HTMX frontend) warehouse (data/warehouse/solar.duckdb)
                                         │
                                         v
                                  Teammates query DB
                                  directly (no API keys)
```

The **warehouse layer** is where teammates hook in for EDA/analytics — see
`docs/WAREHOUSE.md` for the full schema.

## Where to find things

| I want to...                          | Look at...                    |
|---------------------------------------|-------------------------------|
| Change how addresses are resolved     | `solar_geocode.py`            |
| Change how solar production is estimated | `solar_pvwatts.py`         |
| Change how utility rates are looked up | `solar_urdb.py`              |
| Change NEM export compensation        | `solar_nem.py`                |
| Change the financial formulas         | `solar_economics.py`          |
| Change what the report looks like     | `solar_viz.py`                |
| Change the web app UI                 | `app.py`                      |
| Add a new data source                 | Start in `solar_fetch.py`     |
| Run the pipeline from code            | `solar_fetch.quote_from_location()` |
| Run the pipeline from CLI             | `python3 -m solar_fetch ...`  |
| Persist quote to the warehouse        | `solar_etl.etl_quote()` or `python3 solar_etl.py` |
| Query the warehouse (no API keys)     | `solar_warehouse.get_conn()` or `duckdb data/warehouse/solar.duckdb` |

## Data files

| File                          | What it is                            | When to update          |
|-------------------------------|---------------------------------------|-------------------------|
| `data/uszips.csv`             | US ZIP code lat/lon lookup (41K zips) | Rarely (postal boundaries) |
| `data/eia_state_rates_2025.csv` | State avg electricity prices        | Annually from EIA       |
| `data/nem3_acc_2025.csv`      | CA NEM 3.0 export rate schedule       | Annually from CPUC      |

## Caching

All API responses are cached in `~/.solar_cache/` as SQLite databases:

| Cache file       | TTL     | What's in it                    |
|------------------|---------|---------------------------------|
| `geocode.sqlite` | 30 days | Address → lat/lon lookups       |
| `pvwatts.sqlite` | 30 days | Solar production estimates      |
| `urdb.sqlite`    | 7 days  | Utility rate structures         |

To clear caches and force fresh API calls:
```bash
rm -rf ~/.solar_cache/
```

## Test structure

- `test_solar_economics.py` — 39 unit tests for the financial engine (no API calls)
- `test_integration.py` — 20 tests for the full pipeline (all HTTP mocked)

Run all tests:
```bash
pytest test_solar_economics.py test_integration.py -v
```
