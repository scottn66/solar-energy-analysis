"""
solar_urdb.py -- Utility Rate Database (URDB) client for the solar viability project.

Fetches, parses, and normalises residential electricity rate structures from
the OpenEI URDB (via api.openei.org) and the simpler NREL Utility Rates v3
endpoint.  The module exposes a single high-level entry point, ``get_rate``,
which walks a three-tier fallback chain so callers always receive a usable
rate even when detailed tariff data is unavailable:

    1. Full URDB tariff (TOU / tiered / flat) via ``fetch_rate``
    2. Simple residential average via ``fetch_rate_fast`` (NREL v3)
    3. State-level EIA average from bundled CSV

Usage:
    from solar_urdb import get_rate
    rate = get_rate(lat=37.77, lon=-122.42, state="CA")
    print(rate.flat_rate, rate.source)
"""

from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import requests_cache
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cached HTTP session (SQLite-backed, 7-day TTL)
# ---------------------------------------------------------------------------
_CACHE_DIR = Path.home() / ".solar_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_session = requests_cache.CachedSession(
    str(_CACHE_DIR / "urdb"),
    backend="sqlite",
    expire_after=60 * 60 * 24 * 7,  # 7 days
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_US_MEDIAN_RATE = 0.16  # $/kWh -- rough US median residential rate

# Patterns used to deprioritise niche / pilot tariffs during ranking.
_EXCLUDE_PATTERNS = re.compile(
    r"\b(EV|electric\s+vehicle|solar|net\s+metering"
    r"|time-of-use\s+pilot|experimental)\b",
    re.IGNORECASE,
)

