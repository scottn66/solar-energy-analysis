# Solar Viability

**Is rooftop solar worth it at this address?** Type a US ZIP or street address and get a 25-year cashflow — LCOE, payback, NPV, IRR, and a 0–100 viability score — with every assumption documented.

**Live demo:** [scottn66.github.io/solar](https://scottn66.github.io/solar/)  
Look up any California ZIP, open city reports (including **Bend, Oregon**), and read the [technical paper](https://scottn66.github.io/solar/paper.pdf).

This is an independent research model, not a sales tool and not financial advice.

---

## What you get

1. Production from [NREL PVWatts](https://pvwatts.nrel.gov/) at the geocoded coordinate (NASA POWER climatology if NREL is unreachable).
2. Utility rates from OpenEI URDB, with a staleness guard that replaces decade-old tariffs with current bundled schedules — CA IOUs (PG&E, SCE, SDG&E) and Oregon utilities (Pacific Power, Portland General Electric, Central Electric Co-op, Midstate Electric Co-op).
3. Export policy from a state NEM table — California **NEM 3.0** avoided-cost rates, 1:1 net metering in Oregon (ORS 757.300) and 22 other states, Oregon co-op monthly netting, reduced-rate fallbacks elsewhere.
4. Install cost and market maturity from [Berkeley Lab Tracking the Sun](https://emp.lbl.gov/tracking-the-sun) (2.6 million cleaned residential records) when the warehouse is loaded.
5. A 25-year discounted cashflow with the 30% federal ITC (US only), degradation, O&M, and rate escalation.

California is the deep market: 2,593 ZIP scores, NEM 3.0, and IOU vs municipal tariffs. Oregon is first-class — especially Central Oregon around Bend and Redmond, where the high desert out-yields the wet side of the Cascades. Production APIs work worldwide; outside the US you pass `--rate` (your local $/kWh) and the federal ITC is turned off.

## Quick start

```bash
git clone https://github.com/scottn66/solar-energy-analysis.git
cd solar-energy-analysis
pip install -r requirements.txt
cp .env.example .env
# Add a free NREL key: https://developer.nrel.gov/signup/
# Optional EIA key: https://www.eia.gov/opendata/register.php

python3 -m solar_fetch 97701 --system-kw 4.5 --output bend.html
python3 -m solar_fetch 94061 --monthly-kwh 650
python3 -m solar_fetch 97756 --monthly-kwh 900          # Redmond, Central Electric Co-op
python3 -m solar_fetch "London, UK" --rate 0.28 --system-kw 4.5

uvicorn app:app --reload   # http://localhost:8000
```

```bash
pytest test_solar_economics.py test_integration.py test_oregon.py -v
python3 verify_system.py    # live APIs; needs keys

python3 solar_etl.py --batch data/oregon_locations.csv
```

## Architecture

The web app is OLTP (one quote, fast). The DuckDB file is OLAP (history + 2.6M installs). Only the ETL path needs API keys.

```
Address / ZIP
    → geocode (uszips → Census → Nominatim, including non-US)
    → NREL PVWatts (NASA POWER fallback)
    → URDB / bundled schedule / EIA   (or --rate outside the US)
    → NEM policy
    → 25-year score_site()
    → HTML report
         ↘ solar.duckdb  (cache + TTS warehouse)
```

| I want to… | Look at |
|---|---|
| Change the financial formulas | `solar_economics.py` |
| Change NEM export compensation | `solar_nem.py` |
| Change utility-rate lookup | `solar_urdb.py` |
| Change the report | `solar_viz.py` |
| Run the pipeline | `python3 -m solar_fetch …` |
| Query installs in SQL | `duckdb data/warehouse/solar.duckdb` |

Assumptions, warehouse schema, tests, and module map: [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md), [`docs/WAREHOUSE.md`](docs/WAREHOUSE.md), [`docs/TESTS.md`](docs/TESTS.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Scope

**In:** US addresses and ZIPs; first-class California and Oregon treatment (bundled current tariffs, city reports, ZIP heatmaps); California NEM 3.0; 1:1 NEM for 23 states including Oregon; bill-sized or user-specified kW; documented, overridable assumptions.

**Out:** Batteries (self-consumption is a fixed fraction), commercial/industrial, live weather, installer lead-gen. Non-US financials are an estimate that needs your local tariff.

## Origin

Started as the group project for DATA 201 (Database Technologies) at San José State University, Spring 2026. The public version is maintained by [Scott Nelson](https://scottn66.github.io/).
