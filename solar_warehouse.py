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

# Mart tables: cleaned, joinable, dimensional. Built by sql/build_marts.sql.
# Defined here so we can CREATE them (empty) during schema init — the mart
# build script populates them from staging.

MART_DDL = """
-- Shared sequences for mart surrogate keys
CREATE SEQUENCE IF NOT EXISTS seq_location_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_utility_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_tariff_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_quote_id START 1;

CREATE TABLE IF NOT EXISTS dim_location (
    location_id         INTEGER DEFAULT nextval('seq_location_id') PRIMARY KEY,
    lat                 DOUBLE NOT NULL,
    lon                 DOUBLE NOT NULL,
    state               VARCHAR,
    zip_code            VARCHAR,
    city                VARCHAR,
    county              VARCHAR,
    first_seen_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dim_utility (
    utility_id          INTEGER DEFAULT nextval('seq_utility_id') PRIMARY KEY,
    utility_name        VARCHAR NOT NULL,
    state               VARCHAR,
    first_seen_at       TIMESTAMP
);

-- SCD Type 2: one row per (utility, rate_name, effective_from).
-- effective_to = NULL means "currently in effect"
CREATE TABLE IF NOT EXISTS dim_tariff (
    tariff_id           INTEGER DEFAULT nextval('seq_tariff_id') PRIMARY KEY,
    utility_id          INTEGER,
    rate_name           VARCHAR,
    flat_rate           DOUBLE,
    is_tou              BOOLEAN,
    is_tiered           BOOLEAN,
    effective_from      DATE,
    effective_to        DATE,
    source              VARCHAR
);

CREATE TABLE IF NOT EXISTS fact_quote (
    quote_id            INTEGER DEFAULT nextval('seq_quote_id') PRIMARY KEY,
    raw_quote_id        INTEGER,
    created_at          TIMESTAMP,
    location_id         INTEGER,
    utility_id          INTEGER,
    tariff_id           INTEGER,
    system_kw           DOUBLE,
    viability_score     DOUBLE,
    payback_years       DOUBLE,
    npv_25yr            DOUBLE,
    irr                 DOUBLE,
    lcoe                DOUBLE,
    co2_avoided_tons    DOUBLE,
    confidence_level    VARCHAR,
    geocode_source      VARCHAR,
    rate_source         VARCHAR,
    export_policy       VARCHAR
);

CREATE TABLE IF NOT EXISTS fact_rate_history (
    state               VARCHAR,
    period              DATE,
    rate_dollars_per_kwh DOUBLE,
    source              VARCHAR,
    PRIMARY KEY (state, period)
);
"""


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """
    Create all staging and mart tables if they don't already exist.

    Safe to call repeatedly — uses CREATE TABLE IF NOT EXISTS.
    """
    conn.execute(STAGING_DDL)
    conn.execute(MART_DDL)
    logger.info("Warehouse schema ready")


def drop_all(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop every table (and sequence) in the warehouse. Dangerous — testing only."""
    tables = [
        "raw_pvwatts", "raw_urdb", "raw_eia", "raw_geocode", "raw_quote",
        "dim_location", "dim_utility", "dim_tariff",
        "fact_quote", "fact_rate_history",
    ]
    for t in tables:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    for s in ["seq_staging_id", "seq_location_id", "seq_utility_id",
              "seq_tariff_id", "seq_quote_id"]:
        conn.execute(f"DROP SEQUENCE IF EXISTS {s}")


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
