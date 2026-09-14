package api

import (
	"log"
	"net/http"
	"os"
	"time"
)

// defaultCORSOrigin is the Vite dev server (web/vite.config.js pins port 5173).
//
// Both documented workflows are actually same-origin and need no CORS at all: the
// Go server serves web/dist itself, and the Vite dev server proxies /routes,
// /airports, /aircraft, /countries and /health to the API. So a restrictive
// default costs nothing, whereas the previous `*` let any page on the internet
// read this API (2026-07-05 review S7). Override with CORS_ALLOW_ORIGIN; set it to
// `*` explicitly if you genuinely want the old behaviour.
const defaultCORSOrigin = "http://localhost:5173"

// corsOrigin returns the single origin to advertise in Access-Control-Allow-Origin.
func corsOrigin() string {
	if v := os.Getenv("CORS_ALLOW_ORIGIN"); v != "" {
		return v
	}
	return defaultCORSOrigin
}

// traversalBudget caps how long a single graph traversal may run.
//
// Stated relative to the server's 30s WriteTimeout (cmd/server/main.go), because
// the two are easily confused: WriteTimeout kills the *connection* but does not
// cancel the request context, so without an explicit budget a runaway inclusive
// query keeps burning CPU and RAM long after the client has gone (2026-07-05
// review H1). 10s leaves ample room under WriteTimeout to still write a response.
const traversalBudget = 10 * time.Second

// AbsoluteMaxHops is the hard ceiling on traversal depth, independent of any
// configuration.
//
// CLAUDE.md states "Max hops hard limit 3 — enforce at API handler level", but
// GRAPH_MAX_HOPS was accepted verbatim, so GRAPH_MAX_HOPS=8 made 8 a legitimate
// request depth (2026-07-05 review M4). The Phase 6.7 benchmarks put a number on
// what that would cost: traversal work grows roughly 100x per hop, and 3 hops on
// a mega-hub already reaches ~0.7s and gigabytes of allocation. An 8 is not a
// tuning choice, it is an outage — hence a constant rather than a default.
const AbsoluteMaxHops = 3

// clampMaxHops enforces AbsoluteMaxHops on a configured value, announcing any
// change it makes. Silently overriding an operator's setting would be its own
// kind of surprise, so the reduction is logged once at startup.
func clampMaxHops(configured int) int {
	if configured > AbsoluteMaxHops {
		log.Printf("GRAPH_MAX_HOPS=%d exceeds the hard limit of %d; using %d",
			configured, AbsoluteMaxHops, AbsoluteMaxHops)
		return AbsoluteMaxHops
	}
	if configured < 1 {
		log.Printf("GRAPH_MAX_HOPS=%d is not a usable depth; using 1", configured)
		return 1
	}
	return configured
}

// writeTraversalError reports a traversal that was cancelled or ran out of budget.
//
// 503 rather than 500: nothing is broken, the query was simply too expensive to
// finish, and the actionable advice is to narrow it. A client that disconnected
// first never reads this, which is harmless.
func writeTraversalError(w http.ResponseWriter) {
	writeError(w, http.StatusServiceUnavailable, "QUERY_TOO_EXPENSIVE",
		"query exceeded the traversal budget; narrow it with fewer hops, a specific aircraft, or strict mode")
}
