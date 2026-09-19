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

# launchd hands a job the bare PATH /usr/bin:/bin:/usr/sbin:/sbin, which holds no
# Go toolchain, so a plain `exec go` works in a terminal and fails under the
# refresh chain's restart step — as it did on 2026-09-19, stopping a server it
# could not bring back. Resolve the binary here rather than in the plist: the
# plist is not in this repository, and cron would hit the same minimal PATH.
GO_BIN="${GO_BIN:-$(command -v go || true)}"
if [ -z "$GO_BIN" ]; then
  for candidate in /opt/homebrew/bin/go /usr/local/bin/go /usr/local/go/bin/go; do
    if [ -x "$candidate" ]; then
      GO_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$GO_BIN" ]; then
  echo "go toolchain not found — set GO_BIN, or add go to PATH" >&2
  exit 1
fi

exec "$GO_BIN" run ./cmd/server
