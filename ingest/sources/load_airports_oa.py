"""Load OurAirports → staging Neo4j :Airport nodes (same shape as production)."""
from neo4j import GraphDatabase

from airports_oa import parse_airports
from config import staging_neo4j_config

BATCH_SIZE = 1000


def load_airports(driver) -> int:
    airports = parse_airports()
    print(f"  Parsed {len(airports)} scheduled large/medium airports from OurAirports.")

    with driver.session() as session:
        for i in range(0, len(airports), BATCH_SIZE):
            session.execute_write(_merge_batch, airports[i : i + BATCH_SIZE])

    print(f"  Merged {len(airports)} Airport nodes into staging Neo4j.")
    return len(airports)


def _merge_batch(tx, batch: list) -> None:
    tx.run(
        """
        UNWIND $batch AS ap
        MERGE (a:Airport {iata: ap.iata})
        SET a.icao = ap.icao,
            a.name = ap.name,
            a.city = ap.city,
            a.country = ap.country,
            a.lat = ap.lat,
            a.lon = ap.lon,
            a.type = ap.type
        """,
        batch=batch,
    )


if __name__ == "__main__":
    cfg = staging_neo4j_config()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        load_airports(driver)
    finally:
        driver.close()
