import csv
import math
import os

from config import neo4j_config
from neo4j import GraphDatabase

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
BATCH_SIZE = 5000


def haversine(lat1, lon1, lat2, lon2):
    """Compute great-circle distance in km between two points."""
    R = 6371.0  # Earth radius in km
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def parse_routes(filepath, airport_coords):
    """Parse routes.dat → list of route dicts.

    Format: airline,airline_id,src_airport,src_id,dst_airport,dst_id,codeshare,stops,equipment
    Equipment field is space-separated list of aircraft IATA codes.
    One ROUTE per aircraft-route pair (denormalized per ARCHITECTURE Section 2.2).
    """
    routes = []
    skipped = 0
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 9:
                continue

            airline = row[0].strip()
            src = row[2].strip()
            dst = row[4].strip()
            equipment = row[8].strip()

            if not src or src == "\\N" or not dst or dst == "\\N":
                skipped += 1
                continue
            if not equipment or equipment == "\\N":
                skipped += 1
                continue

            # Need coordinates for both airports to compute distance
            if src not in airport_coords or dst not in airport_coords:
                skipped += 1
                continue

            src_lat, src_lon = airport_coords[src]
            dst_lat, dst_lon = airport_coords[dst]
            distance_km = round(haversine(src_lat, src_lon, dst_lat, dst_lon), 1)

            # Split equipment field → one route per aircraft code
            for ac_code in equipment.split(" "):
                ac_code = ac_code.strip()
                if not ac_code:
                    continue
                routes.append(
                    {
                        "src": src,
                        "dst": dst,
                        "aircraft": ac_code,
                        # '' not None: airline is part of the MERGE pattern below and
                        # Neo4j rejects MERGE on a null property, which would abort the
                        # whole batch on a single bad row. Matches the sentinel the
                        # OpenSky loader already uses (ingest/sources/load_graph.py).
                        "airline": airline if airline != "\\N" else "",
                        "distance_km": distance_km,
                    }
                )

    print(f"  Skipped {skipped} routes (missing airports or equipment).")
    return routes


def load_airport_coords(driver):
    """Fetch airport coordinates from Neo4j for Haversine computation."""
    coords = {}
    with driver.session() as session:
        result = session.run(
            "MATCH (a:Airport) RETURN a.iata AS iata, a.lat AS lat, a.lon AS lon"
        )
        for record in result:
            coords[record["iata"]] = (record["lat"], record["lon"])
    return coords


def load_routes(driver):
    # Get airport coords for distance calculation
    airport_coords = load_airport_coords(driver)
    print(f"  Loaded coordinates for {len(airport_coords)} airports.")

    routes = parse_routes(os.path.join(DATA_DIR, "routes.dat"), airport_coords)
    print(f"  Parsed {len(routes)} route-aircraft pairs.")

    with driver.session() as session:
        for i in range(0, len(routes), BATCH_SIZE):
            batch = routes[i : i + BATCH_SIZE]
            session.execute_write(_merge_batch, batch)
            if (i // BATCH_SIZE) % 5 == 0:
                print(
                    f"  ... merged {min(i + BATCH_SIZE, len(routes))}/{len(routes)} routes"
                )

    print(f"  Merged {len(routes)} ROUTE relationships into Neo4j.")
    return len(routes)


def _merge_batch(tx, batch):
    tx.run(
        """
        UNWIND $batch AS r
        MATCH (src:Airport {iata: r.src})
        MATCH (dst:Airport {iata: r.dst})
        MERGE (src)-[route:ROUTE {aircraft: r.aircraft, airline: r.airline}]->(dst)
        SET route.distance_km = r.distance_km
        """,
        batch=batch,
    )


if __name__ == "__main__":
    cfg = neo4j_config()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        load_routes(driver)
    finally:
        driver.close()
