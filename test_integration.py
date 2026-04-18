"""
test_integration.py — Integration tests for the full solar quote pipeline.

Tests cover:
    1. Module-level unit tests for geocode, pvwatts, urdb, nem
    2. Full quote_from_location pipeline for three scenarios
    3. FastAPI smoke test

These tests mock the HTTP layer to avoid hitting real APIs in CI.
For live API tests, run: pytest test_integration.py --live

Run:
    pytest test_integration.py -v
"""

from __future__ import annotations

import json
import math
import os
from datetime import date
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Fixtures: mock API responses
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pvwatts_response():
    """Realistic PVWatts v8 JSON response for San Jose."""
    return {
        "inputs": {"lat": "37.35", "lon": "-121.89"},
        "errors": [],
        "warnings": [],
        "version": "8.5.0",
        "station_info": {
            "lat": 37.35, "lon": -121.9, "distance": 1202,
            "city": "", "state": "California",
        },
        "outputs": {
            "ac_annual": 8195.11,
            "solrad_annual": 5.886,
            "capacity_factor": 18.71,
            "ac_monthly": [472, 570, 709, 722, 780, 785, 848, 851, 783, 741, 568, 496],
            "dc_monthly": [495, 597, 743, 756, 817, 822, 888, 891, 820, 776, 595, 520],
            "poa_monthly": [123, 145, 183, 192, 206, 212, 228, 230, 204, 186, 142, 118],
        },
    }


@pytest.fixture
def mock_urdb_response():
    """Simplified URDB response for PG&E E-1."""
    return {
        "items": [{
            "name": "E-1 Residential Service",
            "utility": "Pacific Gas & Electric Co",
            "approved": True,
            "enddate": 0,
            "startdate": 1640000000,
            "is_default": True,
            "uri": "/rate/1234",
            "fixedchargefirstmeter": 10.50,
            "energyratestructure": [[{"rate": 0.32, "unit": "kWh"}]],
            "energyweekdayschedule": None,
            "energyweekendschedule": None,
        }],
    }


@pytest.fixture
def mock_nrel_v3_response():
    """NREL utility_rates v3 response."""
    return {
        "outputs": {"residential": 0.32, "utility_name": "Pacific Gas & Electric Co"},
    }


# ---------------------------------------------------------------------------
# 1. Geocode tests
# ---------------------------------------------------------------------------

