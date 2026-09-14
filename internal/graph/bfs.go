package graph

import (
	"context"
	"sort"
)

// MaxRoutesPerHopTier bounds accepted routes per hop count rather than overall.
//
// A flat total cap would return an arbitrary N of the millions of paths an
// inclusive mega-hub query enumerates, in traversal order — so the best direct
// routes could be crowded out by arbitrary two-stop ones. Capping each hop tier
// separately preserves the shape of the answer: direct routes survive even when
// the two-stop tier overflows. With the hard 3-hop limit this also bounds a
// response to 4 * MaxRoutesPerHopTier routes.
//
// The same counters gate *expansion*, which is what lets the cap bound work and
// not merely response size. A state that can only contribute to tiers which are
// already full is never enqueued, and its path/edge copies are never made — those
// copies are the dominant allocation. An earlier version relied instead on
// breaking out once *every* tier filled, which benchmarks on 2026-09-13 showed to
// be nearly unreachable: low tiers are naturally sparse (a pair may have exactly
// one direct route), so a tier that can never reach the cap kept the walk running
// to exhaustion.
//
// The benefit is uneven and worth knowing before optimising further. For the
// destination-less variants every child is a candidate result, tiers fill fast and
// gating engages early (~74x faster, ~100x fewer allocations at maxHops=3). For
// destination-seeking queries a tier fills only on a destination hit, which is
// rare, so gating engages late: ~7x faster with allocation volume essentially
// unchanged. What remains there is the per-state copying in extend, not the cap.
// See ARCHITECTURE.md §8.2 for the measured figures.
const MaxRoutesPerHopTier = 250

// ctxCheckInterval is how many dequeues pass between cancellation checks. The
// check is cheap but not free and this is the hot loop, so it is sampled rather
// than run on every state.
const ctxCheckInterval = 256

type bfsState struct {
	node  string
	path  []string
	edges []Edge
}

// routeCollector accumulates results under the per-hop-tier cap and answers the
// questions the traversals use to prune.
type routeCollector struct {
	routes    []Route
	perTier   []int
	saturated int
	truncated bool
}

// newRouteCollector sizes one counter per reachable hop tier: a route with n
// edges sits in tier n-1, and n ranges over 1..maxHops+1.
func newRouteCollector(maxHops int) *routeCollector {
	return &routeCollector{perTier: make([]int, maxHops+1)}
}

// tierFull reports whether a result landing in `tier` would be rejected. An
// out-of-range tier counts as full, since nothing may be recorded there.
func (c *routeCollector) tierFull(tier int) bool {
	if tier < 0 || tier >= len(c.perTier) {
		return true
	}
	return c.perTier[tier] >= MaxRoutesPerHopTier
}

// tiersFullFrom reports whether every tier from `tier` onwards is full — that is,
// whether a state which can only reach those tiers is still worth expanding.
//
// A state at hop depth h yields a destination match in tier h, and each further
// descent lands one tier deeper, so its reachable tiers are exactly h..maxHops.
// The loop runs over at most maxHops+1 <= 4 entries (hops are capped at 3), which
// is far cheaper than the slice copies it avoids.
func (c *routeCollector) tiersFullFrom(tier int) bool {
	if tier < 0 {
		tier = 0
	}
	for t := tier; t < len(c.perTier); t++ {
		if c.perTier[t] < MaxRoutesPerHopTier {
			return false
		}
	}
	return true
}

// add records a route unless its hop tier is already full. It reports whether
// every tier is now saturated, meaning the caller should stop walking entirely.
//
// Callers check tierFull before building the copies add needs, so the rejection
// path here is a safety net rather than the normal route to truncation.
func (c *routeCollector) add(path []string, edges []Edge) (allFull bool) {
	tier := len(edges) - 1
	if c.tierFull(tier) {
		c.truncated = true
		return c.saturated >= len(c.perTier)
	}
	c.perTier[tier]++
	if c.perTier[tier] == MaxRoutesPerHopTier {
		// Truncation is flagged the moment a tier fills, not only when a later
		// result is rejected. With expansion gating a rejection may never be
		// attempted at all, so flagging only on rejection would report a capped
		// result set as complete — exactly the silent wrongness this mechanism
		// exists to prevent. The cost is a conservative true when a tier holds
		// exactly the cap and no more existed, which is the safe way to be wrong.
		c.saturated++
		c.truncated = true
	}
	c.routes = append(c.routes, makeRoute(path, edges))
	return c.saturated >= len(c.perTier)
}

// sortRoutes orders results by hop count, then by total distance. Shared by all
// four BFS variants so their ordering cannot drift apart.
func sortRoutes(routes []Route) {
	sort.Slice(routes, func(i, j int) bool {
		if len(routes[i].Edges) != len(routes[j].Edges) {
			return len(routes[i].Edges) < len(routes[j].Edges)
		}
		return routes[i].TotalDistance < routes[j].TotalDistance
	})
}

