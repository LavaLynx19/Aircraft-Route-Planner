package graph

import (
	"context"
	"fmt"
	"testing"
	"time"
)

// benchBudget bounds every benchmark below with a finite context, for two reasons.
//
// It matches production: a traversal reached through the API always carries the
// handler's 10s budget, never a background context. And it is a hard requirement
// for FindShortestRoute, which has no result cap and no visited set — it pushes
// every neighbour of every popped item, so on the mega-hub fixture an unbounded
// context would let one iteration run for minutes and allocate gigabytes.
// Cancellation is that traversal's only bound, so a benchmark must supply one.
const benchBudget = 30 * time.Second

func benchCtx(b *testing.B) context.Context {
	b.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), benchBudget)
	b.Cleanup(cancel)
	return ctx
}

// Benchmarks for the bounds added in Phase 6.7. The claim being tested is not
// "traversal is fast" but "traversal cost stops growing with depth", because the
// per-tier cap saturates and the walk breaks out. An unmeasured bound is not a
// bound, so these exist to produce numbers rather than to gate CI.
//
// Run with:
//
//	go test ./internal/graph/ -run '^$' -bench . -benchmem

// megaHubGraph reproduces the shape H1 described: one very high-degree airport
// feeding airports that are themselves well connected. With hubFanout=400 and
// spokeFanout=100 an unbounded 3-hop inclusive walk faces on the order of
// 400 * 100 * 100 = 4M paths, before accounting for the parallel-edge
// multiplication that inclusive mode adds on top.
func megaHubGraph(hubFanout, spokeFanout int) *Graph {
	aircraft := []string{"388", "738", "359"}
	total := hubFanout + hubFanout*2 // spokes plus a pool for them to point at

	g := &Graph{
		Airports:  make(map[string]*Airport, total+1),
		Adj:       make(map[string][]Edge, total+1),
		ByCountry: map[string][]string{},
	}
	name := func(i int) string { return fmt.Sprintf("A%05d", i) }

	for i := 0; i < total; i++ {
		g.Airports[name(i)] = &Airport{IATA: name(i), Country: "Testland"}
	}
	g.Airports["HUB"] = &Airport{IATA: "HUB", Country: "Testland"}

	hubEdges := make([]Edge, 0, hubFanout)
	for i := 0; i < hubFanout; i++ {
		hubEdges = append(hubEdges, Edge{
			Dst:      name(i),
			Aircraft: aircraft[i%len(aircraft)],
			Distance: float64(500 + i),
		})
	}
	g.Adj["HUB"] = hubEdges

	for i := 0; i < total; i++ {
		edges := make([]Edge, 0, spokeFanout)
		for j := 1; j <= spokeFanout; j++ {
			dst := name((i + j) % total)
			edges = append(edges, Edge{
				Dst:      dst,
				Aircraft: aircraft[(i+j)%len(aircraft)],
				Distance: float64(300 + j),
			})
		}
		g.Adj[name(i)] = edges
	}
	return g
}

func BenchmarkFindRoutesInclusive(b *testing.B) {
	g := megaHubGraph(400, 100)
	for _, maxHops := range []int{1, 2, 3} {
		b.Run(fmt.Sprintf("maxHops=%d", maxHops), func(b *testing.B) {
			ctx := benchCtx(b)
			b.ReportAllocs()
			var routes []Route
			var truncated bool
			for i := 0; i < b.N; i++ {
				routes, truncated, _ = g.FindRoutesInclusive(
					ctx, "HUB", "A00042", []string{"388"}, maxHops)
			}
			b.ReportMetric(float64(len(routes)), "routes")
			if truncated {
				b.ReportMetric(1, "truncated")
			}
		})
	}
}

func BenchmarkFindRoutesFromInclusive(b *testing.B) {
	// The worst case of the six: no destination to stop at, every edge traversed,
	// and every intermediate node accepted as its own result.
	g := megaHubGraph(400, 100)
	for _, maxHops := range []int{1, 2, 3} {
		b.Run(fmt.Sprintf("maxHops=%d", maxHops), func(b *testing.B) {
			ctx := benchCtx(b)
			b.ReportAllocs()
			var routes []Route
			for i := 0; i < b.N; i++ {
				routes, _, _ = g.FindRoutesFromInclusive(
					ctx, "HUB", []string{"388"}, maxHops)
			}
			b.ReportMetric(float64(len(routes)), "routes")
		})
	}
}

func BenchmarkFindRoutesStrict(b *testing.B) {
	g := megaHubGraph(400, 100)
	ctx := benchCtx(b)
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		g.FindRoutes(ctx, "HUB", "A00042", []string{"388"}, 3) //nolint:errcheck
	}
}

func BenchmarkFindShortestRoute(b *testing.B) {
	// The one traversal with no result cap: correctness of its cost depends
	// entirely on the context deadline, which is why maxHops is held at 2 here.
	// At 3 on this fixture the priority queue grows without any cap to stop it,
	// and the benchmark would measure the 30s budget rather than the algorithm.
	// That is a real property of the code, not a quirk of the benchmark — see the
	// note on benchBudget.
	g := megaHubGraph(400, 100)
	ctx := benchCtx(b)
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		g.FindShortestRoute(ctx, "HUB", "A00042", []string{"388"}, 2, true) //nolint:errcheck
	}
}
