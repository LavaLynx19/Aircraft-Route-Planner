import csv
import os

from config import neo4j_config
from neo4j import GraphDatabase

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
BATCH_SIZE = 1000


def parse_airports(filepath):
    """Parse airports.dat → list of airport dicts.

    Format: id,name,city,country,iata,icao,lat,lon,alt,tz_offset,dst,timezone,type,source
    \\N means null. Filter to type="airport" only.
    """
    airports = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 14:
                continue

            iata = row[4].strip()
            entry_type = row[12].strip()

            # Skip non-airport entries (train stations, ferry ports, etc.)
            if entry_type != "airport":
                continue
            # Skip entries with no IATA code
            if not iata or iata == "\\N":
                continue

            try:
                lat = float(row[6])
                lon = float(row[7])
            except (ValueError, IndexError):
                continue

            airports.append(
                {
                    "iata": iata,
                    "icao": row[5].strip() if row[5].strip() != "\\N" else None,
                    "name": row[1].strip() if row[1].strip() != "\\N" else None,
                    "city": row[2].strip() if row[2].strip() != "\\N" else None,
                    "country": row[3].strip() if row[3].strip() != "\\N" else None,
                    "lat": lat,
                    "lon": lon,
                }
            )
    return airports


def load_airports(driver):
    airports = parse_airports(os.path.join(DATA_DIR, "airports.dat"))
    print(f"  Parsed {len(airports)} airports with IATA codes.")

    with driver.session() as session:
        for i in range(0, len(airports), BATCH_SIZE):
            batch = airports[i : i + BATCH_SIZE]
            session.execute_write(_merge_batch, batch)

    print(f"  Merged {len(airports)} Airport nodes into Neo4j.")
    return len(airports)


def _merge_batch(tx, batch):
    tx.run(
        """
        UNWIND $batch AS ap
        MERGE (a:Airport {iata: ap.iata})
        SET a.icao = ap.icao,
            a.name = ap.name,
            a.city = ap.city,
            a.country = ap.country,
            a.lat = ap.lat,
            a.lon = ap.lon
        """,
        batch=batch,
    )


if __name__ == "__main__":
    cfg = neo4j_config()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        load_airports(driver)
    finally:
        driver.close()
