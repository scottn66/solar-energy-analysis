# Working Notes

A running log of decisions, surprises, and handoffs. Most recent entries on top.

---

## 2026-04-22 — TTS raw data cleaning pipeline

**Built:** `etl/clean_tts.py` — a standalone pandas-only script that takes the raw LBNL Tracking the Sun CSV and produces a cleaned residential-only dataset ready for warehouse loading.

**Input → output:**
- Raw: `data/tts_raw.csv` (3,664,197 rows × 24 cols, 1.8 GB)
- Clean: `data/tts_cleaned.csv` (2,595,468 rows × 25 cols, 595 MB)
- Report: `etl/cleaning_notes.md`
- Log: `etl/cleaning.log`

**Row funnel (70.8% retained):**

| Step | Before | Removed | After |
|---|---:|---:|---:|
| Load raw | 3,664,197 | 0 | 3,664,197 |
| Drop missing economics (step 2) | 3,664,197 | 949,240 | 2,714,957 |
| Residential filter (step 3) | 2,714,957 | 68,075 | 2,646,882 |
| Price outliers (step 4) | 2,646,882 | 51,414 | 2,595,468 |

**Surprises in the data:**

1. **The -1 sentinel was hiding enormous missingness.** After replacement, `battery_rated_capacity_kWh` was 95.5% null (most systems are battery-less) and `ground_mounted` / `azimuth_1` / `tilt_1` were 38–45% null. LBNL's raw CSV reports 100% completeness because the sentinel is not a true null.

2. **ZIP codes were floats.** 2,001,720 rows had zips like `94061.0` from pandas coercion. Step 5 strips the `.0` and zero-pads to 5 digits.

3. **"Redacted" showed up 143,537 times as an installer name.** Step 6 now converts those to null (along with "Unknown", "N/A", etc.). The first run missed these because I checked `is_object_dtype` but pandas 4 uses the newer `StringDtype`; fixed with `_is_string_like()` helper.

4. **Price outliers are rare but real.** 43,341 rows had `$/W < $0.50` and 8,073 had `$/W > $15.00`. Both bounds preserve legitimate low-cost (bulk/large-system) and high-cost (post-incentive reporting) deals while dropping obvious data-entry errors.

5. **Top installers are concentrated:** Sunrun (349,641 installs), Tesla (264,575), Sunpower-era contractors. The long tail has 12,599 other installer names.

6. **Residential subcategories:** 3 values in `customer_segment` — `RES_SF` (single-family) dominates at 2.05M rows, `RES` (generic) at 422K, `RES_MF` (multi-family) at 128K.

**Key quality checks (all PASS):**
- No `-1` sentinels remain anywhere
- All zip_code values are 5-character strings or null
- All price_per_watt values are in [$0.50, $15.00]
- All PV_system_size_DC values are in (0, 100] kW

**Idempotence verified:** running the script twice on the same input produces byte-identical output (MD5 matches).

**Next step:** The cleaned CSV is ready for the warehouse. Suggested approach: add a `raw_tts_installations` staging table to `solar_warehouse.py` and a `solar_etl.py --load-tts` subcommand that bulk-inserts the cleaned CSV. From there, a `fact_tts_installations` mart table can feed the `tts_recent_sample_size` signal used by the viability scoring engine (currently hardcoded to 1000).

---
