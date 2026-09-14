"""Refresh-chain tests: restart verdicts and snapshot-freshness verification.

The restart half of the chain can fail worse than the staleness it fixes — a
server killed and not brought back is worse than one serving yesterday's graph —
so these cover the verdicts and the freshness check rather than the subprocess
plumbing, which is the part a unit test cannot honestly simulate.
"""

import refresh


def _rebuilt(**kw):
    """A healthy rebuild + restart, as `run()` would report it."""
    stats = {
        "raw_rows": 91234,
        "routes_valid": 45069,
        "served": 45069,
        "stale": 5874,
        "servers_running": 1,
        "restarted": 1,
        "restart_failures": [],
    }
    stats.update(kw)
    return stats


class TestClassify:
    def test_healthy_rebuild_and_restart(self):
        outcome, detail = refresh.classify(_rebuilt())
        assert outcome == "OK"
        assert "restarted 1 server(s)" in detail

    def test_no_server_running_is_still_ok(self):
        """Nothing to publish to is not a fault — the rebuild still happened.

        This is the common case on a dev machine, so alarming would make the
        chain noisy every single day it ran with the servers down.
        """
        outcome, detail = refresh.classify(_rebuilt(servers_running=0, restarted=0))
        assert outcome == "OK"
        assert "nothing to restart" in detail

    def test_a_server_that_does_not_come_back_alarms(self):
        """The failure that is worse than staleness: stale-but-serving → nothing."""
        outcome, detail = refresh.classify(
            _rebuilt(restart_failures=["opensky: not healthy on :8081 within 90s"])
        )
        assert outcome not in ("OK", "QUIET")
        assert "8081" in detail

    def test_a_port_still_bound_alarms(self):
        outcome, detail = refresh.classify(
            _rebuilt(restart_failures=["opensky: port 8081 still bound after SIGKILL"])
        )
        assert outcome not in ("OK", "QUIET")
        assert "still bound" in detail

    def test_rebuild_failure_outranks_restart_reporting(self):
        """If nothing was rebuilt, what the servers serve is the smaller problem."""
        outcome, detail = refresh.classify(
            {"raw_rows": 91234, "routes_valid": 0, "restart_failures": []}
        )
        assert outcome == "UNPRODUCTIVE"
        assert "validity filter" in detail

    def test_a_skipped_rebuild_stays_quiet(self):
        """Upstream still writing — no rebuild, so nothing to restart for."""
        outcome, _ = refresh.classify(
            {
                "skipped": "fetch_log still being written",
                "raw_rows": 0,
                "routes_valid": 0,
            }
        )
        assert outcome == "QUIET"


class TestWaitHealthy:
    """Answering is not enough — the snapshot has to be new.

    A server that comes back on the *old* graph reports status ok and would pass
    any naive health check, while silently still hiding the reconciliation.
    """

    def test_rejects_a_reply_carrying_the_old_snapshot(self, monkeypatch):
        monkeypatch.setattr(refresh, "STARTUP_BUDGET_SECONDS", 0.1)
        monkeypatch.setattr(refresh, "POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(
            refresh, "_health", lambda port: {"graph_loaded_at": "2026-09-13T05:00:00Z"}
        )

        assert refresh._wait_healthy(8081, "2026-09-13T05:00:00Z") == {}

    def test_accepts_a_newer_snapshot(self, monkeypatch):
        monkeypatch.setattr(refresh, "POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(
            refresh,
            "_health",
            lambda port: {
                "graph_loaded_at": "2026-09-13T06:00:00Z",
                "edge_count": 45069,
            },
        )

        data = refresh._wait_healthy(8081, "2026-09-13T05:00:00Z")
        assert data["graph_loaded_at"] == "2026-09-13T06:00:00Z"

    def test_an_unstamped_previous_server_accepts_any_stamp(self, monkeypatch):
        """Restarting a server predating the freshness field must still verify."""
        monkeypatch.setattr(refresh, "POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(
            refresh, "_health", lambda port: {"graph_loaded_at": "2026-09-13T06:00:00Z"}
        )

        assert refresh._wait_healthy(8081, "") != {}

    def test_a_server_that_never_answers_times_out(self, monkeypatch):
        monkeypatch.setattr(refresh, "STARTUP_BUDGET_SECONDS", 0.1)
        monkeypatch.setattr(refresh, "POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(refresh, "_health", lambda port: {})

        assert refresh._wait_healthy(8081, "") == {}
