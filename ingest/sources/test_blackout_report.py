"""Tests for the free blackout-yield report (ARCHITECTURE.md §4.1.7, Decision 4)."""

import sqlite3

import blackout_report
import pytest


def _airport(conn, icao, country):
    conn.execute(
        "INSERT INTO schedules_airports (icao, iata, country, rank) VALUES (?,?,?,?)",
        (icao, icao[:3], country, 0),
    )


def _visit(conn, icao, fetched_at, status, rows=0, half=0):
    conn.execute(
        "INSERT INTO schedules_fetch_log (airport_icao, window_date, day_half, status, "
        "flight_count, fetched_at, attempts) VALUES (?,?,?,?,?,?,1)",
        (icao, fetched_at[:10], half, status, rows, fetched_at),
    )


class TestSummarise:
    def test_a_sweep_is_each_airports_visit_ordinal(self, conn):
        """Sweep k compares the same airports at the same point in their history."""
        _airport(conn, "ZAAA", "China")
        _airport(conn, "ZBBB", "China")
        _visit(conn, "ZAAA", "2026-09-01T04:00:00+00:00", "ok", rows=10)
        _visit(conn, "ZAAA", "2026-09-20T04:00:00+00:00", "ok", rows=20, half=1)
        _visit(conn, "ZBBB", "2026-09-10T04:00:00+00:00", "ok", rows=30)

        summary = blackout_report.summarise(blackout_report.visits(conn))
        assert summary[("China", 1)]["visits"] == 2
        assert summary[("China", 2)]["visits"] == 1
        assert ("China", 3) not in summary

    def test_an_error_retry_stays_in_the_sweep_it_failed_to_resolve(self, conn):
        """Errors are retried daily; five failures must not look like five sweeps."""
        _airport(conn, "UZTT", "Uzbekistan")
        _visit(conn, "UZTT", "2026-09-06T04:00:00+00:00", "error")
        _visit(conn, "UZTT", "2026-09-07T04:00:00+00:00", "error")
        _visit(conn, "UZTT", "2026-09-08T04:00:00+00:00", "ok", rows=4)
        _visit(conn, "UZTT", "2026-09-20T04:00:00+00:00", "empty", half=1)

        summary = blackout_report.summarise(blackout_report.visits(conn))
        first = summary[("Uzbekistan", 1)]
        assert (first["visits"], first["error"], first["ok"]) == (3, 2, 1)
        assert summary[("Uzbekistan", 2)]["empty"] == 1
        assert ("Uzbekistan", 3) not in summary

    def test_rows_per_ok_counts_only_successful_visits(self, conn):
        _airport(conn, "UAAA", "Russia")
        _airport(conn, "UBBB", "Russia")
        _airport(conn, "UCCC", "Russia")
        _airport(conn, "UDDD", "Russia")
        _visit(conn, "UAAA", "2026-09-01T04:00:00+00:00", "ok", rows=30)
        _visit(conn, "UBBB", "2026-09-01T05:00:00+00:00", "ok", rows=10)
        _visit(conn, "UCCC", "2026-09-01T06:00:00+00:00", "empty")
        _visit(conn, "UDDD", "2026-09-01T07:00:00+00:00", "error")

        cell = blackout_report.summarise(blackout_report.visits(conn))[("Russia", 1)]
        assert (cell["visits"], cell["ok"], cell["empty"], cell["error"]) == (
            4,
            2,
            1,
            1,
        )
        assert cell["rows"] / cell["ok"] == 20

    def test_the_half_mix_is_counted(self, conn):
        """Opposite halves change yield on their own, so the report must show them."""
        _airport(conn, "ZAAA", "China")
        _visit(conn, "ZAAA", "2026-09-01T04:00:00+00:00", "ok", rows=5, half=1)
        cell = blackout_report.summarise(blackout_report.visits(conn))[("China", 1)]
        assert cell["half1"] == 1


class TestRender:
    def test_an_empty_log_says_so(self):
        assert blackout_report.render({}) == ["No schedules visits logged yet."]

    def test_small_cells_are_marked_and_totals_are_added(self, conn):
        _airport(conn, "ZAAA", "China")
        _visit(conn, "ZAAA", "2026-09-01T04:00:00+00:00", "ok", rows=12)
        lines = blackout_report.render(
            blackout_report.summarise(blackout_report.visits(conn))
        )

        china = next(line for line in lines if line.startswith("China"))
        assert "1*" in china  # one visit is below the reliability floor
        assert "12.0" in china  # rows per ok
        assert any(line.startswith("all") for line in lines)


class TestReadOnly:
    def test_the_report_cannot_write_even_under_a_path_with_a_space(self, tmp_path):
        """The repository lives under "My Projects"; the URI must still resolve."""
        path = tmp_path / "dir with space" / "cache.db"
        path.parent.mkdir()
        sqlite3.connect(path).close()

        conn = blackout_report.open_readonly(str(path))
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("CREATE TABLE t (x INTEGER)")
        finally:
            conn.close()
