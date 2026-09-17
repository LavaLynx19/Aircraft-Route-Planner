package graph

import (
	"context"
	"fmt"
	"os"
	"slices"
	"sort"
	"testing"
	"time"

	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
)

// TestLoadGraphIntegration runs LoadGraph against a real Neo4j and checks the
// invariants the rest of the server relies on. loader.go reads straight from a
// Neo4j session, so a live database is the only way to test it (2026-07-05
// review S3). It is read-only, and skipped under -short or when NEO4J_PASSWORD is
// unset, so a plain `go test ./...` never needs a database.
//
// Point NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD at staging to run it. The route count
// is compared with a live query, so a refresh writing to the same database
// mid-test can make that one check disagree; rerun outside the 11:00 refresh.
func TestLoadGraphIntegration(t *testing.T) {
	if testing.Short() || os.Getenv("NEO4J_PASSWORD") == "" {
		t.Skip("integration test: set NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD to run")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	g, driver, err := LoadGraph(ctx)
	if err != nil {
		t.Fatalf("LoadGraph: %v", err)
	}
	defer driver.Close(ctx)

	if len(g.Airports) == 0 || g.EdgeCount == 0 || len(g.AircraftByIATA) == 0 {
		t.Fatalf("empty graph: %d airports, %d edges, %d aircraft",
			len(g.Airports), g.EdgeCount, len(g.AircraftByIATA))
	}
	if g.LoadedAt.IsZero() {
		t.Error("LoadedAt was not stamped")
	}

	// Report only the first few violations of each invariant; one broken index
	// would otherwise produce thousands of identical lines.
	violations := map[string]int{}
	fail := func(check, format string, args ...any) {
		violations[check]++
		if violations[check] <= 5 {
			t.Errorf("%s: %s", check, fmt.Sprintf(format, args...))
		}
	}

	// EdgeCount matches the adjacency lists, and every edge joins loaded airports.
	edges := 0
	for src, list := range g.Adj {
		if _, ok := g.Airports[src]; !ok {
			fail("edge source", "%s is not a loaded airport", src)
		}
		edges += len(list)
		for _, e := range list {
			if _, ok := g.Airports[e.Dst]; !ok {
				fail("edge destination", "%s -> %s: destination not loaded", src, e.Dst)
			}
		}
	}
	if edges != g.EdgeCount {
		t.Errorf("EdgeCount = %d, adjacency lists hold %d", g.EdgeCount, edges)
	}

	// Only non-stale routes are served (ARCHITECTURE.md §4.1.6): the loaded count
	// must equal the database's own count under the same filter.
	res, err := neo4j.ExecuteQuery(ctx, driver,
		`MATCH (src:Airport)-[r:ROUTE]->(dst:Airport)
		 WHERE r.stale IS NULL AND coalesce(src.iata, '') <> '' AND coalesce(dst.iata, '') <> ''
		 RETURN count(r) AS n`,
		nil, neo4j.EagerResultTransformer)
	if err != nil {
		t.Fatalf("count query: %v", err)
	}
	if n, _ := res.Records[0].Get("n"); n.(int64) != int64(g.EdgeCount) {
		t.Errorf("loaded %d edges, database has %v non-stale routes", g.EdgeCount, n)
	}

	// The country index agrees with the airports, and Countries is sorted and unique.
	for iata, a := range g.Airports {
		if a.Country != "" && !slices.Contains(g.ByCountry[a.Country], iata) {
			fail("ByCountry", "%s missing from ByCountry[%q]", iata, a.Country)
		}
	}
	if !sort.StringsAreSorted(g.Countries) {
		t.Error("Countries is not sorted")
	}
	if len(g.Countries) != len(g.ByCountry) {
		t.Errorf("Countries has %d entries, ByCountry has %d keys", len(g.Countries), len(g.ByCountry))
	}

	// Every aircraft resolves by its own IATA and ICAO codes, and families list
	// their members — the lookups aircraft resolution in the API depends on.
	for iata, ac := range g.AircraftByIATA {
		if !slices.Contains(g.AliasToIATA[iata], iata) {
			fail("AliasToIATA", "%s does not resolve to itself", iata)
		}
		if ac.ICAO != "" && !slices.Contains(g.AliasToIATA[ac.ICAO], iata) {
			fail("AliasToIATA", "ICAO %s does not resolve to %s", ac.ICAO, iata)
		}
		if ac.Family != "" && !slices.Contains(g.Families[ac.Family], iata) {
			fail("Families", "%s missing from family %q", iata, ac.Family)
		}
	}
}
