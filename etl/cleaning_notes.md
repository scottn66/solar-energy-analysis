# TTS Data Cleaning Report

## Source

- File: `data/tts_raw.csv`
- Original rows: 3,664,197
- Original columns: 24
- Run date: 2026-04-22T18:01:12

## Cleaning steps applied

### Step 1: Sentinel replacement
- Replaced `-1` / `"-1"` with `NULL` across all 24 columns.
- Post-replacement null rates by column:

| Column | Null count | Null % |
|---|---:|---:|
| `battery_rated_capacity_kWh` | 3,499,440 | 95.50% |
| `ground_mounted` | 1,650,256 | 45.04% |
| `azimuth_1` | 1,554,076 | 42.41% |
| `tilt_1` | 1,552,430 | 42.37% |
| `tracking` | 1,402,684 | 38.28% |
| `efficiency_module_1` | 1,192,806 | 32.55% |
| `module_quantity_1` | 1,103,435 | 30.11% |
| `output_capacity_inverter_1` | 1,061,288 | 28.96% |
| `technology_module_1` | 975,615 | 26.63% |
| `module_model_1` | 944,304 | 25.77% |
| `total_installed_price` | 937,982 | 25.60% |
| `inverter_model_1` | 847,684 | 23.13% |
| `inverter_manufacturer_1` | 847,684 | 23.13% |
| `module_manufacturer_1` | 844,234 | 23.04% |
| `inverter_loading_ratio` | 832,352 | 22.72% |
| `rebate_or_grant` | 727,828 | 19.86% |
| `third_party_owned` | 635,641 | 17.35% |
| `zip_code` | 210,803 | 5.75% |
| `utility_service_territory` | 125,390 | 3.42% |
| `PV_system_size_DC` | 34,199 | 0.93% |
| `customer_segment` | 2,155 | 0.06% |
| `installer_name` | 4 | 0.00% |
| `state` | 0 | 0.00% |
| `installation_date` | 0 | 0.00% |

### Step 2: Drop missing economic fields
- Dropped 937,982 rows with null/invalid `total_installed_price`
- Dropped 34,199 rows with null/invalid `PV_system_size_DC`
- Overlap: 22,941 rows failed both filters
- Net rows remaining: 2,714,957

### Step 3: Filter to residential
- Dropped 65,210 rows with non-residential `customer_segment`
- Dropped 2,865 rows with `PV_system_size_DC > 100.0 kW`
- Net rows remaining: 2,646,882

### Step 4: Price outlier filtering
- Dropped 43,341 rows with `price_per_watt < $0.50/W`
- Dropped 8,073 rows with `price_per_watt > $15.00/W`
- Net rows remaining: 2,595,468

### Step 5: Case normalization
| Column | Duplicates collapsed | Example |
|---|---:|---|
| `utility_service_territory` | 11 | `SWEPCO` → `Swepco` |
| `inverter_model_1` | 34 | `M190-72-240-Sxx [240V]` → `M190-72-240-SXX [240V]` |
| `inverter_manufacturer_1` | 0 | `KACO` → `Kaco` |
| `module_manufacturer_1` | 1 | `REC Solar` → `Rec Solar` |
| `installer_name` | 0 | `no match` → `No Match` |
| `zip_code` | 2,001,720 | e.g. `94061.0` → `94061` |

### Step 6: Standardize unknowns
| Column | Replacements made |
|---|---:|
| `installer_name` | 143,537 |
| `inverter_manufacturer_1` | 4,127 |

### Step 7: Final validation
- Final rows: 2,595,468 (70.8% of original)
- Columns: 25 (original 24 + 1 derived `price_per_watt`)
- Sentinel check (no -1 remaining): PASS
- Zip format (5-digit string or null): PASS
- Price range ($0.50 <= $/W <= $15.00): PASS
- System size range (0 < kW <= 100.0): PASS

## Post-cleaning column profiles

