# Test Suite Reference

**Last verified:** 102/102 passing in ~8 seconds (plus 28/28 `verify_system.py` end-to-end checks)

This document describes every test in the project and what it protects against. Tests are your safety net — if you change code and a test breaks, you've probably introduced a bug.

## Running the tests

```bash
# Run everything
pytest test_solar_economics.py test_integration.py test_oregon.py -v

# Run one file
pytest test_solar_economics.py -v

# Run one class
pytest test_solar_economics.py::TestSanJoseCase -v

# Run one specific test
pytest test_solar_economics.py::TestSanJoseCase::test_payback_between_4_and_7 -v

# Stop at first failure
pytest -x

# Show print statements and full output
pytest -v -s
```

**CI runs all tests automatically** on every push and PR via `.github/workflows/test.yml`.

## End-to-end verification — `verify_system.py`

Beyond the unit/integration tests (which use mocked HTTP), there's a **full system verification** script that runs the real pipeline against live APIs and makes 28 explicit assertions across 8 phases:

| Phase | What it verifies |
|---|---|
| 0 | Warehouse resets cleanly |
| 1 | Schema is staging-only (5 `raw_*` tables, no mart tables) |
| 2 | `etl_quote()` writes to `raw_quote`, `raw_geocode`, `raw_pvwatts`, `raw_urdb` |
| 3 | Teammate workflow: query warehouse via subprocess with zero API keys in env |
| 4 | Cache-hit latency is consistently <100ms |
| 5 | Rehydrated quote matches original field-for-field (score, NPV, LCOE, payback, production array, rate source, export policy) |
| 6 | Multiple locations accumulate in the warehouse correctly |
| 7 | Malformed input raises `GeocodeError` cleanly (no crash, no partial rows) |
| 8 | Full pytest suite still passes |

Run it:
```bash
python3 verify_system.py
```

Expected output: `28 passed`, exit code 0.

---

## File 1: `test_solar_economics.py` — 43 tests

Tests the **financial math engine** in isolation. No network calls. Runs in <1 second.

### Class: `TestSanJoseCase` — Known-good California site (13 tests)

Uses real data from the San Jose sample row to verify the model produces realistic numbers. These tests pin the documented `VINTAGE_2025` parameter set (30% ITC, 0.5%/yr degradation, $20/kW-yr O&M) so the calibration stays meaningful now that the live defaults track post-2025 law.

| Test | What it checks | Why it matters |
|------|---------------|----------------|
| `test_payback_between_4_and_7` | Payback is 4-7 years at $0.32/kWh | Sanity check on the whole cashflow model |
| `test_lcoe_between_007_and_011` | LCOE is $0.07-$0.11/kWh for $3.80/W system | Catches errors in lifetime kWh or cost calculation |
| `test_viability_score_at_least_75` | Score ≥75 for a strong CA site | End-to-end sanity: good inputs → good score |
| `test_uses_state_electricity_rate` | CA resolves to $0.32 from state table, not Kaggle $0.15 | Rate fallback priority is correct |
| `test_specific_yield_reasonable` | 8195 kWh ÷ 5 kW = 1639 kWh/kW | Basic division; protects against unit errors |
| `test_npv_positive` | NPV > 0 for CA site | If this flips negative, discount rate or savings math broke |
| `test_irr_not_none` | IRR solver converges | Numerical stability of the Newton-Raphson fallback |
| `test_co2_avoided` | ~3.28 tons/yr (8195 kWh × 0.0004) | Environmental calculation matches documented constant |
| `test_production_array_length` | Exactly 25 years of production data | Catches off-by-one errors in the loop |
| `test_production_degrades` | Each year's output < previous year | Degradation formula is applied correctly |
| `test_cashflow_vector_length` | 26 entries (1 upfront + 25 years) | IRR vector is the right shape for numpy_financial |
| `test_gross_cost_calculation` | 5 kW × 1000 × $3.80/W = $19,000 exactly | Catches kW↔W unit confusion |
| `test_net_cost_with_itc` | $19,000 × 0.70 = $13,300 exactly | ITC percentage applied correctly |

### Class: `TestBadCase` — Known-bad site (5 tests)

A deliberately terrible site (cloudy WA, west-facing, 5° tilt at 47° latitude, $5.50/W install, $0.10/kWh grid) to confirm bad investments are correctly flagged.

