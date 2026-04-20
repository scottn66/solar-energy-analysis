"""
solar_etl.py — ETL layer between external APIs and the DuckDB warehouse.

This is the ONLY module that needs API keys. Teammates should point their
analysis code at the warehouse (via solar_warehouse.get_conn()), not the APIs.

Wraps the existing fetchers (solar_geocode, solar_pvwatts, solar_urdb,
solar_eia) and writes their results into raw_* staging tables for
auditability and historical tracking.

Usage:
    # Run a full quote and persist to warehouse
    python3 solar_etl.py --location "San Jose, CA" --monthly-kwh 650

    # Capture every step (geocode, pvwatts, urdb, eia) for a location
    python3 solar_etl.py --location 94061

    # Show warehouse row counts
    python3 solar_etl.py --status

Programmatic:
    from solar_etl import etl_quote
    quote = etl_quote("San Jose, CA", monthly_kwh=650)
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from solar_warehouse import get_conn, ensure_schema, table_counts
from solar_geocode import geocode, GeocodeResult
from solar_pvwatts import fetch_pvwatts, PVWattsResult
from solar_urdb import fetch_rate, get_rate, RateResult
from solar_eia import get_state_rate
from solar_nem import get_export_value
from solar_fetch import quote_from_location, QuoteResult
from solar_economics import score_site, Assumptions

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """UTC timestamp for staging writes (tz-aware)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers: write staging rows from fetcher results
# ---------------------------------------------------------------------------

def _stage_geocode(conn, query: str, result: GeocodeResult) -> None:
    """Insert a geocode result into raw_geocode."""
    conn.execute("""
        INSERT INTO raw_geocode
            (fetched_at, query, source, lat, lon, resolved_address,
             state, zip_code, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        _now(), query, result.source, result.lat, result.lon,
        result.resolved_address, result.state, result.zip_code,
        result.confidence,
    ])


def _stage_pvwatts(
    conn, lat: float, lon: float, system_kw: float,
    result: PVWattsResult,
    tilt: float | None = None, azimuth: float = 180.0, losses: float = 14.0,
) -> None:
    """Insert a PVWatts result into raw_pvwatts."""
    conn.execute("""
        INSERT INTO raw_pvwatts
            (fetched_at, lat, lon, system_kw, tilt, azimuth, losses,
             array_type, module_type, ac_annual_kwh, solrad_annual,
             capacity_factor, station_lat, station_lon, station_distance_m,
             pvwatts_version, response_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        _now(), lat, lon, system_kw, tilt, azimuth, losses,
        1, 0,  # array_type, module_type (defaults)
        result.pvwatts_ac_annual_kwh, result.pvwatts_solrad_annual,
        result.pvwatts_capacity_factor, result.pvwatts_station_lat,
        result.pvwatts_station_lon, result.pvwatts_station_distance_m,
        result.pvwatts_version, json.dumps(result.to_dict(), default=str),
    ])


