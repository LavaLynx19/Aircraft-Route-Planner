"""Sweeper — deletes ROUTE edges that have been marked stale past the grace period.

Deliberately a separate service from the loader (ARCHITECTURE.md §4.1.6). The
loader marks and never deletes; this is the only thing in the pipeline that
removes data, so it stays independently schedulable, independently reviewable,
and pausable without stopping ingestion.

**When a mark reaches clients (corrected 2026-09-13).** The Go engine filters
`r.stale IS NULL` when it *loads the graph at startup*, not per request, so a mark
applied while a server is running does not reach that server's clients until it
restarts — which the daily refresh chain now does at 11:00 (§4.1.5 *Refresh
chain*). This file previously claimed a marked edge "stops being served the moment
the loader marks it", which is true of the database and false of any already-running
server. Sweeping remains housekeeping: it reclaims storage and stops the graph
accumulating tombstones.

Grace period exists because absence of observation is not evidence of absence:
a tail-tier airport is only revisited every ~30.7 days, so an edge unobserved
for one cycle may simply not have been looked at yet. Re-observation clears the
mark, so only routes genuinely gone for two full cycles are removed.

    python3 ingest/sources/sweep_stale.py                  # report only, deletes nothing
    python3 ingest/sources/sweep_stale.py --delete         # delete past-grace edges
    python3 ingest/sources/sweep_stale.py --delete --force # override the blast-radius guard
"""

import sys
from datetime import datetime, timedelta, timezone

from config import staging_neo4j_config
from neo4j import GraphDatabase

GRACE_DAYS = 60  # ~2x the longest tier cycle (tail, 30.7d)

# Refuse to delete more than this share of all ROUTE edges in one sweep.
#
# The grace period stops an *accidental* same-day deletion, but nothing stopped a
# bad reconciliation from being made permanent: a loader bug that marked every edge
# would, 60 days later, hand this script the whole graph to delete (2026-09-10
# review L3).
#
# 10% is deliberately tight, and it will probably refuse the first real sweep. For
# scale, marked populations seen so far are 5,874 of 50,253 edges (11.7%) and 1,783
# (3.5%) — and past-grace is a subset of marked, since re-observation clears a mark.
# Nothing has ever been swept, so the first large deletion is exactly the one a
# human should look at rather than have happen unattended; `--force` exists for
# when the marks are confirmed correct. Raise this only with a measurement to
# justify it.
MAX_DELETE_FRACTION = 0.10


def cutoff_iso(grace_days: int = GRACE_DAYS) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=grace_days)).isoformat(
        timespec="seconds"
    )


def exceeds_blast_radius(past_grace: int, total: int) -> bool:
    """True when deleting `past_grace` of `total` edges is too much for one sweep.

    Pure and separate from the Neo4j calls so it can be tested directly: live data
    currently has nothing past grace, so there is no way to exercise the guard
    end-to-end without waiting 60 days for a mark to age.
    """
    if total <= 0:
        return False
    return past_grace / total > MAX_DELETE_FRACTION


def survey(session, cutoff: str) -> dict:
    # datetime() rather than a string `<`: comparing ISO-8601 lexicographically is
    # only correct while every writer emits the identical format, and it fails
    # *silently* if one ever writes a `Z` suffix or another offset (review L5).
    # A temporal comparison is format-tolerant, and a value it cannot parse raises
    # instead of quietly comparing false — the right direction for a predicate that
    # gates deletion. Verified against live data: identical population to the
    # string comparison, so no migration of existing marks is needed.
    row = session.run(
        """
        MATCH ()-[r:ROUTE]->()
        RETURN count(r) AS total,
               count(CASE WHEN r.stale IS NOT NULL THEN 1 END) AS marked,
               count(CASE WHEN r.stale IS NOT NULL
                           AND datetime(r.stale) < datetime($cutoff) THEN 1 END)
                   AS past_grace
        """,
        cutoff=cutoff,
    ).single()
    return dict(row)


def sweep(session, cutoff: str) -> int:
    return session.execute_write(
        lambda tx: tx.run(
            "MATCH ()-[r:ROUTE]->() WHERE r.stale IS NOT NULL "
            "AND datetime(r.stale) < datetime($cutoff) "
            "DELETE r RETURN count(r) AS n",
            cutoff=cutoff,
        ).single()["n"]
    )


def main() -> None:
    delete = "--delete" in sys.argv
    force = "--force" in sys.argv
    cutoff = cutoff_iso()
    cfg = staging_neo4j_config()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            stats = survey(session, cutoff)
            print(
                f"ROUTE edges: {stats['total']} total, {stats['marked']} marked stale, "
                f"{stats['past_grace']} past the {GRACE_DAYS}-day grace (before {cutoff})."
            )
            if not delete:
                print("Dry run — nothing deleted. Re-run with --delete to sweep.")
                return
            if not stats["past_grace"]:
                print("Nothing past grace; no deletions.")
                return

            if exceeds_blast_radius(stats["past_grace"], stats["total"]) and not force:
                share = stats["past_grace"] / stats["total"]
                print(
                    f"REFUSING: {stats['past_grace']} of {stats['total']} edges "
                    f"({share:.1%}) are past grace, above the "
                    f"{MAX_DELETE_FRACTION:.0%} single-sweep limit."
                )
                print(
                    "A sweep this large is what a bad reconciliation looks like, and "
                    "deleting would make it permanent. Check the source/served split "
                    "with the query in RUNBOOK.md §4, then re-run with --force if the "
                    "marks are genuinely correct."
                )
                sys.exit(1)

            removed = sweep(session, cutoff)
            print(f"Deleted {removed} stale ROUTE relationships.")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
