"""
solar_fetch.py — Orchestrator: location string → full solar economics quote.

Ties together geocoding, PVWatts, URDB rate lookup, NEM export policy,
and the existing score_site() economics engine into a single pipeline.

Usage:
    from solar_fetch import quote_from_location
    quote = quote_from_location("San Jose, CA", monthly_kwh=650)

CLI:
    python -m solar_fetch "1600 Amphitheatre Pkwy, Mountain View, CA" --monthly-kwh 650
    python -m solar_fetch 95192 --fast
    python -m solar_fetch "Boulder, CO" --system-kw 8 --install-date 2026-06-01
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional

import numpy as np

from dotenv import load_dotenv

load_dotenv()

from solar_geocode import geocode, GeocodeResult, GeocodeError
from solar_pvwatts import fetch_pvwatts, PVWattsResult, PVWattsError
from solar_urdb import get_rate, RateResult, URDBError
from solar_nem import get_export_value, ExportValueResult
from solar_economics import score_site, Assumptions, DEFAULTS, SiteResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# QuoteResult — wraps everything for full provenance
# ---------------------------------------------------------------------------
@dataclass
class QuoteResult:
    """Full output of quote_from_location(). Contains all sub-results for provenance."""

    site_result: SiteResult
    geocode_result: GeocodeResult
    pvwatts_result: PVWattsResult
    rate_result: RateResult
    export_result: ExportValueResult

    # Metadata
    system_kw_used: float
    system_sizing_method: str  # "user_specified" | "bill_sized" | "default"
    confidence_level: str      # "high" | "medium" | "low"
    confidence_reasons: list[str]

    def to_dict(self) -> dict:
        """Serialize for JSON output."""
        return {
            "site": self.site_result.to_dict(),
            "geocode": asdict(self.geocode_result),
            "pvwatts": self.pvwatts_result.to_dict(),
            "rate": {
                "flat_rate": self.rate_result.flat_rate,
                "fixed_monthly_charge": self.rate_result.fixed_monthly_charge,
                "utility_name": self.rate_result.utility_name,
                "rate_name": self.rate_result.rate_name,
                "rate_uri": self.rate_result.rate_uri,
                "source": self.rate_result.source,
                "is_tou": self.rate_result.is_tou,
                "is_tiered": self.rate_result.is_tiered,
                "effective_date": str(self.rate_result.effective_date) if self.rate_result.effective_date else None,
            },
            "export": asdict(self.export_result),
            "meta": {
                "system_kw_used": self.system_kw_used,
                "system_sizing_method": self.system_sizing_method,
                "confidence_level": self.confidence_level,
                "confidence_reasons": self.confidence_reasons,
            },
        }


# ---------------------------------------------------------------------------
# Confidence assessment
# ---------------------------------------------------------------------------
def _assess_confidence(
    geo: GeocodeResult,
    rate: RateResult,
    pvw: PVWattsResult,
) -> tuple[str, list[str]]:
    """
    Determine overall confidence based on data source quality.

    High: geocode is high + URDB full rate + PVWatts station <10km
    Medium: any single fallback
    Low: multiple fallbacks stacked
    """
    reasons = []

    # Geocode quality
    if geo.confidence == "high":
        reasons.append("Geocode: high confidence (Census match)")
    elif geo.confidence == "medium":
        reasons.append("Geocode: medium confidence (ZIP centroid or multiple matches)")
    else:
        reasons.append("Geocode: low confidence (fallback)")

    # Rate source quality
    if rate.source == "urdb_full":
        reasons.append(f"Rate: URDB full parse ({rate.rate_name})")
    elif rate.source == "urdb_tiered_avg":
        reasons.append(f"Rate: URDB tiered average ({rate.rate_name})")
    elif rate.source == "bundled_tou":
        # Curated current-year TOU schedule that the staleness guard
        # substitutes for stale/expired URDB rates. This is a deliberate,
        # high-quality replacement — not a degraded fallback.
        reasons.append(f"Rate: bundled current TOU schedule ({rate.rate_name})")
    elif rate.source == "nrel_v3":
        reasons.append("Rate: NREL v3 simple lookup (less precise)")
    else:
        reasons.append(f"Rate: fallback ({rate.source})")

    # PVWatts station proximity
    dist_km = pvw.pvwatts_station_distance_m / 1000.0
    if dist_km < 10:
        reasons.append(f"Solar data: nearby station ({dist_km:.1f} km)")
    elif dist_km < 50:
        reasons.append(f"Solar data: moderate distance ({dist_km:.1f} km)")
    else:
        reasons.append(f"Solar data: distant station ({dist_km:.1f} km)")

    # Score it
    fallback_count = 0
    if geo.confidence != "high":
        fallback_count += 1
    if rate.source not in ("urdb_full", "urdb_tiered_avg", "bundled_tou"):
        fallback_count += 1
    if dist_km >= 50:
        fallback_count += 1

    if fallback_count == 0:
        level = "high"
    elif fallback_count == 1:
        level = "medium"
    else:
        level = "low"

    return level, reasons


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------
def quote_from_location(
    location: str,
    monthly_kwh: Optional[float] = None,
    system_kw: Optional[float] = None,
    detailed: bool = True,
    install_date: Optional[date] = None,
    assumptions: Optional[Assumptions] = None,
) -> QuoteResult:
    """
    End-to-end solar quote from a plain-text location string.

    Pipeline:
        1. geocode(location) → lat, lon, state, zip
        2. fetch_pvwatts(lat, lon) → production estimates
        3. get_rate(lat, lon, state) → utility rate
        4. get_export_value(state, rate) → NEM export compensation
        5. score_site(row, assumptions) → SiteResult

    Parameters
    ----------
    location : str
        Address, city+state, or 5-digit ZIP code.
    monthly_kwh : float, optional
        Average monthly electricity bill in kWh.  If provided, the system
        is auto-sized so annual production ≈ monthly_kwh × 12.
    system_kw : float, optional
        Explicit system size in kW DC.  Overrides monthly_kwh sizing.
    detailed : bool
        If True (default), use full URDB rate lookup.
        If False, use the simpler NREL v3 endpoint.
    install_date : date, optional
        Assumed installation date.  Affects NEM policy (e.g., CA NEM 3.0
        cutoff is 2023-04-15).  Defaults to today.
    assumptions : Assumptions, optional
        Override any economic parameters.  See solar_economics.Assumptions.

    Returns
    -------
    QuoteResult
        Complete analysis with full provenance from every data source.
    """
    if assumptions is None:
        assumptions = Assumptions()

    install_dt = install_date or date.today()

    # --- Step 1: Geocode ---
    logger.info("Geocoding: %s", location)
    geo = geocode(location)
    logger.info("Resolved: %s → (%.4f, %.4f) %s [%s]",
                geo.resolved_address, geo.lat, geo.lon, geo.state, geo.source)

    # --- Step 2: Determine system size ---
    sizing_method = "default"
    size_kw = 5.0  # default

    if system_kw is not None:
        size_kw = system_kw
        sizing_method = "user_specified"
    elif monthly_kwh is not None:
        # Probe call: get specific yield at 5 kW to estimate kWh/kW
        logger.info("Sizing system from monthly bill: %.0f kWh/month", monthly_kwh)
        probe = fetch_pvwatts(geo.lat, geo.lon, system_kw=5.0)
        specific_yield = probe.pvwatts_ac_annual_kwh / 5.0  # kWh per kW
        if specific_yield > 0:
            target_annual = monthly_kwh * 12
            size_kw = round(target_annual / specific_yield * 2) / 2  # nearest 0.5 kW
            size_kw = max(size_kw, 1.0)  # minimum 1 kW
            logger.info("Auto-sized to %.1f kW (%.0f kWh/kW yield)",
                        size_kw, specific_yield)
        sizing_method = "bill_sized"

    # --- Step 3: PVWatts production ---
    logger.info("Fetching PVWatts for (%.4f, %.4f) at %.1f kW", geo.lat, geo.lon, size_kw)
    pvw = fetch_pvwatts(geo.lat, geo.lon, system_kw=size_kw)

    # --- Step 4: Utility rate ---
    logger.info("Looking up utility rate...")
    if detailed:
        rate = get_rate(geo.lat, geo.lon, state=geo.state)
    else:
        from solar_urdb import fetch_rate_fast
        try:
            rate = fetch_rate_fast(geo.lat, geo.lon)
        except Exception as e:
            logger.warning("fetch_rate_fast failed: %s; using get_rate fallback", e)
            rate = get_rate(geo.lat, geo.lon, state=geo.state)

    logger.info("Rate: $%.4f/kWh from %s (%s)", rate.flat_rate, rate.utility_name, rate.source)

    # --- Step 5: NEM export value ---
    export = get_export_value(
        state=geo.state,
        retail_rate=rate.flat_rate,
        install_date=install_dt,
        hourly_production=None,  # TODO: derive from PVWatts monthly if TOU
        hourly_rates=rate.hourly_rates,
    )
    logger.info("Export policy: %s → $%.4f/kWh avg", export.policy_name, export.avg_export_rate)

    # --- Step 6: Build row dict for score_site ---
    row = pvw.to_dict()
    row.update({
        "site_id": f"quote_{geo.zip_code or 'unknown'}",
        "address_label": geo.resolved_address,
        "lat": geo.lat,
        "lon": geo.lon,
        "system_capacity_kw": size_kw,
        "azimuth": 180.0,
        "tilt": abs(geo.lat),  # optimal: tilt ≈ latitude
        "losses": 14.0,
        "state": geo.state,
        "zip_code": geo.zip_code,
        "customer_segment": "RES",
        "tts_recent_sample_size": 1000,  # conservative default
        "tts_median_price_per_watt": assumptions.default_price_per_watt,
    })

    # Wire the real rate and export ratio into assumptions
    nem_ratio = (export.avg_export_rate / rate.flat_rate) if rate.flat_rate > 0 else 0.75
    nem_ratio = min(max(nem_ratio, 0.0), 1.5)  # clamp to reasonable range

    scored_assumptions = Assumptions(
        **{
            **{k: getattr(assumptions, k) for k in assumptions.__dataclass_fields__},
            "electricity_price_override": rate.flat_rate,
            "nem_export_ratio": nem_ratio,
        }
    )

    # Deduct fixed monthly charge from savings by adjusting effective rate
    # (Fixed charges reduce the net benefit but are handled in the report display)

    # --- Step 7: Score ---
    logger.info("Scoring site...")
    result = score_site(row, scored_assumptions)

    # --- Step 8: Assess confidence ---
    confidence_level, confidence_reasons = _assess_confidence(geo, rate, pvw)

    return QuoteResult(
        site_result=result,
        geocode_result=geo,
        pvwatts_result=pvw,
        rate_result=rate,
        export_result=export,
        system_kw_used=size_kw,
        system_sizing_method=sizing_method,
        confidence_level=confidence_level,
        confidence_reasons=confidence_reasons,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_summary(q: QuoteResult):
    """Pretty-print a quote summary to the terminal."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
        from rich.text import Text

        console = Console()
        r = q.site_result

        # Header
        confidence_color = {"high": "green", "medium": "yellow", "low": "red"}[q.confidence_level]
        header = Text()
        header.append(f"\n  {r.address_label}", style="bold white")
        header.append(f"  [{q.confidence_level.upper()} CONFIDENCE]", style=f"bold {confidence_color}")
        console.print(Panel(header, title="Solar Analysis", border_style="blue"))

        # Score
        score_color = "green" if r.viability_score >= 80 else "yellow" if r.viability_score >= 65 else "red"
        console.print(f"\n  [bold {score_color}]{r.viability_score:.0f}/100[/] — {r.viability_label}\n")

        # Main metrics table
        table = Table(show_header=True, header_style="bold cyan", show_edge=False, pad_edge=False)
        table.add_column("Metric", style="dim", width=24)
        table.add_column("Value", justify="right")

        table.add_row("System Size", f"{q.system_kw_used:.1f} kW ({q.system_sizing_method})")
        table.add_row("Annual Production", f"{r.specific_yield * q.system_kw_used:,.0f} kWh")
        table.add_row("Specific Yield", f"{r.specific_yield:,.0f} kWh/kW")
        table.add_row("", "")
        table.add_row("Gross Cost", f"${r.gross_cost:,.0f}")
        table.add_row("Net Cost (after ITC)", f"${r.net_cost:,.0f}")
        table.add_row("LCOE", f"${r.lcoe:.3f}/kWh")
        table.add_row("Retail Rate", f"${r.electricity_rate_used:.3f}/kWh ({q.rate_result.source})")
        table.add_row("Grid Parity Ratio", f"{r.grid_parity_ratio:.3f}")
        table.add_row("", "")
        payback_str = f"{r.simple_payback_years:.1f} years" if not np.isnan(r.simple_payback_years) else "N/A"
        table.add_row("Simple Payback", payback_str)
        table.add_row("NPV (25-yr)", f"${r.npv:,.0f}")
        irr_str = f"{r.irr * 100:.1f}%" if r.irr else "N/A"
        table.add_row("IRR", irr_str)
        table.add_row("Year-1 Savings", f"${r.year_1_savings:,.0f}")
        table.add_row("Lifetime Savings", f"${r.lifetime_savings:,.0f}")
        table.add_row("CO₂ Avoided", f"{r.annual_co2_avoided_tons:.1f} tons/yr")
        table.add_row("", "")
        table.add_row("Utility", q.rate_result.utility_name)
        table.add_row("Rate Schedule", q.rate_result.rate_name)
        table.add_row("Export Policy", q.export_result.policy_name)

        console.print(table)
        console.print()

        # Confidence reasons
        for reason in q.confidence_reasons:
            console.print(f"  [dim]• {reason}[/]")
        console.print()

    except ImportError:
        # Fallback without rich
        r = q.site_result
        print(f"\n{'='*60}")
        print(f"  {r.address_label}")
        print(f"  Score: {r.viability_score:.0f}/100 — {r.viability_label}")
        print(f"{'='*60}")
        print(f"  System:  {q.system_kw_used:.1f} kW | {r.specific_yield:,.0f} kWh/kW")
        print(f"  Cost:    ${r.net_cost:,.0f} (after ITC) | LCOE ${r.lcoe:.3f}/kWh")
        payback_str = f"{r.simple_payback_years:.1f} yr" if not np.isnan(r.simple_payback_years) else "N/A"
        print(f"  Payback: {payback_str} | NPV ${r.npv:,.0f}")
        print(f"  Rate:    ${r.electricity_rate_used:.3f}/kWh from {q.rate_result.utility_name}")
        print(f"  Export:  {q.export_result.policy_name} → ${q.export_result.avg_export_rate:.3f}/kWh")
        print(f"  Confidence: {q.confidence_level}")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Solar viability quote from a location string",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python -m solar_fetch "1600 Amphitheatre Pkwy, Mountain View, CA"
  python -m solar_fetch 95192 --monthly-kwh 650
  python -m solar_fetch "Boulder, CO" --system-kw 8 --install-date 2026-06-01
  python -m solar_fetch 95112 --fast --output report.html
