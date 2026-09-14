// Package env reads configuration from environment variables with fallbacks.
// Shared by cmd/server and the graph loader (2026-07-05 review S6), which
// previously each carried a copy.
package env

import (
	"os"
	"strconv"
)

// Or returns the value of key, or fallback when it is unset or empty.
func Or(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// OrInt returns key parsed as an int, or fallback when it is unset, empty or
// not an integer.
func OrInt(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}
