package api

import (
	"context"
	"fmt"
	"log"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"aircraft-route-planner/internal/graph"
	"aircraft-route-planner/internal/models"
	"aircraft-route-planner/internal/neo4jval"

	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
)

// resolveAircraft looks up an aircraft identifier (IATA, ICAO, or family name)
// and returns the canonical IATA codes, metadata for the response, and the
// original query string (non-empty only for family/multi-code matches).
func (s *Server) resolveAircraft(param string) (codes []string, meta interface{}, query string, err error) {
	codes = s.graph.AliasToIATA[param]
	if len(codes) == 0 {
		return nil, nil, "", fmt.Errorf("aircraft code '%s' not found", param)
	}

	if len(codes) == 1 {
		ac := s.graph.AircraftByIATA[codes[0]]
		if ac != nil {
			meta = models.AircraftMeta{IATA: ac.IATA, ICAO: ac.ICAO, Name: ac.Name, Family: ac.Family}
		}
		return codes, meta, "", nil
	}

	// Multiple codes — family match
	var metas []models.AircraftMeta
	for _, c := range codes {
		ac := s.graph.AircraftByIATA[c]
		if ac != nil {
			metas = append(metas, models.AircraftMeta{IATA: ac.IATA, ICAO: ac.ICAO, Name: ac.Name, Family: ac.Family})
		}
	}
	return codes, metas, param, nil
}

func (s *Server) airportMeta(iata string) *models.AirportMeta {
	ap := s.graph.Airports[iata]
	if ap == nil {
		return nil
	}
	return &models.AirportMeta{
		IATA:    ap.IATA,
		ICAO:    ap.ICAO,
		Name:    ap.Name,
		City:    ap.City,
		Country: ap.Country,
		Lat:     ap.Lat,
		Lon:     ap.Lon,
	}
}

// buildAirportsFromLegs collects every unique airport IATA appearing in any
// returned leg and returns a map of IATA → AirportMeta (with coords) suitable
// for the response meta. Missing airports are silently skipped.
func (s *Server) buildAirportsFromLegs(routes []models.RouteResult) map[string]models.AirportMeta {
	out := make(map[string]models.AirportMeta)
	add := func(iata string) {
		if iata == "" {
			return
		}
		if _, exists := out[iata]; exists {
			return
		}
		if m := s.airportMeta(iata); m != nil {
			out[iata] = *m
		}
	}
	for _, rt := range routes {
		for _, leg := range rt.Legs {
			add(leg.From)
			add(leg.To)
		}
	}
	return out
}

// buildAirportsFromCountryGroups collects every unique airport in the country
// route results, keyed by IATA.
func (s *Server) buildAirportsFromCountryGroups(groups []models.CountryRouteGroup) map[string]models.AirportMeta {
	out := make(map[string]models.AirportMeta)
	add := func(iata string) {
		if iata == "" {
			return
		}
		if _, exists := out[iata]; exists {
			return
		}
		if m := s.airportMeta(iata); m != nil {
			out[iata] = *m
		}
	}
	for _, g := range groups {
		add(g.From.IATA)
		add(g.To.IATA)
	}
	return out
}

// parseMaxHops reads the max_hops parameter. The bool reports whether the value
// was usable; false means the caller must answer 400.
//
// A well-formed but too-large value is still clamped rather than rejected (999 →
// the server ceiling), because §8.1 describes max_hops as a bound and clamping a
// valid integer is not a client error. A value that is not a non-negative integer
// *is* rejected: §8.3 specifies 400 for bad input, and every other malformed
// parameter on this endpoint already returns one (MISSING_PARAM, INVALID_MODE,
// INVALID_SORT). Silently defaulting `abc` to 2 hid client bugs and was the
// inconsistency 2026-07-05 review L2 flagged.
func (s *Server) parseMaxHops(r *http.Request, defaultVal int) (int, bool) {
	raw := r.URL.Query().Get("max_hops")
	if raw == "" {
		return defaultVal, true
	}
	v, err := strconv.Atoi(raw)
	if err != nil || v < 0 {
		return 0, false
	}
	if v > s.maxHops {
		return s.maxHops, true
	}
	return v, true
}

func (s *Server) airlineName(iata string) string {
	if s.graph.Airlines == nil || iata == "" {
		return ""
	}
	if a, ok := s.graph.Airlines[iata]; ok {
		return a.Name
	}
	return ""
}

