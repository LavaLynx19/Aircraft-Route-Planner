"""Shared run scaffolding for the scheduled fetchers (ARCHITECTURE.md §4.1.5).

Both fetchers run unattended under launchd, where the failure mode that matters
is not a crash but a run that looks fine and collects nothing. Every guard here
exists because of an observed failure:

- line-buffered stdout — launchd block-buffers a redirected stream, so a hung or
  SIGTERM'd run previously lost every progress line it had printed
- run markers + non-zero exit — a fetch stalled for 57 consecutive days while
  reporting success, because nothing distinguished "ran" from "collected"
- desktop notification — the only channel that surfaces a background failure
  without someone thinking to read the log

Callers supply a `classify` function so each fetcher decides what "collected
nothing" means for its own source.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone

NOTIFY_TITLE = "aircraft-route-planner"
CAFFEINATE = "/usr/bin/caffeinate"


def hold_awake() -> None:
    """Ask macOS not to sleep until this process exits.

    launchd fires an overdue job in whatever wake it gets, which can be a
    seconds-long DarkWake. The Mac then sleeps mid-run, cutting network calls and
    stalling the refresh (2026-11-02 and 2026-11-03). `-w` ties the assertion to
    our PID, so it ends however the run exits. `-i` blocks idle sleep and `-s`
    blocks system sleep on AC power. macOS may still sleep on battery or with the
    lid closed, which is why the waits also use the monotonic clock.

    A spawned child, not a plist wrapper: Full Disk Access is granted to the
    python3 binary the plist names, and wrapping it in caffeinate risks the TCC
    failure RUNBOOK §4 describes. caffeinate touches no files, so it needs no grant.
    """
    try:
        subprocess.Popen(
            [CAFFEINATE, "-i", "-s", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        # An optimisation, not a precondition: the run can still succeed awake.
        print(f"--- could not hold the Mac awake: {type(e).__name__}: {e} ---")


def notify(message: str) -> None:
    """Surface a failure on the desktop — a launchd run is otherwise invisible."""
    # Message goes through argv, not the script text: AppleScript has no \uXXXX
    # escape, so any non-ASCII in an exception would break an interpolated literal.
    subprocess.run(
        [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            f'display notification (item 1 of argv) with title "{NOTIFY_TITLE}"',
            "-e",
            "end run",
            "--",
            message,
        ],
        check=False,
    )


def run_guard(label: str, run_fn, classify, work_stage: str = "fetch") -> None:
    """Run `run_fn`, print run markers, and alarm on failure or a barren run.

    `classify(stats)` returns (outcome, detail) where outcome is:
      "OK"            — collected normally, exit 0, silent
      "QUIET"         — nothing to do this run, exit 0, silent (not a fault)
      anything else   — treated as an alarm: printed, notified, exit 1

    `work_stage` names what `run_fn` does, and appears in the failure message. It
    defaults to "fetch" for the two collectors; the graph loader passes "rebuild"
    so an unattended failure names the job that actually broke instead of a word
    borrowed from a fetcher.
    """
    # hasattr guard: under a test harness stdout is replaced by a capture
    # object that need not implement reconfigure. Line buffering is an
    # optimisation for the launchd log, never a correctness requirement.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"--- run started {started} ---")
    hold_awake()

    # `classify` runs inside the guard too. It was outside until 2026-09-12, which
    # left the guard with a hole in exactly the shape it exists to prevent: a
    # KeyError on a stats key a fetcher forgot to set would kill the run with an
    # unhandled traceback and no notification. `stage` names which half failed so
    # a reporting bug is not mistaken for a collection failure.
    stage = work_stage
    try:
        stats = run_fn()
        stage = "classify"
        outcome, detail = classify(stats)
    except Exception as e:  # noqa: BLE001 - deliberate: this guard is the last
        # line of defence for an unattended run. Narrowing it would let an
        # unanticipated exception exit silently, which is the exact failure
        # this module exists to prevent.
        detail = f"{type(e).__name__}: {e}"
        print(f"--- run FAILED in {stage}: {detail} ---")
        notify(f"{label} failed in {stage} — {detail}")
        sys.exit(1)
    if outcome in ("OK", "QUIET"):
        print(f"--- run OK: {detail} ---" if detail else "--- run OK ---")
        return
    print(f"--- run {outcome}: {detail} ---")
    notify(f"{label} — {detail}")
    sys.exit(1)
