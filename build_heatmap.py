#!/usr/bin/env python3
"""
build_heatmap.py — Score every ZIP in a state and generate interactive
Plotly heatmaps for static GitHub Pages deployment.

California (~2,593 ZIPs):
  heatmap_ca.html          — Statewide California (initial view: Bay Area)
  heatmap_norcal.html      — NorCal / Bay Area focus

Oregon (~417 ZIPs):
  heatmap_or.html          — Statewide Oregon
  heatmap_central_or.html  — Central Oregon focus (Bend · Redmond ·
                             Deschutes / Jefferson / Crook counties)

Color   = viability score (0–100, Viridis colorscale)
Opacity = confidence tier based on ZIP3-level TTS sample size:
  High   (ZIP3 ≥ 30 installs): opacity 0.90 — full color
  Medium (ZIP3  5–29 installs): opacity 0.55 — dimmed
  NA     (ZIP3  <5  installs):  gray,  opacity 0.30 — no score shown

If the DuckDB warehouse (or its raw_tts_installations table) is missing,
the maps are still built — every ZIP just falls into the gray NA tier and
a warning explains how to load the TTS data.

Usage:
    python3 build_heatmap.py        # California maps (default)
    python3 build_heatmap.py or     # Oregon maps
    python3 build_heatmap.py all    # both states
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import sys
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go

from solar_economics import Assumptions, score_site

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
WAREHOUSE_PATH = "data/warehouse/solar.duckdb"
USZIPS_PATH    = "data/uszips.csv"
SYSTEM_KW      = 4.5   # modeled residential system size

# IOU (PG&E / SCE / SDG&E) — NEM 3.0 assumptions
# Blended PG&E E-TOU-C residential rate 2024 ≈ $0.389/kWh
# NEM 3.0 export credit ≈ CPUC ACC average $0.055/kWh → ratio ≈ 0.055/0.389
IOU_ELEC_RATE    = 0.389
IOU_EXPORT_RATIO = 0.141   # NEM 3.0 ACC-based ≈ $0.055/kWh

# Municipal utility — typically still NEM 2.0 or better; EIA CA average
MUNI_ELEC_RATE    = 0.30
MUNI_EXPORT_RATIO = 0.50   # NEM 2.0-equivalent (full retail)

# ZIP3 prefixes primarily served by CA municipal utilities
# SMUD (Sacramento Metro) = 958xx
# LADWP covers parts of 900-904 but mixed with SCE — omitted for simplicity
MUNI_ZIP3 = {"958"}

# ── Oregon assumptions ────────────────────────────────────────────────────────
# Oregon's PUC-regulated utilities (PGE, Pacific Power) offer 1:1 retail-rate
# net metering (ORS 757.300) → export ratio 1.0.  The Central Oregon co-ops
# net monthly but cash out surplus at wholesale (~$0.05/kWh), so their
# effective export value is ~90% of retail for a load-sized system.
OR_EXPORT_RATIO      = 1.0
OR_COOP_EXPORT_RATIO = 0.90

# Approximate all-in residential rates by utility class ($/kWh, 2026 tariffs):
#   Portland General Electric (Portland metro / Salem)  ≈ $0.157 volumetric
#   EWEB (Eugene municipal)                             ≈ $0.13
#   Pacific Power (most of the rest, incl. Bend)        ≈ $0.140
#   Central OR co-ops (CEC Redmond, Midstate La Pine)   ≈ $0.088 energy charge
OR_PGE_RATE    = 0.157
OR_EWEB_RATE   = 0.13
OR_PACPWR_RATE = 0.140
OR_COOP_RATE   = 0.088

# ZIP3 prefixes dominated by PGE (Portland metro + Willamette Valley north)
OR_PGE_ZIP3  = {"970", "971", "972", "973"}
# Eugene Water & Electric Board (municipal) — city of Eugene ZIP5s only.
# The rest of the 974 prefix (Roseburg, Coos Bay, the south coast) is
# mostly Pacific Power territory with pockets of small PUDs/co-ops.
OR_EWEB_ZIP5 = {"97401", "97402", "97403", "97404", "97405", "97408"}

# Central Oregon electric-co-op territories at ZIP5 granularity.  The 977
# prefix mixes Pacific Power (Bend, Prineville, Madras) with two co-ops:
#   Central Electric Cooperative — Redmond, Sisters, Terrebonne, Powell Butte
#   Midstate Electric Cooperative — La Pine, Sunriver, Crescent, Chemult
OR_COOP_ZIP5 = {
    "97756": "Central Electric Co-op",   # Redmond
    "97759": "Central Electric Co-op",   # Sisters
    "97760": "Central Electric Co-op",   # Terrebonne
    "97753": "Central Electric Co-op",   # Powell Butte
    "97707": "Midstate Electric Co-op",  # Sunriver / Bend south
    "97739": "Midstate Electric Co-op",  # La Pine
    "97733": "Midstate Electric Co-op",  # Crescent
    "97737": "Midstate Electric Co-op",  # Gilchrist
    "97731": "Midstate Electric Co-op",  # Chemult
}

# ──────────────────────────────────────────────────────────────────────────────
# Regional county filters
# ──────────────────────────────────────────────────────────────────────────────
NORCAL_COUNTIES = {
    "City and County of San Francisco",  # SF's full county name in GeoNames data
    "San Mateo",
    "Alameda",
    "Santa Clara",
    "Contra Costa",
    "Marin",
    "Sonoma",
    "Napa",
    "Solano",
    "Sacramento",
    "Yolo",
    "Placer",
    "El Dorado",
    "San Joaquin",   # Stockton/Lodi area
    "Santa Cruz",
}

# Central Oregon — the Bend/Redmond high-desert tri-county area
CENTRAL_OR_COUNTIES = {
    "Deschutes",   # Bend, Redmond, Sisters, La Pine, Sunriver, Terrebonne
    "Jefferson",   # Madras, Culver
    "Crook",       # Prineville, Powell Butte
}

# ──────────────────────────────────────────────────────────────────────────────
# Confidence tiers
# ──────────────────────────────────────────────────────────────────────────────
def confidence_tier(n: int) -> str:
    if n >= 30:
        return "high"
    elif n >= 5:
        return "medium"
    else:
        return "na"


TIER_OPACITY = {"high": 0.90, "medium": 0.55, "na": 0.30}
TIER_SIZE    = {"high": 10,   "medium": 9,     "na": 7}
TIER_NAME    = {
    "high":   "High confidence (ZIP3 ≥30 installs)",
    "medium": "Medium confidence (5–29 installs)",
    "na":     "Insufficient data (<5 installs)",
}

# ──────────────────────────────────────────────────────────────────────────────
# Latitude → specific yield (kWh/kW-yr), California-calibrated
# Piecewise linear fit anchored to NREL PVWatts national results
# ──────────────────────────────────────────────────────────────────────────────
_YIELD_ANCHORS = [
    (32.6, 1810),  # San Diego
    (33.0, 1840),  # LA basin (sunniest CA latitude)
    (34.0, 1780),  # Ventura / Santa Barbara
    (35.5, 1720),  # Bakersfield fringe
    (37.0, 1660),  # Bay Area
    (38.0, 1615),  # Sacramento
    (39.0, 1565),  # Chico / Oroville
    (40.0, 1520),  # Redding
    (41.0, 1475),  # Mt Shasta
    (42.0, 1430),  # Oregon border
]


def lat_to_specific_yield(lat: float) -> float:
    """Piecewise-linear latitude → specific yield (kWh/kW/yr) for California."""
    if lat <= _YIELD_ANCHORS[0][0]:
        return float(_YIELD_ANCHORS[0][1])
    if lat >= _YIELD_ANCHORS[-1][0]:
        return float(_YIELD_ANCHORS[-1][1])
    for i in range(len(_YIELD_ANCHORS) - 1):
        lat0, y0 = _YIELD_ANCHORS[i]
        lat1, y1 = _YIELD_ANCHORS[i + 1]
        if lat0 <= lat <= lat1:
            t = (lat - lat0) / (lat1 - lat0)
            return float(y0 + t * (y1 - y0))
    return 1600.0


# ──────────────────────────────────────────────────────────────────────────────
# Oregon yield model — Cascade rain shadow, not just latitude
#
# Latitude alone fails in Oregon: the Cascades split the state into a cloudy
# marine west side and a sunny high-desert east side.  Bend (44.1°N, east)
# out-produces Portland (45.5°N, west) by ~25% despite being only 1.4° south.
# Two anchor sets, selected by longitude relative to the Cascade crest
# (~121.8–122.0°W); anchors calibrated to NREL PVWatts (south-facing,
# latitude tilt, 14% losses).
# ──────────────────────────────────────────────────────────────────────────────
_OR_CASCADE_CREST_LON = -122.0

_OR_WEST_ANCHORS = [   # marine / Willamette Valley / Rogue Valley
    (42.0, 1425),      # Ashland / Medford (Rogue Valley — driest west-side pocket)
    (43.2, 1350),      # Roseburg / Umpqua Valley
    (44.1, 1250),      # Eugene
    (45.0, 1220),      # Salem
    (45.6, 1200),      # Portland
    (46.2, 1120),      # Astoria / lower Columbia
]

_OR_EAST_ANCHORS = [   # high desert east of the Cascades
    (42.0, 1600),      # Klamath Falls / Lakeview
    (43.5, 1540),      # Chemult / Christmas Valley
    (44.2, 1500),      # Bend / Redmond / Prineville
    (45.0, 1440),      # Madras north / Warm Springs
    (45.8, 1380),      # Pendleton / Columbia Plateau
]

_OR_COAST_LON  = -123.85   # west of this ≈ coastal fog belt
_OR_COAST_MULT = 0.93


def _interp_anchors(lat: float, anchors: list[tuple[float, float]]) -> float:
    if lat <= anchors[0][0]:
        return float(anchors[0][1])
    if lat >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (la0, y0), (la1, y1) in zip(anchors, anchors[1:]):
        if la0 <= lat <= la1:
            t = (lat - la0) / (la1 - la0)
            return float(y0 + t * (y1 - y0))
    return 1300.0


def or_specific_yield(lat: float, lon: float) -> float:
    """Oregon latitude+longitude → specific yield (kWh/kW/yr)."""
    if lon >= _OR_CASCADE_CREST_LON:
        return _interp_anchors(lat, _OR_EAST_ANCHORS)
    y = _interp_anchors(lat, _OR_WEST_ANCHORS)
    if lon <= _OR_COAST_LON:
        y *= _OR_COAST_MULT
    return y


def specific_yield_for(state: str, lat: float, lon: float) -> float:
    """Dispatch to the state-appropriate yield model."""
    if state == "OR":
        return or_specific_yield(lat, lon)
    return lat_to_specific_yield(lat)


# ──────────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────────

def load_state_zips(state: str) -> pd.DataFrame:
    """Load one state's ZIPs from uszips.csv, add zip3 column."""
    df = pd.read_csv(USZIPS_PATH, comment="#", dtype={"zip": str})
    sub = df[df.state_id == state].copy()
    sub["zip"] = sub["zip"].str.zfill(5)
    sub["zip3"] = sub["zip"].str[:3]
    return sub.reset_index(drop=True)


