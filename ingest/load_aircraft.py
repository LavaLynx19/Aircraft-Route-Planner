import csv
import os
from neo4j import GraphDatabase
from config import neo4j_config

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
BATCH_SIZE = 500


def parse_planes(filepath):
    """Parse planes.dat → list of aircraft dicts.

    Format: "name","iata","icao" (no header row).
    \\N means null.
    """
    aircraft = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 3:
                continue
            name, iata, icao = row[0].strip(), row[1].strip(), row[2].strip()
            # Skip entries with no IATA code
            if not iata or iata == "\\N":
                continue
            aircraft.append({
                "iata": iata,
                "icao": icao if icao != "\\N" else None,
                "name": name if name != "\\N" else None,
            })
    return aircraft


def load_aircraft(driver):
    aircraft = parse_planes(os.path.join(DATA_DIR, "planes.dat"))
    print(f"  Parsed {len(aircraft)} aircraft with IATA codes.")

    with driver.session() as session:
        for i in range(0, len(aircraft), BATCH_SIZE):
            batch = aircraft[i : i + BATCH_SIZE]
            session.execute_write(_merge_batch, batch)

    print(f"  Merged {len(aircraft)} Aircraft nodes into Neo4j.")
    return len(aircraft)


def _merge_batch(tx, batch):
    tx.run(
        """
        UNWIND $batch AS ac
        MERGE (a:Aircraft {iata: ac.iata})
        SET a.icao = ac.icao,
            a.name = ac.name
        """,
        batch=batch,
    )


if __name__ == "__main__":
    cfg = neo4j_config()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        load_aircraft(driver)
    finally:
        driver.close()
