package api

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"strings"
	"testing"

	"aircraft-route-planner/internal/graph"
)

// fixtureGraph builds a small in-memory graph covering the topology required
// by the Phase 4 edge-case matrix:
//
//	Airports: LHR (UK), DXB (UAE), SYD (Australia), JFK (USA), LAX (USA)
//	Aircraft: 388 (A380), 359 (A350-900), 351 (A350-1000), 738 (737-800)
//	Aliases:  "A350" → [359, 351]   (multi-code family resolution)
//	Edges:
//	  LHR --388--> DXB --388--> SYD          (strict A380 path)
//	  LHR --738--> JFK --388--> SYD          (inclusive-only path)
//	  LHR --388--> JFK --388--> SYD          (additional strict path)
//	  LHR --359--> JFK                       (direct A350-900)
//	  JFK --738--> LAX                       (no A380 from JFK to LAX)
func fixtureGraph() *graph.Graph {
	airports := map[string]*graph.Airport{
		"LHR": {IATA: "LHR", Name: "Heathrow", City: "London", Country: "United Kingdom", Lat: 51.47, Lon: -0.45},
		"DXB": {IATA: "DXB", Name: "Dubai Intl", City: "Dubai", Country: "United Arab Emirates", Lat: 25.25, Lon: 55.36},
		"SYD": {IATA: "SYD", Name: "Sydney", City: "Sydney", Country: "Australia", Lat: -33.94, Lon: 151.18},
		"JFK": {IATA: "JFK", Name: "JFK", City: "New York", Country: "United States", Lat: 40.64, Lon: -73.78},
		"LAX": {IATA: "LAX", Name: "Los Angeles", City: "Los Angeles", Country: "United States", Lat: 33.94, Lon: -118.41},
	}
	adj := map[string][]graph.Edge{
		"LHR": {
			{Dst: "DXB", Aircraft: "388", Airline: "EK", Distance: 5500},
			{Dst: "JFK", Aircraft: "738", Airline: "BA", Distance: 5550},
			{Dst: "JFK", Aircraft: "388", Airline: "BA", Distance: 5550},
			{Dst: "JFK", Aircraft: "359", Airline: "BA", Distance: 5550},
		},
		"DXB": {
			{Dst: "SYD", Aircraft: "388", Airline: "EK", Distance: 12050},
		},
		"JFK": {
			{Dst: "SYD", Aircraft: "388", Airline: "QF", Distance: 15990},
			{Dst: "LAX", Aircraft: "738", Airline: "AA", Distance: 3970},
		},
	}
	byCountry := map[string][]string{
		"United Kingdom":       {"LHR"},
		"United Arab Emirates": {"DXB"},
		"Australia":            {"SYD"},
		"United States":        {"JFK", "LAX"},
	}
	countries := []string{"Australia", "United Arab Emirates", "United Kingdom", "United States"}
	aircraftByIATA := map[string]*graph.Aircraft{
		"388": {IATA: "388", ICAO: "A388", Name: "Airbus A380-800", Family: "A380"},
		"359": {IATA: "359", ICAO: "A359", Name: "Airbus A350-900", Family: "A350"},
		"351": {IATA: "351", ICAO: "A351", Name: "Airbus A350-1000", Family: "A350"},
		"738": {IATA: "738", ICAO: "B738", Name: "Boeing 737-800", Family: "737"},
	}
	aliasToIATA := map[string][]string{
		"388":  {"388"},
		"A388": {"388"},
		"A380": {"388"},
		"359":  {"359"},
		"351":  {"351"},
		"A350": {"359", "351"},
		"738":  {"738"},
	}
	families := map[string][]string{
		"A380": {"388"},
		"A350": {"359", "351"},
		"737":  {"738"},
	}
	return &graph.Graph{
		Airports:       airports,
		Adj:            adj,
		ByCountry:      byCountry,
		Countries:      countries,
		AircraftByIATA: aircraftByIATA,
		AliasToIATA:    aliasToIATA,
		Families:       families,
	}
}

func newFixtureServer() *Server {
	return NewServer(fixtureGraph(), nil, 2)
}

// TestMain silences the request-logging middleware so test output is clean.
func TestMain(m *testing.M) {
	log.SetOutput(io.Discard)
	os.Exit(m.Run())
}

