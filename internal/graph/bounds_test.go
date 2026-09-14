package graph

import (
	"context"
	"fmt"
	"testing"
)

// Tests for the two bounds added in PLAN.md Phase 6.7 (2026-07-05 review H1):
// the per-hop-tier result cap and context cancellation. traversal_test.go proves
// the cap does NOT fire on ordinary result sets; this file proves it fires when
// it should, and that cancellation is actually observed.

// hubGraph builds a star: HUB with `spokes` direct edges, all on aircraft "388".
// HUB sits in Hubland and every spoke in Spokeland, so the country endpoint has
// something to find too.
func hubGraph(spokes int) *Graph {
	g := &Graph{
		Airports:  map[string]*Airport{"HUB": {IATA: "HUB", Country: "Hubland"}},
		Adj:       map[string][]Edge{},
		ByCountry: map[string][]string{"Hubland": {"HUB"}},
	}
	edges := make([]Edge, 0, spokes)
	spokeList := make([]string, 0, spokes)
	for i := 0; i < spokes; i++ {
		dst := fmt.Sprintf("S%04d", i)
		g.Airports[dst] = &Airport{IATA: dst, Country: "Spokeland"}
		spokeList = append(spokeList, dst)
		edges = append(edges, Edge{Dst: dst, Aircraft: "388", Distance: float64(1000 + i)})
	}
	g.Adj["HUB"] = edges
	g.ByCountry["Spokeland"] = spokeList
	return g
}

func TestCapBoundsResultsPerTier(t *testing.T) {
	g := hubGraph(MaxRoutesPerHopTier + 50)

	routes, truncated, err := g.FindRoutesFrom(testCtx, "HUB", []string{"388"}, 0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(routes) != MaxRoutesPerHopTier {
		t.Errorf("got %d routes, want the cap of %d", len(routes), MaxRoutesPerHopTier)
	}
	if !truncated {
		t.Error("truncated must be true when 50 routes were dropped")
	}
}

func TestCapReportsTruncationAtExactlyTheLimit(t *testing.T) {
	// Documents a deliberate conservatism: `truncated` means "a tier reached its
	// cap", not "more existed". The traversal stops once every tier is full, so it
	// cannot distinguish exactly-at-cap from more-were-waiting. Reporting true is
	// the safe direction — the alternative once reported a capped result set as
	// complete.
	g := hubGraph(MaxRoutesPerHopTier)

	routes, truncated, err := g.FindRoutesFrom(testCtx, "HUB", []string{"388"}, 0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(routes) != MaxRoutesPerHopTier {
		t.Errorf("got %d routes, want %d", len(routes), MaxRoutesPerHopTier)
	}
	if !truncated {
		t.Error("a tier filled exactly to the cap must still report truncation")
	}
}

func TestNoTruncationJustBelowTheLimit(t *testing.T) {
	g := hubGraph(MaxRoutesPerHopTier - 1)

	routes, truncated, err := g.FindRoutesFrom(testCtx, "HUB", []string{"388"}, 0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(routes) != MaxRoutesPerHopTier-1 {
		t.Errorf("got %d routes, want %d", len(routes), MaxRoutesPerHopTier-1)
	}
	if truncated {
		t.Error("one below the cap must not report truncation")
	}
}

func TestLowerTierSurvivesWhenHigherTierOverflows(t *testing.T) {
	// This is the whole justification for a per-tier cap over a flat one: a flat
	// cap filled by 250 two-leg routes in traversal order could crowd out the
	// single one-leg route entirely. Per-tier, the direct route always survives.
	g := hubGraph(MaxRoutesPerHopTier + 50)
	// Re-shape into a chain: HUB→MID only, then MID→every spoke.
	spokeEdges := g.Adj["HUB"]
	g.Airports["MID"] = &Airport{IATA: "MID", Country: "Midland"}
	g.Adj["HUB"] = []Edge{{Dst: "MID", Aircraft: "388", Distance: 100}}
	g.Adj["MID"] = spokeEdges

	routes, truncated, err := g.FindRoutesFrom(testCtx, "HUB", []string{"388"}, 1)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !truncated {
		t.Error("the two-leg tier overflowed, so truncated must be true")
	}

	oneLeg, twoLeg := 0, 0
	for _, r := range routes {
		switch len(r.Edges) {
		case 1:
			oneLeg++
		case 2:
			twoLeg++
		default:
			t.Errorf("unexpected leg count %d in %v", len(r.Edges), r.Path)
		}
	}
	if oneLeg != 1 {
		t.Errorf("the single direct route must survive, got %d one-leg routes", oneLeg)
	}
	if twoLeg != MaxRoutesPerHopTier {
		t.Errorf("two-leg tier: got %d, want the cap of %d", twoLeg, MaxRoutesPerHopTier)
	}
}

func TestCountryRoutesCapped(t *testing.T) {
	g := hubGraph(MaxRoutesPerHopTier + 50)

	routes, truncated, err := g.FindCountryRoutes(testCtx, "Hubland", "Spokeland", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(routes) != MaxRoutesPerHopTier {
		t.Errorf("got %d routes, want the cap of %d", len(routes), MaxRoutesPerHopTier)
	}
	if !truncated {
		t.Error("truncated must be true when the flat cap was reached")
	}
}

func TestCancelledContextStopsEveryTraversal(t *testing.T) {
	// Every entry point samples ctx.Err() starting at its first iteration, so an
	// already-cancelled context is deterministic regardless of graph size.
	g := hubGraph(8)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	t.Run("FindRoutes", func(t *testing.T) {
		if _, _, err := g.FindRoutes(ctx, "HUB", "S0001", []string{"388"}, 2); err == nil {
			t.Error("want a cancellation error, got nil")
		}
	})
	t.Run("FindRoutesFrom", func(t *testing.T) {
		if _, _, err := g.FindRoutesFrom(ctx, "HUB", []string{"388"}, 2); err == nil {
			t.Error("want a cancellation error, got nil")
		}
	})
	t.Run("FindRoutesInclusive", func(t *testing.T) {
		if _, _, err := g.FindRoutesInclusive(ctx, "HUB", "S0001", []string{"388"}, 2); err == nil {
			t.Error("want a cancellation error, got nil")
		}
	})
	t.Run("FindRoutesFromInclusive", func(t *testing.T) {
		if _, _, err := g.FindRoutesFromInclusive(ctx, "HUB", []string{"388"}, 2); err == nil {
			t.Error("want a cancellation error, got nil")
		}
	})
	t.Run("FindShortestRoute", func(t *testing.T) {
		if _, err := g.FindShortestRoute(ctx, "HUB", "S0001", []string{"388"}, 2, false); err == nil {
			t.Error("want a cancellation error, got nil")
		}
	})
	t.Run("FindCountryRoutes", func(t *testing.T) {
		if _, _, err := g.FindCountryRoutes(ctx, "Hubland", "Spokeland", nil); err == nil {
			t.Error("want a cancellation error, got nil")
		}
	})
}

func TestCancellationErrorIsNotReturnedForTrivialGuards(t *testing.T) {
	// The cheap early-exit guards run before any ctx check, so src==dst returns
	// cleanly even on a cancelled context. Documented so it is not mistaken for a
	// missed cancellation.
	g := hubGraph(4)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	routes, truncated, err := g.FindRoutes(ctx, "HUB", "HUB", []string{"388"}, 2)
	if err != nil || routes != nil || truncated {
		t.Errorf("want (nil, false, nil) for src==dst, got (%v, %v, %v)", routes, truncated, err)
	}
}
