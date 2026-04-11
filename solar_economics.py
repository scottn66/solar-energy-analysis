"""
solar_economics.py — Solar site economics and viability scoring engine.

Turns raw fields from PVWatts, NASA POWER, Berkeley Lab TTS, and Kaggle
into a rigorous financial analysis with a composite viability score.

Usage:
    from solar_economics import score_site, Assumptions, DEFAULTS
    result = score_site(row_dict, assumptions=DEFAULTS)

CLI:
    python solar_economics.py input.csv --output enriched.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import warnings
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State residential electricity prices ($/kWh, 2024 EIA annual averages)
# TODO: Refresh annually from https://www.eia.gov/electricity/monthly/
#       Table 5.6.A — Average Retail Price of Electricity
# ---------------------------------------------------------------------------
STATE_ELECTRICITY_PRICES: dict[str, float] = {
    "AL": 0.16, "AK": 0.24, "AZ": 0.15, "AR": 0.13, "CA": 0.32,
    "CO": 0.16, "CT": 0.29, "DE": 0.17, "FL": 0.16, "GA": 0.15,
    "HI": 0.42, "ID": 0.12, "IL": 0.17, "IN": 0.16, "IA": 0.16,
    "KS": 0.15, "KY": 0.13, "LA": 0.13, "ME": 0.26, "MD": 0.17,
    "MA": 0.29, "MI": 0.20, "MN": 0.16, "MS": 0.14, "MO": 0.14,
    "MT": 0.13, "NE": 0.13, "NV": 0.16, "NH": 0.27, "NJ": 0.20,
    "NM": 0.16, "NY": 0.24, "NC": 0.14, "ND": 0.13, "OH": 0.15,
    "OK": 0.13, "OR": 0.14, "PA": 0.18, "RI": 0.28, "SC": 0.15,
    "SD": 0.14, "TN": 0.13, "TX": 0.15, "UT": 0.12, "VT": 0.22,
    "VA": 0.15, "WA": 0.12, "WV": 0.14, "WI": 0.18, "WY": 0.12,
    "DC": 0.16,
}

# National median — used only as final fallback
_US_MEDIAN_RATE = 0.16


# ---------------------------------------------------------------------------
# Assumptions dataclass — every number is overridable
# ---------------------------------------------------------------------------
@dataclass
class Assumptions:
    """
    Configurable parameters for solar economics analysis.

    Each default is documented with its rationale.  Override any field
    by passing ``Assumptions(field=value)`` or by loading from JSON.
    """

    system_life_years: int = 25
    """Industry-standard warranty/analysis horizon for crystalline-silicon PV."""

    degradation_rate: float = 0.005
    """0.5%/yr median from NREL long-term field studies (Jordan & Kurtz 2013)."""

    discount_rate: float = 0.06
    """Nominal WACC for residential solar; 6% blends ~5% debt + equity premium."""

    om_cost_per_kw_year: float = 20.0
    """$20/kW-yr covers inverter reserves, cleaning, monitoring (NREL ATB 2024)."""

    federal_itc: float = 0.30
    """30% Investment Tax Credit under IRA through 2032."""

    electricity_price_override: Optional[float] = None
    """If set, bypasses state lookup table.  Units: $/kWh."""

    electricity_escalation: float = 0.025
    """2.5%/yr nominal rise in retail electricity (EIA AEO reference case)."""

    nem_export_ratio: float = 0.75
    """Fraction of retail rate credited for grid exports.
    1.0 = full NEM 1.0 retail; 0.75 ≈ NEM 2.0; 0.25 ≈ NEM 3.0 / avoided-cost."""

    self_consumption_rate: float = 0.40
    """Fraction of annual production consumed on-site at full retail.
    Typical US residential without storage; 0.60–0.80 with battery."""

    co2_intensity_tons_per_kwh: float = 0.0004
    """US average grid carbon intensity: ~0.4 kg CO2/kWh = 0.0004 tons/kWh
    (EPA eGRID 2022).  Override for regional marginal emission rates."""

    default_price_per_watt: float = 3.50
    """Fallback $/W if tts_median_price_per_watt is missing.
    Approximate US median for residential rooftop (EnergySage 2024)."""

    # Viability score weights — must sum to 1.0
    weight_resource: float = 0.25
    weight_economics: float = 0.50
    weight_site_fit: float = 0.15
    weight_policy: float = 0.10


DEFAULTS = Assumptions()


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class SiteResult:
    """Complete output of score_site().  Every field is documented."""

    # --- Identity ---
    site_id: str
    state: str
    address_label: str

    # --- Resource metrics ---
    specific_yield: float
    """Annual kWh produced per kW of installed capacity (kWh/kW)."""

    capacity_factor: float
    """Fraction of maximum theoretical output actually produced (%)."""

    resource_score: float
    """Specific yield normalized against a 1,800 kWh/kW ceiling, clipped [0,1].
    Rationale: 1,800 kWh/kW is top-decile US residential (desert SW)."""

    # --- Cost build-up ---
    gross_cost: float
    """Pre-incentive installed cost ($) = capacity_kW * 1000 * $/W."""

    net_cost: float
    """After federal ITC: gross_cost * (1 - ITC rate)."""

    lifetime_om: float
    """Total undiscounted O&M over system life ($)."""

    # --- Financial metrics ---
    lcoe: float
    """Levelized cost of energy ($/kWh) = (net_cost + lifetime_om) / lifetime_kWh.
    Does not discount production (simplified; conservative vs discounted LCOE)."""

    simple_payback_years: float
    """Year at which cumulative nominal savings first equal net_cost.
    Interpolated fractionally between integer years.
    Thresholds: <7 excellent, 7–10 good, 10–15 marginal, >15 poor."""

    npv: float
    """Net present value ($) of discounted savings minus net_cost."""

    irr: Optional[float]
    """Internal rate of return on the cashflow vector.
    None if numpy_financial.irr fails to converge (rare edge case)."""

    grid_parity_ratio: float
    """LCOE / year-1 retail price.  <1.0 means solar cheaper than grid.
    <0.6 = obvious yes; 0.6–0.8 = strong; 0.8–1.0 = marginal; >1.0 = grid wins."""

    annual_co2_avoided_tons: float
    """Year-1 CO2 avoided (metric tons) = year_1_kwh * grid_intensity."""

    year_1_savings: float
    """First-year nominal electricity savings ($)."""

    lifetime_savings: float
    """Sum of nominal annual savings over system life ($)."""

    electricity_rate_used: float
    """Retail rate ($/kWh) that was actually used in the calculation."""

    rate_source: str
    """Where the electricity rate came from: 'override', 'state_table', 'kaggle_fallback', 'national_median'."""

    # --- Production arrays (for viz) ---
    year_production: list[float]
    """Annual kWh production for years 0..N-1 with degradation applied."""

    annual_savings: list[float]
    """Nominal savings ($) per year, blending self-consumption and export."""

    cumulative_savings: list[float]
    """Running total of annual_savings."""

    cumulative_grid_cost: list[float]
    """Cumulative electricity cost if no solar installed (the "do nothing" line)."""

    cashflows: list[float]
    """Cashflow vector for IRR: [-net_cost, savings_yr1, savings_yr2, ...]."""

    # --- Site fit metrics ---
    tilt_deviation: float
    """Absolute difference between panel tilt and site latitude (degrees).
    Optimal tilt ≈ latitude for fixed-mount; >15° deviation penalized."""

    azimuth_deviation: float
    """Absolute difference from ideal 180° (true south in northern hemisphere).
    >45° significantly reduces production."""

    site_fit_score: float
    """Normalized score [0,1] penalizing tilt/azimuth deviation."""

    # --- Policy / market proxy ---
    policy_score: float
    """Log-scaled normalization of tts_recent_sample_size.
    High local adoption signals favorable policy/market environment."""

    # --- Composite ---
    viability_score: float
    """Weighted composite score 0–100.
    25% resource + 50% economics + 15% site_fit + 10% policy."""

    viability_label: str
    """Human-readable verdict based on viability_score:
    >=80 Excellent, >=65 Good, >=50 Marginal, <50 Poor."""

    economics_score: float
    """Sub-score [0,1] used in the viability composite.
    Blend of payback bucket (40%), LCOE-vs-retail (40%), NPV sign/magnitude (20%)."""

    # --- Assumptions used ---
    assumptions_used: dict
    """Snapshot of all assumption values for reproducibility."""

    def to_dict(self) -> dict:
        """Serialize to a flat dictionary (JSON-safe)."""
        d = {}
        for k, v in asdict(self).items():
            if isinstance(v, (list, dict)):
                d[k] = v
            elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                d[k] = None
            else:
                d[k] = v
        return d


# ---------------------------------------------------------------------------
# Helper: electricity rate lookup
# ---------------------------------------------------------------------------
def _resolve_electricity_rate(
    state: Optional[str],
    kaggle_median: Optional[float],
    override: Optional[float],
) -> tuple[float, str]:
    """
    Resolve the retail electricity rate with a clear fallback chain.

    Priority:
        1. Explicit override (user-supplied)
        2. State lookup table (EIA data)
        3. Kaggle median from reference dataset
        4. US national median ($0.16/kWh)

    Returns:
        (rate, source_label)
    """
    if override is not None:
        return override, "override"

    if state and state.upper() in STATE_ELECTRICITY_PRICES:
        return STATE_ELECTRICITY_PRICES[state.upper()], "state_table"

    if kaggle_median is not None and not np.isnan(kaggle_median):
        logger.warning(
            "State '%s' not in lookup table; falling back to Kaggle median $%.3f/kWh",
            state, kaggle_median,
        )
        return kaggle_median, "kaggle_fallback"

    logger.warning(
        "No electricity rate source available; using US median $%.2f/kWh",
        _US_MEDIAN_RATE,
    )
    return _US_MEDIAN_RATE, "national_median"


# ---------------------------------------------------------------------------
# Helper: safe field extraction
# ---------------------------------------------------------------------------
def _get(row: dict, key: str, default=np.nan) -> float:
    """Extract a numeric field from a row dict, coercing to float."""
    val = row.get(key)
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------
def _resource_score(specific_yield: float) -> float:
    """
    Normalize specific yield against an 1,800 kWh/kW ceiling.

    Rationale: 1,800 kWh/kW is the top-decile US residential yield
    (achieved in desert Southwest with optimal tilt).  A yield of
    1,800+ maps to 1.0; yields below scale linearly to 0.

    Returns:
        float in [0, 1]
    """
    if specific_yield <= 0 or np.isnan(specific_yield):
        return 0.0
    return float(np.clip(specific_yield / 1800.0, 0.0, 1.0))


def _economics_score(
    payback_years: float,
    grid_parity_ratio: float,
    npv: float,
    net_cost: float,
) -> float:
    """
    Composite economics sub-score [0, 1].

    Three equally-important but differently-weighted components:

    Payback bucket (40% of economics score):
        <7 yr → 1.0  (Excellent: typical for high-rate states with ITC)
        7–10  → 0.7  (Good: solid residential investment)
        10–15 → 0.4  (Marginal: may need financing optimization)
        >15   → 0.1  (Poor: unlikely to be worthwhile)

    LCOE vs retail (40% of economics score):
        grid_parity_ratio <0.6 → 1.0  (Obvious yes: solar far cheaper than grid)
        0.6–0.8            → 0.75 (Strong: clear economic advantage)
        0.8–1.0            → 0.4  (Marginal: depends on assumptions)
        >1.0               → 0.1  (Grid wins: solar more expensive)

    NPV magnitude (20% of economics score):
        NPV > 0             → scale from 0.5 to 1.0 by NPV/net_cost ratio
        NPV ≤ 0             → 0.1

    Returns:
        float in [0, 1]
    """
    # --- Payback bucket ---
    if np.isnan(payback_years) or payback_years > 25:
        pb_score = 0.05
    elif payback_years < 7:
        pb_score = 1.0
    elif payback_years < 10:
        pb_score = 0.7
    elif payback_years < 15:
        pb_score = 0.4
    else:
        pb_score = 0.1

    # --- LCOE vs retail ---
    if np.isnan(grid_parity_ratio):
        lcoe_score = 0.0
    elif grid_parity_ratio < 0.6:
        lcoe_score = 1.0
    elif grid_parity_ratio < 0.8:
        lcoe_score = 0.75
    elif grid_parity_ratio < 1.0:
        lcoe_score = 0.4
    else:
        lcoe_score = 0.1

    # --- NPV magnitude ---
    if np.isnan(npv) or net_cost <= 0:
        npv_score = 0.0
    elif npv > 0:
        # Scale: NPV equal to net_cost → 1.0; NPV = 0 → 0.5
        ratio = min(npv / net_cost, 1.0) if net_cost > 0 else 0.5
        npv_score = 0.5 + 0.5 * ratio
    else:
        npv_score = 0.1

    return 0.40 * pb_score + 0.40 * lcoe_score + 0.20 * npv_score


def _site_fit_score(
    tilt: float,
    azimuth: float,
    latitude: float,
    losses: float,
) -> tuple[float, float, float]:
    """
    Score how well the physical installation is configured.

    Penalizes:
        - Tilt deviation from latitude (optimal for fixed-mount annual yield)
          >15° deviation starts to meaningfully reduce output.
        - Azimuth deviation from 180° (true south in northern hemisphere)
          >45° significantly reduces production.
        - Losses above the 14% PVWatts default (wiring, shading, soiling)

    Returns:
        (tilt_deviation, azimuth_deviation, score ∈ [0, 1])
    """
    # Tilt: ideal = latitude for fixed annual optimization
    tilt_dev = abs(tilt - abs(latitude)) if not (np.isnan(tilt) or np.isnan(latitude)) else 0.0
    # Normalize: 0° deviation = 1.0; 30° deviation = 0.0
    tilt_score = max(0.0, 1.0 - tilt_dev / 30.0)

    # Azimuth: ideal = 180° (true south) in northern hemisphere
    az_dev = abs(azimuth - 180.0) if not np.isnan(azimuth) else 0.0
    # Normalize: 0° deviation = 1.0; 90° deviation = 0.0
    az_score = max(0.0, 1.0 - az_dev / 90.0)

    # Losses: 14% is PVWatts default; penalize excess
    loss_penalty = max(0.0, (losses - 14.0)) / 20.0 if not np.isnan(losses) else 0.0
    loss_score = max(0.0, 1.0 - loss_penalty)

    # Weighted blend: azimuth matters most, then tilt, then losses
    score = 0.40 * az_score + 0.40 * tilt_score + 0.20 * loss_score

    return tilt_dev, az_dev, float(np.clip(score, 0.0, 1.0))


def _policy_score(tts_recent_sample_size: float) -> float:
    """
    Log-scaled proxy for local policy and market maturity.

    Rationale: a ZIP/state with 30,000+ recent installations signals
    favorable net metering, permitting, and installer competition.
    Log scaling prevents outliers from dominating.

    Scale: log10(sample_size) / log10(50,000), clipped [0,1].
        100 installations  → ~0.33
        1,000              → ~0.64
        10,000             → ~0.85
        50,000             → 1.00

    Returns:
        float in [0, 1]
    """
    if np.isnan(tts_recent_sample_size) or tts_recent_sample_size <= 0:
        return 0.0
    raw = math.log10(max(tts_recent_sample_size, 1)) / math.log10(50_000)
    return float(np.clip(raw, 0.0, 1.0))


def _viability_label(score: float) -> str:
    """Map a 0–100 viability score to a human verdict."""
    if score >= 80:
        return "Excellent — install with confidence"
    elif score >= 65:
        return "Good — solid investment with standard financing"
    elif score >= 50:
        return "Marginal — depends on financing and rate trajectory"
    else:
        return "Poor — unlikely to be cost-effective under current assumptions"


# ---------------------------------------------------------------------------
# IRR computation
# ---------------------------------------------------------------------------
def _compute_irr(cashflows: list[float]) -> Optional[float]:
    """
    Compute internal rate of return using numpy_financial if available,
    falling back to numpy's IRR or a manual Newton-Raphson solver.

    Returns None if no real solution is found (e.g., all-positive cashflows,
    or the solver doesn't converge within 100 iterations).
    """
    try:
        import numpy_financial as npf
        result = npf.irr(cashflows)
        if np.isnan(result) or np.isinf(result):
            return None
        return float(result)
    except ImportError:
        pass

    # Manual Newton-Raphson fallback
    # NPV(r) = sum(cf_t / (1+r)^t)  for t in 0..n
    # We seek r where NPV(r) = 0
    cf = np.array(cashflows, dtype=float)
    r = 0.10  # initial guess
    for _ in range(200):
        t = np.arange(len(cf))
        denom = (1 + r) ** t
        npv_val = np.sum(cf / denom)
        # derivative: d(NPV)/dr = sum(-t * cf_t / (1+r)^(t+1))
        d_npv = np.sum(-t * cf / ((1 + r) ** (t + 1)))
        if abs(d_npv) < 1e-12:
            return None
        r_new = r - npv_val / d_npv
        if abs(r_new - r) < 1e-8:
            if np.isnan(r_new) or np.isinf(r_new) or r_new < -1:
                return None
            return float(r_new)
        r = r_new

    return None  # did not converge


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------
def score_site(row: dict, assumptions: Assumptions = DEFAULTS) -> SiteResult:
    """
    Score a single solar site for economics and viability.

    Parameters
    ----------
    row : dict
        Flat dictionary with fields from the master feature table.
        Required: site_id, system_capacity_kw, pvwatts_ac_annual_kwh, state.
        Optional: all other fields (graceful fallbacks documented per field).

    assumptions : Assumptions
        Overridable parameters.  See Assumptions docstring.

    Returns
    -------
    SiteResult
        Complete analysis with all financial metrics, sub-scores, and arrays.
    """
    a = assumptions

    # --- Extract fields with safe fallbacks ---
    site_id = str(row.get("site_id", "unknown"))
    state = str(row.get("state", "")) if row.get("state") else ""
    address_label = str(row.get("address_label", f"{state} site"))
    capacity_kw = _get(row, "system_capacity_kw", 5.0)
    annual_kwh = _get(row, "pvwatts_ac_annual_kwh", 0.0)
    cap_factor = _get(row, "pvwatts_capacity_factor", 0.0)
    latitude = _get(row, "lat", 37.0)
    tilt = _get(row, "tilt", latitude)
    azimuth = _get(row, "azimuth", 180.0)
    losses = _get(row, "losses", 14.0)
    tts_ppw = _get(row, "tts_median_price_per_watt", np.nan)
    tts_recent = _get(row, "tts_recent_sample_size", 0.0)
    kaggle_elec = _get(row, "kaggle_median_Electricity_Price_USD_per_kWh", np.nan)

    # --- Handle missing price per watt ---
    if np.isnan(tts_ppw) or tts_ppw <= 0:
        logger.warning(
            "Site %s: tts_median_price_per_watt missing; using default $%.2f/W",
            site_id, a.default_price_per_watt,
        )
        tts_ppw = a.default_price_per_watt

    # --- Handle zero/missing production ---
    if annual_kwh <= 0 or np.isnan(annual_kwh):
        logger.warning("Site %s: zero or missing production — metrics will be degenerate", site_id)
        annual_kwh = max(annual_kwh, 0.0) if not np.isnan(annual_kwh) else 0.0

    # --- Resource metrics ---
    specific_yield = annual_kwh / capacity_kw if capacity_kw > 0 else 0.0
    res_score = _resource_score(specific_yield)

    # --- Electricity rate ---
    elec_rate, rate_source = _resolve_electricity_rate(
        state, kaggle_elec, a.electricity_price_override
    )

    # --- Cost build-up ---
    # gross_cost: total pre-incentive installed cost
    # = system_capacity_kw * 1000 (convert to watts) * $/watt
    gross_cost = capacity_kw * 1000.0 * tts_ppw

    # net_cost: after 30% federal ITC
    net_cost = gross_cost * (1.0 - a.federal_itc)

    # lifetime_om: simple undiscounted O&M total
    lifetime_om = a.om_cost_per_kw_year * capacity_kw * a.system_life_years

    # --- Production over lifetime with degradation ---
    # year_production[t] = annual_kwh * (1 - degradation_rate)^t
    # Models ~12% cumulative loss over 25 years at 0.5%/yr
    year_production = [
        annual_kwh * (1.0 - a.degradation_rate) ** t
        for t in range(a.system_life_years)
    ]
    lifetime_kwh = sum(year_production)

    # --- Annual savings ---
    # Each year: retail rate escalates; savings blend self-consumption and export
    # self_consumed_kwh * retail  +  exported_kwh * (retail * nem_export_ratio)
    annual_savings_list = []
    cumulative_savings_list = []
    cumulative_grid_cost_list = []
    running_savings = 0.0
    running_grid = 0.0

    for t in range(a.system_life_years):
        retail_t = elec_rate * (1.0 + a.electricity_escalation) ** t
        prod_t = year_production[t]

        self_consumed = prod_t * a.self_consumption_rate
        exported = prod_t * (1.0 - a.self_consumption_rate)

        savings_t = (
            self_consumed * retail_t
            + exported * retail_t * a.nem_export_ratio
        )
        annual_savings_list.append(savings_t)

        running_savings += savings_t
        cumulative_savings_list.append(running_savings)

        # "Do nothing" comparator: full annual grid cost for equivalent consumption
        # Assumes the household would consume ~annual_kwh from the grid
        grid_cost_t = annual_kwh * retail_t  # undegraded demand
        running_grid += grid_cost_t
        cumulative_grid_cost_list.append(running_grid)

    # --- Financial metrics ---
    # LCOE: levelized cost over lifetime production (undiscounted, conservative)
    lcoe = (net_cost + lifetime_om) / lifetime_kwh if lifetime_kwh > 0 else float("inf")

    # Simple payback: first year where cumulative savings >= net_cost
    # Interpolate fractionally for precision
    payback = float("nan")
    for t in range(a.system_life_years):
        if cumulative_savings_list[t] >= net_cost:
            if t == 0:
                payback = 0.0 if annual_savings_list[0] >= net_cost else 1.0
            else:
                # Linear interpolation between year t-1 and year t
                shortfall = net_cost - cumulative_savings_list[t - 1]
                payback = t + shortfall / annual_savings_list[t] if annual_savings_list[t] > 0 else float(t + 1)
            break
    else:
        # Never pays back within system life
        payback = float("nan")

    # NPV: sum of discounted cashflows minus initial cost
    discounted_sum = sum(
        annual_savings_list[t] / (1.0 + a.discount_rate) ** (t + 1)
        for t in range(a.system_life_years)
    )
    npv = discounted_sum - net_cost

    # Cashflow vector for IRR: [-net_cost, savings_yr1, savings_yr2, ...]
    cashflows = [-net_cost] + annual_savings_list
    irr = _compute_irr(cashflows)

    # Grid parity ratio: LCOE / year-1 retail
    year_1_retail = elec_rate  # year 0 rate
    grid_parity = lcoe / year_1_retail if year_1_retail > 0 else float("inf")

    # Year-1 savings and lifetime total
    year_1_savings = annual_savings_list[0] if annual_savings_list else 0.0
    lifetime_savings = sum(annual_savings_list)

    # CO2 avoided (year 1)
    co2_avoided = year_production[0] * a.co2_intensity_tons_per_kwh if year_production else 0.0

    # --- Sub-scores ---
    econ_score = _economics_score(payback, grid_parity, npv, net_cost)
    tilt_dev, az_dev, sf_score = _site_fit_score(tilt, azimuth, latitude, losses)
    pol_score = _policy_score(tts_recent)

    # --- Composite viability score (0–100) ---
    viability = 100.0 * (
        a.weight_resource * res_score
        + a.weight_economics * econ_score
        + a.weight_site_fit * sf_score
        + a.weight_policy * pol_score
    )
    viability = float(np.clip(viability, 0.0, 100.0))

    return SiteResult(
        site_id=site_id,
        state=state,
        address_label=address_label,
        specific_yield=round(specific_yield, 2),
        capacity_factor=round(cap_factor, 2),
        resource_score=round(res_score, 4),
        gross_cost=round(gross_cost, 2),
        net_cost=round(net_cost, 2),
        lifetime_om=round(lifetime_om, 2),
        lcoe=round(lcoe, 4),
        simple_payback_years=round(payback, 2) if not np.isnan(payback) else float("nan"),
        npv=round(npv, 2),
        irr=round(irr, 4) if irr is not None else None,
        grid_parity_ratio=round(grid_parity, 4),
        annual_co2_avoided_tons=round(co2_avoided, 4),
        year_1_savings=round(year_1_savings, 2),
        lifetime_savings=round(lifetime_savings, 2),
        electricity_rate_used=round(elec_rate, 4),
        rate_source=rate_source,
        year_production=[round(x, 2) for x in year_production],
        annual_savings=[round(x, 2) for x in annual_savings_list],
        cumulative_savings=[round(x, 2) for x in cumulative_savings_list],
        cumulative_grid_cost=[round(x, 2) for x in cumulative_grid_cost_list],
        cashflows=[round(x, 2) for x in cashflows],
        tilt_deviation=round(tilt_dev, 2),
        azimuth_deviation=round(az_dev, 2),
        site_fit_score=round(sf_score, 4),
        policy_score=round(pol_score, 4),
        viability_score=round(viability, 1),
        viability_label=_viability_label(viability),
        economics_score=round(econ_score, 4),
        assumptions_used=asdict(a),
    )


# ---------------------------------------------------------------------------
# Sensitivity analysis helper (used by viz tornado chart)
# ---------------------------------------------------------------------------
def sensitivity_tornado(
    row: dict,
    base_assumptions: Assumptions = DEFAULTS,
    perturbation: float = 0.20,
) -> list[dict]:
    """
    Recompute NPV under ±perturbation of key parameters.

    Parameters varied (one at a time):
        - electricity_price_override (± from resolved rate)
        - tts_median_price_per_watt (± from row value)
        - degradation_rate
        - discount_rate
        - nem_export_ratio

    Returns:
        List of dicts: [{param, low_label, high_label, npv_low, npv_high, npv_base, impact}]
        sorted by absolute impact (largest first).
    """
    base_result = score_site(row, base_assumptions)
    npv_base = base_result.npv
    base_rate = base_result.electricity_rate_used

    params = [
        ("Electricity Price", "electricity_price_override", base_rate),
        ("Install Cost ($/W)", None, _get(row, "tts_median_price_per_watt", base_assumptions.default_price_per_watt)),
        ("Degradation Rate", "degradation_rate", base_assumptions.degradation_rate),
        ("Discount Rate", "discount_rate", base_assumptions.discount_rate),
        ("NEM Export Ratio", "nem_export_ratio", base_assumptions.nem_export_ratio),
    ]

    results = []
    for label, attr, base_val in params:
        for direction, mult in [("low", 1 - perturbation), ("high", 1 + perturbation)]:
            new_val = base_val * mult
            if attr == "electricity_price_override" or attr is None:
                # For electricity price: override it
                if label == "Electricity Price":
                    a = Assumptions(**{
                        **asdict(base_assumptions),
                        "electricity_price_override": new_val,
                    })
                    r = score_site(row, a)
                else:
                    # For install cost: modify the row
                    mod_row = {**row, "tts_median_price_per_watt": new_val}
                    r = score_site(mod_row, base_assumptions)
            else:
                a = Assumptions(**{**asdict(base_assumptions), attr: new_val})
                r = score_site(row, a)

            if direction == "low":
                npv_low = r.npv
                low_label = f"${new_val:.3f}" if "Price" in label or "Cost" in label else f"{new_val:.4f}"
            else:
                npv_high = r.npv
                high_label = f"${new_val:.3f}" if "Price" in label or "Cost" in label else f"{new_val:.4f}"

        results.append({
            "param": label,
            "low_label": low_label,
            "high_label": high_label,
            "npv_low": round(npv_low, 2),
            "npv_high": round(npv_high, 2),
            "npv_base": round(npv_base, 2),
            "impact": round(abs(npv_high - npv_low), 2),
        })

    results.sort(key=lambda x: x["impact"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    """CLI: score sites from a CSV and write enriched CSV + JSON summaries."""
    parser = argparse.ArgumentParser(
        description="Solar Economics Scoring Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_csv", help="Path to input CSV with site records")
    parser.add_argument("--output", "-o", default="enriched_sites.csv", help="Output CSV path")
    parser.add_argument("--json-dir", default="site_reports", help="Directory for per-site JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    df = pd.read_csv(args.input_csv)
    logger.info("Loaded %d sites from %s", len(df), args.input_csv)

    json_dir = Path(args.json_dir)
    json_dir.mkdir(exist_ok=True)

    results = []
    for _, row in df.iterrows():
        result = score_site(row.to_dict())
        results.append(result)

        # Write per-site JSON
        json_path = json_dir / f"{result.site_id}.json"
        with open(json_path, "w") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)

    # Build enriched DataFrame
    enriched_rows = []
    for r in results:
        flat = {
            "site_id": r.site_id,
            "viability_score": r.viability_score,
            "viability_label": r.viability_label,
            "specific_yield": r.specific_yield,
            "lcoe": r.lcoe,
            "simple_payback_years": r.simple_payback_years,
            "npv": r.npv,
            "irr": r.irr,
            "grid_parity_ratio": r.grid_parity_ratio,
            "net_cost": r.net_cost,
            "year_1_savings": r.year_1_savings,
            "lifetime_savings": r.lifetime_savings,
            "annual_co2_avoided_tons": r.annual_co2_avoided_tons,
            "electricity_rate_used": r.electricity_rate_used,
            "rate_source": r.rate_source,
            "resource_score": r.resource_score,
            "economics_score": r.economics_score,
            "site_fit_score": r.site_fit_score,
            "policy_score": r.policy_score,
        }
        enriched_rows.append(flat)

    out_df = pd.merge(df, pd.DataFrame(enriched_rows), on="site_id", how="left")
    out_df.to_csv(args.output, index=False)
    logger.info("Wrote enriched CSV: %s", args.output)
    logger.info("Wrote %d JSON summaries to %s/", len(results), json_dir)


if __name__ == "__main__":
    main()
