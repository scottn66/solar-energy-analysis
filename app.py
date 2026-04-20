"""
app.py — Minimal FastAPI + HTMX frontend for solar viability quotes.

Run:
    uvicorn app:app --reload

Single file, no build step. Uses HTMX for seamless form → report swaps.
"""

from __future__ import annotations

import logging
import time
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from solar_fetch import quote_from_location, QuoteResult
from solar_geocode import GeocodeError
from solar_pvwatts import PVWattsError
from solar_urdb import URDBError
from solar_viz import generate_report_html, PALETTE
from solar_warehouse import latest_quote, quote_from_dict
from solar_etl import etl_quote
import json

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = FastAPI(title="Solar Viability Tool", version="1.0.0")


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------
LANDING_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Is solar worth it?</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://unpkg.com/htmx.org@2.0.4"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: {PALETTE['bg_page']};
            color: {PALETTE['text_primary']};
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }}

        .hero {{
            flex: 0 0 auto;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 80px 24px 48px;
            text-align: center;
        }}

        .hero h1 {{
            font-size: 42px;
            font-weight: 700;
            letter-spacing: -1.2px;
            line-height: 1.15;
            color: {PALETTE['indigo']};
            max-width: 600px;
            margin-bottom: 12px;
        }}

        .hero .subtitle {{
            font-size: 17px;
            color: {PALETTE['text_secondary']};
            max-width: 480px;
            line-height: 1.6;
            margin-bottom: 36px;
        }}

        .search-form {{
            width: 100%;
            max-width: 560px;
        }}

        .search-row {{
            display: flex;
            gap: 8px;
        }}

        .search-input {{
            flex: 1;
            padding: 14px 20px;
            font-size: 16px;
            font-family: inherit;
            border: 2px solid {PALETTE['divider']};
            border-radius: 12px;
            background: white;
            color: {PALETTE['text_primary']};
            outline: none;
            transition: border-color 0.2s;
        }}
        .search-input:focus {{
            border-color: {PALETTE['indigo']};
        }}
        .search-input::placeholder {{
            color: {PALETTE['warm_gray']};
        }}

        .search-btn {{
            padding: 14px 28px;
            font-size: 15px;
            font-weight: 600;
            font-family: inherit;
            background: linear-gradient(135deg, {PALETTE['indigo']} 0%, {PALETTE['indigo_light']} 100%);
            color: white;
            border: none;
            border-radius: 12px;
            cursor: pointer;
            transition: transform 0.15s, box-shadow 0.15s;
            white-space: nowrap;
        }}
        .search-btn:hover {{
            transform: translateY(-1px);
            box-shadow: 0 4px 16px rgba(27, 20, 100, 0.25);
        }}

        .advanced-toggle {{
            margin-top: 16px;
            font-size: 13px;
            color: {PALETTE['text_secondary']};
            cursor: pointer;
            user-select: none;
        }}
        .advanced-toggle:hover {{
            color: {PALETTE['indigo']};
        }}

        .advanced-fields {{
            display: none;
            margin-top: 16px;
            text-align: left;
            background: white;
            border: 1px solid {PALETTE['divider']};
            border-radius: 10px;
            padding: 20px;
        }}
        .advanced-fields.open {{
            display: block;
        }}

        .field-row {{
            display: flex;
            gap: 16px;
            margin-bottom: 12px;
        }}
        .field-group {{
            flex: 1;
        }}
        .field-group label {{
            display: block;
            font-size: 12px;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            color: {PALETTE['text_secondary']};
            margin-bottom: 4px;
        }}
        .field-group input {{
            width: 100%;
            padding: 10px 14px;
            font-size: 14px;
            font-family: inherit;
            border: 1px solid {PALETTE['divider']};
            border-radius: 8px;
            background: {PALETTE['bg_page']};
            outline: none;
        }}
        .field-group input:focus {{
            border-color: {PALETTE['indigo']};
        }}

        /* HTMX loading indicator */
        #spinner {{
            display: none;
            text-align: center;
            padding: 60px 20px;
        }}
        .htmx-request #spinner {{
            display: block;
        }}
        .htmx-request #result-content {{
            display: none;
        }}
        .spinner-text {{
            font-size: 16px;
            color: {PALETTE['text_secondary']};
            margin-top: 16px;
        }}
        .spinner-dot {{
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: {PALETTE['amber']};
            animation: pulse 1.2s ease-in-out infinite;
        }}
        .spinner-dot:nth-child(2) {{ animation-delay: 0.2s; }}
        .spinner-dot:nth-child(3) {{ animation-delay: 0.4s; }}
        @keyframes pulse {{
            0%, 80%, 100% {{ transform: scale(0.6); opacity: 0.4; }}
            40% {{ transform: scale(1); opacity: 1; }}
        }}

        #result {{
            width: 100%;
        }}

        .error-card {{
            max-width: 560px;
            margin: 32px auto;
            padding: 24px;
            background: white;
            border: 1px solid {PALETTE['coral']};
            border-radius: 12px;
            text-align: center;
        }}
        .error-card h3 {{
            color: {PALETTE['coral']};
            margin-bottom: 8px;
        }}
        .error-card p {{
            color: {PALETTE['text_secondary']};
            font-size: 14px;
        }}

        .footer {{
            text-align: center;
            padding: 32px;
            color: {PALETTE['warm_gray']};
            font-size: 12px;
            margin-top: auto;
        }}
    </style>