// FindRoutes performs strict BFS: every leg must use one of the given aircraft codes.
// aircraftCodes should already be resolved (canonical IATA codes).
//
// Returns the routes, whether any hop tier reached its cap, and ctx's error if the
// traversal was cancelled or timed out. On cancellation partial results are
// discarded: a half-enumerated answer ordered by traversal accident is more
// misleading than no answer.
func (g *Graph) FindRoutes(ctx context.Context, src, dst string, aircraftCodes []string, maxHops int) ([]Route, bool, error) {
	if src == dst || len(aircraftCodes) == 0 {
		return nil, false, nil
	}

	acSet := make(map[string]bool, len(aircraftCodes))
	for _, c := range aircraftCodes {
		acSet[c] = true
	}

	queue := make([]bfsState, 0, 256)
	queue = append(queue, bfsState{node: src, path: []string{src}})
	col := newRouteCollector(maxHops)

walk:
	for i := 0; i < len(queue); i++ {
		if i%ctxCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return nil, false, err
			}
		}
		st := queue[i]
		hops := len(st.path) - 1

		// Nothing this state can reach is still wanted.
		if col.tiersFullFrom(hops) {
			continue
		}

		for _, edge := range g.Adj[st.node] {
			if !acSet[edge.Aircraft] {
				continue
			}
			if inPath(st.path, edge.Dst) {
				continue
			}

			if edge.Dst == dst {
				// Tier full: this result would be rejected, and truncation was
				// already flagged when the tier filled. Skip before copying.
				if col.tierFull(hops) {
					continue
				}
				newPath, newEdges := extend(st.path, st.edges, edge)
				if col.add(newPath, newEdges) {
					break walk
				}
				continue
			}

			if hops >= maxHops || col.tiersFullFrom(hops+1) {
				continue
			}
			newPath, newEdges := extend(st.path, st.edges, edge)
			queue = append(queue, bfsState{
				node:  edge.Dst,
				path:  newPath,
				edges: newEdges,
			})
		}
	}

	sortRoutes(col.routes)
	return col.routes, col.truncated, nil
}

// extend copies a path and edge list with one more hop appended. These copies are
// the dominant allocation in every traversal, which is why callers prune before
// reaching here rather than after.
func extend(path []string, edges []Edge, edge Edge) ([]string, []Edge) {
	newPath := make([]string, len(path)+1)
	copy(newPath, path)
	newPath[len(path)] = edge.Dst
	newEdges := make([]Edge, len(edges)+1)
	copy(newEdges, edges)
	newEdges[len(edges)] = edge
	return newPath, newEdges
}

func inPath(path []string, node string) bool {
	for _, p := range path {
		if p == node {
			return true
		}
	}
	return false
}

// FindRoutesFrom performs strict BFS with no destination: every reachable airport
// within maxHops where all legs use one of the given aircraft codes is a result.
// Return values follow FindRoutes.
func (g *Graph) FindRoutesFrom(ctx context.Context, src string, aircraftCodes []string, maxHops int) ([]Route, bool, error) {
	if len(aircraftCodes) == 0 {
		return nil, false, nil
	}

	acSet := make(map[string]bool, len(aircraftCodes))
	for _, c := range aircraftCodes {
		acSet[c] = true
	}

	queue := make([]bfsState, 0, 256)
	queue = append(queue, bfsState{node: src, path: []string{src}})
	col := newRouteCollector(maxHops)

walk:
	for i := 0; i < len(queue); i++ {
		if i%ctxCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return nil, false, err
			}
		}
		st := queue[i]
		hops := len(st.path) - 1

		if col.tiersFullFrom(hops) {
			continue
		}

		for _, edge := range g.Adj[st.node] {
			if !acSet[edge.Aircraft] {
				continue
			}
			if inPath(st.path, edge.Dst) {
				continue
			}

			// Every child is both a candidate result (tier `hops`) and possibly
			// worth descending from. If neither applies, skip before copying.
			wantResult := !col.tierFull(hops)
			canDescend := hops < maxHops && !col.tiersFullFrom(hops+1)
			if !wantResult && !canDescend {
				continue
			}

			newPath, newEdges := extend(st.path, st.edges, edge)
			if wantResult {
				if col.add(newPath, newEdges) {
					break walk
				}
			}
			if canDescend {
				queue = append(queue, bfsState{
					node:  edge.Dst,
					path:  newPath,
					edges: newEdges,
				})
			}
		}
	}

	sortRoutes(col.routes)
	return col.routes, col.truncated, nil
}

func makeRoute(path []string, edges []Edge) Route {
	var total float64
	for _, e := range edges {
		total += e.Distance
	}
	return Route{Path: path, Edges: edges, TotalDistance: total}
}
