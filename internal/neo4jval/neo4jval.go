// Package neo4jval extracts typed values from Neo4j records, tolerating
// missing keys and nulls. Shared by the graph loader and the API's search
// handlers (2026-07-05 review S6), which previously each carried a copy.
package neo4jval

import "github.com/neo4j/neo4j-go-driver/v5/neo4j"

// String returns rec[key] as a string, or "" when absent, null or not a string.
func String(rec *neo4j.Record, key string) string {
	v, ok := rec.Get(key)
	if !ok || v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}

// Float returns rec[key] as a float64 (int64 is converted), or 0 when absent,
// null or any other type.
func Float(rec *neo4j.Record, key string) float64 {
	v, ok := rec.Get(key)
	if !ok || v == nil {
		return 0
	}
	switch n := v.(type) {
	case float64:
		return n
	case int64:
		return float64(n)
	default:
		return 0
	}
}