func (s *Server) buildRouteResults(routes []graph.Route, acSet map[string]bool) []models.RouteResult {
	results := make([]models.RouteResult, 0, len(routes))
	for _, rt := range routes {
		legs := make([]models.Leg, 0, len(rt.Edges))
		for i, e := range rt.Edges {
			legs = append(legs, models.Leg{
				From:        rt.Path[i],
				To:          e.Dst,
				DistanceKm:  math.Round(e.Distance*10) / 10,
				Airline:     e.Airline,
				AirlineName: s.airlineName(e.Airline),
				Aircraft:    e.Aircraft,
				IsTarget:    acSet[e.Aircraft],
				// FlightNumber: empty — OpenFlights routes.dat has no flight-number field.
			})
		}
		results = append(results, models.RouteResult{
			Path:            rt.Path,
			Hops:            len(rt.Edges) - 1,
			Legs:            legs,
			TotalDistanceKm: math.Round(rt.TotalDistance*10) / 10,
		})
	}
	return results
}

func makeACSet(codes []string) map[string]bool {
	s := make(map[string]bool, len(codes))
	for _, c := range codes {
		s[c] = true
	}
	return s
}

// handleRoutes handles GET /routes
func (s *Server) handleRoutes(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	aircraft := normalizeCode(q.Get("aircraft"))
	from := normalizeCode(q.Get("from"))
	to := normalizeCode(q.Get("to"))
	mode := q.Get("mode")
	sortBy := q.Get("sort")

	if aircraft == "" || from == "" || to == "" {
		writeError(w, http.StatusBadRequest, "MISSING_PARAM", "aircraft, from, and to are required")
		return
	}
	if mode == "" {
		mode = "strict"
	}
	if mode != "strict" && mode != "inclusive" {
		writeError(w, http.StatusBadRequest, "INVALID_MODE", fmt.Sprintf("invalid mode '%s', must be strict or inclusive", mode))
		return
	}
	if sortBy == "" {
		sortBy = "hops"
	}
	if sortBy != "hops" && sortBy != "distance" {
		writeError(w, http.StatusBadRequest, "INVALID_SORT", fmt.Sprintf("invalid sort '%s', must be hops or distance", sortBy))
		return
	}

	codes, acMeta, acQuery, err := s.resolveAircraft(aircraft)
	if err != nil {
		writeError(w, http.StatusNotFound, "INVALID_AIRCRAFT", err.Error())
		return
	}
	if s.graph.Airports[from] == nil {
		writeError(w, http.StatusNotFound, "INVALID_AIRPORT", fmt.Sprintf("airport '%s' not found", from))
		return
	}
	if s.graph.Airports[to] == nil {
		writeError(w, http.StatusNotFound, "INVALID_AIRPORT", fmt.Sprintf("airport '%s' not found", to))
		return
	}

	maxHops, ok := s.parseMaxHops(r, 2)
	if !ok {
		writeError(w, http.StatusBadRequest, "INVALID_PARAM",
			"max_hops must be a non-negative integer")
		return
	}
	acSet := makeACSet(codes)
	start := time.Now()

	ctx, cancel := context.WithTimeout(r.Context(), traversalBudget)
	defer cancel()

	var routes []graph.Route
	var truncated bool
	if sortBy == "distance" {
		rt, err := s.graph.FindShortestRoute(ctx, from, to, codes, maxHops, mode == "inclusive")
		if err != nil {
			writeTraversalError(w)
			return
		}
		if rt != nil {
			routes = []graph.Route{*rt}
		}
	} else if mode == "inclusive" {
		found, tr, err := s.graph.FindRoutesInclusive(ctx, from, to, codes, maxHops)
		if err != nil {
			writeTraversalError(w)
			return
		}
		routes, truncated = found, tr
	} else {
		found, tr, err := s.graph.FindRoutes(ctx, from, to, codes, maxHops)
		if err != nil {
			writeTraversalError(w)
			return
		}
		routes, truncated = found, tr
	}

	results := s.buildRouteResults(routes, acSet)
	took := time.Since(start).Milliseconds()

	meta := models.RouteMeta{
		Aircraft:      acMeta,
		AircraftQuery: acQuery,
		From:          s.airportMeta(from),
		To:            s.airportMeta(to),
		Mode:          mode,
		TookMs:        took,
		Count:         len(results),
		Airports:      s.buildAirportsFromLegs(results),
		Truncated:     truncated,
	}

	writeJSON(w, http.StatusOK, models.Response{Data: results, Meta: meta})
}