def load_zip3_tts(
    con: duckdb.DuckDBPyConnection, state: str
) -> dict[str, tuple[int, float]]:
    """Return dict: zip3 → (install_count, median_price_per_watt)."""
    rows = con.execute("""
        SELECT
            SUBSTR(zip_code, 1, 3)           AS zip3,
            COUNT(*)                          AS n,
            ROUND(MEDIAN(price_per_watt), 2) AS med_ppw
        FROM raw_tts_installations
        WHERE state = ?
          AND price_per_watt BETWEEN 1.0 AND 15.0
        GROUP BY zip3
    """, [state]).fetchall()
    return {z3: (int(n), float(ppw)) for z3, n, ppw in rows}


def try_load_zip3_tts(state: str) -> dict[str, tuple[int, float]]:
    """Load ZIP3 TTS sample sizes; empty dict (all-NA tiers) if unavailable."""
    try:
        con = duckdb.connect(WAREHOUSE_PATH, read_only=True)
        tts_map = load_zip3_tts(con, state)
        con.close()
        return tts_map
    except Exception as exc:
        print("      " + "!" * 56)
        print(f"      WARNING: warehouse unavailable ({exc}).")
        print("      Maps will still be generated, but EVERY ZIP will render")
        print("      gray ('insufficient data') — no confidence tiers.")
        print("      To fix: python3 solar_etl.py --load-tts data/tts_cleaned.csv")
        print("      " + "!" * 56)
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Scoring loop
# ──────────────────────────────────────────────────────────────────────────────

