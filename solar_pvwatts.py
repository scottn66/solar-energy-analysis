"""
PVWatts API client for the DATA201 Solar Analysis project.

Wraps NREL's PVWatts v8 API to estimate photovoltaic system performance
for a given location and system configuration.  Results are returned as a
PVWattsResult dataclass whose to_dict() output is directly compatible with
the ``row`` dict expected by ``solar_economics.score_site()``.

Quick start::

    from solar_pvwatts import fetch_pvwatts
    result = fetch_pvwatts(lat=40.0, lon=-105.0, system_kw=5.0)
    row = result.to_dict()   # ready for score_site(row)

Environment
-----------
Requires ``NREL_API_KEY`` in a ``.env`` file (loaded via python-dotenv) or
as an environment variable.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean

import requests
import requests_cache
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PVWattsError(Exception):
    """Raised when the PVWatts API returns an unrecoverable error."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PVWattsResult:
    """Structured result from a PVWatts v8 API call.

    Field names match the keys that ``solar_economics.score_site()`` reads
    from its input ``row`` dict, so ``result.to_dict()`` can be merged
    straight into the master feature table.

    Attributes
    ----------
    pvwatts_ac_annual_kwh : float
        Estimated annual AC energy production (kWh).
    pvwatts_solrad_annual : float
        Annual average daily solar radiation on the array plane
        (kWh/m^2/day).
    pvwatts_capacity_factor : float
        Ratio of actual output to maximum possible output (0-1 scale
        as returned by PVWatts, typically 0.10 -- 0.25).
    pvwatts_ac_monthly : list[float]
        Monthly AC energy production (kWh), 12 values (Jan -- Dec).
    pvwatts_poa_monthly : list[float]
        Monthly plane-of-array irradiance (kWh/m^2), 12 values.
    pvwatts_dc_monthly : list[float]
        Monthly DC array output (kWh), 12 values.
    pvwatts_station_distance_m : float
        Distance from the requested location to the weather station
        used by PVWatts (metres).
    pvwatts_station_lat : float
        Latitude of the weather station.
    pvwatts_station_lon : float
        Longitude of the weather station.
    pvwatts_version : str
        API version string (e.g. ``"8.0.0"``).
    """

    pvwatts_ac_annual_kwh: float
    pvwatts_solrad_annual: float
    pvwatts_capacity_factor: float
    pvwatts_ac_monthly: list[float] = field(default_factory=list)
    pvwatts_poa_monthly: list[float] = field(default_factory=list)
    pvwatts_dc_monthly: list[float] = field(default_factory=list)
    pvwatts_station_distance_m: float = 0.0
    pvwatts_station_lat: float = 0.0
    pvwatts_station_lon: float = 0.0
    pvwatts_version: str = ""

    def to_dict(self) -> dict:
        """Return a flat dict compatible with ``score_site()`` field names.

        Includes the raw dataclass fields *plus* three computed monthly
        mean fields:

        - ``pvwatts_ac_monthly_mean``
        - ``pvwatts_poa_monthly_mean``
        - ``pvwatts_dc_monthly_mean``
        """
        d = asdict(self)
        d["pvwatts_ac_monthly_mean"] = mean(self.pvwatts_ac_monthly) if self.pvwatts_ac_monthly else 0.0
        d["pvwatts_poa_monthly_mean"] = mean(self.pvwatts_poa_monthly) if self.pvwatts_poa_monthly else 0.0
        d["pvwatts_dc_monthly_mean"] = mean(self.pvwatts_dc_monthly) if self.pvwatts_dc_monthly else 0.0
        return d


# ---------------------------------------------------------------------------
# Cached session (shared across calls)
# ---------------------------------------------------------------------------

_CACHE_DIR = Path.home() / ".solar_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_session = requests_cache.CachedSession(
    str(_CACHE_DIR / "pvwatts"),          # creates pvwatts.sqlite
    backend="sqlite",
    expire_after=30 * 24 * 60 * 60,       # 30 days in seconds
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PVWATTS_URL = "https://developer.nrel.gov/api/pvwatts/v8.json"
_MAX_RETRIES = 3
_BACKOFF_SECONDS = [1, 2, 4]
# kWh_ac per (kWh/m² GHI), calibrated to PVWatts at San Jose (1665 kWh/kW, GHI 5.32)
_NASA_TO_PVWATTS_K = 0.857
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _redact(exc: Exception) -> str:
    """Strip API keys from exception text before logging."""
    text = str(exc)
    key = os.environ.get("NREL_API_KEY")
    if key:
        text = text.replace(key, "REDACTED")
    return text