""",
    )
    parser.add_argument("location", help="Address, city+state, or 5-digit ZIP")
    parser.add_argument("--monthly-kwh", type=float, help="Average monthly electricity usage (kWh)")
    parser.add_argument("--system-kw", type=float, help="System size in kW DC")
    parser.add_argument("--fast", action="store_true", help="Use NREL v3 rate lookup (faster, less precise)")
    parser.add_argument("--install-date", type=str, help="Installation date (YYYY-MM-DD)")
    parser.add_argument("--output", "-o", type=str, help="Write HTML report to this path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    install_dt = None
    if args.install_date:
        from datetime import datetime
        install_dt = datetime.strptime(args.install_date, "%Y-%m-%d").date()

    try:
        quote = quote_from_location(
            location=args.location,
            monthly_kwh=args.monthly_kwh,
            system_kw=args.system_kw,
            detailed=not args.fast,
            install_date=install_dt,
        )
    except (GeocodeError, PVWattsError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    _print_summary(quote)

    if args.output:
        from solar_viz import generate_report
        row = quote.pvwatts_result.to_dict()
        row.update({
            "site_id": quote.site_result.site_id,
            "address_label": quote.geocode_result.resolved_address,
            "lat": quote.geocode_result.lat,
            "lon": quote.geocode_result.lon,
            "system_capacity_kw": quote.system_kw_used,
            "state": quote.geocode_result.state,
            "zip_code": quote.geocode_result.zip_code,
            "azimuth": 180.0,
            "tilt": abs(quote.geocode_result.lat),
            "losses": 14.0,
            "tts_recent_sample_size": 1000,
            "tts_median_price_per_watt": DEFAULTS.default_price_per_watt,
        })
        generate_report(
            [quote.site_result],
            args.output,
            rows=[row],
            quote=quote,
        )
        print(f"Report written to: {args.output}")


if __name__ == "__main__":
    main()
