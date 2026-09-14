package graph

import (
	"context"
	"sort"
)

func sortCountryRoutes(routes []CountryRoute) {
	sort.Slice(routes, func(i, j int) bool {
		return routes[i].Distance < routes[j].Distance
	})
}

// FindCountryRoutes returns all direct flights between airports in two countries,
// optionally filtered to a set of aircraft codes. Results are sorted by distance.
//
// Takes a code *set* rather than one code (2026-07-05 review L1). The caller used
// to invoke this once per aircraft code and concatenate, which produced a result
// slice that was sorted only in chunks, and was unbounded no matter what cap a
// single call respected. A family like `A350` resolving to several IATA codes is
// the normal case, so one call has to handle it.
//
// A nil or empty `aircraftCodes` means no aircraft filter. There is no ""
// sentinel: `resolveAircraft` rejects an unknown code with a 404 long before this
// point, so an empty set can only mean "unfiltered".
//
// Every result is a single leg, so the per-hop-tier cap of the BFS traversals
// degenerates to one flat cap here — MaxRoutesPerHopTier, reused so the two cannot
// drift apart.
func (g *Graph) FindCountryRoutes(
	ctx context.Context, fromCountry, toCountry string, aircraftCodes []string,
) ([]CountryRoute, bool, error) {
	fromAirports := g.ByCountry[fromCountry]
	if len(fromAirports) == 0 {
		return nil, false, nil
	}

	var acSet map[string]bool
	if len(aircraftCodes) > 0 {
		acSet = make(map[string]bool, len(aircraftCodes))
		for _, c := range aircraftCodes {
			acSet[c] = true
		}
	}

	var results []CountryRoute

	for idx, srcIATA := range fromAirports {
		// Checked per source airport rather than per edge: the inner loop is
		// short, and a country's airport list is the axis that actually grows.
		if idx%ctxCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return nil, false, err
			}
		}
		for _, edge := range g.Adj[srcIATA] {
			dstAirport := g.Airports[edge.Dst]
			if dstAirport == nil || dstAirport.Country != toCountry {
				continue
			}
			if acSet != nil && !acSet[edge.Aircraft] {
				continue
			}
			results = append(results, CountryRoute{
				SrcIATA:  srcIATA,
				DstIATA:  edge.Dst,
				Aircraft: edge.Aircraft,
				Airline:  edge.Airline,
				Distance: edge.Distance,
			})
			if len(results) >= MaxRoutesPerHopTier {
				sortCountryRoutes(results)
				return results, true, nil
			}
		}
	}

	sortCountryRoutes(results)
	return results, false, nil
}
