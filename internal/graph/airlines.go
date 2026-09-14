package graph

import (
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"os"
)

// Airline is a minimal record from OpenFlights airlines.dat.
type Airline struct {
	IATA    string
	ICAO    string
	Name    string
	Country string
	Active  bool
}

// LoadAirlines parses an OpenFlights airlines.dat file at the given path.
// Format (CSV): ID,Name,Alias,IATA,ICAO,Callsign,Country,Active
// Returns a map keyed by IATA code. Airlines with missing/sentinel IATA are
// skipped. When multiple airlines share an IATA code, the active entry wins.
func LoadAirlines(path string) (map[string]*Airline, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	r := csv.NewReader(f)
	r.LazyQuotes = true
	r.FieldsPerRecord = -1

	out := make(map[string]*Airline)
	line := 0
	for {
		rec, err := r.Read()
		line++
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("airlines.dat:%d: %w", line, err)
		}
		if len(rec) < 8 {
			continue
		}
		iata := rec[3]
		if iata == "" || iata == `\N` || iata == "-" {
			continue
		}
		active := rec[7] == "Y"
		next := &Airline{
			IATA:    iata,
			ICAO:    rec[4],
			Name:    rec[1],
			Country: rec[6],
			Active:  active,
		}
		// Prefer the active record over a stale one when codes collide.
		if existing, exists := out[iata]; exists && existing.Active && !active {
			continue
		}
		out[iata] = next
	}
	return out, nil
}
