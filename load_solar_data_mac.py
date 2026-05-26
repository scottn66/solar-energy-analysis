#!/usr/bin/env python3
"""
Solar Energy Analytics — Data Loader (Mac-adapted from Sayli's v3)

Changes from Sayli's original:
  1. Reads MySQL password from MYSQL_PASSWORD env var (no plaintext)
  2. CSV path points at the cleaned LBNL data on this Mac
  3. CHUNK_SIZE upped to 5,000 (faster on local MySQL)
  4. Better progress reporting
  5. Idempotent: appends rows ONLY IF the table is empty; otherwise warns

Usage:
    export MYSQL_PASSWORD='your_password_here'
    python3 load_solar_data_mac.py
"""

import csv
import mysql.connector
import os
import sys
import time
from datetime import datetime

# ──────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────
DB_CONFIG = {
    "host":     "localhost",
    "port":     3306,
    "user":     "root",
    "password": os.environ.get("MYSQL_PASSWORD", ""),  # Set via env var
    "database": "solar_energy_db",
    "allow_local_infile": True,
}

# Path to the cleaned LBNL data on Scott's Mac
CSV_FILE   = "/Users/scottnelson/Desktop/DATA201-Solar/data/tts_cleaned.csv"
CHUNK_SIZE = 5000

# ──────────────────────────────────────────
# EXACT COLUMNS (lowercase after cleaning)
# ──────────────────────────────────────────
EXPECTED_COLUMNS = [
    "installation_date",
    "pv_system_size_dc",
    "total_installed_price",
    "rebate_or_grant",
    "customer_segment",
    "state",
    "zip_code",
    "utility_service_territory",
    "third_party_owned",
    "installer_name",
    "tracking",
    "ground_mounted",
    "azimuth_1",
    "tilt_1",
    "module_manufacturer_1",
    "module_model_1",
    "module_quantity_1",
    "technology_module_1",
    "efficiency_module_1",
    "inverter_manufacturer_1",
    "inverter_model_1",
    "output_capacity_inverter_1",
    "inverter_loading_ratio",
    "battery_rated_capacity_kwh",
    "price_per_watt",
]