// handleRoutesFrom handles GET /routes/from
func (s *Server) handleRoutesFrom(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	aircraft := normalizeCode(q.Get("aircraft"))
	from := normalizeCode(q.Get("from"))
	mode := q.Get("mode")

	if aircraft == "" || from == "" {
		writeError(w, http.StatusBadRequest, "MISSING_PARAM", "aircraft and from are required")
		return
	}
	if mode == "" {
		mode = "strict"
	}
	if mode != "strict" && mode != "inclusive" {
		writeError(w, http.StatusBadRequest, "INVALID_MODE", fmt.Sprintf("invalid mode '%s', must be strict or inclusive", mode))
		return
	}

	codes, acMeta, acQuery, err := s.resolveAircraft(aircraft)
	if err != nil {
		writeError(w, http.StatusNotFound, "INVALID_AIRCRAFT", err.Error())
		return
	}
	if s.graph.Airports[from] == nil {
		writeError(w, http.StatusNotFound, "INVALID_AIRPORT", fmt.Sprintf("airport '%s' not found", from))
		return
	}

	maxHops, ok := s.parseMaxHops(r, 2)
	if !ok {
		writeError(w, http.StatusBadRequest, "INVALID_PARAM",
			"max_hops must be a non-negative integer")
		return
	}
	acSet := makeACSet(codes)
	start := time.Now()

	ctx, cancel := context.WithTimeout(r.Context(), traversalBudget)
	defer cancel()

	var routes []graph.Route
	var truncated bool
	if mode == "inclusive" {
		found, tr, err := s.graph.FindRoutesFromInclusive(ctx, from, codes, maxHops)
		if err != nil {
			writeTraversalError(w)
			return
		}
		routes, truncated = found, tr
	} else {
		found, tr, err := s.graph.FindRoutesFrom(ctx, from, codes, maxHops)
		if err != nil {
			writeTraversalError(w)
			return
		}
		routes, truncated = found, tr
	}

	results := s.buildRouteResults(routes, acSet)
	took := time.Since(start).Milliseconds()

	meta := models.RouteMeta{
		Aircraft:      acMeta,
		AircraftQuery: acQuery,
		From:          s.airportMeta(from),
		Mode:          mode,
		TookMs:        took,
		Count:         len(results),
		Airports:      s.buildAirportsFromLegs(results),
		Truncated:     truncated,
	}

	writeJSON(w, http.StatusOK, models.Response{Data: results, Meta: meta})
}