def _nasa_power_estimate(
    lat: float,
    lon: float,
    system_kw: float,
    losses: float = 14.0,
) -> PVWattsResult:
    """Estimate production from NASA POWER climatology when NREL is down.

    Uses all-sky GHI climatology, scaled by a factor calibrated to PVWatts
    at San Jose. Good enough for a first-look quote; reports mark the source.
    """
    url = "https://power.larc.nasa.gov/api/temporal/climatology/point"
    params = {
        "parameters": "ALLSKY_SFC_SW_DWN",
        "community": "RE",
        "longitude": lon,
        "latitude": lat,
        "format": "JSON",
    }
    resp = _session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    ghi = resp.json()["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
    loss_adj = (1.0 - losses / 100.0) / (1.0 - 0.14)
    monthly_ac = []
    monthly_poa = []
    for month, days in zip(_MONTHS, _DAYS):
        daily = float(ghi[month])
        monthly_poa.append(daily * days)
        monthly_ac.append(daily * days * system_kw * _NASA_TO_PVWATTS_K * loss_adj)
    annual = float(sum(monthly_ac))
    ann_ghi = float(ghi["ANN"])
    cf = annual / (system_kw * 8760.0) if system_kw else 0.0
    logger.info(
        "NASA POWER fallback: GHI %.2f kWh/m²/day → %.0f kWh/yr (%.0f kWh/kW)",
        ann_ghi, annual, annual / system_kw if system_kw else 0.0,
    )
    return PVWattsResult(
        pvwatts_ac_annual_kwh=annual,
        pvwatts_solrad_annual=ann_ghi,
        pvwatts_capacity_factor=cf,
        pvwatts_ac_monthly=monthly_ac,
        pvwatts_poa_monthly=monthly_poa,
        pvwatts_dc_monthly=[x / 0.96 for x in monthly_ac],
        pvwatts_station_distance_m=50_000.0,  # flags "moderate/distant" confidence
        pvwatts_station_lat=lat,
        pvwatts_station_lon=lon,
        pvwatts_version="nasa-power-climatology",
    )


def fetch_pvwatts(
    lat: float,
    lon: float,
    system_kw: float = 5.0,
    tilt: float | None = None,
    azimuth: float = 180.0,
    losses: float = 14.0,
    array_type: int = 1,
    module_type: int = 0,
) -> PVWattsResult:
    """Fetch PVWatts v8 simulation results for a single site.

    PVWatts estimates the energy production and cost of energy of
    grid-connected photovoltaic (PV) energy systems.  This function
    queries the NREL API, caches results locally, and returns a
    structured ``PVWattsResult``.

    Parameters
    ----------
    lat : float
        Site latitude in decimal degrees (positive = north).
    lon : float
        Site longitude in decimal degrees (negative = west).
    system_kw : float, default 5.0
        Nameplate DC system capacity in kilowatts.
    tilt : float or None, default None
        Array tilt angle in degrees from horizontal.  If *None*, defaults
        to ``abs(lat)`` (a common rule-of-thumb for fixed arrays).
    azimuth : float, default 180.0
        Array azimuth angle (degrees clockwise from north).  180 = south-
        facing, which is optimal in the northern hemisphere.
    losses : float, default 14.0
        Total system losses as a percentage (e.g. 14 means 14 %).
    array_type : int, default 1
        Array mounting type:
        0 = fixed open-rack, 1 = fixed roof-mount,
        2 = 1-axis tracking, 3 = 1-axis backtracking,
        4 = 2-axis tracking.
    module_type : int, default 0
        Module type: 0 = standard, 1 = premium, 2 = thin-film.

    Returns
    -------
    PVWattsResult
        Dataclass with annual and monthly production estimates plus
        weather-station metadata.

    Raises
    ------
    PVWattsError
        If the API returns an error after all retry attempts.
    """
    # --- Load API key ---
    load_dotenv()
    api_key = os.environ.get("NREL_API_KEY")
    assert api_key, (
        "NREL_API_KEY not found. Set it in a .env file "
        "(NREL_API_KEY=your_key) or export it as an environment variable. "
        "Get a free key at https://developer.nrel.gov/signup/"
    )

    # --- Build request params ---
    effective_tilt = abs(lat) if tilt is None else tilt

    params = {
        "api_key": api_key,
        "lat": lat,
        "lon": lon,
        "system_capacity": system_kw,
        "tilt": effective_tilt,
        "azimuth": azimuth,
        "losses": losses,
        "array_type": array_type,
        "module_type": module_type,
        "dataset": "nsrdb",
    }

    # --- Request with retry ---
    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            logger.info(
                "PVWatts request: lat=%.4f lon=%.4f capacity=%.1f kW "
                "(attempt %d/%d)",
                lat, lon, system_kw, attempt + 1, _MAX_RETRIES,
            )
            resp = _session.get(_PVWATTS_URL, params=params)

            if getattr(resp, "from_cache", False):
                logger.debug("PVWatts response served from cache")

            resp.raise_for_status()
            break  # success

        except requests.RequestException as exc:
            last_error = exc
            if attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF_SECONDS[attempt]
                logger.warning(
                    "PVWatts request failed (attempt %d/%d): %s  "
                    "— retrying in %ds",
                    attempt + 1, _MAX_RETRIES, _redact(exc), wait,
                )
                time.sleep(wait)
    else:
        logger.warning("PVWatts unreachable; falling back to NASA POWER climatology")
        try:
            return _nasa_power_estimate(lat, lon, system_kw, losses=losses)
        except Exception as nasa_exc:
            msg = f"PVWatts API failed after {_MAX_RETRIES} attempts"
            raise PVWattsError(msg) from last_error

    # --- Parse response ---
    data = resp.json()

    # NREL may return errors even on 200
    if "errors" in data and data["errors"]:
        raise PVWattsError(
            f"PVWatts API error: {'; '.join(data['errors'])}"
        )

    outputs = data["outputs"]
    station = data.get("station_info", {})

    return PVWattsResult(
        pvwatts_ac_annual_kwh=float(outputs["ac_annual"]),
        pvwatts_solrad_annual=float(outputs["solrad_annual"]),
        pvwatts_capacity_factor=float(outputs["capacity_factor"]),
        pvwatts_ac_monthly=[float(v) for v in outputs["ac_monthly"]],
        pvwatts_poa_monthly=[float(v) for v in outputs["poa_monthly"]],
        pvwatts_dc_monthly=[float(v) for v in outputs["dc_monthly"]],
        pvwatts_station_distance_m=float(station.get("distance", 0)),
        pvwatts_station_lat=float(station.get("lat", 0)),
        pvwatts_station_lon=float(station.get("lon", 0)),
        pvwatts_version=str(data.get("version", "")),
    )
