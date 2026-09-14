package models

// Response is the standard success envelope for all endpoints.
type Response struct {
	Data interface{} `json:"data"`
	Meta interface{} `json:"meta"`
}

// ErrorResponse is the envelope for error responses.
type ErrorResponse struct {
	Error ErrorDetail `json:"error"`
}

// ErrorDetail contains the structured error code and message.
type ErrorDetail struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// RouteResult represents a single route in a pathfinding response.
type RouteResult struct {
	Path            []string `json:"path"`
	Hops            int      `json:"hops"`
	Legs            []Leg    `json:"legs"`
	TotalDistanceKm float64  `json:"total_distance_km"`
}

// Leg represents a single flight segment within a route.
type Leg struct {
	From         string  `json:"from"`
	To           string  `json:"to"`
	DistanceKm   float64 `json:"distance_km"`
	Airline      string  `json:"airline"`
	AirlineName  string  `json:"airline_name,omitempty"`
	Aircraft     string  `json:"aircraft"`
	IsTarget     bool    `json:"is_target"`
	FlightNumber string  `json:"flight_number,omitempty"`
}

// AirportMeta is airport metadata included in response meta fields.
type AirportMeta struct {
	IATA    string  `json:"iata"`
	ICAO    string  `json:"icao,omitempty"`
	Name    string  `json:"name"`
	City    string  `json:"city"`
	Country string  `json:"country,omitempty"`
	Lat     float64 `json:"lat"`
	Lon     float64 `json:"lon"`
}

// AircraftMeta is aircraft metadata included in response meta fields.
type AircraftMeta struct {
	IATA   string `json:"iata"`
	ICAO   string `json:"icao,omitempty"`
	Name   string `json:"name"`
	Family string `json:"family,omitempty"`
}

// RouteMeta is the meta field for route endpoint responses.
type RouteMeta struct {
	Aircraft      interface{}            `json:"aircraft"`
	AircraftQuery string                 `json:"aircraft_query,omitempty"`
	From          *AirportMeta           `json:"from,omitempty"`
	To            *AirportMeta           `json:"to,omitempty"`
	Mode          string                 `json:"mode"`
	TookMs        int64                  `json:"took_ms"`
	Count         int                    `json:"count"`
	Airports      map[string]AirportMeta `json:"airports"`

	// Truncated reports that at least one hop tier reached its result cap, so the
	// result set was capped and may be incomplete (ARCHITECTURE.md §8.2). It is
	// deliberately "a cap was reached" rather than "more exist": the traversal
	// stops once every tier is full and so cannot know whether more existed.
	// Omitted when false, leaving an untruncated response byte-identical to
	// before the cap existed.
	Truncated bool `json:"truncated,omitempty"`
}

// CountryRouteGroup groups direct routes between a specific airport pair.
type CountryRouteGroup struct {
	From   AirportMeta         `json:"from"`
	To     AirportMeta         `json:"to"`
	Routes []CountryRouteEntry `json:"routes"`
}

// CountryRouteEntry is a single direct route within a country route group.
type CountryRouteEntry struct {
	Aircraft     string  `json:"aircraft"`
	Airline      string  `json:"airline"`
	AirlineName  string  `json:"airline_name,omitempty"`
	DistanceKm   float64 `json:"distance_km"`
	FlightNumber string  `json:"flight_number,omitempty"`
}

// CountryRouteMeta is the meta field for country route responses.
type CountryRouteMeta struct {
	FromCountry string `json:"from_country"`
	ToCountry   string `json:"to_country"`

	// Aircraft is a single AircraftMeta for a one-code match, or a []AircraftMeta
	// when the query resolved to a family — mirroring RouteMeta, and matching the
	// array form §8.2 documents.
	//
	// Was *AircraftMeta, which could not carry the array, so a family filter such
	// as `A350` reported `"aircraft": null` and was indistinguishable from no
	// filter at all (2026-07-05 review M6).
	Aircraft interface{} `json:"aircraft"`

	// AircraftQuery is the original query string, set only for a family/multi-code
	// match so a caller can tell which family produced the array.
	AircraftQuery string `json:"aircraft_query,omitempty"`

	PairCount  int                    `json:"pair_count"`
	RouteCount int                    `json:"route_count"`
	TookMs     int64                  `json:"took_ms"`
	Airports   map[string]AirportMeta `json:"airports"`

	// Truncated reports that the direct-route cap was reached, so the result set
	// may be incomplete (ARCHITECTURE.md §8.2).
	Truncated bool `json:"truncated,omitempty"`
}

// AircraftSearchResult is a single result from the aircraft search endpoint.
type AircraftSearchResult struct {
	IATA   string `json:"iata"`
	ICAO   string `json:"icao"`
	Name   string `json:"name"`
	Family string `json:"family"`
}

// AirportSearchResult is a single result from the airport search endpoint.
type AirportSearchResult struct {
	IATA    string  `json:"iata"`
	ICAO    string  `json:"icao"`
	Name    string  `json:"name"`
	City    string  `json:"city"`
	Country string  `json:"country"`
	Lat     float64 `json:"lat"`
	Lon     float64 `json:"lon"`
}

// SearchMeta is the meta field for search endpoint responses.
type SearchMeta struct {
	TookMs int64 `json:"took_ms"`
	Count  int   `json:"count"`
}

// HealthData is the response for the health endpoint.
type HealthData struct {
	Status        string `json:"status"`
	AirportCount  int    `json:"airport_count"`
	EdgeCount     int    `json:"edge_count"`
	AircraftCount int    `json:"aircraft_count"`
	CountryCount  int    `json:"country_count"`
	Neo4jOK       bool   `json:"neo4j_ok"`

	// Graph freshness (ARCHITECTURE.md §12.2). Because the graph is loaded once
	// per process and refreshed by restart, these are how a caller — or the
	// refresh chain's own restart step — confirms a rebuild reached clients.
	// Empty/zero when unset, rather than reporting year 0001 and an age of
	// billions of seconds, which a freshness check would misread.
	GraphLoadedAt   string `json:"graph_loaded_at"`
	GraphAgeSeconds int64  `json:"graph_age_seconds"`
}
