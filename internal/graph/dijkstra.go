package graph

import (
	"container/heap"
	"context"
)

type dijkstraItem struct {
	node      string
	path      []string
	edges     []Edge
	dist      float64
	hasTarget bool
	index     int
}

type priorityQueue []*dijkstraItem

func (pq priorityQueue) Len() int           { return len(pq) }
func (pq priorityQueue) Less(i, j int) bool { return pq[i].dist < pq[j].dist }
func (pq priorityQueue) Swap(i, j int)      { pq[i], pq[j] = pq[j], pq[i]; pq[i].index = i; pq[j].index = j }
func (pq *priorityQueue) Push(x interface{}) {
	item := x.(*dijkstraItem)
	item.index = len(*pq)
	*pq = append(*pq, item)
}
func (pq *priorityQueue) Pop() interface{} {
	old := *pq
	n := len(old)
	item := old[n-1]
	old[n-1] = nil
	item.index = -1
	*pq = old[:n-1]
	return item
}

// FindShortestRoute returns the single shortest route (by distance) between src and dst.
// In strict mode (inclusive=false), only edges matching aircraftCodes are traversed.
// In inclusive mode, all edges are traversed but the result must contain at least one matching leg.
// Returns nil if no route exists, or ctx's error if the search was cancelled or
// timed out.
//
// No result cap applies here: the answer is a single route, so there is nothing
// to truncate. The unbounded cost is the priority queue, which cancellation
// covers.
func (g *Graph) FindShortestRoute(ctx context.Context, src, dst string, aircraftCodes []string, maxHops int, inclusive bool) (*Route, error) {
	if src == dst || len(aircraftCodes) == 0 {
		return nil, nil
	}

	acSet := make(map[string]bool, len(aircraftCodes))
	for _, c := range aircraftCodes {
		acSet[c] = true
	}

	pq := &priorityQueue{{
		node: src,
		path: []string{src},
		dist: 0,
	}}
	heap.Init(pq)

	for pops := 0; pq.Len() > 0; pops++ {
		if pops%ctxCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
		}
		item := heap.Pop(pq).(*dijkstraItem)

		if item.node == dst {
			if inclusive && !item.hasTarget {
				continue
			}
			r := makeRoute(item.path, item.edges)
			return &r, nil
		}

		if len(item.path) > maxHops+1 {
			continue
		}

		for _, edge := range g.Adj[item.node] {
			if !inclusive && !acSet[edge.Aircraft] {
				continue
			}
			if inPath(item.path, edge.Dst) {
				continue
			}

			newPath := append(append([]string(nil), item.path...), edge.Dst)
			newEdges := append(append([]Edge(nil), item.edges...), edge)

			heap.Push(pq, &dijkstraItem{
				node:      edge.Dst,
				path:      newPath,
				edges:     newEdges,
				dist:      item.dist + edge.Distance,
				hasTarget: item.hasTarget || acSet[edge.Aircraft],
			})
		}
	}
	return nil, nil
}