CREATE_DB_SQL = "CREATE DATABASE IF NOT EXISTS solar_energy_db;"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS solar_installations (
    id                          INT AUTO_INCREMENT PRIMARY KEY,
    installation_date           DATE,
    pv_system_size_dc           DECIMAL(10,4),
    total_installed_price       DECIMAL(14,2),
    rebate_or_grant             DECIMAL(14,2),
    customer_segment            VARCHAR(20),
    state                       VARCHAR(50),
    zip_code                    VARCHAR(10),
    utility_service_territory   VARCHAR(150),
    third_party_owned           TINYINT,
    installer_name              VARCHAR(150),
    tracking                    TINYINT,
    ground_mounted              TINYINT,
    azimuth_1                   DECIMAL(8,2),
    tilt_1                      DECIMAL(8,2),
    module_manufacturer_1       VARCHAR(150),
    module_model_1              VARCHAR(150),
    module_quantity_1           INT,
    technology_module_1         VARCHAR(50),
    efficiency_module_1         DECIMAL(10,6),
    inverter_manufacturer_1     VARCHAR(150),
    inverter_model_1            VARCHAR(150),
    output_capacity_inverter_1  DECIMAL(10,4),
    inverter_loading_ratio      DECIMAL(10,4),
    battery_rated_capacity_kwh  DECIMAL(10,4),
    price_per_watt              DECIMAL(10,6),
    INDEX idx_state    (state),
    INDEX idx_date     (installation_date),
    INDEX idx_segment  (customer_segment),
    INDEX idx_zip      (zip_code)
);
"""


def connect(database=None):
    cfg = dict(DB_CONFIG)
    if database is None:
        cfg.pop("database", None)
    return mysql.connector.connect(**cfg)


def clean_col(name):
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def clean_val(v):
    s = str(v).strip()
    if s in ("", "NULL", "null", "None", "NaN", "nan", "N/A", "n/a", "none"):
        return None
    return s


def parse_date(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "None", "nan"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def setup_database(force_truncate=False):
    print("STEP 1: Setting up database ...")

    if not DB_CONFIG["password"]:
        sys.exit(
            "\n  ERROR: MYSQL_PASSWORD is not set.\n"
            "  Run: export MYSQL_PASSWORD='your_password'\n"
            "  Then re-run this script."
        )

    try:
        conn = connect()
    except mysql.connector.errors.ProgrammingError as e:
        sys.exit(f"\n  ERROR connecting to MySQL: {e}\n  Check that mysqld is running and the password is correct.")

    cur = conn.cursor()
    cur.execute(CREATE_DB_SQL)
    conn.commit()
    cur.close()
    conn.close()

    conn = connect(database="solar_energy_db")
    cur  = conn.cursor()
    cur.execute(CREATE_TABLE_SQL)

    cur.execute("SELECT count(*) FROM solar_installations")
    existing = cur.fetchone()[0]

    if existing > 0:
        if force_truncate:
            print(f"   Table has {existing:,} existing rows. Truncating ...")
            cur.execute("TRUNCATE TABLE solar_installations;")
        else:
            print(f"   Table already has {existing:,} rows. Use --reload to truncate first.")
            print("   Skipping load. Will only run verification queries.")
            cur.close()
            conn.close()
            return False
    conn.commit()
    cur.close()
    conn.close()
    print("   Database and table [solar_installations] ready.\n")
    return True


def inspect_csv(path):
    print(f"STEP 2: Inspecting CSV ...")
    if not os.path.exists(path):
        sys.exit(f"\n  ERROR: File not found:\n    {path}")

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        row_count = sum(1 for _ in f) - 1

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        reader  = csv.reader(f)
        raw_hdr = next(reader)
        headers = [clean_col(h) for h in raw_hdr]

    print(f"   File      : {path}")
    print(f"   Rows      : {row_count:,}")
    print(f"   Columns   : {len(headers)}")

    available = [c for c in EXPECTED_COLUMNS if c in headers]
    missing   = [c for c in EXPECTED_COLUMNS if c not in headers]
    extra     = [c for c in headers if c not in EXPECTED_COLUMNS]

    if missing:
        print(f"\n   MISSING columns (will be NULL): {missing}")
    if extra:
        print(f"   EXTRA columns in CSV (ignored): {extra}")
    print(f"\n   Matched {len(available)} / {len(EXPECTED_COLUMNS)} expected columns.\n")

    return headers, available, row_count


def load_csv_to_mysql(path, headers, available, total_rows):
    print(f"STEP 3: Loading {total_rows:,} rows in batches of {CHUNK_SIZE:,} ...")

    insert_sql = (
        f"INSERT INTO solar_installations ({', '.join(available)}) "
        f"VALUES ({', '.join(['%s'] * len(available))})"
    )

    col_index = {h: i for i, h in enumerate(headers)}
    date_cols = {"installation_date"}

    conn = connect(database="solar_energy_db")
    cur  = conn.cursor()

    batch         = []
    rows_inserted = 0
    skipped       = 0
    start_time    = time.time()

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        next(reader)  # skip header

        for line_num, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) < len(headers) * 0.5:
                skipped += 1
                continue

            try:
                values = []
                for c in available:
                    idx = col_index.get(c)
                    raw = row[idx] if idx is not None and idx < len(row) else ""
                    if c in date_cols:
                        values.append(parse_date(raw))
                    else:
                        values.append(clean_val(raw))
                batch.append(tuple(values))
            except Exception:
                skipped += 1
                continue

            if len(batch) >= CHUNK_SIZE:
                cur.executemany(insert_sql, batch)
                conn.commit()
                rows_inserted += len(batch)
                batch.clear()
                pct  = rows_inserted / total_rows * 100 if total_rows else 0
                rate = rows_inserted / max(time.time() - start_time, 0.1)
                eta  = (total_rows - rows_inserted) / max(rate, 1)
                print(f"   {rows_inserted:>10,} / {total_rows:,}  ({pct:5.1f}%)  "
                      f"{rate:,.0f} rows/sec  ETA: {eta:.0f}s", end="\r")

    if batch:
        cur.executemany(insert_sql, batch)
        conn.commit()
        rows_inserted += len(batch)

    elapsed = time.time() - start_time
    print(f"\n   Inserted : {rows_inserted:,} rows in {elapsed:.0f}s ({rows_inserted/elapsed:,.0f} rows/sec)")
    if skipped:
        print(f"   Skipped  : {skipped:,} malformed/short rows")
    print()
    cur.close()
    conn.close()


def run_verification():
    print("STEP 4: Verification queries ...\n")
    conn = connect(database="solar_energy_db")
    cur  = conn.cursor()

    checks = [
        ("Total rows loaded",
         "SELECT COUNT(*) FROM solar_installations"),
        ("Distinct states",
         "SELECT COUNT(DISTINCT state) FROM solar_installations"),
        ("Date range",
         "SELECT MIN(installation_date), MAX(installation_date) FROM solar_installations"),
        ("Top 5 states by installations",
         """SELECT state, COUNT(*) AS installs
            FROM solar_installations
            GROUP BY state ORDER BY installs DESC LIMIT 5"""),
        ("Customer segment breakdown",
         """SELECT customer_segment, COUNT(*) AS cnt,
                   ROUND(AVG(pv_system_size_dc),3) AS avg_size_kw,
                   ROUND(AVG(price_per_watt),4)    AS avg_price_w
            FROM solar_installations
            GROUP BY customer_segment ORDER BY cnt DESC"""),
        ("Median price/watt by year (recent)",
         """SELECT YEAR(installation_date) AS yr,
                   COUNT(*) AS n,
                   ROUND(AVG(price_per_watt), 3) AS avg_pw
            FROM solar_installations
            WHERE installation_date >= '2020-01-01'
              AND price_per_watt BETWEEN 1.0 AND 15.0
            GROUP BY yr ORDER BY yr"""),
    ]

    for title, sql in checks:
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            print(f"  -- {title} --")
            for r in rows:
                print(f"     {r}")
            print()
        except Exception as e:
            print(f"  WARNING: {title} failed: {e}\n")

    cur.close()
    conn.close()


if __name__ == "__main__":
    print("=" * 60)
    print("  Solar Energy Analytics  |  Mac-adapted Loader")
    print(f"  Source: {CSV_FILE}")
    print("=" * 60 + "\n")

    force_truncate = "--reload" in sys.argv
    should_load = setup_database(force_truncate=force_truncate)

    if should_load:
        headers, available, total = inspect_csv(CSV_FILE)
        load_csv_to_mysql(CSV_FILE, headers, available, total)

    run_verification()

    print("=" * 60)
    print("  Pipeline complete!")
    print("  Next: start Flask backend with the same MYSQL_PASSWORD.")
    print("=" * 60)