func get(t *testing.T, srv *Server, url string) (int, []byte) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, url, nil)
	w := httptest.NewRecorder()
	srv.Handler().ServeHTTP(w, req)
	resp := w.Result()
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	return resp.StatusCode, body
}

func decode(t *testing.T, body []byte, v any) {
	t.Helper()
	if err := json.NewDecoder(bytes.NewReader(body)).Decode(v); err != nil {
		t.Fatalf("decode JSON: %v\nbody=%s", err, string(body))
	}
}

// errorBody is the shape of a writeError response.
type errorBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

// routeResp is a partial decode of the /routes and /routes/from response.
type routeResp struct {
	Data []struct {
		Path            []string `json:"path"`
		Hops            int      `json:"hops"`
		TotalDistanceKm float64  `json:"total_distance_km"`
		Legs            []struct {
			From     string `json:"from"`
			To       string `json:"to"`
			Aircraft string `json:"aircraft"`
			IsTarget bool   `json:"is_target"`
		} `json:"legs"`
	} `json:"data"`
	Meta struct {
		Aircraft any    `json:"aircraft"`
		Mode     string `json:"mode"`
		Count    int    `json:"count"`
	} `json:"meta"`
}

type countryResp struct {
	Data []struct {
		From struct {
			IATA string `json:"iata"`
		} `json:"from"`
		To struct {
			IATA string `json:"iata"`
		} `json:"to"`
		Routes []struct {
			Aircraft   string  `json:"aircraft"`
			DistanceKm float64 `json:"distance_km"`
		} `json:"routes"`
	} `json:"data"`
	Meta struct {
		PairCount  int `json:"pair_count"`
		RouteCount int `json:"route_count"`

		// Added 2026-09-13 for review M6: meta.aircraft is a single object for one
		// code, an array for a family, or null when unfiltered — so it is decoded
		// raw and asserted per case in country_test.go. aircraft_query names the
		// family that produced an array and is omitted otherwise.
		Aircraft      json.RawMessage `json:"aircraft"`
		AircraftQuery string          `json:"aircraft_query"`
	} `json:"meta"`
}

type searchResp struct {
	Data []string `json:"data"`
	Meta struct {
		Count int `json:"count"`
	} `json:"meta"`
}

func TestHandleRoutes_Validation(t *testing.T) {
	srv := newFixtureServer()
	cases := []struct {
		name     string
		url      string
		wantCode int
		wantErr  string
	}{
		{"missing aircraft", "/routes?from=LHR&to=SYD", 400, "MISSING_PARAM"},
		{"missing from", "/routes?aircraft=388&to=SYD", 400, "MISSING_PARAM"},
		{"missing to", "/routes?aircraft=388&from=LHR", 400, "MISSING_PARAM"},
		{"unknown aircraft", "/routes?aircraft=XYZ&from=LHR&to=SYD", 404, "INVALID_AIRCRAFT"},
		{"unknown airport from", "/routes?aircraft=388&from=ZZZ&to=SYD", 404, "INVALID_AIRPORT"},
		{"unknown airport to", "/routes?aircraft=388&from=LHR&to=ZZZ", 404, "INVALID_AIRPORT"},
		{"invalid mode", "/routes?aircraft=388&from=LHR&to=SYD&mode=banana", 400, "INVALID_MODE"},
		{"invalid sort", "/routes?aircraft=388&from=LHR&to=SYD&sort=banana", 400, "INVALID_SORT"},
		// Jul review L2: a non-integer max_hops used to default silently to 2,
		// hiding client bugs, while every other malformed param here returns 400.
		{"unparseable max_hops", "/routes?aircraft=388&from=LHR&to=SYD&max_hops=abc", 400, "INVALID_PARAM"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			code, body := get(t, srv, tc.url)
			if code != tc.wantCode {
				t.Fatalf("status: want %d, got %d (body=%s)", tc.wantCode, code, body)
			}
			var e errorBody
			decode(t, body, &e)
			if e.Error.Code != tc.wantErr {
				t.Fatalf("error code: want %s, got %s", tc.wantErr, e.Error.Code)
			}
		})
	}
}

