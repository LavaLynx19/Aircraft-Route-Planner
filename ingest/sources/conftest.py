"""Shared fixtures for the ingest-pipeline tests.

The modules under test use flat imports (`from config import ...`) because they
run with `ingest/sources` as the working directory under launchd, so that
directory must be on `sys.path` before they will import at all.

Run with the interpreter the agents actually use, not a newer one:

    /usr/bin/python3 -m pytest ingest/sources -q

Testing on CLT Python 3.9 is deliberate. The `socket.timeout` defect fixed on
2026-09-05 exists *only* on 3.9 — `socket.timeout` is a sibling of
`TimeoutError` there rather than an alias — so a suite run on 3.10+ would have
declared that bug absent.
"""

import os
import sqlite3
import sys

import pytest

SOURCES_DIR = os.path.dirname(os.path.abspath(__file__))
if SOURCES_DIR not in sys.path:
    sys.path.insert(0, SOURCES_DIR)

DAY = 86400


@pytest.fixture
def conn():
    """In-memory DB carrying the real schema, built by the modules' own init_db.

    Using the production `init_db` rather than a hand-written CREATE TABLE means
    the tests exercise the schema that actually ships, including its migrations.
    """
    import fetch_flights
    import fetch_schedules

    c = sqlite3.connect(":memory:")
    fetch_flights.init_db(c)
    fetch_schedules.init_db(c)
    yield c
    c.close()


@pytest.fixture
def notified(monkeypatch):
    """Capture runner.notify calls instead of firing real desktop notifications."""
    import runner

    calls = []
    monkeypatch.setattr(runner, "notify", calls.append)
    return calls


def add_airport(conn, icao, *, tier="hub", rank=0, iata=None, kind="large_airport"):
    conn.execute(
        "INSERT INTO fetch_airports (icao, iata, type, rank, tier) VALUES (?,?,?,?,?)",
        (icao, iata or icao[:3], kind, rank, tier),
    )
    conn.commit()


def add_attempt(conn, icao, *, status, days_ago, window_days=2):
    """Record one OpenSky fetch attempt `days_ago` days before now."""
    import time

    begin = int(time.time()) - days_ago * DAY
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log (airport_icao, window_begin, window_end, "
        "status, flight_count, fetched_at) VALUES (?,?,?,?,?,?)",
        (icao, begin, begin + window_days * DAY, status, 0, _iso(days_ago)),
    )
    conn.commit()


def add_schedule_airport(conn, icao, *, rank=0):
    conn.execute(
        "INSERT INTO schedules_airports (icao, iata, country, rank) VALUES (?,?,?,?)",
        (icao, icao[:3], "Testland", rank),
    )
    conn.commit()


def add_schedule_attempt(conn, icao, *, status, days_ago, half=0, attempts=1):
    conn.execute(
        "INSERT OR REPLACE INTO schedules_fetch_log (airport_icao, window_date, "
        "day_half, status, flight_count, fetched_at, attempts) VALUES (?,?,?,?,?,?,?)",
        (icao, _iso(days_ago)[:10], half, status, 0, _iso(days_ago), attempts),
    )
    conn.commit()


def _iso(days_ago):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )
