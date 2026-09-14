package graph

import (
	"context"
	"strings"
	"testing"
)

// Baseline tests for the six traversal entry points, written BEFORE threading
// context.Context and a per-hop result cap through them (PLAN.md Phase 6.7,
// 2026-07-05 review H1). Their job is to pin down current behaviour so the
// refactor is verifiable: several of these semantics are non-obvious and easy to
// change by accident.
//
// What they deliberately document:
//   - maxHops counts *intermediate* stops, so a direct route is returned even at
//     maxHops=0 — the destination check runs before the expansion check
//   - parallel edges yield separate routes (LHR→JFK on 738/388/359 is three
//     results, not one), which is the multiplier behind the state explosion. In
//     inclusive mode this compounds: a single matching leg anywhere qualifies the
//     whole path, so each parallel leg seeds a separate accepted route
//   - FindRoutesFrom* append every intermediate node as its own result
//   - inclusive mode traverses every edge and filters at the destination on
//     hasTarget, which is where the ~14M-state blow-up comes from
//   - all four BFS variants sort by (hop count, then total distance)

// testGraph mirrors the fixture in internal/api/handlers_test.go, which is
// package-private there and so cannot be reused. Its useful properties: three
// parallel LHR→JFK edges on different aircraft, two distinct 2-leg paths to SYD,
// and a JFK→LAX leg reachable only on 738 (so strict and inclusive disagree).
func testGraph() *Graph {
	return &Graph{
		Airports: map[string]*Airport{
			"LHR": {IATA: "LHR", Country: "United Kingdom"},
			"DXB": {IATA: "DXB", Country: "United Arab Emirates"},
			"SYD": {IATA: "SYD", Country: "Australia"},
			"JFK": {IATA: "JFK", Country: "United States"},
			"LAX": {IATA: "LAX", Country: "United States"},
		},
		Adj: map[string][]Edge{
			"LHR": {
				{Dst: "DXB", Aircraft: "388", Airline: "EK", Distance: 5500},
				{Dst: "JFK", Aircraft: "738", Airline: "BA", Distance: 5550},
				{Dst: "JFK", Aircraft: "388", Airline: "BA", Distance: 5550},
				{Dst: "JFK", Aircraft: "359", Airline: "BA", Distance: 5550},
			},
			"DXB": {
				{Dst: "SYD", Aircraft: "388", Airline: "EK", Distance: 12050},
			},
			"JFK": {
				{Dst: "SYD", Aircraft: "388", Airline: "QF", Distance: 15990},
				{Dst: "LAX", Aircraft: "738", Airline: "AA", Distance: 3970},
			},
		},
		ByCountry: map[string][]string{
			"United Kingdom":       {"LHR"},
			"United Arab Emirates": {"DXB"},
			"Australia":            {"SYD"},
			"United States":        {"JFK", "LAX"},
		},
		Countries: []string{
			"Australia", "United Arab Emirates", "United Kingdom", "United States",
		},
	}
}

// pathsOf renders routes as "LHR-DXB-SYD" strings, in result order, so a test
// failure names the paths rather than dumping structs.
func pathsOf(routes []Route) []string {
	out := make([]string, 0, len(routes))
	for _, r := range routes {
		out = append(out, strings.Join(r.Path, "-"))
	}
	return out
}

// testCtx is the context every traversal call below uses. These tests are about
// traversal behaviour; cancellation has its own tests.
var testCtx = context.Background()

// okRoutes asserts a traversal neither errored nor truncated, then renders it.
// The truncation assertion matters: it proves the per-tier cap does not fire on
// ordinary result sets, so these baselines measure behaviour and not the cap.
func okRoutes(t *testing.T, routes []Route, truncated bool, err error) []string {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected traversal error: %v", err)
	}
	if truncated {
		t.Fatalf("unexpected truncation: fixture is far below MaxRoutesPerHopTier (%d)",
			MaxRoutesPerHopTier)
	}
	return pathsOf(routes)
}

