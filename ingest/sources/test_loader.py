"""Graph-loader tests: timestamp parsing, type mapping, recency, validity."""

import time
from collections import defaultdict

import load_graph
import pytest
from conftest import DAY, add_airport, add_attempt, add_schedule_attempt


class TestParseScheduled:
    """Sep review M2, fixed 2026-09-13.

    These cases previously asserted the *opposite*: that seconds, a `T` separator
    and a numeric offset all returned None. That encoded the fragility as intended
    behaviour — a single upstream format change would have dropped every schedule
    row. Accepting them is the fix, so the assertions are inverted deliberately.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-09-05 11:30Z", (2026, 9, 5, 11, 30)),  # the shape stored today
            ("2026-09-05 11:30:00Z", (2026, 9, 5, 11, 30)),  # seconds
            ("2026-09-05T11:30Z", (2026, 9, 5, 11, 30)),  # ISO T separator
            ("2026-09-05T11:30:00Z", (2026, 9, 5, 11, 30)),  # both
            ("2026-09-05 11:30+00:00", (2026, 9, 5, 11, 30)),  # numeric offset
            ("2026-09-05T11:30:00+00:00", (2026, 9, 5, 11, 30)),  # full isoformat
            ("2026-09-05 11:30z", (2026, 9, 5, 11, 30)),  # lowercase suffix
            ("  2026-09-05 11:30Z  ", (2026, 9, 5, 11, 30)),  # padded
        ],
    )
    def test_accepts_every_plausible_iso_shape(self, value, expected):
        dt = load_graph._parse_scheduled(value)
        assert dt is not None, f"{value!r} should parse"
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == expected
        assert dt.tzinfo is not None, "a naive result would break UTC arithmetic"

    def test_a_naive_stamp_is_treated_as_utc(self):
        """The column is named scheduled_utc and the API documents UTC."""
        dt = load_graph._parse_scheduled("2026-09-05 11:30")
        assert dt is not None
        assert dt.utcoffset().total_seconds() == 0

    @pytest.mark.parametrize(
        "value", ["not a time", "", "   ", None, 17, "2026-13-45 99:99Z"]
    )
    def test_still_rejects_what_is_genuinely_unparseable(self, value):
        assert load_graph._parse_scheduled(value) is None


class TestModelToIcaotype:
    def test_maps_names_from_planes_dat(self):
        mapping = load_graph.model_to_icaotype()
        assert mapping["boeing 737-800"] == "B738"

    def test_maps_supplement_and_alias_spellings(self):
        mapping = load_graph.model_to_icaotype()
        assert mapping["airbus a320 neo"] == "A20N"  # MODEL_ALIASES
        assert mapping["airbus a320neo"] == "A20N"  # SUPPLEMENT_TYPES name

    @pytest.mark.parametrize("generic", ["boeing 737", "airbus a330", "boeing 777"])
    def test_leaves_ambiguous_generics_unmapped(self, generic):
        """Deliberate: picking a variant would invent precision the source lacks."""
        assert generic not in load_graph.model_to_icaotype()


class TestRecencyDaysByAirport:
    def test_derives_twice_the_observed_interval(self, conn):
        add_airport(conn, "AAAA", tier="hub")
        for days in (0, 20, 40):  # median gap 20 days
            add_attempt(conn, "AAAA", status="ok", days_ago=days)

        assert load_graph.recency_days_by_airport(conn)["AAAA"] == pytest.approx(
            40.0, abs=0.5
        )

    def test_floors_at_the_constant(self, conn):
        """A fast cadence must not shrink the window below RECENCY_DAYS."""
        add_airport(conn, "BBBB", tier="hub")
        for days in (0, 2, 4):
            add_attempt(conn, "BBBB", status="ok", days_ago=days)

        assert (
            load_graph.recency_days_by_airport(conn)["BBBB"] == load_graph.RECENCY_DAYS
        )

    @pytest.mark.parametrize(
        ("tier", "expected"),
        [("hub", 14.0), ("tail", 61.0)],
    )
    def test_falls_back_to_tier_cycle_below_two_samples(self, conn, tier, expected):
        add_airport(conn, "CCCC", tier=tier)
        add_attempt(conn, "CCCC", status="ok", days_ago=5)  # one sample only

        assert load_graph.recency_days_by_airport(conn)["CCCC"] == expected

    def test_empty_attempts_do_not_set_a_window(self, conn):
        """Only successes describe a cadence; an empty run observed nothing."""
        add_airport(conn, "DDDD", tier="hub")
        add_attempt(conn, "DDDD", status="empty", days_ago=1)
        add_attempt(conn, "DDDD", status="empty", days_ago=30)

        assert load_graph.recency_days_by_airport(conn)["DDDD"] == 14.0

    def test_schedule_airports_get_their_own_fallback(self, conn):
        conn.execute(
            "INSERT INTO schedules_airports (icao, iata, country, rank) "
            "VALUES ('ZSPD','PVG','China',0)"
        )
        conn.commit()
        add_schedule_attempt(conn, "ZSPD", status="ok", days_ago=1)

        assert load_graph.recency_days_by_airport(conn)["ZSPD"] == 47.0


def _route(weeks, last_seen_days_ago, flights=1):
    airport = {"iata": "AAA", "lat": 0.0, "lon": 0.0}
    dst = {"iata": "BBB", "lat": 1.0, "lon": 1.0}
    return {
        ("AAA", "BBB", "738", "XX"): {
            "flights": flights,
            "weeks": {f"2026-{n:02d}" for n in range(1, weeks + 1)},
            "days": {1},
            "last_seen": time.time() - last_seen_days_ago * DAY,
            "src": airport,
            "dst": dst,
            "origin_icao": "AAAA",
            "icaotype": "B738",
        }
    }


class TestShapeValid:
    def test_three_weeks_passes_regardless_of_age(self):
        m = defaultdict(int)
        out = load_graph.shape_valid(_route(3, 400), {}, {}, "opensky", m)
        assert len(out) == 1
        assert m["opensky_filtered_out"] == 0

    def test_one_week_passes_when_inside_the_recency_window(self):
        m = defaultdict(int)
        out = load_graph.shape_valid(_route(1, 5), {"AAAA": 14.0}, {}, "opensky", m)
        assert len(out) == 1

    def test_one_week_fails_outside_the_recency_window(self):
        m = defaultdict(int)
        out = load_graph.shape_valid(_route(1, 40), {"AAAA": 14.0}, {}, "opensky", m)
        assert out == []
        assert m["opensky_filtered_out"] == 1

    def test_per_airport_window_overrides_the_constant(self):
        """A tail airport's wider window keeps a route the constant would drop."""
        m = defaultdict(int)
        out = load_graph.shape_valid(_route(1, 40), {"AAAA": 61.0}, {}, "opensky", m)
        assert len(out) == 1

    def test_records_the_sample_behind_the_frequency(self):
        """observations/days_sampled make a coarse 14 distinguishable from a real one."""
        m = defaultdict(int)
        out = load_graph.shape_valid(
            _route(1, 1), {"AAAA": 14.0}, {"AAAA": 0.5}, "aerodatabox", m
        )
        assert out[0]["observations"] == 1
        assert out[0]["days_sampled"] == 0.5
        assert out[0]["weekly_frequency"] == 14  # 1 flight / 0.5 days * 7
        assert out[0]["source"] == "aerodatabox"

    def test_frequency_scales_with_the_sampled_window(self):
        """The same observation over a full day must not read as twice-daily."""
        m = defaultdict(int)
        out = load_graph.shape_valid(
            _route(1, 1), {"AAAA": 14.0}, {"AAAA": 1.0}, "opensky", m
        )
        assert out[0]["weekly_frequency"] == 7


