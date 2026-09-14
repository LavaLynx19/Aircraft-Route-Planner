#!/usr/bin/env python3
"""Aircraft Route Planner — Data Ingestion Pipeline.

Populates Neo4j from OpenFlights CSV data.
Run: python ingest/main.py
"""
import time
from neo4j import GraphDatabase
from config import neo4j_config
from constraints import create_constraints
from load_aircraft import load_aircraft
from normalize_aircraft import normalize_aircraft
from load_airports import load_airports
from load_routes import load_routes


def print_summary(driver):
    with driver.session() as session:
        airports = session.run("MATCH (a:Airport) RETURN count(a) AS c").single()["c"]
        aircraft = session.run("MATCH (ac:Aircraft) RETURN count(ac) AS c").single()["c"]
        routes = session.run("MATCH ()-[r:ROUTE]->() RETURN count(r) AS c").single()["c"]
        a380 = session.run(
            "MATCH ()-[r:ROUTE {aircraft: '388'}]->() RETURN count(r) AS c"
        ).single()["c"]

    print("\n--- Summary ---")
    print(f"  Airports:  {airports}")
    print(f"  Aircraft:  {aircraft}")
    print(f"  Routes:    {routes}")
    print(f"  A380 (388) routes: {a380}")


def main():
    cfg = neo4j_config()
    print(f"Connecting to Neo4j at {cfg['uri']}...")
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))

    # Verify connectivity
    driver.verify_connectivity()
    print("Connected.\n")

    start = time.time()

    try:
        print("[1/5] Creating constraints and indexes...")
        create_constraints(driver)

        print("\n[2/5] Loading aircraft...")
        load_aircraft(driver)

        print("\n[3/5] Normalizing aircraft (family + aliases)...")
        normalize_aircraft(driver)

        print("\n[4/5] Loading airports...")
        load_airports(driver)

        print("\n[5/5] Loading routes...")
        load_routes(driver)

        elapsed = time.time() - start
        print(f"\nIngestion complete in {elapsed:.1f}s.")
        print_summary(driver)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
