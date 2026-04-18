# Financial Assumptions — Full Reference

Every number in the financial model is documented here with its source and rationale. All are overridable via the `Assumptions` dataclass in `solar_economics.py`.

## System Parameters

| Parameter | Default | Source | Notes |
|-----------|---------|--------|-------|
| System lifetime | 25 years | Industry standard | Matches typical panel warranty period. Most panels produce 80%+ at year 25. |
| Panel degradation | 0.5%/year | NREL (Jordan & Kurtz 2013) | Median from 2,000+ field studies. Cumulative ~12% loss over 25 years. |
| System losses | 14% | PVWatts default | Covers wiring, soiling, shading, inverter efficiency, snow. Increase for shaded sites. |

## Financial Parameters

| Parameter | Default | Source | Notes |
|-----------|---------|--------|-------|
| Discount rate | 6% | Blended WACC | Residential solar: ~5% debt cost + equity premium. Lower = more favorable NPV. |
| Federal ITC | 30% | IRA (2022) | Investment Tax Credit. Valid through 2032 at 30%, steps down to 26% in 2033. |
| O&M cost | $20/kW/year | NREL ATB 2024 | Covers cleaning, monitoring, inverter replacement fund. $100/yr for a 5 kW system. |
| Install cost | $3.50/W | EnergySage 2024 | National median. Overridden by local TTS data when available. CA is typically $3.50-4.50/W. |

## Electricity Parameters

| Parameter | Default | Source | Notes |
|-----------|---------|--------|-------|
| Retail rate | State lookup | EIA Table 5.6.A | CA=$0.32, HI=$0.42, TX=$0.15. Overridden by URDB when available. |
| Rate escalation | 2.5%/year | EIA AEO | Historical US average. CA has been ~4%/yr recently. Conservative. |
| Self-consumption | 40% | Typical residential | Fraction used on-site at full retail. 60-80% with battery storage. |
| NEM export ratio | 75% of retail | Varies by state | CA NEM 3.0 is ~17% of retail. NJ/NY are 100%. See `solar_nem.py`. |

## Environmental

| Parameter | Default | Source | Notes |
|-----------|---------|--------|-------|
| Grid CO2 intensity | 0.0004 t/kWh | EPA eGRID 2022 | US national average. CA is ~0.0002 (cleaner grid). |

## Viability Score Weights

| Component | Weight | What it measures |
|-----------|--------|------------------|
| Resource quality | 25% | Specific yield vs 1,800 kWh/kW ceiling |
| Economics | 50% | Blend of payback (40%), LCOE vs retail (40%), NPV (20%) |
| Site fit | 15% | Tilt/azimuth deviation from optimal |
| Market maturity | 10% | Local installation count (log-scaled) |

## Score Thresholds

| Metric | Excellent | Good | Marginal | Poor |
|--------|-----------|------|----------|------|
| Viability score | 80+ | 65-79 | 50-64 | <50 |
| Simple payback | <7 yr | 7-10 yr | 10-15 yr | >15 yr |
| Grid parity (LCOE/retail) | <0.6 | 0.6-0.8 | 0.8-1.0 | >1.0 |

## How to Override

```python
from solar_economics import Assumptions, score_site

custom = Assumptions(
    federal_itc=0.26,           # 2033 reduced ITC
    electricity_escalation=0.04, # Aggressive rate growth
    self_consumption_rate=0.70,  # Battery storage scenario
    nem_export_ratio=0.17,       # CA NEM 3.0
)

result = score_site(row, assumptions=custom)
```