def _stage_urdb(conn, lat: float, lon: float, result: RateResult) -> None:
    """Insert a utility rate lookup into raw_urdb."""
    conn.execute("""
        INSERT INTO raw_urdb
            (fetched_at, lat, lon, sector, utility_name, rate_name,
             flat_rate, fixed_monthly_charge, is_tou, is_tiered,
             effective_date, source, rate_uri, response_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        _now(), lat, lon, "Residential",
        result.utility_name, result.rate_name, result.flat_rate,
        result.fixed_monthly_charge, result.is_tou, result.is_tiered,
        result.effective_date, result.source, result.rate_uri,
        json.dumps(result.raw, default=str) if result.raw else None,
    ])


def _stage_eia(conn, state: str, rate: float, period: str, source: str) -> None:
    """Insert an EIA state rate lookup into raw_eia."""
    conn.execute("""
        INSERT INTO raw_eia
            (fetched_at, state, period, rate_dollars_per_kwh, source)
        VALUES (?, ?, ?, ?, ?)
    """, [_now(), state, period, rate, source])


def _stage_quote(conn, location_query: str, quote: QuoteResult) -> int:
    """Insert a full quote result into raw_quote. Returns the inserted row's id."""
    r = quote.site_result
    conn.execute("""
        INSERT INTO raw_quote
            (fetched_at, location_query, lat, lon, state, zip_code,
             system_kw, system_sizing_method,
             viability_score, viability_label, payback_years, npv_25yr,
             irr, lcoe, co2_avoided_tons,
             utility_name, rate_name, rate_source, electricity_rate_used,
             export_policy, export_rate,
             confidence_level, confidence_reasons, full_result_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?)
    """, [
        _now(), location_query,
        quote.geocode_result.lat, quote.geocode_result.lon,
        quote.geocode_result.state, quote.geocode_result.zip_code,
        quote.system_kw_used, quote.system_sizing_method,
        r.viability_score, r.viability_label, r.simple_payback_years,
        r.npv, r.irr, r.lcoe, r.annual_co2_avoided_tons,
        quote.rate_result.utility_name, quote.rate_result.rate_name,
        quote.rate_result.source, r.electricity_rate_used,
        quote.export_result.policy_name, quote.export_result.avg_export_rate,
        quote.confidence_level, json.dumps(quote.confidence_reasons),
        json.dumps(quote.to_dict(), default=str),
    ])
    row_id = conn.execute("SELECT currval('seq_staging_id')").fetchone()[0]
    return int(row_id)


# ---------------------------------------------------------------------------
# Public ETL entry points
# ---------------------------------------------------------------------------

def etl_quote(
    location: str,
    monthly_kwh: Optional[float] = None,
    system_kw: Optional[float] = None,
    install_date: Optional[date] = None,
    assumptions: Optional[Assumptions] = None,
    db_path: Optional[Path] = None,
) -> QuoteResult:
    """
    Run the full solar quote pipeline and persist all intermediate results
    to the DuckDB warehouse.

    This wraps ``solar_fetch.quote_from_location()`` with staging inserts.
    Every API call's output lands in the appropriate raw_* table.

    Parameters
    ----------
    location : str
        Address, city+state, or ZIP.
    monthly_kwh : float, optional
        Average monthly kWh for bill-sized sizing.
    system_kw : float, optional
        Explicit system size.
    install_date : date, optional
        Installation date (affects NEM policy).
    assumptions : Assumptions, optional
        Economic parameter overrides.
    db_path : Path, optional
        Custom warehouse path (for tests).

    Returns
    -------
    QuoteResult
        Same as ``quote_from_location()`` — plus warehouse has been updated.
    """
    conn = get_conn(db_path)
    ensure_schema(conn)

    try:
        # Run the full pipeline (this uses requests_cache, so repeat calls are cheap)
        quote = quote_from_location(
            location=location,
            monthly_kwh=monthly_kwh,
            system_kw=system_kw,
            install_date=install_date,
            assumptions=assumptions,
        )

        # Stage each step's output.  We re-capture from the QuoteResult
        # rather than re-calling the fetchers.
        _stage_geocode(conn, location, quote.geocode_result)
        _stage_pvwatts(
            conn,
            quote.geocode_result.lat, quote.geocode_result.lon,
            quote.system_kw_used, quote.pvwatts_result,
            tilt=abs(quote.geocode_result.lat),
        )
        _stage_urdb(
            conn, quote.geocode_result.lat, quote.geocode_result.lon,
            quote.rate_result,
        )
        # If rate came from EIA, also log a raw_eia row for rate-history analysis
        if quote.rate_result.source in ("eia_live", "eia_bundled_2025",
                                         "eia_staleness_override"):
            _stage_eia(
                conn, quote.geocode_result.state,
                quote.rate_result.flat_rate,
                str(date.today().year),
                quote.rate_result.source,
            )

        quote_id = _stage_quote(conn, location, quote)
        logger.info("ETL complete: raw_quote id=%d", quote_id)

        return quote

    finally:
        conn.close()


def etl_status(db_path: Optional[Path] = None) -> dict[str, int]:
    """Return row counts for every warehouse table (sanity check)."""
    conn = get_conn(db_path)
    ensure_schema(conn)
    try:
        return table_counts(conn)
    finally:
        conn.close()


def build_marts(db_path: Optional[Path] = None) -> dict[str, int]:
    """
    Rebuild the dim_* and fact_* mart tables from staging.

    Executes sql/build_marts.sql.  Safe to re-run — each section
    DELETEs first, so marts always reflect the latest staging data.

    Returns the row counts for the rebuilt mart tables.
    """
    sql_path = Path(__file__).resolve().parent / "sql" / "build_marts.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"Mart SQL not found: {sql_path}")

    conn = get_conn(db_path)
    ensure_schema(conn)
    try:
        sql_text = sql_path.read_text()
        # DuckDB executes multi-statement scripts via the `execute` method.
        # Strip the one SQLite-specific line that DuckDB doesn't understand.
        sql_text = "\n".join(
            line for line in sql_text.splitlines()
            if "sqlite_sequence" not in line and "ON CONFLICT DO NOTHING" not in line
        )
        conn.execute(sql_text)
        logger.info("Mart tables rebuilt from staging")

        mart_tables = ["dim_location", "dim_utility", "dim_tariff",
                       "fact_quote", "fact_rate_history"]
        counts = {
            t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
            for t in mart_tables
        }
        return counts
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ETL: fetch solar data and load into the DuckDB warehouse.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 solar_etl.py --location 94061 --monthly-kwh 650
  python3 solar_etl.py --location "Boulder, CO" --system-kw 8
  python3 solar_etl.py --status
""",
    )
    parser.add_argument("--location", help="Address, city+state, or ZIP")
    parser.add_argument("--monthly-kwh", type=float,
                        help="Average monthly kWh for bill sizing")
    parser.add_argument("--system-kw", type=float, help="System size in kW")
    parser.add_argument("--install-date", type=str,
                        help="Installation date (YYYY-MM-DD)")
    parser.add_argument("--status", action="store_true",
                        help="Print warehouse row counts and exit")
    parser.add_argument("--build-marts", action="store_true",
                        help="Rebuild dim_* and fact_* tables from staging")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.status:
        counts = etl_status()
        print("Warehouse tables:")
        for name, count in sorted(counts.items()):
            print(f"  {name:20s}  {count:>8} rows")
        return

    if args.build_marts:
        print("Rebuilding mart tables from staging...")
        mart_counts = build_marts()
        print("Mart tables:")
        for name, count in sorted(mart_counts.items()):
            print(f"  {name:20s}  {count:>8} rows")
        return

    if not args.location:
        parser.error("--location is required (or use --status / --build-marts)")

    install_dt = None
    if args.install_date:
        install_dt = datetime.strptime(args.install_date, "%Y-%m-%d").date()

    quote = etl_quote(
        location=args.location,
        monthly_kwh=args.monthly_kwh,
        system_kw=args.system_kw,
        install_date=install_dt,
    )

    r = quote.site_result
    print()
    print(f"  {r.address_label}")
    print(f"  Score: {r.viability_score:.0f}/100 — {r.viability_label}")
    print(f"  System: {quote.system_kw_used:.1f} kW ({quote.system_sizing_method})")
    print(f"  Payback: {r.simple_payback_years:.1f} yr | NPV ${r.npv:,.0f}")
    print(f"  Rate: ${r.electricity_rate_used:.3f}/kWh from {quote.rate_result.utility_name}")
    print()
    print("  Staged to warehouse:")
    for name, count in sorted(etl_status().items()):
        if count > 0:
            print(f"    {name}: {count} rows")


if __name__ == "__main__":
    main()
