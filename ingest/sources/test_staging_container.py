"""Container lifecycle tests: the docker and driver calls are faked.

Whether launchd can really reach Docker is not something a unit test can show;
RUNBOOK §4 covers verifying that with a real launchd run.
"""

import subprocess
from types import SimpleNamespace

import load_graph
import pytest
import refresh
import staging_container
from neo4j.exceptions import AuthError, ServiceUnavailable


def _result(returncode=0, stderr=""):
    return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


class _FakeDriver:
    """Raises each queued error from verify_connectivity, then succeeds."""

    def __init__(self, errors=()):
        self.errors = list(errors)
        self.closed = False

    def verify_connectivity(self):
        if self.errors:
            raise self.errors.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setattr(
        staging_container,
        "staging_neo4j_config",
        lambda: {"uri": "bolt://x", "user": "u", "password": "p"},
    )
    monkeypatch.setattr(staging_container.time, "sleep", lambda _: None)


def _use_driver(monkeypatch, driver):
    monkeypatch.setattr(
        staging_container.GraphDatabase, "driver", lambda *a, **kw: driver
    )


class TestDockerPath:
    def test_prefers_path(self, monkeypatch):
        monkeypatch.setattr(
            staging_container.shutil, "which", lambda _: "/on/path/docker"
        )
        assert staging_container._docker() == "/on/path/docker"

    def test_falls_back_when_launchd_path_has_no_docker(self, monkeypatch):
        monkeypatch.setattr(
            staging_container.shutil,
            "which",
            lambda c: c if c == "/opt/homebrew/bin/docker" else None,
        )
        assert staging_container._docker() == "/opt/homebrew/bin/docker"

    def test_missing_everywhere_raises(self, monkeypatch):
        monkeypatch.setattr(staging_container.shutil, "which", lambda _: None)
        with pytest.raises(RuntimeError, match="docker CLI not found"):
            staging_container._docker()


class TestEnsureRunning:
    @pytest.fixture(autouse=True)
    def _docker_path(self, monkeypatch, fake_env):
        monkeypatch.setattr(staging_container, "_docker", lambda: "docker")

    def test_missing_container_names_the_fix(self, monkeypatch):
        monkeypatch.setattr(
            staging_container.subprocess,
            "run",
            lambda *a, **kw: _result(1, "Error: No such container: x"),
        )
        with pytest.raises(RuntimeError, match="docker compose -f"):
            staging_container.ensure_running()

    def test_unreachable_daemon_reports_docker_reason(self, monkeypatch):
        monkeypatch.setattr(
            staging_container.subprocess,
            "run",
            lambda *a, **kw: _result(1, "Cannot connect to the Docker daemon"),
        )
        with pytest.raises(RuntimeError, match="Cannot connect"):
            staging_container.ensure_running()

    def test_waits_through_boot_until_connected(self, monkeypatch):
        monkeypatch.setattr(
            staging_container.subprocess, "run", lambda *a, **kw: _result()
        )
        driver = _FakeDriver(
            [ServiceUnavailable("booting"), ServiceUnavailable("booting")]
        )
        _use_driver(monkeypatch, driver)
        staging_container.ensure_running()
        assert driver.errors == []
        assert driver.closed

    def test_never_ready_times_out_naming_the_error(self, monkeypatch):
        monkeypatch.setattr(
            staging_container.subprocess, "run", lambda *a, **kw: _result()
        )
        driver = _FakeDriver([ServiceUnavailable("booting")])
        _use_driver(monkeypatch, driver)
        clock = iter([0, 1, staging_container.READY_BUDGET_SECONDS + 1])
        monkeypatch.setattr(staging_container.time, "time", lambda: next(clock))
        with pytest.raises(RuntimeError, match="ServiceUnavailable"):
            staging_container.ensure_running()
        assert driver.closed

    def test_bad_password_fails_without_waiting(self, monkeypatch):
        monkeypatch.setattr(
            staging_container.subprocess, "run", lambda *a, **kw: _result()
        )
        driver = _FakeDriver([AuthError("nope"), ServiceUnavailable("unreached")])
        _use_driver(monkeypatch, driver)
        with pytest.raises(AuthError):
            staging_container.ensure_running()
        assert len(driver.errors) == 1


