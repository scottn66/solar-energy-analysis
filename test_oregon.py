"""
test_oregon.py — Oregon-region coverage tests.

Verifies the pieces that make Oregon (especially Central Oregon around
Bend/Redmond) a first-class region: bundled utility rate schedules, NEM
policy handling including the Central Oregon co-ops, the city-report list,
the batch-ETL location file, and the Cascade-aware heatmap yield model.

All tests are offline — no API keys or HTTP required.

Run:
    pytest test_oregon.py -v
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from solar_nem import (
    NEM_1_FOR_1_STATES,
    OR_COOP_EXPORT_RATIO,
    get_export_value,
)
from solar_urdb import _load_bundled_tou, _load_eia_state_rates

DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_uszips() -> dict[str, str]:
    """zip → state_id from the bundled ZIP table."""
    out: dict[str, str] = {}
    with open(DATA_DIR / "uszips.csv", newline="", encoding="utf-8") as fh:
        lines = (line for line in fh if not line.startswith("#"))
        for row in csv.DictReader(lines):
            out[row["zip"].zfill(5)] = row["state_id"]
    return out


# ---------------------------------------------------------------------------
# Bundled rate schedules (data/utility_tou_schedules.csv)
# ---------------------------------------------------------------------------

class TestBundledOregonRates:
    def test_pacific_power_matches(self):
        r = _load_bundled_tou("Pacific Power", state="OR")
        assert r is not None
        assert "Schedule 4" in r.rate_name
        assert 0.10 < r.flat_rate < 0.20
        assert r.fixed_monthly_charge > 0

    def test_pacificorp_matches_only_in_oregon(self):
        # Bare "PacifiCorp" also covers Rocky Mountain Power states —
        # the Oregon schedule must not leak into Utah quotes.
        assert _load_bundled_tou("PacifiCorp", state="OR") is not None
        assert _load_bundled_tou("PacifiCorp", state="UT") is None
        assert _load_bundled_tou("Rocky Mountain Power", state="UT") is None
        # The "Pacific Power" brand also operates in WA and far-northern CA
        # (URDB: "Pacific Power (California)") at different tariffs — the
        # Oregon schedule must not leak there either.
        assert _load_bundled_tou("Pacific Power (California)", state="CA") is None
        assert _load_bundled_tou("Pacific Power", state="WA") is None

    def test_portland_general_is_not_california_pge(self):
        oregon = _load_bundled_tou("Portland General Electric Co", state="OR")
        california = _load_bundled_tou("Pacific Gas & Electric Co", state="CA")
        assert oregon is not None and california is not None
        assert "Schedule 7" in oregon.rate_name
        assert "E-TOU-C" in california.rate_name
        # CA's PG&E is roughly 2.5x Oregon PGE's rate — mixing them up
        # would corrupt every quote in one of the two states.
        assert california.flat_rate > oregon.flat_rate * 2

    def test_central_electric_coop(self):
        r = _load_bundled_tou("Central Electric Cooperative, Inc", state="OR")
        assert r is not None
        assert 0.05 < r.flat_rate < 0.12
        assert r.fixed_monthly_charge > 20  # co-ops recover cost via facilities charge

    def test_midstate_electric_coop(self):
        r = _load_bundled_tou("Midstate Electric Cooperative", state="OR")
        assert r is not None
        assert 0.05 < r.flat_rate < 0.12
        assert r.fixed_monthly_charge == pytest.approx(35.00)

    def test_oregon_defaults_are_flat_not_tou(self):
        # Oregon's default residential service is flat — unlike CA's
        # default-TOU.  A flat schedule must not masquerade as TOU.
        for utility in ("Pacific Power", "Portland General Electric",
                        "Central Electric Cooperative",
                        "Midstate Electric Cooperative"):
            r = _load_bundled_tou(utility, state="OR")
            assert r is not None, utility
            assert r.is_tou is False, utility
            assert r.hourly_rates is None, utility

    def test_california_schedules_still_tou(self):
        r = _load_bundled_tou("Pacific Gas & Electric Co", state="CA")
        assert r is not None
        assert r.is_tou is True
        assert r.hourly_rates is not None and len(r.hourly_rates) == 8760

    def test_effective_dates_parsed_from_csv(self):
        ca = _load_bundled_tou("Pacific Gas & Electric Co", state="CA")
        pacpwr = _load_bundled_tou("Pacific Power", state="OR")
        assert ca.effective_date.year == 2024
        assert pacpwr.effective_date.year == 2026


# ---------------------------------------------------------------------------
# NEM export policy
# ---------------------------------------------------------------------------

class TestOregonNEM:
    def test_oregon_is_one_to_one_state(self):
        assert "OR" in NEM_1_FOR_1_STATES

    def test_investor_owned_utilities_get_full_retail(self):
        r = get_export_value("OR", 0.14, utility_name="Pacific Power")
        assert r.policy_name == "1:1 Net Metering"
        assert r.avg_export_rate == pytest.approx(0.14)
        assert r.is_exact is True
        assert "757.300" in r.explanation  # cites the statute

    def test_central_oregon_coops_net_monthly(self):
        for utility in ("Central Electric Cooperative, Inc",
                        "Midstate Electric Cooperative"):
            r = get_export_value("OR", 0.09, utility_name=utility)
            assert r.policy_name == "Co-op NEM (monthly netting)", utility
            assert r.avg_export_rate == pytest.approx(0.09 * OR_COOP_EXPORT_RATIO)
            assert r.is_exact is False

    def test_coop_branch_is_oregon_only(self):
        # A similarly named utility outside OR must not hit the co-op branch.
        r = get_export_value("CA", 0.30, utility_name="Central Electric")
        assert r.policy_name != "Co-op NEM (monthly netting)"

    def test_unknown_utility_falls_back_to_state_policy(self):
        r = get_export_value("OR", 0.157)
        assert r.policy_name == "1:1 Net Metering"


# ---------------------------------------------------------------------------
# City-report list
# ---------------------------------------------------------------------------

class TestCityList:
    def test_entries_are_five_tuples_with_unique_slugs(self):
        from build_city_reports import CITIES
        slugs = [c[0] for c in CITIES]
        assert len(slugs) == len(set(slugs))
        for entry in CITIES:
            assert len(entry) == 5, entry
            assert entry[2] in ("CA", "OR"), entry

    def test_all_zips_exist_in_uszips_with_matching_state(self):
        from build_city_reports import CITIES
        zips = _load_uszips()
        for slug, _name, state, zip_code, _note in CITIES:
            assert zip_code in zips, f"{slug}: ZIP {zip_code} not in uszips.csv"
            assert zips[zip_code] == state, (
                f"{slug}: ZIP {zip_code} is in {zips[zip_code]}, not {state}"
            )

    def test_central_oregon_cluster_present(self):
        from build_city_reports import CITIES
        slugs = {c[0] for c in CITIES}
        for wanted in ("bend-or", "redmond-or", "sisters-or", "prineville-or",
                       "madras-or", "la-pine-or", "sunriver-or", "terrebonne-or"):
            assert wanted in slugs, wanted


# ---------------------------------------------------------------------------
# Batch ETL location file
# ---------------------------------------------------------------------------

class TestOregonBatchFile:
    def test_batch_csv_parses_and_covers_central_oregon(self):
        path = DATA_DIR / "oregon_locations.csv"
        zips = _load_uszips()
        with open(path, newline="", encoding="utf-8") as fh:
            lines = (line for line in fh if not line.lstrip().startswith("#"))
            rows = [r for r in csv.DictReader(lines) if (r.get("location") or "").strip()]
        assert len(rows) >= 10
        locations = {r["location"].strip() for r in rows}
        # Bend, Redmond, Sisters, La Pine must be covered
        for zip_code in ("97701", "97756", "97759", "97739"):
            assert zip_code in locations
        for r in rows:
            loc = r["location"].strip()
            assert loc in zips and zips[loc] == "OR", loc
            for key in ("monthly_kwh", "system_kw"):
                val = (r.get(key) or "").strip()
                if val:
                    float(val)  # optional fields must be numeric when present

    def test_etl_batch_missing_file_raises(self):
        from solar_etl import etl_batch
        with pytest.raises(FileNotFoundError):
            etl_batch("does_not_exist.csv")


# ---------------------------------------------------------------------------
# Heatmap yield model + utility assumptions
# ---------------------------------------------------------------------------

class TestOregonYieldModel:
    def test_bend_beats_portland_despite_higher_latitude_neighbors(self):
        from build_heatmap import or_specific_yield
        bend     = or_specific_yield(44.06, -121.31)
        portland = or_specific_yield(45.52, -122.68)
        assert bend > portland * 1.20  # rain shadow ≈ +25%

    def test_east_west_split_at_cascade_crest(self):
        from build_heatmap import or_specific_yield
        # Same latitude, opposite sides of the crest: Bend vs Eugene
        east = or_specific_yield(44.06, -121.31)
        west = or_specific_yield(44.06, -123.09)
        assert east > west

    def test_yields_within_plausible_band(self):
        from build_heatmap import or_specific_yield
        for lat, lon in [(42.0, -121.8), (44.1, -121.3), (45.5, -122.7),
                         (42.3, -122.9), (46.1, -123.8), (44.0, -117.2)]:
            y = or_specific_yield(lat, lon)
            assert 900 <= y <= 1750, (lat, lon, y)

    def test_coastal_fog_discount(self):
        from build_heatmap import or_specific_yield
        coast  = or_specific_yield(44.6, -124.05)   # Newport
        valley = or_specific_yield(44.6, -123.10)   # Corvallis-ish
        assert coast < valley

    def test_california_dispatch_unchanged(self):
        from build_heatmap import lat_to_specific_yield, specific_yield_for
        assert specific_yield_for("CA", 37.0, -122.0) == lat_to_specific_yield(37.0)


class TestHeatmapUtilityAssumptions:
    def test_redmond_is_coop_with_reduced_export(self):
        from build_heatmap import OR_COOP_EXPORT_RATIO, _utility_assumptions
        assum, label = _utility_assumptions("OR", "97756", "977")
        assert "Central Electric" in label
        assert assum.nem_export_ratio == pytest.approx(OR_COOP_EXPORT_RATIO)

    def test_bend_is_pacific_power_full_retail(self):
        from build_heatmap import OR_PACPWR_RATE, _utility_assumptions
        assum, label = _utility_assumptions("OR", "97701", "977")
        assert label == "Pacific Power"
        assert assum.electricity_price_override == pytest.approx(OR_PACPWR_RATE)
        assert assum.nem_export_ratio == pytest.approx(1.0)

    def test_portland_is_pge(self):
        from build_heatmap import _utility_assumptions
        _assum, label = _utility_assumptions("OR", "97202", "972")
        assert label == "PGE"

    def test_eweb_is_eugene_city_only(self):
        from build_heatmap import _utility_assumptions
        _assum, label = _utility_assumptions("OR", "97401", "974")
        assert label == "EWEB (muni)"
        # Roseburg shares the 974 prefix but is Pacific Power territory
        _assum, label = _utility_assumptions("OR", "97470", "974")
        assert label == "Pacific Power"

    def test_california_assumptions_unchanged(self):
        from build_heatmap import (IOU_ELEC_RATE, IOU_EXPORT_RATIO,
                                   _utility_assumptions)
        assum, label = _utility_assumptions("CA", "94061", "940")
        assert label == "IOU (NEM 3.0)"
        assert assum.electricity_price_override == pytest.approx(IOU_ELEC_RATE)
        assert assum.nem_export_ratio == pytest.approx(IOU_EXPORT_RATIO)
        assum_muni, label_muni = _utility_assumptions("CA", "95814", "958")
        assert label_muni == "Muni"


# ---------------------------------------------------------------------------
# Portfolio anchors + EIA fallback
# ---------------------------------------------------------------------------

class TestPortfolioOregon:
    def test_central_oregon_anchors_present(self):
        from build_portfolio import ANCHORS
        cities = {(c, s) for c, s, *_ in ANCHORS}
        assert ("Bend", "OR") in cities
        assert ("Redmond", "OR") in cities

    def test_high_desert_yield_override(self):
        from build_portfolio import (CITY_CLIMATE_MULT, STATE_CLIMATE_MULT,
                                     specific_yield_from_lat)
        # Bend must land near 1,475 kWh/kW/yr — the state-level OR
        # multiplier alone (calibrated to the Willamette Valley) puts it
        # near 1,090, ~26% too low.
        bend = specific_yield_from_lat(44.06) * CITY_CLIMATE_MULT[("Bend", "OR")]
        assert 1400 <= bend <= 1550
        portland = specific_yield_from_lat(45.52) * STATE_CLIMATE_MULT["OR"]
        assert bend > portland * 1.20


class TestEIAFallback:
    def test_oregon_present_in_bundled_state_rates(self):
        rates = _load_eia_state_rates()
        assert rates.get("OR") == pytest.approx(0.14)
