"""Start and stop the staging Neo4j container around the daily refresh (§4.1.5).

Neo4j is the only thing the scheduled jobs need Docker for, and only the refresh
needs it. Leaving it up all day keeps Docker's Resource Saver from ever engaging,
so `refresh.py` holds the container only for as long as it is in use.

`docker start`/`stop` rather than `docker compose`: compose would read
`docker-compose.staging.yml` and `.env` from under ~/Desktop, adding a TCC
dependency for a separately spawned binary (RUNBOOK §4, Full Disk Access). The
container already exists, so nothing here needs those files. The cost is that a
wiped container has to be recreated by hand, which the error message says.

Docker Desktop itself is deliberately left alone: if the daemon is unreachable the
start fails and the run alarms through `run_guard`.
"""

import shutil
import subprocess
import time

from config import staging_neo4j_config
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError

CONTAINER = "aircraft-route-neo4j-staging"
# launchd's bare PATH holds neither install, the same trap run-server.sh hit with Go.
DOCKER_FALLBACKS = ("/usr/local/bin/docker", "/opt/homebrew/bin/docker")
READY_BUDGET_SECONDS = 180
POLL_INTERVAL_SECONDS = 2
# Waking Docker from Resource Saver happens inside this call, so it needs some room.
START_TIMEOUT_SECONDS = 120
# Docker's default grace is 10s, after which it SIGKILLs. That can catch Neo4j
# mid-checkpoint and force a recovery on the next start.
STOP_GRACE_SECONDS = 120
STOP_TIMEOUT_SECONDS = STOP_GRACE_SECONDS + 30


def _docker() -> str:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in DOCKER_FALLBACKS:
        if shutil.which(candidate):
            return candidate
    raise RuntimeError(
        "docker CLI not found on PATH or in " + ", ".join(DOCKER_FALLBACKS)
    )


def _wait_ready() -> None:
    """Block until Neo4j accepts an authenticated connection.

    `docker start` returns when the process launches, well before bolt answers. A
    wrong password will not fix itself by waiting, so it fails at once.
    """
    cfg = staging_neo4j_config()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    last_error = "no connection attempt completed"
    deadline = time.time() + READY_BUDGET_SECONDS
    try:
        while time.time() < deadline:
            try:
                driver.verify_connectivity()
                return
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001 - the driver raises several types
                # while the server boots; the timeout below is what bounds this.
                last_error = type(e).__name__
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        driver.close()
    raise RuntimeError(
        f"{CONTAINER} not ready within {READY_BUDGET_SECONDS}s: {last_error}"
    )


def ensure_running() -> None:
    """Start the container (a no-op if it is up) and wait until it is usable."""
    out = subprocess.run(
        [_docker(), "start", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
        timeout=START_TIMEOUT_SECONDS,
    )
    if out.returncode != 0:
        reason = out.stderr.strip()
        if "No such container" in reason:
            raise RuntimeError(
                f"{CONTAINER} does not exist — recreate it with "
                "`docker compose -f docker-compose.staging.yml up -d`"
            )
        raise RuntimeError(f"docker start failed: {reason}")
    _wait_ready()


def stop() -> str:
    """Stop the container. Returns "" on success, else the reason.

    Never raises: this runs in a `finally` after the rebuild, and a failed stop
    costs RAM, not data, so it must not replace the rebuild's own verdict.
    """
    try:
        out = subprocess.run(
            [_docker(), "stop", "-t", str(STOP_GRACE_SECONDS), CONTAINER],
            capture_output=True,
            text=True,
            check=False,
            timeout=STOP_TIMEOUT_SECONDS,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}"
    if out.returncode == 0 or "No such container" in out.stderr:
        return ""
    return out.stderr.strip() or f"docker stop exited {out.returncode}"
