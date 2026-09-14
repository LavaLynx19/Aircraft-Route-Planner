"""Script 1 — OpenSky raw flight fetcher (runs daily via launchd).

Strategy (ARCHITECTURE.md §13.1, breadth): ~930 airports, each fetched one
2-day window per ~7.4-day cycle. Every request costs a flat 30 credits against
the 4,000/day /flights bucket, so a run does ≤125 requests (≈3,750 credits)
and stops early if X-Rate-Limit-Remaining drops below the floor.

Queue order: never-fetched airports first (by priority rank), then stalest.
The non-integer cycle length rotates which weekdays each airport samples.
Resumable: fetch_log records every attempt; re-runs skip windows already 'ok'.
"""

import json
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from airports_oa import fetch_list
from config import OSK_API_BASE, OSK_TOKEN_URL, SQLITE_PATH, opensky_config
from fetch_queue import order_due
from runner import run_guard

TOKEN_REFRESH_SECONDS = 25 * 60  # tokens live 30 min
REQUEST_PAUSE_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_flights (
    icao24 TEXT NOT NULL,
    callsign TEXT,
    origin TEXT NOT NULL,
    destination TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(icao24, first_seen)
);
CREATE INDEX IF NOT EXISTS idx_raw_flights_origin ON raw_flights(origin);
CREATE INDEX IF NOT EXISTS idx_raw_flights_callsign ON raw_flights(callsign);

CREATE TABLE IF NOT EXISTS fetch_log (
    airport_icao TEXT NOT NULL,
    window_begin INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    status TEXT NOT NULL,
    flight_count INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (airport_icao, window_begin)
);

