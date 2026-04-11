# Solar Energy Analysis - DATA 201

Full-stack solar viability analysis: from a plain-text address to a premium analyst-grade HTML report with real utility rates, NEM export policy, and 25-year financial projections.

## Data Flow

```
                          quote_from_location("95112")
                                    |
                 +------------------+------------------+
                 |                                     |
          solar_geocode.py                      solar_pvwatts.py
          ZIP / Census / Nominatim              NREL PVWatts v8
          "95112" -> (37.35, -121.89, CA)       -> 8,195 kWh/yr
                 |                                     |
                 +------------------+------------------+
                                    |
                 +------------------+------------------+
                 |                                     |
           solar_urdb.py                        solar_nem.py
           URDB -> NREL v3 -> EIA fallback      NEM 3.0 / 1:1 / default
           PG&E E-1: $0.32/kWh (TOU 8760hr)    CA: ~$0.055/kWh export
                 |                                     |
                 +------------------+------------------+
                                    |
                          solar_economics.py
                          score_site() -> SiteResult
                          viability: 94/100, payback: 5.7yr
                                    |
                 +------------------+------------------+
                 |                                     |
           solar_viz.py                           app.py
           HTML report (7 charts)                 FastAPI + HTMX
           provenance, TOU overlay                "Is solar worth it?"
```

## Quick Start

```bash
# Clone and install
git clone https://github.com/scottn66/solar-energy-analysis.git
cd solar-energy-analysis
pip install -r requirements.txt

# Add your NREL API key
cp .env.example .env
# Edit .env and paste your key (get one free at developer.nrel.gov/signup)

# Run a quote from the CLI
python -m solar_fetch "San Jose, CA" --monthly-kwh 650 --output report.html

# Or start the web app
uvicorn app:app --reload
# Open http://localhost:8000

# Run all 59 tests
pytest test_solar_economics.py test_integration.py -v
```

## Project Structure

```
Core Pipeline:
  solar_geocode.py         Address -> (lat, lon, state, zip)
  solar_pvwatts.py         (lat, lon) -> annual/monthly production
  solar_urdb.py            (lat, lon) -> utility rate (flat/tiered/TOU)
  solar_nem.py             (state, rate) -> export compensation
  solar_fetch.py           Orchestrator: location string -> QuoteResult
  solar_economics.py       Financial engine: score_site() -> SiteResult

Presentation:
  solar_viz.py             Plotly HTML report generator (7 charts)
  app.py                   FastAPI + HTMX web frontend

Tests:
  test_solar_economics.py  39 tests: economics engine, sub-scores, edge cases
  test_integration.py      20 tests: geocode, pvwatts, urdb, nem, pipeline, FastAPI

Data:
  data/uszips.csv          41,490 US ZIP centroids (pgeocode/GeoNames)
  data/eia_state_rates_2025.csv   EIA residential averages by state
  data/nem3_acc_2025.csv   CA NEM 3.0 avoided-cost approximation (12mo x 24hr)

Notebooks:
  notebooks/01_nrel_api.ipynb       NREL PVWatts API exploration
  notebooks/02_nasa_api.ipynb       NASA POWER API exploration
  notebooks/03_data_loading.ipynb   Berkeley Lab data download
  notebooks/04_eda_peninsula.ipynb  Bay Area solar EDA
  notebooks/05_data_dictionary.ipynb  Column reference
  notebooks/06_pipeline.ipynb       Integration pipeline
```

## CLI Examples

```bash
# Simple ZIP code
python -m solar_fetch 95112

# Full address with bill-based sizing
python -m solar_fetch "1600 Amphitheatre Pkwy, Mountain View, CA" --monthly-kwh 650

# Custom system size with HTML report output
python -m solar_fetch "Boulder, CO" --system-kw 8 --output boulder_report.html

# Fast mode (NREL v3 rate, no URDB)
python -m solar_fetch 95192 --fast

# Future installation date (affects NEM policy)
python -m solar_fetch "San Diego, CA" --install-date 2026-06-01

# Verbose logging
python -m solar_fetch 78701 -v
```

## Data Sources and Freshness

| Source | Used By | Cache TTL | Refresh Instructions |
|--------|---------|-----------|---------------------|
| **NREL PVWatts v8** | `solar_pvwatts.py` | 30 days | Automatic via API |
| **OpenEI URDB** | `solar_urdb.py` | 7 days | Automatic via API |
| **NREL Utility Rates v3** | `solar_urdb.py` (fallback) | 7 days | Automatic via API |
| **US Census Geocoder** | `solar_geocode.py` | 30 days | Automatic via API |
| **Nominatim/OSM** | `solar_geocode.py` (fallback) | 30 days | Automatic via API |
| **EIA State Rates** | `data/eia_state_rates_2025.csv` | Bundled | Update annually from [EIA Table 5.6.A](https://www.eia.gov/electricity/monthly/epm_table_5_6_a.html) |
| **US ZIP Centroids** | `data/uszips.csv` | Bundled | Regenerate from [GeoNames](https://www.geonames.org/) via pgeocode |
| **CA NEM 3.0 ACC** | `data/nem3_acc_2025.csv` | Bundled | Approximate; update from [CPUC ACC](https://www.cpuc.ca.gov/industries-and-topics/electrical-energy/demand-side-management/net-energy-metering/nem-revisit/cost-effectiveness) |
| **NEM State Policies** | `solar_nem.py` (hardcoded) | N/A | Review annually via [DSIRE](https://www.dsireusa.org/) |

All API responses are cached in `~/.solar_cache/` as SQLite databases via `requests_cache`.

## Assumptions Reference

Every parameter is overridable via the `Assumptions` dataclass.

| Parameter | Default | Rationale |
|-----------|---------|-----------|
| `system_life_years` | 25 | Industry-standard warranty horizon for c-Si PV |
| `degradation_rate` | 0.005 (0.5%/yr) | NREL long-term field studies (Jordan & Kurtz 2013) |
| `discount_rate` | 0.06 (6%) | Nominal WACC for residential solar |
| `om_cost_per_kw_year` | $20 | Inverter reserves, cleaning, monitoring (NREL ATB 2024) |
| `federal_itc` | 0.30 (30%) | Investment Tax Credit under IRA through 2032 |
| `electricity_escalation` | 0.025 (2.5%/yr) | EIA Annual Energy Outlook reference case |
| `nem_export_ratio` | 0.75 | Fraction of retail credited for exports |
| `self_consumption_rate` | 0.40 | Fraction consumed on-site at full retail |
| `co2_intensity_tons_per_kwh` | 0.0004 | US avg grid intensity (EPA eGRID 2022) |
| `default_price_per_watt` | $3.50 | Fallback $/W if TTS missing (EnergySage 2024) |

## Agile Workflow

We use GitHub Issues and GitHub Projects for task management:
- Issues labeled by sprint (`sprint-1`, `sprint-2`) and category
- See `CONTRIBUTING.md` for branching strategy and PR workflow

## Team

| Member | Role |
|--------|------|
| | |
| | |
| | |
| | |
