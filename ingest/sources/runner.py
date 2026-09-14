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

import subprocess
import sys
from datetime import datetime, timezone

NOTIFY_TITLE = "aircraft-route-planner"


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
