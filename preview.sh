#!/usr/bin/env bash
#
# preview.sh — Launch a local preview of the Solar Viability site.
#
# Starts TWO servers:
#   1. Static site  (http://localhost:8080)  — the exact GitHub Pages content:
#        /                 project landing
#        /reports/         city report cards (20 cities)
#        /reports/<city>.html   individual reports
#        /heatmap/         ZIP heatmap landing + the two maps
#   2. Calculator   (http://localhost:8000)  — the live FastAPI quote tool
#        (enter any address/ZIP → live NREL + utility-rate report)
#
# Usage:
#   ./preview.sh            # start both, open the landing pages
#   ./preview.sh --no-open  # start both, don't auto-open the browser
#   Ctrl-C                  # stops both servers cleanly
#
set -euo pipefail
cd "$(dirname "$0")"

STATIC_DIR="_deploy/solar"
STATIC_PORT=8080
CALC_PORT=8000
OPEN_BROWSER=1
[[ "${1:-}" == "--no-open" ]] && OPEN_BROWSER=0

if [[ ! -d "$STATIC_DIR" ]]; then
  echo "ERROR: $STATIC_DIR not found. Run from the project root." >&2
  exit 1
fi

# Free the ports if something is already listening
for p in "$STATIC_PORT" "$CALC_PORT"; do
  lsof -ti "tcp:$p" 2>/dev/null | xargs kill -9 2>/dev/null || true
done

# Start static server (serves the deploy folder = what GitHub Pages will host)
( cd "$STATIC_DIR" && exec python3 -m http.server "$STATIC_PORT" --bind 127.0.0.1 ) \
  >/tmp/solar_static.log 2>&1 &
STATIC_PID=$!

# Start the FastAPI calculator (live quotes; needs .env API keys)
exec_uvicorn() { exec uvicorn app:app --host 127.0.0.1 --port "$CALC_PORT" --log-level warning; }
( exec_uvicorn ) >/tmp/solar_calc.log 2>&1 &
CALC_PID=$!

cleanup() {
  echo ""
  echo "Stopping preview servers…"
  kill "$STATIC_PID" "$CALC_PID" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

# Wait for both to answer
echo "Starting preview servers…"
for i in $(seq 1 15); do
  s1=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$STATIC_PORT/reports/" 2>/dev/null || echo 000)
  s2=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$CALC_PORT/" 2>/dev/null || echo 000)
  [[ "$s1" == "200" && "$s2" == "200" ]] && break
  sleep 1
done

cat <<EOF

  ┌─────────────────────────────────────────────────────────────┐
  │  Solar Viability — local preview                            │
  ├─────────────────────────────────────────────────────────────┤
  │  STATIC SITE (what GitHub Pages will serve):                │
  │    Landing   →  http://localhost:$STATIC_PORT/                       │
  │    Reports   →  http://localhost:$STATIC_PORT/reports/               │
  │    Heatmaps  →  http://localhost:$STATIC_PORT/heatmap/               │
  │                                                             │
  │  CALCULATOR (live FastAPI quote tool):                      │
  │    Tool      →  http://localhost:$CALC_PORT/                        │
  ├─────────────────────────────────────────────────────────────┤
  │  Static-server log:  /tmp/solar_static.log                  │
  │  Calculator log:     /tmp/solar_calc.log                    │
  │  Press Ctrl-C to stop both servers.                         │
  └─────────────────────────────────────────────────────────────┘

EOF

if [[ "$OPEN_BROWSER" == "1" ]]; then
  open "http://localhost:$STATIC_PORT/reports/" 2>/dev/null || true
  open "http://localhost:$CALC_PORT/" 2>/dev/null || true
fi

# Keep the script alive so the servers keep running until Ctrl-C
wait