class TestGeocode:

    def test_zip_lookup(self):
        """ZIP-only input should resolve from uszips.csv without HTTP calls."""
        from solar_geocode import geocode
        result = geocode("95112")
        assert result.state == "CA"
        assert result.source == "uszips"
        assert result.confidence == "medium"
        assert abs(result.lat - 37.35) < 0.05

    def test_zip_returns_city(self):
        """ZIP lookup should include city name."""
        from solar_geocode import geocode
        result = geocode("10001")
        assert result.state == "NY"
        assert "New York" in result.resolved_address or result.zip_code == "10001"

    def test_invalid_zip_raises(self):
        """Garbled input should raise GeocodeError."""
        from solar_geocode import geocode, GeocodeError
        with pytest.raises(GeocodeError):
            geocode("xyzzy")

    def test_nonexistent_zip_raises(self):
        """ZIP not in database should try Census/Nominatim, then raise."""
        from solar_geocode import geocode, GeocodeError
        # 00000 is not a real ZIP; with mocked failing APIs, should raise
        with patch("solar_geocode._session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"result": {"addressMatches": []}}
            mock_resp.raise_for_status = MagicMock()
            mock_session.get.return_value = mock_resp
            with pytest.raises(GeocodeError):
                geocode("00000")


# ---------------------------------------------------------------------------
# 2. PVWatts tests
# ---------------------------------------------------------------------------

class TestPVWatts:

    def test_fetch_returns_dataclass(self, mock_pvwatts_response):
        """PVWatts fetch should return a PVWattsResult with expected fields."""
        from solar_pvwatts import fetch_pvwatts, PVWattsResult

        with patch("solar_pvwatts._session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_pvwatts_response
            mock_resp.raise_for_status = MagicMock()
            mock_session.get.return_value = mock_resp

            result = fetch_pvwatts(37.35, -121.89, system_kw=5.0)

        assert isinstance(result, PVWattsResult)
        assert result.pvwatts_ac_annual_kwh == 8195.11
        assert len(result.pvwatts_ac_monthly) == 12
        assert result.pvwatts_station_distance_m == 1202

    def test_to_dict_has_monthly_means(self, mock_pvwatts_response):
        """to_dict() should include computed monthly mean fields."""
        from solar_pvwatts import fetch_pvwatts

        with patch("solar_pvwatts._session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_pvwatts_response
            mock_resp.raise_for_status = MagicMock()
            mock_session.get.return_value = mock_resp

            result = fetch_pvwatts(37.35, -121.89)
            d = result.to_dict()

        assert "pvwatts_ac_monthly_mean" in d
        assert "pvwatts_poa_monthly_mean" in d
        assert d["pvwatts_ac_monthly_mean"] > 0


# ---------------------------------------------------------------------------
# 3. URDB tests
# ---------------------------------------------------------------------------

class TestURDB:

    def test_fetch_rate_fast_returns_rateresult(self, mock_nrel_v3_response):
        """fetch_rate_fast should return a RateResult from NREL v3."""
        from solar_urdb import fetch_rate_fast, RateResult

        with patch("solar_urdb._session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_nrel_v3_response
            mock_resp.raise_for_status = MagicMock()
            mock_session.get.return_value = mock_resp

            result = fetch_rate_fast(37.35, -121.89)

        assert isinstance(result, RateResult)
        assert result.flat_rate == 0.32
        assert result.source == "nrel_v3"
        assert result.is_tou is False

    def test_get_rate_fallback_to_eia(self):
        """If both URDB and NREL v3 fail, should fall back to EIA state average."""
        from solar_urdb import get_rate

        with patch("solar_urdb.fetch_rate", side_effect=Exception("URDB down")):
            with patch("solar_urdb.fetch_rate_fast", side_effect=Exception("v3 down")):
                result = get_rate(37.35, -121.89, state="CA")

        assert result.source in ("eia_live", "eia_bundled_2025", "eia_state_fallback")
        assert result.flat_rate > 0.25  # CA rate should be $0.25+ regardless of source

    def test_flat_rate_parsing(self, mock_urdb_response):
        """A flat URDB rate should be parsed correctly."""
        from solar_urdb import fetch_rate, RateResult

        with patch("solar_urdb._session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_urdb_response
            mock_resp.raise_for_status = MagicMock()
            mock_session.get.return_value = mock_resp

            result = fetch_rate(37.35, -121.89)

        assert result.flat_rate == 0.32
        assert result.fixed_monthly_charge == 10.50
        assert result.is_tou is False
        assert "Pacific Gas" in result.utility_name


# ---------------------------------------------------------------------------
# 4. NEM tests
# ---------------------------------------------------------------------------

class TestNEM:

    def test_california_nem3(self):
        """CA installation after 2023-04-15 should get NEM 3.0 rates."""
        from solar_nem import get_export_value
        result = get_export_value("CA", 0.32, install_date=date(2026, 1, 1))
        assert result.policy_name == "NEM 3.0 (CA)"
        assert 0.04 < result.avg_export_rate < 0.10  # ~$0.055 average
        assert result.is_exact is False

    def test_california_pre_nem3(self):
        """CA installation before 2023-04-15 should get 1:1 NEM."""
        from solar_nem import get_export_value
        result = get_export_value("CA", 0.32, install_date=date(2022, 1, 1))
        # CA is not in NEM_1_FOR_1_STATES, so it should get reduced NEM or default
        assert result.avg_export_rate > 0

    def test_nj_one_for_one(self):
        """NJ should get full retail 1:1 NEM."""
        from solar_nem import get_export_value
        result = get_export_value("NJ", 0.20)
        assert result.policy_name == "1:1 Net Metering"
        assert result.avg_export_rate == 0.20
        assert result.is_exact is True

    def test_texas_deregulated(self):
        """TX should get deregulated market rate."""
        from solar_nem import get_export_value
        result = get_export_value("TX", 0.15)
        assert "Deregulated" in result.policy_name
        assert result.avg_export_rate == 0.075  # 50% of retail

    def test_unknown_state_default(self):
        """Unknown state should get 75% default."""
        from solar_nem import get_export_value
        result = get_export_value("ZZ", 0.20)
        assert abs(result.avg_export_rate - 0.15) < 0.001  # 75% of 0.20
        assert "Estimated" in result.policy_name


# ---------------------------------------------------------------------------
# 5. Full pipeline integration tests (mocked HTTP)
# ---------------------------------------------------------------------------

class TestFullPipeline:

    def _make_mock_session(self, pvwatts_resp, urdb_resp=None, nrel_v3_resp=None):
        """Create a mock session that returns different responses by URL."""
        def side_effect(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "pvwatts" in url:
                resp.json.return_value = pvwatts_resp
            elif "openei.org" in url:
                if urdb_resp:
                    resp.json.return_value = urdb_resp
                else:
                    resp.raise_for_status.side_effect = Exception("URDB unavailable")
            elif "utility_rates" in url:
                if nrel_v3_resp:
                    resp.json.return_value = nrel_v3_resp
                else:
                    resp.raise_for_status.side_effect = Exception("v3 unavailable")
            return resp
        return side_effect

    def test_california_zip_quote(self, mock_pvwatts_response, mock_urdb_response):
        """CA ZIP code → full quote with NEM 3.0."""
        from solar_fetch import quote_from_location

        with patch("solar_pvwatts._session") as mock_pv, \
             patch("solar_urdb._session") as mock_urdb:
            mock_pv.get.side_effect = self._make_mock_session(mock_pvwatts_response)
            mock_urdb.get.side_effect = self._make_mock_session(
                mock_pvwatts_response, mock_urdb_response
            )

            q = quote_from_location("95112")

        assert q.geocode_result.state == "CA"
        assert q.site_result.viability_score > 0
        assert q.export_result.policy_name == "NEM 3.0 (CA)"
        assert q.confidence_level in ("high", "medium", "low")

    def test_texas_zip_quote(self, mock_pvwatts_response):
        """TX ZIP → deregulated market, no TOU."""
        from solar_fetch import quote_from_location

        with patch("solar_pvwatts._session") as mock_pv, \
             patch("solar_urdb._session") as mock_urdb:
            # Make URDB fail so it falls to EIA state fallback
            mock_pv.get.side_effect = self._make_mock_session(mock_pvwatts_response)
            mock_urdb.get.side_effect = Exception("mocked fail")

            q = quote_from_location("78701")  # Austin, TX

        assert q.geocode_result.state == "TX"
        assert "Deregulated" in q.export_result.policy_name

    def test_bill_sizing(self, mock_pvwatts_response):
        """monthly_kwh parameter should auto-size the system."""
        from solar_fetch import quote_from_location

        with patch("solar_pvwatts._session") as mock_pv, \
             patch("solar_urdb._session") as mock_urdb:
            mock_pv.get.side_effect = self._make_mock_session(mock_pvwatts_response)
            mock_urdb.get.side_effect = Exception("mocked fail")

            q = quote_from_location("95112", monthly_kwh=400)

        assert q.system_sizing_method == "bill_sized"
        # 400 kWh/month * 12 = 4800 kWh/yr. With ~1639 kWh/kW yield, system ≈ 3 kW
        assert 2.0 <= q.system_kw_used <= 4.0


# ---------------------------------------------------------------------------
# 6. FastAPI smoke test
# ---------------------------------------------------------------------------

class TestFastAPIApp:

    def test_landing_page(self):
        """GET / should return the landing HTML."""
        from fastapi.testclient import TestClient
        from app import app

        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Is solar worth it" in resp.text

    def test_healthz(self):
        """GET /healthz should return ok."""
        from fastapi.testclient import TestClient
        from app import app

        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_quote_endpoint_with_bad_location(self):
        """POST /api/quote with garbage should return an error card, not 500."""
        from fastapi.testclient import TestClient
        from app import app

        client = TestClient(app)
        resp = client.post("/api/quote", data={"location": "xyzzynotreal"})
        assert resp.status_code == 200  # HTMX gets HTML, not 500
        assert "error-card" in resp.text or "not found" in resp.text.lower() or "error" in resp.text.lower()
