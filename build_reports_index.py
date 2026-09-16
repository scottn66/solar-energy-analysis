#!/usr/bin/env python3
"""
build_reports_index.py — Regenerate _deploy/solar/reports/index.html from the
_manifest.json produced by build_city_reports.py.

Cards are sorted by viability score (desc). Each card shows a score dial,
population-rank chip, and the key economics. Self-contained, no JS framework.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

REPORTS_DIR = Path("_deploy/solar/reports")
MANIFEST    = REPORTS_DIR / "_manifest.json"
OUT         = REPORTS_DIR / "index.html"

# Utility display abbreviations (substring → short label)
# "portland general" must come before any PG&E-ish pattern — Oregon's PGE
# is a different company from California's PG&E.
UTIL_SHORT = [
    ("portland general",            "PGE (Oregon)"),
    ("pacific gas",                 "PG&E"),
    ("southern california edison",  "SCE"),
    ("san diego gas",               "SDG&E"),
    ("los angeles department",      "LADWP"),
    ("sacramento municipal",        "SMUD"),
    ("city & county of san franc",  "CleanPowerSF"),
    ("city of anaheim",             "Anaheim PU"),
    ("city of riverside",           "Riverside PU"),
    ("pacific power",               "Pacific Power"),
    ("pacificorp",                  "Pacific Power"),
    ("portland general",            "PGE"),
    ("central electric",            "Central Electric Co-op"),
    ("midstate electric",           "Midstate Electric Co-op"),
]


def util_short(name: str) -> str:
    low = (name or "").lower()
    for key, short in UTIL_SHORT:
        if key in low:
            return short
    return name or "—"


def score_color(s: float) -> str:
    """Score → score-dial background color."""
    if s >= 90:   return "#1B9E77"   # deep green
    if s >= 80:   return "#26A69A"   # teal
    if s >= 65:   return "#F39C12"   # amber
    if s >= 50:   return "#E67E22"   # orange
    return "#95A5A6"                 # gray


def verdict_style(s: float) -> tuple[str, str]:
    """(bg, fg) colors for the verdict pill."""
    if s >= 80:   return ("#E8F5E9", "#2E7D32")
    if s >= 65:   return ("#FFF8E1", "#F57F17")
    if s >= 50:   return ("#FFF3E0", "#E65100")
    return ("#ECEFF1", "#546E7A")


REGION_KEYWORDS = {
    "los-angeles": "socal la basin", "san-diego": "socal coast",
    "san-jose": "bay area south bay", "san-francisco": "bay area peninsula",
    "fresno": "central valley", "sacramento": "central valley capital",
    "long-beach": "socal la county", "oakland": "bay area east bay",
    "bakersfield": "central valley kern", "anaheim": "socal orange county",
    "stockton": "central valley", "riverside": "socal inland empire",
    "irvine": "socal orange county", "santa-ana": "socal orange county",
    "chula-vista": "socal south bay san diego", "stanford": "bay area peninsula university",
    "berkeley": "bay area east bay university", "atherton": "bay area peninsula",
    "redwood-city": "bay area peninsula", "arnold": "sierra foothills",
    # Oregon
    "portland-or": "willamette valley portland metro",
    "salem-or": "willamette valley capital",
    "eugene-or": "willamette valley lane county",
    "medford-or": "rogue valley southern oregon",
    "bend-or": "central oregon high desert deschutes",
    "redmond-or": "central oregon high desert deschutes",
    "sisters-or": "central oregon high desert deschutes",
    "prineville-or": "central oregon high desert crook",
    "madras-or": "central oregon high desert jefferson",
    "la-pine-or": "central oregon high desert deschutes",
    "sunriver-or": "central oregon high desert deschutes resort",
    "terrebonne-or": "central oregon high desert smith rock",
}

STATE_SEARCH_WORDS = {"CA": "california ca", "OR": "oregon or"}


def card_html(c: dict) -> str:
    name   = html.escape(c["name"])
    state  = c.get("state", "CA")   # older manifests predate the state field
    zipc   = html.escape(str(c["zip"]))
    slug   = c["slug"]
    score  = c["score"]
    util   = util_short(c["utility"])
    util_e = html.escape(util)
    annual = f'{c["annual_kwh"]:,.0f} kWh' if c.get("annual_kwh") else "—"
    sys_kw = f'{c["system_kw"]:.1f} kW'
    lcoe   = f'${c["lcoe"]:.3f}/kWh'
    payback= f'{c["payback"]:.1f} yr'
    export = html.escape(c["export_label"])
    note   = html.escape(c.get("note", ""))
    vbg, vfg = verdict_style(score)
    label  = html.escape(c["label"])
    search = html.escape(
        f'{name} {zipc} {util} {export} {REGION_KEYWORDS.get(slug, "")} '
        f'solar {STATE_SEARCH_WORDS.get(state, state.lower())}'.lower()
    )

    return f"""    <a class="card" href="{slug}.html" data-s="{search}">
      <div class="card-top">
        <div class="score-circle" style="background:{score_color(score)};box-shadow:0 3px 10px {score_color(score)}55;"><span>{score:.0f}</span></div>
        <div class="card-title">
          <h3>{name}</h3>
          <div class="zip">{state} &middot; ZIP {zipc} &middot; <span class="rank">{note}</span></div>
        </div>
      </div>
      <div class="card-body">
        <dl class="detail-grid">
          <dt>System Size</dt><dd>{sys_kw}</dd>
          <dt>Annual Yield</dt><dd>{annual}</dd>
          <dt>LCOE</dt><dd>{lcoe}</dd>
          <dt>Payback</dt><dd>{payback}</dd>
          <dt>Utility</dt><dd>{util_e}</dd>
          <dt>Export</dt><dd>{export}</dd>
        </dl>
        <span class="verdict" style="background:{vbg};color:{vfg};">{label}</span>
      </div>
    </a>"""


def build() -> str:
    cards = json.loads(MANIFEST.read_text())
    # Sort by score desc, then payback asc
    cards.sort(key=lambda c: (-c["score"], c["payback"]))
    cards_html = "\n\n".join(card_html(c) for c in cards)
    n = len(cards)
    best = cards[0]
    best_line = f'{html.escape(best["name"])} leads at {best["score"]:.0f}/100'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Solar Viability Reports</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: #F4F5F7;
    color: #1A1A2E;
    -webkit-font-smoothing: antialiased;
  }}

  /* ---------- Hero ---------- */
  .hero {{
    background: linear-gradient(135deg, #1B1464 0%, #2D2B8C 45%, #4A3FB5 100%);
    color: #fff;
    padding: 3.5rem 1.5rem 3rem;
    text-align: center;
  }}
  .hero h1 {{ font-size: 2.25rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 0.5rem; }}
  .hero p {{ font-size: 1.05rem; opacity: 0.82; max-width: 580px; margin: 0 auto 1.75rem; line-height: 1.55; }}
  .hero-nav {{ display: flex; gap: 0.75rem; justify-content: center; flex-wrap: wrap; margin-bottom: 1.75rem; }}
  .hero-nav a {{
    display: inline-block; padding: 0.5rem 1.25rem; border-radius: 24px;
    font-size: 0.88rem; font-weight: 600; text-decoration: none;
    background: rgba(255,255,255,0.14); color: #fff;
    border: 1.5px solid rgba(255,255,255,0.4); transition: background 0.18s;
  }}
  .hero-nav a:hover {{ background: rgba(255,255,255,0.26); }}

  /* ---------- Search ---------- */
  .search-wrap {{ max-width: 500px; margin: 0 auto; position: relative; }}
  .search-wrap svg {{ position: absolute; left: 16px; top: 50%; transform: translateY(-50%); opacity: 0.45; }}
  .search-wrap input {{
    width: 100%; padding: 0.9rem 1rem 0.9rem 2.8rem; border: none; border-radius: 14px;
    font-size: 1rem; font-family: inherit; outline: none;
    background: rgba(255,255,255,0.14); color: #fff;
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); transition: background 0.2s;
  }}
  .search-wrap input::placeholder {{ color: rgba(255,255,255,0.5); }}
  .search-wrap input:focus {{ background: rgba(255,255,255,0.24); }}

  /* ---------- Container ---------- */
  .container {{ max-width: 1080px; margin: 0 auto; padding: 2rem 1.5rem 1rem; }}
  .result-count {{ font-size: 0.85rem; color: #888; margin-bottom: 1rem; font-weight: 500; }}

  /* ---------- Cards grid ---------- */
  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(285px, 1fr));
    gap: 1.25rem;
    margin-bottom: 1.5rem;
  }}
  .card {{
    background: #fff; border-radius: 16px; overflow: hidden;
    box-shadow: 0 2px 10px rgba(27,20,100,0.07);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
    text-decoration: none; color: inherit; display: flex; flex-direction: column;
  }}
  .card:hover {{ transform: translateY(-5px); box-shadow: 0 10px 28px rgba(27,20,100,0.14); }}

  .card-top {{ display: flex; align-items: center; gap: 1rem; padding: 1.25rem 1.25rem 0.65rem; }}
  .score-circle {{
    width: 54px; height: 54px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }}
  .score-circle span {{ color: #fff; font-weight: 800; font-size: 1.2rem; }}
  .card-title h3 {{ font-size: 1.1rem; font-weight: 700; margin-bottom: 0.1rem; color: #1B1464; }}
  .card-title .zip {{ font-size: 0.82rem; color: #777; font-weight: 500; }}
  .card-title .rank {{ color: #4A3FB5; font-weight: 600; }}

  .card-body {{ padding: 0 1.25rem 1.25rem; flex: 1; }}
  .detail-grid {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 0.45rem 0.75rem;
    font-size: 0.84rem; margin-top: 0.65rem;
  }}
  .detail-grid dt {{ color: #8D8D8D; font-weight: 500; }}
  .detail-grid dd {{ font-weight: 600; text-align: right; color: #1A1A2E; }}
  .verdict {{
    display: inline-block; margin-top: 0.85rem; padding: 0.3rem 0.85rem;
    border-radius: 20px; font-size: 0.76rem; font-weight: 600; letter-spacing: 0.02em;
  }}

  /* ---------- No-match ---------- */
  .no-match {{ display: none; text-align: center; padding: 2.5rem 1rem; color: #666; }}
  .no-match.visible {{ display: block; }}
  .no-match h3 {{ font-size: 1.15rem; margin-bottom: 0.5rem; color: #1B1464; }}
  .no-match code {{
    display: block; margin: 0.85rem auto 0; background: #EAECF0;
    padding: 0.7rem 1.1rem; border-radius: 10px; font-size: 0.84rem;
    text-align: left; max-width: 440px; color: #1B1464;
  }}

  /* ---------- About ---------- */
  .about {{ margin-top: 1.5rem; padding: 2rem; background: #fff; border-radius: 16px; box-shadow: 0 2px 10px rgba(27,20,100,0.05); }}
  .about h2 {{ font-size: 1.2rem; font-weight: 700; margin-bottom: 0.85rem; color: #1B1464; }}
  .about p {{ font-size: 0.9rem; line-height: 1.7; color: #555; margin-bottom: 0.6rem; }}
  .about a {{ color: #26A69A; text-decoration: none; font-weight: 500; }}
  .about a:hover {{ text-decoration: underline; }}

  footer {{ text-align: center; padding: 2.25rem 1rem; font-size: 0.78rem; color: #AAA; letter-spacing: 0.01em; }}

  @media (max-width: 600px) {{
    .hero h1 {{ font-size: 1.65rem; }}
    .cards {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<!-- ===== Hero ===== -->
<div class="hero">
  <h1>Is solar worth it in your city?</h1>
  <p>Independent viability reports for {n} locations across California and Oregon &mdash; from the largest cities to the Central Oregon high desert around Bend and Redmond &mdash; powered by NREL irradiance data and live utility rate detection.</p>
  <div class="hero-nav">
    <a href="../">&#8592; Project Home</a>
    <a href="../heatmap/">View ZIP Heatmaps &rarr;</a>
  </div>
  <div class="search-wrap">
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    <input type="text" id="search" placeholder="Search by city, ZIP, utility, or region..." autocomplete="off">
  </div>
</div>

<!-- ===== Cards ===== -->
<div class="container">
  <div class="result-count" id="result-count">Showing all {n} reports &middot; sorted by viability score &middot; {best_line}</div>
  <div class="cards" id="cards">

{cards_html}

  </div>

  <!-- No-match message -->
  <div class="no-match" id="no-match">
    <h3>No matching report found</h3>
    <p>We don't have a report for that location yet. You can generate one from the command line:</p>
    <code>python -m solar_fetch YOUR_ZIP --system-kw 4.5 --output report.html</code>
  </div>

  <!-- About section -->
  <div class="about">
    <h2>About These Reports</h2>
    <p>
      Each report models a 4.5&nbsp;kW south-facing residential rooftop system using irradiance and
      weather data from the <a href="https://developer.nrel.gov/" target="_blank" rel="noopener">NREL Developer Network</a>
      (PVWatts&nbsp;v8). The utility and electricity rate are detected automatically per ZIP; for the three
      CPUC-regulated investor-owned utilities (PG&amp;E, SCE, SDG&amp;E) we apply current <strong>NEM&nbsp;3.0</strong>
      export pricing, while municipal utilities (LADWP, SMUD, Anaheim, Riverside) use their own net-metering
      tariffs. Oregon locations use the state's <strong>1:1 retail-rate net metering</strong> (ORS&nbsp;757.300)
      with rates from Portland General Electric, Pacific Power, or the Central Oregon electric co-ops.
    </p>
    <p>
      Viability scores (0&ndash;100) combine levelized cost of energy (LCOE), simple payback period,
      net present value (NPV), solar resource quality, panel orientation, and local market maturity
      into a single weighted index (25% resource + 50% economics + 15% site fit + 10% policy).
      A score of 65 or above is rated "Good" for most homeowners. California locations clear that bar
      easily on high retail rates; Oregon's cheap hydro-heavy electricity stretches paybacks even where
      the solar resource is excellent, as it is east of the Cascades in Bend and Redmond.
    </p>
    <p>
      Data sources: <a href="https://pvwatts.nrel.gov/" target="_blank" rel="noopener">NREL PVWatts</a>,
      <a href="https://openei.org/wiki/Utility_Rate_Database" target="_blank" rel="noopener">OpenEI URDB</a>,
      <a href="https://www.eia.gov/" target="_blank" rel="noopener">EIA rate data</a>, and
      <a href="https://emp.lbl.gov/tracking-the-sun" target="_blank" rel="noopener">Berkeley Lab TTS</a>.
      Started as a San Jose State DATA&nbsp;201 project; now an independent model.
      &mdash; <a href="../heatmap/">Explore the ZIP-level heatmaps (California &amp; Oregon) &rarr;</a>
    </p>
    <p style="font-size:0.82rem;color:#888;margin-top:0.75rem;">
      Independent research model — not financial, tax, or installation advice.
    </p>
  </div>
</div>

<footer>
  Solar viability reports &middot; Scott Nelson &middot;
  <a href="https://github.com/scottn66/solar-energy-analysis" style="color:#26A69A;">GitHub</a>
</footer>

<script>
(function () {{
  var input = document.getElementById('search');
  var cards = document.querySelectorAll('.card');
  var noMatch = document.getElementById('no-match');
  var count = document.getElementById('result-count');
  var total = cards.length;

  input.addEventListener('input', function () {{
    var q = this.value.toLowerCase().trim();
    var visible = 0;
    cards.forEach(function (card) {{
      var s = (card.getAttribute('data-s') || '').toLowerCase();
      var show = !q || s.indexOf(q) !== -1;
      card.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    noMatch.className = visible === 0 && q ? 'no-match visible' : 'no-match';
    count.textContent = q
      ? 'Showing ' + visible + ' of ' + total + ' reports'
      : 'Showing all ' + total + ' reports · sorted by viability score';
  }});
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    OUT.write_text(build())
    n = len(json.loads(MANIFEST.read_text()))
    print(f"Wrote {OUT} with {n} city cards.")
