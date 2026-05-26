"""
build_portfolio.py — Generate a multi-state residential solar portfolio dataset
for the dashboard.

Strategy
--------
We don't want to hit the NREL/NASA APIs 50+ times for a class project, so we
synthesize plausible PVWatts inputs from public reference data:

  * Specific-yield (kWh/kW) is estimated from latitude using a piecewise
    fit calibrated to NREL's PVWatts national results (1,100 kWh/kW at
    high-latitude WA/ME to 1,800 kWh/kW in the desert SW).
  * Electricity rates come from EIA Table 5.6.A (already in data/).
  * TTS install cost ($/W) varies by state using LBL TTS 2024 medians
    (high-volume mature markets ~$3.20, emerging markets ~$3.80).
  * One representative zip per state + a few extras for the largest markets.

Each row is then fed through solar_economics.score_site() — the SAME engine
used for real API-driven rows — so the dashboard metrics are computed by
production code, not hand-rolled estimates.

Output: portfolio_sites.csv
"""

from __future__ import annotations

import csv
import json
import logging
import math
import random
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from solar_economics import (
    DEFAULTS,
    STATE_ELECTRICITY_PRICES,
    score_site,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

random.seed(20260511)  # reproducible

# ---------------------------------------------------------------------------
# Latitude → specific yield curve (kWh/kW-yr), calibrated to NREL PVWatts
# ---------------------------------------------------------------------------
# Anchors (lat → kWh/kW/yr):
#   25°N (FL/HI/AZ desert)  → 1750
#   33°N (Phoenix/LA)       → 1820
#   37°N (San Jose/Denver)  → 1650
#   42°N (Boston/Chicago)   → 1300
#   47°N (Seattle/MN)       → 1150
#   58°N (Anchorage)        →  900
# Plus per-state climate multiplier for cloudy/dry adjustment.

STATE_CLIMATE_MULT = {
    # Sunny/dry
    "AZ": 1.10, "NM": 1.08, "NV": 1.08, "CA": 1.05, "UT": 1.06,
    "CO": 1.05, "TX": 1.03, "OK": 1.02, "HI": 1.02, "FL": 1.00,
    # Average
    "GA": 1.00, "AL": 0.99, "SC": 0.99, "NC": 0.98, "TN": 0.97,
    "AR": 0.99, "MS": 0.98, "LA": 0.95, "KS": 1.01, "NE": 1.00,
    "MO": 0.98, "IA": 0.97, "SD": 0.99, "ND": 0.98, "WY": 1.03,
    "MT": 1.00, "ID": 1.02, "DE": 0.96, "DC": 0.96, "MD": 0.96,
    "VA": 0.97, "KY": 0.94, "WV": 0.92,
    # Cloudy
    "WA": 0.85, "OR": 0.88, "MI": 0.88, "OH": 0.90, "IN": 0.92,
    "IL": 0.93, "WI": 0.89, "MN": 0.92, "PA": 0.92, "NY": 0.90,
    "NJ": 0.94, "CT": 0.93, "MA": 0.94, "RI": 0.94, "NH": 0.92,
    "VT": 0.89, "ME": 0.88, "AK": 0.65,
}

# State-level TTS median $/W (Berkeley Lab TTS 2024 calibrated)
STATE_TTS_PPW = {
    "CA": 3.80, "TX": 2.95, "FL": 2.80, "AZ": 2.90, "NV": 2.85,
    "NJ": 3.65, "MA": 3.95, "NY": 3.85, "CT": 3.90, "RI": 3.95,
    "CO": 3.20, "UT": 3.10, "NM": 3.30, "HI": 4.20, "OR": 3.40,
    "WA": 3.50, "IL": 3.40, "MD": 3.55, "VA": 3.30, "PA": 3.30,
    "NC": 3.10, "SC": 3.15, "GA": 3.05, "MI": 3.45, "MN": 3.50,
    "WI": 3.55, "OH": 3.35, "IN": 3.40, "MO": 3.30, "KS": 3.25,
    "OK": 3.20, "AR": 3.30, "LA": 3.35, "AL": 3.30, "MS": 3.40,
    "TN": 3.25, "KY": 3.40, "WV": 3.55, "VT": 3.85, "NH": 3.80,
    "ME": 3.85, "DE": 3.55, "DC": 3.70, "IA": 3.45, "NE": 3.40,
    "ND": 3.60, "SD": 3.55, "MT": 3.60, "WY": 3.55, "ID": 3.45,
    "AK": 4.50,
}

# Approximate recent install sample size per state (LBL TTS 2024 rounded)
STATE_TTS_RECENT = {
    "CA": 380000, "TX": 95000, "FL": 110000, "AZ": 145000, "NV": 32000,
    "NJ": 88000, "MA": 92000, "NY": 145000, "CT": 28000, "RI": 9500,
    "CO": 41000, "UT": 38000, "NM": 22000, "HI": 78000, "OR": 16000,
    "WA": 22000, "IL": 28000, "MD": 38000, "VA": 22000, "PA": 24000,
    "NC": 38000, "SC": 22000, "GA": 28000, "MI": 12000, "MN": 18000,
    "WI": 7000, "OH": 15000, "IN": 8500, "MO": 8000, "KS": 4500,
    "OK": 5500, "AR": 4500, "LA": 8000, "AL": 4500, "MS": 1800,
    "TN": 8500, "KY": 3500, "WV": 2000, "VT": 9500, "NH": 11000,
    "ME": 18000, "DE": 8000, "DC": 7500, "IA": 6000, "NE": 3000,
    "ND": 600, "SD": 900, "MT": 4000, "WY": 1200, "ID": 7000,
    "AK": 1100,
}

# Hand-picked representative anchor cities (city, state, lat, lon, zip)
ANCHORS = [
    ("Birmingham", "AL", 33.5207, -86.8025, 35203),
    ("Anchorage", "AK", 61.2181, -149.9003, 99501),
    ("Phoenix", "AZ", 33.4484, -112.0740, 85001),
    ("Little Rock", "AR", 34.7465, -92.2896, 72201),
    ("Los Angeles", "CA", 34.0522, -118.2437, 90001),
    ("San Jose", "CA", 37.3382, -121.8863, 95192),
    ("San Diego", "CA", 32.7157, -117.1611, 92101),
    ("Denver", "CO", 39.7392, -104.9903, 80201),
    ("Hartford", "CT", 41.7658, -72.6734, 6101),
    ("Wilmington", "DE", 39.7391, -75.5398, 19801),
    ("Washington", "DC", 38.9072, -77.0369, 20001),
    ("Miami", "FL", 25.7617, -80.1918, 33101),
    ("Orlando", "FL", 28.5383, -81.3792, 32801),
    ("Atlanta", "GA", 33.7490, -84.3880, 30301),
    ("Honolulu", "HI", 21.3099, -157.8581, 96801),
    ("Boise", "ID", 43.6150, -116.2023, 83701),
    ("Chicago", "IL", 41.8781, -87.6298, 60601),
    ("Indianapolis", "IN", 39.7684, -86.1581, 46201),
    ("Des Moines", "IA", 41.5868, -93.6250, 50301),
    ("Wichita", "KS", 37.6872, -97.3301, 67201),
    ("Louisville", "KY", 38.2527, -85.7585, 40201),
    ("New Orleans", "LA", 29.9511, -90.0715, 70112),
    ("Portland", "ME", 43.6591, -70.2568, 4101),
    ("Baltimore", "MD", 39.2904, -76.6122, 21201),
    ("Boston", "MA", 42.3601, -71.0589, 2108),
    ("Detroit", "MI", 42.3314, -83.0458, 48201),
    ("Minneapolis", "MN", 44.9778, -93.2650, 55401),
    ("Jackson", "MS", 32.2988, -90.1848, 39201),
    ("Kansas City", "MO", 39.0997, -94.5786, 64101),
    ("Billings", "MT", 45.7833, -108.5007, 59101),
    ("Omaha", "NE", 41.2565, -95.9345, 68101),
    ("Las Vegas", "NV", 36.1699, -115.1398, 89101),
    ("Manchester", "NH", 42.9956, -71.4548, 3101),
    ("Newark", "NJ", 40.7357, -74.1724, 7101),
    ("Albuquerque", "NM", 35.0844, -106.6504, 87101),
    ("New York", "NY", 40.7128, -74.0060, 10001),
    ("Charlotte", "NC", 35.2271, -80.8431, 28201),
    ("Bismarck", "ND", 46.8083, -100.7837, 58501),
    ("Columbus", "OH", 39.9612, -82.9988, 43201),
    ("Oklahoma City", "OK", 35.4676, -97.5164, 73101),
    ("Portland", "OR", 45.5152, -122.6784, 97201),
    ("Philadelphia", "PA", 39.9526, -75.1652, 19101),
    ("Providence", "RI", 41.8240, -71.4128, 2901),
    ("Charleston", "SC", 32.7765, -79.9311, 29401),
    ("Sioux Falls", "SD", 43.5446, -96.7311, 57101),
    ("Nashville", "TN", 36.1627, -86.7816, 37201),
    ("Austin", "TX", 30.2672, -97.7431, 78701),
    ("Houston", "TX", 29.7604, -95.3698, 77001),
    ("Dallas", "TX", 32.7767, -96.7970, 75201),
    ("Salt Lake City", "UT", 40.7608, -111.8910, 84101),
    ("Burlington", "VT", 44.4759, -73.2121, 5401),
    ("Richmond", "VA", 37.5407, -77.4360, 23218),
    ("Seattle", "WA", 47.6062, -122.3321, 98101),
    ("Charleston", "WV", 38.3498, -81.6326, 25301),
    ("Milwaukee", "WI", 43.0389, -87.9065, 53201),
    ("Cheyenne", "WY", 41.1400, -104.8202, 82001),
]


def specific_yield_from_lat(lat: float) -> float:
    """Piecewise linear interpolation of latitude → kWh/kW-yr."""
    anchors = [(25, 1750), (33, 1820), (37, 1650), (42, 1300), (47, 1150), (58, 900)]
    lat = abs(lat)
    if lat <= anchors[0][0]:
        return anchors[0][1]
    if lat >= anchors[-1][0]:
        return anchors[-1][1]
    for (la1, y1), (la2, y2) in zip(anchors, anchors[1:]):
        if la1 <= lat <= la2:
            f = (lat - la1) / (la2 - la1)
            return y1 + f * (y2 - y1)
    return 1400


def synthesize_row(
    city: str,
    state: str,
    lat: float,
    lon: float,
    zip_code: int,
    capacity_kw: float = 5.0,
    segment: str = "RES",
) -> dict:
    """Build a row dict shaped like enriched_sites.csv input columns."""
    base_yield = specific_yield_from_lat(lat) * STATE_CLIMATE_MULT.get(state, 0.95)
    # +/- 4% site-level noise (roof orientation, microclimate)
    yield_kwh_per_kw = base_yield * (1 + random.uniform(-0.04, 0.04))
    annual_kwh = yield_kwh_per_kw * capacity_kw

    # Capacity factor = annual_kwh / (capacity_kw * 8760)
    cap_factor = annual_kwh / (capacity_kw * 8760) * 100

    return {
        "site_id": f"{state}_{zip_code:05d}_{int(capacity_kw)}kw_{segment}",
        "address_label": f"{city}, {state}",
        "city": city,
        "lat": lat,
        "lon": lon,
        "system_capacity_kw": capacity_kw,
        "azimuth": 180,
        "tilt": round(abs(lat), 0),  # latitude-tilt rule
        "array_type": 1,
        "module_type": 0,
        "losses": 14,
        "state": state,
        "zip_code": zip_code,
        "customer_segment": segment,
        "pvwatts_ac_annual_kwh": annual_kwh,
        "pvwatts_solrad_annual": yield_kwh_per_kw / 365.0,  # rough peak-sun-hours
        "pvwatts_capacity_factor": cap_factor,
        "tts_median_price_per_watt": STATE_TTS_PPW.get(state, 3.50),
        "tts_recent_sample_size": STATE_TTS_RECENT.get(state, 5000),
    }


def main():
    out_rows = []
    for city, state, lat, lon, zip_code in ANCHORS:
        # Residential 5 kW
        row = synthesize_row(city, state, lat, lon, zip_code, capacity_kw=5.0, segment="RES")
        result = score_site(row, DEFAULTS)
        # merge: keep inputs + add scored fields
        flat = {**row}
        for k, v in result.to_dict().items():
            if k in (
                "viability_score", "viability_label", "specific_yield", "lcoe",
                "simple_payback_years", "npv", "irr", "grid_parity_ratio",
                "net_cost", "gross_cost", "year_1_savings", "lifetime_savings",
                "annual_co2_avoided_tons", "electricity_rate_used", "rate_source",
                "resource_score", "economics_score", "site_fit_score", "policy_score",
                "capacity_factor", "tilt_deviation", "azimuth_deviation",
            ):
                flat[k] = v
        # production array (compacted to 25 ints for chart use)
        flat["year_production_json"] = json.dumps([int(x) for x in result.year_production])
        flat["cumulative_savings_json"] = json.dumps([int(x) for x in result.cumulative_savings])
        flat["cumulative_grid_cost_json"] = json.dumps([int(x) for x in result.cumulative_grid_cost])
        out_rows.append(flat)

        # Larger residential 8 kW (a few states)
        if state in {"CA", "TX", "FL", "AZ", "NY", "MA", "NJ", "CO", "HI"}:
            row2 = synthesize_row(city, state, lat, lon, zip_code, capacity_kw=8.0, segment="RES")
            r2 = score_site(row2, DEFAULTS)
            flat2 = {**row2}
            for k, v in r2.to_dict().items():
                if k in (
                    "viability_score", "viability_label", "specific_yield", "lcoe",
                    "simple_payback_years", "npv", "irr", "grid_parity_ratio",
                    "net_cost", "gross_cost", "year_1_savings", "lifetime_savings",
                    "annual_co2_avoided_tons", "electricity_rate_used", "rate_source",
                    "resource_score", "economics_score", "site_fit_score", "policy_score",
                    "capacity_factor", "tilt_deviation", "azimuth_deviation",
                ):
                    flat2[k] = v
            flat2["year_production_json"] = json.dumps([int(x) for x in r2.year_production])
            flat2["cumulative_savings_json"] = json.dumps([int(x) for x in r2.cumulative_savings])
            flat2["cumulative_grid_cost_json"] = json.dumps([int(x) for x in r2.cumulative_grid_cost])
            out_rows.append(flat2)

    df = pd.DataFrame(out_rows)
    out_path = Path("portfolio_sites.csv")
    df.to_csv(out_path, index=False)
    print(f"Wrote {len(df)} sites to {out_path}")
    print(f"Mean viability: {df['viability_score'].mean():.1f}")
    print(f"Median payback: {df['simple_payback_years'].median():.1f} yr")
    print(f"States covered: {df['state'].nunique()}")
    # Drop a JSON version for the dashboard (so we can embed directly)
    json_path = Path("portfolio_sites.json")
    json_path.write_text(df.to_json(orient="records", indent=2))
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
