package api

import (
	"encoding/json"
	"log"
	"net/http"
	"time"

	"aircraft-route-planner/internal/graph"
	"aircraft-route-planner/internal/models"

	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
)

// Server holds shared dependencies for all HTTP handlers.
type Server struct {
	graph   *graph.Graph
	driver  neo4j.DriverWithContext
	maxHops int
	mux     *http.ServeMux
}

// NewServer creates a Server and registers all routes.
func NewServer(g *graph.Graph, driver neo4j.DriverWithContext, maxHops int) *Server {
	s := &Server{
		graph:   g,
		driver:  driver,
		maxHops: clampMaxHops(maxHops),
		mux:     http.NewServeMux(),
	}

	s.mux.HandleFunc("GET /routes", s.handleRoutes)
	s.mux.HandleFunc("GET /routes/from", s.handleRoutesFrom)
	s.mux.HandleFunc("GET /routes/countries", s.handleCountryRoutes)
	s.mux.HandleFunc("GET /countries/search", s.handleCountrySearch)
	s.mux.HandleFunc("GET /airports/search", s.handleAirportSearch)
	s.mux.HandleFunc("GET /aircraft/search", s.handleAircraftSearch)
	s.mux.HandleFunc("GET /health", s.handleHealth)

	return s
}

// Handler returns the mux wrapped in middleware.
func (s *Server) Handler() http.Handler {
	var h http.Handler = s.mux
	h = loggingMiddleware(h)
	h = corsMiddleware(h, corsOrigin())
	return h
}

// corsMiddleware advertises a single allowed origin. Resolved once when the
// handler is built rather than per request — it cannot change while running.
func corsMiddleware(next http.Handler, origin string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Set("Vary", "Origin")
		w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)
		log.Printf("%s %s %d %v", r.Method, r.URL.String(), sw.status, time.Since(start))
	})
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("json encode error: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, models.ErrorResponse{
		Error: models.ErrorDetail{Code: code, Message: message},
	})
}