| Test | What it checks |
|------|---------------|
| `test_viability_below_40` | Score under 40 (Poor verdict) |
| `test_long_payback` | Payback >15 years or never pays back |
| `test_high_lcoe` | LCOE > $0.15/kWh (too expensive) |
| `test_poor_site_fit` | Site fit score reflects bad orientation |
| `test_low_policy_score` | 50 local installations → low market signal |

### Class: `TestEdgeCases` — Graceful handling of weird input (8 tests)

| Test | What it checks | Scenario |
|------|---------------|----------|
| `test_zero_production` | 0 kWh → LCOE=∞, payback=NaN, no crash | Sun doesn't shine |
| `test_missing_state` | None state → falls back to Kaggle rate | Data quality issue |
| `test_missing_state_no_kaggle` | No state, no Kaggle → US median $0.16 | All rate sources fail |
| `test_missing_price_per_watt` | No install cost → uses default $3.50/W | TTS data incomplete |
| `test_irr_all_positive_cashflows` | No negative cashflow → returns None gracefully | Mathematically degenerate |
| `test_irr_standard_case` | -$10K then $2K×10yr → ~15% IRR | Solver correctness |
| `test_electricity_price_override` | Manual rate bypasses all lookups | User control |
| `test_custom_assumptions` | ITC=0%, life=10yr → flows through correctly | Overridable parameters work |

### Class: `TestSubScores` — Individual component scores (6 tests)

| Test | What it checks |
|------|---------------|
| `test_resource_score_ceiling` | Yield ≥1800 kWh/kW caps at 1.0 |
| `test_resource_score_zero` | Zero/negative/NaN yield → 0.0 |
| `test_resource_score_linear` | 900 kWh/kW → exactly 0.5 |
| `test_policy_score_scaling` | 50,000 installations → ~1.0 (log-scaled) |
| `test_site_fit_perfect` | Tilt=latitude, azimuth=180° → ~1.0 |
| `test_site_fit_terrible` | 90° off azimuth, huge tilt mismatch → <0.3 |

### Class: `TestSensitivity` — Tornado chart analysis (3 tests)

| Test | What it checks |
|------|---------------|
| `test_tornado_returns_5_params` | Varies exactly 5 parameters (price, install, degradation, discount, NEM) |
| `test_tornado_sorted_by_impact` | Results sorted by biggest NPV impact first |
| `test_tornado_has_required_fields` | Each result has param, npv_low, npv_high, npv_base, impact |

### Class: `TestRateResolution` — Electricity rate lookup priority (4 tests)

| Test | What it checks |
|------|---------------|
| `test_override_wins` | Manual override beats state table |
| `test_state_table` | Hawaii → $0.42/kWh from EIA lookup |
| `test_kaggle_fallback` | Unknown state "XX" with Kaggle → uses Kaggle rate |
| `test_national_median_fallback` | Unknown state, no Kaggle → US median $0.16 |

### Class: `TestPost2025Defaults` — current-law engine defaults (4 tests)

The San Jose calibration tests pin a documented `VINTAGE_2025` parameter set
(30% ITC, 0.5%/yr degradation, $20/kW-yr O&M); these verify the live defaults.

| Test | What it checks |
|------|---------------|
| `test_default_itc_is_zero` | Section 25D terminated post-2025 → default ITC 0% |
| `test_default_net_cost_equals_gross` | No credit: net cost = gross cost |
| `test_itc_loss_lengthens_payback` | Removing the ITC lengthens payback vs the 2025 vintage |
| `test_updated_parameter_defaults` | Degradation 0.7%/yr, O&M $31/kW·yr, CO2 0.00035 t/kWh |

---

## File 2: `test_integration.py` — 26 tests

Tests the **full pipeline** with mocked HTTP so no real APIs are hit during CI. Runs in ~6 seconds.

### Class: `TestGeocode` — Address resolution (4 tests)

| Test | What it checks |
|------|---------------|
| `test_zip_lookup` | "95112" → San Jose, CA (from local uszips.csv, no HTTP) |
| `test_zip_returns_city` | "10001" → New York, NY |
| `test_invalid_zip_raises` | "xyzzy" → raises GeocodeError |
| `test_nonexistent_zip_raises` | "00000" → tries Census/Nominatim, then raises |

### Class: `TestPVWatts` — NREL solar production (2 tests)

