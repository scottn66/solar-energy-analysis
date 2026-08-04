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
| Retail rate | State lookup | EIA Table 5.6.A | CA=$0.32, HI=$0.42, TX=$0.15, OR=$0.14. Overridden by URDB when available. |
| Rate escalation | 2.5%/year | EIA AEO | Historical US average. CA has been ~4%/yr recently. Conservative. |
| Self-consumption | 40% | Typical residential | Fraction used on-site at full retail. 60-80% with battery storage. |
| NEM export ratio | 75% of retail | Varies by state | CA NEM 3.0 is ~17% of retail. NJ/NY/OR are 100%. See `solar_nem.py`. |

## Oregon Region (Bend/Redmond focus)

Bundled tariffs in `data/utility_tou_schedules.csv` (snapshot 2026-08; Oregon
default residential service is flat, not TOU):

| Utility | Serves | Rate used | Fixed/mo | Vintage |
|---------|--------|-----------|----------|---------|
| Pacific Power (PacifiCorp) | Bend, Prineville, Madras, Medford, Klamath Falls | $0.140/kWh all-in | $14.00 | 2026-04 (+2.9% adjustment) |
| Portland General Electric (`PGE-OR`) | Portland metro, Salem | $0.157/kWh volumetric | $13.60 | 2026-04 (+5% adjustment) |
| Central Electric Co-op (`CEC-OR`) | Redmond, Sisters, Terrebonne, Powell Butte | $0.085/kWh energy | $29.00* | 2025-10 (+8.5% BPA-driven) |
| Midstate Electric Co-op (`Midstate-OR`) | La Pine, Sunriver, Crescent | $0.090/kWh energy | $35.00 | 2025-11 |

\* CEC's exact facilities charge is published only in a PDF on cec.coop —
verify before relying on it.

**Oregon NEM policy** (see `solar_nem.py`):
- PUC-regulated utilities (PGE, Pacific Power): full 1:1 retail-rate kWh net
  metering under ORS 757.300, 25 kW residential cap, March annual true-up
  (surplus credits go to low-income bill assistance). A PGE successor tariff
  has been floated but not filed as of 2026 — review annually.
- Consumer-owned co-ops (Central Electric, Midstate Electric): boards set
  their own terms — monthly netting at retail with monthly surplus cashed
  out at wholesale (~$0.047/kWh, no annual banking). Modeled as a blended
  **90% of retail** export value for a load-sized system.

**Central Oregon solar resource:** the Cascade rain shadow gives
Bend/Redmond ~1,475–1,500 kWh/kW/yr (vs ~1,200 in Portland) — the heatmap
yield model (`build_heatmap.py`) splits east/west at the crest (~122°W)
rather than using latitude alone. Calibrated to NREL PVWatts, south-facing,
latitude tilt, 14% losses.

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
