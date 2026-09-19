"""Queue-ordering tests for both fetchers.

These exist because of a specific incident. Both queues ordered by last
*success*, so an airport in a region the source cannot see returned `empty`
forever, never acquired a success timestamp, and therefore sat permanently at
the head of the queue ahead of airports that do return data. Measured
2026-09-10: 41 hub airports retried up to 8 times each with zero yield while
KATL, KORD and EGLL went unfetched for 67 days, on a finite API budget.

The bug was one SQL `ORDER BY`. `test_empty_airport_does_not_starve_the_queue`
is the regression that would have caught it.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import fetch_flights
import fetch_schedules
from conftest import (
    add_airport,
    add_attempt,
    add_schedule_airport,
    add_schedule_attempt,
)
from fetch_queue import order_due


class TestOpenSkyQueue:
    def test_empty_airport_does_not_starve_the_queue(self, conn):
        """An always-empty airport must not outrank one unvisited for 60 days."""
        add_airport(conn, "EMPTY", rank=0)
        add_airport(conn, "REALHUB", rank=1)
        for days in (1, 6, 11, 16, 21):  # five attempts, never a success
            add_attempt(conn, "EMPTY", status="empty", days_ago=days)
        add_attempt(conn, "REALHUB", status="ok", days_ago=60)

        queue = fetch_flights.due_airports(conn, "hub", 1, set())
        assert queue == ["REALHUB"]

    def test_never_attempted_comes_first(self, conn):
        add_airport(conn, "SEEN", rank=0)
        add_airport(conn, "FRESH", rank=1)
        add_attempt(conn, "SEEN", status="ok", days_ago=90)

        assert fetch_flights.due_airports(conn, "hub", 2, set())[0] == "FRESH"

    def test_ties_break_on_traffic_rank(self, conn):
        add_airport(conn, "BUSY", rank=0)
        add_airport(conn, "QUIET", rank=99)

        assert fetch_flights.due_airports(conn, "hub", 2, set()) == ["BUSY", "QUIET"]

    def test_tiers_are_budgeted_separately(self, conn):
        """A tail airport must never appear in the hub queue, however stale."""
        add_airport(conn, "HUBA", tier="hub", rank=5)
        add_airport(conn, "TAILA", tier="tail", rank=0)
        add_attempt(conn, "HUBA", status="ok", days_ago=1)
        add_attempt(conn, "TAILA", status="ok", days_ago=365)

        assert fetch_flights.due_airports(conn, "hub", 10, set()) == ["HUBA"]
        assert fetch_flights.due_airports(conn, "tail", 10, set()) == ["TAILA"]

    def test_done_set_is_excluded(self, conn):
        add_airport(conn, "AAAA", rank=0)
        add_airport(conn, "BBBB", rank=1)

        assert fetch_flights.due_airports(conn, "hub", 5, {"AAAA"}) == ["BBBB"]

    def test_error_attempts_also_advance_the_clock(self, conn):
        """Errors must count as attempts, else a broken airport starves the queue."""
        add_airport(conn, "BROKEN", rank=0)
        add_airport(conn, "OTHER", rank=1)
        add_attempt(conn, "BROKEN", status="error", days_ago=0)
        add_attempt(conn, "OTHER", status="ok", days_ago=30)

        assert fetch_flights.due_airports(conn, "hub", 1, set()) == ["OTHER"]


class TestSchedulesQueue:
    def test_persistent_failure_does_not_starve_the_queue(self, conn):
        """UZTT failed 5 of 5 and was re-queued every run — this is that case."""
        add_schedule_airport(conn, "UZTT", rank=0)
        add_schedule_airport(conn, "ZBAA", rank=1)
        for days in (0, 1, 2, 3, 4):
            add_schedule_attempt(conn, "UZTT", status="error", days_ago=days)
        add_schedule_attempt(conn, "ZBAA", status="ok", days_ago=30)

        assert [icao for icao, _ in fetch_schedules.due_airports(conn, 1)] == ["ZBAA"]

    def test_never_attempted_starts_on_first_half(self, conn):
        add_schedule_airport(conn, "ZSPD")
        assert fetch_schedules.due_airports(conn, 1) == [("ZSPD", 0)]

    def test_half_flips_after_a_success(self, conn):
        add_schedule_airport(conn, "ZSPD")
        add_schedule_attempt(conn, "ZSPD", status="ok", days_ago=23, half=0)

        assert fetch_schedules.due_airports(conn, 1) == [("ZSPD", 1)]

    def test_half_flips_after_an_empty_window(self, conn):
        """No departures is a real observation; the flights may be in the other half."""
        add_schedule_airport(conn, "UZFA")
        add_schedule_attempt(conn, "UZFA", status="empty", days_ago=23, half=0)

        assert fetch_schedules.due_airports(conn, 1) == [("UZFA", 1)]

    def test_half_flips_from_the_newest_resolved_visit(self, conn):
        """An old success must not outvote a newer empty window."""
        add_schedule_airport(conn, "UZFA")
        add_schedule_attempt(conn, "UZFA", status="ok", days_ago=46, half=0)
        add_schedule_attempt(conn, "UZFA", status="empty", days_ago=23, half=1)

        assert fetch_schedules.due_airports(conn, 1) == [("UZFA", 0)]

    def test_half_does_not_flip_after_a_failure(self, conn):
        """A failed attempt collected nothing, so retry the same half."""
        add_schedule_airport(conn, "ZSPD")
        add_schedule_attempt(conn, "ZSPD", status="error", days_ago=1, half=0)

        assert fetch_schedules.due_airports(conn, 1) == [("ZSPD", 0)]


class TestWindowAlreadyResolved:
    """The skip guard added for 2026-09-10 review L7.

    Same-window re-fetching was previously prevented only as an emergent property
    of `ORDER BY last_attempt ASC` — an airport fetched today sorted last. Correct,
    but an accident of the ordering, so a future ordering change could silently
    reintroduce double-spending against the hard 600-unit monthly cap.

    These are synthetic by necessity: as of 2026-09-13, 282 of 443 airports have
    never been attempted, so the queue is still serving first visits and no airport
    has reached a *second* visit. Half 1 has therefore never been fetched in
    production and the guard cannot be exercised against live data for ~15 more
    days. Tests are the only way to pin it down now.

    Setting these up needs care: the selected half is `1 - (half of the most recent
    'ok')`, so to make the queue choose half H the newest success must be half 1-H.
    """

    def _both_halves_done(self, conn, icao, *, older_status="ok"):
        """Newest ok on half 1 (so half 0 is selected next), half 0 already logged."""
        add_schedule_airport(conn, icao)
        # window_for(0) is offset -1440, i.e. yesterday's date — matching days_ago=1.
        add_schedule_attempt(conn, icao, status=older_status, days_ago=1, half=0)
        # Newest ok, so last_half = 1 and the next selection is half 0.
        add_schedule_attempt(conn, icao, status="ok", days_ago=0, half=1)

    def test_a_window_already_collected_is_skipped(self, conn):
        self._both_halves_done(conn, "ZSPD")

        assert fetch_schedules.due_airports(conn, 5) == []

    def test_an_empty_window_also_counts_as_resolved(self, conn):
        """Mirrors fetch_flights: re-querying an empty window buys the same answer."""
        self._both_halves_done(conn, "ZSPD", older_status="empty")

        assert fetch_schedules.due_airports(conn, 5) == []

    def test_an_errored_window_is_retried(self, conn):
        """An error collected nothing, so that window is still owed."""
        self._both_halves_done(conn, "ZSPD", older_status="error")

        assert fetch_schedules.due_airports(conn, 5) == [("ZSPD", 0)]

    def test_skipping_does_not_shrink_the_batch(self, conn):
        """A skipped airport must not waste a call slot.

        due_airports orders every candidate before filtering and truncating, so a
        skipped window can never leave the batch below budget.
        """
        self._both_halves_done(conn, "ZSPD")  # fully resolved, must be skipped
        add_schedule_airport(conn, "ZBAA", rank=9)  # never attempted

        assert fetch_schedules.due_airports(conn, 1) == [("ZBAA", 0)]


class TestUnitAccounting:
    def test_retries_are_counted_as_units_not_rows(self, conn):
        """A throttle retry is a second real API call against the 600-unit cap."""
        add_schedule_attempt(conn, "ZSPD", status="ok", days_ago=0, attempts=2)
        add_schedule_attempt(conn, "ZBAA", status="ok", days_ago=0, attempts=1)

        rows = conn.execute("SELECT count(*) FROM schedules_fetch_log").fetchone()[0]
        assert rows == 2
        assert fetch_schedules.units_used_this_month(conn) == 3

    def test_empty_log_reports_zero_not_none(self, conn):
        assert fetch_schedules.units_used_this_month(conn) == 0


def test_window_halves_tile_the_last_24h():
    """The two halves must abut exactly — no gap, no overlap."""
    off0, _ = fetch_schedules.window_for(0)
    off1, _ = fetch_schedules.window_for(1)
    assert off0 == -1440
    assert off1 == -fetch_schedules.WINDOW_MINUTES
    assert off0 + fetch_schedules.WINDOW_MINUTES == off1


class TestOrderDue:
    """The one ordering rule both queues delegate to (2026-09-10 review L6).

    It was two SQL `ORDER BY` clauses before the extraction, so the strongest
    proof of a behaviour-preserving move is a comparison against SQLite itself.
    """

    ROWS = (
        ("NEVER_B", 7, None),
        ("NEVER_A", 2, None),
        ("NEVER_UNRANKED", None, None),
        ("STALE", 5, "2026-07-01T09:00:00+00:00"),
        ("TIE_HI", 9, "2026-08-15T09:00:00+00:00"),
        ("TIE_LO", 1, "2026-08-15T09:00:00+00:00"),
        ("TIE_UNRANKED", None, "2026-08-15T09:00:00+00:00"),
        ("RECENT", 0, "2026-09-13T09:00:00+00:00"),
    )

    def test_matches_the_sqlite_order_it_replaced(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t (icao TEXT, rank INTEGER, last_attempt TEXT)")
        c.executemany("INSERT INTO t VALUES (?,?,?)", self.ROWS)
        expected = c.execute(
            "SELECT icao, rank, last_attempt FROM t "
            "ORDER BY (last_attempt IS NULL) DESC, last_attempt ASC, rank ASC"
        ).fetchall()
        c.close()

        assert order_due(list(reversed(self.ROWS))) == expected

    def test_never_attempted_then_stalest_then_rank(self):
        assert [r[0] for r in order_due(self.ROWS)] == [
            "NEVER_UNRANKED",
            "NEVER_A",
            "NEVER_B",
            "STALE",
            "TIE_UNRANKED",
            "TIE_LO",
            "TIE_HI",
            "RECENT",
        ]

    def test_extra_columns_ride_along_untouched(self):
        rows = [("B", 1, None, "half-b"), ("A", 0, None, "half-a")]
        assert order_due(rows) == [("A", 0, None, "half-a"), ("B", 1, None, "half-b")]


class TestSchedulesBatchIsNotShortened:
    def test_resolved_windows_at_the_head_do_not_shrink_the_batch(self, conn):
        """Synthetic guard for the `limit * 3` over-fetch that L6 retired.

        The old query took three rows per budget slot and filtered only those, so
        three resolved windows ahead of one owed airport returned nothing at
        limit=1. Consistent production timestamps make that ordering unlikely (a
        window resolved today sorts last), which is why it is built by hand: each
        resolved airport's log rows carry the current window but an old fetched_at.

        The half-1 success is a day newer than the half-0 empty row. Both count
        as resolved when choosing the next half, so identical timestamps would
        make that choice a tie.
        """
        _offset, current_half0_date = fetch_schedules.window_for(0)
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=40)).isoformat(timespec="seconds")
        newer = (now - timedelta(days=39)).isoformat(timespec="seconds")
        insert = (
            "INSERT OR REPLACE INTO schedules_fetch_log (airport_icao, window_date, "
            "day_half, status, flight_count, fetched_at, attempts) VALUES (?,?,?,?,?,?,?)"
        )
        for rank, icao in enumerate(("ZRES1", "ZRES2", "ZRES3")):
            add_schedule_airport(conn, icao, rank=rank)
            # Newest ok on half 1, so half 0 is selected next ...
            conn.execute(insert, (icao, "2026-01-01", 1, "ok", 0, newer, 1))
            # ... and half 0's current window is already resolved.
            conn.execute(insert, (icao, current_half0_date, 0, "empty", 0, old, 1))
        conn.commit()
        add_schedule_airport(conn, "ZOWED", rank=9)
        add_schedule_attempt(conn, "ZOWED", status="ok", days_ago=10, half=0)

        assert fetch_schedules.due_airports(conn, 1) == [("ZOWED", 1)]