</head>
<body>

<div class="hero">
    <h1>Is solar worth it at your address?</h1>
    <p class="subtitle">
        Enter any US address or ZIP code. We'll pull real solar data, your utility's rates,
        and run a full financial analysis in seconds.
    </p>

    <form class="search-form"
          hx-post="/api/quote"
          hx-target="#result"
          hx-indicator="#result">
        <div class="search-row">
            <input type="text" name="location" class="search-input"
                   placeholder="Address, city, or ZIP code..."
                   required autofocus>
            <button type="submit" class="search-btn">Analyze</button>
        </div>

        <div class="advanced-toggle" onclick="this.nextElementSibling.classList.toggle('open')">
            &#9662; Advanced options
        </div>
        <div class="advanced-fields">
            <div class="field-row">
                <div class="field-group">
                    <label>Monthly usage (kWh)</label>
                    <input type="number" name="monthly_kwh" placeholder="e.g., 650"
                           step="10" min="0">
                </div>
                <div class="field-group">
                    <label>System size (kW)</label>
                    <input type="number" name="system_kw" placeholder="auto-sized"
                           step="0.5" min="1">
                </div>
            </div>
        </div>
    </form>
</div>

<div id="result">
    <div id="spinner">
        <div>
            <span class="spinner-dot"></span>
            <span class="spinner-dot"></span>
            <span class="spinner-dot"></span>
        </div>
        <div class="spinner-text" id="spinner-msg">Finding your address...</div>
        <script>
            (function() {{
                const msgs = [
                    "Finding your address...",
                    "Pulling solar data...",
                    "Checking your utility's rates...",
                    "Running the numbers..."
                ];
                let i = 0;
                const el = document.getElementById('spinner-msg');
                setInterval(() => {{
                    i = (i + 1) % msgs.length;
                    if (el) el.textContent = msgs[i];
                }}, 800);
            }})();
        </script>
    </div>
    <div id="result-content"></div>
</div>

<div class="footer">
    Data: NREL PVWatts v8.5 &middot; OpenEI URDB &middot; NASA POWER &middot; EIA
</div>

</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def landing():
    """Serve the landing page."""
    return LANDING_HTML


