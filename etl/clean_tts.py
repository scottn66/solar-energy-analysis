"""
clean_tts.py — Cleaning pipeline for the LBNL Tracking the Sun dataset.

Input:  data/tts_raw.csv     (3.66M rows, 24 analysis columns)
Output: data/tts_cleaned.csv (cleaned, residential-only, outliers removed)
Report: etl/cleaning_notes.md
Log:    etl/cleaning.log

Usage:
    python3 etl/clean_tts.py --input data/tts_raw.csv \\
                              --output data/tts_cleaned.csv \\
                              --report etl/cleaning_notes.md

    # Preview without writing
    python3 etl/clean_tts.py --input data/tts_raw.csv --dry-run

    # Read in chunks for memory-constrained machines
    python3 etl/clean_tts.py --input data/tts_raw.csv --chunksize 500000

LBNL uses -1 as a sentinel for "not reported". This script replaces those
with NaN so downstream analysis sees the real missingness.  0 is preserved
as a legitimate value (e.g., tracking=0 means "no tracking").

The pipeline is idempotent: running twice on the same input produces the
same output.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The 24 analysis columns used across the project (see notebooks/03_data_loading.ipynb)
ANALYSIS_COLUMNS = [
    "installation_date",
    "PV_system_size_DC",
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
    "battery_rated_capacity_kWh",
]

# Values LBNL uses as sentinels for "not reported".
# Applied to BOTH numeric columns (as -1) and string columns (as "-1").
SENTINEL_VALUES = [-1, "-1"]

# String values that mean "unknown" across free-text columns.
# All of these get converted to NaN during Step 6.
KNOWN_UNKNOWNS = [
    "Unknown", "UNKNOWN", "unknown",
    "redacted", "Redacted", "REDACTED",
    "no match", "No Match", "NO MATCH",
    "",
    "NA", "N/A", "n/a",
]

# Economic validity bounds for price_per_watt (computed in Step 4).
# Based on NREL cost benchmarks; outside this range values are near-certainly
# data errors or non-standard transactions (barter, post-incentive reporting,
# commercial-reseller pricing miscategorized as residential, etc.).
PRICE_PER_WATT_MIN = 0.50
PRICE_PER_WATT_MAX = 15.00

# Residential system size cap. Anything larger is commercial-scale and drops
# out in Step 3 regardless of customer_segment.
MAX_RESIDENTIAL_KW = 100.0

# Residential customer_segment codes.  LBNL uses RES, RES_SF, RES_MF, etc.
RESIDENTIAL_PREFIX = "RES"

# ---------------------------------------------------------------------------
# Logging setup (console + file handlers)
# ---------------------------------------------------------------------------
logger = logging.getLogger("clean_tts")


def setup_logging(log_path: Path, verbose: bool = False) -> None:
    """Configure root logger: INFO to console, DEBUG to file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    # Clear any existing handlers (lets us re-run in the same process)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_path, mode="w")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    )
    logger.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------