CREATE TABLE IF NOT EXISTS fetch_airports (
    icao TEXT PRIMARY KEY,
    iata TEXT NOT NULL,
    type TEXT NOT NULL,
    rank INTEGER NOT NULL,
    tier TEXT NOT NULL DEFAULT 'tail'
);
"""


class TokenManager:
    def __init__(self, client_id: str, client_secret: str):
        self._id, self._secret = client_id, client_secret
        self._token, self._issued_at = None, 0.0

    def token(self) -> str:
        if self._token is None or time.time() - self._issued_at > TOKEN_REFRESH_SECONDS:
            body = urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": self._id,
                    "client_secret": self._secret,
                }
            ).encode()
            token_req = urllib.request.Request(OSK_TOKEN_URL, data=body)
            with urllib.request.urlopen(
                token_req, timeout=REQUEST_TIMEOUT_SECONDS
            ) as resp:
                self._token = json.load(resp)["access_token"]
            self._issued_at = time.time()
        return self._token


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS will not add a column to a table that predates
    # the tiered strategy, so bring an existing fetch_airports forward in place.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(fetch_airports)")}
    if "tier" not in columns:
        conn.execute(
            "ALTER TABLE fetch_airports ADD COLUMN tier TEXT NOT NULL DEFAULT 'tail'"
        )
    conn.commit()


def refresh_fetch_airports(conn: sqlite3.Connection, hub_size: int) -> tuple:
    """(Re)build the priority fetch list from OurAirports and assign tiers.

    Returns (total, hub_count). Blackout-region airports are already excluded by
    fetch_list; the top `hub_size` by traffic rank become the weekly-refresh tier.
    """
    airports = fetch_list()
    conn.execute("DELETE FROM fetch_airports")
    conn.executemany(
        "INSERT INTO fetch_airports (icao, iata, type, rank, tier) VALUES (?,?,?,?,?)",
        [
            (a["icao"], a["iata"], a["type"], i, "hub" if i < hub_size else "tail")
            for i, a in enumerate(airports)
        ],
    )
    conn.commit()
    return len(airports), min(hub_size, len(airports))


def due_airports(
    conn: sqlite3.Connection, tier: str, max_requests: int, done: set
) -> list:
    """Never-attempted first (by rank), then longest-unvisited, within one tier.

    Tiers are budgeted separately so the long tail cannot crowd out the hubs:
    a single global queue would spend every request on the 2,500+ tail airports
    and starve the hubs it is meant to keep fresh.

    Priority is `fetch_queue.order_due`: by last *attempt*, not last success —
    the starvation incident behind that rule is recorded there. An 'empty'
    result is a legitimate observation ("no departures in this window"), not a
    failure to look.
    """
    rows = conn.execute(
        """
        SELECT fa.icao, fa.rank, MAX(fl.fetched_at) AS last_attempt
        FROM fetch_airports fa
        LEFT JOIN fetch_log fl
            ON fl.airport_icao = fa.icao
        WHERE fa.tier = ?
        GROUP BY fa.icao
        """,
        (tier,),
    ).fetchall()
    return [r[0] for r in order_due(rows) if r[0] not in done][:max_requests]


def current_window() -> tuple:
    """The 2 most recent complete UTC days (OpenSky data lands after a nightly batch)."""
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    begin = today - timedelta(days=2)
    return int(begin.timestamp()), int(today.timestamp()) - 1


def fetch_departures(tm: TokenManager, icao: str, begin: int, end: int) -> tuple:
    """Return (status, flights, credits_remaining). status ∈ ok|empty|error|rate_limited."""
    query = urllib.parse.urlencode({"airport": icao, "begin": begin, "end": end})
    url = f"{OSK_API_BASE}/flights/departure?{query}"
    try:
        # tm.token() may hit the network (25-min refresh); keep it inside the try
        # so a refresh blip costs one airport instead of aborting the whole run.
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {tm.token()}"}
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            remaining = int(resp.headers.get("X-Rate-Limit-Remaining", "-1"))
            return "ok", json.load(resp), remaining
    except urllib.error.HTTPError as e:
        remaining = int(e.headers.get("X-Rate-Limit-Remaining", "-1"))
        if e.code == 404:
            return "empty", [], remaining
        if e.code == 429:
            return "rate_limited", [], remaining
        return "error", [], remaining
    except (OSError, json.JSONDecodeError) as e:
        # OSError, not TimeoutError: on Python 3.9 socket.timeout is a sibling of
        # TimeoutError under OSError, not an alias (that landed in 3.10), so
        # `except TimeoutError` silently missed read timeouts and one slow
        # response killed the entire run. OSError also covers URLError and
        # connection resets. HTTPError is a URLError subclass but is handled above.
        print(f"  {icao}: {type(e).__name__}: {e}")
        return "error", [], -1


def store_flights(conn: sqlite3.Connection, flights: list, fetched_at: str) -> int:
    rows = [
        (
            f["icao24"],
            (f.get("callsign") or "").strip() or None,
            f["estDepartureAirport"],
            f.get("estArrivalAirport"),
            f["firstSeen"],
            f["lastSeen"],
            fetched_at,
        )
        for f in flights
        if f.get("estDepartureAirport") and f.get("icao24") and f.get("firstSeen")
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO raw_flights "
        "(icao24, callsign, origin, destination, first_seen, last_seen, fetched_at) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def run() -> dict:
    cfg = opensky_config()
    conn = sqlite3.connect(SQLITE_PATH)
    init_db(conn)
    n_airports, n_hub = refresh_fetch_airports(conn, cfg["hub_size"])
    begin, end = current_window()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Skip airports already resolved for this window (resumability). 'empty'
    # counts as resolved: re-querying a window that returned no departures just
    # spends credits to learn the same thing.
    done = {
        r[0]
        for r in conn.execute(
            "SELECT airport_icao FROM fetch_log "
            "WHERE window_begin = ? AND status IN ('ok', 'empty')",
            (begin,),
        )
    }
    # Split the daily budget by tier rather than by a single stalest-first queue.
    hub_daily = math.ceil(cfg["hub_size"] / cfg["hub_cycle_days"])
    tail_daily = max(0, cfg["max_requests"] - hub_daily)
    hub_queue = due_airports(conn, "hub", hub_daily, done)
    tail_queue = due_airports(conn, "tail", tail_daily, done)
    queue = hub_queue + tail_queue

    print(
        f"fetch_flights: {n_airports} airports ({n_hub} hub / {n_airports - n_hub} tail), "
        f"{len(hub_queue)} hub + {len(tail_queue)} tail due this run, "
        f"window {begin}..{end}"
    )

    tm = TokenManager(cfg["client_id"], cfg["client_secret"])
    stats = {
        "ok": 0,
        "empty": 0,
        "error": 0,
        "flights": 0,
        "queued": len(queue),
        "stop_reason": "queue_exhausted",
    }
    for i, icao in enumerate(queue):
        status, flights, remaining = fetch_departures(tm, icao, begin, end)
        if status == "rate_limited":
            print(
                f"  429 rate-limited at {icao} (remaining={remaining}) — stopping run."
            )
            stats["stop_reason"] = "rate_limited"
            break
        stored = store_flights(conn, flights, now_iso) if flights else 0
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log "
            "(airport_icao, window_begin, window_end, status, flight_count, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (icao, begin, end, status, stored, now_iso),
        )
        conn.commit()
        stats[status] += 1
        stats["flights"] += stored
        if remaining != -1 and remaining < cfg["credit_floor"]:
            print(
                f"  Credit floor reached (remaining={remaining}) after {icao} — stopping run."
            )
            stats["stop_reason"] = "credit_floor"
            break
        if i + 1 < len(queue):
            time.sleep(REQUEST_PAUSE_SECONDS)

    total = conn.execute("SELECT count(*) FROM raw_flights").fetchone()[0]
    print(
        f"fetch_flights done: {stats['ok']} ok, {stats['empty']} empty, "
        f"{stats['error']} error, +{stats['flights']} flights (raw_flights total: {total})"
    )
    conn.close()
    return stats


def classify(stats: dict) -> tuple:
    """Decide whether a completed run actually collected anything.

    A barren run exits 0 on its own — no request *errored*, the credit budget
    just ran out — so without this it is indistinguishable from a healthy one.
    That is how a revoked credential or a quota change stalls accumulation for
    weeks while every run reports OK.
    """
    if stats["queued"] == 0:
        return "QUIET", "nothing due this window"
    if stats["ok"] == 0:
        return "UNPRODUCTIVE", (
            f"0 of {stats['queued']} airports returned data "
            f"(stopped: {stats['stop_reason']})"
        )
    return "OK", ""


if __name__ == "__main__":
    run_guard("Daily OpenSky fetch", run, classify)