# Default $/W when the ZIP3 TTS sample is too small (LBL TTS 2024 medians)
STATE_DEFAULT_PPW = {"CA": 3.80, "OR": 3.40}


def _utility_assumptions(state: str, zip5: str, zip3: str) -> tuple[Assumptions, str]:
    """Pick (Assumptions, utility_label) for a ZIP based on its utility."""
    if state == "OR":
        if zip5 in OR_COOP_ZIP5:
            # Co-ops: cheap BPA power, but monthly netting w/ wholesale cash-out
            return Assumptions(
                electricity_price_override=OR_COOP_RATE,
                nem_export_ratio=OR_COOP_EXPORT_RATIO,
            ), OR_COOP_ZIP5[zip5]
        if zip5 in OR_EWEB_ZIP5:
            rate, label = OR_EWEB_RATE, "EWEB (muni)"
        elif zip3 in OR_PGE_ZIP3:
            rate, label = OR_PGE_RATE, "PGE"
        else:
            rate, label = OR_PACPWR_RATE, "Pacific Power"
        return Assumptions(
            electricity_price_override=rate,
            nem_export_ratio=OR_EXPORT_RATIO,
        ), label

    # California
    if zip3 in MUNI_ZIP3:
        return Assumptions(
            electricity_price_override=MUNI_ELEC_RATE,
            nem_export_ratio=MUNI_EXPORT_RATIO,
        ), "Muni"
    return Assumptions(
        electricity_price_override=IOU_ELEC_RATE,
        nem_export_ratio=IOU_EXPORT_RATIO,
    ), "IOU (NEM 3.0)"


