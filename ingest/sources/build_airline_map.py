"""Build icao_airline_map.json — ICAO callsign prefix → IATA airline code.

Derived from OpenFlights airlines.dat (already on disk). No API involved:
the Aviationstack /airlines fallback is dead (403 on free tier, tested
2026-07-05). Unresolvable callsigns get airline='' downstream.

Format: id,name,alias,iata,icao,callsign,country,active
"""

import csv
import json
import os

from config import AIRLINE_MAP_JSON, DATA_DIR


def build_airline_map() -> dict:
    """Parse airlines.dat → {ICAO prefix: IATA code}, active carriers preferred."""
    mapping: dict = {}
    inactive: dict = {}
    with open(os.path.join(DATA_DIR, "airlines.dat"), "r", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 8:
                continue
            iata, icao, active = row[3].strip(), row[4].strip(), row[7].strip()
            if len(icao) != 3 or not icao.isalpha() or icao == "N/A":
                continue
            if len(iata) != 2 or iata in ("\\N", "-", ""):
                continue
            target = mapping if active == "Y" else inactive
            # First active carrier wins a contested prefix (dupes are rare, defunct)
            target.setdefault(icao.upper(), iata.upper())

    # Fill gaps with inactive carriers — a stale mapping beats none for display
    for icao, iata in inactive.items():
        mapping.setdefault(icao, iata)
    return mapping


def main() -> None:
    mapping = build_airline_map()
    with open(AIRLINE_MAP_JSON, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=1, sort_keys=True)
    print(f"  Wrote {len(mapping)} ICAO→IATA airline prefixes to {AIRLINE_MAP_JSON}")
    for probe in ("BAW", "UAE", "DLH", "AAL"):
        print(f"    {probe} → {mapping.get(probe)}")


if __name__ == "__main__":
    main()