func assertPaths(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("got %d routes %v, want %d %v", len(got), got, len(want), want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("route %d: got %q, want %q (full: %v)", i, got[i], want[i], got)
		}
	}
}

func TestFindRoutes(t *testing.T) {
	g := testGraph()
	tests := []struct {
		name     string
		src, dst string
		codes    []string
		maxHops  int
		want     []string
	}{
		{
			name: "two distinct two-leg paths, ordered by total distance",
			src:  "LHR", dst: "SYD", codes: []string{"388"}, maxHops: 2,
			want: []string{"LHR-DXB-SYD", "LHR-JFK-SYD"},
		},
		{
			name: "parallel edges are separate routes",
			src:  "LHR", dst: "JFK", codes: []string{"388", "738", "359"}, maxHops: 1,
			want: []string{"LHR-JFK", "LHR-JFK", "LHR-JFK"},
		},
		{
			name: "maxHops=0 still returns a direct route",
			src:  "LHR", dst: "JFK", codes: []string{"388"}, maxHops: 0,
			want: []string{"LHR-JFK"},
		},
		{
			name: "maxHops=0 excludes any two-leg path",
			src:  "LHR", dst: "SYD", codes: []string{"388"}, maxHops: 0,
			want: nil,
		},
		{
			name: "strict mode rejects a path whose last leg is another aircraft",
			src:  "LHR", dst: "LAX", codes: []string{"388"}, maxHops: 2,
			want: nil,
		},
		{
			// The exact contrast with inclusive mode below: 359 does match
			// LHR→JFK, but strict requires EVERY leg to match and JFK→LAX is
			// 738-only, so the path is still rejected.
			name: "strict mode needs every leg to match, not merely one",
			src:  "LHR", dst: "LAX", codes: []string{"359"}, maxHops: 2,
			want: nil,
		},
		{
			name: "same src and dst yields nothing",
			src:  "LHR", dst: "LHR", codes: []string{"388"}, maxHops: 2,
			want: nil,
		},
		{
			name: "no aircraft codes yields nothing",
			src:  "LHR", dst: "SYD", codes: nil, maxHops: 2,
			want: nil,
		},
		{
			name: "unknown source yields nothing",
			src:  "ZZZ", dst: "SYD", codes: []string{"388"}, maxHops: 2,
			want: nil,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			routes, truncated, err := g.FindRoutes(testCtx, tc.src, tc.dst, tc.codes, tc.maxHops)
			assertPaths(t, okRoutes(t, routes, truncated, err), tc.want)
		})
	}
}

func TestFindRoutesFrom(t *testing.T) {
	g := testGraph()
	tests := []struct {
		name    string
		src     string
		codes   []string
		maxHops int
		want    []string
	}{
		{
			name: "every intermediate node is itself a result, shortest legs first",
			src:  "LHR", codes: []string{"388"}, maxHops: 1,
			want: []string{"LHR-DXB", "LHR-JFK", "LHR-DXB-SYD", "LHR-JFK-SYD"},
		},
		{
			name: "maxHops=0 stops at one leg",
			src:  "LHR", codes: []string{"388"}, maxHops: 0,
			want: []string{"LHR-DXB", "LHR-JFK"},
		},
		{
			name: "a source with no matching edges yields nothing",
			src:  "SYD", codes: []string{"388"}, maxHops: 2,
			want: nil,
		},
		{
			name: "no aircraft codes yields nothing",
			src:  "LHR", codes: nil, maxHops: 2,
			want: nil,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			routes, truncated, err := g.FindRoutesFrom(testCtx, tc.src, tc.codes, tc.maxHops)
			assertPaths(t, okRoutes(t, routes, truncated, err), tc.want)
		})
	}
}