| Test | What it checks |
|------|---------------|
| `test_fetch_returns_dataclass` | Mock API response → correct PVWattsResult fields |
| `test_to_dict_has_monthly_means` | `to_dict()` includes computed monthly averages |

### Class: `TestURDB` — Utility rate lookup (3 tests)

| Test | What it checks |
|------|---------------|
| `test_fetch_rate_fast_returns_rateresult` | NREL v3 mock → correct RateResult |
| `test_get_rate_fallback_to_eia` | When URDB + v3 both fail → EIA fallback works |
| `test_flat_rate_parsing` | Mock URDB JSON → correctly parses flat rate + fixed charge |

### Class: `TestNEM` — Export compensation policy (5 tests)

| Test | What it checks |
|------|---------------|
| `test_california_nem3` | CA after 2023-04-15 → NEM 3.0 (~$0.055/kWh avg) |
| `test_california_pre_nem3` | CA before 2023-04-15 → not NEM 3.0 |
| `test_nj_one_for_one` | New Jersey → full retail 1:1 NEM |
| `test_texas_deregulated` | Texas → 50% of retail (deregulated market) |
| `test_unknown_state_default` | "ZZ" → 75% of retail (default estimate) |

### Class: `TestFullPipeline` — End-to-end quote flow (3 tests)

| Test | What it checks |
|------|---------------|
| `test_california_zip_quote` | "95112" → full QuoteResult with NEM 3.0 and positive score |
| `test_texas_zip_quote` | "78701" → deregulated market export policy |
| `test_bill_sizing` | monthly_kwh=400 → auto-sizes system to ~3 kW |

### Class: `TestFastAPIApp` — Web app smoke tests (3 tests)

| Test | What it checks |
|------|---------------|
| `test_landing_page` | GET / → 200, contains "Is solar worth it" |
| `test_healthz` | GET /healthz → `{"ok": true}` |
| `test_quote_endpoint_with_bad_location` | POST garbage → friendly HTML error card, not 500 crash |

### Class: `TestWarehouse` — DuckDB warehouse layer (6 tests)

| Test | What it checks |
|------|---------------|
| `test_schema_creation` | `ensure_schema()` creates the 5 `raw_*` staging tables and no mart tables; idempotent |
| `test_etl_writes_staging_rows` | `etl_quote()` inserts rows into `raw_geocode`, `raw_pvwatts`, `raw_urdb`, `raw_quote` |
| `test_quote_from_dict_roundtrip` | `quote_from_dict()` rebuilds a full `QuoteResult` from stored JSON |
| `test_app_uses_warehouse_cache` | Second `/api/quote` for same location hits the warehouse — proves the OLTP/OLAP wiring |
| `test_load_tts_inserts_rows` | `load_tts()` bulk-loads the cleaned TTS CSV into `raw_tts_installations` |
| `test_staging_row_is_queryable` | Inserted rows can be queried back with expected values |

---

## File 3: `test_oregon.py` — 33 tests

Oregon-region coverage: bundled tariffs, NEM policy (including the Central
Oregon co-ops), the city-report list, the batch-ETL file, and the
Cascade-aware heatmap yield model. All offline — no API keys or HTTP.

### Class: `TestBundledOregonRates` — bundled rate schedules (8 tests)

| Test | What it checks |
|------|---------------|
| `test_pacific_power_matches` | "Pacific Power" → Schedule 4, plausible rate + fixed charge |
| `test_pacificorp_matches_only_in_oregon` | OR schedule applies only to OR quotes — not UT/Rocky Mountain Power, nor the "Pacific Power" brand in WA/CA |
| `test_portland_general_is_not_california_pge` | Oregon PGE and California PG&E resolve to different schedules (~2.5x rate gap) |
| `test_central_electric_coop` | CEC (Redmond) → flat co-op rate + facilities charge |
| `test_midstate_electric_coop` | Midstate (La Pine) → flat rate + $35/mo facilities charge |
| `test_oregon_defaults_are_flat_not_tou` | OR default tariffs report `is_tou=False`, no hourly vector |
| `test_california_schedules_still_tou` | CA IOU schedules still expand to an 8760-hour TOU vector |
| `test_effective_dates_parsed_from_csv` | New `effective` CSV column parsed (2024 CA / 2026 OR vintages) |

### Class: `TestOregonNEM` — export policy (5 tests)