| Column | Dtype | Non-null | Null % | Min / Top-3 | Median | Max |
|---|---|---:|---:|---|---|---|
| `installation_date` | str | 2,595,468 | 0.0% | 8092 unique | '2022-07-01'=6,676; '2021-07-01'=5,704; '2020-07-01'=4,272 | - |
| `PV_system_size_DC` | float64 | 2,595,468 | 0.0% | 0.075 | 6.300 | 100.000 |
| `total_installed_price` | float64 | 2,595,468 | 0.0% | 227.000 | 26400.000 | 1062071.310 |
| `rebate_or_grant` | float64 | 2,355,611 | 9.2% | 0.000 | 0.000 | 1106117.000 |
| `customer_segment` | str | 2,595,468 | 0.0% | 3 unique | 'RES_SF'=2,046,173; 'RES'=421,683; 'RES_MF'=127,612 | - |
| `tracking` | float64 | 1,958,269 | 24.6% | 0.000 | 0.000 | 1.000 |
| `ground_mounted` | float64 | 1,778,834 | 31.5% | 0.000 | 0.000 | 1.000 |
| `zip_code` | str | 2,565,241 | 1.2% | 10885 unique | '95648'=11,733; '93727'=10,450; '92584'=10,326 | - |
| `state` | str | 2,595,468 | 0.0% | 23 unique | 'CA'=1,744,629; 'AZ'=162,537; 'NY'=160,303 | - |
| `utility_service_territory` | str | 2,501,115 | 3.6% | 577 unique | 'Pacific Gas And Electric'=806,551; 'Southern California Edison'=617,706; 'San Diego Gas And Electric'=287,622 | - |
| `third_party_owned` | float64 | 2,314,904 | 10.8% | 0.000 | 0.000 | 1.000 |
| `installer_name` | str | 2,381,229 | 8.3% | 12599 unique | 'Sunrun'=349,641; 'Tesla'=264,575; 'Sunpower'=141,633 | - |
| `azimuth_1` | float64 | 1,971,422 | 24.0% | 0.000 | 180.000 | 360.000 |
| `tilt_1` | float64 | 1,972,997 | 24.0% | 0.000 | 20.000 | 90.000 |
| `module_manufacturer_1` | str | 2,215,268 | 14.6% | 400 unique | 'Qcells North America'=408,255; 'Sunpower'=288,982; 'Lg Electronics Inc.'=179,385 | - |
| `module_model_1` | str | 2,151,439 | 17.1% | 9313 unique | 'Q.PEAK DUO BLK ML-G10+ 400'=69,719; 'SPR-X21-350-BLK-E-AC'=47,709; 'Q.PEAK DUO BLK-G6+ 340'=35,930 | - |
| `module_quantity_1` | float64 | 2,229,160 | 14.1% | 1.000 | 18.000 | 45996.000 |
| `technology_module_1` | str | 2,122,463 | 18.2% | 5 unique | 'Mono-c-Si'=1,679,716; 'Multi-c-Si'=440,267; 'Thin Film'=2,435 | - |
| `efficiency_module_1` | float64 | 2,117,038 | 18.4% | 0.055 | 0.198 | 0.301 |
| `inverter_manufacturer_1` | str | 2,165,990 | 16.5% | 146 unique | 'Solaredge Technologies Ltd.'=760,611; 'Enphase Energy Inc.'=729,419; 'Sunpower'=206,500 | - |
| `inverter_model_1` | str | 2,170,117 | 16.4% | 2377 unique | 'SE3800H-US [240V]'=156,061; 'SE7600H-US [240V]'=135,136; 'IQ8PLUS-72-2-US [240V]'=111,319 | - |
| `output_capacity_inverter_1` | float64 | 2,168,465 | 16.5% | 0.175 | 3.500 | 2079.000 |
| `inverter_loading_ratio` | float64 | 2,274,001 | 12.4% | 0.500 | 1.160 | 2.500 |
| `battery_rated_capacity_kWh` | float64 | 135,198 | 94.8% | 0.100 | 13.200 | 194000.000 |
| `price_per_watt` | float64 | 2,595,468 | 0.0% | 0.500 | 4.250 | 15.000 |

## Decision rationale

- **Sentinel -1 replaced rather than dropped** because LBNL uses it uniformly as "not reported" — these are structurally missing, not erroneous.
- **Price rows dropped rather than imputed** because imputing installed price would corrupt all downstream economic metrics (LCOE, payback, price_per_watt).
- **Residential filter applied** because the project scope is residential solar viability.
- **Price outlier bounds ($0.50–$15.00/W)** based on NREL cost benchmarks; values outside this range are almost certainly data errors or non-standard transactions.