# Patterns that signal a "standard" residential tariff during ranking.
_PREFER_PATTERNS = re.compile(
    r"\b(standard|default|basic|residential\s+service"
    r"|schedule\s+e-1|schedule\s+r)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------
@dataclass
class RateResult:
    """Structured result of a utility rate lookup.

    Attributes
    ----------
    flat_rate : float
        Weighted-average electricity price in $/kWh.  For TOU rates this is
        the simple mean of the 8 760-hour vector; for tiered rates it is
        computed assuming 900 kWh/month consumption.
    hourly_rates : np.ndarray | None
        An 8 760-element array of $/kWh values when the tariff is
        time-of-use, otherwise ``None``.
    fixed_monthly_charge : float
        Fixed monthly customer charge in $/month.
    utility_name : str
        Name of the electric utility.
    rate_name : str
        Tariff schedule name as recorded in the URDB.
    rate_uri : str
        Link to the rate detail page on openei.org.
    source : str
        Which lookup path produced this result.  One of
        ``"urdb_full"``, ``"urdb_tiered_avg"``, ``"nrel_v3"``,
        or ``"eia_state_fallback"``.
    effective_date : date | None
        Start date of the tariff, if known.
    is_tou : bool
        ``True`` when the tariff has time-of-use periods.
    is_tiered : bool
        ``True`` when the tariff has multiple consumption tiers.
    raw : dict
        The original API response dict, preserved for debugging.
    """

    flat_rate: float
    hourly_rates: np.ndarray | None
    fixed_monthly_charge: float
    utility_name: str
    rate_name: str
    rate_uri: str
    source: str
    effective_date: date | None
    is_tou: bool
    is_tiered: bool
    raw: dict = field(repr=False)


class URDBError(Exception):
    """Raised when a URDB / rate lookup fails in an unrecoverable way."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _api_key() -> str:
    """Return the NREL / OpenEI API key from the environment."""
    key = os.environ.get("NREL_API_KEY", "")
    if not key:
        raise URDBError(
            "NREL_API_KEY not set. Add it to your .env file or environment."
        )
    return key


def _epoch_to_date(epoch: int | float | None) -> date | None:
    """Convert an epoch-seconds timestamp to a :class:`date`, or ``None``."""
    if epoch is None or epoch == 0:
        return None
    try:
        return datetime.utcfromtimestamp(epoch).date()
    except (OSError, ValueError, OverflowError):
        return None


def _is_rate_current(item: dict) -> bool:
    """Return ``True`` when *item* has no end date or its end date is in the
    future (i.e. the tariff is still active).
    """
    enddate = item.get("enddate")
    if enddate is None or enddate == 0:
        return True
    try:
        return datetime.utcfromtimestamp(enddate).date() > date.today()
    except (OSError, ValueError, OverflowError):
        return True  # cannot parse -- keep it


def _rate_sort_key(item: dict) -> tuple:
    """Return a sort key for ranking candidate tariffs.

    Higher values are preferred.  The tuple is:

    1. ``1`` if ``is_default`` is truthy, else ``0``
    2. ``1`` if the name matches standard-tariff patterns, else ``0``
    3. ``startdate`` epoch (larger = more recent, preferred)
    """
    is_default = 1 if item.get("is_default") else 0
    name = item.get("name", "")
    name_preferred = 1 if _PREFER_PATTERNS.search(name) else 0
    startdate = item.get("startdate") or 0
    return (is_default, name_preferred, startdate)


def _weighted_tiered_rate(tiers: list[dict], monthly_kwh: float = 900.0) -> float:
    """Compute a consumption-weighted average rate for a tiered tariff.

    Parameters
    ----------
    tiers : list[dict]
        Each dict must contain ``"rate"`` ($/kWh) and optionally ``"max"``
        (kWh ceiling for the tier).  Tiers are assumed to be ordered from
        lowest to highest consumption.
    monthly_kwh : float
        Assumed monthly household consumption (default 900 kWh -- close to the
        US residential average).

    Returns
    -------
    float
        Weighted average $/kWh rate.

    Notes
    -----
    The 900 kWh/month assumption is documented in the EIA Residential Energy
    Consumption Survey (RECS 2020).  It provides a reasonable single-point
    estimate when the actual load profile is unknown.  For sites with
    significantly different consumption the effective rate will differ.
    """
    remaining = monthly_kwh
    total_cost = 0.0
    total_kwh = 0.0

    for tier in tiers:
        if remaining <= 0:
            break
        rate = tier.get("rate", 0.0) or 0.0
        ceiling = tier.get("max")
        if ceiling is None or ceiling == 0:
            # Last / uncapped tier -- absorb everything remaining.
            kwh_in_tier = remaining
        else:
            kwh_in_tier = min(remaining, ceiling)
        total_cost += kwh_in_tier * rate
        total_kwh += kwh_in_tier
        remaining -= kwh_in_tier

    if total_kwh == 0:
        return 0.0
    return total_cost / total_kwh


def _build_8760_tou(
    weekday_sched: list[list[int]],
    weekend_sched: list[list[int]],
    rate_structure: list[list[dict]],
) -> np.ndarray:
    """Build an 8 760-hour rate vector from TOU schedule matrices.

    Parameters
    ----------
    weekday_sched : list[list[int]]
        12x24 matrix (months x hours-of-day) of period indices for weekdays
        (Monday--Friday).
    weekend_sched : list[list[int]]
        12x24 matrix of period indices for weekends (Saturday--Sunday).
    rate_structure : list[list[dict]]
        List of periods, each containing a list of tier dicts.  Only
        ``tier[0]["rate"]`` is used (tier-0 simplification for TOU).

    Returns
    -------
    np.ndarray
        Array of shape ``(8760,)`` with $/kWh for each hour of the year.

    Notes
    -----
    Uses 2024 as the reference calendar year (leap year, 8 784 hours) but
    truncates / pads to exactly 8 760 entries for compatibility with
    standard solar-production arrays.  Monday--Friday map to the weekday
    schedule; Saturday and Sunday map to the weekend schedule.
    """
    import calendar

    rates = np.zeros(8760, dtype=np.float64)
    hour_index = 0

    for month_0 in range(12):  # 0-indexed month
        # Number of days in this month (2024 is a leap year).
        year = 2024
        month_1 = month_0 + 1  # calendar module uses 1-indexed months
        days_in_month = calendar.monthrange(year, month_1)[1]

        for day in range(1, days_in_month + 1):
            # day-of-week: 0=Monday ... 6=Sunday  (Python convention)
            dow = calendar.weekday(year, month_1, day)
            is_weekend = dow >= 5  # Saturday=5, Sunday=6

            sched = weekend_sched if is_weekend else weekday_sched

            for hour in range(24):
                if hour_index >= 8760:
                    break  # cap at standard year length

                period_idx = sched[month_0][hour]
                # Look up tier-0 rate for the period.
                try:
                    rate_val = rate_structure[period_idx][0].get("rate", 0.0) or 0.0
                except (IndexError, TypeError):
                    rate_val = 0.0

                rates[hour_index] = rate_val
                hour_index += 1

    return rates[:8760]


def _load_eia_state_rates() -> dict[str, float]:
    """Load state-level residential electricity rates from the bundled CSV.

    File path: ``data/eia_state_rates_2025.csv`` relative to this module.

    Returns
    -------
    dict[str, float]
        Mapping of two-letter state abbreviation to $/kWh rate.
    """
    csv_path = Path(__file__).resolve().parent / "data" / "eia_state_rates_2025.csv"
    rates: dict[str, float] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        lines = (line for line in fh if not line.startswith("#"))
        reader = csv.DictReader(lines)
        for row in reader:
            state = row.get("state", "").strip().upper()
            try:
                rates[state] = float(row["rate_dollars_per_kwh"])
            except (KeyError, ValueError):
                continue
    return rates


_STATE_IN_NAME = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND",
    "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}


def _utility_label_for_state(name: str, state: str) -> str:
    """If URDB tagged a utility with the wrong state, fix the label."""
    if not name or not state:
        return name or ""
    st = state.strip().upper()
    for word, abbr in _STATE_IN_NAME.items():
        needle = f"({word})"
        if needle in name and abbr != st:
            inv = {v: k for k, v in _STATE_IN_NAME.items()}
            return name.replace(needle, f"({inv.get(st, st)})")
    return name


def _load_bundled_tou(utility_name: str, state: str | None = None) -> RateResult | None:
    """Load a bundled rate schedule for a known utility.

    Falls back to bundled data/utility_tou_schedules.csv when the URDB has
    stale rates.  Covers the three CA IOUs (PG&E, SCE, SDG&E) and the four
    utilities serving Oregon — Portland General Electric, Pacific Power
    (PacifiCorp), Central Electric Cooperative, and Midstate Electric
    Cooperative (the latter two serve Central Oregon around Redmond,
    Sisters, La Pine, and Sunriver).

    The bundled file contains peak/off-peak rates by season.  This function
    expands them into a full 8760-hour rate vector (summer = Jun-Sep,
    winter = Oct-May).  Schedules with a single year-round price (the
    Oregon default tariffs are flat, unlike California's default-TOU) are
    returned with ``is_tou=False`` and no hourly vector.

    Returns None if the utility isn't in the bundled file.
    """
    csv_path = Path(__file__).resolve().parent / "data" / "utility_tou_schedules.csv"
    if not csv_path.exists():
        return None

    # Normalize utility name for matching.  "Portland General" must be
    # checked before the PG&E patterns — Oregon's PGE is a different company
    # from California's PG&E.
    name_lower = utility_name.lower()
    state_upper = (state or "").strip().upper()
    utility_key = None
    if "portland general" in name_lower:
        utility_key = "PGE-OR"
    elif "pacific gas" in name_lower or "pg&e" in name_lower or "pg+e" in name_lower:
        utility_key = "PG&E"
    elif "southern california edison" in name_lower or "sce" in name_lower:
        utility_key = "SCE"
    elif "san diego" in name_lower or "sdge" in name_lower or "sdg&e" in name_lower:
        utility_key = "SDG&E"
    elif "pacific power" in name_lower and state_upper in ("", "OR"):
        # Pacific Power is PacifiCorp's brand in OR, WA, and far-northern
        # CA (URDB uses names like "Pacific Power (California)"), and the
        # bundled schedule is Oregon's — gate it to Oregon quotes.
        utility_key = "PacifiCorp"
    elif "pacificorp" in name_lower:
        # Bare "PacifiCorp" also covers Rocky Mountain Power (UT/WY/ID),
        # whose rates differ — only apply the Oregon schedule when the
        # quote is actually in Oregon.
        if state_upper in ("", "OR"):
            utility_key = "PacifiCorp"
    elif "central electric" in name_lower and state_upper in ("", "OR"):
        utility_key = "CEC-OR"
    elif (
        "midstate electric" in name_lower or "mid-state electric" in name_lower
    ) and state_upper in ("", "OR"):
        utility_key = "Midstate-OR"

    if utility_key is None:
        return None

    # Parse the CSV
    rows_by_season: dict[str, list[dict]] = {"summer": [], "winter": []}
    rate_name = ""
    fixed_monthly = 0.0
    effective = date(2024, 1, 1)
    with open(csv_path, newline="", encoding="utf-8") as fh:
        lines = (line for line in fh if not line.startswith("#"))
        reader = csv.DictReader(lines)
        for row in reader:
            if row.get("utility", "").strip() == utility_key:
                rate_name = row.get("rate_name", "")
                fixed_monthly = float(row.get("fixed_monthly", 0))
                eff_raw = (row.get("effective") or "").strip()
                if eff_raw:
                    try:
                        parts = eff_raw.split("-")
                        effective = date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, 1)
                    except (ValueError, IndexError):
                        pass
                season = row.get("season", "").strip().lower()
                if season in rows_by_season:
                    rows_by_season[season].append({
                        "start": int(row["start_hour"]),
                        "end": int(row["end_hour"]),
                        "rate": float(row["rate_dollars_per_kwh"]),
                    })

    if not rows_by_season["summer"] and not rows_by_season["winter"]:
        return None

    # Build 8760-hour vector using a standard non-leap year
    # Summer months: June (5) through September (8), 0-indexed
    summer_months = {5, 6, 7, 8}
    days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]  # standard year
    hourly_rates = np.zeros(8760, dtype=np.float64)
    hour_idx = 0

    for month in range(12):
        season = "summer" if month in summer_months else "winter"
        period_rows = rows_by_season[season]
        for day in range(days_in_month[month]):
            for hour in range(24):
                rate = 0.0
                for pr in period_rows:
                    if pr["start"] <= hour < pr["end"]:
                        rate = pr["rate"]
                        break
                hourly_rates[hour_idx] = rate
                hour_idx += 1

    flat_avg = float(np.mean(hourly_rates))

    # A schedule whose price never varies by hour (the Oregon default
    # tariffs, unlike California's default-TOU) is a flat rate, not TOU.
    is_flat = bool(np.max(hourly_rates) == np.min(hourly_rates))

    logger.info(
        "Loaded bundled %s schedule: %s %s (avg $%.4f/kWh, peak $%.4f, off-peak $%.4f)",
        "flat" if is_flat else "TOU",
        utility_key, rate_name, flat_avg,
        float(np.max(hourly_rates)), float(np.min(hourly_rates[hourly_rates > 0])),
    )

    return RateResult(
        flat_rate=flat_avg,
        hourly_rates=None if is_flat else hourly_rates,
        fixed_monthly_charge=fixed_monthly,
        utility_name=utility_name,
        rate_name=f"{rate_name} (bundled {effective.year} schedule)",
        rate_uri="",
        source="bundled_tou",
        effective_date=effective,
        is_tou=not is_flat,
        is_tiered=False,
        raw={},
    )


# ---------------------------------------------------------------------------
# Public API: fetch_rate_fast (NREL Utility Rates v3 -- simple fallback)
# ---------------------------------------------------------------------------
def fetch_rate_fast(lat: float, lon: float) -> RateResult:
    """Fetch a simple residential electricity rate from the NREL v3 endpoint.

    This is a lightweight fallback that returns a single flat rate for the
    location.  It does **not** capture TOU periods, demand charges, or
    tiered structures.

    Parameters
    ----------
    lat, lon : float
        Geographic coordinates (decimal degrees, WGS-84).

    Returns
    -------
    RateResult
        With ``source="nrel_v3"``, ``is_tou=False``, ``is_tiered=False``.

    Raises
    ------
    URDBError
        On HTTP errors or missing data in the API response.
    """
    key = _api_key()
    url = "https://developer.nrel.gov/api/utility_rates/v3.json"
    params = {"lat": lat, "lon": lon, "api_key": key}

    try:
        resp = _session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise URDBError(f"NREL v3 request failed: {exc}") from exc

    outputs = data.get("outputs", {})
    flat_rate = outputs.get("residential")
    if flat_rate is None:
        raise URDBError(
            "NREL v3 response missing 'residential' rate. "
            f"Response keys: {list(data.keys())}"
        )

    utility_name = outputs.get("utility_name", "Unknown")
    flat_rate = float(flat_rate)

    logger.info(
        "fetch_rate_fast: flat_rate=%.4f, utility=%s (source=nrel_v3)",
        flat_rate,
        utility_name,
    )

    return RateResult(
        flat_rate=flat_rate,
        hourly_rates=None,
        fixed_monthly_charge=0.0,
        utility_name=utility_name,
        rate_name="NREL v3 residential average",
        rate_uri="",
        source="nrel_v3",
        effective_date=None,
        is_tou=False,
        is_tiered=False,
        raw=data,
    )


# ---------------------------------------------------------------------------
# Public API: fetch_rate (full URDB tariff parsing)
# ---------------------------------------------------------------------------
def fetch_rate(
    lat: float,
    lon: float,
    sector: str = "Residential",
    prefer_default: bool = True,
) -> RateResult:
    """Fetch and parse a detailed utility rate from the OpenEI URDB.

    Workflow
    --------
    1. Query ``api.openei.org/utility_rates`` with ``detail=full`` to
       retrieve up to 50 tariff records near (*lat*, *lon*).
    2. Filter out expired, unapproved, and niche/pilot tariffs.
    3. Rank survivors by ``is_default``, name heuristics, and recency.
    4. Parse the top-ranked tariff.  Three shapes are handled:

       * **Flat** -- single tier, single period.  ``source="urdb_full"``.
       * **Tiered** -- multiple consumption tiers.  A weighted-average rate
         is computed assuming **900 kWh/month** household consumption
         (US residential average per EIA RECS 2020).
         ``source="urdb_tiered_avg"``.
       * **Time-of-use (TOU)** -- the ``energyweekdayschedule`` and
         ``energyweekendschedule`` 12x24 matrices are expanded into a
         full 8 760-hour rate vector using 2024 as the reference
         calendar year.  ``source="urdb_full"``, ``is_tou=True``.

    Parameters
    ----------
    lat, lon : float
        Geographic coordinates.
    sector : str
        Rate sector (``"Residential"``, ``"Commercial"``, etc.).
    prefer_default : bool
        When ``True`` (default), tariffs flagged ``is_default`` in the URDB
        are ranked highest.

    Returns
    -------
    RateResult

    Raises
    ------
    URDBError
        When no valid rates are found or the API request fails.
    """
    key = _api_key()
    url = "https://api.openei.org/utility_rates"
    params = {
        "version": "latest",
        "format": "json",
        "detail": "full",
        "sector": sector,
        "lat": lat,
        "lon": lon,
        "limit": 50,
        "api_key": key,
    }

    try:
        resp = _session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise URDBError(f"OpenEI URDB request failed: {exc}") from exc

    items: list[dict] = data.get("items", [])
    if not items:
        raise URDBError(
            f"No utility rates found in the URDB for lat={lat}, lon={lon}, "
            f"sector={sector}.  The location may be outside utility service "
            "territories covered by OpenEI."
        )

    # ------------------------------------------------------------------
    # Step 2: Filter
    # ------------------------------------------------------------------
    # Keep only current, approved tariffs.
    candidates = [
        it for it in items if _is_rate_current(it) and it.get("approved", True)
    ]

    # Separate "niche" tariffs (EV, solar, pilot, etc.) so we can use
    # them as a last resort if no standard tariff survives filtering.
    standard = [it for it in candidates if not _EXCLUDE_PATTERNS.search(it.get("name", ""))]
    if standard:
        candidates = standard

    if not candidates:
        # All rates are expired — many large utilities (e.g., PG&E) have
        # stale enddate fields in the URDB.  Relax the filter: accept
        # approved rates regardless of enddate, preferring the most recent.
        logger.warning(
            "All %d URDB rates are expired; relaxing enddate filter", len(items)
        )
        candidates = [it for it in items if it.get("approved", True)]
        standard = [it for it in candidates if not _EXCLUDE_PATTERNS.search(it.get("name", ""))]
        if standard:
            candidates = standard
        if not candidates:
            raise URDBError(
                f"All {len(items)} URDB rates for lat={lat}, lon={lon} are "
                "unapproved or excluded by name filters."
            )

    # ------------------------------------------------------------------
    # Step 3: Rank
    # ------------------------------------------------------------------
    candidates.sort(key=_rate_sort_key, reverse=True)
    best = candidates[0]

    # ------------------------------------------------------------------
    # Step 4: Parse the best tariff
    # ------------------------------------------------------------------
    rate_structure: list[list[dict]] = best.get("energyratestructure", [])
    weekday_sched: list[list[int]] | None = best.get("energyweekdayschedule")
    weekend_sched: list[list[int]] | None = best.get("energyweekendschedule")

    is_tou = False
    is_tiered = False
    hourly_rates: np.ndarray | None = None
    source = "urdb_full"

    # Detect TOU: presence of both schedule matrices with more than one
    # distinct period index.
    has_tou_schedules = (
        weekday_sched is not None
        and weekend_sched is not None
        and len(weekday_sched) == 12
        and len(weekend_sched) == 12
    )
    if has_tou_schedules:
        # Count unique period indices across both schedules.
        all_periods: set[int] = set()
        for row in weekday_sched:  # type: ignore[union-attr]
            all_periods.update(row)
        for row in weekend_sched:  # type: ignore[union-attr]
            all_periods.update(row)
        if len(all_periods) > 1:
            is_tou = True

    if is_tou and has_tou_schedules:
        # Build full 8760-hour TOU vector.
        hourly_rates = _build_8760_tou(
            weekday_sched,  # type: ignore[arg-type]
            weekend_sched,  # type: ignore[arg-type]
            rate_structure,
        )
        flat_rate = float(np.mean(hourly_rates))
        source = "urdb_full"
    elif rate_structure:
        # Flat or tiered -- look at the first period.
        first_period = rate_structure[0]
        if len(first_period) > 1:
            is_tiered = True
            flat_rate = _weighted_tiered_rate(first_period, monthly_kwh=900.0)
            source = "urdb_tiered_avg"
        else:
            flat_rate = float((first_period[0].get("rate", 0.0) or 0.0))
            source = "urdb_full"
    else:
        raise URDBError(
            f"Rate '{best.get('name', '?')}' has no energyratestructure."
        )

    # ------------------------------------------------------------------
    # Step 5 & 6: Fixed charge and metadata
    # ------------------------------------------------------------------
    fixed_monthly = float(best.get("fixedchargefirstmeter", 0) or 0)
    utility_name = best.get("utility", "Unknown")
    rate_name = best.get("name", "Unknown")
    startdate = best.get("startdate")
    effective = _epoch_to_date(startdate)

    uri_raw = best.get("uri", "")
    if uri_raw and not uri_raw.startswith("http"):
        rate_uri = "https://openei.org" + uri_raw
    else:
        rate_uri = uri_raw or ""

    logger.info(
        "fetch_rate: flat_rate=%.4f, is_tou=%s, is_tiered=%s, "
        "rate_name='%s' (source=%s)",
        flat_rate,
        is_tou,
        is_tiered,
        rate_name,
        source,
    )

    return RateResult(
        flat_rate=flat_rate,
        hourly_rates=hourly_rates,
        fixed_monthly_charge=fixed_monthly,
        utility_name=utility_name,
        rate_name=rate_name,
        rate_uri=rate_uri,
        source=source,
        effective_date=effective,
        is_tou=is_tou,
        is_tiered=is_tiered,
        raw=best,
    )


# ---------------------------------------------------------------------------
# Public API: get_rate (entry point with fallback chain)
# ---------------------------------------------------------------------------
def get_rate(
    lat: float,
    lon: float,
    state: str | None = None,
    sector: str = "Residential",
) -> RateResult:
    """High-level rate lookup with a three-tier fallback chain.

    The function tries progressively simpler data sources until one succeeds:

    1. **Full URDB tariff** via :func:`fetch_rate` -- detailed TOU / tiered
       parsing from OpenEI.
    2. **NREL v3 simple rate** via :func:`fetch_rate_fast` -- single
       residential average for the location.
    3. **EIA state average** from the bundled
       ``data/eia_state_rates_2025.csv`` file.
    4. **US median** ($0.16/kWh) as absolute last resort.

    Each fallback logs at WARNING level so downstream consumers know that
    precision has been degraded.

    Parameters
    ----------
    lat, lon : float
        Geographic coordinates.
    state : str | None
        Two-letter US state abbreviation.  Used only for the EIA fallback
        (step 3).  If ``None`` and the first two sources fail, the US
        median rate is returned.
    sector : str
        Rate sector passed to :func:`fetch_rate`.

    Returns
    -------
    RateResult
    """
    # --- Fallback 1: Full URDB ---
    try:
        urdb_result = fetch_rate(lat, lon, sector=sector)

        # Staleness guard: if the URDB rate data is more than 3 years old,
        # check against the EIA state average.  URDB has stale entries for
        # many large utilities (e.g., PG&E rates from 2014 at $0.15 when
        # the real current rate is $0.32).  If the EIA rate is significantly
        # higher, use EIA instead — the URDB structure is accurate but the
        # dollar amounts are outdated.
        if state and urdb_result.effective_date:
            from datetime import timedelta
            age = date.today() - urdb_result.effective_date
            if age > timedelta(days=3 * 365):
                # Use live EIA API for the current rate (falls back to CSV internally)
                from solar_eia import get_state_rate
                eia_rate, eia_period, eia_source = get_state_rate(state)

                if eia_rate and eia_rate > urdb_result.flat_rate * 1.5:
                    logger.warning(
                        "URDB rate ($%.4f from %s) is >3 years old and %.0f%% below "
                        "EIA %s rate ($%.4f, %s). Checking for bundled TOU schedule.",
                        urdb_result.flat_rate, urdb_result.effective_date,
                        (1 - urdb_result.flat_rate / eia_rate) * 100,
                        eia_source, eia_rate, eia_period,
                    )
                    # Try bundled TOU first — preserves peak/off-peak structure
                    bundled = _load_bundled_tou(urdb_result.utility_name, state=state)
                    if bundled is not None:
                        logger.info(
                            "Using bundled TOU schedule for %s ($%.4f avg)",
                            bundled.rate_name, bundled.flat_rate,
                        )
                        return bundled

                    # Fall back to flat EIA rate (now potentially live data)
                    return RateResult(
                        flat_rate=eia_rate,
                        hourly_rates=None,
                        fixed_monthly_charge=urdb_result.fixed_monthly_charge,
                        utility_name=_utility_label_for_state(
                            urdb_result.utility_name, state
                        ),
                        rate_name=f"EIA {state.upper()} {eia_period} (URDB was stale: {urdb_result.rate_name})",
                        rate_uri="https://www.eia.gov/electricity/monthly/epm_table_5_6_a.html",
                        source=eia_source,
                        effective_date=None,
                        is_tou=False,
                        is_tiered=False,
                        raw=urdb_result.raw,
                    )

        return urdb_result
    except Exception as exc:
        logger.warning(
            "fetch_rate failed (lat=%.4f, lon=%.4f): %s. "
            "Falling back to NREL v3.",
            lat,
            lon,
            exc,
        )

    # --- Fallback 2: NREL v3 simple rate ---
    try:
        return fetch_rate_fast(lat, lon)
    except Exception as exc:
        logger.warning(
            "fetch_rate_fast failed (lat=%.4f, lon=%.4f): %s. "
            "Falling back to EIA state average.",
            lat,
            lon,
            exc,
        )

    # --- Fallback 3: EIA state-level average (live API → bundled CSV) ---
    if state:
        state_upper = state.strip().upper()
        try:
            from solar_eia import get_state_rate
            rate, period, source = get_state_rate(state_upper)
            if rate is not None:
                logger.warning(
                    "Using EIA fallback for %s: $%.4f/kWh (%s, %s).",
                    state_upper, rate, period, source,
                )
                return RateResult(
                    flat_rate=rate,
                    hourly_rates=None,
                    fixed_monthly_charge=0.0,
                    utility_name=f"EIA state average ({state_upper})",
                    rate_name=f"EIA {period} residential -- {state_upper}",
                    rate_uri="https://www.eia.gov/electricity/monthly/epm_table_5_6_a.html",
                    source=source,
                    effective_date=None,
                    is_tou=False,
                    is_tiered=False,
                    raw={"state": state_upper, "rate": rate, "period": period},
                )
            else:
                logger.warning(
                    "State '%s' not found in EIA data; using US median.", state_upper
                )
        except Exception as exc:
            logger.warning("Failed to load EIA state rates CSV: %s", exc)

    # --- Fallback 4: US median ---
    logger.warning(
        "All rate sources exhausted for lat=%.4f, lon=%.4f. "
        "Returning US median rate $%.2f/kWh.",
        lat,
        lon,
        _US_MEDIAN_RATE,
    )
    return RateResult(
        flat_rate=_US_MEDIAN_RATE,
        hourly_rates=None,
        fixed_monthly_charge=0.0,
        utility_name="US Median (fallback)",
        rate_name="US median residential rate",
        rate_uri="https://www.eia.gov/electricity/monthly/epm_table_5_6_a.html",
        source="eia_state_fallback",
        effective_date=None,
        is_tou=False,
        is_tiered=False,
        raw={"fallback": "us_median", "rate": _US_MEDIAN_RATE},
    )
