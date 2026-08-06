# Solar Energy Analysis

**DATA 201 Group Project** &nbsp;·&nbsp; Scott Nelson, Sayli, Shraddha

> *"Is solar worth it at this address?"* — Type any US address or ZIP code, get a financial analysis with 8 interactive charts in under a second.

---

## 1 · Intention

**Problem.** Residential solar economics are hard to evaluate. The real numbers depend on your local utility's current rate structure, your state's net-metering policy, local installation costs, and the physical site's solar resource. Consumer-facing calculators (EnergySage, Project Sunroof) wrap these in a sales funnel; they don't show you the math or let you challenge the assumptions.

**Goal.** Build an honest, transparent solar viability tool that:
1. Pulls data from authoritative federal sources (NREL, EIA, OpenEI URDB, NASA).
2. Runs a 25-year cashflow model with every assumption documented and overridable.
3. Produces a single-page HTML report with hover tooltips explaining what each metric means.
4. Works for any US address without signup, tracking, or lead capture.

**Audience.** Our classmates, our instructor, and anyone curious whether a specific address makes sense for solar.

---

## 2 · Methodology — OLTP / OLAP split

The project separates *transactional* work (one quote at a time, served fast) from *analytical* work (batch aggregations, trend analysis, EDA across many quotes). This is the standard data-engineering pattern and it gives us three concrete benefits: the web app is fast, API keys live in one place only, and teammates can run unlimited SQL against a shared history without hitting rate limits.

```
   OLTP (per-request, low-latency)        OLAP (batch, analytical)
   ──────────────────────────────         ────────────────────────────────
   app.py                                 Teammates running EDA
   "Score this one address, now"          "How does score vary by state?"
                │                                        │
                ▼                                        ▼
            ┌─────────────────────────────────────────────────┐
            │  data/warehouse/solar.duckdb                    │
            │  (5 raw_* staging tables — shared source of     │
            │   truth with JSON response history preserved)   │
            └─────────────────────────┬───────────────────────┘
                                      ▲
                                      │  (only on cache miss)
                                      │
                            solar_etl.py → live APIs
                            (ONLY code path that needs
                             NREL + EIA keys)
```

**The three layers in one sentence each:**

| Layer | What it does | Who runs it |
|---|---|---|
| **OLTP** — `app.py` | Checks warehouse first; cache hit = rebuild report in <100ms. | End users via `uvicorn app:app` |
| **ETL** — `solar_etl.py` | Runs the full pipeline; persists every intermediate result. | Scott (the one with API keys) |
| **OLAP** — `solar.duckdb` | Queryable history: every quote, rate lookup, and API response. | Sayli & Shraddha for EDA |

**Why DuckDB** (not Postgres, BigQuery, SQLite): single file committable to git, columnar storage for fast aggregations, zero config, proper SQL dialect including window functions and `QUALIFY`. Meets the "simple for teammates" requirement without sacrificing capability.

---

## 3 · Scope

### In scope
- **Geographies:** any US address or 5-digit ZIP code. Two regions get first-class treatment with bundled current tariffs, city reports, and ZIP heatmaps: **California** and **Oregon** (including the Central Oregon high desert around Bend/Redmond).
- **System sizing:** user-specified kW, bill-sized from monthly kWh, or default 5 kW.
- **Rate data:** URDB (with staleness detection → bundled schedule for PG&E/SCE/SDG&E and the Oregon utilities Pacific Power, Portland General Electric, Central Electric Co-op, Midstate Electric Co-op → EIA state average → US median).
- **NEM policies:** California NEM 3.0 avoided-cost, 1:1 net metering for 23 states (incl. Oregon per ORS 757.300), Oregon co-op monthly netting, reduced/deregulated fallbacks.
- **Financial model:** 25-year cashflow with degradation, escalation, ITC, O&M, NEM export; produces LCOE, payback, NPV, IRR, viability score.

### Out of scope (deliberately)
- **Batteries** — model assumes no on-site storage. Self-consumption is a fixed fraction.
- **Commercial / industrial sites** — sector filter is hardcoded to Residential.
- **Non-US locations** — the geocoder returns only US matches.
- **Real-time weather** — production is based on PVWatts TMY (typical meteorological year), not live forecasts.
- **Live submission to installers** — no integration with any sales channel.

---

## 4 · Critical pieces

### The orchestrator: `solar_fetch.quote_from_location()`
The single entry point that ties every data source to the economics model. Any change to the pipeline touches this function.

### The warehouse: `solar_warehouse.py` + `solar.duckdb`
Five `raw_*` staging tables with full JSON response history. Two tables you'll actually query (`raw_quote`, `raw_eia`); three audit tables you won't (`raw_pvwatts`, `raw_urdb`, `raw_geocode`). See [`docs/WAREHOUSE.md`](docs/WAREHOUSE.md).

### The rate staleness guard: `solar_urdb.get_rate()` + bundled schedules
URDB has PG&E data from 2014 ($0.15/kWh) but the real rate is $0.38+. The staleness detector catches this and swaps in a bundled current rate schedule — TOU for the CA IOUs, flat 2025/2026 tariffs for the Oregon utilities (Pacific Power, Portland General Electric, and the Central Oregon co-ops). Without this fix every CA quote scored "marginal" instead of "excellent" — with it, scores match reality.

