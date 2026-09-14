#!/usr/bin/env bash
# Run the Go API server against a chosen data source. Both sources stay
# isolated in their own Neo4j containers and can run simultaneously:
#
#   ./scripts/run-server.sh openflights   # 2014 baseline  → Neo4j :7687, API :8080
#   ./scripts/run-server.sh opensky       # live pipeline  → Neo4j :7688, API :8081
#
# Refresh opensky data first with: python3 ingest/sources/load_graph.py
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; source .env; set +a

case "${1:-}" in
  openflights)
    export PORT="${OPENFLIGHTS_API_PORT:-8080}"
    echo "OpenFlights (2014 baseline) — Neo4j $NEO4J_URI → API :$PORT"
    ;;
  opensky)
    export NEO4J_URI="$NEO4J_STAGING_URI"
    export NEO4J_USER="$NEO4J_STAGING_USER"
    export NEO4J_PASSWORD="$NEO4J_STAGING_PASSWORD"
    export PORT="${OPENSKY_API_PORT:-8081}"
    echo "OpenSky (live pipeline) — Neo4j $NEO4J_URI → API :$PORT"
    ;;
  *)
    echo "usage: $0 {openflights|opensky}" >&2
    exit 1
    ;;
esac

exec go run ./cmd/server