class TestStop:
    @pytest.fixture(autouse=True)
    def _docker_path(self, monkeypatch):
        monkeypatch.setattr(staging_container, "_docker", lambda: "docker")

    def test_gives_neo4j_time_to_checkpoint(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _result()

        monkeypatch.setattr(staging_container.subprocess, "run", fake_run)
        assert staging_container.stop() == ""
        assert calls == [["docker", "stop", "-t", "120", staging_container.CONTAINER]]

    def test_missing_container_is_nothing_to_stop(self, monkeypatch):
        monkeypatch.setattr(
            staging_container.subprocess,
            "run",
            lambda *a, **kw: _result(1, "Error: No such container: x"),
        )
        assert staging_container.stop() == ""

    def test_docker_failure_is_returned(self, monkeypatch):
        monkeypatch.setattr(
            staging_container.subprocess,
            "run",
            lambda *a, **kw: _result(1, "Cannot connect to the Docker daemon"),
        )
        assert "Cannot connect" in staging_container.stop()

    def test_timeout_is_returned_not_raised(self, monkeypatch):
        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(staging_container.subprocess, "run", fake_run)
        assert "TimeoutExpired" in staging_container.stop()

    def test_missing_cli_is_returned_not_raised(self, monkeypatch):
        def no_docker():
            raise RuntimeError("docker CLI not found")

        monkeypatch.setattr(staging_container, "_docker", no_docker)
        assert "docker CLI not found" in staging_container.stop()


class TestRefreshLifecycle:
    @pytest.fixture
    def stops(self, monkeypatch):
        calls = []

        def fake_stop():
            calls.append(True)
            return ""

        monkeypatch.setattr(staging_container, "stop", fake_stop)
        monkeypatch.setattr(
            refresh, "servers_config", lambda: {"openflights": 8080, "opensky": 8081}
        )
        return calls

    @staticmethod
    def _listening(monkeypatch, *ports):
        monkeypatch.setattr(
            refresh, "_listener_pids", lambda port: [1] if port in ports else []
        )

    def test_stops_when_no_api_is_listening(self, monkeypatch, stops):
        self._listening(monkeypatch)
        monkeypatch.setattr(
            load_graph, "run", lambda before_connect: {"raw_rows": 1, "routes_valid": 1}
        )
        refresh.run()
        assert stops == [True]

    def test_stays_up_when_opensky_api_is_listening(self, monkeypatch, stops):
        self._listening(monkeypatch, 8081)
        monkeypatch.setattr(
            load_graph, "run", lambda before_connect: {"skipped": "busy"}
        )
        refresh.run()
        assert stops == []

    def test_openflights_api_does_not_hold_staging_up(self, monkeypatch, stops):
        self._listening(monkeypatch, 8080)
        monkeypatch.setattr(
            load_graph, "run", lambda before_connect: {"skipped": "busy"}
        )
        refresh.run()
        assert stops == [True]

    def test_skipped_rebuild_never_starts_it_but_still_stops(self, monkeypatch, stops):
        self._listening(monkeypatch)
        monkeypatch.setattr(
            load_graph, "run", lambda before_connect: {"skipped": "busy"}
        )
        starts = []
        monkeypatch.setattr(
            staging_container, "ensure_running", lambda: starts.append(True)
        )
        refresh.run()
        assert starts == []
        assert stops == [True]

    def test_rebuild_is_given_the_start_hook(self, monkeypatch, stops):
        self._listening(monkeypatch)
        seen = []

        def fake_run(before_connect):
            seen.append(before_connect)
            return {"skipped": "busy"}

        monkeypatch.setattr(load_graph, "run", fake_run)
        refresh.run()
        assert seen == [staging_container.ensure_running]

    def test_failed_rebuild_still_stops_and_keeps_its_error(self, monkeypatch, stops):
        self._listening(monkeypatch)

        def boom(before_connect):
            raise ValueError("rebuild broke")

        monkeypatch.setattr(load_graph, "run", boom)
        with pytest.raises(ValueError, match="rebuild broke"):
            refresh.run()
        assert stops == [True]

    def test_failed_stop_is_reported_without_changing_the_outcome(self, monkeypatch):
        monkeypatch.setattr(
            refresh, "servers_config", lambda: {"openflights": 8080, "opensky": 8081}
        )
        self._listening(monkeypatch)
        monkeypatch.setattr(staging_container, "stop", lambda: "daemon gone")
        monkeypatch.setattr(
            load_graph,
            "run",
            lambda before_connect: {
                "raw_rows": 5,
                "routes_valid": 5,
                "served": 5,
                "stale": 0,
            },
        )
        stats = refresh.run()
        outcome, detail = refresh.classify(stats)
        assert outcome == "OK"
        assert "daemon gone" in detail