### The rehydrator: `solar_warehouse.quote_from_dict()`
Turns a cached JSON row back into a full `QuoteResult` object tree. This is what makes the OLTP cache hit work without re-running any pipeline code.

### The economics engine: `solar_economics.score_site()`
45-field `SiteResult` dataclass with every parameter overridable via `Assumptions`. 43 unit tests pin down the math.

### The report: `solar_viz.py`
Plotly-based single-file HTML report. Hover tooltips on every metric explain what it means. Provenance section shows exactly which API each number came from.

---

## Getting Started

```bash
# Clone and install
git clone https://github.com/scottn66/solar-energy-analysis.git
cd solar-energy-analysis
pip install -r requirements.txt

# Set up API keys (one-time)
cp .env.example .env
# Edit .env: add NREL_API_KEY (required) and EIA_API_KEY (optional, enables live rates)

# Verify the full system works end-to-end
python3 verify_system.py
# Expected: 28/28 passed

# Run tests
pytest test_solar_economics.py test_integration.py test_oregon.py -v
# Expected: 102 passed
```

### Usage

```bash
# Get a quick quote on the terminal
python3 -m solar_fetch 94061 --monthly-kwh 650

# Central Oregon quote (Redmond — Pacific Power city core; CEC serves the fringe)
python3 -m solar_fetch 97756 --monthly-kwh 900

# Generate an interactive HTML report
python3 -m solar_fetch 94061 --monthly-kwh 650 --output report.html

# Add a quote to the warehouse (the OLAP/ETL path)
python3 solar_etl.py --location 94061 --monthly-kwh 650

# Populate the warehouse for the whole Oregon region in one command
python3 solar_etl.py --batch data/oregon_locations.csv

# Check warehouse contents
python3 solar_etl.py --status

# Launch the web app (OLTP path, reads from warehouse first)
uvicorn app:app --reload
# Open http://localhost:8000

# Query the warehouse from SQL (teammate workflow — no API keys needed)
duckdb data/warehouse/solar.duckdb
> SELECT location_query, viability_score, utility_name FROM raw_quote;
```

---

## Verification & CI

- **`python3 verify_system.py`** — 28 end-to-end checks across 8 phases (schema, ETL, cache, numerical equivalence, multi-location, error handling, test suite).
- **`pytest`** — 102 automated tests (43 economics + 26 integration & warehouse + 33 Oregon-region). CI runs on every push via `.github/workflows/test.yml`.
- **Last verified:** 28/28 verification checks + 102/102 tests passing.

---

## Project structure

```
Working files:
  solar_fetch.py            Orchestrator — ties everything together
  solar_economics.py        Financial math (score_site)
  solar_viz.py              Chart generation & HTML report
  app.py                    FastAPI OLTP layer with cache-first reads

Data pipeline modules:
  solar_geocode.py          Address → coordinates
  solar_pvwatts.py          NREL solar production
  solar_urdb.py             Utility rate lookup + staleness guard
  solar_eia.py              EIA live state rates
  solar_nem.py              NEM export policy

Warehouse layer (OLAP):
  solar_warehouse.py        DuckDB schema + latest_quote + quote_from_dict
  solar_etl.py              Run pipeline + persist to warehouse
  data/warehouse/solar.duckdb   (the file itself — gitignored)

Verification & tests:
  verify_system.py          End-to-end smoke test (28 assertions)
  test_solar_economics.py   43 unit tests
  test_integration.py       26 integration tests (warehouse + HTTP-mocked pipeline)
  test_oregon.py            33 Oregon-region tests (rates, NEM, cities, yield model)

Exploration:
  notebooks/                Jupyter EDA notebooks
  sample_site.csv           Single-row sample input for testing

Reference data:
  data/uszips.csv           US ZIP code coordinates
  data/eia_state_rates_2025.csv   Bundled state electricity prices
  data/nem3_acc_2025.csv          CA NEM 3.0 export rate schedule
  data/utility_tou_schedules.csv  Bundled rate schedules (CA IOUs + OR utilities)
  data/oregon_locations.csv       Curated Oregon batch for solar_etl.py --batch
```

---

## Deep Dives

- **[`docs/WAREHOUSE.md`](docs/WAREHOUSE.md)** — warehouse schema, example queries, teammate workflow
- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — module map, where-to-find-things, caching
- **[`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md)** — every financial parameter with source and rationale
- **[`docs/TESTS.md`](docs/TESTS.md)** — test suite reference (all 102 tests catalogued)
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — branching strategy and PR workflow

---

## Team

| Member | Primary focus |
|---|---|
| Scott | Pipeline architecture, warehouse, economics engine |
| Sayli | *(unclaimed — see "Good areas to pick up" below)* |
| Shraddha | *(unclaimed — see "Good areas to pick up" below)* |

**Good areas to pick up:**
- **EDA & storytelling** — query the warehouse across states/utilities and write up the patterns you find
- **Data quality** — validate scoring against known real installations; flag implausible outputs
- **New locations** — run `solar_etl.py` on a diverse batch of addresses and compare the results
- **Presentation** — distill the HTML report into slides for the final project
