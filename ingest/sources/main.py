"""Staging pipeline orchestrator (Phase 6.2) — targets STAGING Neo4j only.

Run: python ingest/sources/main.py          (from repo root, staging Neo4j up)

Sets up staging structure (constraints, airports, aircraft) and prints the
fetch/load commands. Flight fetching runs separately (daily, launchd):
    python ingest/sources/fetch_flights.py
    python ingest/sources/load_graph.py
"""

import os
import sys
import time

from neo4j import GraphDatabase

# Parent ingest/ modules AFTER the script dir, so `config` resolves to
# sources/config.py (whose neo4j_config alias targets staging).
sys.path.insert(1, os.path.join(os.path.dirname(__file__), ".."))

from build_airline_map import main as build_airline_map
from config import staging_neo4j_config
from constraints import create_constraints
from load_aircraft import load_aircraft
from load_aircraft_db import main as load_aircraft_db
from load_airports_oa import load_airports
from normalize_aircraft import normalize_aircraft


def print_summary(driver) -> None:
    with driver.session() as session:
        airports = session.run("MATCH (a:Airport) RETURN count(a) AS c").single()["c"]
        aircraft = session.run("MATCH (ac:Aircraft) RETURN count(ac) AS c").single()[
            "c"
        ]
        routes = session.run("MATCH ()-[r:ROUTE]->() RETURN count(r) AS c").single()[
            "c"
        ]
    print("\n--- Staging summary ---")
    print(f"  Airports:  {airports}")
    print(f"  Aircraft:  {aircraft}")
    print(f"  Routes:    {routes}")


def main() -> None:
    cfg = staging_neo4j_config()
    print(f"Connecting to STAGING Neo4j at {cfg['uri']}...")
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    driver.verify_connectivity()
    print("Connected.\n")

    start = time.time()
    try:
        print("[1/5] Creating constraints and indexes...")
        create_constraints(driver)

        print("\n[2/5] Loading airports (OurAirports)...")
        load_airports(driver)

        print("\n[3/5] Loading aircraft (planes.dat) + normalizing...")
        load_aircraft(driver)
        normalize_aircraft(driver)

        print("\n[4/5] Building airline prefix map (airlines.dat)...")
        build_airline_map()

        print("\n[5/5] Loading icao24→type database (Mictronics/tar1090-db)...")
        load_aircraft_db()

        print(f"\nStaging setup complete in {time.time() - start:.1f}s.")
        print_summary(driver)
        print("\nNext: python ingest/sources/fetch_flights.py   (daily via launchd)")
        print(
            "Then: python ingest/sources/load_graph.py       (after data accumulates)"
        )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