def score_all_zips(
    zips: pd.DataFrame,
    tts_map: dict[str, tuple[int, float]],
    state: str = "CA",
) -> pd.DataFrame:
    """
    Score every ZIP in *zips* using score_site() and return a DataFrame with
    all fields needed for the map.
    """
    records = []
    errors  = 0
    default_ppw = STATE_DEFAULT_PPW.get(state, 3.50)

    for row in zips.itertuples(index=False):
        z3 = row.zip3
        tts_n, tts_ppw = tts_map.get(z3, (0, 3.50))

        # Utility type → different electricity rate / export assumptions
        assum, utility_label = _utility_assumptions(state, row.zip, z3)

        # Specific yield from location; annual kWh for modeled system
        sy         = specific_yield_for(state, row.lat, row.lng)
        annual_kwh = sy * SYSTEM_KW

        site_row = {
            "site_id":                    row.zip,
            "state":                      state,
            "address_label":              f"{row.city}, {state} {row.zip}",
            "system_capacity_kw":         SYSTEM_KW,
            "pvwatts_ac_annual_kwh":      annual_kwh,
            "pvwatts_capacity_factor":    sy / 8760.0,
            "lat":                        row.lat,
            "tilt":                       row.lat,   # optimal fixed tilt = latitude
            "azimuth":                    180.0,     # true south
            "losses":                     14.0,      # PVWatts default
            # Use TTS median $/W if sample size ≥ 5; otherwise state default
            "tts_median_price_per_watt":  tts_ppw if tts_n >= 5 else default_ppw,
            "tts_recent_sample_size":     float(tts_n),
        }

        try:
            result = score_site(site_row, assumptions=assum)
            tier   = confidence_tier(tts_n)
            records.append({
                "zip":           row.zip,
                "city":          row.city,
                "state":         state,
                "county":        getattr(row, "county", ""),
                "lat":           row.lat,
                "lng":           row.lng,
                "zip3":          z3,
                "tts_n":         tts_n,
                "tier":          tier,
                "score":         round(result.viability_score, 1),
                "lcoe":          round(result.lcoe, 4),
                "payback":       round(result.simple_payback_years, 1),
                "npv":           round(result.npv, 0),
                "irr":           round(result.irr * 100, 1) if result.irr else None,
                "annual_kwh":    round(annual_kwh, 0),
                "specific_yield": round(sy, 0),
                "ppw_used":      round(site_row["tts_median_price_per_watt"], 2),
                "rate_used":     round(result.electricity_rate_used, 3),
                "label":         result.viability_label,
                "utility_type":  utility_label,
            })
        except Exception as exc:
            logger.warning("ZIP %s (%s) failed: %s", row.zip, row.city, exc)
            errors += 1

    if errors:
        print(f"      {errors} ZIPs skipped due to scoring errors")

    return pd.DataFrame(records)


