import json
import os
import re

from config import neo4j_config
from neo4j import GraphDatabase

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ALIASES_FILE = os.path.join(DATA_DIR, "aircraft_aliases.json")
BATCH_SIZE = 500


def extract_family(name, icao):
    """Extract aircraft family from name string.

    Rules per ARCHITECTURE Section 4.2:
    - Airbus: "Airbus A350-900" → "A350"
    - Boeing: "Boeing 777-300ER" → "777"
    - Embraer: "Embraer 190" → "E190"
    - Other: use ICAO code as fallback
    """
    if not name:
        return icao

    # Airbus: match A followed by digits, ignore variant suffix
    m = re.search(r"\b(A\d{3})", name)
    if m and ("Airbus" in name or "A3" in name or "A2" in name):
        return m.group(1)

    # Boeing: match 3-digit model number
    m = re.search(r"\b(7[0-9]{2})", name)
    if m and "Boeing" in name:
        return m.group(1)

    # Embraer: "Embraer 190" → "E190", "Embraer ERJ-145" → "ERJ"
    if "Embraer" in name:
        m = re.search(r"\b(\d{3})\b", name)
        if m:
            return f"E{m.group(1)}"
        m = re.search(r"\b(ERJ)\b", name)
        if m:
            return "ERJ"

    # Bombardier CRJ
    m = re.search(r"\b(CRJ)", name)
    if m:
        return "CRJ"

    # ATR
    m = re.search(r"\b(ATR)\b", name)
    if m:
        return "ATR"

    # Fallback to ICAO
    return icao


def build_aliases(iata, icao, name):
    """Build list of all known identifiers for an aircraft."""
    aliases = set()
    if iata:
        aliases.add(iata)
    if icao:
        aliases.add(icao)
    if name:
        # Add name without manufacturer prefix
        for prefix in (
            "Airbus ",
            "Boeing ",
            "Embraer ",
            "Bombardier ",
            "Aerospatiale/Alenia ",
            "De Havilland ",
            "BAe ",
            "McDonnell Douglas ",
            "Fokker ",
            "Saab ",
        ):
            if name.startswith(prefix):
                stripped = name[len(prefix) :]
                aliases.add(stripped)
                break
    return list(aliases)


def normalize_aircraft(driver):
    # Load optional overrides
    overrides = {}
    if os.path.exists(ALIASES_FILE):
        with open(ALIASES_FILE, "r") as f:
            overrides = json.load(f)
        print(f"  Loaded {len(overrides)} alias overrides.")

    # Fetch all aircraft from Neo4j
    with driver.session() as session:
        result = session.run(
            "MATCH (ac:Aircraft) RETURN ac.iata AS iata, ac.icao AS icao, ac.name AS name"
        )
        aircraft = [record.data() for record in result]

    print(f"  Normalizing {len(aircraft)} aircraft...")

    updates = []
    for ac in aircraft:
        iata = ac["iata"]
        icao = ac["icao"]
        name = ac["name"]

        family = extract_family(name, icao)
        aliases = build_aliases(iata, icao, name)

        # Apply overrides
        if iata in overrides:
            ov = overrides[iata]
            if "family" in ov:
                family = ov["family"]
            if "aliases" in ov:
                aliases = list(set(aliases + ov["aliases"]))

        updates.append(
            {
                "iata": iata,
                "family": family,
                "aliases": aliases,
            }
        )

    # Batch update
    with driver.session() as session:
        for i in range(0, len(updates), BATCH_SIZE):
            batch = updates[i : i + BATCH_SIZE]
            session.execute_write(_update_batch, batch)

    print(f"  Updated {len(updates)} Aircraft nodes with family + aliases.")
    return len(updates)


def _update_batch(tx, batch):
    tx.run(
        """
        UNWIND $batch AS ac
        MATCH (a:Aircraft {iata: ac.iata})
        SET a.family = ac.family,
            a.aliases = ac.aliases
        """,
        batch=batch,
    )


if __name__ == "__main__":
    cfg = neo4j_config()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        normalize_aircraft(driver)
    finally:
        driver.close()
