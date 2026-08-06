#!/usr/bin/env python3
"""
build_city_reports.py — Batch-generate solar viability reports for a curated
list of cities (California: top 15 by population + Stanford + Berkeley;
Oregon: statewide anchors + the Central Oregon cluster around Bend/Redmond)
and emit:

  1. One standalone HTML report per city  →  _deploy/solar/reports/<slug>.html
  2. A manifest of card metrics           →  _deploy/solar/reports/_manifest.json

The manifest is consumed by build_reports_index.py to regenerate the reports
landing page card grid.

Usage:
    python3 build_city_reports.py              # all cities
    python3 build_city_reports.py berkeley     # one slug (for testing)
    python3 build_city_reports.py --state OR   # only one state's cities
    python3 build_city_reports.py --system-kw 6.0
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

from solar_fetch import quote_from_location
from solar_geocode import GeocodeError
from solar_pvwatts import PVWattsError
from solar_viz import generate_report

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REPORTS_DIR = Path("_deploy/solar/reports")
MANIFEST    = REPORTS_DIR / "_manifest.json"
SYSTEM_KW   = 4.5   # match existing cards; uniform system for fair comparison

# State-level TTS median install price ($/W, LBNL Tracking the Sun 2024)
STATE_PPW = {"CA": 3.80, "OR": 3.40}

# ──────────────────────────────────────────────────────────────────────────────
# City list — (slug, display_name, state, zip, population_rank_or_note)
# CA: top 15 most-populous cities (2023 est.) + Stanford + Berkeley.
# OR: statewide anchors (Portland, Salem, Eugene, Medford) + the Central
#     Oregon high-desert cluster around Bend/Redmond.
# ZIP is a central/representative ZIP for each city.
# ──────────────────────────────────────────────────────────────────────────────
CITIES = [
    ("los-angeles",  "Los Angeles",  "CA", "90012", "#1 · 3.8M"),
    ("san-diego",    "San Diego",    "CA", "92101", "#2 · 1.4M"),
    ("san-jose",     "San Jose",     "CA", "95113", "#3 · 1.0M"),
    ("san-francisco","San Francisco","CA", "94103", "#4 · 810K"),
    ("fresno",       "Fresno",       "CA", "93721", "#5 · 545K"),
    ("sacramento",   "Sacramento",   "CA", "95814", "#6 · 528K"),
    ("long-beach",   "Long Beach",   "CA", "90802", "#7 · 451K"),
    ("oakland",      "Oakland",      "CA", "94607", "#8 · 419K"),
    ("bakersfield",  "Bakersfield",  "CA", "93301", "#9 · 413K"),
    ("anaheim",      "Anaheim",      "CA", "92805", "#10 · 345K"),
    ("stockton",     "Stockton",     "CA", "95202", "#11 · 322K"),
    ("riverside",    "Riverside",    "CA", "92501", "#12 · 315K"),
    ("irvine",       "Irvine",       "CA", "92618", "#13 · 311K"),
    ("santa-ana",    "Santa Ana",    "CA", "92701", "#14 · 310K"),
    ("chula-vista",  "Chula Vista",  "CA", "91910", "#15 · 276K"),
    ("stanford",     "Stanford",     "CA", "94305", "University"),
    ("berkeley",     "Berkeley",     "CA", "94704", "UC Berkeley"),
    # Legacy Peninsula / foothills examples (kept for continuity)
    ("atherton",     "Atherton",     "CA", "94027", "Peninsula"),
    ("redwood-city", "Redwood City", "CA", "94061", "Peninsula"),
    ("arnold",       "Arnold",       "CA", "95223", "Sierra foothills"),
    # Oregon — statewide anchors
    ("portland-or",  "Portland",     "OR", "97202", "#1 OR · 630K"),
    ("salem-or",     "Salem",        "OR", "97301", "#2 OR · 180K"),
    ("eugene-or",    "Eugene",       "OR", "97401", "#3 OR · 178K"),
    ("medford-or",   "Medford",      "OR", "97501", "SW Oregon · 89K"),
    # Oregon — Central Oregon high desert (Bend/Redmond metro)
    ("bend-or",      "Bend",         "OR", "97701", "Central OR · 103K"),
    ("redmond-or",   "Redmond",      "OR", "97756", "Central OR · 37K"),
    ("sisters-or",   "Sisters",      "OR", "97759", "Central OR · 3.5K"),
    ("prineville-or","Prineville",   "OR", "97754", "Central OR · 11K"),
    ("madras-or",    "Madras",       "OR", "97741", "Central OR · 8K"),
    ("la-pine-or",   "La Pine",      "OR", "97739", "Central OR · 3K"),
    ("sunriver-or",  "Sunriver",     "OR", "97707", "Central OR resort"),
    ("terrebonne-or","Terrebonne",   "OR", "97760", "Smith Rock"),
]


def extract_card(slug, name, state, zip_code, note, quote) -> dict:
    """Pull the handful of fields the landing-page card needs."""
    d = quote.to_dict()
    site = d["site"]
    pvw  = d["pvwatts"]
    rate = d["rate"]
    exp  = d["export"]
    meta = d["meta"]

    # Annual production — try common field names
    annual_kwh = (
        pvw.get("ac_annual_kwh")
        or pvw.get("pvwatts_ac_annual_kwh")
        or pvw.get("annual_kwh")
        or site.get("year_production", [0])[0]
    )

    return {
        "slug":        slug,
        "name":        name,
        "state":       state,
        "zip":         zip_code,
        "note":        note,
        "score":       round(site["viability_score"], 0),
        "label":       site["viability_label"],
        "lcoe":        round(site["lcoe"], 3),
        "payback":     round(site["simple_payback_years"], 1),
        "npv":         round(site.get("npv", 0), 0),
        "system_kw":   round(meta["system_kw_used"], 1),
        "annual_kwh":  round(annual_kwh, 0) if annual_kwh else None,
        "utility":     rate.get("utility_name") or "—",
        "rate_name":   rate.get("rate_name") or "",
        "flat_rate":   round(rate["flat_rate"], 3) if rate.get("flat_rate") else None,
        "is_tou":      rate.get("is_tou", False),
        "export_label": _export_label(exp, rate),
        "confidence":  meta.get("confidence_level", "—"),
        "resolved":    d["geocode"].get("resolved_address", ""),
        "lat":         d["geocode"].get("lat"),
        "lon":         d["geocode"].get("lon"),
    }


def _export_label(exp: dict, rate: dict) -> str:
    """Short human label for the export/net-metering regime.

    Only the three CPUC-regulated CA IOUs (PG&E, SCE, SDG&E) are under
    NEM 3.0.  Publicly-owned/municipal utilities (LADWP, SMUD, Anaheim,
    Riverside) and CCAs run their own net-metering tariffs.  Oregon offers
    full 1:1 retail-rate net metering statewide (ORS 757.300), so the
    export policy from solar_nem is surfaced directly for non-CA-IOU
    utilities when available.
    """
    util = (rate.get("utility_name") or "").lower()
    iou_keys = (
        "pacific gas", "pg&e",
        "southern california edison", "sce",
        "san diego gas", "sdg&e",
    )
    # "Portland General Electric" is Oregon's PGE, not California's PG&E —
    # exclude it explicitly before the substring check.
    if "portland general" not in util and any(k in util for k in iou_keys):
        return "NEM 3.0"
    policy = ((exp or {}).get("policy_name") or "")
    if policy.startswith("1:1"):
        return "1:1 net metering"
    if policy.startswith("Co-op"):
        return "Co-op monthly netting"
    return "Net metering"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    system_kw = SYSTEM_KW
    for a in sys.argv[1:]:
        if a.startswith("--system-kw"):
            # supports "--system-kw 6" handled by next arg; also "--system-kw=6"
            if "=" in a:
                system_kw = float(a.split("=", 1)[1])
    # handle "--system-kw 6.0" space form
    if "--system-kw" in sys.argv:
        i = sys.argv.index("--system-kw")
        if i + 1 < len(sys.argv):
            system_kw = float(sys.argv[i + 1])
            args = [x for x in args if x != sys.argv[i + 1]]

    state_filter = None
    for a in sys.argv[1:]:
        if a.startswith("--state="):
            state_filter = a.split("=", 1)[1].strip().upper()
    if "--state" in sys.argv:
        i = sys.argv.index("--state")
        if i + 1 < len(sys.argv):
            state_filter = sys.argv[i + 1].strip().upper()
            args = [x for x in args if x != sys.argv[i + 1]]

    selected = CITIES
    if state_filter:
        selected = [c for c in selected if c[2] == state_filter]
        if not selected:
            sys.exit(f"No cities for state {state_filter!r}. "
                     f"Known states: {sorted({c[2] for c in CITIES})}")
    if args:
        wanted = set(args)
        selected = [c for c in selected if c[0] in wanted]
        if not selected:
            sys.exit(f"No matching slugs in: {[c[0] for c in CITIES]}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing manifest so single-city runs don't wipe the rest
    manifest: dict[str, dict] = {}
    if MANIFEST.exists():
        try:
            manifest = {c["slug"]: c for c in json.loads(MANIFEST.read_text())}
        except Exception:
            manifest = {}

    print("=" * 64)
    print(f"  Batch report generation — {len(selected)} cities @ {system_kw} kW")
    print("=" * 64)

    ok, failed = 0, []
    for idx, (slug, name, state, zip_code, note) in enumerate(selected, 1):
        out = REPORTS_DIR / f"{slug}.html"
        print(f"\n[{idx}/{len(selected)}] {name}, {state} ({zip_code}) → {out.name}")
        t0 = time.time()
        try:
            quote = quote_from_location(
                location=zip_code,
                system_kw=system_kw,
                detailed=True,
            )
            # Build row for the report (mirrors solar_fetch.main)
            row = quote.pvwatts_result.to_dict()
            row.update({
                "site_id":            quote.site_result.site_id,
                "address_label":      f"{name}, {state} {zip_code}",
                "lat":                quote.geocode_result.lat,
                "lon":                quote.geocode_result.lon,
                "system_capacity_kw": quote.system_kw_used,
                "state":              state,
                "zip_code":           zip_code,
                "azimuth":            180.0,
                "tilt":               abs(quote.geocode_result.lat),
                "losses":             14.0,
                "tts_recent_sample_size":     1000,
                "tts_median_price_per_watt":  STATE_PPW.get(state, 3.50),
            })
            generate_report([quote.site_result], str(out), rows=[row], quote=quote)

            card = extract_card(slug, name, state, zip_code, note, quote)
            manifest[slug] = card
            dt = time.time() - t0
            print(f"      score={card['score']:.0f}  payback={card['payback']} yr  "
                  f"LCOE=${card['lcoe']}  util={card['utility'][:28]}  ({dt:.1f}s)")
            ok += 1
        except (GeocodeError, PVWattsError) as e:
            print(f"      FAILED: {e}")
            failed.append((slug, str(e)))
        except Exception as e:
            print(f"      ERROR: {type(e).__name__}: {e}")
            failed.append((slug, f"{type(e).__name__}: {e}"))

    # Write manifest ordered by CITIES order
    ordered = [manifest[c[0]] for c in CITIES if c[0] in manifest]
    MANIFEST.write_text(json.dumps(ordered, indent=2))

    print("\n" + "=" * 64)
    print(f"  Done: {ok} ok, {len(failed)} failed.  Manifest: {MANIFEST}")
    if failed:
        for slug, err in failed:
            print(f"    ✗ {slug}: {err}")
    print("=" * 64)


if __name__ == "__main__":
    main()
