package graph

import (
	"math"
	"testing"
)

// Expected distances are exact fractions of a great circle on the sphere
// Haversine assumes, so the test checks the formula rather than a real-world
// distance table that would bake in some other earth model.
func TestHaversine(t *testing.T) {
	quarter := earthRadiusKm * math.Pi / 2

	tests := []struct {
		name                   string
		lat1, lon1, lat2, lon2 float64
		want                   float64
	}{
		{"same point", 51.47, -0.45, 51.47, -0.45, 0},
		{"quarter turn along the equator", 0, 0, 0, 90, quarter},
		{"half turn along the equator", 0, 0, 0, 180, 2 * quarter},
		{"equator to pole", 0, 0, 90, 0, quarter},
		{"pole to pole", 90, 0, -90, 0, 2 * quarter},
		{"across the antimeridian", 0, 179, 0, -179, earthRadiusKm * 2 * math.Pi / 180},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Haversine(tt.lat1, tt.lon1, tt.lat2, tt.lon2)
			if math.Abs(got-tt.want) > 1e-6 {
				t.Errorf("Haversine = %.9f km, want %.9f", got, tt.want)
			}
			if back := Haversine(tt.lat2, tt.lon2, tt.lat1, tt.lon1); math.Abs(back-got) > 1e-9 {
				t.Errorf("not symmetric: %.9f one way, %.9f the other", got, back)
			}
		})
	}
}
