package graph

// pathState is one BFS frontier entry. Every state of a traversal lives in a single
// slice and refers to its predecessor by index, so no path is copied during the
// walk: a path is rebuilt by following parents only for the states that become
// results, of which there are at most 4 * MaxRoutesPerHopTier.
//
// This replaced a state carrying its own copy of the path and edge list, made on
// every expansion (Phase 6.8). Those copies were the dominant allocation of every
// traversal — ~8M allocations and 2.65 GB for one 3-hop inclusive query from a
// mega-hub — and expansion gating could not remove them for destination-seeking
// queries, whose tiers fill late. See ARCHITECTURE.md §8.2.
type pathState struct {
	// edge is the adjacency entry that reached this state, or nil for the root.
	// It points into Graph.Adj, which is sound only because the graph is immutable
	// after load and shared read-only.
	edge      *Edge
	parent    int32  // index of the predecessor; -1 for the root
	hops      uint16 // edges between the root and this state
	hasTarget bool   // inclusive mode: some leg so far uses a requested aircraft
}

// pathArena holds every state of one traversal rooted at src. States are never
// removed — the BFS walks the slice by index — so the queue and the storage are
// the same slice.
type pathArena struct {
	src    string
	states []pathState
}

func newPathArena(src string, capacity int) *pathArena {
	states := make([]pathState, 1, capacity)
	states[0] = pathState{parent: -1}
	return &pathArena{src: src, states: states}
}

// node returns the airport state i stands at.
func (a *pathArena) node(i int32) string {
	if e := a.states[i].edge; e != nil {
		return e.Dst
	}
	return a.src
}

// push records a child of parent reached over edge.
func (a *pathArena) push(parent int32, edge *Edge, hasTarget bool) {
	a.states = append(a.states, pathState{
		edge:      edge,
		parent:    parent,
		hops:      a.states[parent].hops + 1,
		hasTarget: hasTarget,
	})
}

// visits reports whether the path ending at state i passes through airport. The
// parent chain is at most maxHops+1 states long, the same work as scanning a
// copied path.
func (a *pathArena) visits(i int32, airport string) bool {
	for ; i >= 0; i = a.states[i].parent {
		if a.node(i) == airport {
			return true
		}
	}
	return false
}

// route materialises the path ending at state i followed by one more edge — the
// shape of every BFS result — so allocation happens only for results kept.
// Distance is summed by makeRoute in forward edge order, exactly as before the
// rewrite, so no floating-point difference can reorder sorted results.
func (a *pathArena) route(i int32, last *Edge) Route {
	n := int(a.states[i].hops) + 1
	path := make([]string, n+1)
	edges := make([]Edge, n)
	path[n] = last.Dst
	edges[n-1] = *last
	for k := n - 1; k > 0; k-- {
		st := &a.states[i]
		path[k] = st.edge.Dst
		edges[k-1] = *st.edge
		i = st.parent
	}
	path[0] = a.src
	return makeRoute(path, edges)
}
