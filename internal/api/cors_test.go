package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// CORS had no test coverage at all before 2026-09-13, which is how
// `Access-Control-Allow-Origin: *` survived (2026-07-05 review S7). A
// security-relevant response header with no assertion behind it can be widened
// again by accident and nothing would fail.

// corsProbePath is deliberately a request that fails validation.
//
// The CORS middleware wraps every route, so any path exercises it — but it must
// be one that never reaches the Neo4j driver, because newFixtureServer passes
// nil for it. /routes with no parameters returns 400 MISSING_PARAM first, with
// the header already set by the middleware. (/health tolerates a nil driver too,
// but only by reporting the database as degraded — not a normal path to probe.)
const corsProbePath = "/routes"

func originFor(t *testing.T, srv *Server) string {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, corsProbePath, nil)
	w := httptest.NewRecorder()
	srv.Handler().ServeHTTP(w, req)
	return w.Result().Header.Get("Access-Control-Allow-Origin")
}

func TestCORSDefaultsToTheViteDevOrigin(t *testing.T) {
	// Both documented workflows are same-origin (the Go server serves web/dist;
	// the Vite dev server proxies the API paths), so a restrictive default costs
	// nothing. web/vite.config.js pins port 5173.
	t.Setenv("CORS_ALLOW_ORIGIN", "")

	if got := originFor(t, newFixtureServer()); got != defaultCORSOrigin {
		t.Errorf("got %q, want the default %q", got, defaultCORSOrigin)
	}
}

func TestCORSIsNotWildcardByDefault(t *testing.T) {
	// The regression that matters: a bare `*` lets any page on the internet read
	// this API.
	t.Setenv("CORS_ALLOW_ORIGIN", "")

	if got := originFor(t, newFixtureServer()); got == "*" {
		t.Error("Access-Control-Allow-Origin must not default to *")
	}
}

func TestCORSOriginIsConfigurable(t *testing.T) {
	t.Setenv("CORS_ALLOW_ORIGIN", "https://routes.example.com")

	if got := originFor(t, newFixtureServer()); got != "https://routes.example.com" {
		t.Errorf("got %q, want the configured origin", got)
	}
}

func TestCORSWildcardStillAvailableWhenAskedForExplicitly(t *testing.T) {
	// Opting back in is a deliberate act, not a default.
	t.Setenv("CORS_ALLOW_ORIGIN", "*")

	if got := originFor(t, newFixtureServer()); got != "*" {
		t.Errorf("got %q, want * when explicitly configured", got)
	}
}

func TestCORSSetsVaryOrigin(t *testing.T) {
	// Without Vary, a shared cache could serve a response carrying one allowed
	// origin to a client on another.
	t.Setenv("CORS_ALLOW_ORIGIN", "")
	req := httptest.NewRequest(http.MethodGet, corsProbePath, nil)
	w := httptest.NewRecorder()
	newFixtureServer().Handler().ServeHTTP(w, req)

	if got := w.Result().Header.Get("Vary"); got != "Origin" {
		t.Errorf("got Vary %q, want Origin", got)
	}
}

func TestPreflightStillShortCircuits(t *testing.T) {
	// The middleware change must not break OPTIONS handling.
	t.Setenv("CORS_ALLOW_ORIGIN", "")
	req := httptest.NewRequest(http.MethodOptions, "/routes", nil)
	w := httptest.NewRecorder()
	newFixtureServer().Handler().ServeHTTP(w, req)

	resp := w.Result()
	if resp.StatusCode != http.StatusNoContent {
		t.Errorf("got %d, want 204", resp.StatusCode)
	}
	if got := resp.Header.Get("Access-Control-Allow-Methods"); got != "GET, OPTIONS" {
		t.Errorf("got methods %q, want \"GET, OPTIONS\"", got)
	}
}
