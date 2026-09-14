#!/usr/bin/env python3
"""Phase 6.4 — promote the new-source pipeline to PRODUCTION Neo4j.

Wipes production and re-ingests from the current sources: OurAirports
(airports), planes.dat + supplement (aircraft), and the accumulated OpenSky
SQLite cache (routes). Re-run any time to refresh production from the cache
(the Go server must be restarted afterwards — it loads the graph at startup).

Rollback to the OpenFlights baseline: python ingest/main.py

This is the ONLY sources/ script that touches production, and only when
invoked explicitly: python ingest/sources/promote.py --yes
"""

import os
import sys
import time

from neo4j import GraphDatabase

sys.path.insert(1, os.path.join(os.path.dirname(__file__), ".."))

from config import load_env
from constraints import create_constraints
from load_aircraft import load_aircraft
from load_airports_oa import load_airports
from load_graph import (
    aggregate,
    load_routes,
    merge_supplement_aircraft,
    shared_lookups,
)
from normalize_aircraft import normalize_aircraft


def production_driver():
    load_env()
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise SystemExit("NEO4J_PASSWORD not set")
    print(f"PRODUCTION target: {uri}")
    return GraphDatabase.driver(uri, auth=(user, password))


def wipe(driver) -> None:
    with driver.session() as session:
        n = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        print(f"  Wiping production ({n} nodes)...")
        # Batched delete keeps memory bounded on large graphs
        while True:
            deleted = session.run(
                "MATCH (n) WITH n LIMIT 20000 DETACH DELETE n RETURN count(n) AS c"
            ).single()["c"]
            if deleted == 0:
                break
    print("  Production wiped.")


def main() -> None:
    if "--yes" not in sys.argv:
        raise SystemExit("Refusing to wipe production without --yes")

    import sqlite3

    from config import SQLITE_PATH

    conn = sqlite3.connect(SQLITE_PATH)
    try:
        valid, m, _ = aggregate(conn, shared_lookups(conn))
    finally:
        conn.close()
    if not valid:
        raise SystemExit("No valid routes in the SQLite cache — aborting before wipe.")
    print(f"Aggregated {len(valid)} valid routes from {m['total']} raw flights.")

    driver = production_driver()
    driver.verify_connectivity()
    start = time.time()
    try:
        wipe(driver)
        print("[1/4] Constraints...")
        create_constraints(driver)
        print("[2/4] Airports (OurAirports)...")
        load_airports(driver)
        print("[3/4] Aircraft (planes.dat + normalize + supplement)...")
        load_aircraft(driver)
        normalize_aircraft(driver)
        n_supp = merge_supplement_aircraft(driver, {r["icaotype"] for r in valid})
        print(f"  +{n_supp} supplement Aircraft nodes")
        print("[4/4] Routes (OpenSky-derived)...")
        load_routes(driver, valid)

        with driver.session() as session:
            counts = {
                label: session.run(q).single()["c"]
                for label, q in [
                    ("Airports", "MATCH (a:Airport) RETURN count(a) AS c"),
                    ("Aircraft", "MATCH (ac:Aircraft) RETURN count(ac) AS c"),
                    ("Routes", "MATCH ()-[r:ROUTE]->() RETURN count(r) AS c"),
                ]
            }
        print(f"\nPromotion complete in {time.time() - start:.1f}s: {counts}")
        print("Restart the Go server to load the new graph.")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
