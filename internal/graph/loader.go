package graph

import (
	"context"
	"errors"
	"log"
	"os"
	"sort"
	"time"

	"aircraft-route-planner/internal/env"
	"aircraft-route-planner/internal/neo4jval"

	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
)

// LoadGraph connects to Neo4j, loads all airports, routes, and aircraft into
// an in-memory Graph, then closes the driver. The returned Graph is read-only.
func LoadGraph(ctx context.Context) (*Graph, neo4j.DriverWithContext, error) {
	uri := env.Or("NEO4J_URI", "bolt://localhost:7687")
	user := env.Or("NEO4J_USER", "neo4j")

	// No fallback password (2026-07-05 review S4). A default meant a missing
	// NEO4J_PASSWORD silently produced a *working* connection with a credential
	// published in this repository. URI and user keep their defaults: they are
	// addresses, not secrets.
	password := os.Getenv("NEO4J_PASSWORD")
	if password == "" {
		return nil, nil, errors.New(
			"NEO4J_PASSWORD is not set — source .env before starting the server " +
				"(scripts/run-server.sh does this)")
	}

	driver, err := neo4j.NewDriverWithContext(uri, neo4j.BasicAuth(user, password, ""))
	if err != nil {
		return nil, nil, err
	}
	if err := driver.VerifyConnectivity(ctx); err != nil {
		driver.Close(ctx)
		return nil, nil, err
	}

	start := time.Now()

	g := &Graph{
		Airports:       make(map[string]*Airport),
		Adj:            make(map[string][]Edge),
		ByCountry:      make(map[string][]string),
		AircraftByIATA: make(map[string]*Aircraft),
		AliasToIATA:    make(map[string][]string),
		Families:       make(map[string][]string),
		Airlines:       make(map[string]*Airline),
	}

	session := driver.NewSession(ctx, neo4j.SessionConfig{AccessMode: neo4j.AccessModeRead})
	defer session.Close(ctx)

	if err := loadAirports(ctx, session, g); err != nil {
		driver.Close(ctx)
		return nil, nil, err
	}
	if err := loadRoutes(ctx, session, g); err != nil {
		driver.Close(ctx)
		return nil, nil, err
	}
	if err := loadAircraft(ctx, session, g); err != nil {
		driver.Close(ctx)
		return nil, nil, err
	}

	// airlines.dat is read directly from disk — it isn't part of the Neo4j
	// graph (airlines are a lookup table for response labels, not a search
	// surface). Failures are logged but non-fatal.
	airlinesPath := env.Or("AIRLINES_DAT", "ingest/data/airlines.dat")
	if airlines, err := LoadAirlines(airlinesPath); err != nil {
		log.Printf("airlines lookup unavailable (%s): %v", airlinesPath, err)
	} else {
		g.Airlines = airlines
		log.Printf("Loaded %d airlines from %s", len(airlines), airlinesPath)
	}

	edgeCount := 0
	for _, edges := range g.Adj {
		edgeCount += len(edges)
	}
	g.EdgeCount = edgeCount

	// Stamped once, at the end of a successful load. The graph is immutable
	// afterwards and refresh is by process restart (§4.1.5 *Refresh chain*), so
	// this is what tells an operator whether a rebuild reached clients.
	g.LoadedAt = time.Now().UTC()

	log.Printf("Graph loaded in %v: %d airports, %d edges, %d aircraft, %d countries, %d airlines",
		time.Since(start).Round(time.Millisecond),
		len(g.Airports), edgeCount, len(g.AircraftByIATA), len(g.Countries), len(g.Airlines))

	return g, driver, nil
}

