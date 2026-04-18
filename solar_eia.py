"""
solar_eia.py — Live electricity rate lookup from EIA Open Data API.

Fetches current residential electricity prices by state from the
U.S. Energy Information Administration (EIA) API v2.

Falls back gracefully to the bundled CSV when the API is unavailable.

Usage:
    from solar_eia import get_state_rate
    rate = get_state_rate("CA")  # Returns 0.3029 ($/kWh)
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

import requests_cache
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cached session — 30-day TTL (state rates change slowly)
# ---------------------------------------------------------------------------
_CACHE_DIR = Path.home() / ".solar_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_session = requests_cache.CachedSession(
    str(_CACHE_DIR / "eia"),
    backend="sqlite",
    expire_after=60 * 60 * 24 * 30,  # 30 days
)

# ---------------------------------------------------------------------------
# Bundled fallback
# ---------------------------------------------------------------------------
_bundled_rates: dict[str, float] | None = None


def _load_bundled_rates() -> dict[str, float]:
    """Load rates from data/eia_state_rates_2025.csv as fallback."""
    global _bundled_rates
    if _bundled_rates is not None:
        return _bundled_rates

    csv_path = Path(__file__).resolve().parent / "data" / "eia_state_rates_2025.csv"
    _bundled_rates = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        lines = (line for line in fh if not line.startswith("#"))
        reader = csv.DictReader(lines)
        for row in reader:
            state = row.get("state", "").strip().upper()
            try:
                _bundled_rates[state] = float(row["rate_dollars_per_kwh"])
            except (KeyError, ValueError):
                continue
    return _bundled_rates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_state_rate(state: str) -> tuple[float, str, str]:
    """
    Get the current residential electricity rate for a US state.

    Tries the EIA API first (live data, cached 30 days), then falls
    back to the bundled 2025 CSV snapshot.

    Parameters
    ----------
    state : str
        Two-letter US state abbreviation (e.g., "CA", "TX").

    Returns
    -------
    tuple[float, str, str]
        (rate_dollars_per_kwh, period, source)
        - rate: the price in $/kWh
        - period: the month the data is from (e.g., "2026-01")
        - source: "eia_live" or "eia_bundled_2025"
    """
    state = state.strip().upper()
    api_key = os.environ.get("EIA_API_KEY", "")

    if api_key:
        try:
            rate, period = _fetch_from_api(state, api_key)
            if rate is not None:
                logger.info(
                    "EIA live rate for %s: $%.4f/kWh (%s)", state, rate, period
                )
                return rate, period, "eia_live"
        except Exception as exc:
            logger.warning("EIA API failed for %s: %s. Using bundled rates.", state, exc)

    # Fallback to bundled CSV
    bundled = _load_bundled_rates()
    rate = bundled.get(state)
    if rate is not None:
        logger.info("EIA bundled rate for %s: $%.4f/kWh (2024 snapshot)", state, rate)
        return rate, "2024", "eia_bundled_2025"

    # Final fallback: US median
    logger.warning("No EIA rate for state '%s'. Using US median $0.16/kWh.", state)
    return 0.16, "unknown", "us_median"


def _fetch_from_api(state: str, api_key: str) -> tuple[float | None, str]:
    """
    Fetch the latest residential electricity price from EIA API v2.

    Endpoint: https://api.eia.gov/v2/electricity/retail-sales/data/
    Returns the most recent monthly residential price for the given state.

    The EIA API can be slow (5-15s response times), so results are cached
    for 30 days via requests_cache.
    """
    url = "https://api.eia.gov/v2/electricity/retail-sales/data/"
    params = {
        "frequency": "monthly",
        "data[0]": "price",
        "facets[sectorid][]": "RES",
        "facets[stateid][]": state,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 1,
        "api_key": api_key,
    }

    resp = _session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    rows = data.get("response", {}).get("data", [])
    if not rows:
        return None, ""

    row = rows[0]
    price_cents = row.get("price")
    period = row.get("period", "")

    if price_cents is None:
        return None, period

    return float(price_cents) / 100.0, period