# ──────────────────────────────────────────────────────────────────────────────
# Hover text builder
# ──────────────────────────────────────────────────────────────────────────────

def build_hover(r: pd.Series, show_score: bool = True) -> str:
    """Build HTML hover tooltip for a ZIP row."""
    if not show_score:
        return (
            f"<b>{r.city}, {r.state} {r.zip}</b><br>"
            f"County: {r.county}<br>"
            f"Confidence: Insufficient data (ZIP3 n={r.tts_n})<br>"
            f"Score: N/A"
        )
    irr_str = f"{r.irr:.1f}%" if r.irr is not None and not pd.isna(r.irr) else "n/a"
    return (
        f"<b>{r.city}, {r.state} {r.zip}</b><br>"
        f"County: {r.county}<br>"
        f"<b>Score: {r.score:.0f}/100</b> — {r.label}<br>"
        f"Payback: {r.payback:.1f} yr | IRR: {irr_str}<br>"
        f"LCOE: ${r.lcoe:.3f}/kWh | NPV: ${r.npv:,.0f}<br>"
        f"Yield: {r.specific_yield:,.0f} kWh/kW | {r.annual_kwh:,.0f} kWh/yr<br>"
        f"Rate: ${r.rate_used:.3f}/kWh | $/W: {r.ppw_used:.2f}<br>"
        f"Utility: {r.utility_type} | ZIP3 n={r.tts_n:,}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Map builder
# ──────────────────────────────────────────────────────────────────────────────

_CA_FOOTNOTE = (
    "Data: NREL PVWatts (yield), Berkeley Lab TTS ($/W, confidence), EIA (rates) · "
    "NEM 3.0 export ≈ $0.055/kWh CPUC ACC avg · "
    "Score = 25% resource + 50% economics + 15% site-fit + 10% policy"
)

_OR_FOOTNOTE = (
    "Data: NREL PVWatts (yield), Berkeley Lab TTS ($/W, confidence), EIA (rates) · "
    "OR: 1:1 net metering (ORS 757.300) · yield model splits at the Cascade crest · "
    "Score = 25% resource + 50% economics + 15% site-fit + 10% policy"
)


def build_map(
    df: pd.DataFrame,
    title: str,
    center_lat: float,
    center_lon: float,
    zoom: float,
    output_path: Path,
    footnote: str = _CA_FOOTNOTE,
) -> None:
    """
    Build a Plotly Scattermap figure with three confidence-tier layers
    and write to a self-contained HTML file.
    """
    traces: list[go.BaseTraceType] = []

    # ── Layer 1: NA tier (gray, score not shown) ──────────────────────────────
    df_na = df[df.tier == "na"]
    if not df_na.empty:
        hover_na = [build_hover(r, show_score=False) for _, r in df_na.iterrows()]
        traces.append(go.Scattermap(
            lat=df_na["lat"],
            lon=df_na["lng"],
            mode="markers",
            marker=dict(
                size=TIER_SIZE["na"],
                color="#9E9E9E",
                opacity=TIER_OPACITY["na"],
            ),
            text=hover_na,
            hoverinfo="text",
            name=TIER_NAME["na"],
            showlegend=True,
        ))

    # ── Layer 2: Medium tier (colored, dimmed) ────────────────────────────────
    df_med = df[df.tier == "medium"]
    if not df_med.empty:
        hover_med = [build_hover(r) for _, r in df_med.iterrows()]
        traces.append(go.Scattermap(
            lat=df_med["lat"],
            lon=df_med["lng"],
            mode="markers",
            marker=dict(
                size=TIER_SIZE["medium"],
                color=df_med["score"],
                colorscale="Viridis",
                cmin=40, cmax=95,
                opacity=TIER_OPACITY["medium"],
                showscale=False,
            ),
            text=hover_med,
            hoverinfo="text",
            name=TIER_NAME["medium"],
            showlegend=True,
        ))

    # ── Layer 3: High tier (colored, full opacity, colorbar) ──────────────────
    df_hi = df[df.tier == "high"]
    if not df_hi.empty:
        hover_hi = [build_hover(r) for _, r in df_hi.iterrows()]
        traces.append(go.Scattermap(
            lat=df_hi["lat"],
            lon=df_hi["lng"],
            mode="markers",
            marker=dict(
                size=TIER_SIZE["high"],
                color=df_hi["score"],
                colorscale="Viridis",
                cmin=40, cmax=95,
                opacity=TIER_OPACITY["high"],
                colorbar=dict(
                    title=dict(text="Viability<br>Score", side="right"),
                    thickness=14,
                    len=0.60,
                    yanchor="middle",
                    y=0.50,
                    x=1.01,
                    tickvals=[40, 50, 65, 80, 95],
                    ticktext=["40 Poor", "50", "65 Good", "80 Excellent", "95"],
                    bgcolor="rgba(255,255,255,0.85)",
                    bordercolor="#CCCCCC",
                    borderwidth=1,
                ),
                showscale=True,
            ),
            text=hover_hi,
            hoverinfo="text",
            name=TIER_NAME["high"],
            showlegend=True,
        ))

    fig = go.Figure(traces)

    fig.update_layout(
        title=dict(
            text=title,
            x=0.50,
            xanchor="center",
            font=dict(size=17, family="Inter, -apple-system, sans-serif", color="#1B1464"),
        ),
        map=dict(
            style="open-street-map",
            center=dict(lat=center_lat, lon=center_lon),
            zoom=zoom,
        ),
        legend=dict(
            x=0.01,
            y=0.01,
            bgcolor="rgba(255,255,255,0.90)",
            bordercolor="rgba(0,0,0,0.12)",
            borderwidth=1,
            font=dict(size=11, family="Inter, sans-serif"),
            itemsizing="constant",
        ),
        margin=dict(l=0, r=0, t=50, b=0),
        height=720,
        hoverlabel=dict(
            bgcolor="rgba(255,255,255,0.95)",
            bordercolor="#26A69A",
            font=dict(size=12, family="Inter, sans-serif"),
        ),
    )

    # Annotation row: data credits + policy note
    fig.add_annotation(
        text=footnote,
        xref="paper", yref="paper",
        x=0.5, y=-0.01,
        xanchor="center", yanchor="top",
        showarrow=False,
        font=dict(size=9, color="#888888"),
    )

    fig.write_html(
        str(output_path),
        include_plotlyjs="cdn",
        full_html=True,
        config={"displayModeBar": True, "scrollZoom": True, "responsive": True},
    )
    size_kb = output_path.stat().st_size // 1024
    print(f"  ✓  {output_path.name:30s}  ({size_kb:,} KB)")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def _score_state(state: str) -> pd.DataFrame:
    """Shared load → TTS → score pipeline for one state."""
    print(f"\n[1/3] Loading {state} ZIPs from uszips.csv …")
    zips = load_state_zips(state)
    print(f"      {len(zips):,} {state} ZIPs loaded")

    print("\n[2/3] Querying ZIP3 install sample sizes from warehouse …")
    tts_map = try_load_zip3_tts(state)
    n_high   = sum(1 for n, _ in tts_map.values() if n >= 30)
    n_medium = sum(1 for n, _ in tts_map.values() if 5 <= n < 30)
    n_low    = sum(1 for n, _ in tts_map.values() if n < 5)
    print(f"      {len(tts_map)} {state} ZIP3 prefixes found")
    print(f"      High (≥30): {n_high}  Medium (5–29): {n_medium}  Low (<5): {n_low}")

    print(f"\n[3/3] Scoring {len(zips):,} ZIPs …")
    results = score_all_zips(zips, tts_map, state=state)
    print(f"      Scored: {len(results):,} ZIPs")
    tc = results["tier"].value_counts().to_dict()
    print(f"      Tiers — high: {tc.get('high',0)}, medium: {tc.get('medium',0)}, na: {tc.get('na',0)}")
    print(f"      Score range: {results['score'].min():.1f} – {results['score'].max():.1f}  "
          f"(mean {results['score'].mean():.1f})")
    return results


def _print_top5(results: pd.DataFrame) -> None:
    hi = results[results.tier == "high"]
    if not hi.empty:
        top5 = hi.nlargest(5, "score")[["city", "zip", "score", "payback"]].values
        print("  Top 5 scoring ZIPs (high-confidence):")
        for city, z, s, pb in top5:
            print(f"    {city:20s} {z}  score={s:.0f}  payback={pb:.1f} yr")


def build_california() -> None:
    print("=" * 62)
    print("  build_heatmap.py — California Solar Viability Heatmap")
    print("=" * 62)
    results = _score_state("CA")

    print("\nGenerating interactive maps …")
    # Statewide CA — initial view centered on Bay Area
    build_map(
        results,
        title="California Residential Solar Viability — All ZIP Codes",
        center_lat=37.45,
        center_lon=-119.80,
        zoom=5.5,
        output_path=Path("heatmap_ca.html"),
    )

    # NorCal subset — Bay Area through Sacramento
    norcal = results[results["county"].isin(NORCAL_COUNTIES)].copy()
    print(f"      NorCal subset: {len(norcal):,} ZIPs across {norcal['county'].nunique()} counties")
    build_map(
        norcal,
        title="NorCal Solar Viability — Bay Area · Peninsula · Sacramento Valley",
        center_lat=37.70,
        center_lon=-122.05,
        zoom=8.2,
        output_path=Path("heatmap_norcal.html"),
    )

    # Compact ZIP lookup JSON for the static GitHub Pages search box
    lookup = []
    for r in results.itertuples(index=False):
        show = r.tier != "na"
        lookup.append({
            "z": r.zip,
            "c": r.city,
            "co": getattr(r, "county", ""),
            "s": r.score if show else None,
            "p": r.payback if show else None,
            "n": int(r.npv) if show else None,
            "l": r.lcoe if show else None,
            "v": r.label if show else None,
            "t": r.tier,
        })
    json_path = Path("data/ca_zips.json")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(lookup, separators=(",", ":")))
    print(f"      Lookup JSON: {json_path}  ({json_path.stat().st_size // 1024} KB)")

    deploy = Path("_deploy/solar")
    if deploy.exists():
        (deploy / "data").mkdir(parents=True, exist_ok=True)
        shutil.copy2(json_path, deploy / "data" / "ca_zips.json")
        shutil.copy2(Path("heatmap_ca.html"), deploy / "heatmap" / "heatmap_ca.html")
        shutil.copy2(Path("heatmap_norcal.html"), deploy / "heatmap" / "heatmap_norcal.html")
        print("      Copied maps + lookup JSON to _deploy/solar/")

    print("\n" + "=" * 62)
    print("  heatmap_ca.html      — Statewide California view")
    print("  heatmap_norcal.html  — NorCal / Bay Area view")
    print("  data/ca_zips.json    — ZIP lookup for the public demo")
    print()
    _print_top5(results)
    print("=" * 62)


