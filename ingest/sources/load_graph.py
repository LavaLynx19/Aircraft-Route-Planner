"""Script 2 — offline graph loader: SQLite raw_flights → staging Neo4j ROUTE edges.

Pipeline (ARCHITECTURE.md §4.1.1–§4.1.4): destination inference → aggregation →
validity filter → icao24→aircraft mapping → airline resolution → Neo4j load.
Prints the pipeline metrics. Run any time; re-runs are idempotent (MERGE).

Airline is a MERGE key — null would abort the batch (Neo4j rejects MERGE on
null property), so unresolved airlines use '' as the stored sentinel.
"""

import csv
import json
import math
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

from airports_oa import parse_airports
from config import (
    AIRLINE_MAP_JSON,
    BLACKOUT_ICAO_PREFIXES,
    DATA_DIR,
    SQLITE_PATH,
    staging_neo4j_config,
)
from neo4j import GraphDatabase
from runner import run_guard

CONFIDENCE_PCT = 80.0  # destination inference threshold (§4.1.2)
MIN_WEEKS = 3  # validity: standard threshold (§4.1.3)
RECENCY_DAYS = 14  # validity: recency exception floor (§4.1.3)
BATCH_SIZE = 1000
SCHEDULE_WINDOW_DAYS = 0.5  # AeroDataBox visit covers a 12h window (§4.1.5)

# Ordering gate for the refresh chain (§4.1.5 *Refresh chain*). launchd schedules on
# wall-clock only and has no dependency graph, so the rebuild cannot assume the
# 09:00 and 10:00 fetchers have finished — on wake from sleep all three agents fire
# at once, in no guaranteed order. The loader therefore reads the fetchers' own logs
# rather than trusting the gap between scheduled times.
UPSTREAM_QUIET_MINUTES = 5  # a log written more recently than this may still be mid-run
UPSTREAM_STALE_HOURS = 36  # beyond this the collectors have stopped, not merely lagged

# Recency fallback for airports with <2 successful fetches: twice the tier's
# nominal cycle (hub 7d, tail 30.7d, schedules 23.3d).
TIER_FALLBACK_DAYS = {"hub": 14.0, "tail": 61.0, "schedules": 47.0}

# AeroDataBox model strings absent from planes.dat's 2014 name list. Generic
# strings ("Boeing 737", "Airbus A330") are deliberately NOT mapped: picking a
# variant would fabricate precision the source never provided, so they stay
# unmapped and get counted.
MODEL_ALIASES = {
    "airbus a319 neo": "A19N",
    "airbus a320 neo": "A20N",
    "airbus a321 neo": "A21N",
    "airbus a320-200": "A320",
    "airbus a321-200": "A321",
    "airbus a321-200 (sharklets)": "A321",
    "bombardier crj700": "CRJ7",
    "bombardier crj900": "CRJ9",
    "embraer 175": "E175",
    "sukhoi superjet 100": "SU95",
}