class Report:
    """Accumulates per-step stats and renders the final Markdown report."""

    def __init__(self, input_path: Path, n_rows: int, n_cols: int):
        self.input_path = input_path
        self.n_rows_original = n_rows
        self.n_cols_original = n_cols
        self.run_at = datetime.now()
        self.step_rows: list[tuple[str, int, int, int]] = []
        # (step_name, rows_before, rows_removed, rows_after)
        self.sections: dict[str, str] = {}
        self.step1_null_rates: pd.Series | None = None
        self.step4_low_count: int = 0
        self.step4_high_count: int = 0
        self.step5_stats: list[tuple[str, int, str]] = []
        self.step6_stats: list[tuple[str, int]] = []
        self.final_df: pd.DataFrame | None = None
        self.validations: dict[str, bool] = {}

    def track_step(self, name: str, before: int, after: int) -> None:
        removed = before - after
        self.step_rows.append((name, before, removed, after))
        pct = after / self.n_rows_original * 100 if self.n_rows_original else 0
        logger.info(
            "  %s: %s rows -> %s rows (removed %s, %.1f%% of original)",
            name, f"{before:,}", f"{after:,}", f"{removed:,}", pct,
        )

    def write_markdown(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(self._render())
        logger.info("Wrote cleaning report: %s", path)

    def _render(self) -> str:
        out = []
        out.append("# TTS Data Cleaning Report\n")

        # Source
        out.append("## Source\n")
        out.append(f"- File: `{self.input_path}`")
        out.append(f"- Original rows: {self.n_rows_original:,}")
        out.append(f"- Original columns: {self.n_cols_original}")
        out.append(f"- Run date: {self.run_at.isoformat(timespec='seconds')}\n")

        # Cleaning steps
        out.append("## Cleaning steps applied\n")

        # Step 1
        out.append("### Step 1: Sentinel replacement")
        out.append(f"- Replaced `-1` / `\"-1\"` with `NULL` across all {self.n_cols_original} columns.")
        out.append("- Post-replacement null rates by column:\n")
        out.append("| Column | Null count | Null % |")
        out.append("|---|---:|---:|")
        if self.step1_null_rates is not None:
            total = self.n_rows_original
            for col, count in self.step1_null_rates.items():
                pct = count / total * 100 if total else 0
                out.append(f"| `{col}` | {count:,} | {pct:.2f}% |")
        out.append("")

        # Step 2
        out.append(self.sections.get("step2", ""))

        # Step 3
        out.append(self.sections.get("step3", ""))

        # Step 4
        out.append("### Step 4: Price outlier filtering")
        out.append(f"- Dropped {self.step4_low_count:,} rows with `price_per_watt < ${PRICE_PER_WATT_MIN:.2f}/W`")
        out.append(f"- Dropped {self.step4_high_count:,} rows with `price_per_watt > ${PRICE_PER_WATT_MAX:.2f}/W`")
        out.append(f"- Net rows remaining: {self.sections.get('step4_remaining', 'n/a')}\n")

        # Step 5
        out.append("### Step 5: Case normalization")
        out.append("| Column | Duplicates collapsed | Example |")
        out.append("|---|---:|---|")
        for col, collapsed, example in self.step5_stats:
            out.append(f"| `{col}` | {collapsed:,} | {example} |")
        out.append("")

        # Step 6
        out.append("### Step 6: Standardize unknowns")
        out.append("| Column | Replacements made |")
        out.append("|---|---:|")
        for col, n in self.step6_stats:
            out.append(f"| `{col}` | {n:,} |")
        out.append("")

        # Step 7
        out.append("### Step 7: Final validation")
        final_n = len(self.final_df) if self.final_df is not None else 0
        pct = final_n / self.n_rows_original * 100 if self.n_rows_original else 0
        final_cols = len(self.final_df.columns) if self.final_df is not None else 0
        out.append(f"- Final rows: {final_n:,} ({pct:.1f}% of original)")
        out.append(f"- Columns: {final_cols} (original {self.n_cols_original} + 1 derived `price_per_watt`)")
        for check, ok in self.validations.items():
            out.append(f"- {check}: {'PASS' if ok else 'FAIL'}")
        out.append("")

        # Post-cleaning profiles
        out.append("## Post-cleaning column profiles\n")
        if self.final_df is not None:
            out.append("| Column | Dtype | Non-null | Null % | Min / Top-3 | Median | Max |")
            out.append("|---|---|---:|---:|---|---|---|")
            for col in self.final_df.columns:
                s = self.final_df[col]
                non_null = s.notna().sum()
                null_pct = (len(s) - non_null) / len(s) * 100 if len(s) else 0
                if pd.api.types.is_numeric_dtype(s):
                    mn = f"{s.min():.3f}" if non_null else "n/a"
                    md = f"{s.median():.3f}" if non_null else "n/a"
                    mx = f"{s.max():.3f}" if non_null else "n/a"
                    out.append(
                        f"| `{col}` | {s.dtype} | {non_null:,} | {null_pct:.1f}% | {mn} | {md} | {mx} |"
                    )
                else:
                    nunique = s.nunique(dropna=True)
                    top = s.value_counts(dropna=True).head(3).to_dict()
                    top_str = "; ".join(f"{k!r}={v:,}" for k, v in top.items())
                    out.append(
                        f"| `{col}` | {s.dtype} | {non_null:,} | {null_pct:.1f}% | {nunique} unique | {top_str} | - |"
                    )
            out.append("")

        # Decision rationale
        out.append("## Decision rationale\n")
        out.append(
            "- **Sentinel -1 replaced rather than dropped** because LBNL uses it "
            "uniformly as \"not reported\" — these are structurally missing, not "
            "erroneous."
        )
        out.append(
            "- **Price rows dropped rather than imputed** because imputing "
            "installed price would corrupt all downstream economic metrics "
            "(LCOE, payback, price_per_watt)."
        )
        out.append(
            "- **Residential filter applied** because the project scope is "
            "residential solar viability."
        )
        out.append(
            f"- **Price outlier bounds (${PRICE_PER_WATT_MIN:.2f}–${PRICE_PER_WATT_MAX:.2f}/W)** "
            "based on NREL cost benchmarks; values outside this range are almost "
            "certainly data errors or non-standard transactions.\n"
        )

        return "\n".join(out)


# ---------------------------------------------------------------------------
# Cleaning steps — each is its own function for testability
# ---------------------------------------------------------------------------

def load_raw(input_path: Path, chunksize: int | None = None) -> pd.DataFrame:
    """Load the raw TTS CSV.  Uses usecols to grab only the 24 analysis columns."""
    logger.info("Reading raw data: %s", input_path)
    if chunksize:
        chunks = pd.read_csv(
            input_path,
            encoding="latin1",
            usecols=ANALYSIS_COLUMNS,
            dtype={"zip_code": str},
            low_memory=False,
            chunksize=chunksize,
        )
        df = pd.concat(chunks, ignore_index=True)
    else:
        df = pd.read_csv(
            input_path,
            encoding="latin1",
            usecols=ANALYSIS_COLUMNS,
            dtype={"zip_code": str},
            low_memory=False,
        )
    logger.info("Loaded %s rows x %s columns", f"{len(df):,}", len(df.columns))
    return df


def step1_replace_sentinels(df: pd.DataFrame, report: Report) -> pd.DataFrame:
    """Replace -1 and '-1' with NaN across all columns.  0 is preserved."""
    logger.info("\n=== STEP 1: Replace sentinel values (-1 -> NaN) ===")

    # Numeric columns: replace integer/float -1 with NaN
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            df[col] = df[col].replace(-1, np.nan)
        else:
            # Object/string columns: replace both -1 and "-1"
            df[col] = df[col].replace(SENTINEL_VALUES, np.nan)

    # Per-column null rates
    null_counts = df.isna().sum().sort_values(ascending=False)
    report.step1_null_rates = null_counts

    logger.info("Null rates after sentinel replacement (top 10):")
    for col, n in null_counts.head(10).items():
        pct = n / len(df) * 100
        logger.info("  %-35s  %10s  (%.2f%%)", col, f"{n:,}", pct)

    return df


def step2_drop_missing_economics(df: pd.DataFrame, report: Report) -> pd.DataFrame:
    """Drop rows with null or non-positive total_installed_price or PV_system_size_DC."""
    logger.info("\n=== STEP 2: Drop rows missing critical economic fields ===")

    before = len(df)

    price_bad = df["total_installed_price"].isna() | (df["total_installed_price"] <= 0)
    size_bad = df["PV_system_size_DC"].isna() | (df["PV_system_size_DC"] <= 0)

    n_price = int(price_bad.sum())
    n_size = int(size_bad.sum())
    n_overlap = int((price_bad & size_bad).sum())

    df = df.loc[~(price_bad | size_bad)].copy()
    after = len(df)

    logger.info("  %s rows failed price filter", f"{n_price:,}")
    logger.info("  %s rows failed size filter", f"{n_size:,}")
    logger.info("  %s rows failed both", f"{n_overlap:,}")
    report.track_step("Step 2 (drop missing economics)", before, after)

    report.sections["step2"] = "\n".join([
        "### Step 2: Drop missing economic fields",
        f"- Dropped {n_price:,} rows with null/invalid `total_installed_price`",
        f"- Dropped {n_size:,} rows with null/invalid `PV_system_size_DC`",
        f"- Overlap: {n_overlap:,} rows failed both filters",
        f"- Net rows remaining: {after:,}\n",
    ])
    return df


def step3_filter_residential(df: pd.DataFrame, report: Report) -> pd.DataFrame:
    """Keep only residential rows and drop systems > 100 kW."""
    logger.info("\n=== STEP 3: Filter to residential ===")

    before = len(df)

    segment = df["customer_segment"].astype(str).str.upper().str.strip()
    non_res = ~segment.str.startswith(RESIDENTIAL_PREFIX)
    too_big = df["PV_system_size_DC"] > MAX_RESIDENTIAL_KW

    n_non_res = int(non_res.sum())
    # Exclude overlap from too_big count so each logged number reflects its own filter
    n_big = int((too_big & ~non_res).sum())

    df = df.loc[~(non_res | too_big)].copy()
    after = len(df)

    logger.info("  %s rows non-residential", f"{n_non_res:,}")
    logger.info("  %s rows > %s kW (among remaining residential)",
                f"{n_big:,}", MAX_RESIDENTIAL_KW)
    report.track_step("Step 3 (residential filter)", before, after)

    report.sections["step3"] = "\n".join([
        "### Step 3: Filter to residential",
        f"- Dropped {n_non_res:,} rows with non-residential `customer_segment`",
        f"- Dropped {n_big:,} rows with `PV_system_size_DC > {MAX_RESIDENTIAL_KW} kW`",
        f"- Net rows remaining: {after:,}\n",
    ])
    return df


def step4_price_outliers(df: pd.DataFrame, report: Report) -> pd.DataFrame:
    """Compute price_per_watt and drop outliers outside [$0.50, $15.00]."""
    logger.info("\n=== STEP 4: Price outlier filtering ===")

    before = len(df)

    df["price_per_watt"] = (
        df["total_installed_price"] / (df["PV_system_size_DC"] * 1000.0)
    )

    too_low = df["price_per_watt"] < PRICE_PER_WATT_MIN
    too_high = df["price_per_watt"] > PRICE_PER_WATT_MAX

    n_low = int(too_low.sum())
    n_high = int(too_high.sum())

    df = df.loc[~(too_low | too_high)].copy()
    after = len(df)

    logger.info("  %s rows with $/W < $%.2f", f"{n_low:,}", PRICE_PER_WATT_MIN)
    logger.info("  %s rows with $/W > $%.2f", f"{n_high:,}", PRICE_PER_WATT_MAX)
    report.track_step("Step 4 (price outlier filter)", before, after)

    report.step4_low_count = n_low
    report.step4_high_count = n_high
    report.sections["step4_remaining"] = f"{after:,}"
    return df


def _casefold_column(
    df: pd.DataFrame, col: str, casing: str, report: Report
) -> pd.DataFrame:
    """
    Normalize a string column: strip whitespace, apply title/upper casing,
    then log how many unique values collapsed together.
    """
    if col not in df.columns:
        return df

    before_unique = df[col].dropna().nunique()
    original_sample_mapping: dict[str, str] = {}

    def normalize(val):
        if pd.isna(val):
            return val
        s = str(val).strip()
        if not s:
            return np.nan
        if casing == "title":
            out = s.title()
        elif casing == "upper":
            out = s.upper()
        else:
            out = s
        # Remember one example of the transformation
        if s != out and s not in original_sample_mapping:
            original_sample_mapping[s] = out
        return out

    df[col] = df[col].map(normalize)
    after_unique = df[col].dropna().nunique()
    collapsed = before_unique - after_unique

    example = next(
        (f"`{k}` → `{v}`" for k, v in original_sample_mapping.items()),
        "(no case changes)",
    )
    logger.info("  %s: %s unique -> %s unique (collapsed %s)  %s",
                col, f"{before_unique:,}", f"{after_unique:,}",
                f"{collapsed:,}", example)
    report.step5_stats.append((col, collapsed, example))
    return df


def step5_case_normalization(df: pd.DataFrame, report: Report) -> pd.DataFrame:
    """Normalize casing + whitespace on categorical columns."""
    logger.info("\n=== STEP 5: Case normalization ===")

    df = _casefold_column(df, "utility_service_territory", "title", report)
    df = _casefold_column(df, "inverter_model_1", "upper", report)
    df = _casefold_column(df, "inverter_manufacturer_1", "title", report)
    df = _casefold_column(df, "module_manufacturer_1", "title", report)
    df = _casefold_column(df, "installer_name", "title", report)

    # zip_code: strip trailing ".0", left-pad to 5 digits, keep as string
    if "zip_code" in df.columns:
        before_zip = df["zip_code"].copy()

        def fix_zip(val):
            if pd.isna(val):
                return val
            s = str(val).strip()
            if not s or s.lower() == "nan":
                return np.nan
            # Handle "94061.0" coming from float coercion
            if s.endswith(".0"):
                s = s[:-2]
            # Keep only digits
            s = "".join(ch for ch in s if ch.isdigit())
            if not s:
                return np.nan
            return s.zfill(5)[:5]

        df["zip_code"] = df["zip_code"].map(fix_zip)
        reformatted = int((before_zip.astype(str) != df["zip_code"].astype(str)).sum())
        logger.info("  zip_code: %s values reformatted (padded/stripped)",
                    f"{reformatted:,}")
        report.step5_stats.append(
            ("zip_code", reformatted, "e.g. `94061.0` → `94061`")
        )

    # installer_name: treat "No Match" as null (after title-casing)
    if "installer_name" in df.columns:
        mask = df["installer_name"].isin(["No Match"])
        n = int(mask.sum())
        df.loc[mask, "installer_name"] = np.nan
        if n > 0:
            logger.info("  installer_name: %s 'No Match' values -> NULL", f"{n:,}")

    return df


def _is_string_like(series: pd.Series) -> bool:
    """True if the column holds text (old object dtype OR new pandas StringDtype)."""
    return (
        pd.api.types.is_object_dtype(series)
        or pd.api.types.is_string_dtype(series)
    )


def step6_standardize_unknowns(df: pd.DataFrame, report: Report) -> pd.DataFrame:
    """Replace known unknown markers with NaN in string columns."""
    logger.info("\n=== STEP 6: Standardize known unknowns ===")

    for col in df.columns:
        if not _is_string_like(df[col]):
            continue
        before_nulls = df[col].isna().sum()
        df[col] = df[col].replace(KNOWN_UNKNOWNS, np.nan)
        after_nulls = df[col].isna().sum()
        n = int(after_nulls - before_nulls)
        if n > 0:
            logger.info("  %s: %s replacements", col, f"{n:,}")
            report.step6_stats.append((col, n))

    return df


def step7_validate(df: pd.DataFrame, report: Report) -> None:
    """Run all final validations and log them."""
    logger.info("\n=== STEP 7: Final validation ===")

    final_n = len(df)
    pct = final_n / report.n_rows_original * 100 if report.n_rows_original else 0
    logger.info("  Final rows: %s (%.1f%% of original)", f"{final_n:,}", pct)
    logger.info("  Columns: %s", len(df.columns))

    # No -1 sentinels
    has_neg_one = False
    for col in df.columns:
        vals = df[col]
        if pd.api.types.is_numeric_dtype(vals):
            if (vals == -1).any():
                has_neg_one = True
                logger.warning("  FAIL: -1 found in %s", col)
        else:
            if (vals.astype(str) == "-1").any():
                has_neg_one = True
                logger.warning("  FAIL: '-1' found in %s", col)
    report.validations["Sentinel check (no -1 remaining)"] = not has_neg_one

    # zip_code format
    if "zip_code" in df.columns:
        zips = df["zip_code"].dropna()
        bad_format = zips[~zips.astype(str).str.match(r"^\d{5}$")]
        zip_ok = len(bad_format) == 0
        report.validations["Zip format (5-digit string or null)"] = zip_ok
        if not zip_ok:
            logger.warning("  FAIL: %s zip_codes not 5 digits (e.g. %s)",
                           f"{len(bad_format):,}", list(bad_format.head(3)))

    # price_per_watt range
    if "price_per_watt" in df.columns:
        ppw = df["price_per_watt"].dropna()
        bad_ppw = ppw[(ppw < PRICE_PER_WATT_MIN) | (ppw > PRICE_PER_WATT_MAX)]
        ppw_ok = len(bad_ppw) == 0
        report.validations[
            f"Price range (${PRICE_PER_WATT_MIN:.2f} <= $/W <= ${PRICE_PER_WATT_MAX:.2f})"
        ] = ppw_ok

    # PV_system_size_DC range
    if "PV_system_size_DC" in df.columns:
        sizes = df["PV_system_size_DC"].dropna()
        bad_size = sizes[(sizes <= 0) | (sizes > MAX_RESIDENTIAL_KW)]
        size_ok = len(bad_size) == 0
        report.validations[
            f"System size range (0 < kW <= {MAX_RESIDENTIAL_KW})"
        ] = size_ok

    # Report per-column null rates (sorted desc)
    logger.info("  Null rates by column (sorted desc):")
    nulls = df.isna().sum().sort_values(ascending=False)
    for col, n in nulls.items():
        pct = n / final_n * 100 if final_n else 0
        logger.info("    %-35s  %10s  (%.2f%%)", col, f"{n:,}", pct)

    # Numeric summaries
    num_cols = df.select_dtypes(include="number").columns
    if len(num_cols):
        logger.info("  Numeric column summaries (min / median / max / IQR):")
        for col in num_cols:
            s = df[col].dropna()
            if len(s) == 0:
                continue
            q1, q3 = s.quantile([0.25, 0.75])
            logger.info(
                "    %-35s  min=%.3f  med=%.3f  max=%.3f  IQR=%.3f",
                col, s.min(), s.median(), s.max(), q3 - q1,
            )

    # Cardinality of categoricals (covers both old object and new str dtypes)
    cat_cols = [c for c in df.columns if _is_string_like(df[c])]
    if len(cat_cols):
        logger.info("  Categorical cardinality:")
        for col in cat_cols:
            n_unique = df[col].nunique(dropna=True)
            logger.info("    %-35s  %s unique", col, f"{n_unique:,}")

    report.final_df = df


def step8_write(df: pd.DataFrame, output_path: Path) -> None:
    """Write the cleaned DataFrame to CSV."""
    logger.info("\n=== STEP 8: Write output ===")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8", na_rep="")
    size_mb = output_path.stat().st_size / 1e6
    logger.info("Wrote %s (%.1f MB, %s rows)",
                output_path, size_mb, f"{len(df):,}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    input_path: Path,
    output_path: Path,
    report_path: Path,
    chunksize: int | None = None,
    dry_run: bool = False,
) -> Report:
    df = load_raw(input_path, chunksize=chunksize)
    report = Report(input_path, len(df), len(df.columns))
    report.track_step("Step 0 (load raw)", len(df), len(df))

    df = step1_replace_sentinels(df, report)
    df = step2_drop_missing_economics(df, report)
    df = step3_filter_residential(df, report)
    df = step4_price_outliers(df, report)
    df = step5_case_normalization(df, report)
    df = step6_standardize_unknowns(df, report)
    step7_validate(df, report)

    if dry_run:
        logger.info("\n--dry-run set; skipping CSV write")
    else:
        step8_write(df, output_path)

    report.write_markdown(report_path)

    # Final summary table
    logger.info("\n%s", "=" * 72)
    logger.info("PIPELINE SUMMARY")
    logger.info("%s", "=" * 72)
    logger.info("%-40s  %12s  %12s  %12s  %8s",
                "Step", "Before", "Removed", "After", "% orig")
    logger.info("%s", "-" * 72)
    for name, before, removed, after in report.step_rows:
        pct = after / report.n_rows_original * 100 if report.n_rows_original else 0
        logger.info("%-40s  %12s  %12s  %12s  %7.1f%%",
                    name, f"{before:,}", f"{removed:,}", f"{after:,}", pct)
    logger.info("%s", "=" * 72)

    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean the LBNL Tracking the Sun dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", type=Path, required=True,
                        help="Path to raw TTS CSV")
    parser.add_argument("--output", type=Path, default=Path("data/tts_cleaned.csv"),
                        help="Where to write the cleaned CSV")
    parser.add_argument("--report", type=Path,
                        default=Path("etl/cleaning_notes.md"),
                        help="Where to write the Markdown report")
    parser.add_argument("--log", type=Path, default=Path("etl/cleaning.log"),
                        help="Where to write the run log")
    parser.add_argument("--chunksize", type=int, default=None,
                        help="Read the CSV in chunks of this many rows (for low-RAM machines)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run all steps but skip the CSV write")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show DEBUG-level logs on the console")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    setup_logging(args.log, verbose=args.verbose)

    if not args.input.exists():
        logger.error("Input not found: %s", args.input)
        return 1

    try:
        run_pipeline(
            input_path=args.input,
            output_path=args.output,
            report_path=args.report,
            chunksize=args.chunksize,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