def build_oregon() -> None:
    print("=" * 62)
    print("  build_heatmap.py — Oregon Solar Viability Heatmap")
    print("=" * 62)
    results = _score_state("OR")

    print("\nGenerating interactive maps …")
    # Statewide OR — centered between the Willamette Valley and the high desert
    build_map(
        results,
        title="Oregon Residential Solar Viability — All ZIP Codes",
        center_lat=44.10,
        center_lon=-121.60,
        zoom=6.0,
        output_path=Path("heatmap_or.html"),
        footnote=_OR_FOOTNOTE,
    )

    # Central Oregon subset — Bend / Redmond tri-county high desert
    central = results[results["county"].isin(CENTRAL_OR_COUNTIES)].copy()
    print(f"      Central OR subset: {len(central):,} ZIPs across "
          f"{central['county'].nunique()} counties")
    build_map(
        central,
        title="Central Oregon Solar Viability — Bend · Redmond · High Desert",
        center_lat=44.15,
        center_lon=-121.30,
        zoom=8.3,
        output_path=Path("heatmap_central_or.html"),
        footnote=_OR_FOOTNOTE,
    )

    print("\n" + "=" * 62)
    print("  heatmap_or.html          — Statewide Oregon view")
    print("  heatmap_central_or.html  — Central Oregon (Bend/Redmond) view")
    print()
    _print_top5(results)
    print("=" * 62)


def main() -> None:
    target = (sys.argv[1].lower() if len(sys.argv) > 1 else "ca")
    if target not in ("ca", "or", "all"):
        sys.exit(f"Unknown target {target!r}. Usage: python3 build_heatmap.py [ca|or|all]")
    if target in ("ca", "all"):
        build_california()
    if target in ("or", "all"):
        build_oregon()


if __name__ == "__main__":
    main()