| Test | What it checks |
|------|---------------|
| `test_oregon_is_one_to_one_state` | OR in the 1:1 NEM state set |
| `test_investor_owned_utilities_get_full_retail` | Pacific Power/PGE → full retail, cites ORS 757.300 |
| `test_central_oregon_coops_net_monthly` | CEC/Midstate → "Co-op NEM (monthly netting)" at ~90% of retail |
| `test_coop_branch_is_oregon_only` | Similarly named utility outside OR doesn't hit the co-op branch |
| `test_unknown_utility_falls_back_to_state_policy` | No utility name → state 1:1 default |

### Class: `TestCityList` — report city list (3 tests)

| Test | What it checks |
|------|---------------|
| `test_entries_are_five_tuples_with_unique_slugs` | List shape + unique slugs + known states |
| `test_all_zips_exist_in_uszips_with_matching_state` | Every city ZIP resolves to the declared state |
| `test_central_oregon_cluster_present` | Bend, Redmond, Sisters, Prineville, Madras, La Pine, Sunriver, Terrebonne all present |

### Class: `TestOregonBatchFile` — batch ETL input (2 tests)

| Test | What it checks |
|------|---------------|
| `test_batch_csv_parses_and_covers_central_oregon` | `data/oregon_locations.csv` parses; all ZIPs are real OR ZIPs; Bend/Redmond/Sisters/La Pine covered |
| `test_etl_batch_missing_file_raises` | `etl_batch()` raises `FileNotFoundError` for a missing CSV |

### Class: `TestOregonYieldModel` — Cascade-aware yield (5 tests)

| Test | What it checks |
|------|---------------|
| `test_bend_beats_portland_despite_higher_latitude_neighbors` | Bend out-yields Portland by >20% (rain shadow) |
| `test_east_west_split_at_cascade_crest` | Same latitude, east of crest > west of crest |
| `test_yields_within_plausible_band` | All sample points within 900–1,750 kWh/kW/yr |
| `test_coastal_fog_discount` | Coast ZIPs discounted vs Willamette Valley |
| `test_california_dispatch_unchanged` | CA still uses the original latitude-only model |

### Class: `TestHeatmapUtilityAssumptions` — rate/export mapping (7 tests)

| Test | What it checks |
|------|---------------|
| `test_redmond_city_is_pacific_power` | 97756 → Pacific Power (city core dominant; CEC serves the fringe) |
| `test_sisters_is_coop_with_reduced_export` | 97759 → Central Electric Co-op, 0.90 export ratio |
| `test_bend_is_pacific_power_full_retail` | 97701 → Pacific Power, 1:1 export |
| `test_portland_is_pge` | 97202 → PGE |
| `test_eweb_is_eugene_city_only` | 97401 → EWEB; Roseburg 97470 (same ZIP3) → Pacific Power |
| `test_oregon_uses_near_term_escalation` | OR assumptions carry 5%/yr escalation |
| `test_california_assumptions_unchanged` | CA IOU/muni assumptions untouched |

### Classes: `TestPortfolioOregon` + `TestEIAFallback` (3 tests)

| Test | What it checks |
|------|---------------|
| `test_central_oregon_anchors_present` | Bend + Redmond in the portfolio anchor list |
| `test_high_desert_yield_override` | City-level multiplier puts Bend at ~1,475 kWh/kW/yr |
| `test_oregon_present_in_bundled_state_rates` | EIA fallback CSV has OR at $0.14/kWh |

---

## When tests fail

1. **Don't skip or delete them** — fix the underlying issue or update the expectation if the business logic legitimately changed.
2. **Read the assertion carefully** — pytest shows what was expected vs what was returned.
3. **Run the one failing test** in isolation with `-v -s` to see all output.
4. **Check if your change broke a downstream assumption** — e.g., changing a default in `Assumptions` will affect the San Jose case numbers.

## Adding new tests

When you add a new feature or fix a bug, add a test that would have caught it:

```python
# In the appropriate test class:
def test_my_new_feature(self):
    """One-line description of what you're protecting."""
    result = score_site(row)
    assert result.some_field == expected_value
```

Naming convention: `test_<what_it_does>` (snake_case, starts with `test_`).

---

## Test philosophy

- **Fast:** the full suite runs in <10 seconds so you run it often
- **Isolated:** no real network calls in CI (all HTTP mocked in `test_integration.py`)
- **Readable:** the test name alone should tell you what broke
- **One thing per test:** if a test fails, you should know exactly what's wrong