# planes.dat is 2014-era and lacks post-2015 type designators. Without this
# supplement, most current narrowbody traffic (neo/MAX/A220) would be dropped.
# IATA equipment codes per IATA standard. Integration point: aircraft_aliases.json.
SUPPLEMENT_TYPES = {
    "AT75": {"iata": "AT7", "name": "ATR 72-500", "family": "AT7"},
    "AT76": {"iata": "AT7", "name": "ATR 72-600", "family": "AT7"},
    "E45X": {"iata": "ER4", "name": "Embraer ERJ-145XR", "family": "ERJ"},
    "A19N": {"iata": "31N", "name": "Airbus A319neo", "family": "A320"},
    "A20N": {"iata": "32N", "name": "Airbus A320neo", "family": "A320"},
    "A21N": {"iata": "32Q", "name": "Airbus A321neo", "family": "A320"},
    "A339": {"iata": "339", "name": "Airbus A330-900neo", "family": "A330"},
    "A35K": {"iata": "351", "name": "Airbus A350-1000", "family": "A350"},
    "B37M": {"iata": "7M7", "name": "Boeing 737 MAX 7", "family": "737"},
    "B38M": {"iata": "7M8", "name": "Boeing 737 MAX 8", "family": "737"},
    "B39M": {"iata": "7M9", "name": "Boeing 737 MAX 9", "family": "737"},
    "B3XM": {"iata": "7MJ", "name": "Boeing 737 MAX 10", "family": "737"},
    "B78X": {"iata": "78J", "name": "Boeing 787-10", "family": "787"},
    "BCS1": {"iata": "221", "name": "Airbus A220-100", "family": "A220"},
    "BCS3": {"iata": "223", "name": "Airbus A220-300", "family": "A220"},
    "E195": {"iata": "E95", "name": "Embraer 195", "family": "E190"},
    "E290": {"iata": "290", "name": "Embraer E190-E2", "family": "E190"},
    "E295": {"iata": "295", "name": "Embraer E195-E2", "family": "E190"},
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def type_to_iata() -> dict:
    """ICAO type designator → IATA equipment code (planes.dat + supplement + overrides)."""
    mapping = {}
    with open(os.path.join(DATA_DIR, "planes.dat"), "r", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            iata, icao = row[1].strip(), row[2].strip()
            if iata not in ("\\N", "") and icao not in ("\\N", ""):
                mapping[icao.upper()] = iata
    for icao, meta in SUPPLEMENT_TYPES.items():
        mapping.setdefault(icao, meta["iata"])
    overrides = os.path.join(DATA_DIR, "aircraft_aliases.json")
    if os.path.exists(overrides):
        with open(overrides, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str):
                    mapping[k.upper()] = v
    return mapping


def model_to_icaotype() -> dict:
    """Aircraft model name → ICAO type designator.

    Fallback for the ~28% of schedule rows that carry no Mode-S address.
    planes.dat stores a name alongside each code, so the file already used for
    type→IATA doubles as a name index; SUPPLEMENT_TYPES adds the post-2014
    designators and MODEL_ALIASES the spellings this source uses.
    """
    mapping = {}
    with open(os.path.join(DATA_DIR, "planes.dat"), "r", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            name, icao = row[0].strip(), row[2].strip()
            if name and icao not in ("\\N", ""):
                mapping[name.lower()] = icao.upper()
    for icao, meta in SUPPLEMENT_TYPES.items():
        mapping.setdefault(meta["name"].lower(), icao)
    mapping.update(MODEL_ALIASES)
    return mapping


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def recency_days_by_airport(conn: sqlite3.Connection) -> dict:
    """Per-airport recency window: 2x the observed resample interval, min 14 days.

    RECENCY_DAYS was one constant calibrated to the old single 7.4-day cycle.
    Tiering introduced three cadences (hub 7d, tail ~30.7d, schedules ~23.3d) and
    no single constant serves them: a tail airport would always fall outside a
    14-day window and drop to the >=3-distinct-weeks branch, which at a 30.7-day
    cycle needs 92+ days per airport — the graph would go sparse and stay sparse.
    Deriving the window from each airport's own fetch history self-calibrates.
    """
    stamps = defaultdict(list)
    for icao, ts in conn.execute(
        "SELECT airport_icao, window_begin FROM fetch_log WHERE status = 'ok'"
    ):
        stamps[icao].append(ts / 86400.0)
    if _table_exists(conn, "schedules_fetch_log"):
        for icao, iso in conn.execute(
            "SELECT airport_icao, fetched_at FROM schedules_fetch_log "
            "WHERE status = 'ok'"
        ):
            try:
                stamps[icao].append(datetime.fromisoformat(iso).timestamp() / 86400.0)
            except ValueError:
                continue

    out = {}
    for icao, days in stamps.items():
        if len(days) < 2:
            continue
        days.sort()
        gaps = sorted(b - a for a, b in zip(days, days[1:]))
        mid = len(gaps) // 2
        median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
        out[icao] = max(RECENCY_DAYS, 2.0 * median)

    for icao, tier in conn.execute("SELECT icao, tier FROM fetch_airports"):
        out.setdefault(icao, TIER_FALLBACK_DAYS.get(tier, RECENCY_DAYS))
    if _table_exists(conn, "schedules_airports"):
        for (icao,) in conn.execute("SELECT icao FROM schedules_airports"):
            out.setdefault(icao, TIER_FALLBACK_DAYS["schedules"])
    return out


def build_callsign_patterns(conn: sqlite3.Connection) -> dict:
    """(callsign, origin) → destination, from OBSERVED rows only (never inferred —
    feeding inferred rows back in would create a self-reinforcing loop)."""
    rows = conn.execute(
        """
        SELECT callsign, origin, destination, COUNT(*) AS obs
        FROM raw_flights
        WHERE destination IS NOT NULL AND callsign IS NOT NULL
        GROUP BY callsign, origin, destination
        """
    ).fetchall()
    by_pair = defaultdict(list)
    for callsign, origin, dest, obs in rows:
        by_pair[(callsign, origin)].append((dest, obs))
    patterns = {}
    for pair, dests in by_pair.items():
        total = sum(o for _, o in dests)
        best_dest, best_obs = max(dests, key=lambda d: d[1])
        if best_obs * 100.0 / total >= CONFIDENCE_PCT:
            patterns[pair] = best_dest
    return patterns


def sampled_days_by_origin(conn: sqlite3.Connection) -> dict:
    """Observed days per origin airport (denominator for frequency extrapolation).

    Schedule origins are measured in the same unit but sample only a 12h window
    per visit, so each counts as half a day. Reusing the ADS-B assumption of a
    full day here would halve every schedule-sourced weekly_frequency — silently,
    since nothing else would look wrong.
    """
    rows = conn.execute(
        """
        SELECT airport_icao,
               SUM((window_end - window_begin + 1) / 86400.0)
        FROM fetch_log WHERE status IN ('ok', 'empty')
        GROUP BY airport_icao
        """
    ).fetchall()
    out = {r[0]: max(r[1], 1.0) for r in rows}
    if _table_exists(conn, "schedules_fetch_log"):
        for icao, visits in conn.execute(
            "SELECT airport_icao, count(*) FROM schedules_fetch_log "
            "WHERE status IN ('ok', 'empty') GROUP BY airport_icao"
        ):
            # Floor at one window, not one day: a single visit really is 12h.
            out[icao] = max(visits * SCHEDULE_WINDOW_DAYS, SCHEDULE_WINDOW_DAYS)
    return out


def extract_airline(callsign: str, airline_map: dict) -> str:
    prefix = callsign[:3] if callsign and len(callsign) >= 3 else ""
    if prefix.isalpha():
        return airline_map.get(prefix.upper(), "")
    return ""


def shared_lookups(conn: sqlite3.Connection) -> tuple:
    """Build the three lookups both aggregators need, once (Sep review M5).

    `parse_airports()` parses an 85,696-row CSV and the aircraft query materialises
    496,832 rows; both ran once for `aggregate()` and again for
    `aggregate_schedules()`. The cost that matters is not time but the loader's
    memory high-water mark, which `aggregate()` already stresses by holding every
    route's state at once.

    Hoisted into the caller rather than memoised with `lru_cache`: a cache would
    keep these alive for the whole process, which makes the high-water mark worse
    rather than better, and hides object lifetime.

    icao24 keys are lowercased. Both tables store lowercase today (verified
    2026-09-13: 0 rows in either differ from `lower()`), so the two callers were
    already building identical dicts — one with `.lower()`, one without. Keeping the
    stricter form means the shared dict cannot be weaker than either original.
    """
    ac_map = type_to_iata()
    icao24_type = {
        k.lower(): v
        for k, v in conn.execute("SELECT icao24, icaotype FROM aircraft_types")
    }
    airports = {a["icao"]: a for a in parse_airports() if a["icao"]}
    return ac_map, icao24_type, airports


def aggregate(conn: sqlite3.Connection, lookups: tuple) -> tuple:
    """raw_flights → route dicts + metrics. `lookups` from shared_lookups()."""
    with open(AIRLINE_MAP_JSON, "r", encoding="utf-8") as f:
        airline_map = json.load(f)
    ac_map, icao24_type, airports = lookups
    patterns = build_callsign_patterns(conn)

    m = defaultdict(int)
    unmapped_types = defaultdict(int)
    routes = {}  # (src_iata, dst_iata, ac_iata, airline) → stats
    for icao24, callsign, origin, dest, first_seen, last_seen in conn.execute(
        "SELECT icao24, callsign, origin, destination, first_seen, last_seen FROM raw_flights"
    ):
        m["total"] += 1
        # Blackout-region origins belong to the schedules source (§4.1.5). They
        # were in the OpenSky queue until 2026-09-05, so raw_flights still holds
        # legacy rows for them; without this the same route is produced by both
        # sources and one silently overwrites the other's metrics on MERGE.
        if origin.startswith(BLACKOUT_ICAO_PREFIXES):
            m["dropped_blackout_origin"] += 1
            continue
        if dest is None:
            m["null_dest"] += 1
            dest = patterns.get((callsign, origin))
            if dest is None:
                m["dropped_no_dest"] += 1
                continue
            m["inferred"] += 1
        if dest == origin:
            m["dropped_same_airport"] += 1
            continue
        src_ap, dst_ap = airports.get(origin), airports.get(dest)
        if not src_ap or not dst_ap:
            m["dropped_unknown_airport"] += 1
            continue
        icaotype = icao24_type.get(icao24.lower()) if icao24 else None
        ac_iata = ac_map.get(icaotype) if icaotype else None
        if not ac_iata:
            m["dropped_unmapped_aircraft"] += 1
            if icaotype:
                unmapped_types[icaotype] += 1
            continue
        airline = extract_airline(callsign or "", airline_map)
        if not airline:
            m["airline_unresolved"] += 1

        dt = datetime.fromtimestamp(first_seen, tz=timezone.utc)
        key = (src_ap["iata"], dst_ap["iata"], ac_iata, airline)
        r = routes.get(key)
        if r is None:
            r = routes[key] = {
                "flights": 0,
                "weeks": set(),
                "days": set(),
                "last_seen": 0,
                "src": src_ap,
                "dst": dst_ap,
                "origin_icao": origin,
                "icaotype": icaotype,
            }
        r["flights"] += 1
        r["weeks"].add(dt.strftime("%G-%V"))
        r["days"].add(dt.isoweekday())
        r["last_seen"] = max(r["last_seen"], first_seen)

    m["routes_total"] = len(routes)
    return routes, m, unmapped_types


def shape_valid(
    routes: dict, recency: dict, sampled: dict, source: str, m: dict
) -> list:
    """Apply the validity filter and shape surviving routes for Neo4j.

    One definition of the rule for both sources — duplicating it per source is
    how the two would silently drift apart.
    """
    now = time.time()
    valid = []
    for (src, dst, ac, airline), r in routes.items():
        window = recency.get(r["origin_icao"], RECENCY_DAYS)
        recent = r["last_seen"] >= now - window * 86400
        if len(r["weeks"]) >= MIN_WEEKS or (len(r["weeks"]) >= 1 and recent):
            days_sampled = sampled.get(r["origin_icao"], 2.0)
            valid.append(
                {
                    "src": src,
                    "dst": dst,
                    "aircraft": ac,
                    "airline": airline,
                    "distance_km": round(
                        haversine_km(
                            r["src"]["lat"],
                            r["src"]["lon"],
                            r["dst"]["lat"],
                            r["dst"]["lon"],
                        ),
                        1,
                    ),
                    "weekly_frequency": max(1, round(r["flights"] / days_sampled * 7)),
                    "days_of_week": sorted(r["days"]),
                    "icaotype": r["icaotype"],
                    "source": source,
                    # The sample behind weekly_frequency. A 12h schedule visit gives
                    # days_sampled=0.5, where the smallest expressible rate is 14/wk —
                    # without these a coarse 14 is indistinguishable from a measured one.
                    "observations": r["flights"],
                    "days_sampled": round(days_sampled, 2),
                }
            )
        else:
            m[f"{source}_filtered_out"] += 1
    return valid


def _parse_scheduled(value: str):
    """AeroDataBox scheduled time → aware datetime, or None if unparseable.

    Accepts a range of ISO-8601 shapes rather than one exact format. Until
    2026-09-13 this was a single `strptime(value, "%Y-%m-%d %H:%MZ")`. Every one of
    the 6,426 stored rows carries that shape today, so nothing was broken — but if
    the API ever emitted seconds, a `T` separator or a numeric offset, *every*
    schedule row would have failed to parse and the entire AeroDataBox
    contribution would have silently vanished from the graph, signalled only by a
    counter in output nobody reads on a run that is now unattended (Sep review M2).

    Python 3.9's `fromisoformat` rejects a trailing `Z`, so it is normalised to
    `+00:00` first, and a space separator to `T`. A naive result is assumed UTC,
    which is what the API documents — the field is named `scheduled_utc`.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def aggregate_schedules(conn: sqlite3.Connection, m: dict, lookups: tuple) -> tuple:
    """raw_schedules → route dicts (ARCHITECTURE.md §4.1.5).

    Neither destination inference (§4.1.2) nor callsign→airline resolution
    (§4.1.4) runs here: this source resolves both itself, so inferring would
    only add error.
    """
    if not _table_exists(conn, "raw_schedules"):
        return {}, defaultdict(int)
    ac_map, icao24_type, airports = lookups
    model_map = model_to_icaotype()
    by_iata = {a["iata"]: a for a in airports.values()}

    unmapped_models = defaultdict(int)
    routes = {}

    # One physical departure must count once, even when the API revised its time.
    #
    # raw_schedules is deduplicated by UNIQUE(origin_icao, flight_number,
    # scheduled_utc), so a revised scheduledTime for a flight already stored has a
    # different tuple and inserts a *second* row for the same departure. Each
    # spurious row incremented `flights`, hence `observations` and
    # `weekly_frequency` — making observations untrustworthy as a confidence
    # signal, which was the whole reason for storing it (Sep review M3).
    #
    # Deduped here rather than by changing the UNIQUE constraint: SQLite cannot
    # alter one in place, a rebuild would not clean rows already stored, and
    # raw_schedules is more useful as a faithful record of what the API said —
    # revisions stay recoverable. Measured 2026-09-13: 5 surplus rows of 6,426.
    #
    # The date is taken as the leading 10 characters, which holds for every shape
    # _parse_scheduled accepts (all ISO-8601, date first).
    seen_departures = set()

    for (
        origin,
        dest_iata,
        dest_icao,
        airline,
        mode_s,
        model,
        scheduled,
        flight_no,
        is_cargo,
    ) in conn.execute(
        "SELECT origin_icao, dest_iata, dest_icao, airline_iata, mode_s, "
        "aircraft_model, scheduled_utc, flight_number, is_cargo FROM raw_schedules"
    ):
        m["sched_total"] += 1

        # A route planner must not offer a leg no passenger can book (Sep review
        # L4, ARCHITECTURE.md §4.1.5). Only this source can tell: OpenSky has no
        # cargo flag, so ADS-B freighter edges remain — an accepted inconsistency.
        if is_cargo:
            m["sched_dropped_cargo"] += 1
            continue

        departure = (origin, flight_no, (scheduled or "")[:10])
        if departure in seen_departures:
            m["sched_duplicate_same_day"] += 1
            continue
        seen_departures.add(departure)
        src_ap = airports.get(origin)
        dst_ap = airports.get(dest_icao) if dest_icao else None
        if dst_ap is None and dest_iata:
            dst_ap = by_iata.get(dest_iata)
        if not src_ap or not dst_ap:
            m["sched_dropped_unknown_airport"] += 1
            continue
        if src_ap["iata"] == dst_ap["iata"]:
            m["sched_dropped_same_airport"] += 1
            continue

        icaotype = icao24_type.get(mode_s.lower()) if mode_s else None
        if icaotype:
            m["sched_type_from_modes"] += 1
        elif model:
            # Mode-S wins: the registry is authoritative, while the model string
            # is looser and sometimes stale (observed "Boeing 737-200" on a
            # Chinese domestic route, and "Airbus A320" where Mode-S said A21N).
            icaotype = model_map.get(model.strip().lower())
            if icaotype:
                m["sched_type_from_model"] += 1
        ac_iata = ac_map.get(icaotype) if icaotype else None
        if not ac_iata:
            m["sched_dropped_unmapped_aircraft"] += 1
            if model:
                unmapped_models[model] += 1
            continue

        dt = _parse_scheduled(scheduled)
        if dt is None:
            m["sched_dropped_bad_time"] += 1
            continue
        if not airline:
            m["sched_airline_unresolved"] += 1

        key = (src_ap["iata"], dst_ap["iata"], ac_iata, airline or "")
        r = routes.get(key)
        if r is None:
            r = routes[key] = {
                "flights": 0,
                "weeks": set(),
                "days": set(),
                "last_seen": 0,
                "src": src_ap,
                "dst": dst_ap,
                "origin_icao": origin,
                "icaotype": icaotype,
            }
        r["flights"] += 1
        r["weeks"].add(dt.strftime("%G-%V"))
        r["days"].add(dt.isoweekday())
        r["last_seen"] = max(r["last_seen"], dt.timestamp())
    return routes, unmapped_models


def load_routes(driver, valid: list) -> None:
    with driver.session() as session:
        for i in range(0, len(valid), BATCH_SIZE):
            session.execute_write(_merge_route_batch, valid[i : i + BATCH_SIZE])


def _merge_route_batch(tx, batch: list) -> None:
    tx.run(
        """
        UNWIND $batch AS r
        MATCH (src:Airport {iata: r.src})
        MATCH (dst:Airport {iata: r.dst})
        MERGE (src)-[route:ROUTE {aircraft: r.aircraft, airline: r.airline}]->(dst)
        SET route.distance_km = r.distance_km,
            route.weekly_frequency = r.weekly_frequency,
            route.days_of_week = r.days_of_week,
            route.source = r.source,
            route.observations = r.observations,
            route.days_sampled = r.days_sampled
        """,
        batch=batch,
    )


def mark_stale(driver, valid: list) -> tuple:
    """Stamp `stale` on ROUTE edges absent from the valid set; clear it on present ones.

    The loader MERGEs and never deletes, so without this the validity filter
    (Section 4.1.3) only governs edge *creation* and has no authority over what is
    already in the graph — measured 2026-09-05, 25,314 rejected edges were still
    being served. Marking is deliberately separate from deletion (Section 4.1.6):
    this stays an idempotent write, and a separate sweeper removes the marked.

    Clearing on re-observation matters: a route that goes quiet for a cycle and
    returns is un-marked in place, keeping its identity rather than being deleted
    and recreated.
    """
    keys = [
        {
            "src": r["src"],
            "dst": r["dst"],
            "aircraft": r["aircraft"],
            "airline": r["airline"],
        }
        for r in valid
    ]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _reconcile(tx) -> tuple:
        """Mark the complement of the valid set, in ONE transaction.

        Atomicity is the point. This previously marked everything in one
        transaction and cleared the valid edges in ~20 more, so for the duration
        of the load *every* ROUTE edge was marked — and since
        internal/graph/loader.go filters `WHERE r.stale IS NULL`, a server
        starting in that window loaded a graph with zero routes and served empty
        results with no error. Inside a single transaction a reader sees either
        the before- or the after-state, never the intermediate one.
        """
        tx.run(
            "MATCH ()-[r:ROUTE]->() WHERE r.stale IS NULL SET r.stale = $now",
            now=now,
        )
        for i in range(0, len(keys), BATCH_SIZE):
            tx.run(
                """
                UNWIND $batch AS k
                MATCH (:Airport {iata: k.src})
                      -[r:ROUTE {aircraft: k.aircraft, airline: k.airline}]->
                      (:Airport {iata: k.dst})
                REMOVE r.stale
                """,
                batch=keys[i : i + BATCH_SIZE],
            )
        # Report the resulting state, not a difference of two counts. The old
        # `marked - cleared` subtracted incomparable quantities — `marked` counted
        # only previously-unmarked edges while the clear pass touches every valid
        # edge — and went negative (-19,546) once the graph grew.
        served = tx.run(
            "MATCH ()-[r:ROUTE]->() WHERE r.stale IS NULL RETURN count(r) AS n"
        ).single()["n"]
        stale = tx.run(
            "MATCH ()-[r:ROUTE]->() WHERE r.stale IS NOT NULL RETURN count(r) AS n"
        ).single()["n"]
        return served, stale

    with driver.session() as session:
        return session.execute_write(_reconcile)


def merge_supplement_aircraft(driver, used_types: set) -> int:
    """Ensure :Aircraft nodes exist for supplement types seen in routes."""
    batch = [
        {
            "iata": meta["iata"],
            "icao": icao,
            "name": meta["name"],
            "family": meta["family"],
            "aliases": [icao, meta["iata"], meta["name"]],
        }
        for icao, meta in SUPPLEMENT_TYPES.items()
        if icao in used_types
    ]
    if not batch:
        return 0
    with driver.session() as session:
        session.execute_write(
            lambda tx: tx.run(
                """
                UNWIND $batch AS ac
                MERGE (a:Aircraft {iata: ac.iata})
                SET a.icao = ac.icao, a.name = ac.name,
                    a.family = ac.family, a.aliases = ac.aliases
                """,
                batch=batch,
            )
        )
    return len(batch)


def _parse_iso(value: str):
    """Parse a fetcher's `fetched_at`; None on anything unexpected.

    The fetchers write `datetime.now(timezone.utc).isoformat(timespec="seconds")`,
    an explicit `+00:00` offset that `fromisoformat` accepts on 3.9 (a bare 'Z'
    would not). Deliberately strict and None-returning rather than guessing, so a
    format drift surfaces as "no upstream data" instead of crashing the rebuild.
    """
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def upstream_state(conn: sqlite3.Connection) -> dict:
    """Is it safe, and worth it, to rebuild right now? (§4.1.5 *Refresh chain*)

    Two conditions, reported differently on purpose:

    - `busy` — a fetch log was written within UPSTREAM_QUIET_MINUTES, so a fetcher
      may still be mid-run, and rebuilding from a half-written day is exactly what
      this gate exists to prevent. Skipping is *not* a fault: the rebuild is
      idempotent and tomorrow's run catches up. It stays silent because launchd
      fires every overdue agent at once on wake from sleep, in no guaranteed order,
      so an unfinished upstream is routine rather than exceptional — alarming here
      would cry wolf on a schedule and train the alarm to be ignored.
    - `stale_hours` — nothing has been collected for far longer than a day. This
      does *not* block, since rebuilding stays safe and still reconciles; it alarms
      instead. It is an independent check that the collection layer has stopped,
      which is the precise blind spot that went unnoticed for 57 days.

    Quiescence is a proxy for "finished", not proof of it: a fetcher pausing longer
    than UPSTREAM_QUIET_MINUTES would read as idle. The fetchers pause only
    REQUEST_PAUSE_SECONDS between calls, so the margin is wide.
    """
    now = datetime.now(timezone.utc)
    newest = None
    busy_table = None
    for table in ("fetch_log", "schedules_fetch_log"):
        if not _table_exists(conn, table):
            continue
        row = conn.execute(f"SELECT max(fetched_at) FROM {table}").fetchone()
        dt = _parse_iso(row[0]) if row and row[0] else None
        if dt is None:
            continue
        if (now - dt).total_seconds() / 60.0 < UPSTREAM_QUIET_MINUTES:
            busy_table = table
        if newest is None or dt > newest:
            newest = dt
    if newest is None:
        return {"busy": False, "busy_table": None, "stale_hours": None}
    age_hours = (now - newest).total_seconds() / 3600.0
    return {
        "busy": busy_table is not None,
        "busy_table": busy_table,
        "stale_hours": age_hours if age_hours > UPSTREAM_STALE_HOURS else None,
    }


def run(before_connect=None) -> dict:
    """Rebuild the staging graph.

    `before_connect` runs only once there is something to load, so the refresh chain
    can start Neo4j there and a skipped or empty run never starts it. A hand-run
    passes nothing and leaves container state alone.
    """
    conn = sqlite3.connect(SQLITE_PATH)
    try:
        gate = upstream_state(conn)
        if gate["busy"]:
            print(
                f"--- {gate['busy_table']} written under {UPSTREAM_QUIET_MINUTES}min "
                "ago; a fetcher may still be running — skipping rebuild ---"
            )
            return {
                "skipped": f"{gate['busy_table']} still being written",
                "raw_rows": 0,
                "routes_valid": 0,
            }
        lookups = shared_lookups(conn)
        flight_routes, m, unmapped = aggregate(conn, lookups)
        sched_routes, unmapped_models = aggregate_schedules(conn, m, lookups)
        recency = recency_days_by_airport(conn)
        sampled = sampled_days_by_origin(conn)
    finally:
        conn.close()

    valid = shape_valid(flight_routes, recency, sampled, "opensky", m) + shape_valid(
        sched_routes, recency, sampled, "aerodatabox", m
    )
    m["routes_valid"] = len(valid)
    m["sched_routes_total"] = len(sched_routes)

    total = max(m["total"], 1)
    print("--- Graph loader metrics ---")
    print(f"  raw flights:            {m['total']}")
    print(f"  NULL destination:       {m['null_dest']} ({m['null_dest'] / total:.1%})")
    print(
        f"  inferred destinations:  {m['inferred']}"
        f" ({m['inferred'] / max(m['null_dest'], 1):.1%} of NULLs)"
    )
    print(f"  dropped (no dest):      {m['dropped_no_dest']}")
    print(
        f"  dropped (bad airport):  {m['dropped_unknown_airport'] + m['dropped_same_airport']}"
    )
    print(
        f"  dropped (unmapped a/c): {m['dropped_unmapped_aircraft']}"
        f" ({m['dropped_unmapped_aircraft'] / total:.1%})"
    )
    print(f"  airline unresolved:     {m['airline_unresolved']}")
    print(
        f"  dropped (blackout origin, owned by schedules): {m['dropped_blackout_origin']}"
    )
    print(
        f"  routes (opensky): {m['routes_total']} observed → "
        f"{m['opensky_filtered_out']} filtered"
    )
    if unmapped:
        top = sorted(unmapped.items(), key=lambda kv: -kv[1])[:10]
        print(f"  top unmapped ICAO types: {top}")

    if m["sched_total"]:
        st = m["sched_total"]
        print("  --- schedules (AeroDataBox) ---")
        print(f"  raw schedule rows:      {st}")
        print(
            f"  type from Mode-S:       {m['sched_type_from_modes']}"
            f" ({m['sched_type_from_modes'] / st:.1%})"
        )
        print(
            f"  type from model name:   {m['sched_type_from_model']}"
            f" ({m['sched_type_from_model'] / st:.1%})"
        )
        print(
            f"  dropped (unmapped a/c): {m['sched_dropped_unmapped_aircraft']}"
            f" ({m['sched_dropped_unmapped_aircraft'] / st:.1%})"
        )
        print(
            f"  dropped (bad airport):  "
            f"{m['sched_dropped_unknown_airport'] + m['sched_dropped_same_airport']}"
        )
        print(f"  dropped (cargo):        {m['sched_dropped_cargo']}")
        print(
            f"  duplicate same-day departures skipped: {m['sched_duplicate_same_day']}"
        )
        print(f"  airline unresolved:     {m['sched_airline_unresolved']}")
        print(
            f"  routes (aerodatabox): {m['sched_routes_total']} observed → "
            f"{m['aerodatabox_filtered_out']} filtered"
        )
        if unmapped_models:
            top = sorted(unmapped_models.items(), key=lambda kv: -kv[1])[:8]
            print(f"  top unmapped models: {top}")
    print(f"  TOTAL valid routes: {m['routes_valid']}")

    raw_rows = m["total"] + m["sched_total"]
    if not valid:
        print("No valid routes to load — accumulate more data first.")
        return {
            "raw_rows": raw_rows,
            "routes_valid": 0,
            "stale_hours": gate["stale_hours"],
        }

    if before_connect:
        before_connect()

    cfg = staging_neo4j_config()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        try:
            driver.verify_connectivity()
        except Exception as e:
            # A precondition, not a load failure. The useful message is "the database
            # was not reachable" rather than a driver traceback: a hand-run relies on
            # the container having been started manually, and the refresh chain's
            # `before_connect` should already have ruled this out.
            raise RuntimeError(
                f"staging Neo4j unreachable at {cfg['uri']}: {type(e).__name__}"
            ) from e
        n_supp = merge_supplement_aircraft(driver, {r["icaotype"] for r in valid})
        load_routes(driver, valid)
        served, stale = mark_stale(driver, valid)
        print(
            f"Loaded {len(valid)} ROUTE relationships into staging Neo4j "
            f"(+{n_supp} supplement Aircraft nodes)."
        )
        print(
            f"Reconciliation: {served} edges served, {stale} marked stale and excluded "
            f"from the read path (sweep_stale.py, 60-day grace)."
        )
    finally:
        driver.close()
    return {
        "raw_rows": raw_rows,
        "routes_valid": m["routes_valid"],
        "served": served,
        "stale": stale,
        "stale_hours": gate["stale_hours"],
    }


def classify(stats: dict) -> tuple:
    """Decide whether an unattended rebuild actually produced a served graph.

    The loader became a scheduled job on 2026-09-12 (§4.1.5 *Refresh chain*), so it
    needs what the fetchers already have: a rebuild that writes nothing reads exactly
    like a healthy one in the log, and that is how a stalled pipeline stays invisible.
    """
    if stats.get("skipped"):
        return "QUIET", stats["skipped"]
    if not stats["raw_rows"]:
        return "QUIET", "SQLite cache is empty — nothing to rebuild yet"
    if not stats["routes_valid"]:
        return "UNPRODUCTIVE", (
            f"0 valid routes from {stats['raw_rows']} raw rows — the validity filter "
            "rejected everything (§4.1.3)"
        )
    if stats.get("stale_hours"):
        return "STALE_UPSTREAM", (
            f"rebuilt {stats['served']} served edges, but no fetch has written for "
            f"{stats['stale_hours']:.0f}h — the collectors have stopped"
        )
    return "OK", f"{stats['served']} edges served, {stats['stale']} marked stale"


if __name__ == "__main__":
    run_guard("Daily graph rebuild", run, classify, work_stage="rebuild")