func TestHandleRoutes_StrictAndInclusive(t *testing.T) {
	srv := newFixtureServer()

	t.Run("strict mode all legs target", func(t *testing.T) {
		code, body := get(t, srv, "/routes?aircraft=388&from=LHR&to=SYD&max_hops=2")
		if code != 200 {
			t.Fatalf("status %d body=%s", code, body)
		}
		var rr routeResp
		decode(t, body, &rr)
		if rr.Meta.Count == 0 {
			t.Fatalf("expected ≥1 result, got 0")
		}
		if rr.Meta.Mode != "strict" {
			t.Fatalf("default mode should be strict, got %s", rr.Meta.Mode)
		}
		for _, r := range rr.Data {
			for _, leg := range r.Legs {
				if !leg.IsTarget {
					t.Errorf("strict mode: every leg must have is_target=true (path=%v)", r.Path)
				}
				if leg.Aircraft != "388" {
					t.Errorf("strict mode: every leg aircraft must be 388 (got %s)", leg.Aircraft)
				}
			}
		}
	})

	t.Run("inclusive mode allows non-target legs", func(t *testing.T) {
		code, body := get(t, srv, "/routes?aircraft=388&from=LHR&to=SYD&max_hops=2&mode=inclusive")
		if code != 200 {
			t.Fatalf("status %d body=%s", code, body)
		}
		var rr routeResp
		decode(t, body, &rr)
		if rr.Meta.Mode != "inclusive" {
			t.Fatalf("mode = %s", rr.Meta.Mode)
		}
		var foundMixed bool
		for _, r := range rr.Data {
			hasNonTarget := false
			hasTarget := false
			for _, leg := range r.Legs {
				if leg.IsTarget {
					hasTarget = true
				} else {
					hasNonTarget = true
				}
			}
			if !hasTarget {
				t.Errorf("inclusive: route must have ≥1 target leg (path=%v legs=%v)", r.Path, r.Legs)
			}
			if hasNonTarget {
				foundMixed = true
			}
		}
		if !foundMixed {
			t.Errorf("inclusive: expected at least one route with a non-target leg (738→388 via JFK)")
		}
	})

	t.Run("inclusive with max_hops=0 only direct on target aircraft", func(t *testing.T) {
		// LHR has no direct 388 edge to SYD, so result must be empty.
		_, body := get(t, srv, "/routes?aircraft=388&from=LHR&to=SYD&max_hops=0&mode=inclusive")
		var rr routeResp
		decode(t, body, &rr)
		if rr.Meta.Count != 0 {
			t.Fatalf("expected 0 results for inclusive max_hops=0 LHR→SYD, got %d", rr.Meta.Count)
		}
	})

	t.Run("max_hops=0 direct only", func(t *testing.T) {
		// Direct LHR→JFK on 388 exists.
		_, body := get(t, srv, "/routes?aircraft=388&from=LHR&to=JFK&max_hops=0")
		var rr routeResp
		decode(t, body, &rr)
		if rr.Meta.Count != 1 {
			t.Fatalf("expected 1 direct LHR→JFK on 388, got %d", rr.Meta.Count)
		}
		if rr.Data[0].Hops != 0 {
			t.Errorf("direct flight Hops should be 0, got %d", rr.Data[0].Hops)
		}
	})

	t.Run("max_hops above ceiling is silently clamped", func(t *testing.T) {
		// maxHops=2 in fixture; passing 999 must not error.
		code, _ := get(t, srv, "/routes?aircraft=388&from=LHR&to=SYD&max_hops=999")
		if code != 200 {
			t.Fatalf("clamping must not error, got status %d", code)
		}
	})

	// Behaviour deliberately changed 2026-09-13 (Jul review L2). This previously
	// asserted 200 under the name "negative max_hops falls back to default".
	// ARCHITECTURE §8.3 specifies 400 for bad input and this endpoint already
	// rejects every other malformed parameter, so silent coercion was the outlier.
	// Clamping a well-formed but oversized value is unchanged — see the 999 case.
	t.Run("negative max_hops is rejected, not coerced", func(t *testing.T) {
		code, body := get(t, srv, "/routes?aircraft=388&from=LHR&to=SYD&max_hops=-5")
		if code != 400 {
			t.Fatalf("got status %d, want 400; body: %s", code, body)
		}
	})

	t.Run("no routes found returns empty data not error", func(t *testing.T) {
		// Strict 388 LHR→LAX has no all-388 path (JFK→LAX is 738 only).
		code, body := get(t, srv, "/routes?aircraft=388&from=LHR&to=LAX&max_hops=2")
		if code != 200 {
			t.Fatalf("expected 200 with empty data, got %d body=%s", code, body)
		}
		var rr routeResp
		decode(t, body, &rr)
		if rr.Meta.Count != 0 {
			t.Errorf("expected count=0, got %d", rr.Meta.Count)
		}
	})

	t.Run("aircraft family resolves to multiple codes", func(t *testing.T) {
		code, body := get(t, srv, "/routes?aircraft=A350&from=LHR&to=JFK&max_hops=0")
		if code != 200 {
			t.Fatalf("status %d", code)
		}
		var raw map[string]any
		decode(t, body, &raw)
		meta := raw["meta"].(map[string]any)
		ac, ok := meta["aircraft"].([]any)
		if !ok {
			t.Fatalf("expected meta.aircraft to be array for family, got %T", meta["aircraft"])
		}
		if len(ac) != 2 {
			t.Errorf("expected 2 family members for A350, got %d", len(ac))
		}
	})

	t.Run("sort=distance returns the single shortest route", func(t *testing.T) {
		// Jul review M2, resolved 2026-09-13 in favour of single-shortest semantics
		// (ARCHITECTURE §8.1 amended to document it; §5.2 already assigned Dijkstra
		// this job). Tightened from the original `Count > 1` assertion, which also
		// passed when zero routes came back — the more likely regression.
		//
		// Two 388 paths exist: LHR-DXB-SYD at 5500+12050 = 17550 and LHR-JFK-SYD at
		// 5550+15990 = 21540, so the shortest is unambiguous.
		code, body := get(t, srv, "/routes?aircraft=388&from=LHR&to=SYD&max_hops=2&sort=distance")
		if code != 200 {
			t.Fatalf("status %d body=%s", code, body)
		}
		var rr routeResp
		decode(t, body, &rr)
		if rr.Meta.Count != 1 || len(rr.Data) != 1 {
			t.Fatalf("want exactly 1 route, got count=%d data=%d; body=%s",
				rr.Meta.Count, len(rr.Data), body)
		}
		if got := strings.Join(rr.Data[0].Path, "-"); got != "LHR-DXB-SYD" {
			t.Errorf("got path %q, want the shorter LHR-DXB-SYD", got)
		}
		if rr.Data[0].TotalDistanceKm != 17550 {
			t.Errorf("got %.0f km, want 17550", rr.Data[0].TotalDistanceKm)
		}
	})
}