@app.post("/api/quote", response_class=HTMLResponse)
async def api_quote(
    location: str = Form(...),
    monthly_kwh: float = Form(None),
    system_kw: float = Form(None),
):
    """
    Handle a quote request. Returns an HTML fragment that HTMX swaps into #result.

    OLTP path: first check the DuckDB warehouse (OLAP storage) for a recent
    quote at this location. If one exists and is <=7 days old, return it
    without any live API calls. Otherwise run the full pipeline via
    etl_quote(), which persists the new quote to the warehouse so future
    requests (including teammates' EDA) can see it.
    """
    try:
        # --- OLAP cache check ---
        cached = latest_quote(location, max_age_days=7)
        if cached:
            logger.info(
                "OLAP cache hit for '%s' (fetched %s)",
                location, cached["fetched_at"],
            )
            quote = quote_from_dict(json.loads(cached["full_result_json"]))
        else:
            # --- Cache miss: run live pipeline, persist to warehouse ---
            logger.info("OLAP cache miss for '%s'; running live pipeline", location)
            quote = etl_quote(
                location=location,
                monthly_kwh=monthly_kwh if monthly_kwh else None,
                system_kw=system_kw if system_kw else None,
            )

        # Build row for report
        row = quote.pvwatts_result.to_dict()
        row.update({
            "site_id": quote.site_result.site_id,
            "address_label": quote.geocode_result.resolved_address,
            "lat": quote.geocode_result.lat,
            "lon": quote.geocode_result.lon,
            "system_capacity_kw": quote.system_kw_used,
            "state": quote.geocode_result.state,
            "zip_code": quote.geocode_result.zip_code,
            "azimuth": 180.0,
            "tilt": abs(quote.geocode_result.lat),
            "losses": 14.0,
            "tts_recent_sample_size": 1000,
            "tts_median_price_per_watt": quote.site_result.assumptions_used.get(
                "default_price_per_watt", 3.50
            ),
        })

        # Generate inline HTML (not full page)
        html = generate_report_html(
            [quote.site_result],
            rows=[row],
            quote=quote,
            embed_mode=True,
        )

        return HTMLResponse(content=html)

    except GeocodeError as e:
        return HTMLResponse(content=_error_card(
            "Location not found",
            f"We couldn't geocode \"{location}\". Try a full street address or 5-digit ZIP code.",
            str(e),
        ))
    except PVWattsError as e:
        return HTMLResponse(content=_error_card(
            "Solar data unavailable",
            "NREL's PVWatts service returned an error. Please try again.",
            str(e),
        ))
    except URDBError as e:
        return HTMLResponse(content=_error_card(
            "Rate lookup failed",
            "We couldn't find utility rates for this location.",
            str(e),
        ))
    except Exception as e:
        logger.exception("Unexpected error in /api/quote")
        return HTMLResponse(content=_error_card(
            "Something went wrong",
            "An unexpected error occurred. Please try again or try a different location.",
            str(e),
        ))


def _error_card(title: str, message: str, detail: str = "") -> str:
    """Build an HTML error card for HTMX swap."""
    detail_html = f"<p style='font-size: 12px; color: {PALETTE['warm_gray']}; margin-top: 8px;'><code>{detail}</code></p>" if detail else ""
    return f"""
    <div class="error-card">
        <h3>{title}</h3>
        <p>{message}</p>
        {detail_html}
    </div>
    """


@app.get("/healthz")
async def healthz():
    """Health check with cache stats."""
    stats = {}
    try:
        import requests_cache
        from pathlib import Path
        cache_dir = Path.home() / ".solar_cache"
        for db_file in cache_dir.glob("*.sqlite"):
            name = db_file.stem
            try:
                session = requests_cache.CachedSession(str(db_file.with_suffix("")))
                # Count cached responses
                cached = len(list(session.cache.responses))
                stats[name] = {"cached_responses": cached}
            except Exception:
                stats[name] = {"error": "could not read"}
    except Exception:
        pass

    return {"ok": True, "cache": stats}
