"""
Geocoding module for the solar viability analysis project.

Resolves user-supplied locations (street addresses, city/state, ZIP codes)
to latitude/longitude using a multi-provider fallback chain with caching.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests_cache
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cached HTTP session (SQLite-backed, 30-day TTL)
# ---------------------------------------------------------------------------
_CACHE_DIR = Path.home() / ".solar_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_session = requests_cache.CachedSession(
    str(_CACHE_DIR / "geocode"),
    backend="sqlite",
    expire_after=60 * 60 * 24 * 30,  # 30 days
)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_uszips: dict[str, dict] | None = None          # lazy-loaded ZIP lookup
_state_names: dict[str, str] | None = None       # "oregon" → "OR"
_nominatim_last_call: float = 0.0                # rate-limit tracker
_NOMINATIM_UA = (
    "solar-viability/1.0 (https://github.com/scottn66/solar-energy-analysis)"
)

_ZIP_RE = re.compile(r"^\d{5}$")


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeocodeResult:
    """Structured geocoding result."""

    lat: float
    lon: float
    resolved_address: str
    state: str
    zip_code: str
    source: str
    confidence: str  # "high" | "medium" | "low"
    country: str = "US"  # ISO 3166-1 alpha-2


class GeocodeError(Exception):
    """Raised when all geocoding providers fail."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _load_uszips() -> dict[str, dict]:
    """Load *data/uszips.csv* (relative to this module) into a dict keyed by
    ZIP code string.  Comment lines (starting with ``#``) are skipped.
    """
    global _uszips, _state_names
    if _uszips is not None:
        return _uszips

    csv_path = Path(__file__).resolve().parent / "data" / "uszips.csv"
    _uszips = {}
    names: dict[str, str] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        # Skip comment lines before handing off to DictReader
        lines = (line for line in fh if not line.startswith("#"))
        reader = csv.DictReader(lines)
        for row in reader:
            _uszips[row["zip"]] = row
            sid = (row.get("state_id") or "").strip()
            sname = (row.get("state_name") or "").strip().lower()
            if sid:
                names[sid.lower()] = sid
            if sname and sid:
                names[sname] = sid
    _state_names = names
    return _uszips


def _normalize_state(raw: str) -> str:
    """Map 'Oregon' or 'or' to 'OR'. Unknown values are returned unchanged."""
    if not raw:
        return ""
    s = raw.strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    _load_uszips()
    return (_state_names or {}).get(s.lower(), s)


def _try_zip_lookup(location: str) -> GeocodeResult | None:
    """Return a GeocodeResult when *location* is a bare 5-digit ZIP code."""
    if not _ZIP_RE.match(location):
        return None

    zips = _load_uszips()
    row = zips.get(location)
    if row is None:
        return None

    return GeocodeResult(
        lat=float(row["lat"]),
        lon=float(row["lng"]),
        resolved_address=f"{row['city']}, {row['state_id']} {row['zip']}",
        state=row["state_id"],
        zip_code=row["zip"],
        source="uszips",
        confidence="medium",
        country="US",
    )


def _try_census(location: str) -> GeocodeResult | None:
    """Query the US Census Bureau geocoder."""
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {
        "benchmark": "Public_AR_Current",
        "format": "json",
        "address": location,
    }

    try:
        resp = _session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Census geocoder request failed: %s", exc)
        return None

    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        return None

    match = matches[0]
    coords = match.get("coordinates", {})
    components = match.get("addressComponents", {})

    lat = coords.get("y")
    lon = coords.get("x")
    if lat is None or lon is None:
        return None

    state = _normalize_state(components.get("state", ""))
    zip_code = components.get("zip", "")
    matched_address = match.get("matchedAddress", location)

    # If state is missing, try to extract it from the matched address string
    # Typical format: "123 MAIN ST, CITY, STATE, ZIP"
    if not state and matched_address:
        parts = [p.strip() for p in matched_address.split(",")]
        if len(parts) >= 3:
            state = _normalize_state(
                parts[-2].strip().split()[0] if parts[-2].strip() else ""
            )

    confidence = "high" if len(matches) == 1 else "medium"

    return GeocodeResult(
        lat=float(lat),
        lon=float(lon),
        resolved_address=matched_address,
        state=state,
        zip_code=zip_code,
        source="census",
        confidence=confidence,
        country="US",
    )


