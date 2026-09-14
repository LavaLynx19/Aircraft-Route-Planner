package graph

import "context"

type bfsIncState struct {
	node      string
	path      []string
	edges     []Edge
	hasTarget bool
}

// FindRoutesInclusive performs inclusive BFS: at least one leg must use the given aircraft.
// All edges are traversed; paths are accepted only if hasTarget is true at destination.
//
// This is the traversal H1 was really about: because every edge is followed and a
// single matching leg qualifies an entire path, a 3-hop query from a mega-hub
// enumerates millions of states. Expansion gating on the per-tier counters is what
// keeps that bounded — see the note on MaxRoutesPerHopTier. Return values follow
// FindRoutes.
func (g *Graph) FindRoutesInclusive(ctx context.Context, src, dst string, aircraftCodes []string, maxHops int) ([]Route, bool, error) {
	if src == dst || len(aircraftCodes) == 0 {
		return nil, false, nil
	}

	acSet := make(map[string]bool, len(aircraftCodes))
	for _, c := range aircraftCodes {
		acSet[c] = true
	}

	queue := make([]bfsIncState, 0, 1024)
	queue = append(queue, bfsIncState{node: src, path: []string{src}})
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
			if inPath(st.path, edge.Dst) {
				continue
			}

			newHasTarget := st.hasTarget || acSet[edge.Aircraft]

			if edge.Dst == dst {
				// Skip before copying: either the path does not qualify, or its
				// tier is full and truncation was already flagged when it filled.
				if !newHasTarget || col.tierFull(hops) {
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
			queue = append(queue, bfsIncState{
				node:      edge.Dst,
				path:      newPath,
				edges:     newEdges,
				hasTarget: newHasTarget,
			})
		}
	}

	sortRoutes(col.routes)
	return col.routes, col.truncated, nil
}

// FindRoutesFromInclusive performs inclusive BFS with no destination: every reachable
// airport within maxHops where at least one leg uses the given aircraft is a result.
// Return values follow FindRoutes.
func (g *Graph) FindRoutesFromInclusive(ctx context.Context, src string, aircraftCodes []string, maxHops int) ([]Route, bool, error) {
	if len(aircraftCodes) == 0 {
		return nil, false, nil
	}

	acSet := make(map[string]bool, len(aircraftCodes))
	for _, c := range aircraftCodes {
		acSet[c] = true
	}

	queue := make([]bfsIncState, 0, 1024)
	queue = append(queue, bfsIncState{node: src, path: []string{src}})
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
			if inPath(st.path, edge.Dst) {
				continue
			}

			newHasTarget := st.hasTarget || acSet[edge.Aircraft]

			// A qualifying child is a result in tier `hops`; any child may still
			// be worth descending from, since a later leg can supply the match.
			wantResult := newHasTarget && !col.tierFull(hops)
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
				queue = append(queue, bfsIncState{
					node:      edge.Dst,
					path:      newPath,
					edges:     newEdges,
					hasTarget: newHasTarget,
				})
			}
		}
	}

	sortRoutes(col.routes)
	return col.routes, col.truncated, nil
}
