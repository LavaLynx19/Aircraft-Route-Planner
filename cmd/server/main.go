package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"aircraft-route-planner/internal/api"
	"aircraft-route-planner/internal/env"
	"aircraft-route-planner/internal/graph"
)

// apiPrefixes are the path prefixes routed to the JSON API. Anything else falls
// through to the static file server when web/dist exists.
var apiPrefixes = []string{"/routes", "/airports", "/aircraft", "/countries", "/health"}

func isAPIPath(p string) bool {
	for _, pref := range apiPrefixes {
		if p == pref || strings.HasPrefix(p, pref+"/") {
			return true
		}
	}
	return false
}

func main() {
	port := env.Or("PORT", "8080")
	maxHops := env.OrInt("GRAPH_MAX_HOPS", 3)
	staticDir := env.Or("WEB_DIST_DIR", "web/dist")

	ctx := context.Background()
	g, driver, err := graph.LoadGraph(ctx)
	if err != nil {
		log.Fatalf("Failed to load graph: %v", err)
	}

	srv := api.NewServer(g, driver, maxHops)
	handler := srv.Handler()
	if info, err := os.Stat(staticDir); err == nil && info.IsDir() {
		fs := http.FileServer(http.Dir(staticDir))
		apiHandler := handler
		handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if isAPIPath(r.URL.Path) {
				apiHandler.ServeHTTP(w, r)
				return
			}
			fs.ServeHTTP(w, r)
		})
		log.Printf("Serving static files from %s/", staticDir)
	} else {
		log.Printf("No %s/ directory; API-only mode (run Vite dev server separately)", staticDir)
	}

	httpServer := &http.Server{
		Addr:    ":" + port,
		Handler: handler,
		// ReadHeaderTimeout bounds the header phase specifically, which is what a
		// slowloris client stretches. ReadTimeout alone does not cover it: a client
		// dribbling headers resets nothing and holds the connection (review S7).
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		// Kept above internal/api's 10s traversalBudget so an expensive query still
		// has room to write its response or its 503.
		WriteTimeout: 30 * time.Second,
		// Without IdleTimeout a keep-alive connection is held until the client
		// disconnects, so idle peers accumulate indefinitely.
		IdleTimeout: 120 * time.Second,
	}

	done := make(chan os.Signal, 1)
	signal.Notify(done, syscall.SIGINT, syscall.SIGTERM)

	// shutdownDone closes only once Shutdown has finished draining connections.
	//
	// ListenAndServe returns the moment Shutdown is *called*, not when it
	// completes, so without this wait main would reach driver.Close() while
	// in-flight handlers were still running — and the search endpoints query Neo4j
	// directly, so they would hit a closed driver mid-request (2026-07-05 review
	// M3). Waiting cannot deadlock: ErrServerClosed is only produced by Shutdown,
	// which this goroutine calls, and any other listen error exits via Fatalf.
	shutdownDone := make(chan struct{})

	go func() {
		defer close(shutdownDone)
		<-done
		log.Println("Shutting down...")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(shutdownCtx); err != nil {
			log.Printf("Shutdown error: %v", err)
		}
	}()

	log.Printf("Server listening on :%s", port)
	if err := httpServer.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatalf("Server error: %v", err)
	}

	<-shutdownDone
	driver.Close(context.Background())
	log.Println("Server stopped")
}
