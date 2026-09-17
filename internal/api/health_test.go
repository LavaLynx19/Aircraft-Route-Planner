package api

import (
	"net/http"
	"testing"
)

// A server built without a Neo4j driver — every fixture server is — must answer
// /health by reporting the database as unreachable, not by panicking.
func TestHealthWithoutDriverReportsDegraded(t *testing.T) {
	code, body := get(t, newFixtureServer(), "/health")
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", code, body)
	}

	var resp struct {
		Data struct {
			Status        string `json:"status"`
			Neo4jOK       *bool  `json:"neo4j_ok"`
			AirportCount  int    `json:"airport_count"`
			GraphLoadedAt string `json:"graph_loaded_at"`
		} `json:"data"`
	}
	decode(t, body, &resp)

	if resp.Data.Neo4jOK == nil || *resp.Data.Neo4jOK {
		t.Errorf("neo4j_ok = %v, want false", resp.Data.Neo4jOK)
	}
	if resp.Data.Status != "degraded" {
		t.Errorf("status = %q, want %q", resp.Data.Status, "degraded")
	}
	if resp.Data.AirportCount != 5 {
		t.Errorf("airport_count = %d, want the fixture's 5", resp.Data.AirportCount)
	}
	if resp.Data.GraphLoadedAt != "" {
		t.Errorf("graph_loaded_at = %q, want empty for a graph not built by LoadGraph", resp.Data.GraphLoadedAt)
	}
}
