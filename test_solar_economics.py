"""
test_solar_economics.py — pytest suite for the solar economics engine.

Covers:
    1. Known-good California case (San Jose sample row)
    2. Deliberately bad case (low yield, high cost, cheap electricity)
    3. Edge cases (zero production, missing state, IRR no-solution,
       missing price_per_watt)

Run:
    pytest test_solar_economics.py -v
"""

import math

import numpy as np
import pytest

from solar_economics import (
    Assumptions,
    DEFAULTS,
    SiteResult,
    score_site,
    sensitivity_tornado,
    _resource_score,
    _economics_score,
    _site_fit_score,
    _policy_score,
    _compute_irr,
    _resolve_electricity_rate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def san_jose_row() -> dict:
    """The sample San Jose row from the project spec."""
    return {
        "site_id": "demo_site_001",
        "address_label": "San Jose, CA",
        "lat": 37.33,
        "lon": -121.8863,
        "system_capacity_kw": 5.0,
        "azimuth": 180,
        "tilt": 20,
        "array_type": 1,
        "module_type": 0,
        "losses": 14,
        "state": "CA",
        "zip_code": 95192,
        "customer_segment": "RES",
        "pvwatts_ac_annual_kwh": 8195.11,
        "pvwatts_solrad_annual": 5.886,
        "pvwatts_capacity_factor": 18.71,
        "pvwatts_poa_monthly_mean": 179.22,
        "pvwatts_dc_monthly_mean": 715.42,
        "pvwatts_ac_monthly_mean": 682.93,
        "pvwatts_station_distance_m": 1202,
        "pvwatts_station_lat": 37.33,
        "pvwatts_station_lon": -121.90,
        "pvwatts_version": "8.5.0",
        "nasa_hours": 744,
        "nasa_ghi_mean": 86.86,
        "nasa_ghi_max": 573.95,
        "nasa_temp_mean_c": 9.84,
        "nasa_temp_max_c": 21.65,
        "nasa_rh_mean": 90.08,
        "nasa_ws_mean": 1.71,
        "nasa_midday_ghi_mean": 310.62,
        "tts_geo_level_used": "state",
        "tts_sample_size": 63221,
        "tts_median_system_size_dc": 4.20,
        "tts_median_price_per_watt": 3.80,
        "tts_median_tilt": 18,
        "tts_median_azimuth": 180,
        "tts_tracking_rate": 0,
        "tts_ground_mount_rate": 0,
        "tts_module_efficiency_median": 0.202,
        "tts_battery_kwh_median": -1,
        "tts_recent_sample_size": 36351,
        "tts_recent_median_price_per_watt": 3.80,
        "kaggle_median_Electricity_Price_USD_per_kWh": 0.15,
        "kaggle_median_Payback_Period_Years": 9.1,
        "kaggle_median_Solar_Viability_Score": 56.5,
    }


@pytest.fixture
def bad_row() -> dict:
    """A deliberately poor site: low yield, high cost, cheap electricity."""
    return {
        "site_id": "bad_site_001",
        "address_label": "Cloudy Town, WA",
        "lat": 47.6,
        "lon": -122.3,
        "system_capacity_kw": 4.0,
        "azimuth": 270,  # west-facing (bad)
        "tilt": 5,       # nearly flat (bad for 47° latitude)
        "losses": 22,    # high shading/soiling losses
        "state": "WA",
        "pvwatts_ac_annual_kwh": 3200.0,  # very low: ~800 kWh/kW
        "pvwatts_capacity_factor": 9.1,
        "tts_median_price_per_watt": 5.50,  # expensive install
        "tts_recent_sample_size": 50,       # very low adoption
        "kaggle_median_Electricity_Price_USD_per_kWh": 0.10,
    }


# ---------------------------------------------------------------------------
# 1. Known-good California case
# ---------------------------------------------------------------------------

class TestSanJoseCase:
    """The San Jose sample should produce strong solar economics."""

    def test_payback_between_4_and_7(self, san_jose_row):
        """CA @ $0.32/kWh with 30% ITC: payback should be 4-7 years.

        At $0.32/kWh, year-1 savings ≈ $1,600–1,900.  Net cost ≈ $13,300.
        Simple payback ≈ 13300/1700 ≈ 7.8... but escalation helps.
        With 40% self-consumption at full retail + 60% at 75% retail:
        effective rate ≈ 0.32 * (0.40 + 0.60*0.75) = 0.32 * 0.85 = $0.272/kWh
        Year-1 savings ≈ 8195 * 0.272 ≈ $2,229.  Payback ≈ 13300/2229 ≈ 5.97.
        """
        result = score_site(san_jose_row)
        assert 4.0 <= result.simple_payback_years <= 7.0, (
            f"Expected payback 4-7 years, got {result.simple_payback_years:.2f}"
        )

    def test_lcoe_between_007_and_011(self, san_jose_row):
        """LCOE for a $3.80/W system in a good solar resource should be $0.07-$0.11.

        Net cost = 5 * 1000 * 3.80 * 0.70 = $13,300
        Lifetime O&M = 20 * 5 * 25 = $2,500
        Lifetime kWh ≈ 8195 * 24.25 (degradation sum) ≈ 190,700 kWh
        LCOE ≈ (13300 + 2500) / 190700 ≈ $0.083/kWh
        """
        result = score_site(san_jose_row)
        assert 0.07 <= result.lcoe <= 0.11, (
            f"Expected LCOE $0.07-$0.11, got ${result.lcoe:.4f}"
        )

    def test_viability_score_at_least_75(self, san_jose_row):
        """San Jose CA should score >=75 on the composite viability.

        Strong resource (1639 kWh/kW), excellent economics (payback <7,
        grid parity ratio <0.3), near-perfect site fit (azimuth 180°,
        tilt 20° vs lat 37°), and high policy score (36k recent installs).
        """
        result = score_site(san_jose_row)
        assert result.viability_score >= 75.0, (
            f"Expected viability >= 75, got {result.viability_score:.1f}"
        )

    def test_uses_state_electricity_rate(self, san_jose_row):
        """CA state should resolve to $0.32/kWh from the state table, not Kaggle."""
        result = score_site(san_jose_row)
        assert result.electricity_rate_used == 0.32
        assert result.rate_source == "state_table"

    def test_specific_yield_reasonable(self, san_jose_row):
        """8195 kWh / 5 kW = 1639 kWh/kW — should be captured accurately."""
        result = score_site(san_jose_row)
        assert abs(result.specific_yield - 1639.02) < 1.0

    def test_npv_positive(self, san_jose_row):
        """With CA rates, NPV should be solidly positive."""
        result = score_site(san_jose_row)
        assert result.npv > 0, f"Expected positive NPV, got ${result.npv:.2f}"

    def test_irr_not_none(self, san_jose_row):
        """IRR should converge for a standard profitable case."""
        result = score_site(san_jose_row)
        assert result.irr is not None
        assert result.irr > 0.10  # should be well above 10% for CA

    def test_co2_avoided(self, san_jose_row):
        """Year-1 CO2 avoided ≈ 8195 * 0.0004 ≈ 3.28 tons."""
        result = score_site(san_jose_row)
        assert 3.0 <= result.annual_co2_avoided_tons <= 3.5

    def test_production_array_length(self, san_jose_row):
        """Production array should have 25 entries (one per year)."""
        result = score_site(san_jose_row)
        assert len(result.year_production) == 25
        assert len(result.annual_savings) == 25
        assert len(result.cumulative_savings) == 25

    def test_production_degrades(self, san_jose_row):
        """Each year's production should be less than the previous."""
        result = score_site(san_jose_row)
        for i in range(1, len(result.year_production)):
            assert result.year_production[i] < result.year_production[i - 1]

    def test_cashflow_vector_length(self, san_jose_row):
        """Cashflow vector = [-net_cost] + 25 years of savings = 26 entries."""
        result = score_site(san_jose_row)
        assert len(result.cashflows) == 26
        assert result.cashflows[0] < 0  # initial outlay is negative

    def test_gross_cost_calculation(self, san_jose_row):
        """Gross cost = 5kW * 1000 * $3.80/W = $19,000."""
        result = score_site(san_jose_row)
        assert result.gross_cost == 19000.0

    def test_net_cost_with_itc(self, san_jose_row):
        """Net cost = $19,000 * (1 - 0.30) = $13,300."""
        result = score_site(san_jose_row)
        assert result.net_cost == 13300.0


# ---------------------------------------------------------------------------
# 2. Deliberately bad case
# ---------------------------------------------------------------------------

class TestBadCase:
    """A site with poor resource, expensive install, and cheap electricity."""

    def test_viability_below_40(self, bad_row):
        """Poor site should score <40 on viability.

        Low yield (~800 kWh/kW), west-facing, flat tilt at 47° latitude,
        high losses, expensive install ($5.50/W), and cheap grid ($0.12/kWh).
        """
        result = score_site(bad_row)
        assert result.viability_score < 40, (
            f"Expected viability < 40, got {result.viability_score:.1f}"
        )

    def test_long_payback(self, bad_row):
        """Bad site should have payback >15 years or NaN (never pays back)."""
        result = score_site(bad_row)
        assert (
            math.isnan(result.simple_payback_years)
            or result.simple_payback_years > 15
        ), f"Expected payback >15 or NaN, got {result.simple_payback_years}"

    def test_high_lcoe(self, bad_row):
        """With $5.50/W and low production, LCOE should be high."""
        result = score_site(bad_row)
        assert result.lcoe > 0.15, f"Expected LCOE >$0.15, got ${result.lcoe:.4f}"

    def test_poor_site_fit(self, bad_row):
        """West-facing (270°) at tilt 5° for 47° latitude should score poorly."""
        result = score_site(bad_row)
        assert result.site_fit_score < 0.5
        assert result.azimuth_deviation == 90.0
        assert result.tilt_deviation == 42.6  # |5 - 47.6|

    def test_low_policy_score(self, bad_row):
        """Only 50 recent installations → low policy/market score."""
        result = score_site(bad_row)
        assert result.policy_score < 0.4


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_zero_production(self):
        """Zero production should not crash; metrics should be degenerate."""
        row = {
            "site_id": "zero_prod",
            "system_capacity_kw": 5.0,
            "pvwatts_ac_annual_kwh": 0.0,
            "pvwatts_capacity_factor": 0.0,
            "state": "CA",
            "lat": 37.0,
            "tilt": 20,
            "azimuth": 180,
            "losses": 14,
            "tts_median_price_per_watt": 3.80,
            "tts_recent_sample_size": 1000,
        }
        result = score_site(row)
        assert result.specific_yield == 0.0
        assert result.resource_score == 0.0
        assert result.lcoe == float("inf")
        assert math.isnan(result.simple_payback_years)
        assert result.viability_score < 25  # site_fit + policy still contribute small amounts

    def test_missing_state(self):
        """Missing state should fall back through the rate chain gracefully."""
        row = {
            "site_id": "no_state",
            "system_capacity_kw": 5.0,
            "pvwatts_ac_annual_kwh": 7000.0,
            "pvwatts_capacity_factor": 16.0,
            "state": None,
            "lat": 35.0,
            "tilt": 20,
            "azimuth": 180,
            "losses": 14,
            "tts_median_price_per_watt": 3.50,
            "tts_recent_sample_size": 500,
            "kaggle_median_Electricity_Price_USD_per_kWh": 0.18,
        }
        result = score_site(row)
        # Should fall back to Kaggle median ($0.18)
        assert result.electricity_rate_used == 0.18
        assert result.rate_source == "kaggle_fallback"

    def test_missing_state_no_kaggle(self):
        """Missing state AND missing Kaggle → falls back to national median."""
        row = {
            "site_id": "no_rate_source",
            "system_capacity_kw": 5.0,
            "pvwatts_ac_annual_kwh": 7000.0,
            "pvwatts_capacity_factor": 16.0,
            "state": "XX",  # not in lookup table
            "lat": 35.0,
            "tilt": 20,
            "azimuth": 180,
            "losses": 14,
            "tts_median_price_per_watt": 3.50,
            "tts_recent_sample_size": 500,
        }
        result = score_site(row)
        assert result.electricity_rate_used == 0.16  # national median
        assert result.rate_source == "national_median"

    def test_missing_price_per_watt(self):
        """Missing tts_median_price_per_watt should use the default $3.50/W."""
        row = {
            "site_id": "no_ppw",
            "system_capacity_kw": 5.0,
            "pvwatts_ac_annual_kwh": 7500.0,
            "pvwatts_capacity_factor": 17.0,
            "state": "TX",
            "lat": 32.0,
            "tilt": 25,
            "azimuth": 180,
            "losses": 14,
            "tts_recent_sample_size": 5000,
        }
        result = score_site(row)
        # gross_cost = 5 * 1000 * 3.50 = $17,500
        assert result.gross_cost == 17500.0

    def test_irr_all_positive_cashflows(self):
        """IRR with no negative cashflow should return None (no real solution)."""
        # This is mathematically degenerate — no sign change
        result = _compute_irr([100, 200, 300])
        # Newton-Raphson may not converge or finds a nonsensical root
        # Either None or a very large number is acceptable
        assert result is None or result > 10.0

    def test_irr_standard_case(self):
        """IRR for a standard investment should be reasonable."""
        # -$10,000 upfront, $2,000/yr for 10 years
        cfs = [-10000] + [2000] * 10
        result = _compute_irr(cfs)
        assert result is not None
        assert 0.10 < result < 0.25  # ~15% expected

    def test_electricity_price_override(self, san_jose_row):
        """Override should bypass state table entirely."""
        custom = Assumptions(electricity_price_override=0.50)
        result = score_site(san_jose_row, custom)
        assert result.electricity_rate_used == 0.50
        assert result.rate_source == "override"
        # Higher rate = faster payback
        default_result = score_site(san_jose_row)
        assert result.simple_payback_years < default_result.simple_payback_years

    def test_custom_assumptions(self, san_jose_row):
        """Custom assumptions should flow through correctly."""
        custom = Assumptions(
            federal_itc=0.0,  # no ITC
            system_life_years=10,
        )
        result = score_site(san_jose_row, custom)
        # No ITC: net_cost = gross_cost = $19,000
        assert result.net_cost == 19000.0
        # Only 10 years of production/savings
        assert len(result.year_production) == 10


# ---------------------------------------------------------------------------
# 4. Sub-score unit tests
# ---------------------------------------------------------------------------

class TestSubScores:

    def test_resource_score_ceiling(self):
        """Yield at or above 1800 kWh/kW should cap at 1.0."""
        assert _resource_score(1800) == 1.0
        assert _resource_score(2200) == 1.0

    def test_resource_score_zero(self):
        assert _resource_score(0) == 0.0
        assert _resource_score(-100) == 0.0
        assert _resource_score(float("nan")) == 0.0

    def test_resource_score_linear(self):
        """900 kWh/kW should be 0.5."""
        assert abs(_resource_score(900) - 0.5) < 0.01

    def test_policy_score_scaling(self):
        """50,000 installations should map to ~1.0."""
        assert abs(_policy_score(50000) - 1.0) < 0.01
        assert _policy_score(0) == 0.0
        assert _policy_score(100) > 0.3

    def test_site_fit_perfect(self):
        """180° azimuth, tilt == latitude, 14% losses → score ~1.0."""
        _, _, score = _site_fit_score(tilt=37.0, azimuth=180.0, latitude=37.0, losses=14.0)
        assert score > 0.95

    def test_site_fit_terrible(self):
        """90° off azimuth, 45° tilt deviation, high losses."""
        _, _, score = _site_fit_score(tilt=0.0, azimuth=90.0, latitude=45.0, losses=30.0)
        assert score < 0.3


# ---------------------------------------------------------------------------
# 5. Sensitivity tornado
# ---------------------------------------------------------------------------

class TestSensitivity:

    def test_tornado_returns_5_params(self, san_jose_row):
        """Tornado should return results for 5 parameters."""
        results = sensitivity_tornado(san_jose_row)
        assert len(results) == 5

    def test_tornado_sorted_by_impact(self, san_jose_row):
        """Results should be sorted by descending absolute impact."""
        results = sensitivity_tornado(san_jose_row)
        impacts = [r["impact"] for r in results]
        assert impacts == sorted(impacts, reverse=True)

    def test_tornado_has_required_fields(self, san_jose_row):
        """Each result should have all required fields."""
        results = sensitivity_tornado(san_jose_row)
        for r in results:
            assert "param" in r
            assert "npv_low" in r
            assert "npv_high" in r
            assert "npv_base" in r
            assert "impact" in r


# ---------------------------------------------------------------------------
# 6. Resolve electricity rate
# ---------------------------------------------------------------------------

class TestRateResolution:

    def test_override_wins(self):
        rate, src = _resolve_electricity_rate("CA", 0.15, override=0.50)
        assert rate == 0.50
        assert src == "override"

    def test_state_table(self):
        rate, src = _resolve_electricity_rate("HI", 0.15, override=None)
        assert rate == 0.42
        assert src == "state_table"

    def test_kaggle_fallback(self):
        rate, src = _resolve_electricity_rate("XX", 0.22, override=None)
        assert rate == 0.22
        assert src == "kaggle_fallback"

    def test_national_median_fallback(self):
        rate, src = _resolve_electricity_rate("XX", None, override=None)
        assert rate == 0.16
        assert src == "national_median"


class TestAssumptionFormatting:

    def test_itc_and_rate_are_human_readable(self):
        from solar_viz import format_assumption
        label, value = format_assumption("federal_itc", 0.30)
        assert "ITC" in label or "tax credit" in label.lower()
        assert "30" in value
        label, value = format_assumption("electricity_price_override", 0.389)
        assert "$0.389" in value
        label, value = format_assumption("system_life_years", 25)
        assert "25" in value
        assert "year" in value.lower()
