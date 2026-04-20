"""
solar_warehouse.py — DuckDB warehouse for the solar analysis pipeline.

Provides connection management and schema creation for the project's
analytical warehouse. Teammates query this file's tables directly;
the ETL layer (solar_etl.py) writes to it.

Usage:
    from solar_warehouse import get_conn, ensure_schema
    conn = get_conn()
    ensure_schema(conn)
    conn.execute("SELECT count(*) FROM raw_pvwatts").fetchone()
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default location for the warehouse file
# ---------------------------------------------------------------------------
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "warehouse" / "solar.duckdb"


def get_conn(db_path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """
    Open (and create if needed) a connection to the DuckDB warehouse.

    Parameters
    ----------
    db_path : Path or str, optional
        Location of the .duckdb file. Defaults to data/warehouse/solar.duckdb.

    Returns
    -------
    duckdb.DuckDBPyConnection
        An open connection.  Close it with `conn.close()` when done.
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
# Staging tables: raw captures of every API call, for auditability + replay.
# Each row preserves the full JSON response so we can re-parse later if the
# schema changes.

STAGING_DDL = """
-- Sequence shared by all staging tables (simpler than one per table)
CREATE SEQUENCE IF NOT EXISTS seq_staging_id START 1;

CREATE TABLE IF NOT EXISTS raw_pvwatts (
    id                  INTEGER DEFAULT nextval('seq_staging_id') PRIMARY KEY,
    fetched_at          TIMESTAMP NOT NULL,
    lat                 DOUBLE NOT NULL,
    lon                 DOUBLE NOT NULL,
    system_kw           DOUBLE NOT NULL,
    tilt                DOUBLE,
    azimuth             DOUBLE,
    losses              DOUBLE,
    array_type          INTEGER,
    module_type         INTEGER,
    ac_annual_kwh       DOUBLE,
    solrad_annual       DOUBLE,
    capacity_factor     DOUBLE,
    station_lat         DOUBLE,
    station_lon         DOUBLE,
    station_distance_m  DOUBLE,
    pvwatts_version     VARCHAR,
    response_json       JSON
);

CREATE TABLE IF NOT EXISTS raw_urdb (
    id                  INTEGER DEFAULT nextval('seq_staging_id') PRIMARY KEY,
    fetched_at          TIMESTAMP NOT NULL,
    lat                 DOUBLE NOT NULL,
    lon                 DOUBLE NOT NULL,
    sector              VARCHAR,
    utility_name        VARCHAR,
    rate_name           VARCHAR,
    flat_rate           DOUBLE,
    fixed_monthly_charge DOUBLE,
    is_tou              BOOLEAN,
    is_tiered           BOOLEAN,
    effective_date      DATE,
    source              VARCHAR,
    rate_uri            VARCHAR,
    response_json       JSON
);

CREATE TABLE IF NOT EXISTS raw_eia (
    id                  INTEGER DEFAULT nextval('seq_staging_id') PRIMARY KEY,
    fetched_at          TIMESTAMP NOT NULL,
    state               VARCHAR NOT NULL,
    period              VARCHAR NOT NULL,
    rate_dollars_per_kwh DOUBLE,
    source              VARCHAR,
    response_json       JSON
);

CREATE TABLE IF NOT EXISTS raw_geocode (
    id                  INTEGER DEFAULT nextval('seq_staging_id') PRIMARY KEY,
    fetched_at          TIMESTAMP NOT NULL,
    query               VARCHAR NOT NULL,
    source              VARCHAR,
    lat                 DOUBLE,
    lon                 DOUBLE,
    resolved_address    VARCHAR,
    state               VARCHAR,
    zip_code            VARCHAR,
    confidence          VARCHAR
);

CREATE TABLE IF NOT EXISTS raw_quote (
    id                  INTEGER DEFAULT nextval('seq_staging_id') PRIMARY KEY,
    fetched_at          TIMESTAMP NOT NULL,
    location_query      VARCHAR,
    lat                 DOUBLE,
    lon                 DOUBLE,
    state               VARCHAR,
    zip_code            VARCHAR,
    system_kw           DOUBLE,
    system_sizing_method VARCHAR,
    viability_score     DOUBLE,
    viability_label     VARCHAR,
    payback_years       DOUBLE,
    npv_25yr            DOUBLE,
    irr                 DOUBLE,
    lcoe                DOUBLE,
    co2_avoided_tons    DOUBLE,
    utility_name        VARCHAR,
    rate_name           VARCHAR,
    rate_source         VARCHAR,
    electricity_rate_used DOUBLE,
    export_policy       VARCHAR,
    export_rate         DOUBLE,
    confidence_level    VARCHAR,
    confidence_reasons  JSON,
    full_result_json    JSON
);
"""

def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """
    Create all staging tables if they don't already exist.

    Safe to call repeatedly — uses CREATE TABLE IF NOT EXISTS.

    The warehouse has only ``raw_*`` staging tables — no mart layer.
    For a class-sized dataset, querying the staging tables directly
    (with the full JSON response preserved) is simpler and more flexible
    than maintaining a dim/fact star schema.
    """
    conn.execute(STAGING_DDL)
    logger.info("Warehouse schema ready")