class TestParseIso:
    def test_accepts_the_format_the_fetchers_write(self):
        dt = load_graph._parse_iso("2026-09-12T05:00:00+00:00")
        assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 9, 12, 5)
        assert dt.tzinfo is not None

    def test_assumes_utc_when_no_offset_is_present(self):
        """A naive stamp must not become naive-vs-aware arithmetic in the gate."""
        assert load_graph._parse_iso("2026-09-12T05:00:00").tzinfo is not None

    @pytest.mark.parametrize("value", ["not a time", "", None, 17])
    def test_rejects_anything_unexpected(self, value):
        """Returns None so a format drift reads as 'no upstream data', not a crash."""
        assert load_graph._parse_iso(value) is None


class TestUpstreamState:
    """The ordering gate (§4.1.5 *Refresh chain*).

    launchd has no dependency graph, so these cases are what stands between a
    scheduled rebuild and a half-written SQLite day.
    """

    def test_empty_logs_are_neither_busy_nor_stale(self, conn):
        """A fresh clone has the tables but no rows — nothing to block or alarm on."""
        state = load_graph.upstream_state(conn)
        assert state["busy"] is False
        assert state["stale_hours"] is None

    def test_a_write_seconds_ago_reads_as_busy(self, conn):
        add_attempt(conn, "AAAA", status="ok", days_ago=0)

        state = load_graph.upstream_state(conn)
        assert state["busy"] is True
        assert state["busy_table"] == "fetch_log"

    def test_yesterdays_run_is_quiescent_and_not_yet_stale(self, conn):
        """The normal path: fetchers done hours ago, well inside the 36h window."""
        add_attempt(conn, "AAAA", status="ok", days_ago=1)

        state = load_graph.upstream_state(conn)
        assert state["busy"] is False
        assert state["stale_hours"] is None

    def test_long_silence_reports_hours_so_it_can_alarm(self, conn):
        """This is the independent check that the collectors have stopped."""
        add_attempt(conn, "AAAA", status="ok", days_ago=3)

        assert load_graph.upstream_state(conn)["stale_hours"] == pytest.approx(
            72.0, abs=1.0
        )

    def test_the_schedules_log_counts_too(self, conn):
        add_schedule_attempt(conn, "ZSPD", status="ok", days_ago=0)

        state = load_graph.upstream_state(conn)
        assert state["busy"] is True
        assert state["busy_table"] == "schedules_fetch_log"

    def test_a_recent_run_in_either_log_clears_staleness(self, conn):
        """Staleness is about the newest write overall, not per-source."""
        add_attempt(conn, "AAAA", status="ok", days_ago=5)
        add_schedule_attempt(conn, "ZSPD", status="ok", days_ago=1)

        assert load_graph.upstream_state(conn)["stale_hours"] is None