func TestHandleRoutesFrom(t *testing.T) {
	srv := newFixtureServer()

	t.Run("missing aircraft", func(t *testing.T) {
		code, body := get(t, srv, "/routes/from?from=LHR")
		if code != 400 {
			t.Fatalf("status %d body=%s", code, body)
		}
	})

	t.Run("strict reaches all 388-reachable airports", func(t *testing.T) {
		_, body := get(t, srv, "/routes/from?aircraft=388&from=LHR&max_hops=2")
		var rr routeResp
		decode(t, body, &rr)
		dests := map[string]bool{}
		for _, r := range rr.Data {
			dests[r.Path[len(r.Path)-1]] = true
			for _, leg := range r.Legs {
				if !leg.IsTarget {
					t.Errorf("strict: leg must be target (got %s)", leg.Aircraft)
				}
			}
		}
		// Reachable on 388 from LHR within 2 hops: DXB, JFK, SYD.
		for _, d := range []string{"DXB", "JFK", "SYD"} {
			if !dests[d] {
				t.Errorf("expected %s reachable on 388 from LHR; got dests=%v", d, dests)
			}
		}
	})

	t.Run("inclusive every result has at least one target leg", func(t *testing.T) {
		_, body := get(t, srv, "/routes/from?aircraft=738&from=LHR&max_hops=2&mode=inclusive")
		var rr routeResp
		decode(t, body, &rr)
		if rr.Meta.Count == 0 {
			t.Fatalf("expected ≥1 inclusive route from LHR on 738")
		}
		for _, r := range rr.Data {
			has := false
			for _, leg := range r.Legs {
				if leg.IsTarget {
					has = true
					break
				}
			}
			if !has {
				t.Errorf("inclusive: route %v has no target leg", r.Path)
			}
		}
	})

	t.Run("aircraft with no edges from origin returns empty", func(t *testing.T) {
		// 738 has no edges from SYD in fixture.
		_, body := get(t, srv, "/routes/from?aircraft=738&from=SYD&max_hops=2")
		var rr routeResp
		decode(t, body, &rr)
		if rr.Meta.Count != 0 {
			t.Errorf("expected 0 results, got %d", rr.Meta.Count)
		}
	})
}

