# Solar Energy Analysis - DATA 201

Analyzing solar installation trends, costs, and energy production potential in the Bay Area using data from NREL, NASA POWER, and Berkeley Lab's Tracking the Sun dataset. Includes a full economics scoring engine and premium HTML report generator.

## Project Structure

```
solar_economics.py             # Core scoring engine (Part 1)
test_solar_economics.py        # pytest suite — 39 tests (Part 2)
solar_viz.py                   # Plotly HTML report generator (Part 3)
sample_site.csv                # Sample San Jose site for testing

notebooks/
  01_nrel_api.ipynb            # NREL PVWatts API exploration
  02_nasa_api.ipynb            # NASA POWER API exploration
  03_data_loading.ipynb        # Download & load Berkeley Lab data
  04_eda_peninsula.ipynb       # Exploratory data analysis (Bay Area)
  05_data_dictionary.ipynb     # Column definitions & data reference
  06_pipeline.ipynb            # End-to-end integration pipeline

docs/                          # Sprint notes, meeting minutes
```

## Setup

```bash
# 1. Clone
git clone https://github.com/scottn66/solar-energy-analysis.git
cd solar-energy-analysis

# 2. Install dependencies
pip install -r requirements.txt

# 3. API key (choose one)
# Local: copy .env.example to .env and paste your NREL key
cp .env.example .env
# Colab: add NREL_API_KEY to the Secrets sidebar

# 4. Run tests to verify
pytest test_solar_economics.py -v
```

Get a free NREL API key at [developer.nrel.gov/signup](https://developer.nrel.gov/signup/).

## Usage

### Score a single site (Python)

```python
from solar_economics import score_site, Assumptions

row = {"site_id": "my_site", "system_capacity_kw": 5, "pvwatts_ac_annual_kwh": 8195,
       "state": "CA", "lat": 37.33, "tilt": 20, "azimuth": 180, "losses": 14,
       "tts_median_price_per_watt": 3.80, "tts_recent_sample_size": 36000}

result = score_site(row)
print(f"Viability: {result.viability_score}/100 — {result.viability_label}")
print(f"Payback: {result.simple_payback_years:.1f} yr | NPV: ${result.npv:,.0f}")
```

### Score a CSV (CLI)

```bash
python solar_economics.py sample_site.csv -o enriched.csv --json-dir reports/
```

### Generate HTML report

```bash
python solar_viz.py sample_site.csv -o solar_report.html
```

Or from Python:

```python
from solar_viz import generate_report
generate_report([result], "report.html", rows=[row_dict])
```

### Override assumptions

```python
custom = Assumptions(
    federal_itc=0.0,          # no tax credit
    nem_export_ratio=0.25,    # NEM 3.0
    electricity_escalation=0.04,
    system_life_years=30,
)
result = score_site(row, assumptions=custom)
```

## Data Sources

| Source | Description | Access |
|--------|-------------|--------|
| **NREL PVWatts v8** | Modeled solar production estimates | [API](https://developer.nrel.gov/docs/solar/pvwatts/v8/) (key required) |
| **NASA POWER** | Hourly solar radiation & weather data | [API](https://power.larc.nasa.gov/) (open) |
| **Tracking the Sun** | Real solar installation records (Berkeley Lab) | [Google Drive](https://drive.google.com/file/d/1NQh4TRC_IqDz2r5vfZuxDm6LGjEuexdu/view) |
| **Kaggle Supplement** | Urban solar ROI dataset | [Kaggle](https://www.kaggle.com/datasets/shaistashahid/urban-solar-roi-and-sustainability) |

## Assumptions Reference

Every parameter is overridable via the `Assumptions` dataclass.

| Parameter | Default | Rationale |
|-----------|---------|-----------|
| `system_life_years` | 25 | Industry-standard warranty/analysis horizon for c-Si PV |
| `degradation_rate` | 0.005 (0.5%/yr) | Median from NREL long-term field studies (Jordan & Kurtz 2013) |
| `discount_rate` | 0.06 (6%) | Nominal WACC for residential solar |
| `om_cost_per_kw_year` | $20 | Covers inverter reserves, cleaning, monitoring (NREL ATB 2024) |
| `federal_itc` | 0.30 (30%) | Investment Tax Credit under IRA through 2032 |
| `electricity_escalation` | 0.025 (2.5%/yr) | EIA Annual Energy Outlook reference case |
| `nem_export_ratio` | 0.75 | Fraction of retail credited for exports (1.0=NEM1, 0.75=NEM2, 0.25=NEM3) |
| `self_consumption_rate` | 0.40 | Fraction consumed on-site at full retail (higher with battery) |
| `co2_intensity_tons_per_kwh` | 0.0004 | US avg grid intensity, EPA eGRID 2022 |
| `default_price_per_watt` | $3.50 | Fallback if TTS data missing (EnergySage 2024 median) |

## Viability Score Formula

**Composite score (0-100)** = weighted blend of four sub-scores:

| Component | Weight | What it measures |
|-----------|--------|-----------------|
| **Resource** | 25% | Specific yield normalized against 1,800 kWh/kW ceiling |
| **Economics** | 50% | Blend of payback bucket (40%), LCOE-vs-retail (40%), NPV magnitude (20%) |
| **Site Fit** | 15% | Tilt deviation from latitude, azimuth deviation from 180°, excess losses |
| **Policy/Market** | 10% | Log-scaled TTS recent sample size (local adoption signal) |

### Thresholds

| Metric | Excellent | Good | Marginal | Poor |
|--------|-----------|------|----------|------|
| Payback | <7 yr | 7-10 yr | 10-15 yr | >15 yr |
| Grid parity ratio | <0.6 | 0.6-0.8 | 0.8-1.0 | >1.0 |
| Viability score | >=80 | 65-79 | 50-64 | <50 |

## Report Visualizations

The HTML report includes 7 interactive charts:

1. **Hero gauge** — viability score with threshold bands
2. **Economics waterfall** — gross cost to net benefit flow
3. **Cumulative cashflow** — breakeven curve with payback marker
4. **LCOE vs retail** — grid parity comparison
5. **Monthly production** — seasonal pattern estimate
6. **Sensitivity tornado** — NPV under +/-20% parameter changes
7. **Peer comparison** — this site vs Kaggle reference distribution

## Agile Workflow

We use **GitHub Issues** and **GitHub Projects** for task management:
- Each notebook module has an assigned owner
- Issues are labeled by sprint (`sprint-1`, `sprint-2`) and category
- See `CONTRIBUTING.md` for branching strategy and PR workflow

## Team

| Member | Role |
|--------|------|
| | |
| | |
| | |
| | |
