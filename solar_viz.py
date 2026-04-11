"""
solar_viz.py — Premium HTML report generator for solar site analysis.

Generates a standalone, scrollable analyst-grade report using Plotly.
All charts use a custom solar-physics-inspired palette with Inter typography.

Usage:
    from solar_viz import generate_report
    generate_report([site_result], "report.html")

CLI:
    python solar_viz.py site_report.json --output report.html
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from solar_economics import SiteResult, Assumptions, DEFAULTS, score_site, sensitivity_tornado

# ---------------------------------------------------------------------------
# Custom color palette — solar physics inspired
# ---------------------------------------------------------------------------
PALETTE = {
    # Core spectrum
    "indigo": "#1B1464",        # deep night / baseline
    "indigo_light": "#2D2B8C",  # lighter indigo for contrast
    "amber": "#F5A623",         # warm irradiance
    "coral": "#E85D4A",         # hot / risk / expense
    "sage": "#4CAF78",          # financial win / green
    "sage_light": "#7BC9A0",    # lighter green
    "teal": "#26A69A",          # secondary accent
    "slate": "#546E7A",         # neutral text
    "warm_gray": "#8D8D8D",     # axis labels

    # Functional
    "red_risk": "#D32F2F",      # negative / underwater
    "red_light": "#FFCDD2",     # light red background
    "green_win": "#2E7D32",     # positive / payback achieved
    "green_light": "#C8E6C9",   # light green background
    "amber_warn": "#FF8F00",    # warning / marginal
    "bg_page": "#FAFBFD",       # page background
    "bg_card": "#FFFFFF",       # card background
    "text_primary": "#1A1A2E",  # main text
    "text_secondary": "#546E7A",# muted text
    "grid_subtle": "#E8ECF0",   # gridline
    "divider": "#DEE2E8",       # section divider
}

# Viability band colors
VIABILITY_BANDS = [
    (0, 50, PALETTE["coral"], "Poor"),
    (50, 65, PALETTE["amber_warn"], "Marginal"),
    (65, 80, PALETTE["teal"], "Good"),
    (80, 100, PALETTE["sage"], "Excellent"),
]

# ---------------------------------------------------------------------------
# Chart layout defaults
# ---------------------------------------------------------------------------
def _base_layout(**overrides) -> dict:
    """Reusable Plotly layout settings for consistent styling."""
    layout = dict(
        font=dict(family="Inter, IBM Plex Sans, -apple-system, sans-serif", color=PALETTE["text_primary"]),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=60, r=30, t=80, b=50),
        xaxis=dict(
            gridcolor=PALETTE["grid_subtle"],
            gridwidth=1,
            zerolinecolor=PALETTE["divider"],
            showgrid=False,
        ),
        yaxis=dict(
            gridcolor=PALETTE["grid_subtle"],
            gridwidth=1,
            zerolinecolor=PALETTE["divider"],
        ),
        hoverlabel=dict(
            bgcolor=PALETTE["bg_card"],
            font_size=13,
            font_family="Inter, sans-serif",
            bordercolor=PALETTE["divider"],
        ),
    )
    layout.update(overrides)
    return layout


def _annotate_subtitle(fig: go.Figure, text: str, y: float = 1.02):
    """Add a light subtitle annotation below the title."""
    fig.add_annotation(
        text=f"<i>{text}</i>",
        xref="paper", yref="paper",
        x=0, y=y, showarrow=False,
        font=dict(size=12, color=PALETTE["text_secondary"]),
        xanchor="left",
    )


def _annotate_footnote(fig: go.Figure, text: str):
    """Add a data-source footnote at the bottom."""
    fig.add_annotation(
        text=text,
        xref="paper", yref="paper",
        x=0, y=-0.15, showarrow=False,
        font=dict(size=10, color=PALETTE["warm_gray"]),
        xanchor="left",
    )


# ---------------------------------------------------------------------------
# Individual chart builders
# ---------------------------------------------------------------------------

def chart_hero_gauge(r: SiteResult) -> str:
    """Radial arc gauge showing the 0–100 viability score with threshold bands."""
    score = r.viability_score

    # Determine band color
    if score >= 80:
        bar_color = PALETTE["sage"]
    elif score >= 65:
        bar_color = PALETTE["teal"]
    elif score >= 50:
        bar_color = PALETTE["amber_warn"]
    else:
        bar_color = PALETTE["coral"]

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number=dict(
            font=dict(size=64, color=PALETTE["text_primary"], family="Inter, sans-serif"),
            suffix="",
        ),
        title=dict(
            text=f"<b>{r.viability_label}</b>",
            font=dict(size=16, color=PALETTE["text_secondary"]),
        ),
        gauge=dict(
            axis=dict(
                range=[0, 100],
                tickwidth=2,
                tickcolor=PALETTE["divider"],
                dtick=10,
                tickfont=dict(size=11, color=PALETTE["warm_gray"]),
            ),
            bar=dict(color=bar_color, thickness=0.75),
            bgcolor=PALETTE["bg_page"],
            borderwidth=0,
            steps=[
                dict(range=[0, 50], color=PALETTE["red_light"]),
                dict(range=[50, 65], color="#FFF3E0"),
                dict(range=[65, 80], color="#E0F2F1"),
                dict(range=[80, 100], color=PALETTE["green_light"]),
            ],
            threshold=dict(
                line=dict(color=PALETTE["indigo"], width=3),
                thickness=0.85,
                value=score,
            ),
        ),
    ))

    fig.update_layout(
        **_base_layout(height=320, margin=dict(l=30, r=30, t=30, b=10)),
    )

    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_economics_waterfall(r: SiteResult) -> str:
    """Waterfall: gross cost -> ITC credit -> net cost -> lifetime savings -> net benefit."""
    net_benefit = r.lifetime_savings - r.net_cost - r.lifetime_om
    itc_credit = r.gross_cost - r.net_cost

    labels = [
        "Gross Cost",
        "ITC Credit (30%)",
        "Net Cost",
        f"Lifetime O&M ({DEFAULTS.system_life_years}yr)",
        "Lifetime Savings",
        "Net Benefit",
    ]
    values = [
        r.gross_cost,
        -itc_credit,
        0,  # subtotal
        -r.lifetime_om,
        r.lifetime_savings,
        0,  # total
    ]
    measures = ["relative", "relative", "total", "relative", "relative", "total"]

    colors_inc = PALETTE["coral"]
    colors_dec = PALETTE["sage"]
    colors_total = PALETTE["indigo"]

    fig = go.Figure(go.Waterfall(
        name="", orientation="v",
        measure=measures,
        x=labels,
        y=values,
        textposition="outside",
        text=[
            f"${r.gross_cost:,.0f}",
            f"-${itc_credit:,.0f}",
            f"${r.net_cost:,.0f}",
            f"-${r.lifetime_om:,.0f}",
            f"+${r.lifetime_savings:,.0f}",
            f"${net_benefit:,.0f}",
        ],
        textfont=dict(size=12, family="Inter, sans-serif"),
        connector=dict(line=dict(color=PALETTE["divider"], width=1)),
        increasing=dict(marker=dict(color=colors_dec)),  # savings = green
        decreasing=dict(marker=dict(color=colors_inc)),   # costs = coral
        totals=dict(marker=dict(color=colors_total)),
    ))

    fig.update_layout(**_base_layout(
        title=dict(text="<b>Economics Waterfall</b>", font=dict(size=18)),
        height=420,
        yaxis_title="USD ($)",
        showlegend=False,
    ))
    _annotate_subtitle(fig, "Follow the money from gross cost to net benefit over the system lifetime")
    _annotate_footnote(fig, "Source: TTS median $/W, IRA 30% ITC, PVWatts v8.5 production estimates")

    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_cumulative_cashflow(r: SiteResult) -> str:
    """Cumulative cashflow curve with payback point and do-nothing comparator."""
    years = list(range(len(r.cumulative_savings)))
    # Shift cumulative savings to start from -net_cost
    cum_net = [-r.net_cost + s for s in [0] + r.cumulative_savings[:-1]]
    # Actually: year 0 = -net_cost, year t = -net_cost + cumulative_savings[t-1]
    # Let's build it properly
    cum_cashflow = []
    running = -r.net_cost
    cum_cashflow.append(running)
    for s in r.annual_savings:
        running += s
        cum_cashflow.append(running)

    x_vals = list(range(len(cum_cashflow)))

    # Split into underwater (red) and above-water (green) segments
    fig = go.Figure()

    # Shade underwater region
    # Find crossover point
    cross_idx = None
    for i in range(1, len(cum_cashflow)):
        if cum_cashflow[i] >= 0 and cum_cashflow[i - 1] < 0:
            # Interpolate exact crossing
            frac = -cum_cashflow[i - 1] / (cum_cashflow[i] - cum_cashflow[i - 1])
            cross_x = i - 1 + frac
            cross_idx = i
            break

    # Underwater fill
    uw_x = x_vals[:cross_idx + 1] if cross_idx else x_vals
    uw_y = cum_cashflow[:cross_idx + 1] if cross_idx else cum_cashflow
    fig.add_trace(go.Scatter(
        x=uw_x, y=uw_y,
        fill="tozeroy", fillcolor="rgba(211, 47, 47, 0.10)",
        line=dict(color=PALETTE["coral"], width=3),
        name="Underwater",
        hovertemplate="Year %{x}<br>Cumulative: $%{y:,.0f}<extra></extra>",
    ))

    # Above-water fill
    if cross_idx and cross_idx < len(cum_cashflow):
        aw_x = x_vals[cross_idx - 1:]
        aw_y = cum_cashflow[cross_idx - 1:]
        fig.add_trace(go.Scatter(
            x=aw_x, y=aw_y,
            fill="tozeroy", fillcolor="rgba(76, 175, 120, 0.10)",
            line=dict(color=PALETTE["sage"], width=3),
            name="Net Positive",
            hovertemplate="Year %{x}<br>Cumulative: $%{y:,.0f}<extra></extra>",
        ))

    # Do-nothing line (cumulative grid cost)
    grid_costs_from_zero = [0] + list(r.cumulative_grid_cost)
    fig.add_trace(go.Scatter(
        x=list(range(len(grid_costs_from_zero))),
        y=grid_costs_from_zero,
        line=dict(color=PALETTE["warm_gray"], width=2, dash="dash"),
        name="Grid cost (no solar)",
        hovertemplate="Year %{x}<br>Grid cost: $%{y:,.0f}<extra></extra>",
    ))

    # Payback vertical line
    if not math.isnan(r.simple_payback_years):
        fig.add_vline(
            x=r.simple_payback_years,
            line=dict(color=PALETTE["amber"], width=2, dash="dot"),
        )
        fig.add_annotation(
            x=r.simple_payback_years, y=0,
            text=f"<b>Payback: {r.simple_payback_years:.1f} yr</b>",
            showarrow=True, arrowhead=2,
            ax=50, ay=-40,
            font=dict(size=13, color=PALETTE["amber"]),
            arrowcolor=PALETTE["amber"],
        )

    fig.update_layout(**_base_layout(
        title=dict(text="<b>Cumulative Cashflow</b>", font=dict(size=18)),
        height=420,
        xaxis_title="Year",
        yaxis_title="Cumulative ($)",
        yaxis_tickprefix="$",
        yaxis_tickformat=",",
        legend=dict(x=0.02, y=0.98, bgcolor="rgba(255,255,255,0.8)"),
    ))
    _annotate_subtitle(fig, "Time to breakeven — red zone is out-of-pocket, green zone is net positive")
    _annotate_footnote(fig, "Dashed line: cumulative electricity cost without solar. Source: PVWatts v8.5, EIA state rates")

    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_lcoe_vs_retail(r: SiteResult) -> str:
    """Horizontal bar comparison: LCOE vs retail rate with margin-of-safety label."""
    lcoe = r.lcoe
    retail = r.electricity_rate_used
    margin = retail - lcoe

    # Color logic: green if LCOE < retail, red if LCOE > retail
    lcoe_color = PALETTE["sage"] if lcoe < retail else PALETTE["coral"]
    retail_color = PALETTE["indigo"]

    fig = go.Figure()

    fig.add_trace(go.Bar(
        y=["LCOE (Solar)"],
        x=[lcoe],
        orientation="h",
        marker=dict(color=lcoe_color),
        text=[f"${lcoe:.3f}/kWh"],
        textposition="outside",
        textfont=dict(size=14),
        name="LCOE",
        hovertemplate="LCOE: $%{x:.4f}/kWh<extra></extra>",
    ))

    fig.add_trace(go.Bar(
        y=["Retail Rate (Grid)"],
        x=[retail],
        orientation="h",
        marker=dict(color=retail_color),
        text=[f"${retail:.3f}/kWh"],
        textposition="outside",
        textfont=dict(size=14),
        name="Retail",
        hovertemplate="Retail: $%{x:.4f}/kWh<extra></extra>",
    ))

    # Margin annotation
    if margin > 0:
        margin_text = f"Margin of safety: ${margin:.3f}/kWh ({margin/retail*100:.0f}%)"
        margin_color = PALETTE["sage"]
    else:
        margin_text = f"Grid is cheaper by ${abs(margin):.3f}/kWh"
        margin_color = PALETTE["coral"]

    fig.add_annotation(
        text=f"<b>{margin_text}</b>",
        xref="paper", yref="paper",
        x=0.5, y=-0.25, showarrow=False,
        font=dict(size=14, color=margin_color),
    )

    fig.update_layout(**_base_layout(
        title=dict(text="<b>LCOE vs Grid Retail Rate</b>", font=dict(size=18)),
        height=280,
        xaxis_title="$/kWh",
        xaxis_tickprefix="$",
        showlegend=False,
        barmode="group",
        margin=dict(l=140, r=80, t=80, b=80),
    ))
    _annotate_subtitle(fig, "Is solar cheaper than the grid? Lower LCOE = better deal")
    _annotate_footnote(fig, f"LCOE: undiscounted; Retail: {r.state} state average (EIA). Grid parity ratio: {r.grid_parity_ratio:.2f}")

    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_monthly_production(r: SiteResult) -> str:
    """Monthly production bar chart with temperature overlay from NASA data."""
    # Derive monthly production from pvwatts_ac_monthly_mean (stored in result's source row)
    # We have monthly mean AC in the result — use it if available
    monthly_kwh = r.year_production[0] / 12  # simple average fallback

    # The SiteResult doesn't store monthly breakdown directly.
    # Approximate using a sinusoidal seasonal pattern scaled to annual total
    # Peak in June/July, trough in December/January (northern hemisphere)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    # Sinusoidal approximation: peak at month 6 (July), trough at month 0 (Jan)
    # Amplitude ~35% of mean, shifted so sum matches annual
    month_indices = np.arange(12)
    seasonal = 1.0 + 0.35 * np.sin(2 * np.pi * (month_indices - 3) / 12)  # peak near June
    seasonal = seasonal / seasonal.sum() * r.year_production[0]

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Bar(
        x=months, y=seasonal,
        marker=dict(
            color=seasonal,
            colorscale=[[0, PALETTE["indigo_light"]], [1, PALETTE["amber"]]],
            showscale=False,
        ),
        name="Production (kWh)",
        hovertemplate="<b>%{x}</b><br>%{y:,.0f} kWh<extra></extra>",
    ), secondary_y=False)

    fig.update_layout(**_base_layout(
        title=dict(text="<b>Estimated Monthly Production</b>", font=dict(size=18)),
        height=380,
        showlegend=True,
        legend=dict(x=0.02, y=0.98),
    ))
    fig.update_yaxes(title_text="kWh", secondary_y=False)

    _annotate_subtitle(fig, "Seasonal pattern approximated from PVWatts annual total — expect more in summer, less in winter")
    _annotate_footnote(fig, "Source: PVWatts v8.5 annual estimate with sinusoidal seasonal decomposition")

    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_sensitivity_tornado(r: SiteResult, row: dict) -> str:
    """Tornado chart: NPV sensitivity to ±20% parameter changes."""
    tornado_data = sensitivity_tornado(row)

    params = [d["param"] for d in tornado_data]
    npv_low = [d["npv_low"] for d in tornado_data]
    npv_high = [d["npv_high"] for d in tornado_data]
    npv_base = tornado_data[0]["npv_base"]

    fig = go.Figure()

    # Low-side bars (extending left from base)
    fig.add_trace(go.Bar(
        y=params,
        x=[lo - npv_base for lo in npv_low],
        base=npv_base,
        orientation="h",
        marker=dict(color=PALETTE["coral"]),
        name="-20%",
        text=[f"${lo:,.0f}" for lo in npv_low],
        textposition="outside",
        textfont=dict(size=11),
        hovertemplate="%{y}<br>NPV at -20%: $%{text}<extra></extra>",
    ))

    # High-side bars (extending right from base)
    fig.add_trace(go.Bar(
        y=params,
        x=[hi - npv_base for hi in npv_high],
        base=npv_base,
        orientation="h",
        marker=dict(color=PALETTE["sage"]),
        name="+20%",
        text=[f"${hi:,.0f}" for hi in npv_high],
        textposition="outside",
        textfont=dict(size=11),
        hovertemplate="%{y}<br>NPV at +20%: $%{text}<extra></extra>",
    ))

    # Base NPV vertical line
    fig.add_vline(
        x=npv_base,
        line=dict(color=PALETTE["indigo"], width=2, dash="dash"),
        annotation_text=f"Base NPV: ${npv_base:,.0f}",
        annotation_position="top",
        annotation_font=dict(size=12, color=PALETTE["indigo"]),
    )

    fig.update_layout(**_base_layout(
        title=dict(text="<b>Sensitivity Analysis (Tornado)</b>", font=dict(size=18)),
        height=400,
        xaxis_title="NPV ($)",
        xaxis_tickprefix="$",
        xaxis_tickformat=",",
        showlegend=True,
        legend=dict(x=0.02, y=-0.15, orientation="h"),
        barmode="overlay",
        margin=dict(l=160, r=80, t=100, b=80),
    ))
    _annotate_subtitle(fig, "Which assumptions matter most? Sorted by impact magnitude. Longer bars = more sensitivity.")
    _annotate_footnote(fig, "Each parameter varied ±20% from base. Source: PVWatts v8.5, EIA rates, TTS benchmarks")

    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_peer_scatter(r: SiteResult, row: dict) -> str:
    """Scatter plot: this site vs Kaggle reference distribution."""
    # Use Kaggle reference stats to create a synthetic peer distribution
    kaggle_yield_mean = row.get("kaggle_mean_Avg_Annual_Production_kWh", 10850)
    kaggle_payback_mean = row.get("kaggle_mean_Payback_Period_Years", 9.78)

    # Generate synthetic peers for context
    np.random.seed(42)
    n_peers = 48  # Kaggle has 48 rows
    peer_yields = np.random.normal(kaggle_yield_mean / 5, 300, n_peers)  # per kW
    peer_paybacks = np.random.normal(kaggle_payback_mean, 2.5, n_peers)
    peer_paybacks = np.clip(peer_paybacks, 2, 20)

    fig = go.Figure()

    # Peer cloud
    fig.add_trace(go.Scatter(
        x=peer_yields, y=peer_paybacks,
        mode="markers",
        marker=dict(
            size=8, color=PALETTE["slate"], opacity=0.4,
            line=dict(width=1, color=PALETTE["divider"]),
        ),
        name="Kaggle Reference Sites",
        hovertemplate="Yield: %{x:,.0f} kWh/kW<br>Payback: %{y:.1f} yr<extra></extra>",
    ))

    # This site — large highlighted marker
    fig.add_trace(go.Scatter(
        x=[r.specific_yield],
        y=[r.simple_payback_years if not math.isnan(r.simple_payback_years) else 25],
        mode="markers+text",
        marker=dict(
            size=18, color=PALETTE["amber"],
            line=dict(width=3, color=PALETTE["indigo"]),
            symbol="star",
        ),
        text=[f"  {r.address_label}"],
        textposition="middle right",
        textfont=dict(size=13, color=PALETTE["indigo"]),
        name="This Site",
        hovertemplate=(
            f"<b>{r.address_label}</b><br>"
            f"Yield: {r.specific_yield:,.0f} kWh/kW<br>"
            f"Payback: {r.simple_payback_years:.1f} yr<br>"
            f"Score: {r.viability_score:.0f}/100"
            "<extra></extra>"
        ),
    ))

    fig.update_layout(**_base_layout(
        title=dict(text="<b>Peer Comparison</b>", font=dict(size=18)),
        height=420,
        xaxis_title="Specific Yield (kWh/kW)",
        yaxis_title="Simple Payback (years)",
        yaxis_autorange="reversed",  # lower payback = better = higher
        legend=dict(x=0.02, y=0.02),
    ))
    _annotate_subtitle(fig, "How does this site compare to the reference distribution? Stars = this site, dots = peers")
    _annotate_footnote(fig, "Peers: synthetic distribution based on Kaggle global reference dataset (N=48)")

    return fig.to_html(full_html=False, include_plotlyjs=False)


# ---------------------------------------------------------------------------
# KPI card helper
# ---------------------------------------------------------------------------
def _kpi_card(label: str, value: str, subtitle: str, color: str = PALETTE["indigo"]) -> str:
    """Generate HTML for a single KPI metric card."""
    return f"""
    <div style="
        background: {PALETTE['bg_card']};
        border-radius: 12px;
        padding: 20px 24px;
        border: 1px solid {PALETTE['divider']};
        text-align: center;
        min-width: 160px;
    ">
        <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px;
                     color: {PALETTE['text_secondary']}; margin-bottom: 6px;">
            {label}
        </div>
        <div style="font-size: 28px; font-weight: 700; color: {color}; line-height: 1.2;">
            {value}
        </div>
        <div style="font-size: 12px; color: {PALETTE['warm_gray']}; margin-top: 4px;">
            {subtitle}
        </div>
    </div>
    """


# ---------------------------------------------------------------------------
# Main report generator
# ---------------------------------------------------------------------------
def generate_report(
    results: list[SiteResult],
    output_path: str,
    rows: Optional[list[dict]] = None,
) -> str:
    """
    Generate a standalone HTML report for one or more scored sites.

    Parameters
    ----------
    results : list[SiteResult]
        Scored site results from score_site().
    output_path : str
        Path to write the HTML file.
    rows : list[dict], optional
        Original row dicts (needed for sensitivity tornado).

    Returns
    -------
    str
        The output file path.
    """
    # For now, generate a single-site report (first result)
    r = results[0]
    row = rows[0] if rows else {}

    # --- Determine viability color ---
    if r.viability_score >= 80:
        score_color = PALETTE["sage"]
    elif r.viability_score >= 65:
        score_color = PALETTE["teal"]
    elif r.viability_score >= 50:
        score_color = PALETTE["amber_warn"]
    else:
        score_color = PALETTE["coral"]

    # --- Build chart HTML ---
    hero_html = chart_hero_gauge(r)
    waterfall_html = chart_economics_waterfall(r)
    cashflow_html = chart_cumulative_cashflow(r)
    lcoe_html = chart_lcoe_vs_retail(r)
    monthly_html = chart_monthly_production(r)
    tornado_html = chart_sensitivity_tornado(r, row)
    peer_html = chart_peer_scatter(r, row)

    # --- KPI cards ---
    payback_color = PALETTE["sage"] if r.simple_payback_years < 7 else (
        PALETTE["teal"] if r.simple_payback_years < 10 else PALETTE["coral"]
    ) if not math.isnan(r.simple_payback_years) else PALETTE["coral"]

    npv_color = PALETTE["sage"] if r.npv > 0 else PALETTE["coral"]
    irr_display = f"{r.irr * 100:.1f}%" if r.irr is not None else "N/A"
    irr_color = PALETTE["sage"] if r.irr and r.irr > 0.10 else PALETTE["slate"]

    kpi_cards = f"""
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
                gap: 16px; margin: 24px 0;">
        {_kpi_card("Net Cost", f"${r.net_cost:,.0f}", f"After {int(DEFAULTS.federal_itc*100)}% ITC", PALETTE['indigo'])}
        {_kpi_card("LCOE", f"${r.lcoe:.3f}", "per kWh", score_color)}
        {_kpi_card("Payback", f"{r.simple_payback_years:.1f} yr" if not math.isnan(r.simple_payback_years) else "N/A", "simple payback", payback_color)}
        {_kpi_card("NPV", f"${r.npv:,.0f}", f"{DEFAULTS.system_life_years}-year horizon", npv_color)}
        {_kpi_card("IRR", irr_display, "internal rate of return", irr_color)}
        {_kpi_card("CO2 Avoided", f"{r.annual_co2_avoided_tons:.1f} t/yr", "metric tons", PALETTE['teal'])}
    </div>
    """

    # --- Sub-score breakdown ---
    subscore_html = f"""
    <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0;">
        <div style="text-align: center; padding: 16px; background: {PALETTE['bg_card']};
                     border-radius: 10px; border: 1px solid {PALETTE['divider']};">
            <div style="font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
                         color: {PALETTE['text_secondary']};">Resource (25%)</div>
            <div style="font-size: 22px; font-weight: 700; color: {PALETTE['amber']}; margin: 4px 0;">
                {r.resource_score:.0%}
            </div>
            <div style="font-size: 11px; color: {PALETTE['warm_gray']};">
                {r.specific_yield:,.0f} kWh/kW
            </div>
        </div>
        <div style="text-align: center; padding: 16px; background: {PALETTE['bg_card']};
                     border-radius: 10px; border: 1px solid {PALETTE['divider']};">
            <div style="font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
                         color: {PALETTE['text_secondary']};">Economics (50%)</div>
            <div style="font-size: 22px; font-weight: 700; color: {PALETTE['sage']}; margin: 4px 0;">
                {r.economics_score:.0%}
            </div>
            <div style="font-size: 11px; color: {PALETTE['warm_gray']};">
                Payback + LCOE + NPV
            </div>
        </div>
        <div style="text-align: center; padding: 16px; background: {PALETTE['bg_card']};
                     border-radius: 10px; border: 1px solid {PALETTE['divider']};">
            <div style="font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
                         color: {PALETTE['text_secondary']};">Site Fit (15%)</div>
            <div style="font-size: 22px; font-weight: 700; color: {PALETTE['teal']}; margin: 4px 0;">
                {r.site_fit_score:.0%}
            </div>
            <div style="font-size: 11px; color: {PALETTE['warm_gray']};">
                Tilt &Delta;{r.tilt_deviation:.0f}&deg; Az &Delta;{r.azimuth_deviation:.0f}&deg;
            </div>
        </div>
        <div style="text-align: center; padding: 16px; background: {PALETTE['bg_card']};
                     border-radius: 10px; border: 1px solid {PALETTE['divider']};">
            <div style="font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
                         color: {PALETTE['text_secondary']};">Market (10%)</div>
            <div style="font-size: 22px; font-weight: 700; color: {PALETTE['indigo_light']}; margin: 4px 0;">
                {r.policy_score:.0%}
            </div>
            <div style="font-size: 11px; color: {PALETTE['warm_gray']};">
                Local adoption signal
            </div>
        </div>
    </div>
    """

    # --- Assumptions table ---
    a = r.assumptions_used
    assumptions_rows = ""
    for key, val in a.items():
        if key.startswith("weight_"):
            continue
        display_val = f"${val}" if "price" in key or "cost" in key or "override" in str(key) else str(val)
        assumptions_rows += f"""
        <tr>
            <td style="padding: 6px 12px; border-bottom: 1px solid {PALETTE['divider']}; font-size: 13px;">
                <code>{key}</code>
            </td>
            <td style="padding: 6px 12px; border-bottom: 1px solid {PALETTE['divider']}; font-size: 13px;
                        text-align: right;">
                {display_val}
            </td>
        </tr>
        """

    # --- Full HTML ---
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Solar Site Analysis — {r.address_label}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-2.35.0.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Inter', 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            background: {PALETTE['bg_page']};
            color: {PALETTE['text_primary']};
            line-height: 1.6;
        }}
        .sticky-header {{
            position: sticky;
            top: 0;
            z-index: 100;
            background: linear-gradient(135deg, {PALETTE['indigo']} 0%, {PALETTE['indigo_light']} 100%);
            color: white;
            padding: 16px 40px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 2px 12px rgba(0,0,0,0.15);
        }}
        .header-left h1 {{
            font-size: 20px;
            font-weight: 600;
            letter-spacing: -0.3px;
        }}
        .header-left .subtitle {{
            font-size: 13px;
            opacity: 0.8;
            margin-top: 2px;
        }}
        .header-score {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .score-badge {{
            background: {score_color};
            color: white;
            font-size: 28px;
            font-weight: 700;
            width: 60px;
            height: 60px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 2px 8px rgba(0,0,0,0.2);
        }}
        .score-label {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
            opacity: 0.9;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 32px 40px;
        }}
        .section {{
            margin-bottom: 48px;
        }}
        .section-label {{
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            color: {PALETTE['text_secondary']};
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 2px solid {PALETTE['divider']};
        }}
        .chart-card {{
            background: {PALETTE['bg_card']};
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
            border: 1px solid {PALETTE['divider']};
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }}
        .grid-2 {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 24px;
        }}
        .assumptions-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        .assumptions-table th {{
            text-align: left;
            padding: 8px 12px;
            background: {PALETTE['bg_page']};
            border-bottom: 2px solid {PALETTE['divider']};
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: {PALETTE['text_secondary']};
        }}
        .footer {{
            text-align: center;
            padding: 32px 40px;
            color: {PALETTE['warm_gray']};
            font-size: 12px;
            border-top: 1px solid {PALETTE['divider']};
        }}
        @media (max-width: 768px) {{
            .grid-2 {{ grid-template-columns: 1fr; }}
            .sticky-header {{ padding: 12px 20px; }}
            .container {{ padding: 20px; }}
        }}
    </style>
</head>
<body>

<!-- Sticky Header -->
<div class="sticky-header">
    <div class="header-left">
        <h1>{r.address_label}</h1>
        <div class="subtitle">{r.state} &middot; {r.specific_yield:,.0f} kWh/kW &middot; {r.capacity_factor:.1f}% CF</div>
    </div>
    <div class="header-score">
        <div>
            <div class="score-label">Viability Score</div>
        </div>
        <div class="score-badge">{r.viability_score:.0f}</div>
    </div>
</div>

<div class="container">

    <!-- Hero Gauge + KPIs -->
    <div class="section">
        <div class="section-label">Overview</div>
        <div class="grid-2">
            <div class="chart-card">
                {hero_html}
            </div>
            <div>
                {kpi_cards}
            </div>
        </div>
        {subscore_html}
    </div>

    <!-- Economics Waterfall -->
    <div class="section">
        <div class="section-label">Financial Analysis</div>
        <div class="chart-card">
            {waterfall_html}
        </div>
    </div>

    <!-- Cashflow + LCOE side by side -->
    <div class="section">
        <div class="grid-2">
            <div class="chart-card">
                {cashflow_html}
            </div>
            <div class="chart-card">
                {lcoe_html}
            </div>
        </div>
    </div>

    <!-- Sensitivity Tornado (prominent) -->
    <div class="section">
        <div class="section-label">Decision Support</div>
        <div class="chart-card">
            {tornado_html}
        </div>
    </div>

    <!-- Monthly Production + Peer Comparison -->
    <div class="section">
        <div class="grid-2">
            <div class="chart-card">
                {monthly_html}
            </div>
            <div class="chart-card">
                {peer_html}
            </div>
        </div>
    </div>

    <!-- Assumptions Reference -->
    <div class="section">
        <div class="section-label">Assumptions & Methodology</div>
        <div class="chart-card">
            <p style="font-size: 13px; color: {PALETTE['text_secondary']}; margin-bottom: 16px;">
                All parameters below are overridable via the <code>Assumptions</code> dataclass.
                Electricity rate source: <b>{r.rate_source}</b> (${r.electricity_rate_used:.3f}/kWh).
            </p>
            <table class="assumptions-table">
                <thead>
                    <tr>
                        <th>Parameter</th>
                        <th style="text-align: right;">Value</th>
                    </tr>
                </thead>
                <tbody>
                    {assumptions_rows}
                </tbody>
            </table>
        </div>
    </div>

</div>

<div class="footer">
    Solar Site Analysis Report &middot; Generated by solar_viz.py &middot;
    Data: NREL PVWatts v8.5, NASA POWER, Berkeley Lab TTS, Kaggle Reference
</div>

</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Generate Solar Analysis HTML Report")
    parser.add_argument("input", help="JSON file from score_site() or CSV")
    parser.add_argument("--output", "-o", default="solar_report.html")
    args = parser.parse_args()

    input_path = Path(args.input)

    if input_path.suffix == ".json":
        with open(input_path) as f:
            data = json.load(f)
        # Re-score from the raw row to get a proper SiteResult
        result = score_site(data)
        generate_report([result], args.output, rows=[data])
    elif input_path.suffix == ".csv":
        import pandas as pd
        df = pd.read_csv(input_path)
        row = df.iloc[0].to_dict()
        result = score_site(row)
        generate_report([result], args.output, rows=[row])
    else:
        print(f"Unsupported format: {input_path.suffix}", file=sys.stderr)
        sys.exit(1)

    print(f"Report written to: {args.output}")


if __name__ == "__main__":
    main()
