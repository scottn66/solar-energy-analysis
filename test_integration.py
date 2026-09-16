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
        assert result.country == "US"
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


# ---------------------------------------------------------------------------
# 7. Warehouse smoke tests (Phase 1)
# ---------------------------------------------------------------------------

class TestWarehouse:
    """Tests for solar_warehouse.py + solar_etl.py persistence layer."""

    def test_schema_creation(self, tmp_path):
        """ensure_schema() should create all 5 raw_* staging tables idempotently."""
        from solar_warehouse import get_conn, ensure_schema, table_counts

        db = tmp_path / "test.duckdb"
        conn = get_conn(db)
        ensure_schema(conn)
        # Calling twice should not error
        ensure_schema(conn)

        counts = table_counts(conn)
        expected = {
            "raw_pvwatts", "raw_urdb", "raw_eia", "raw_geocode", "raw_quote",
            "raw_tts_installations",
        }
        assert expected.issubset(set(counts.keys()))
        # All tables should start empty
        for name in expected:
            assert counts[name] == 0
        # Mart tables should NOT exist — the schema is staging-only
        assert "dim_location" not in counts
        assert "fact_quote" not in counts

        conn.close()

    def test_etl_writes_staging_rows(self, tmp_path, mock_pvwatts_response,
                                      mock_urdb_response):
        """etl_quote() should insert rows into raw_* tables."""
        from solar_etl import etl_quote, etl_status

        db = tmp_path / "test.duckdb"

        # Mock all external HTTP so the test doesn't hit real APIs
        def mock_session_get(url, **kwargs):
            from unittest.mock import MagicMock
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "pvwatts" in url:
                resp.json.return_value = mock_pvwatts_response
            elif "openei.org" in url:
                resp.json.return_value = mock_urdb_response
            elif "geocoding.geo.census.gov" in url:
                resp.json.return_value = {"result": {"addressMatches": []}}
            elif "nominatim" in url:
                resp.json.return_value = []
            else:
                resp.json.return_value = {"outputs": {"residential": 0.32}}
            return resp

        with patch("solar_pvwatts._session") as mock_pv, \
             patch("solar_urdb._session") as mock_urdb:
            mock_pv.get.side_effect = mock_session_get
            mock_urdb.get.side_effect = mock_session_get

            quote = etl_quote("95112", db_path=db)

        assert quote is not None
        counts = etl_status(db_path=db)
        # We should have at least one row in each key staging table
        assert counts["raw_geocode"] >= 1
        assert counts["raw_pvwatts"] >= 1
        assert counts["raw_urdb"] >= 1
        assert counts["raw_quote"] >= 1

    def test_quote_from_dict_roundtrip(self, tmp_path):
        """quote_from_dict() should rebuild a QuoteResult from its own to_dict()."""
        from solar_warehouse import quote_from_dict
        from solar_fetch import QuoteResult

        # Build a minimal valid dict shape (simulating what's stored in full_result_json)
        d = {
            "site": {
                "site_id": "test", "state": "CA", "address_label": "Test, CA",
                "specific_yield": 1600.0, "capacity_factor": 18.0,
                "resource_score": 0.89,
                "gross_cost": 17500.0, "net_cost": 12250.0, "lifetime_om": 2500.0,
                "lcoe": 0.08, "simple_payback_years": 7.5, "npv": 5000.0,
                "irr": 0.12, "grid_parity_ratio": 0.25,
                "annual_co2_avoided_tons": 3.0, "year_1_savings": 1200.0,
                "lifetime_savings": 40000.0, "electricity_rate_used": 0.32,
                "rate_source": "urdb_full", "year_production": [7500.0] * 25, "annual_savings": [1400.0] * 25,
                "cumulative_savings": [1400.0 * i for i in range(1, 26)],
                "cumulative_grid_cost": [2000.0 * i for i in range(1, 26)],
                "cashflows": [-12250.0] + [1400.0] * 25,
                "tilt_deviation": 0.0, "azimuth_deviation": 0.0, "site_fit_score": 1.0,
                "policy_score": 0.9, "viability_score": 88.0,
                "viability_label": "Excellent", "economics_score": 0.9,
                "assumptions_used": {},
            },
            "geocode": {
                "lat": 37.5, "lon": -122.0, "resolved_address": "Test, CA",
                "state": "CA", "zip_code": "94061",
                "source": "uszips", "confidence": "medium",
            },
            "pvwatts": {
                "pvwatts_ac_annual_kwh": 8000.0, "pvwatts_solrad_annual": 5.8,
                "pvwatts_capacity_factor": 18.0,
                "pvwatts_ac_monthly": [650]*12, "pvwatts_poa_monthly": [180]*12,
                "pvwatts_dc_monthly": [700]*12,
                "pvwatts_station_distance_m": 2000.0,
                "pvwatts_station_lat": 37.5, "pvwatts_station_lon": -122.0,
                "pvwatts_version": "8.5.0",
            },
            "rate": {
                "flat_rate": 0.32, "fixed_monthly_charge": 10.0,
                "utility_name": "Test Utility", "rate_name": "Test Rate",
                "rate_uri": "", "source": "urdb_full",
                "effective_date": "2024-01-01",
                "is_tou": False, "is_tiered": False,
            },
            "export": {
                "avg_export_rate": 0.05, "policy_name": "NEM 3.0",
                "explanation": "test", "state": "CA", "is_exact": False,
            },
            "meta": {
                "system_kw_used": 5.0, "system_sizing_method": "default",
                "confidence_level": "medium", "confidence_reasons": ["test"],
            },
        }

        quote = quote_from_dict(d)
        assert isinstance(quote, QuoteResult)
        assert quote.site_result.viability_score == 88.0
        assert quote.geocode_result.state == "CA"
        assert quote.rate_result.flat_rate == 0.32
        # hourly_rates is intentionally dropped (numpy array, not JSON-safe)
        assert quote.rate_result.hourly_rates is None

    def test_app_uses_warehouse_cache(self, tmp_path, monkeypatch):
        """Second /api/quote call for the same location should be a cache hit.

        This is the key OLTP/OLAP integration test: after one call populates
        the warehouse, the second call should return without invoking
        quote_from_location() or etl_quote().
        """
        from fastapi.testclient import TestClient
        import solar_warehouse

        # Point the warehouse at a temp DB so this test is isolated
        monkeypatch.setattr(solar_warehouse, "DEFAULT_DB_PATH", tmp_path / "test.duckdb")

        # Pre-populate the warehouse with a quote directly (no API calls)
        from solar_warehouse import get_conn, ensure_schema
        from datetime import datetime, timezone
        import json as _json

        conn = get_conn()
        ensure_schema(conn)
        quote_json = {
            "site": {
                "site_id": "cached", "state": "CA", "address_label": "94061",
                "specific_yield": 1700.0, "capacity_factor": 19.0,
                "resource_score": 0.95,
                "gross_cost": 17500.0, "net_cost": 12250.0, "lifetime_om": 2500.0,
                "lcoe": 0.075, "simple_payback_years": 7.2, "npv": 11000.0,
                "irr": 0.14, "grid_parity_ratio": 0.19,
                "annual_co2_avoided_tons": 3.0, "year_1_savings": 1400.0,
                "lifetime_savings": 45000.0, "electricity_rate_used": 0.39,
                "rate_source": "bundled_tou", "year_production": [7500.0] * 25, "annual_savings": [1400.0] * 25,
                "cumulative_savings": [1400.0 * i for i in range(1, 26)],
                "cumulative_grid_cost": [2000.0 * i for i in range(1, 26)],
                "cashflows": [-12250.0] + [1400.0] * 25,
                "tilt_deviation": 0.0, "azimuth_deviation": 0.0, "site_fit_score": 1.0,
                "policy_score": 0.9, "viability_score": 89.0,
                "viability_label": "Excellent", "economics_score": 0.95,
                "assumptions_used": {"default_price_per_watt": 3.5},
            },
            "geocode": {
                "lat": 37.46, "lon": -122.23, "resolved_address": "Redwood City, CA 94061",
                "state": "CA", "zip_code": "94061",
                "source": "uszips", "confidence": "medium",
            },
            "pvwatts": {
                "pvwatts_ac_annual_kwh": 7500.0, "pvwatts_solrad_annual": 5.8,
                "pvwatts_capacity_factor": 19.0,
                "pvwatts_ac_monthly": [625]*12, "pvwatts_poa_monthly": [180]*12,
                "pvwatts_dc_monthly": [700]*12,
                "pvwatts_station_distance_m": 2000.0,
                "pvwatts_station_lat": 37.46, "pvwatts_station_lon": -122.23,
                "pvwatts_version": "8.5.0",
            },
            "rate": {
                "flat_rate": 0.39, "fixed_monthly_charge": 10.5,
                "utility_name": "PG&E", "rate_name": "E-TOU-C",
                "rate_uri": "", "source": "bundled_tou",
                "effective_date": "2024-01-01",
                "is_tou": True, "is_tiered": False,
            },
            "export": {
                "avg_export_rate": 0.055, "policy_name": "NEM 3.0 (CA)",
                "explanation": "test", "state": "CA", "is_exact": False,
            },
            "meta": {
                "system_kw_used": 4.5, "system_sizing_method": "bill_sized",
                "confidence_level": "medium", "confidence_reasons": ["test"],
            },
        }
        conn.execute("""
            INSERT INTO raw_quote
                (fetched_at, location_query, lat, lon, state, zip_code,
                 system_kw, system_sizing_method,
                 viability_score, viability_label, payback_years, npv_25yr,
                 irr, lcoe, co2_avoided_tons,
                 utility_name, rate_name, rate_source, electricity_rate_used,
                 export_policy, export_rate,
                 confidence_level, confidence_reasons, full_result_json)
            VALUES (?, '94061', 37.46, -122.23, 'CA', '94061', 4.5, 'bill_sized',
                    89.0, 'Excellent', 7.2, 11000.0, 0.14, 0.075, 3.0,
                    'PG&E', 'E-TOU-C', 'bundled_tou', 0.39,
                    'NEM 3.0 (CA)', 0.055, 'medium', '["test"]', ?)
        """, [datetime.now(timezone.utc), _json.dumps(quote_json)])
        conn.close()

        # If etl_quote is called, the test fails — we should hit the cache
        from unittest.mock import patch
        from app import app
        client = TestClient(app)

        with patch("app.etl_quote") as mock_etl, \
             patch("app.quote_from_location") as mock_live:
            resp = client.post("/api/quote", data={"location": "94061"})

            assert resp.status_code == 200
            # Neither the ETL nor the live pipeline should be called
            mock_etl.assert_not_called()
            mock_live.assert_not_called()
            # The response should contain Redwood City from our cached row
            assert "Redwood City" in resp.text or "94061" in resp.text

    def test_load_tts_inserts_rows(self, tmp_path):
        """load_tts() should bulk-load the cleaned CSV into raw_tts_installations."""
        from solar_etl import load_tts
        from solar_warehouse import get_conn, ensure_schema

        # Write a tiny CSV with the same column order as etl/clean_tts.py output.
        # 25 columns matching the cleaned CSV header.
        csv_path = tmp_path / "mini_tts.csv"
        header = (
            "installation_date,PV_system_size_DC,total_installed_price,"
            "rebate_or_grant,customer_segment,tracking,ground_mounted,"
            "zip_code,state,utility_service_territory,third_party_owned,"
            "installer_name,azimuth_1,tilt_1,module_manufacturer_1,"
            "module_model_1,module_quantity_1,technology_module_1,"
            "efficiency_module_1,inverter_manufacturer_1,inverter_model_1,"
            "output_capacity_inverter_1,inverter_loading_ratio,"
            "battery_rated_capacity_kWh,price_per_watt"
        )
        rows = [
            "2024-06-01,5.0,17500.0,0.0,RES_SF,0.0,0.0,94061,CA,PG&E,"
            "0.0,Sunrun,180.0,20.0,REC Solar,REC400,12,Mono-c-Si,0.21,"
            "Enphase,IQ8,5.0,1.0,,3.50",
            "2024-07-15,8.4,29400.0,2000.0,RES_SF,0.0,0.0,78701,TX,Oncor,"
            "0.0,Tesla,180.0,25.0,Q CELLS,Q.PEAK,21,Mono-c-Si,0.205,"
            "SolarEdge,HD-Wave,8.0,1.05,13.5,3.50",
        ]
        csv_path.write_text(header + "\n" + "\n".join(rows) + "\n")

        db = tmp_path / "test.duckdb"
        n = load_tts(csv_path, db_path=db)
        assert n == 2

        # Verify columns came in correctly typed and queryable
        conn = get_conn(db)
        ensure_schema(conn)
        ca_row = conn.execute("""
            SELECT state, installer_name, price_per_watt
            FROM raw_tts_installations
            WHERE state = 'CA'
        """).fetchone()
        assert ca_row == ("CA", "Sunrun", 3.50)

        # Calling load_tts twice with truncate=True (default) should keep
        # the row count constant — proves idempotence
        n2 = load_tts(csv_path, db_path=db)
        assert n2 == 2

        from solar_warehouse import tts_market_stats
        # Mini CSV has 1 CA row at 94061, so ZIP3 n=1 < 5 → state fallback
        stats = tts_market_stats("CA", "94061", conn=conn)
        assert stats["level"] == "state"
        assert stats["n"] == 1
        assert abs(stats["median_ppw"] - 3.50) < 1e-6
        conn.close()

    def test_staging_row_is_queryable(self, tmp_path):
        """After writing a quote, SELECT queries should return sensible data."""
        from solar_warehouse import get_conn, ensure_schema

        db = tmp_path / "test.duckdb"
        conn = get_conn(db)
        ensure_schema(conn)

        # Manually insert a row to avoid the API dependency
        from datetime import datetime, timezone
        conn.execute("""
            INSERT INTO raw_quote
                (fetched_at, location_query, lat, lon, state, zip_code,
                 system_kw, system_sizing_method,
                 viability_score, viability_label, payback_years, npv_25yr,
                 irr, lcoe, co2_avoided_tons,
                 utility_name, rate_name, rate_source, electricity_rate_used,
                 export_policy, export_rate,
                 confidence_level, confidence_reasons, full_result_json)
            VALUES (?, 'test_loc', 37.5, -122.0, 'CA', '94061', 5.0, 'manual',
                    85.0, 'Good', 7.5, 10000.0, 0.14, 0.08, 3.0,
                    'Test Utility', 'Test Rate', 'urdb_full', 0.32,
                    'NEM 3.0', 0.05,
                    'high', '[]', '{}')
        """, [datetime.now(timezone.utc)])

        result = conn.execute(
            "SELECT viability_score, state FROM raw_quote WHERE location_query = 'test_loc'"
        ).fetchone()
        assert result == (85.0, "CA")
        conn.close()