func TestFindRoutesInclusive(t *testing.T) {
	g := testGraph()
	tests := []struct {
		name     string
		src, dst string
		codes    []string
		maxHops  int
		want     []string
	}{
		{
			// The contrast with TestFindRoutes: strict finds nothing here because
			// JFK→LAX is 738-only, while inclusive accepts the path on the
			// strength of the 388 first leg.
			name: "accepts a path where only one leg matches",
			src:  "LHR", dst: "LAX", codes: []string{"388"}, maxHops: 2,
			want: []string{"LHR-JFK-LAX"},
		},
		{
			// "320" appears on no edge in this fixture, which is what makes this a
			// true no-match case. 359 would not: LHR→JFK exists on 359, so that
			// code does match a leg and the path is legitimately accepted.
			name: "rejects a path with no matching leg at all",
			src:  "LHR", dst: "LAX", codes: []string{"320"}, maxHops: 2,
			want: nil,
		},
		{
			// Four results, not two. Inclusive traverses every edge, so each of the
			// three parallel LHR→JFK legs seeds its own path to SYD and the shared
			// 388 second leg qualifies all three. Hop counts tie, so distance
			// orders them: 17550 first, then three at 21540. Compare the strict
			// case above, which yields two — this gap is the state growth H1 is about.
			name: "every parallel first leg produces its own inclusive path",
			src:  "LHR", dst: "SYD", codes: []string{"388"}, maxHops: 2,
			want: []string{
				"LHR-DXB-SYD", "LHR-JFK-SYD", "LHR-JFK-SYD", "LHR-JFK-SYD",
			},
		},
		{
			name: "same src and dst yields nothing",
			src:  "LHR", dst: "LHR", codes: []string{"388"}, maxHops: 2,
			want: nil,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			routes, truncated, err := g.FindRoutesInclusive(
				testCtx, tc.src, tc.dst, tc.codes, tc.maxHops)
			assertPaths(t, okRoutes(t, routes, truncated, err), tc.want)
		})
	}
}

func TestFindRoutesFromInclusive(t *testing.T) {
	g := testGraph()

	// Counted deliberately: the three parallel LHR→JFK edges each seed their own
	// queue state, so LHR-JFK-SYD is reached three times over. This duplication
	// IS the combinatorial growth H1 is about, so the baseline records it rather
	// than hiding it behind a set comparison.
	routes, truncated, err := g.FindRoutesFromInclusive(testCtx, "LHR", []string{"388"}, 1)
	got := okRoutes(t, routes, truncated, err)
	want := []string{
		"LHR-DXB", "LHR-JFK",
		"LHR-DXB-SYD", "LHR-JFK-LAX", "LHR-JFK-SYD", "LHR-JFK-SYD", "LHR-JFK-SYD",
	}
	if len(got) != len(want) {
		t.Fatalf("got %d routes %v, want %d", len(got), got, len(want))
	}
	counts := map[string]int{}
	for _, p := range got {
		counts[p]++
	}
	if counts["LHR-JFK-SYD"] != 3 {
		t.Errorf("expected LHR-JFK-SYD three times (one per parallel first leg), got %d: %v",
			counts["LHR-JFK-SYD"], got)
	}
	if counts["LHR-JFK-LAX"] != 1 {
		t.Errorf("expected LHR-JFK-LAX once, got %d: %v", counts["LHR-JFK-LAX"], got)
	}

	if r, _, err := g.FindRoutesFromInclusive(testCtx, "LHR", nil, 2); err != nil || r != nil {
		t.Errorf("no aircraft codes should yield nothing, got %v (err %v)", pathsOf(r), err)
	}
}

