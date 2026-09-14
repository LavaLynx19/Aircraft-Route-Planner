package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"aircraft-route-planner/internal/graph"
)

// Handler-level tests for the two traversal bounds added in Phase 6.7:
// the 503 returned when a traversal runs out of budget, and `truncated`
// reaching the response meta. internal/graph proves the mechanisms; these prove
// the wiring, which is a separate thing — writeTraversalError is called from
// three distinct blocks in handlers.go and `truncated` has to survive into two
// different meta structs.

// getCancelled issues a request whose context is already cancelled.
//
// The shared get() helper builds its own request, so it cannot carry one. This
// route into the 503 path needs no production change: each handler derives its
// budget with context.WithTimeout(r.Context(), traversalBudget), and deriving
// from an already-cancelled parent yields an already-cancelled context, so the
// traversal returns ctx.Err() on its first check. Shrinking traversalBudget to
// force a real deadline would have meant making a const into a var purely for
// tests, and would have made the test slow.
func getCancelled(t *testing.T, srv *Server, url string) (int, []byte) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	req := httptest.NewRequest(http.MethodGet, url, nil).WithContext(ctx)
	w := httptest.NewRecorder()
	srv.Handler().ServeHTTP(w, req)

	resp := w.Result()
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	return resp.StatusCode, body
}

func TestClampMaxHops(t *testing.T) {
	// Jul review M4: GRAPH_MAX_HOPS was accepted verbatim, so an 8 became a
	// legitimate request depth. CLAUDE.md makes 3 an absolute limit, and the 6.7
	// benchmarks (~100x work per hop) are why it has to be absolute rather than
	// a default.
	tests := []struct {
		name       string
		configured int
		want       int
	}{
		{"above the hard limit is clamped", 8, AbsoluteMaxHops},
		{"far above is clamped", 99, AbsoluteMaxHops},
		{"at the limit is kept", 3, 3},
		{"below the limit is kept", 2, 2},
		{"one is kept", 1, 1},
		{"zero is not a usable depth", 0, 1},
		{"negative is not a usable depth", -4, 1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := clampMaxHops(tc.configured); got != tc.want {
				t.Errorf("clampMaxHops(%d) = %d, want %d", tc.configured, got, tc.want)
			}
		})
	}
}

func TestServerCeilingSurvivesAnOverlargeConfig(t *testing.T) {
	// End-to-end: a server configured past the hard limit must not honour a
	// request at that depth either, since parseMaxHops clamps against s.maxHops.
	srv := NewServer(fixtureGraph(), nil, 8)
	if srv.maxHops != AbsoluteMaxHops {
		t.Fatalf("server maxHops = %d, want %d", srv.maxHops, AbsoluteMaxHops)
	}

	req := httptest.NewRequest(http.MethodGet,
		"/routes?aircraft=388&from=LHR&to=SYD&max_hops=8", nil)
	if got, ok := srv.parseMaxHops(req, 2); !ok || got != AbsoluteMaxHops {
		t.Errorf("parseMaxHops with max_hops=8 = (%d, %v), want (%d, true)",
			got, ok, AbsoluteMaxHops)
	}
}

func TestTraversalCancellationReturns503(t *testing.T) {
	srv := newFixtureServer()

	// One case per writeTraversalError call site, including both branches of the
	// /routes handler's mode/sort switch — a single endpoint would leave the
	// others unproven.
	tests := []struct {
		name string
		url  string
	}{
		{"routes strict", "/routes?aircraft=388&from=LHR&to=SYD&max_hops=2"},
		{"routes inclusive", "/routes?aircraft=388&from=LHR&to=SYD&max_hops=2&mode=inclusive"},
		{"routes sort=distance (dijkstra)", "/routes?aircraft=388&from=LHR&to=SYD&sort=distance"},
		{"routes/from strict", "/routes/from?aircraft=388&from=LHR&max_hops=2"},
		{"routes/from inclusive", "/routes/from?aircraft=388&from=LHR&max_hops=2&mode=inclusive"},
		{"routes/countries", "/routes/countries?from=United%20Kingdom&to=United%20States"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			code, body := getCancelled(t, srv, tc.url)
			if code != http.StatusServiceUnavailable {
				t.Fatalf("got %d, want 503; body: %s", code, body)
			}

			var resp struct {
				Error struct {
					Code    string `json:"code"`
					Message string `json:"message"`
				} `json:"error"`
			}
			decode(t, body, &resp)
			if resp.Error.Code != "QUERY_TOO_EXPENSIVE" {
				t.Errorf("got error code %q, want QUERY_TOO_EXPENSIVE", resp.Error.Code)
			}
			if resp.Error.Message == "" {
				t.Error("a 503 must say how to narrow the query, not just fail")
			}
		})
	}
}

func TestNormalRequestIsUnaffectedByTheBudget(t *testing.T) {
	// Guards against the 503 path being reachable by accident: the same URLs must
	// still answer 200 on a live context.
	srv := newFixtureServer()
	for _, url := range []string{
		"/routes?aircraft=388&from=LHR&to=SYD&max_hops=2",
		"/routes/from?aircraft=388&from=LHR&max_hops=2",
		"/routes/countries?from=United%20Kingdom&to=United%20States",
	} {
		if code, body := get(t, srv, url); code != http.StatusOK {
			t.Errorf("%s: got %d, want 200; body: %s", url, code, body)
		}
	}
}

