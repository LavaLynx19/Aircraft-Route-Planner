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
import pytest
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


# A tier of 2 at twice the rate of the rest, small enough to reason about by hand.
TIER_CFG = {"priority_tier_size": 2, "priority_rate_multiplier": 2}


class TestSchedulesTiering:
    """Queue priority within the blackout set (ARCHITECTURE.md §4.1.7, Decisions 1-2)."""

    def test_split_gives_a_tier_airport_twice_the_rate(self):
        """The production shape: 150 priority airports and 293 others at 19 calls."""
        tier, rest = fetch_schedules.split_budget(19, 150, 293, 2)
        assert (tier, rest) == (10, 9)
        # Whole calls only, so the realised ratio is near 2 rather than exactly 2.
        assert (tier / 150) / (rest / 293) == pytest.approx(2, rel=0.1)

    def test_split_edge_cases(self):
        assert fetch_schedules.split_budget(0, 150, 293, 2) == (0, 0)
        assert fetch_schedules.split_budget(19, 0, 443, 2) == (0, 19)
        assert fetch_schedules.split_budget(19, 443, 0, 2) == (19, 0)
        # A multiplier of 1 shares calls in proportion to airport counts.
        assert fetch_schedules.split_budget(19, 150, 293, 1) == (6, 13)

    def test_without_any_yield_the_tier_is_the_busiest(self):
        rows = [("A", 0, 0, None), ("B", 1, 0, None), ("C", 2, 0, None)]
        assert fetch_schedules.rank_priority(rows, 2) == {"A", "B"}

    def test_a_productive_airport_outranks_a_busier_band(self):
        """Measured yield and band priors share one unit, so they compete directly."""
        rows = [
            ("BUSY1", 0, 1, 10.0),
            ("BUSY2", 1, 1, 12.0),
            ("QUIET", 60, 2, 90.0),  # two successes: its own yield counts
        ]
        assert fetch_schedules.rank_priority(rows, 2) == {"QUIET", "BUSY1"}

    def test_own_yield_counts_only_from_the_second_success(self):
        """One 12h window is too noisy to rank on; the band median stands in for it."""
        band_zero = [("A", 0, 1, 50.0), ("B", 2, 1, 60.0)]
        elsewhere = [("FAR", 100, 1, 20.0)]
        one_visit = band_zero + [("LOW", 1, 1, 5.0)] + elsewhere
        two_visits = band_zero + [("LOW", 1, 2, 5.0)] + elsewhere
        assert "LOW" in fetch_schedules.rank_priority(one_visit, 2)
        assert "LOW" not in fetch_schedules.rank_priority(two_visits, 2)

    def test_queue_stays_flat_until_the_first_sweep_completes(self, conn):
        """Tiering is for revisits; before then it would only slow first coverage."""
        for i in range(4):
            add_schedule_airport(conn, f"Z00{i}", rank=i)
        add_schedule_attempt(conn, "Z000", status="ok", days_ago=5)

        queue, how = fetch_schedules.plan_queue(conn, 3, TIER_CFG)
        assert queue == fetch_schedules.due_airports(conn, 3)
        assert how.startswith("flat until the first sweep completes: 1 of 4")

    def test_once_every_airport_is_tried_the_tier_gets_its_share(self, conn):
        for i in range(6):
            icao = f"Z00{i}"
            add_schedule_airport(conn, icao, rank=i)
            add_schedule_attempt(conn, icao, status="ok", days_ago=20 + i, half=0)

        # A tier of 2 at 2x the rate of the other 4: split_budget(4, 2, 4, 2) == (2, 2).
        queue, how = fetch_schedules.plan_queue(conn, 4, TIER_CFG)
        assert how.startswith(
            "tiered: 2 calls to 2 priority airports, 2 to the other 4"
        )
        assert {icao for icao, _ in queue[:2]} == {"Z000", "Z001"}
        assert len(queue) == 4
        # The half still flips after a resolved visit, inside every tier.
        assert all(half == 1 for _, half in queue)

    def test_only_restricts_the_queue_to_one_tier(self, conn):
        for icao in ("ZAAA", "ZBBB", "ZCCC"):
            add_schedule_airport(conn, icao)
        due = fetch_schedules.due_airports(conn, 5, only={"ZBBB"})
        assert [icao for icao, _ in due] == ["ZBBB"]


class TestStoredQuota:
    """The pre-run budget is the last value the provider reported (§4.1.8)."""

    NOW = datetime(2026, 10, 10, 4, 30, 0, tzinfo=timezone.utc)

    def _quota(self, remaining, *, reset_in_days=20.0, observed_days_ago=1.0):
        return {
            "remaining": remaining,
            "limit": 600,
            "reset_at": (self.NOW + timedelta(days=reset_in_days)).isoformat(),
            "observed_at": (self.NOW - timedelta(days=observed_days_ago)).isoformat(),
        }

    def test_nothing_is_stored_before_the_first_response(self, conn):
        assert fetch_schedules.load_quota(conn) is None

    def test_a_saved_quota_round_trips(self, conn):
        quota = self._quota(412)
        fetch_schedules.save_quota(conn, quota)
        assert fetch_schedules.load_quota(conn) == quota

    def test_a_later_response_overwrites_the_row(self, conn):
        fetch_schedules.save_quota(conn, self._quota(412))
        fetch_schedules.save_quota(conn, self._quota(410))
        rows = conn.execute("SELECT remaining FROM schedules_quota").fetchall()
        assert rows == [(410,)]

    def test_no_row_is_unknown_and_the_run_may_start(self):
        assert fetch_schedules.known_units(None, self.NOW) is None
        assert fetch_schedules.quota_allows_run(None, 20, self.NOW)

    def test_a_current_row_above_the_reserve_allows_the_run(self):
        stored = self._quota(412)
        assert fetch_schedules.known_units(stored, self.NOW) == 412
        assert fetch_schedules.quota_allows_run(stored, 20, self.NOW)

    def test_a_current_row_at_the_reserve_blocks_the_run(self):
        assert not fetch_schedules.quota_allows_run(self._quota(20), 20, self.NOW)
        assert not fetch_schedules.quota_allows_run(self._quota(0), 20, self.NOW)

    def test_a_passed_reset_means_the_full_limit_again(self):
        """The 2026-10-01 case: an exhausted row must not outlive its period."""
        stored = self._quota(0, reset_in_days=-0.5)
        assert fetch_schedules.known_units(stored, self.NOW) == 600
        assert fetch_schedules.quota_allows_run(stored, 20, self.NOW)

    def test_a_row_older_than_the_age_limit_is_unknown(self):
        """Headerless responses never refresh the row, so it must expire."""
        age = fetch_schedules.QUOTA_MAX_AGE_DAYS + 0.1
        stored = self._quota(0, observed_days_ago=age)
        assert fetch_schedules.known_units(stored, self.NOW) is None
        assert fetch_schedules.quota_allows_run(stored, 20, self.NOW)

    def test_a_row_just_inside_the_age_limit_still_counts(self):
        age = fetch_schedules.QUOTA_MAX_AGE_DAYS - 0.1
        stored = self._quota(0, observed_days_ago=age)
        assert not fetch_schedules.quota_allows_run(stored, 20, self.NOW)