func TestHandleCountryRoutes(t *testing.T) {
	srv := newFixtureServer()

	t.Run("missing param", func(t *testing.T) {
		code, _ := get(t, srv, "/routes/countries?from=United%20Kingdom")
		if code != 400 {
			t.Fatalf("status %d", code)
		}
	})

	t.Run("unknown country", func(t *testing.T) {
		code, body := get(t, srv, "/routes/countries?from=Atlantis&to=Australia")
		if code != 404 {
			t.Fatalf("status %d body=%s", code, body)
		}
		var e errorBody
		decode(t, body, &e)
		if e.Error.Code != "COUNTRY_NOT_FOUND" {
			t.Errorf("got code %s", e.Error.Code)
		}
	})

	t.Run("UK to USA without aircraft groups by airport pair", func(t *testing.T) {
		_, body := get(t, srv, "/routes/countries?from=United%20Kingdom&to=United%20States")
		var cr countryResp
		decode(t, body, &cr)
		// LHR→JFK has 3 aircraft (388, 738, 359) → 1 pair, 3 routes.
		if cr.Meta.PairCount != 1 {
			t.Errorf("PairCount: got %d, want 1", cr.Meta.PairCount)
		}
		if cr.Meta.RouteCount != 3 {
			t.Errorf("RouteCount: got %d, want 3", cr.Meta.RouteCount)
		}
	})

	t.Run("UK to USA filtered by aircraft 388", func(t *testing.T) {
		_, body := get(t, srv, "/routes/countries?from=United%20Kingdom&to=United%20States&aircraft=388")
		var cr countryResp
		decode(t, body, &cr)
		if cr.Meta.RouteCount != 1 {
			t.Errorf("RouteCount: got %d, want 1", cr.Meta.RouteCount)
		}
		if len(cr.Data) > 0 && cr.Data[0].Routes[0].Aircraft != "388" {
			t.Errorf("expected 388, got %s", cr.Data[0].Routes[0].Aircraft)
		}
	})

	t.Run("unknown aircraft on country query 404", func(t *testing.T) {
		code, _ := get(t, srv, "/routes/countries?from=United%20Kingdom&to=Australia&aircraft=XYZ")
		if code != 404 {
			t.Errorf("status %d", code)
		}
	})

	t.Run("no matching routes returns empty data", func(t *testing.T) {
		// UAE → USA has no edges in fixture.
		_, body := get(t, srv, "/routes/countries?from=United%20Arab%20Emirates&to=United%20States")
		var cr countryResp
		decode(t, body, &cr)
		if cr.Meta.RouteCount != 0 {
			t.Errorf("expected 0 routes, got %d", cr.Meta.RouteCount)
		}
	})
}

// airportMetaJSON mirrors models.AirportMeta for test decoding.
type airportMetaJSON struct {
	IATA    string  `json:"iata"`
	ICAO    string  `json:"icao"`
	Name    string  `json:"name"`
	City    string  `json:"city"`
	Country string  `json:"country"`
	Lat     float64 `json:"lat"`
	Lon     float64 `json:"lon"`
}