def _try_nominatim(location: str, us_only: bool = True) -> GeocodeResult | None:
    """Query the OpenStreetMap Nominatim API (rate-limited)."""
    global _nominatim_last_call

    # Enforce 1-second minimum between calls
    elapsed = time.time() - _nominatim_last_call
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "format": "json",
        "addressdetails": 1,
        "limit": 1,
        "q": location,
    }
    if us_only:
        params["countrycodes"] = "us"
    headers = {"User-Agent": _NOMINATIM_UA}

    try:
        resp = _session.get(url, params=params, headers=headers, timeout=15)
        _nominatim_last_call = time.time()
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _nominatim_last_call = time.time()
        logger.warning("Nominatim request failed: %s", exc)
        return None

    if not data or not isinstance(data, list):
        return None

    hit = data[0]
    address_detail = hit.get("address", {})
    country_code = (address_detail.get("country_code") or "").upper()
    if us_only and country_code and country_code != "US":
        logger.warning(
            "Nominatim returned non-US result (country_code=%s); skipping.",
            country_code,
        )
        return None

    try:
        lat = float(hit["lat"])
        lon = float(hit["lon"])
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Nominatim response missing lat/lon: %s", exc)
        return None

    if country_code == "US":
        state = _normalize_state(address_detail.get("state", ""))
        postcode = address_detail.get("postcode", "") or ""
        zip_code = postcode.split("-")[0][:5] if postcode[:5].isdigit() else postcode
        country = "US"
    else:
        state = ""
        zip_code = address_detail.get("postcode", "") or ""
        country = country_code or "XX"

    display_name = hit.get("display_name", location)

    return GeocodeResult(
        lat=lat,
        lon=lon,
        resolved_address=display_name,
        state=state,
        zip_code=zip_code,
        source="nominatim",
        confidence="medium" if country == "US" else "low",
        country=country,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def geocode(location: str) -> GeocodeResult:
    """Resolve a location string to geographic coordinates.

    The function tries providers in order and returns the first successful
    result:

    1. **ZIP-code shortcut** -- if *location* is exactly five digits, look it
       up in the bundled ``data/uszips.csv`` file (fast, offline).
    2. **US Census Geocoder** -- authoritative for US street addresses;
       confidence is ``"high"`` when exactly one match is returned.
    3. **Nominatim (OpenStreetMap)** -- US results first, then a global
       retry so non-US cities resolve.  A 1-second rate limit is enforced.

    All HTTP requests are routed through a :class:`requests_cache.CachedSession`
    backed by a SQLite database in ``~/.solar_cache/`` with a 30-day TTL.

    Parameters
    ----------
    location : str
        Free-form location string (street address, city/state, or ZIP code).

    Returns
    -------
    GeocodeResult
        Dataclass containing lat, lon, resolved_address, state, zip_code,
        source, and confidence.

    Raises
    ------
    GeocodeError
        If none of the providers return a usable result.
    """
    location = location.strip()
    if not location:
        raise GeocodeError("Empty location string provided.")

    tried: list[str] = []

    # (a) ZIP-only shortcut
    result = _try_zip_lookup(location)
    if result is not None:
        return result
    if _ZIP_RE.match(location):
        tried.append("uszips (ZIP not found)")

    # (b) US Census Geocoder
    tried.append("census")
    result = _try_census(location)
    if result is not None:
        return result
    logger.warning("Census geocoder returned no results; falling back to Nominatim.")

    # (c) Nominatim US fallback
    tried.append("nominatim-us")
    result = _try_nominatim(location, us_only=True)
    if result is not None:
        return result
    logger.warning("Nominatim returned no US results for location: %s", location)

    # (d) Nominatim worldwide — production estimates still work via PVWatts
    tried.append("nominatim-global")
    result = _try_nominatim(location, us_only=False)
    if result is not None:
        return result

    # (e) All providers exhausted
    raise GeocodeError(
        f"All geocoding providers failed for '{location}'. Tried: {', '.join(tried)}"
    )
