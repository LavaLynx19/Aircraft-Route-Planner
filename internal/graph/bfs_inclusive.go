package graph

import "context"

// FindRoutesInclusive performs inclusive BFS: at least one leg must use the given aircraft.
// All edges are traversed; paths are accepted only if hasTarget is true at destination.
//
// This is the traversal H1 was really about: because every edge is followed and a
// single matching leg qualifies an entire path, a 3-hop query from a mega-hub
// enumerates millions of states. Expansion gating on the per-tier counters bounds
// the work, and parent-indexed states (pathState) keep each of those states from
// copying its path — see the notes on MaxRoutesPerHopTier and pathState. Return
// values follow FindRoutes.
func (g *Graph) FindRoutesInclusive(ctx context.Context, src, dst string, aircraftCodes []string, maxHops int) ([]Route, bool, error) {
	if src == dst || len(aircraftCodes) == 0 {
		return nil, false, nil
	}

	acSet := make(map[string]bool, len(aircraftCodes))
	for _, c := range aircraftCodes {
		acSet[c] = true
	}

	arena := newPathArena(src, 1024)
	col := newRouteCollector(maxHops)

walk:
	for i := 0; i < len(arena.states); i++ {
		if i%ctxCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return nil, false, err
			}
		}
		cur := int32(i)
		hops := int(arena.states[i].hops)
		hasTarget := arena.states[i].hasTarget

		// Nothing this state can reach is still wanted.
		if col.tiersFullFrom(hops) {
			continue
		}

		adj := g.Adj[arena.node(cur)]
		for j := range adj {
			edge := &adj[j]
			if arena.visits(cur, edge.Dst) {
				continue
			}

			newHasTarget := hasTarget || acSet[edge.Aircraft]

			if edge.Dst == dst {
				// Skip before materialising: either the path does not qualify, or
				// its tier is full and truncation was already flagged when it filled.
				if !newHasTarget || col.tierFull(hops) {
					continue
				}
				if col.add(arena.route(cur, edge)) {
					break walk
				}
				continue
			}

			if hops >= maxHops || col.tiersFullFrom(hops+1) {
				continue
			}
			arena.push(cur, edge, newHasTarget)
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

	arena := newPathArena(src, 1024)
	col := newRouteCollector(maxHops)

walk:
	for i := 0; i < len(arena.states); i++ {
		if i%ctxCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return nil, false, err
			}
		}
		cur := int32(i)
		hops := int(arena.states[i].hops)
		hasTarget := arena.states[i].hasTarget

		if col.tiersFullFrom(hops) {
			continue
		}

		adj := g.Adj[arena.node(cur)]
		for j := range adj {
			edge := &adj[j]
			if arena.visits(cur, edge.Dst) {
				continue
			}

			newHasTarget := hasTarget || acSet[edge.Aircraft]

			// A qualifying child is a result in tier `hops`; any child may still
			// be worth descending from, since a later leg can supply the match.
			// Adding a result fills only tier `hops`, so it cannot change whether
			// the deeper tiers are still open.
			if newHasTarget && !col.tierFull(hops) {
				if col.add(arena.route(cur, edge)) {
					break walk
				}
			}
			if hops < maxHops && !col.tiersFullFrom(hops+1) {
				arena.push(cur, edge, newHasTarget)
			}
		}
	}

	sortRoutes(col.routes)
	return col.routes, col.truncated, nil
}