func TestAirportsMapInRouteMeta(t *testing.T) {
	srv := newFixtureServer()

	t.Run("/routes meta.airports contains every IATA in legs with lat/lon", func(t *testing.T) {
		_, body := get(t, srv, "/routes?aircraft=388&from=LHR&to=SYD&max_hops=2")
		var resp struct {
			Data []struct {
				Legs []struct {
					From string `json:"from"`
					To   string `json:"to"`
				} `json:"legs"`
			} `json:"data"`
			Meta struct {
				Airports map[string]airportMetaJSON `json:"airports"`
			} `json:"meta"`
		}
		decode(t, body, &resp)

		seen := map[string]bool{}
		for _, r := range resp.Data {
			for _, leg := range r.Legs {
				seen[leg.From] = true
				seen[leg.To] = true
			}
		}
		if len(seen) == 0 {
			t.Fatalf("no legs in response, cannot validate airports map")
		}
		for iata := range seen {
			ap, ok := resp.Meta.Airports[iata]
			if !ok {
				t.Errorf("airports map missing %s", iata)
				continue
			}
			if ap.IATA != iata {
				t.Errorf("airports[%s].iata = %q, want %q", iata, ap.IATA, iata)
			}
			if ap.Lat == 0 && ap.Lon == 0 {
				t.Errorf("airports[%s] has no coords (lat=%v lon=%v)", iata, ap.Lat, ap.Lon)
			}
		}
	})

	t.Run("/routes/from meta.airports contains every IATA in legs", func(t *testing.T) {
		_, body := get(t, srv, "/routes/from?aircraft=388&from=LHR&max_hops=2")
		var resp struct {
			Data []struct {
				Legs []struct {
					From string `json:"from"`
					To   string `json:"to"`
				} `json:"legs"`
			} `json:"data"`
			Meta struct {
				Airports map[string]airportMetaJSON `json:"airports"`
			} `json:"meta"`
		}
		decode(t, body, &resp)
		for _, r := range resp.Data {
			for _, leg := range r.Legs {
				if _, ok := resp.Meta.Airports[leg.From]; !ok {
					t.Errorf("airports map missing %s (from)", leg.From)
				}
				if _, ok := resp.Meta.Airports[leg.To]; !ok {
					t.Errorf("airports map missing %s (to)", leg.To)
				}
			}
		}
	})

	t.Run("/routes/countries meta.airports contains every paired airport", func(t *testing.T) {
		_, body := get(t, srv, "/routes/countries?from=United%20Kingdom&to=United%20States")
		var resp struct {
			Data []struct {
				From struct {
					IATA string  `json:"iata"`
					Lat  float64 `json:"lat"`
					Lon  float64 `json:"lon"`
				} `json:"from"`
				To struct {
					IATA string  `json:"iata"`
					Lat  float64 `json:"lat"`
					Lon  float64 `json:"lon"`
				} `json:"to"`
			} `json:"data"`
			Meta struct {
				Airports map[string]airportMetaJSON `json:"airports"`
			} `json:"meta"`
		}
		decode(t, body, &resp)
		if len(resp.Data) == 0 {
			t.Fatalf("expected at least one airport-pair group")
		}
		for _, g := range resp.Data {
			if g.From.Lat == 0 && g.From.Lon == 0 {
				t.Errorf("country group from %s missing coords", g.From.IATA)
			}
			if g.To.Lat == 0 && g.To.Lon == 0 {
				t.Errorf("country group to %s missing coords", g.To.IATA)
			}
			if _, ok := resp.Meta.Airports[g.From.IATA]; !ok {
				t.Errorf("airports map missing %s", g.From.IATA)
			}
			if _, ok := resp.Meta.Airports[g.To.IATA]; !ok {
				t.Errorf("airports map missing %s", g.To.IATA)
			}
		}
	})
}

func TestHandleCountrySearch(t *testing.T) {
	srv := newFixtureServer()

	t.Run("missing q", func(t *testing.T) {
		code, _ := get(t, srv, "/countries/search")
		if code != 400 {
			t.Fatalf("status %d", code)
		}
	})

	t.Run("substring match case-insensitive", func(t *testing.T) {
		_, body := get(t, srv, "/countries/search?q=united")
		var sr searchResp
		decode(t, body, &sr)
		want := []string{"United Arab Emirates", "United Kingdom", "United States"}
		got := append([]string(nil), sr.Data...)
		sort.Strings(got)
		if len(got) != len(want) {
			t.Fatalf("got %d matches (%v), want %d (%v)", len(got), got, len(want), want)
		}
		for i := range want {
			if got[i] != want[i] {
				t.Errorf("match %d: got %s want %s", i, got[i], want[i])
			}
		}
	})

	t.Run("no match returns empty", func(t *testing.T) {
		_, body := get(t, srv, "/countries/search?q=zzznope")
		var sr searchResp
		decode(t, body, &sr)
		if sr.Meta.Count != 0 {
			t.Errorf("expected 0 matches, got %d", sr.Meta.Count)
		}
	})
}
