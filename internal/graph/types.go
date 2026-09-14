package graph

import "time"

// Airport represents an airport node from Neo4j.
type Airport struct {
	IATA    string
	ICAO    string
	Name    string
	City    string
	Country string
	Lat     float64
	Lon     float64
}

// Edge represents a single route relationship (one per aircraft-route pair).
type Edge struct {
	Dst      string  // destination airport IATA code
	Aircraft string  // IATA equipment code
	Airline  string  // operating airline IATA code
	Distance float64 // pre-computed Haversine distance in km
}

// Aircraft represents an aircraft type with alias/family metadata.
type Aircraft struct {
	IATA    string
	ICAO    string
	Name    string
	Family  string
	Aliases []string
}

// Graph holds the in-memory representation of the route network.
type Graph struct {
	Airports       map[string]*Airport  // IATA → Airport
	Adj            map[string][]Edge    // source IATA → outgoing edges
	ByCountry      map[string][]string  // country name → airport IATA codes
	Countries      []string             // sorted distinct country names
	AircraftByIATA map[string]*Aircraft // IATA → Aircraft
	AliasToIATA    map[string][]string  // any identifier → canonical IATA code(s)
	Families       map[string][]string  // family name → IATA codes
	Airlines       map[string]*Airline  // IATA → Airline (from airlines.dat)

	// LoadedAt is when this snapshot was read out of Neo4j. The graph is
	// immutable after load and refresh happens by process restart rather than
	// in-place reload (ARCHITECTURE.md §4.1.5 *Refresh chain*), so this doubles
	// as "how stale is what we are serving" — the only way to confirm that a
	// nightly rebuild actually reached clients.
	LoadedAt time.Time

	// EdgeCount is the total number of ROUTE edges across Adj, counted once at
	// load. /health used to rescan every adjacency list on every request
	// (2026-07-05 review L3); the graph is immutable, so once is enough.
	//
	// A Graph assembled by hand — test fixtures do this — leaves it zero.
	EdgeCount int
}

// Route is a pathfinding result.
type Route struct {
	Path          []string // ordered airport IATA codes
	Edges         []Edge   // one per hop
	TotalDistance float64
}

// CountryRoute is a direct flight between airports in two countries.
type CountryRoute struct {
	SrcIATA  string
	DstIATA  string
	Aircraft string
	Airline  string
	Distance float64
}
