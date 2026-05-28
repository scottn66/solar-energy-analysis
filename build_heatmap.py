#!/usr/bin/env python3
"""
build_heatmap.py — Score all ~2,593 CA ZIPs and generate two interactive
Plotly heatmaps for static GitHub Pages deployment:

  heatmap_ca.html      — Statewide California (initial view: Bay Area)
  heatmap_norcal.html  — NorCal / Bay Area focus

Color   = viability score (0–100, Viridis colorscale)
Opacity = confidence tier based on ZIP3-level TTS sample size:
  High   (ZIP3 ≥ 30 installs): opacity 0.90 — full color
  Medium (ZIP3  5–29 installs): opacity 0.55 — dimmed
  NA     (ZIP3  <5  installs):  gray,  opacity 0.30 — no score shown

Usage:
    python3 build_heatmap.py
    # Outputs: heatmap_ca.html, heatmap_norcal.html
"""

from __future__ import annotations

import logging
import math
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

# ──────────────────────────────────────────────────────────────────────────────
# NorCal county filter
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
# Data loading
# ──────────────────────────────────────────────────────────────────────────────

def load_ca_zips() -> pd.DataFrame:
    """Load 2,593 CA ZIPs from uszips.csv, add zip3 column."""
    df = pd.read_csv(USZIPS_PATH, comment="#", dtype={"zip": str})
    ca = df[df.state_id == "CA"].copy()
    ca["zip"] = ca["zip"].str.zfill(5)
    ca["zip3"] = ca["zip"].str[:3]
    return ca.reset_index(drop=True)


def load_zip3_tts(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[int, float]]:
    """Return dict: zip3 → (install_count, median_price_per_watt)."""
    rows = con.execute("""
        SELECT
            SUBSTR(zip_code, 1, 3)           AS zip3,
            COUNT(*)                          AS n,
            ROUND(MEDIAN(price_per_watt), 2) AS med_ppw
        FROM raw_tts_installations
        WHERE state = 'CA'
          AND price_per_watt BETWEEN 1.0 AND 15.0
        GROUP BY zip3
    """).fetchall()
    return {z3: (int(n), float(ppw)) for z3, n, ppw in rows}


# ──────────────────────────────────────────────────────────────────────────────
# Scoring loop
# ──────────────────────────────────────────────────────────────────────────────

