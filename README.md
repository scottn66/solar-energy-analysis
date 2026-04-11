# Solar Energy Analysis

**DATA 201 Group Project**

> *"Is solar worth it at this address?"* — Type any US address or ZIP code and get a full financial analysis with interactive charts.

---

## Getting Started (for team members)

### 1. Clone the repo

```bash
git clone https://github.com/scottn66/solar-energy-analysis.git
cd solar-energy-analysis
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up your API key

We use a free NREL key to pull solar data. Get one in 30 seconds at [developer.nrel.gov/signup](https://developer.nrel.gov/signup/), then:

```bash
cp .env.example .env
# Open .env and paste your key after NREL_API_KEY=
```

### 4. Verify everything works

```bash
pytest test_solar_economics.py test_integration.py -v
# Should show: 59 passed
```

### 5. Try it out

```bash
# Quick quote for any ZIP code
python3 -m solar_fetch 94027

# With your monthly electricity usage (more accurate sizing)
python3 -m solar_fetch "San Jose, CA" --monthly-kwh 650

# Generate a full interactive HTML report
python3 -m solar_fetch 94027 --monthly-kwh 650 --output report.html

# Launch the web app
uvicorn app:app --reload
# Then open http://localhost:8000
```

---

## How It Works

When you enter an address, the system runs this pipeline automatically:

```
  "94027"
     |
     v
 +------------------+     +------------------+
 |  1. GEOCODE      |     |  2. SOLAR DATA   |
 |  ZIP -> lat/lon  |---->|  NREL PVWatts    |
 |  Census fallback |     |  8,195 kWh/yr    |
 +------------------+     +------------------+
                                   |
                    +--------------+--------------+
                    |                             |
             +------+-------+           +---------+--------+
             | 3. RATE LOOK |           | 4. EXPORT POLICY |
             |    UP        |           |   NEM 3.0 / 1:1  |
             |  URDB / EIA  |           |   by state       |
             +--------------+           +------------------+
                    |                             |
                    +-------------+---------------+
                                  |
                                  v
                    +----------------------------+
                    |  5. FINANCIAL ANALYSIS     |
                    |  25-year cashflow model    |
                    |  LCOE, payback, NPV, IRR   |
                    |  Viability score 0-100     |
                    +----------------------------+
                                  |
                    +-------------+---------------+
                    |                             |
             +------+-------+           +---------+-------+
             | HTML REPORT  |           |  WEB APP        |
             | 8 interactive|           |  FastAPI + HTMX |
             | Plotly charts|           |  localhost:8000  |
             +--------------+           +-----------------+
```

---

## Project Structure

```
What you'll work with most:
  solar_fetch.py           The main entry point - ties everything together
  solar_economics.py       The financial math (score_site function)
  solar_viz.py             Chart and report generation
  app.py                   Web interface

Data pipeline modules:
  solar_geocode.py         Turns addresses into coordinates
  solar_pvwatts.py         Gets solar production estimates from NREL
  solar_urdb.py            Looks up your utility's electricity rate
  solar_nem.py             Determines solar export compensation by state

Exploration notebooks (run in order):
  notebooks/01_nrel_api.ipynb       NREL PVWatts API deep-dive
  notebooks/02_nasa_api.ipynb       NASA weather data exploration
  notebooks/03_data_loading.ipynb   Load Berkeley Lab installation dataset
  notebooks/04_eda_peninsula.ipynb  Bay Area solar trends analysis
  notebooks/05_data_dictionary.ipynb  What each column means
  notebooks/06_pipeline.ipynb       Full data integration pipeline

Reference data:
  data/uszips.csv                   US ZIP code coordinates (41K entries)
  data/eia_state_rates_2025.csv     Electricity prices by state
  data/nem3_acc_2025.csv            CA NEM 3.0 export rate schedule

Tests:
  test_solar_economics.py           39 tests for the financial engine
  test_integration.py               20 tests for the full pipeline
```

---

## What the Report Shows

The generated HTML report includes **8 interactive charts**:

| Chart | What it tells you |
|-------|------------------|
| **Viability Gauge** | Overall 0-100 score with color-coded bands |
| **KPI Cards** | Net cost, LCOE, payback, NPV, IRR, CO2 at a glance |
| **Economics Waterfall** | How cost flows from gross to net benefit |
| **Cumulative Cashflow** | When you break even (red zone vs green zone) |
| **LCOE vs Retail Rate** | Is solar cheaper than the grid? By how much? |
| **Sensitivity Tornado** | Which assumptions matter most to the outcome |
| **Monthly Production** | Seasonal solar output pattern |
| **Peer Comparison** | How this site stacks up against reference data |

Plus a **TOU rate overlay** (when utility has time-of-use pricing) and a full **provenance section** showing exactly where every number came from.

---

## Key Assumptions

These are the defaults baked into the financial model. All are overridable.

| What | Default | Why |
|------|---------|-----|
| System lifetime | 25 years | Standard solar panel warranty period |
| Panel degradation | 0.5% per year | Panels slowly lose efficiency over time |
| Federal tax credit (ITC) | 30% | Current US incentive through 2032 |
| Electricity price increase | 2.5% per year | Historical average rate of grid price growth |
| Self-consumption | 40% | How much solar you use directly vs export to grid |
| Export credit | 75% of retail | What the utility pays you for excess power (varies by state) |
| O&M cost | $20/kW per year | Cleaning, monitoring, inverter replacement fund |
| Install cost | $3.50/W | National average; overridden by local data when available |

---

## Data Sources

| Source | What it provides | How fresh |
|--------|-----------------|-----------|
| [NREL PVWatts v8](https://developer.nrel.gov/docs/solar/pvwatts/v8/) | Solar production estimates for any location | Live API (cached 30 days) |
| [OpenEI URDB](https://openei.org/wiki/Utility_Rate_Database) | Utility electricity rates (flat, tiered, TOU) | Live API (cached 7 days) |
| [NASA POWER](https://power.larc.nasa.gov/) | Hourly solar radiation and weather | Live API |
| [Berkeley Lab TTS](https://emp.lbl.gov/tracking-the-sun) | Real solar installation records (2M+ systems) | Downloaded dataset |
| [EIA](https://www.eia.gov/electricity/monthly/) | State average electricity prices | Bundled CSV (update annually) |
| [US Census Geocoder](https://geocoding.geo.census.gov/) | Address to lat/lon conversion | Live API (cached 30 days) |

---

## For Developers: Branching & Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for full details. Quick version:

1. Pull latest `main`
2. Create a branch: `git checkout -b feature/your-task`
3. Make changes, commit with clear messages
4. Push and open a Pull Request
5. Get one teammate's review before merging

---

## Team

| Member | Focus Area |
|--------|-----------|
| | |
| | |
| | |
| | |