class TestLoaderClassify:
    def test_a_skipped_run_is_quiet_not_a_fault(self):
        """Skipping a busy upstream is routine — launchd fires all agents on wake."""
        outcome, detail = load_graph.classify(
            {
                "skipped": "fetch_log still being written",
                "raw_rows": 0,
                "routes_valid": 0,
            }
        )
        assert outcome == "QUIET"
        assert "still being written" in detail

    def test_an_empty_cache_is_quiet(self):
        outcome, _ = load_graph.classify({"raw_rows": 0, "routes_valid": 0})
        assert outcome == "QUIET"

    def test_raw_rows_but_no_valid_routes_alarms(self):
        """The silent failure this classify exists for: a rebuild that writes nothing."""
        outcome, detail = load_graph.classify({"raw_rows": 91234, "routes_valid": 0})
        assert outcome == "UNPRODUCTIVE"
        assert "91234" in detail

    def test_healthy_rebuild_reports_the_post_state(self):
        outcome, detail = load_graph.classify(
            {"raw_rows": 91234, "routes_valid": 45069, "served": 45069, "stale": 5874}
        )
        assert outcome == "OK"
        assert "45069 edges served" in detail and "5874" in detail

    def test_a_successful_rebuild_on_stopped_collectors_still_alarms(self):
        """The rebuild worked; the pipeline feeding it did not. That must surface."""
        outcome, detail = load_graph.classify(
            {
                "raw_rows": 91234,
                "routes_valid": 45069,
                "served": 45069,
                "stale": 5874,
                "stale_hours": 72.0,
            }
        )
        assert outcome not in ("OK", "QUIET")
        assert "72h" in detail