def score_all_zips(
    zips: pd.DataFrame,
    tts_map: dict[str, tuple[int, float]],
) -> pd.DataFrame:
    """
    Score every CA ZIP using score_site() and return a DataFrame with
    all fields needed for the map.
    """
    records = []
    errors  = 0

    for row in zips.itertuples(index=False):
        z3 = row.zip3
        tts_n, tts_ppw = tts_map.get(z3, (0, 3.50))

        # Utility type → different electricity rate / export assumptions
        is_muni = z3 in MUNI_ZIP3
        if is_muni:
            assum = Assumptions(
                electricity_price_override=MUNI_ELEC_RATE,
                nem_export_ratio=MUNI_EXPORT_RATIO,
            )
        else:
            assum = Assumptions(
                electricity_price_override=IOU_ELEC_RATE,
                nem_export_ratio=IOU_EXPORT_RATIO,
            )

        # Specific yield from latitude; annual kWh for modeled system
        sy         = lat_to_specific_yield(row.lat)
        annual_kwh = sy * SYSTEM_KW

        site_row = {
            "site_id":                    row.zip,
            "state":                      "CA",
            "address_label":              f"{row.city}, CA {row.zip}",
            "system_capacity_kw":         SYSTEM_KW,
            "pvwatts_ac_annual_kwh":      annual_kwh,
            "pvwatts_capacity_factor":    sy / 8760.0,
            "lat":                        row.lat,
            "tilt":                       row.lat,   # optimal fixed tilt = latitude
            "azimuth":                    180.0,     # true south
            "losses":                     14.0,      # PVWatts default
            # Use TTS median $/W if sample size ≥ 5; otherwise state default
            "tts_median_price_per_watt":  tts_ppw if tts_n >= 5 else 3.80,
            "tts_recent_sample_size":     float(tts_n),
        }

        try:
            result = score_site(site_row, assumptions=assum)
            tier   = confidence_tier(tts_n)
            records.append({
                "zip":           row.zip,
                "city":          row.city,
                "county":        getattr(row, "county", ""),
                "lat":           row.lat,
                "lng":           row.lng,
                "zip3":          z3,
                "tts_n":         tts_n,
                "tier":          tier,
                "is_muni":       is_muni,
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
                "utility_type":  "Muni" if is_muni else "IOU (NEM 3.0)",
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
            f"<b>{r.city}, CA {r.zip}</b><br>"
            f"County: {r.county}<br>"
            f"Confidence: Insufficient data (ZIP3 n={r.tts_n})<br>"
            f"Score: N/A"
        )
    irr_str = f"{r.irr:.1f}%" if r.irr is not None and not pd.isna(r.irr) else "n/a"
    return (
        f"<b>{r.city}, CA {r.zip}</b><br>"
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

def build_map(
    df: pd.DataFrame,
    title: str,
    center_lat: float,
    center_lon: float,
    zoom: float,
    output_path: Path,
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

    # Subtitle annotation
    subtitle = (
        f"4.5 kW system · IOU: ${IOU_ELEC_RATE}/kWh NEM 3.0 (export ratio {IOU_EXPORT_RATIO}) · "
        f"Muni: ${MUNI_ELEC_RATE}/kWh NEM 2.0-equiv · "
        f"ZIP3 confidence tiers from {sum(v[0] for v in [])}"
    )

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

    # Annotation row: data credits + NEM 3.0 note
    fig.add_annotation(
        text=(
            "Data: NREL PVWatts (yield), Berkeley Lab TTS ($/W, confidence), EIA (rates) · "
            "NEM 3.0 export ≈ $0.055/kWh CPUC ACC avg · "
            "Score = 25% resource + 50% economics + 15% site-fit + 10% policy"
        ),
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

def main() -> None:
    print("=" * 62)
    print("  build_heatmap.py — California Solar Viability Heatmap")
    print("=" * 62)

    # 1. Load CA ZIP coordinates
    print("\n[1/4] Loading CA ZIPs from uszips.csv …")
    zips = load_ca_zips()
    print(f"      {len(zips):,} CA ZIPs loaded")

    # 2. Load ZIP3 TTS sample sizes from warehouse
    print("\n[2/4] Querying ZIP3 install sample sizes from warehouse …")
    try:
        con = duckdb.connect(WAREHOUSE_PATH, read_only=True)
        tts_map = load_zip3_tts(con)
        con.close()
    except Exception as exc:
        sys.exit(f"\n  ERROR opening warehouse: {exc}\n"
                 f"  Make sure {WAREHOUSE_PATH} exists and has rows in raw_tts_installations.\n"
                 f"  Run: python3 solar_etl.py --load-tts data/tts_cleaned.csv")

    n_high   = sum(1 for n, _ in tts_map.values() if n >= 30)
    n_medium = sum(1 for n, _ in tts_map.values() if 5 <= n < 30)
    n_low    = sum(1 for n, _ in tts_map.values() if n < 5)
    print(f"      {len(tts_map)} CA ZIP3 prefixes found")
    print(f"      High (≥30): {n_high}  Medium (5–29): {n_medium}  Low (<5): {n_low}")

    # 3. Score all ZIPs
    print(f"\n[3/4] Scoring {len(zips):,} ZIPs …  (typically 30–60 s)")
    results = score_all_zips(zips, tts_map)
    print(f"      Scored: {len(results):,} ZIPs")
    tc = results["tier"].value_counts().to_dict()
    print(f"      Tiers — high: {tc.get('high',0)}, medium: {tc.get('medium',0)}, na: {tc.get('na',0)}")
    print(f"      Score range: {results['score'].min():.1f} – {results['score'].max():.1f}  "
          f"(mean {results['score'].mean():.1f})")

    # 4. Build maps
    print("\n[4/4] Generating interactive maps …")

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

    print("\n" + "=" * 62)
    print("  Done!")
    print("  heatmap_ca.html      — Statewide California view")
    print("  heatmap_norcal.html  — NorCal / Bay Area view")
    print()
    # Quick stats
    hi = results[results.tier == "high"]
    if not hi.empty:
        top5 = hi.nlargest(5, "score")[["city", "zip", "score", "payback"]].values
        print("  Top 5 scoring ZIPs (high-confidence):")
        for city, z, s, pb in top5:
            print(f"    {city:20s} {z}  score={s:.0f}  payback={pb:.1f} yr")
    print("=" * 62)


if __name__ == "__main__":
    main()