func TestFindShortestRoute(t *testing.T) {
	g := testGraph()
	tests := []struct {
		name      string
		src, dst  string
		codes     []string
		maxHops   int
		inclusive bool
		want      string // "" means nil
	}{
		{
			name: "picks the cheaper of two two-leg paths",
			src:  "LHR", dst: "SYD", codes: []string{"388"}, maxHops: 2,
			want: "LHR-DXB-SYD",
		},
		{
			name: "respects maxHops=0 by finding no two-leg route",
			src:  "LHR", dst: "SYD", codes: []string{"388"}, maxHops: 0,
			want: "",
		},
		{
			name: "strict mode will not use a non-matching leg",
			src:  "LHR", dst: "LAX", codes: []string{"388"}, maxHops: 2,
			want: "",
		},
		{
			name: "inclusive mode will, given one matching leg",
			src:  "LHR", dst: "LAX", codes: []string{"388"}, maxHops: 2,
			inclusive: true, want: "LHR-JFK-LAX",
		},
		{
			// "320" is on no edge. 359 would not work here for the same reason as
			// in TestFindRoutesInclusive: it matches LHR→JFK, so the route qualifies.
			name: "inclusive mode still requires at least one matching leg",
			src:  "LHR", dst: "LAX", codes: []string{"320"}, maxHops: 2,
			inclusive: true, want: "",
		},
		{
			name: "same src and dst yields nil",
			src:  "LHR", dst: "LHR", codes: []string{"388"}, maxHops: 2,
			want: "",
		},
		{
			name: "no aircraft codes yields nil",
			src:  "LHR", dst: "SYD", codes: nil, maxHops: 2,
			want: "",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := g.FindShortestRoute(
				testCtx, tc.src, tc.dst, tc.codes, tc.maxHops, tc.inclusive)
			if err != nil {
				t.Fatalf("unexpected traversal error: %v", err)
			}
			if tc.want == "" {
				if got != nil {
					t.Fatalf("want nil, got %q", strings.Join(got.Path, "-"))
				}
				return
			}
			if got == nil {
				t.Fatalf("want %q, got nil", tc.want)
			}
			if p := strings.Join(got.Path, "-"); p != tc.want {
				t.Errorf("got %q, want %q", p, tc.want)
			}
		})
	}
}

func TestFindCountryRoutes(t *testing.T) {
	g := testGraph()
	tests := []struct {
		name     string
		from, to string
		aircraft []string
		wantLen  int
	}{
		{
			// nil means unfiltered. There is no "" sentinel any more: the function
			// takes a code set (2026-07-05 review L1).
			name: "direct flights only, one per parallel edge",
			from: "United Kingdom", to: "United States", wantLen: 3,
		},
		{
			name: "single-code filter narrows to the matching edge",
			from: "United Kingdom", to: "United States",
			aircraft: []string{"388"}, wantLen: 1,
		},
		{
			// The case the old signature could not express, and the reason L1
			// existed: a family resolves to several codes, and the caller used to
			// loop per code and concatenate — leaving the result sorted only in
			// chunks. One call, one sort. LHR→JFK carries 738/388/359, so two of
			// the three parallel edges match here.
			name: "multi-code filter matches every listed code in one call",
			from: "United Kingdom", to: "United States",
			aircraft: []string{"359", "738"}, wantLen: 2,
		},
		{
			name: "an empty non-nil set is also unfiltered",
			from: "United Kingdom", to: "United States",
			aircraft: []string{}, wantLen: 3,
		},
		{
			name: "a code present on no edge yields nothing",
			from: "United Kingdom", to: "United States",
			aircraft: []string{"320"}, wantLen: 0,
		},
		{
			// LHR→DXB→SYD exists but this endpoint is direct-only by design.
			name: "does not chain through an intermediate country",
			from: "United Kingdom", to: "Australia", wantLen: 0,
		},
		{
			name: "a country with no outbound edges yields nothing",
			from: "Australia", to: "United States", wantLen: 0,
		},
		{
			name: "unknown country yields nothing",
			from: "Atlantis", to: "United States", wantLen: 0,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, truncated, err := g.FindCountryRoutes(
				testCtx, tc.from, tc.to, tc.aircraft)
			if err != nil {
				t.Fatalf("unexpected traversal error: %v", err)
			}
			if truncated {
				t.Fatalf("unexpected truncation on a %d-route fixture", len(got))
			}
			if len(got) != tc.wantLen {
				t.Fatalf("got %d routes %+v, want %d", len(got), got, tc.wantLen)
			}
			for i := 1; i < len(got); i++ {
				if got[i-1].Distance > got[i].Distance {
					t.Errorf("results not sorted by distance: %+v", got)
				}
			}
		})
	}
}