def drop_all(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop every table (and sequence) in the warehouse. Dangerous — testing only."""
    tables = ["raw_pvwatts", "raw_urdb", "raw_eia", "raw_geocode", "raw_quote"]
    for t in tables:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("DROP SEQUENCE IF EXISTS seq_staging_id")


def quote_from_dict(d: dict):
    """
    Rehydrate a QuoteResult from its ``to_dict()`` output.

    The ``raw_quote.full_result_json`` column stores the full serialized
    QuoteResult.  When the OLTP app hits a cache hit in the warehouse,
    this function reconstructs the full object tree so the report can
    be re-rendered without any live API calls.

    Imports are lazy to avoid circular imports at module load time.

    Parameters
    ----------
    d : dict
        The deserialized JSON from ``raw_quote.full_result_json``
        (i.e. ``QuoteResult.to_dict()`` output).

    Returns
    -------
    QuoteResult
        A fully populated QuoteResult. Note that ``RateResult.hourly_rates``
        (a numpy array, not JSON-serializable) is set to ``None`` and
        ``RateResult.raw`` (the original API response) is set to ``{}``.
        Everything else round-trips exactly.
    """
    from solar_fetch import QuoteResult
    from solar_economics import SiteResult
    from solar_geocode import GeocodeResult
    from solar_pvwatts import PVWattsResult
    from solar_urdb import RateResult
    from solar_nem import ExportValueResult

    def _fields_only(cls, data):
        return {k: v for k, v in data.items() if k in cls.__dataclass_fields__}

    rate_data = _fields_only(RateResult, d["rate"])
    # hourly_rates is a numpy array and isn't persisted in full_result_json
    rate_data["hourly_rates"] = None
    rate_data["raw"] = {}

    # effective_date is stored as an ISO string (or null); dataclass expects date|None
    ed = rate_data.get("effective_date")
    if isinstance(ed, str):
        from datetime import date as _date
        try:
            rate_data["effective_date"] = _date.fromisoformat(ed)
        except ValueError:
            rate_data["effective_date"] = None

    return QuoteResult(
        site_result=SiteResult(**_fields_only(SiteResult, d["site"])),
        geocode_result=GeocodeResult(**_fields_only(GeocodeResult, d["geocode"])),
        pvwatts_result=PVWattsResult(**_fields_only(PVWattsResult, d["pvwatts"])),
        rate_result=RateResult(**rate_data),
        export_result=ExportValueResult(**_fields_only(ExportValueResult, d["export"])),
        system_kw_used=d["meta"]["system_kw_used"],
        system_sizing_method=d["meta"]["system_sizing_method"],
        confidence_level=d["meta"]["confidence_level"],
        confidence_reasons=d["meta"]["confidence_reasons"],
    )


def latest_quote(
    location_query: str,
    conn: duckdb.DuckDBPyConnection | None = None,
    max_age_days: int = 7,
) -> dict | None:
    """
    Return the most recent quote for a location, if one exists in the warehouse
    and is fresh enough.  Returns None if no matching quote or all are too old.

    This is the "warehouse-first" read path for Phase 3.  It lets the app (or
    teammates) check if we've already priced this location without re-running
    the full pipeline.

    Parameters
    ----------
    location_query : str
        The original user input (e.g. "94061" or "San Jose, CA").
    conn : duckdb connection, optional
        Reuse an existing connection, or pass None to open and close one here.
    max_age_days : int
        How stale a quote can be before we consider it missing.  Default 7.

    Returns
    -------
    dict or None
        Column→value dict for the freshest matching raw_quote row, or None.
    """
    close_when_done = conn is None
    if conn is None:
        conn = get_conn()
        ensure_schema(conn)

    try:
        # DuckDB parser doesn't allow prepared-statement params inside INTERVAL.
        # Safe to string-interpolate since `max_age_days` is validated to be int.
        days = int(max_age_days)
        row = conn.execute(
            f"""
            SELECT *
            FROM raw_quote
            WHERE lower(location_query) = lower(?)
              AND fetched_at >= now() - INTERVAL {days} DAYS
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            [location_query],
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in conn.description]
        return dict(zip(cols, row))
    finally:
        if close_when_done:
            conn.close()


def table_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Return row counts for every table.  Useful for smoke tests + debugging."""
    out: dict[str, int] = {}
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    for (name,) in rows:
        try:
            count = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            out[name] = int(count)
        except Exception:
            out[name] = -1
    return out


if __name__ == "__main__":
    # Quick smoke test: create the schema and print row counts
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    conn = get_conn()
    ensure_schema(conn)
    counts = table_counts(conn)
    print(f"Warehouse path: {DEFAULT_DB_PATH}")
    print("Tables:")
    for name, count in counts.items():
        print(f"  {name:20s}  {count:>8} rows")
    conn.close()
