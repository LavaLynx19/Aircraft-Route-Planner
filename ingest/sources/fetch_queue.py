"""Fetch-queue priority shared by both fetchers (2026-09-10 review L6).

Each fetcher selects its own candidates — a tier filter for OpenSky, a day-half
lookup for AeroDataBox — and they share only the ordering rule below. It used to
live in two SQL `ORDER BY` clauses, and the 2026-09-10 starvation fix had to be
applied to both by hand.

Named `fetch_queue`, not `queue`: the agents run with `ingest/sources` as the
working directory and conftest puts it first on `sys.path`, so a local
`queue.py` would shadow the stdlib module that `concurrent.futures` and
`logging.handlers` import.
"""


def order_due(rows: list) -> list:
    """Sort `(icao, rank, last_attempt, ...)` rows into fetch priority.

    Never-attempted first, then longest since the last attempt, ties broken by
    traffic rank. This is exactly the order SQLite gave for
    `ORDER BY (last_attempt IS NULL) DESC, last_attempt ASC, rank ASC`, including
    a NULL rank sorting first. `last_attempt` is the ISO-8601 string both
    fetchers write, compared as a string just as SQLite compared it.

    Priority is by last *attempt*, not last success. Ordering on last success
    starved both queues: an airport the source cannot see returns 'empty' or
    fails forever, never acquires a success timestamp, and so stays permanently
    ahead of everything else. Measured 2026-09-10 — 41 OpenSky hub airports
    retried up to 8 times each with zero yield while KATL, KORD and EGLL went
    unfetched for 67 days; on the schedules side UZTT failed 5 of 5 and was
    re-queued every run.

    The caller filters and truncates afterwards. Doing it inside a `LIMIT`-ed
    query would silently return fewer airports than the budget whenever some
    are skipped.
    """
    return sorted(
        rows,
        key=lambda r: (
            r[2] is not None,
            r[2] or "",
            r[1] is not None,
            r[1] if r[1] is not None else 0,
        ),
    )