// handleCountryRoutes handles GET /routes/countries
func (s *Server) handleCountryRoutes(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	fromCountry := q.Get("from")
	toCountry := q.Get("to")
	aircraftParam := q.Get("aircraft")

	if fromCountry == "" || toCountry == "" {
		writeError(w, http.StatusBadRequest, "MISSING_PARAM", "from and to country names are required")
		return
	}
	if _, ok := s.graph.ByCountry[fromCountry]; !ok {
		writeError(w, http.StatusNotFound, "COUNTRY_NOT_FOUND", fmt.Sprintf("country '%s' not found", fromCountry))
		return
	}
	if _, ok := s.graph.ByCountry[toCountry]; !ok {
		writeError(w, http.StatusNotFound, "COUNTRY_NOT_FOUND", fmt.Sprintf("country '%s' not found", toCountry))
		return
	}

	// acMeta is interface{} so it can carry either a single AircraftMeta or the
	// []AircraftMeta a family resolves to. The previous *AircraftMeta could hold
	// only the single form, so `aircraft=A350` reported "aircraft": null and looked
	// identical to an unfiltered query (2026-07-05 review M6). acQuery names the
	// family that produced an array; resolveAircraft already returned it and the
	// handler was discarding it.
	var acMeta interface{}
	var acQuery string
	var aircraftCodes []string

	if aircraftParam != "" {
		codes, meta, query, err := s.resolveAircraft(aircraftParam)
		if err != nil {
			writeError(w, http.StatusNotFound, "INVALID_AIRCRAFT", err.Error())
			return
		}
		aircraftCodes = codes
		acMeta, acQuery = meta, query
	}

	start := time.Now()

	ctx, cancel := context.WithTimeout(r.Context(), traversalBudget)
	defer cancel()

	// One call with the whole code set (2026-07-05 review L1). This replaced a loop
	// that called once per aircraft code and concatenated, which left the result
	// sorted only in chunks — and which needed a cap re-applied across the loop,
	// because a sum of capped calls is not itself capped. Passing the set removes
	// both problems: one scan, one sort, one cap.
	countryRoutes, truncated, err := s.graph.FindCountryRoutes(
		ctx, fromCountry, toCountry, aircraftCodes)
	if err != nil {
		writeTraversalError(w)
		return
	}

	// Group by airport pair
	type pairKey struct{ src, dst string }
	grouped := make(map[pairKey]*models.CountryRouteGroup)
	var order []pairKey

	for _, cr := range countryRoutes {
		key := pairKey{cr.SrcIATA, cr.DstIATA}
		grp, exists := grouped[key]
		if !exists {
			srcAp := s.graph.Airports[cr.SrcIATA]
			dstAp := s.graph.Airports[cr.DstIATA]
			grp = &models.CountryRouteGroup{
				From: models.AirportMeta{
					IATA:    cr.SrcIATA,
					ICAO:    srcAp.ICAO,
					Name:    srcAp.Name,
					City:    srcAp.City,
					Country: fromCountry,
					Lat:     srcAp.Lat,
					Lon:     srcAp.Lon,
				},
				To: models.AirportMeta{
					IATA:    cr.DstIATA,
					ICAO:    dstAp.ICAO,
					Name:    dstAp.Name,
					City:    dstAp.City,
					Country: toCountry,
					Lat:     dstAp.Lat,
					Lon:     dstAp.Lon,
				},
			}
			grouped[key] = grp
			order = append(order, key)
		}
		grp.Routes = append(grp.Routes, models.CountryRouteEntry{
			Aircraft:    cr.Aircraft,
			Airline:     cr.Airline,
			AirlineName: s.airlineName(cr.Airline),
			DistanceKm:  math.Round(cr.Distance*10) / 10,
		})
	}

	// §5.5 specifies "sorted by distance of first route in each pair", which was
	// not met even for a single aircraft code: `order` is built in airport-scan
	// order, and the sort inside FindCountryRoutes orders individual routes, not
	// pairs. Sorting the pairs here is what actually satisfies the contract
	// (2026-07-05 review L1, which framed this as a family-only defect).
	//
	// Each group's routes arrive distance-ascending because the flat result was
	// sorted before grouping, so Routes[0] is the pair's shortest.
	sort.SliceStable(order, func(i, j int) bool {
		a, b := grouped[order[i]], grouped[order[j]]
		if len(a.Routes) == 0 || len(b.Routes) == 0 {
			return len(a.Routes) > len(b.Routes)
		}
		return a.Routes[0].DistanceKm < b.Routes[0].DistanceKm
	})

	data := make([]models.CountryRouteGroup, 0, len(order))
	routeCount := 0
	for _, key := range order {
		grp := grouped[key]
		data = append(data, *grp)
		routeCount += len(grp.Routes)
	}

	took := time.Since(start).Milliseconds()

	meta := models.CountryRouteMeta{
		FromCountry:   fromCountry,
		ToCountry:     toCountry,
		Aircraft:      acMeta,
		AircraftQuery: acQuery,
		PairCount:     len(data),
		RouteCount:    routeCount,
		TookMs:        took,
		Airports:      s.buildAirportsFromCountryGroups(data),
		Truncated:     truncated,
	}

	writeJSON(w, http.StatusOK, models.Response{Data: data, Meta: meta})
}

// handleCountrySearch handles GET /countries/search
func (s *Server) handleCountrySearch(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query().Get("q")
	if q == "" {
		writeError(w, http.StatusBadRequest, "MISSING_PARAM", "q is required")
		return
	}

	start := time.Now()
	lower := strings.ToLower(q)
	var matches []string
	for _, c := range s.graph.Countries {
		if strings.Contains(strings.ToLower(c), lower) {
			matches = append(matches, c)
		}
	}

	took := time.Since(start).Milliseconds()
	writeJSON(w, http.StatusOK, models.Response{
		Data: matches,
		Meta: models.SearchMeta{TookMs: took, Count: len(matches)},
	})
}