func loadAirports(ctx context.Context, session neo4j.SessionWithContext, g *Graph) error {
	result, err := session.Run(ctx,
		`MATCH (a:Airport)
		 RETURN a.iata AS iata, a.icao AS icao, a.name AS name,
		        a.city AS city, a.country AS country, a.lat AS lat, a.lon AS lon`,
		nil)
	if err != nil {
		return err
	}

	countrySet := make(map[string]bool)
	for result.Next(ctx) {
		rec := result.Record()
		iata := neo4jval.String(rec, "iata")
		if iata == "" {
			continue
		}
		airport := &Airport{
			IATA:    iata,
			ICAO:    neo4jval.String(rec, "icao"),
			Name:    neo4jval.String(rec, "name"),
			City:    neo4jval.String(rec, "city"),
			Country: neo4jval.String(rec, "country"),
			Lat:     neo4jval.Float(rec, "lat"),
			Lon:     neo4jval.Float(rec, "lon"),
		}
		g.Airports[iata] = airport
		if airport.Country != "" {
			g.ByCountry[airport.Country] = append(g.ByCountry[airport.Country], iata)
			countrySet[airport.Country] = true
		}
	}
	if err := result.Err(); err != nil {
		return err
	}

	g.Countries = make([]string, 0, len(countrySet))
	for c := range countrySet {
		g.Countries = append(g.Countries, c)
	}
	sort.Strings(g.Countries)
	return nil
}

func loadRoutes(ctx context.Context, session neo4j.SessionWithContext, g *Graph) error {
	result, err := session.Run(ctx,
		// r.stale IS NULL excludes edges the ingest validity filter has rejected
		// (ARCHITECTURE.md §4.1.6). The loader marks them but does not delete —
		// a separate sweeper does that on its own cadence — so without this
		// filter stale routes would keep being served until the sweeper ran,
		// making the sweeper's schedule a correctness dependency.
		`MATCH (src:Airport)-[r:ROUTE]->(dst:Airport)
		 WHERE r.stale IS NULL
		 RETURN src.iata AS src, dst.iata AS dst,
		        r.aircraft AS aircraft, r.airline AS airline, r.distance_km AS distance`,
		nil)
	if err != nil {
		return err
	}

	for result.Next(ctx) {
		rec := result.Record()
		src := neo4jval.String(rec, "src")
		dst := neo4jval.String(rec, "dst")
		if src == "" || dst == "" {
			continue
		}
		edge := Edge{
			Dst:      dst,
			Aircraft: neo4jval.String(rec, "aircraft"),
			Airline:  neo4jval.String(rec, "airline"),
			Distance: neo4jval.Float(rec, "distance"),
		}
		g.Adj[src] = append(g.Adj[src], edge)
	}
	return result.Err()
}

func loadAircraft(ctx context.Context, session neo4j.SessionWithContext, g *Graph) error {
	result, err := session.Run(ctx,
		`MATCH (ac:Aircraft)
		 RETURN ac.iata AS iata, ac.icao AS icao, ac.name AS name,
		        ac.family AS family, ac.aliases AS aliases`,
		nil)
	if err != nil {
		return err
	}

	for result.Next(ctx) {
		rec := result.Record()
		iata := neo4jval.String(rec, "iata")
		if iata == "" {
			continue
		}

		var aliases []string
		if raw, ok := rec.Get("aliases"); ok && raw != nil {
			if list, ok := raw.([]interface{}); ok {
				for _, v := range list {
					if s, ok := v.(string); ok {
						aliases = append(aliases, s)
					}
				}
			}
		}

		family := neo4jval.String(rec, "family")
		ac := &Aircraft{
			IATA:    iata,
			ICAO:    neo4jval.String(rec, "icao"),
			Name:    neo4jval.String(rec, "name"),
			Family:  family,
			Aliases: aliases,
		}
		g.AircraftByIATA[iata] = ac

		// Build AliasToIATA: map every known identifier to this aircraft's IATA code.
		seen := make(map[string]bool)
		addAlias := func(alias string) {
			if alias != "" && !seen[alias] {
				seen[alias] = true
				g.AliasToIATA[alias] = append(g.AliasToIATA[alias], iata)
			}
		}
		addAlias(iata)
		addAlias(ac.ICAO)
		for _, a := range aliases {
			addAlias(a)
		}
		if family != "" {
			addAlias(family)
			g.Families[family] = append(g.Families[family], iata)
		}
	}
	return result.Err()
}