// hubFixtureGraph builds a star large enough to overflow one hop tier: HUB with
// `spokes` direct 388 edges. graph.hubGraph is package-private there, so this is
// the api-side equivalent. AircraftByIATA/AliasToIATA are required because
// resolveAircraft rejects unknown codes before any traversal runs, and ByCountry
// because /routes/countries reads it.
func hubFixtureGraph(spokes int) *graph.Graph {
	g := &graph.Graph{
		Airports: map[string]*graph.Airport{
			"HUB": {IATA: "HUB", Name: "Hub", City: "Hubtown", Country: "Hubland"},
		},
		Adj:       map[string][]graph.Edge{},
		ByCountry: map[string][]string{"Hubland": {"HUB"}},
		Countries: []string{"Hubland", "Spokeland"},
		AircraftByIATA: map[string]*graph.Aircraft{
			"388": {IATA: "388", ICAO: "A388", Name: "Airbus A380-800", Family: "A380"},
		},
		AliasToIATA: map[string][]string{"388": {"388"}, "A388": {"388"}},
		Families:    map[string][]string{"A380": {"388"}},
	}

	edges := make([]graph.Edge, 0, spokes)
	spokeNames := make([]string, 0, spokes)
	for i := 0; i < spokes; i++ {
		dst := fmt.Sprintf("S%02d", i)
		g.Airports[dst] = &graph.Airport{IATA: dst, Name: dst, City: dst, Country: "Spokeland"}
		spokeNames = append(spokeNames, dst)
		edges = append(edges, graph.Edge{
			Dst: dst, Aircraft: "388", Airline: "XX", Distance: float64(1000 + i),
		})
	}
	g.Adj["HUB"] = edges
	g.ByCountry["Spokeland"] = spokeNames
	return g
}

func TestTruncatedReachesRouteMeta(t *testing.T) {
	over := graph.MaxRoutesPerHopTier + 25
	srv := NewServer(hubFixtureGraph(over), nil, 2)

	code, body := get(t, srv, "/routes/from?aircraft=388&from=HUB&max_hops=0")
	if code != http.StatusOK {
		t.Fatalf("got %d, want 200; body: %s", code, body)
	}

	var resp struct {
		Data []json.RawMessage `json:"data"`
		Meta struct {
			Count     int  `json:"count"`
			Truncated bool `json:"truncated"`
		} `json:"meta"`
	}
	decode(t, body, &resp)

	if !resp.Meta.Truncated {
		t.Errorf("%d routes available with a cap of %d — meta.truncated must be true",
			over, graph.MaxRoutesPerHopTier)
	}
	if resp.Meta.Count != graph.MaxRoutesPerHopTier {
		t.Errorf("got count %d, want the cap of %d", resp.Meta.Count, graph.MaxRoutesPerHopTier)
	}
	if len(resp.Data) != graph.MaxRoutesPerHopTier {
		t.Errorf("got %d data entries, want %d", len(resp.Data), graph.MaxRoutesPerHopTier)
	}
}

func TestTruncatedReachesCountryMeta(t *testing.T) {
	srv := NewServer(hubFixtureGraph(graph.MaxRoutesPerHopTier+25), nil, 2)

	code, body := get(t, srv, "/routes/countries?from=Hubland&to=Spokeland")
	if code != http.StatusOK {
		t.Fatalf("got %d, want 200; body: %s", code, body)
	}

	var resp struct {
		Meta struct {
			RouteCount int  `json:"route_count"`
			Truncated  bool `json:"truncated"`
		} `json:"meta"`
	}
	decode(t, body, &resp)

	if !resp.Meta.Truncated {
		t.Error("the direct-route cap was reached, so meta.truncated must be true")
	}
	if resp.Meta.RouteCount != graph.MaxRoutesPerHopTier {
		t.Errorf("got route_count %d, want the cap of %d",
			resp.Meta.RouteCount, graph.MaxRoutesPerHopTier)
	}
}

func TestTruncatedIsAbsentWhenNothingWasCapped(t *testing.T) {
	// Asserted on the raw JSON, not a typed struct: the point is that `omitempty`
	// keeps an untruncated response byte-identical to before the cap existed. A
	// typed decode would read a missing key as false and hide a regression here.
	srv := newFixtureServer()

	for _, url := range []string{
		"/routes?aircraft=388&from=LHR&to=SYD&max_hops=2",
		"/routes/from?aircraft=388&from=LHR&max_hops=2",
		"/routes/countries?from=United%20Kingdom&to=United%20States",
	} {
		code, body := get(t, srv, url)
		if code != http.StatusOK {
			t.Fatalf("%s: got %d, want 200", url, code)
		}

		var envelope struct {
			Meta map[string]json.RawMessage `json:"meta"`
		}
		decode(t, body, &envelope)
		if _, present := envelope.Meta["truncated"]; present {
			t.Errorf("%s: meta.truncated must be omitted when false, got %s",
				url, envelope.Meta["truncated"])
		}
	}
}