// handleAirportSearch handles GET /airports/search
func (s *Server) handleAirportSearch(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query().Get("q")
	if q == "" {
		writeError(w, http.StatusBadRequest, "MISSING_PARAM", "q is required")
		return
	}

	start := time.Now()
	ctx := r.Context()
	session := s.driver.NewSession(ctx, neo4j.SessionConfig{AccessMode: neo4j.AccessModeRead})
	defer session.Close(ctx)

	result, err := session.Run(ctx,
		`CALL db.index.fulltext.queryNodes("airport_search", $query)
		 YIELD node, score
		 RETURN node.iata AS iata, node.icao AS icao, node.name AS name,
		        node.city AS city, node.country AS country,
		        node.lat AS lat, node.lon AS lon
		 ORDER BY score DESC LIMIT 20`,
		map[string]interface{}{"query": escapeLuceneQuery(q)})
	if err != nil {
		// Detail is logged, not returned: the raw driver error echoes index names
		// and query internals back to the caller (2026-07-05 review M1).
		log.Printf("airport search failed (q=%q): %v", q, err)
		writeError(w, http.StatusInternalServerError, "SEARCH_ERROR", "airport search failed")
		return
	}

	var results []models.AirportSearchResult
	for result.Next(ctx) {
		rec := result.Record()
		results = append(results, models.AirportSearchResult{
			IATA:    neo4jval.String(rec, "iata"),
			ICAO:    neo4jval.String(rec, "icao"),
			Name:    neo4jval.String(rec, "name"),
			City:    neo4jval.String(rec, "city"),
			Country: neo4jval.String(rec, "country"),
			Lat:     neo4jval.Float(rec, "lat"),
			Lon:     neo4jval.Float(rec, "lon"),
		})
	}
	if err := result.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "SEARCH_ERROR", fmt.Sprintf("airport search failed: %v", err))
		return
	}

	took := time.Since(start).Milliseconds()
	if results == nil {
		results = []models.AirportSearchResult{}
	}
	writeJSON(w, http.StatusOK, models.Response{
		Data: results,
		Meta: models.SearchMeta{TookMs: took, Count: len(results)},
	})
}

// handleAircraftSearch handles GET /aircraft/search
func (s *Server) handleAircraftSearch(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query().Get("q")
	if q == "" {
		writeError(w, http.StatusBadRequest, "MISSING_PARAM", "q is required")
		return
	}

	start := time.Now()
	ctx := r.Context()
	session := s.driver.NewSession(ctx, neo4j.SessionConfig{AccessMode: neo4j.AccessModeRead})
	defer session.Close(ctx)

	result, err := session.Run(ctx,
		`CALL db.index.fulltext.queryNodes("aircraft_search", $query)
		 YIELD node, score
		 RETURN node.iata AS iata, node.icao AS icao, node.name AS name, node.family AS family
		 ORDER BY score DESC LIMIT 20`,
		map[string]interface{}{"query": escapeLuceneQuery(q)})
	if err != nil {
		// Detail is logged, not returned — see the airport search above.
		log.Printf("aircraft search failed (q=%q): %v", q, err)
		writeError(w, http.StatusInternalServerError, "SEARCH_ERROR", "aircraft search failed")
		return
	}

	var results []models.AircraftSearchResult
	for result.Next(ctx) {
		rec := result.Record()
		results = append(results, models.AircraftSearchResult{
			IATA:   neo4jval.String(rec, "iata"),
			ICAO:   neo4jval.String(rec, "icao"),
			Name:   neo4jval.String(rec, "name"),
			Family: neo4jval.String(rec, "family"),
		})
	}
	if err := result.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "SEARCH_ERROR", fmt.Sprintf("aircraft search failed: %v", err))
		return
	}

	took := time.Since(start).Milliseconds()
	if results == nil {
		results = []models.AircraftSearchResult{}
	}
	writeJSON(w, http.StatusOK, models.Response{
		Data: results,
		Meta: models.SearchMeta{TookMs: took, Count: len(results)},
	})
}

// handleHealth handles GET /health
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	// Counted once at load, not rescanned per request (2026-07-05 review L3).
	edgeCount := s.graph.EdgeCount

	neo4jOK := true
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.driver.VerifyConnectivity(ctx); err != nil {
		neo4jOK = false
	}

	status := "ok"
	if !neo4jOK {
		status = "degraded"
	}

	// A zero LoadedAt means the Graph was built without going through
	// LoadGraph (test fixtures do this). Report it as unset rather than as
	// year 0001, which would surface as an age of billions of seconds and
	// read to a freshness check as the staleest possible graph.
	loadedAt, ageSeconds := "", int64(0)
	if !s.graph.LoadedAt.IsZero() {
		loadedAt = s.graph.LoadedAt.Format(time.RFC3339)
		ageSeconds = int64(time.Since(s.graph.LoadedAt).Seconds())
	}

	writeJSON(w, http.StatusOK, models.Response{
		Data: models.HealthData{
			Status:          status,
			AirportCount:    len(s.graph.Airports),
			EdgeCount:       edgeCount,
			AircraftCount:   len(s.graph.AircraftByIATA),
			CountryCount:    len(s.graph.Countries),
			Neo4jOK:         neo4jOK,
			GraphLoadedAt:   loadedAt,
			GraphAgeSeconds: ageSeconds,
		},
		Meta: models.SearchMeta{TookMs: 0, Count: 1},
	})
}
