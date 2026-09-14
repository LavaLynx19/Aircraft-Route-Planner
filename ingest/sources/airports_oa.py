"""OurAirports parsing shared by the staging loader and the fetcher.

Filter (per ARCHITECTURE.md §13.1): type IN (large_airport, medium_airport)
AND scheduled_service = 'yes' → ~3,307 airports. Country names come from
countries.csv (iso_country is a 2-letter code; the Go engine indexes full names).
"""

from __future__ import annotations

import csv
import os
import shutil
import urllib.request
from collections import Counter

from config import (
    BLACKOUT_ICAO_PREFIXES,
    COUNTRIES_CSV,
    COUNTRIES_URL,
    DATA_DIR,
    OURAIRPORTS_CSV,
    OURAIRPORTS_URL,
)

FETCH_TYPES = ("large_airport", "medium_airport")


DOWNLOAD_TIMEOUT_SECONDS = 60


def _download_if_missing(path: str, url: str) -> None:
    """Fetch `url` to `path` if absent.

    Not urlretrieve: it takes no timeout, so a hung server blocks a launchd run
    indefinitely and the run guard never regains control to report it (same
    defect class as the OAuth token request fixed 2026-09-05).

    Downloads to a .part file and renames on success, because the only freshness
    check is os.path.exists — a truncated file from an interrupted download would
    otherwise be treated as complete on every later run.
    """
    if os.path.exists(path):
        return
    print(f"  Downloading {url} ...")
    tmp = f"{path}.part"
    with (
        urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp,
        open(tmp, "wb") as f,
    ):
        shutil.copyfileobj(resp, f)
    os.replace(tmp, path)


def country_names() -> dict:
    """countries.csv → {iso code: full name}."""
    _download_if_missing(COUNTRIES_CSV, COUNTRIES_URL)
    with open(COUNTRIES_CSV, "r", encoding="utf-8") as f:
        return {r["code"]: r["name"] for r in csv.DictReader(f)}


def parse_airports() -> list:
    """OurAirports CSV → filtered airport dicts (iata, icao, name, city, country, lat, lon, type)."""
    _download_if_missing(OURAIRPORTS_CSV, OURAIRPORTS_URL)
    countries = country_names()
    airports = []
    with open(OURAIRPORTS_CSV, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["type"] not in FETCH_TYPES or r["scheduled_service"] != "yes":
                continue
            iata = r["iata_code"].strip()
            icao = r["icao_code"].strip() or r["ident"].strip()
            if not iata:
                continue  # graph keys airports on IATA
            if len(icao) != 4 or not icao.isalnum():
                icao = None
            try:
                lat, lon = float(r["latitude_deg"]), float(r["longitude_deg"])
            except ValueError:
                continue
            airports.append(
                {
                    "iata": iata,
                    "icao": icao,
                    "name": r["name"].strip() or None,
                    "city": r["municipality"].strip() or None,
                    "country": countries.get(r["iso_country"], r["iso_country"]),
                    "lat": lat,
                    "lon": lon,
                    "type": r["type"],
                }
            )
    return airports


def _route_counts() -> Counter:
    """Departure counts per airport IATA from OpenFlights routes.dat.

    2014 data is stale for routes but serves as a stable relative-traffic
    ranking — without it, an alphabetical cutoff at the fetch limit would
    drop major hubs whose ICAO starts late in the alphabet (VHHH, ZBAA, YSSY).
    """
    counts: Counter = Counter()
    path = os.path.join(DATA_DIR, "routes.dat")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) > 2 and row[2] not in ("\\N", ""):
                    counts[row[2]] += 1
    return counts


def blackout_list() -> list:
    """Airports in the measured ADS-B blackout regions, busiest first.

    OpenSky returns empty for ~89% of these (China) and 9.5 flights per success
    (Russia/CIS) because its volunteer receiver network has no coverage there —
    a gap no amount of budget or accumulation closes. These are fetched from
    AeroDataBox schedules instead (ARCHITECTURE.md §4.1.5). Not capped: the whole
    set is small enough that the monthly unit budget, not the list, is the limit.
    """
    traffic = _route_counts()
    return sorted(
        (
            a
            for a in parse_airports()
            if a["icao"] and a["icao"].startswith(BLACKOUT_ICAO_PREFIXES)
        ),
        key=lambda a: (-traffic[a["iata"]], a["icao"]),
    )


def fetch_list(limit: int | None = None) -> list:
    """Priority-ordered OpenSky fetch targets (must have ICAO — OpenSky keys on it).

    Excludes the ADS-B blackout regions, which are fetched from schedules instead
    (see blackout_list and ARCHITECTURE.md §4.1.5) — querying them here spent
    ~3,810 credits per cycle to return empty. large_airport before medium; within
    type, busiest first (routes.dat proxy), then alphabetical for determinism.

    `limit=None` returns every viable airport: under the tiered strategy the
    budget is applied per tier at fetch time, not by truncating this list.
    """
    traffic = _route_counts()
    ranked = sorted(
        (
            a
            for a in parse_airports()
            if a["icao"] and not a["icao"].startswith(BLACKOUT_ICAO_PREFIXES)
        ),
        key=lambda a: (FETCH_TYPES.index(a["type"]), -traffic[a["iata"]], a["icao"]),
    )
    return ranked if limit is None else ranked[:limit]
