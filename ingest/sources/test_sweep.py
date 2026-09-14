"""Sweeper guard tests (2026-09-10 review L3).

`exceeds_blast_radius` is deliberately a pure function so it can be tested at all:
live data currently has nothing past the 60-day grace, so the guard cannot be
exercised end-to-end without waiting for a mark to age. These cases stand in for
that.
"""

from datetime import datetime, timezone

import pytest
import sweep_stale


class TestExceedsBlastRadius:
    @pytest.mark.parametrize(
        ("past_grace", "total", "expected"),
        [
            (0, 50000, False),  # nothing to delete
            (100, 50000, False),  # 0.2%, routine
            (5000, 50000, False),  # exactly 10% — at the limit, not over it
            (5001, 50000, True),  # one edge over
            (5874, 50253, True),  # a real marked population, 11.7%
            (1783, 50253, False),  # today's marked population, 3.5%
            (50253, 50253, True),  # the catastrophe the guard exists for
        ],
    )
    def test_threshold(self, past_grace, total, expected):
        assert sweep_stale.exceeds_blast_radius(past_grace, total) is expected

    def test_an_empty_graph_does_not_trip_the_guard(self):
        """0/0 must not raise, and refusing on an empty graph would be nonsense."""
        assert sweep_stale.exceeds_blast_radius(0, 0) is False

    def test_the_limit_is_a_fraction_not_a_percentage(self):
        """Guards against someone 'fixing' 0.10 to 10 and disabling the check."""
        assert 0 < sweep_stale.MAX_DELETE_FRACTION < 1


class TestCutoff:
    def test_cutoff_is_grace_days_in_the_past_and_utc(self):
        cutoff = sweep_stale.cutoff_iso()
        parsed = datetime.fromisoformat(cutoff)

        assert parsed.tzinfo is not None, "a naive cutoff would compare wrongly"
        age_days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
        assert age_days == pytest.approx(sweep_stale.GRACE_DAYS, abs=0.01)

    def test_cutoff_format_matches_what_the_loader_writes(self):
        """Both sides must stay parseable by Neo4j's datetime().

        The loader stamps `r.stale` with isoformat(timespec="seconds") and the
        sweeper builds its cutoff the same way. Review L5 was that comparing these
        as *strings* is only correct while the formats match exactly; the queries
        now compare them as temporals instead, but they still have to parse.
        """
        cutoff = sweep_stale.cutoff_iso()
        assert len(cutoff) == len("2026-09-05T14:38:04+00:00")
        assert cutoff[10] == "T"
        assert cutoff.endswith("+00:00")

    def test_a_shorter_grace_moves_the_cutoff_forward(self):
        assert sweep_stale.cutoff_iso(1) > sweep_stale.cutoff_iso(60)
