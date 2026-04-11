"""
solar_nem.py -- Net Energy Metering (NEM) export compensation module.

Determines how much a solar homeowner gets paid (or credited) for excess
electricity exported to the grid.  Export compensation varies dramatically
by state and has a first-order impact on payback period:

    * 1:1 net metering (e.g. NJ, NY) credits exports at the full retail rate,
      making solar highly attractive.
    * Reduced-rate NEM (e.g. IN, FL) credits a fraction of retail, lowering
      savings by 15-30%.
    * California's NEM 3.0 (eff. 2023-04-15) uses time-varying avoided-cost
      rates that can be 3x higher in the evening peak than at midday, so
      battery storage becomes essential.

This module centralises that logic so the rest of the pipeline can call
``get_export_value()`` with a state code and retail rate and receive a
consistent ``ExportValueResult``.

# NEM policies change frequently and this module should be reviewed annually.
# TODO: Integrate with DSIRE (https://www.dsireusa.org/) for authoritative
#       state-level policy data and automatic updates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache for the NEM 3.0 CSV (loaded lazily on first use)
# ---------------------------------------------------------------------------
_nem3_data: np.ndarray | None = None  # shape (288, 3): month, hour, export_rate

_DATA_DIR = Path(__file__).resolve().parent / "data"

# ---------------------------------------------------------------------------
# State policy look-up tables
# ---------------------------------------------------------------------------

# States that currently offer full 1:1 retail-rate net metering for
# residential solar.  This is the most favourable policy for homeowners.
NEM_1_FOR_1_STATES: set[str] = {
    "NJ", "NY", "MA", "MD", "CT", "RI", "VT", "NH", "ME",
    "PA", "OH", "IL", "MN", "WI", "CO", "OR", "DC",
    "NM", "AZ", "NV", "MT", "IA", "MO",
}

# States with reduced or value-of-solar export rates.
# The float value is the approximate multiplier of the retail rate.
REDUCED_NEM_STATES: dict[str, float] = {
    "IN": 0.75,   # Indiana: ~75% of retail
    "SC": 0.80,   # South Carolina
    "FL": 0.85,   # Florida
    "NC": 0.80,   # North Carolina
    "GA": 0.70,   # Georgia
    "AL": 0.70,   # Alabama
    "MS": 0.70,   # Mississippi
    "LA": 0.75,   # Louisiana
    "AR": 0.75,   # Arkansas
    "KY": 0.75,   # Kentucky
    "TN": 0.75,   # Tennessee
    "WV": 0.70,   # West Virginia
    "HI": 0.80,   # Hawaii (varies by island/program)
    "UT": 0.90,   # Utah: export credit schedule
    "ID": 0.85,   # Idaho
}

# Deregulated / competitive retail markets where export compensation is
# utility-specific and hard to generalise.
DEREGULATED_STATES: set[str] = {"TX", "OK"}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ExportValueResult:
    """Result of an export-value look-up for a given state and rate."""

    avg_export_rate: float   # $/kWh average export compensation
    policy_name: str         # e.g. "NEM 3.0", "1:1 Net Metering", "Estimated"
    explanation: str         # Human-readable description for the report
    state: str
    is_exact: bool           # True if we have specific policy data, False if estimated


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_nem3_csv() -> np.ndarray:
    """Load and cache the NEM 3.0 avoided-cost rates from CSV.

    Returns
    -------
    np.ndarray
        Array of shape (288, 3) with columns [month, hour, export_rate].
        288 rows = 12 months x 24 hours.
    """
    global _nem3_data
    if _nem3_data is not None:
        return _nem3_data

    csv_path = _DATA_DIR / "nem3_acc_2025.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"NEM 3.0 rate file not found at {csv_path}.  "
            "Ensure data/nem3_acc_2025.csv is present."
        )

    # Read CSV using pandas — handles # comment lines cleanly
    import pandas as pd
    df = pd.read_csv(csv_path, comment="#")
    _nem3_data = df[["month", "hour", "export_rate"]].values.astype(np.float64)
    logger.debug("Loaded NEM 3.0 rate table: %d rows from %s", len(_nem3_data), csv_path)
    return _nem3_data


def _nem3_weighted_average(hourly_production: np.ndarray | None) -> float:
    """Compute the production-weighted average NEM 3.0 export rate.

    Parameters
    ----------
    hourly_production : np.ndarray or None
        If provided, must be length 8760 (one value per hour of a standard
        year).  Production values weight each hour's export rate so that
        hours when the system produces more power count more.
        If None, returns the simple (unweighted) average of all 288 rate
        entries.

    Returns
    -------
    float
        Weighted (or simple) average export rate in $/kWh.
    """
    rates = _load_nem3_csv()  # (288, 3): month, hour, export_rate

    if hourly_production is None:
        return float(rates[:, 2].mean())

    # Build a full 8760-length export-rate vector by mapping each hour of
    # the year to its (month, hour) bin in the rate table.
    if len(hourly_production) != 8760:
        raise ValueError(
            f"hourly_production must have 8760 elements, got {len(hourly_production)}"
        )

    # Build a look-up dict: (month, hour) -> export_rate
    rate_lookup: dict[tuple[int, int], float] = {}
    for row in rates:
        month_key = int(row[0])
        hour_key = int(row[1])
        rate_lookup[(month_key, hour_key)] = float(row[2])

    # Map each of the 8760 hours to its month and hour-of-day.
    # Standard year: Jan=31, Feb=28, ..., Dec=31
    days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    export_rates_8760 = np.empty(8760, dtype=np.float64)

    idx = 0
    for m, ndays in enumerate(days_in_month, start=1):
        for _day in range(ndays):
            for h in range(24):
                export_rates_8760[idx] = rate_lookup.get((m, h), 0.0)
                idx += 1

    total_production = hourly_production.sum()
    if total_production == 0:
        return float(rates[:, 2].mean())

    weighted_avg = float(
        (hourly_production * export_rates_8760).sum() / total_production
    )
    return weighted_avg


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_export_value(
    state: str,
    retail_rate: float,
    install_date: date = date.today(),
    hourly_production: np.ndarray | None = None,
    hourly_rates: np.ndarray | None = None,
) -> ExportValueResult:
    """Determine the export compensation rate for a residential solar system.

    The function checks state-level NEM policies in priority order:

    1. **California NEM 3.0** -- time-varying avoided-cost rates (post
       2023-04-15 interconnections).
    2. **1:1 net metering** -- full retail credit (NJ, NY, MA, etc.).
    3. **Reduced NEM** -- a fraction of retail (IN, FL, GA, etc.).
    4. **Deregulated markets** -- utility-specific, estimated at 50% of
       retail (TX, OK).
    5. **Default** -- 75% of retail with a logged warning.

    Parameters
    ----------
    state : str
        Two-letter US state abbreviation (uppercase).
    retail_rate : float
        The homeowner's retail electricity rate in $/kWh.
    install_date : date, optional
        System interconnection date.  Determines which NEM vintage applies
        (relevant for CA NEM 2.0 vs 3.0).  Defaults to today.
    hourly_production : np.ndarray or None, optional
        8760-length array of hourly solar production (kWh).  Used for
        California NEM 3.0 to compute a production-weighted average export
        rate.  Ignored for other states.
    hourly_rates : np.ndarray or None, optional
        Reserved for future use (e.g. utility-specific TOU schedules).

    Returns
    -------
    ExportValueResult
        Dataclass with the average export rate, policy name, human-readable
        explanation, state code, and an ``is_exact`` flag.
    """
    state = state.upper().strip()

    # ------------------------------------------------------------------
    # 1. California NEM 3.0 (interconnections on or after 2023-04-15)
    # ------------------------------------------------------------------
    if state == "CA" and install_date >= date(2023, 4, 15):
        avg_rate = _nem3_weighted_average(hourly_production)
        return ExportValueResult(
            avg_export_rate=avg_rate,
            policy_name="NEM 3.0 (CA)",
            explanation=(
                "California NEM 3.0 uses avoided-cost-based export rates. "
                "Evening peak hours (4-9pm) compensate ~3x more than midday. "
                "Rates approximated from CPUC Avoided Cost Calculator 2025."
            ),
            state=state,
            is_exact=False,
        )

    # ------------------------------------------------------------------
    # 2. Full 1:1 retail-rate net metering
    # ------------------------------------------------------------------
    if state in NEM_1_FOR_1_STATES:
        return ExportValueResult(
            avg_export_rate=retail_rate,
            policy_name="1:1 Net Metering",
            explanation=(
                f"{state} currently offers 1:1 retail-rate net metering "
                "for residential solar."
            ),
            state=state,
            is_exact=True,
        )

    # ------------------------------------------------------------------
    # 3. Reduced / value-of-solar NEM
    # ------------------------------------------------------------------
    if state in REDUCED_NEM_STATES:
        multiplier = REDUCED_NEM_STATES[state]
        return ExportValueResult(
            avg_export_rate=retail_rate * multiplier,
            policy_name=f"Reduced NEM ({state})",
            explanation=(
                f"{state} offers an export credit estimated at "
                f"{multiplier:.0%} of the retail rate. Actual compensation "
                "may vary by utility and tariff."
            ),
            state=state,
            is_exact=False,
        )

    # ------------------------------------------------------------------
    # 4. Deregulated / competitive retail markets
    # ------------------------------------------------------------------
    if state in DEREGULATED_STATES:
        return ExportValueResult(
            avg_export_rate=retail_rate * 0.50,
            policy_name=f"Deregulated Market ({state})",
            explanation=(
                f"{state} has a deregulated electricity market where export "
                "compensation varies significantly by retail provider. "
                "The rate used here (~50% of retail) is a rough estimate; "
                "check with your specific utility for actual buy-back rates."
            ),
            state=state,
            is_exact=False,
        )

    # ------------------------------------------------------------------
    # 5. Default / unknown state
    # ------------------------------------------------------------------
    logger.warning(
        "No specific NEM policy data for state '%s'. "
        "Using default estimate of 75%% of retail rate ($%.4f/kWh).",
        state,
        retail_rate * 0.75,
    )
    return ExportValueResult(
        avg_export_rate=retail_rate * 0.75,
        policy_name="Estimated (default)",
        explanation=(
            f"No specific net metering policy data available for {state}. "
            "Export compensation estimated at 75% of the retail rate. "
            "Check DSIRE (https://www.dsireusa.org/) for current policy details."
        ),
        state=state,
        is_exact=False,
    )
