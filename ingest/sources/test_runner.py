"""Run-guard and outcome-classification tests.

The guard exists so an unattended launchd run cannot fail silently, so its own
failure modes are the thing most worth testing. `test_raising_classify_*` is the
regression for the hole found on 2026-09-10: `classify` was called outside the
`try`, so a KeyError there killed the run with an unhandled traceback and no
notification — precisely the failure this module was written to prevent.
"""

import fetch_flights
import fetch_schedules
import pytest
import runner


def _ok(stats=None):
    return lambda: stats or {"ok": 1, "queued": 1}


class TestRunGuard:
    def test_clean_run_is_silent_and_exits_zero(self, notified):
        runner.run_guard("T", _ok(), lambda s: ("OK", ""))
        assert notified == []

    def test_quiet_outcome_does_not_alarm(self, notified):
        """Quota exhaustion is an expected monthly end state, not a fault."""
        runner.run_guard("T", _ok(), lambda s: ("QUIET", "quota exhausted"))
        assert notified == []

    def test_unproductive_outcome_alarms(self, notified):
        with pytest.raises(SystemExit) as exc:
            runner.run_guard("T", _ok(), lambda s: ("UNPRODUCTIVE", "0 of 19"))
        assert exc.value.code == 1
        assert "0 of 19" in notified[0]

    def test_raising_run_fn_alarms_and_names_the_stage(self, notified):
        def boom():
            raise ConnectionResetError(54, "Connection reset by peer")

        with pytest.raises(SystemExit) as exc:
            runner.run_guard("T", boom, lambda s: ("OK", ""))
        assert exc.value.code == 1
        assert "in fetch" in notified[0]
        assert "ConnectionResetError" in notified[0]

    def test_raising_classify_alarms_instead_of_escaping(self, notified):
        """The 2026-09-10 hole: this used to exit with an unhandled traceback."""

        def bad_classify(stats):
            raise KeyError("monthly_remaining")

        with pytest.raises(SystemExit) as exc:
            runner.run_guard("T", _ok(), bad_classify)
        assert exc.value.code == 1
        assert "in classify" in notified[0]
        assert "KeyError" in notified[0]

    def test_work_stage_names_the_job_that_failed(self, notified):
        """The loader passes "rebuild": an unattended failure must not say "fetch"."""

        def boom():
            raise RuntimeError("staging Neo4j unreachable")

        with pytest.raises(SystemExit):
            runner.run_guard("T", boom, lambda s: ("OK", ""), work_stage="rebuild")
        assert "in rebuild" in notified[0]

    def test_stage_distinguishes_fetch_from_classify_failures(self, notified):
        """A reporting bug must not be mistaken for a collection failure."""

        def boom():
            raise RuntimeError("network")

        with pytest.raises(SystemExit):
            runner.run_guard("T", boom, lambda s: ("OK", ""))
        fetch_msg = notified[-1]

        with pytest.raises(SystemExit):
            runner.run_guard("T", _ok(), lambda s: 1 / 0)
        classify_msg = notified[-1]

        assert "in fetch" in fetch_msg and "in classify" in classify_msg


class TestOpenSkyClassify:
    def test_healthy_run(self):
        outcome, _ = fetch_flights.classify(
            {"ok": 76, "queued": 125, "stop_reason": "queue_exhausted"}
        )
        assert outcome == "OK"

    def test_nothing_due_is_quiet(self):
        outcome, _ = fetch_flights.classify(
            {"ok": 0, "queued": 0, "stop_reason": "queue_exhausted"}
        )
        assert outcome == "QUIET"

    def test_barren_run_alarms_with_the_stop_reason(self):
        outcome, detail = fetch_flights.classify(
            {"ok": 0, "queued": 125, "stop_reason": "credit_floor"}
        )
        assert outcome == "UNPRODUCTIVE"
        assert "credit_floor" in detail


class TestSchedulesClassify:
    def _stats(self, **kw):
        base = {
            "ok": 0,
            "queued": 19,
            "empty": 0,
            "error": 0,
            "quota_exhausted": False,
            "monthly_remaining": 400,
        }
        base.update(kw)
        return base

    def test_healthy_run(self):
        outcome, _ = fetch_schedules.classify(self._stats(ok=14))
        assert outcome == "OK"

    def test_quota_exhaustion_is_quiet_not_a_fault(self):
        """Near the 600-unit ceiling every remaining run legitimately does nothing.

        Alarming here would fire a false positive every month by design.
        """
        outcome, _ = fetch_schedules.classify(
            self._stats(queued=0, quota_exhausted=True)
        )
        assert outcome == "QUIET"

    def test_budget_available_but_nothing_collected_alarms(self):
        outcome, detail = fetch_schedules.classify(self._stats(ok=0, error=19))
        assert outcome == "UNPRODUCTIVE"
        assert "19" in detail

    def test_an_all_empty_run_still_alarms_and_says_so(self):
        """19 airports with no departures at all is suspicious, not quiet.

        The detail must separate empty windows from errors: they point at
        different causes (an API or coverage change versus connectivity).
        """
        outcome, detail = fetch_schedules.classify(self._stats(ok=0, empty=19))
        assert outcome == "UNPRODUCTIVE"
        assert "19 empty, 0 errors" in detail
