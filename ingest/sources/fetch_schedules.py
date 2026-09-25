"""Script 1b — AeroDataBox schedule fetcher for the ADS-B blackout regions.

OpenSky sees nothing over China and Russia/CIS: measured 89% empty for ICAO
prefix Z and 9.5 flights per success for U, because its coverage is a map of
volunteer receiver locations rather than of air traffic. Published schedules do
cover those regions (AeroDataBox reports China 95% / Russia 100% for the
schedules layer), so the 443 airports behind that blackout are fetched here
instead (ARCHITECTURE.md §4.1.5).

Budget: RapidAPI Basic is 600 units/month, 1 unit per airport call, and the API
caps a request at a 12-hour window. Rather than spend 2 units/airport for a full
day, each visit takes one 12h half and alternates halves between visits — the
graph records route existence and aircraft type, not individual flights, and one
12h window at a hub returns hundreds of departures. That halves the sweep to
~443 units, giving a ~23-day cycle instead of ~45.

Resumable: schedules_fetch_log records every attempt keyed by (airport, date,
half); re-runs skip halves already 'ok'.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from airports_oa import blackout_list
from config import ADB_API_BASE, ADB_API_HOST, SQLITE_PATH, aerodatabox_config
from fetch_queue import order_due
from runner import run_guard

REQUEST_PAUSE_SECONDS = 1.1  # RapidAPI Basic allows 1 req/s
REQUEST_TIMEOUT_SECONDS = 60
WINDOW_MINUTES = 720  # API hard cap: >12h returns HTTP 400
# Cloudflare fronts this API and rejects urllib's default "Python-urllib/3.9"
# signature with 403 / "error code: 1010". Identify the client honestly instead.
USER_AGENT = "aircraft-route-planner/1.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_schedules (
    origin_icao TEXT NOT NULL,
    dest_iata TEXT,
    dest_icao TEXT,
    flight_number TEXT NOT NULL,
    airline_iata TEXT,
    mode_s TEXT,
    aircraft_model TEXT,
    scheduled_utc TEXT NOT NULL,
    is_cargo INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    UNIQUE(origin_icao, flight_number, scheduled_utc)
);
CREATE INDEX IF NOT EXISTS idx_raw_schedules_origin ON raw_schedules(origin_icao);
CREATE INDEX IF NOT EXISTS idx_raw_schedules_mode_s ON raw_schedules(mode_s);

CREATE TABLE IF NOT EXISTS schedules_fetch_log (
    airport_icao TEXT NOT NULL,
    window_date TEXT NOT NULL,
    day_half INTEGER NOT NULL,
    status TEXT NOT NULL,
    flight_count INTEGER,
    fetched_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (airport_icao, window_date, day_half)
);

CREATE TABLE IF NOT EXISTS schedules_airports (
    icao TEXT PRIMARY KEY,
    iata TEXT NOT NULL,
    country TEXT,
    rank INTEGER NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS will not add a column to a log that predates
    # per-attempt accounting, so bring an existing one forward in place. Same
    # pattern as fetch_flights.init_db for fetch_airports.tier.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(schedules_fetch_log)")}
    if "attempts" not in columns:
        conn.execute(
            "ALTER TABLE schedules_fetch_log "
            "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1"
        )
    conn.commit()


def refresh_airports(conn: sqlite3.Connection) -> int:
    """(Re)build the blackout-region fetch list, busiest first."""
    airports = blackout_list()
    conn.execute("DELETE FROM schedules_airports")
    conn.executemany(
        "INSERT INTO schedules_airports (icao, iata, country, rank) VALUES (?,?,?,?)",
        [(a["icao"], a["iata"], a["country"], i) for i, a in enumerate(airports)],
    )
    conn.commit()
    return len(airports)


def units_used_this_month(conn: sqlite3.Connection) -> int:
    """Every HTTP call costs a unit, successful or not — count conservatively.

    Sums `attempts` rather than counting rows: a throttle retry is a second real
    API call against the 600-unit monthly cap but produces only one log row, so
    counting rows drifted *below* actual spend — the direction that makes the
    fetcher believe it has budget it does not.

    Still a lower bound: calls made outside the fetcher (manual probes) never
    reach this log, and a call that returns quota_exceeded is not logged at all.
    """
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    row = conn.execute(
        "SELECT COALESCE(SUM(attempts), 0) FROM schedules_fetch_log "
        "WHERE substr(fetched_at, 1, 7) = ?",
        (month,),
    ).fetchone()
    return row[0] if row else 0


def due_airports(conn: sqlite3.Connection, limit: int, only: set | None = None) -> list:
    """Never-attempted first (by traffic rank), then longest-unvisited.

    Returns (icao, day_half). Priority is `fetch_queue.order_due`, by last
    *attempt*: ordering on last success let a persistently failing airport sit
    at the head of the queue forever — UZTT failed 5 of 5 attempts and was
    re-queued every single run, and 10 such airports were consuming a 19-call
    daily budget (measured 2026-09-10).

    The half, in contrast, flips from the last *resolved* visit — 'ok' or
    'empty', the same pair `_window_resolved` counts. An empty window is a real
    observation, so the next visit samples the other half, where the flights may
    be. A failed attempt collected nothing, so the next try covers the same half
    rather than skip it.

    `only` restricts the queue to one tier's airports (`plan_queue`); the
    ordering and window rules are the same inside every tier.
    """
    # Order every candidate, then drop already-resolved windows, then truncate.
    # This used to over-fetch `limit * 3` rows and filter only those, which came
    # back under budget whenever enough of that head was already resolved;
    # ordering all 443 rows removes the guess (2026-09-10 review L6).
    rows = conn.execute(
        """
        SELECT sa.icao,
               sa.rank,
               MAX(sl.fetched_at) AS last_attempt,
               (SELECT day_half FROM schedules_fetch_log
                 WHERE airport_icao = sa.icao AND status IN ('ok','empty')
                 ORDER BY fetched_at DESC LIMIT 1) AS last_half
        FROM schedules_airports sa
        LEFT JOIN schedules_fetch_log sl
            ON sl.airport_icao = sa.icao
        GROUP BY sa.icao
        """
    ).fetchall()

    if only is not None:
        rows = [r for r in rows if r[0] in only]

    out = []
    for icao, _rank, _last_attempt, last_half in order_due(rows):
        if len(out) >= limit:
            break
        half = 0 if last_half is None else 1 - last_half
        if _window_resolved(conn, icao, half):
            continue
        out.append((icao, half))
    return out


# Queue priority within the blackout set — ARCHITECTURE.md §4.1.7, Decisions 1-2.
#
# One unit for every airport: rows stored per successful visit. An airport's own
# figure counts only from its second success, because a single 12h window is too
# noisy to rank on; before that it takes the median of its traffic-rank band, so
# measured and unmeasured airports compete on the same scale. (Ranking each group
# by its own percentile was rejected: with five measured airports, a strong hub
# could leave the tier merely for coming third of five.)
YIELD_MIN_VISITS = 2
RANK_BAND = 50


def rank_priority(rows: list, size: int) -> set:
    """Pick the priority tier from `(icao, traffic_rank, ok_visits, rows_per_ok)` rows.

    With no successful visit anywhere there is no yield to use, and the tier is
    simply the `size` busiest airports by traffic rank.
    """
    observed = [r for r in rows if r[2] >= 1]
    bands: dict = {}
    for _icao, rank, _ok, per_ok in observed:
        bands.setdefault(rank // RANK_BAND, []).append(per_ok)
    band_prior = {band: statistics.median(v) for band, v in bands.items()}
    overall = statistics.median([r[3] for r in observed]) if observed else None

    def score(row: tuple) -> float:
        _icao, rank, ok, per_ok = row
        value = (
            per_ok
            if ok >= YIELD_MIN_VISITS
            else band_prior.get(rank // RANK_BAND, overall)
        )
        return float("-inf") if value is None else value

    ordered = sorted(rows, key=lambda r: (-score(r), r[1]))
    return {r[0] for r in ordered[:size]}


def priority_tier(conn: sqlite3.Connection, size: int) -> set:
    """The priority tier, re-derived from the log on every run (Decision 2)."""
    rows = conn.execute(
        """
        SELECT sa.icao,
               sa.rank,
               COALESCE(SUM(sl.status = 'ok'), 0) AS ok_visits,
               SUM(CASE WHEN sl.status = 'ok' THEN sl.flight_count END) AS ok_rows
        FROM schedules_airports sa
        LEFT JOIN schedules_fetch_log sl ON sl.airport_icao = sa.icao
        GROUP BY sa.icao
        """
    ).fetchall()
    return rank_priority(
        [
            (icao, rank, ok, (ok_rows / ok) if ok else None)
            for icao, rank, ok, ok_rows in rows
        ],
        size,
    )


def split_budget(budget: int, tier_n: int, rest_n: int, multiplier: float) -> tuple:
    """Divide a run's calls so a tier airport is visited `multiplier` times as often.

    A tier's per-airport rate is its calls over its airports, so the split solves
    tier_calls / tier_n = multiplier * rest_calls / rest_n. Unspent calls are not
    moved between tiers: the cap is monthly, so they remain for later runs.
    """
    if budget <= 0:
        return 0, 0
    if tier_n <= 0:
        return 0, budget
    if rest_n <= 0:
        return budget, 0
    weight = multiplier * tier_n
    tier_calls = round(budget * weight / (weight + rest_n))
    return tier_calls, budget - tier_calls


def plan_queue(conn: sqlite3.Connection, budget: int, cfg: dict) -> tuple:
    """This run's queue, and a one-line description of how it was built.

    Tiering waits for the first sweep to finish. Its purpose is revisit cadence;
    before every airport has been tried once, splitting the budget would only
    slow first coverage of the airports still unvisited.
    """
    airports = {icao for (icao,) in conn.execute("SELECT icao FROM schedules_airports")}
    attempted = {
        icao
        for (icao,) in conn.execute(
            "SELECT DISTINCT airport_icao FROM schedules_fetch_log"
        )
    } & airports
    if len(attempted) < len(airports):
        return (
            due_airports(conn, budget),
            f"flat until the first sweep completes: {len(attempted)} of {len(airports)} attempted",
        )

    tier = priority_tier(conn, cfg["priority_tier_size"])
    rest = airports - tier
    tier_calls, rest_calls = split_budget(
        budget, len(tier), len(rest), cfg["priority_rate_multiplier"]
    )
    queue = due_airports(conn, tier_calls, only=tier) + due_airports(
        conn, rest_calls, only=rest
    )
    how = (
        f"tiered: {tier_calls} calls to {len(tier)} priority airports, "
        f"{rest_calls} to the other {len(rest)}"
    )
    return queue, how


def _window_resolved(conn: sqlite3.Connection, icao: str, half: int) -> bool:
    """Has this airport's *specific* window already been collected?

    Makes explicit what was previously an emergent property of the ordering
    (2026-09-10 review L7). Same-window re-fetching was prevented only because an
    airport fetched today sorted last under `ORDER BY last_attempt ASC` — correct,
    but an accident of the ordering rather than a stated invariant, so a future
    ordering change could silently reintroduce double-spending against the hard
    600-unit monthly cap.

    Mirrors `fetch_flights`, where 'empty' also counts as resolved: re-querying a
    window that returned no departures spends a unit to learn the same thing.

    The window date comes from `window_for()` rather than being recomputed in SQL,
    so there is exactly one definition of where a window starts.
    """
    _offset, window_date = window_for(half)
    return (
        conn.execute(
            "SELECT 1 FROM schedules_fetch_log "
            "WHERE airport_icao = ? AND window_date = ? AND day_half = ? "
            "AND status IN ('ok','empty') LIMIT 1",
            (icao, window_date, half),
        ).fetchone()
        is not None
    )


def window_for(day_half: int) -> tuple:
    """Offset for one of the two 12h halves tiling the last 24 hours.

    Expressed as an offset from now rather than local midnight: only the
    relative-time form of the endpoint is verified, and it sidesteps per-airport
    timezone maths entirely.
    """
    offset = -1440 if day_half == 0 else -WINDOW_MINUTES
    date = (datetime.now(timezone.utc) + timedelta(minutes=offset)).strftime("%Y-%m-%d")
    return offset, date


def fetch_departures(api_key: str, icao: str, offset_minutes: int) -> tuple:
    """Return (status, departures). status ∈ ok|empty|error|quota_exceeded."""
    query = urllib.parse.urlencode(
        {
            "offsetMinutes": offset_minutes,
            "durationMinutes": WINDOW_MINUTES,
            "withCodeshared": "false",  # one physical flight -> one edge, as ADS-B gives
            "withCancelled": "false",
        }
    )
    # quote(safe=""): the code is a path segment, so "/" must be encoded too.
    path_icao = urllib.parse.quote(icao, safe="")
    url = f"{ADB_API_BASE}/flights/airports/icao/{path_icao}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": ADB_API_HOST,
            "User-Agent": USER_AGENT,
        },
    )
    http_status = None
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            http_status = resp.status
            if http_status == 204:
                # The API's documented answer for a window with no departures
                # (OpenAPI GetAirportFlightsRelative), with an empty body. Parsing
                # it as JSON recorded these windows as errors, and an error never
                # flips the half, so each such airport re-queried the same empty
                # half on every visit and its other half was never sampled. 42
                # responses logged this way by 2026-09-17, 21 of 76 calls in that
                # week's four runs.
                return "empty", []
            body = json.load(resp)
        departures = body.get("departures") or []
        return ("ok" if departures else "empty"), departures
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace").strip()
        if e.code == 429:
            # 429 is overloaded: the per-second limiter and the monthly quota
            # share it. Only the latter should stop the run — treating a
            # throttle blip as quota exhaustion would truncate the sweep and
            # report it as a normal quiet run.
            if "per second" in detail.lower():
                print(f"  {icao}: throttled, backing off")
                return "throttled", []
            return "quota_exceeded", []
        # The body is what identifies a Cloudflare block (1010) or an
        # unsubscribed key; the status alone is not diagnostic.
        print(f"  {icao}: HTTP {e.code}: {detail}")
        return "error", []
    except (OSError, json.JSONDecodeError) as e:
        # OSError, not TimeoutError: on Python 3.9 socket.timeout is a sibling of
        # TimeoutError under OSError, not an alias, so the narrower form lets one
        # slow response abort the whole run. The HTTP status, when a response
        # arrived, is what tells a malformed body apart from a network failure.
        where = f" (HTTP {http_status})" if http_status else ""
        print(f"  {icao}: {type(e).__name__}{where}: {e}")
        return "error", []


def store_departures(
    conn: sqlite3.Connection, icao: str, flights: list, fetched_at: str
) -> int:
    """Flatten AeroDataBox departure records into raw_schedules rows."""
    rows = []
    for f in flights:
        movement = f.get("movement") or {}
        airport = movement.get("airport") or {}
        scheduled = (movement.get("scheduledTime") or {}).get("utc")
        number = (f.get("number") or "").strip()
        if not scheduled or not number:
            continue  # no identity or no time — cannot dedup or date it
        aircraft = f.get("aircraft") or {}
        airline = f.get("airline") or {}
        rows.append(
            (
                icao,
                (airport.get("iata") or "").strip() or None,
                (airport.get("icao") or "").strip() or None,
                number,
                (airline.get("iata") or "").strip() or None,
                (aircraft.get("modeS") or "").strip().lower() or None,
                (aircraft.get("model") or "").strip() or None,
                scheduled,
                1 if f.get("isCargo") else 0,
                fetched_at,
            )
        )
    conn.executemany(
        "INSERT OR IGNORE INTO raw_schedules "
        "(origin_icao, dest_iata, dest_icao, flight_number, airline_iata, mode_s, "
        " aircraft_model, scheduled_utc, is_cargo, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def run() -> dict:
    cfg = aerodatabox_config()
    conn = sqlite3.connect(SQLITE_PATH)
    init_db(conn)
    n_airports = refresh_airports(conn)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    used = units_used_this_month(conn)
    spendable = cfg["monthly_units"] - cfg["unit_reserve"] - used
    budget = max(0, min(cfg["daily_calls"], spendable))
    queue, how = plan_queue(conn, budget, cfg)

    print(
        f"fetch_schedules: {n_airports} blackout airports, {used} units used this "
        f"month, {budget} calls this run — {how}"
    )

    stats = {
        "ok": 0,
        "empty": 0,
        "error": 0,
        "flights": 0,
        "queued": len(queue),
        "quota_exhausted": budget == 0,
        "monthly_remaining": max(0, spendable),
        "units_spent": 0,
    }

    for i, (icao, half) in enumerate(queue):
        offset, window_date = window_for(half)
        calls = 1
        status, flights = fetch_departures(cfg["api_key"], icao, offset)
        if status == "throttled":
            time.sleep(REQUEST_PAUSE_SECONDS * 3)
            calls += 1
            status, flights = fetch_departures(cfg["api_key"], icao, offset)
            if status == "throttled":  # still throttled: record, keep going
                status = "error"
        stats["units_spent"] += calls
        if status == "quota_exceeded":
            print(f"  429 at {icao} — monthly quota exhausted, stopping run.")
            stats["quota_exhausted"] = True
            break
        stored = store_departures(conn, icao, flights, now_iso) if flights else 0
        conn.execute(
            "INSERT OR REPLACE INTO schedules_fetch_log "
            "(airport_icao, window_date, day_half, status, flight_count, fetched_at, "
            " attempts) "
            "VALUES (?,?,?,?,?,?,?)",
            (icao, window_date, half, status, stored, now_iso, calls),
        )
        conn.commit()
        stats[status] += 1
        stats["flights"] += stored
        if i + 1 < len(queue):
            time.sleep(REQUEST_PAUSE_SECONDS)

    # Recomputed after the loop from actual HTTP calls, not outcome counts: the
    # pre-run figure ignores this run's spend, and outcome counts miss retries.
    stats["monthly_remaining"] = max(0, spendable - stats["units_spent"])
    total = conn.execute("SELECT count(*) FROM raw_schedules").fetchone()[0]
    print(
        f"fetch_schedules done: {stats['ok']} ok, {stats['empty']} empty, "
        f"{stats['error']} error, +{stats['flights']} rows "
        f"(raw_schedules total: {total})"
    )
    conn.close()
    return stats


def classify(stats: dict) -> tuple:
    """Quota exhaustion is the expected monthly end state, not a fault.

    Near the 600-unit ceiling every remaining run of the month legitimately does
    nothing, so alarming on it would fire a false positive every month by design.
    A run that had budget and still collected nothing is the real signal.
    """
    if stats["quota_exhausted"] and stats["ok"] == 0:
        return "QUIET", "monthly unit quota exhausted — resumes next month"
    if stats["queued"] == 0:
        return "QUIET", "nothing due this window"
    if stats["ok"] == 0:
        return "UNPRODUCTIVE", (
            f"0 of {stats['queued']} airports returned schedules "
            f"({stats['empty']} empty, {stats['error']} errors)"
        )
    return "OK", f"{stats['monthly_remaining']} units left this month"


if __name__ == "__main__":
    run_guard("Daily AeroDataBox schedule fetch", run, classify)
