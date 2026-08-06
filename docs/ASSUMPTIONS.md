# Financial Assumptions — Full Reference

Every number in the financial model is documented here with its source and rationale. All are overridable via the `Assumptions` dataclass in `solar_economics.py`.

## System Parameters

| Parameter | Default | Source | Notes |
|-----------|---------|--------|-------|
| System lifetime | 25 years | Industry standard | Matches typical panel warranty period. Most panels produce 80%+ at year 25. |
| Panel degradation | 0.7%/year | NREL PV Fleet (Jordan et al. 2022) | National fleet median. The older 0.5%/yr figure was the 2013 Jordan & Kurtz review median. |
| System losses | 14% | PVWatts default | Covers wiring, soiling, shading, inverter efficiency, snow. Increase for shaded sites. |

## Financial Parameters

| Parameter | Default | Source | Notes |
|-----------|---------|--------|-------|
| Discount rate | 6% | Blended WACC | Residential solar: ~5% debt cost + equity premium. Lower = more favorable NPV. |
| Federal ITC | **0%** | Pub. L. 119-21 (July 2025); IRS FS-2025-05 | Section 25D was **terminated for expenditures after 2025-12-31** — IRS keys eligibility to installation-completion date. Set `federal_itc=0.30` explicitly for pre-2026 installs or third-party-owned (Section 48E) analyses. |
| O&M cost | $31/kW/year | NREL ATB (residential) | ATB residential PV fixed O&M is $30-34/kWdc-yr; $20/kW-yr is the commercial-scale figure. ~$140/yr for a 4.5 kW system. |
| Install cost | $3.50/W | EnergySage 2024 | National median. Overridden by local TTS data when available. CA is typically $3.50-4.50/W. |

## Electricity Parameters

| Parameter | Default | Source | Notes |
|-----------|---------|--------|-------|
| Retail rate | State lookup | EIA Table 5.6.A | CA=$0.32, HI=$0.42, TX=$0.15, OR=$0.14 (2024 vintage — bundled utility schedules override where present). |
| Rate escalation | 2.5%/year | Long-run reference | EIA AEO implies ~2%/yr nationally, but OR/CA actuals ran 5-8%/yr 2020-2025. `build_heatmap.py` uses 5%/yr for Oregon; override regionally for near-term-sensitive work. |
| Self-consumption | 40% | Typical residential | Fraction used on-site at full retail. 60-80% with battery storage. |
| NEM export ratio | 75% of retail | Varies by state | CA NEM 3.0 is ~17% of retail. NJ/NY/OR are 100%. See `solar_nem.py`. |

## Oregon Region (Bend/Redmond focus)

Bundled tariffs in `data/utility_tou_schedules.csv` (snapshot 2026-08,
re-verified against primary sources; Oregon default residential service is
flat, not TOU). Rates are **avoidable volumetric** figures — bill-average
all-in rates including fixed charges run ~1-2¢ higher:

| Utility | Serves | Rate used | Fixed/mo | Vintage |
|---------|--------|-----------|----------|---------|
| Pacific Power (PacifiCorp) | Bend, Redmond city core, Prineville, Madras, Medford, Klamath Falls | $0.160/kWh (all-in ≈$0.175) | ≈$14* | 2026-04 (+2.9%; ~11% more pending at OPUC) |
| Portland General Electric (`PGE-OR`) | Portland metro, Salem | $0.190/kWh (all-in ≈$0.21) | $13.00 | 2026-04 (+5% adjustment) |
| Central Electric Co-op (`CEC-OR`) | Redmond-area fringe, Sisters, Terrebonne, Powell Butte | $0.085/kWh energy | ≈$29* | 2025-10 (+8.5% BPA-driven; more phases signaled) |
| Midstate Electric Co-op (`Midstate-OR`) | La Pine, Sunriver, Crescent | $0.090/kWh energy | $35.00 + ≈$1.25/kW demand | 2025-11 |

\* Approximate — the exact figures are published only in tariff PDFs the
utilities serve behind fetch-blocking; verify before relying on them.

**Territory caution:** Pacific Power and CEC interleave street by street
around Redmond, Bend, Terrebonne, and Prineville — Redmond's city core is
predominantly Pacific Power even though CEC is headquartered there. CEC
publishes an address-lookup provider map; always verify by meter, not town.

**Oregon NEM policy** (see `solar_nem.py`):
- PUC-regulated utilities (PGE, Pacific Power): full 1:1 retail-rate kWh net
  metering under ORS 757.300, 25 kW residential cap, March annual true-up
  (surplus credits go to low-income bill assistance). A PGE successor tariff
  has been floated but not filed as of 2026; OPUC rulemaking AR 688 (opened
  Apr 2026) is the first live docket touching the Division 39 net-metering
  rules — review annually.
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
| Grid CO2 intensity | 0.00035 t/kWh | EPA eGRID2023 (767 lb/MWh) | US national average. Hydro-heavy NWPP (Oregon) is ~0.00029; CA ~0.0002. |

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
