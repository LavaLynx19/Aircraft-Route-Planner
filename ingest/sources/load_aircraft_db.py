"""Load the Mictronics/tar1090-db aircraft database into SQLite.

Populates the aircraft_types table (icao24 → ICAO type designator) used by
load_graph.py to map OpenSky hex codes to aircraft. Source: wiedehopf's
tar1090-db single-CSV export (ODC-By, attribute Mictronics), replacing the
frozen ADS-B Exchange list (ARCHITECTURE.md §13.1).

CSV format (semicolon-separated, gzipped): icao24;registration;icaotype;dbFlags;...
"""
import csv
import gzip
import os
import sqlite3
import urllib.request

from config import AIRCRAFT_DB_CSV, AIRCRAFT_DB_URL, SQLITE_PATH


def download(refresh: bool = False) -> None:
    if os.path.exists(AIRCRAFT_DB_CSV) and not refresh:
        return
    print(f"  Downloading {AIRCRAFT_DB_URL} ...")
    req = urllib.request.Request(AIRCRAFT_DB_URL, headers={"User-Agent": "aircraft-route-planner"})
    with urllib.request.urlopen(req) as resp, open(AIRCRAFT_DB_CSV, "wb") as f:
        f.write(resp.read())


def load_aircraft_types(conn: sqlite3.Connection) -> int:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS aircraft_types (
               icao24 TEXT PRIMARY KEY,
               icaotype TEXT NOT NULL,
               registration TEXT
           )"""
    )
    rows = []
    with gzip.open(AIRCRAFT_DB_CSV, "rt", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f, delimiter=";"):
            if len(row) < 3:
                continue
            icao24, reg, icaotype = row[0].strip().lower(), row[1].strip(), row[2].strip()
            if len(icao24) != 6 or not icaotype:
                continue
            rows.append((icao24, icaotype.upper(), reg or None))

    conn.execute("DELETE FROM aircraft_types")
    conn.executemany(
        "INSERT OR REPLACE INTO aircraft_types (icao24, icaotype, registration) VALUES (?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def main(refresh: bool = False) -> None:
    download(refresh=refresh)
    conn = sqlite3.connect(SQLITE_PATH)
    try:
        n = load_aircraft_types(conn)
        print(f"  Loaded {n} icao24→type mappings into aircraft_types (SQLite).")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    main(refresh="--refresh" in sys.argv)
