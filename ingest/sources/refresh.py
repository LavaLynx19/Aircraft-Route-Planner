"""Chain entry point for the daily graph refresh (ARCHITECTURE.md §4.1.5).

Rebuilds the staging graph, then restarts whichever API servers are already
running so the rebuild actually reaches clients. Scheduled as the launchd agent
`com.aircraft-route-planner.refresh`.

`load_graph.py` stays independently runnable and keeps its own guard, on purpose:
a rebuild by hand must never restart someone's servers as a side effect (the
fetch/load separation of §4.1.1 applied one step further along the chain).

Why restart rather than reload: the Go server loads the graph once into an
immutable structure and reads it without mutexes (§9). A one-day freshness SLA
makes process restart sufficient, so the `/admin/reload` endpoint §12.1 sketches
was deliberately not built — restart keeps that invariant untouched.

The restart half must not fail worse than the staleness it exists to fix, which
drives three rules here:

- only servers already listening are touched; one deliberately stopped stays
  stopped, because starting a server nobody asked for is its own surprise
- the port must be observably free before rebinding. `run-server.sh` execs
  `go run`, whose compiled child is the real listener, so killing one and not the
  other either orphans the parent or leaves the port held — and the restart would
  then "succeed" while a stale server keeps answering
- a server that fails to come back, or comes back still serving the old snapshot,
  alarms. Replacing a stale-but-serving server with no server is worse than doing
  nothing
"""

import json
import os
import signal
import subprocess
import time
import urllib.request

import load_graph
from config import REPO_ROOT, servers_config
from runner import run_guard

HEALTH_TIMEOUT_SECONDS = 3
# `go run` compiles before it binds. A warm build cache makes that instant, but
# the cache key includes the environment, so the first build under launchd's
# minimal PATH is a full rebuild: measured 103s on 2026-09-19, against the old
# 90s budget, which reported a restart as failed while it was still compiling.
# Any toolchain or environment change re-cools the cache, so the budget carries
# the margin rather than assuming a warm one.
STARTUP_BUDGET_SECONDS = 180
SHUTDOWN_BUDGET_SECONDS = 15
POLL_INTERVAL_SECONDS = 2


def _health(port: int) -> dict:
    """GET /health, or {} if the server is not answering.

    Catching OSError covers urllib's URLError too; ValueError covers a truncated
    or non-JSON body from a server still starting up.
    """
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read()).get("data", {})
    except (OSError, ValueError):
        return {}


def _listener_pids(port: int) -> list:
    """PIDs holding `port` open for LISTEN — the authority on "is it running"."""
    out = subprocess.run(
        ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(p) for p in out.stdout.split() if p.strip().isdigit()]


def _signal_group(pid: int, sig: int) -> None:
    """Signal pid's whole process group, falling back to the bare pid.

    The group matters: `run-server.sh` execs `go run`, which spawns the compiled
    binary as a child in the same group. Signalling only the listener can leave
    the parent behind; signalling only the parent leaves the real listener holding
    the port.
    """
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _stop(port: int) -> bool:
    """Stop whatever listens on `port`. True once the port is observably free."""
    for pid in _listener_pids(port):
        _signal_group(pid, signal.SIGTERM)

    deadline = time.time() + SHUTDOWN_BUDGET_SECONDS
    while time.time() < deadline:
        if not _listener_pids(port):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)

    # Escalate once. A still-bound port means the replacement would fail to bind
    # and exit, leaving the stale server answering — the failure mode this
    # function exists to rule out.
    for pid in _listener_pids(port):
        _signal_group(pid, signal.SIGKILL)
    time.sleep(POLL_INTERVAL_SECONDS)
    return not _listener_pids(port)


def _start(profile: str) -> None:
    """Launch a server detached, so it outlives this (launchd-run) process."""
    script = os.path.join(REPO_ROOT, "scripts", "run-server.sh")
    log_path = os.path.join(REPO_ROOT, "ingest", "data", f"api-{profile}.log")
    with open(log_path, "ab") as log:
        # Fixed script path, and `profile` comes from servers_config() — never
        # from user input, so there is no injection surface here.
        subprocess.Popen(
            [script, profile],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def _wait_healthy(port: int, previous_loaded_at: str) -> dict:
    """Poll until the server answers *and* serves a newer snapshot than before.

    Answering is not enough: a server that came back on the old graph would look
    healthy while still hiding the reconciliation. Timestamps are RFC3339 UTC from
    the same source, so a lexicographic comparison is an ordering comparison. An
    empty `previous_loaded_at` (a server predating the freshness field) makes any
    stamped response newer, which is the correct reading.
    """
    deadline = time.time() + STARTUP_BUDGET_SECONDS
    while time.time() < deadline:
        data = _health(port)
        if data and data.get("graph_loaded_at", "") > previous_loaded_at:
            return data
        time.sleep(POLL_INTERVAL_SECONDS)
    return {}


def run() -> dict:
    stats = load_graph.run()
    if stats.get("skipped"):
        # Nothing was rebuilt, so there is nothing for a restart to publish.
        return stats

    ports = servers_config()
    running = {profile: port for profile, port in ports.items() if _listener_pids(port)}
    stats["servers_running"] = len(running)
    stats["restarted"] = 0
    failures = []

    if not running:
        print("--- no API server listening; nothing to restart ---")

    for profile, port in running.items():
        before = _health(port).get("graph_loaded_at", "")
        print(f"--- restarting {profile} on :{port} (was loaded {before or 'unset'})")
        if not _stop(port):
            failures.append(f"{profile}: port {port} still bound after SIGKILL")
            continue
        _start(profile)
        data = _wait_healthy(port, before)
        if not data:
            failures.append(
                f"{profile}: not healthy on :{port} within {STARTUP_BUDGET_SECONDS}s"
            )
            continue
        stats["restarted"] += 1
        print(
            f"--- {profile} back up: {data.get('edge_count')} edges, "
            f"loaded {data.get('graph_loaded_at')} ---"
        )

    stats["restart_failures"] = failures
    return stats


def classify(stats: dict) -> tuple:
    """Rebuild verdict first, then the restart half.

    A rebuild problem outranks a restart problem: if the graph was not rebuilt,
    what the servers are serving is the smaller issue.
    """
    outcome, detail = load_graph.classify(stats)
    if outcome != "OK":
        return outcome, detail

    failures = stats.get("restart_failures") or []
    if failures:
        return "RESTART_FAILED", f"{detail}; " + "; ".join(failures)
    if not stats.get("servers_running"):
        return "OK", f"{detail}; no API server running, nothing to restart"
    return "OK", f"{detail}; restarted {stats['restarted']} server(s)"


if __name__ == "__main__":
    run_guard("Daily graph refresh", run, classify, work_stage="refresh")
