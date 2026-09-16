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
    electricity_rate: Optional[float] = None,
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
            electricity_rate=electricity_rate,
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


def etl_batch(
    csv_path: Path | str,
    db_path: Optional[Path] = None,
) -> dict[str, list]:
    """
    Run :func:`etl_quote` for every row of a batch CSV and persist each
    result to the warehouse.  This is the one-command way to populate the
    warehouse for a whole region, e.g.::

        python3 solar_etl.py --batch data/oregon_locations.csv

    CSV format (``#`` comment lines ignored)::

        location,monthly_kwh,system_kw,note
        97756,900,,Redmond — Central Electric Co-op

    ``location`` is required (ZIP or "City, ST"); ``monthly_kwh`` and
    ``system_kw`` are optional floats; ``note`` is echoed to the console.
    A failing row is reported and skipped — one bad address doesn't abort
    the batch.

    Returns
    -------
    dict
        ``{"ok": [(location, viability_score), ...],
           "failed": [(location, error_message), ...]}``
    """
    import csv as _csv

    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Batch CSV not found: {csv_path}")

    rows: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        lines = (line for line in fh if not line.lstrip().startswith("#"))
        for row in _csv.DictReader(lines):
            if (row.get("location") or "").strip():
                rows.append(row)

    ok: list[tuple[str, float]] = []
    failed: list[tuple[str, str]] = []
    print(f"Batch ETL: {len(rows)} locations from {csv_path}")

    for idx, row in enumerate(rows, 1):
        location = row["location"].strip()
        note = (row.get("note") or "").strip()

        def _opt_float(key: str) -> Optional[float]:
            val = (row.get(key) or "").strip()
            return float(val) if val else None

        label = f"{location}" + (f"  ({note})" if note else "")
        print(f"  [{idx}/{len(rows)}] {label}")
        try:
            quote = etl_quote(
                location=location,
                monthly_kwh=_opt_float("monthly_kwh"),
                system_kw=_opt_float("system_kw"),
                db_path=db_path,
            )
            r = quote.site_result
            print(f"      score={r.viability_score:.0f}  "
                  f"payback={r.simple_payback_years:.1f} yr  "
                  f"rate=${r.electricity_rate_used:.3f}/kWh  "
                  f"({quote.rate_result.utility_name[:36]})")
            ok.append((location, r.viability_score))
        except Exception as exc:
            print(f"      FAILED: {type(exc).__name__}: {exc}")
            failed.append((location, f"{type(exc).__name__}: {exc}"))

    print(f"Batch complete: {len(ok)} ok, {len(failed)} failed.")
    return {"ok": ok, "failed": failed}


def etl_status(db_path: Optional[Path] = None) -> dict[str, int]:
    """Return row counts for every warehouse table (sanity check)."""
    conn = get_conn(db_path)
    ensure_schema(conn)
    try:
        return table_counts(conn)
    finally:
        conn.close()


def load_tts(
    csv_path: Path | str,
    db_path: Optional[Path] = None,
    truncate: bool = True,
) -> int:
    """
    Bulk-load the cleaned LBNL Tracking the Sun CSV into raw_tts_installations.

    The CSV is the output of ``etl/clean_tts.py`` — already null-safe, with
    -1 sentinels removed and zip codes normalized.  This function streams it
    directly into DuckDB via ``read_csv_auto`` (no pandas in the middle).

    Parameters
    ----------
    csv_path : Path or str
        Path to the cleaned CSV (typically ``data/tts_cleaned.csv``).
    db_path : Path, optional
        Custom warehouse path.  Defaults to ``data/warehouse/solar.duckdb``.
    truncate : bool
        If True (default), DELETE existing rows before loading.  Idempotent:
        re-running on the same file produces the same row count.

    Returns
    -------
    int
        Number of rows loaded into ``raw_tts_installations``.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Cleaned TTS CSV not found: {csv_path}")

    conn = get_conn(db_path)
    ensure_schema(conn)
    try:
        if truncate:
            n_before = conn.execute(
                "SELECT count(*) FROM raw_tts_installations"
            ).fetchone()[0]
            conn.execute("DELETE FROM raw_tts_installations")
            if n_before:
                logger.info("Truncated raw_tts_installations (was %d rows)", n_before)

        # DuckDB reads the CSV directly. The 25-column order in the cleaned
        # CSV matches our table definition exactly (after id and loaded_at).
        # We let DuckDB auto-detect dtypes and only override zip_code (it
        # arrives as integer-looking but must stay a string for joins).
        logger.info("Loading TTS CSV: %s", csv_path)
        conn.execute(
            f"""
            INSERT INTO raw_tts_installations (
                loaded_at, installation_date, PV_system_size_DC,
                total_installed_price, rebate_or_grant, customer_segment,
                tracking, ground_mounted, zip_code, state,
                utility_service_territory, third_party_owned, installer_name,
                azimuth_1, tilt_1, module_manufacturer_1, module_model_1,
                module_quantity_1, technology_module_1, efficiency_module_1,
                inverter_manufacturer_1, inverter_model_1,
                output_capacity_inverter_1, inverter_loading_ratio,
                battery_rated_capacity_kWh, price_per_watt
            )
            SELECT now() AS loaded_at, *
            FROM read_csv_auto(
                '{csv_path}',
                header=true,
                types={{'zip_code': 'VARCHAR'}}
            )
            """
        )

        n_loaded = conn.execute(
            "SELECT count(*) FROM raw_tts_installations"
        ).fetchone()[0]
        logger.info("Loaded %s rows into raw_tts_installations", f"{n_loaded:,}")
        return int(n_loaded)
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
  python3 solar_etl.py --batch data/oregon_locations.csv
  python3 solar_etl.py --status
  python3 solar_etl.py --load-tts data/tts_cleaned.csv
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
    parser.add_argument("--load-tts", type=str, metavar="CSV_PATH",
                        help="Bulk-load the cleaned TTS CSV into "
                             "raw_tts_installations and exit")
    parser.add_argument("--batch", type=str, metavar="CSV_PATH",
                        help="Run the full pipeline for every location in a "
                             "batch CSV (see data/oregon_locations.csv) and "
                             "persist each to the warehouse")
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
            print(f"  {name:30s}  {count:>10,} rows")
        return

    if args.load_tts:
        n = load_tts(args.load_tts)
        print(f"\n  Loaded {n:,} rows into raw_tts_installations")
        print(f"  Warehouse status:")
        for name, count in sorted(etl_status().items()):
            print(f"    {name:30s}  {count:>10,} rows")
        return

    if args.batch:
        summary = etl_batch(args.batch)
        print("\n  Warehouse status:")
        for name, count in sorted(etl_status().items()):
            if count > 0:
                print(f"    {name:30s}  {count:>10,} rows")
        if summary["failed"]:
            raise SystemExit(1)
        return

    if not args.location:
        parser.error("--location is required (or use --status / --load-tts / --batch)")

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
