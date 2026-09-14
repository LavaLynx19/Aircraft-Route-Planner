package api

import (
	"encoding/json"
	"net/http"
	"testing"
)

// Handler-level coverage for the two /routes/countries defects fixed in batch 8
// (2026-07-05 review M6 and L1). Neither had any test, which is how a family
// filter reporting `"aircraft": null` and an unsorted pair order both survived a
// review that named them.

// countryResp lives in handlers_test.go — it predates this file and is shared
// with TestHandleCountryRoutes. Its Meta gained Aircraft/AircraftQuery there
// rather than being duplicated here.

func TestCountryRoutesFamilyFilterReportsItsAircraft(t *testing.T) {
	// M6: CountryRouteMeta.Aircraft was *AircraftMeta, which cannot hold the array
	// form §8.2 documents, so `aircraft=A350` reported null — indistinguishable
	// from no filter at all. The fixture resolves A350 to {359, 351}.
	srv := newFixtureServer()

	code, body := get(t, srv,
		"/routes/countries?from=United%20Kingdom&to=United%20States&aircraft=A350")
	if code != http.StatusOK {
		t.Fatalf("got %d, want 200; body: %s", code, body)
	}

	var resp countryResp
	decode(t, body, &resp)

	if string(resp.Meta.Aircraft) == "null" {
		t.Fatal("meta.aircraft is null for a family filter — the M6 regression")
	}
	var family []struct {
		IATA string `json:"iata"`
	}
	if err := json.Unmarshal(resp.Meta.Aircraft, &family); err != nil {
		t.Fatalf("meta.aircraft should be an array for a family, got %s: %v",
			resp.Meta.Aircraft, err)
	}
	if len(family) != 2 {
		t.Errorf("got %d family members for A350, want 2 (359, 351)", len(family))
	}
	if resp.Meta.AircraftQuery != "A350" {
		t.Errorf("got aircraft_query %q, want \"A350\"", resp.Meta.AircraftQuery)
	}
}

func TestCountryRoutesSingleCodeStillReportsAnObject(t *testing.T) {
	// The single-code form must stay an object, not become an array — §8.2 keeps
	// them distinct, and aircraft_query is omitted.
	srv := newFixtureServer()

	_, body := get(t, srv,
		"/routes/countries?from=United%20Kingdom&to=United%20States&aircraft=388")
	var resp countryResp
	decode(t, body, &resp)

	var single struct {
		IATA string `json:"iata"`
	}
	if err := json.Unmarshal(resp.Meta.Aircraft, &single); err != nil {
		t.Fatalf("meta.aircraft should be an object for one code, got %s",
			resp.Meta.Aircraft)
	}
	if single.IATA != "388" {
		t.Errorf("got aircraft %q, want 388", single.IATA)
	}
	if resp.Meta.AircraftQuery != "" {
		t.Errorf("aircraft_query should be omitted for a single code, got %q",
			resp.Meta.AircraftQuery)
	}
}

func TestCountryRoutesUnfilteredReportsNoAircraft(t *testing.T) {
	srv := newFixtureServer()

	_, body := get(t, srv,
		"/routes/countries?from=United%20Kingdom&to=United%20States")
	var resp countryResp
	decode(t, body, &resp)

	if string(resp.Meta.Aircraft) != "null" {
		t.Errorf("an unfiltered query should report aircraft null, got %s",
			resp.Meta.Aircraft)
	}
}

func TestCountryRoutesPairsAreOrderedByShortestRoute(t *testing.T) {
	// L1, and it was worse than filed: `order` was built in airport-scan order and
	// the sort inside FindCountryRoutes orders individual routes, so §5.5's
	// "sorted by distance of first route in each pair" was unmet for *every*
	// query, not only family ones.
	//
	// The fixture's UK→UAE/US pairs are LHR→DXB at 5500 and LHR→JFK at 5550, so
	// ordering across countries is not observable from one country pair. Within
	// United States, JFK and LAX are both destinations from... no LHR edge reaches
	// LAX directly, so this asserts the invariant generically instead: whatever
	// pairs come back, each group's first route is its shortest, and the groups
	// ascend by that value.
	srv := newFixtureServer()

	_, body := get(t, srv,
		"/routes/countries?from=United%20Kingdom&to=United%20States")
	var resp countryResp
	decode(t, body, &resp)

	if len(resp.Data) == 0 {
		t.Fatal("expected at least one pair")
	}
	for _, grp := range resp.Data {
		if len(grp.Routes) == 0 {
			t.Fatalf("pair %s→%s has no routes", grp.From.IATA, grp.To.IATA)
		}
		first := grp.Routes[0].DistanceKm
		for _, r := range grp.Routes {
			if r.DistanceKm < first {
				t.Errorf("pair %s→%s: routes[0]=%.0f is not the shortest (%.0f)",
					grp.From.IATA, grp.To.IATA, first, r.DistanceKm)
			}
		}
	}
	for i := 1; i < len(resp.Data); i++ {
		prev := resp.Data[i-1].Routes[0].DistanceKm
		cur := resp.Data[i].Routes[0].DistanceKm
		if prev > cur {
			t.Errorf("pairs not ascending by first-route distance: %.0f before %.0f",
				prev, cur)
		}
	}
}

func TestCountryRoutesMultiCodeMatchesEveryCode(t *testing.T) {
	// The behaviour the old per-code loop got wrong: a family must return the union
	// of its codes' routes, in one globally sorted result rather than concatenated
	// chunks. LHR→JFK carries 738/388/359, so A350 ({359,351}) matches exactly the
	// 359 edge here.
	srv := newFixtureServer()

	_, body := get(t, srv,
		"/routes/countries?from=United%20Kingdom&to=United%20States&aircraft=A350")
	var resp countryResp
	decode(t, body, &resp)

	seen := map[string]int{}
	for _, grp := range resp.Data {
		for _, r := range grp.Routes {
			seen[r.Aircraft]++
		}
	}
	if seen["359"] != 1 {
		t.Errorf("expected the 359 edge once, got %d (all: %v)", seen["359"], seen)
	}
	for code := range seen {
		if code != "359" && code != "351" {
			t.Errorf("aircraft %q is not in the A350 family", code)
		}
	}
	if resp.Meta.RouteCount != 1 {
		t.Errorf("got route_count %d, want 1", resp.Meta.RouteCount)
	}
}
