# Aircraft Route Planner — Project Standards

Overrides global CLAUDE.md for this project.

Languages: Go (API server + graph engine, cmd/ + internal/), Python (data ingestion only, ingest/). No frameworks: net/http stdlib for Go, csv stdlib for Python.

Database: Neo4j via official drivers (neo4j for Python, neo4j-go-driver/v5 for Go). No ORM, no query builder — write Cypher directly. One ROUTE relationship per aircraft-route pair (denormalized, see Architecture Section 2.2).

Constraints: Max hops hard limit 3 — enforce at API handler level. Haversine distance computed at ingestion time, stored on ROUTE relationships. Search queries → Neo4j Cypher; pathfinding → Go in-memory engine.

Go specifics: container/heap for priority queue (implement interface, no third-party). Graph loaded once at startup, shared read-only — no mutexes.

Python specifics: neo4j official driver v5 (not py2neo). Batch via UNWIND+MERGE. Create constraints before loading data.

Documents: design decisions → ARCHITECTURE.md, implementation steps → PLAN.md, business context → README.md.
