"""Blackout-set yield by country and sweep — the free signal of ARCHITECTURE.md §4.1.7.

Decision 4 of §4.1.7 splits blackout-region evidence into what arrives for free and
what costs credits. This is the free half: `schedules_fetch_log` accumulates ~19
visits a day across the 443 blackout airports, and this report turns it into
"does traffic there still look the way it did?" without anyone writing an ad-hoc
query — the gap that let the original partition go unverified (Sep review M6).

It answers a traffic question, not a coverage one. Whether OpenSky's receivers can
now see a region needs a paid probe (§4.1.7, Decision 5); nothing here spends one.

A sweep is each airport's k-th resolved visit (ok or empty), so sweep k compares the
same airports at the same point in their history. An error is retried the next day and
counts toward the sweep it failed to resolve, not as a sweep of its own. Successive sweeps sample opposite 12-hour halves of the
day, which changes yield on its own; compare sweep k with sweep k+2 like-for-like.

No thresholds and no alerts: what counts as a *sharp shift* is an open parameter of
§4.1.7, to be set from this report once enough sweeps exist.

Run:  python3 ingest/sources/blackout_report.py   (read-only; never writes)
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections import defaultdict

from config import SQLITE_PATH

# Below this many visits a rate is shown but marked: a single country-sweep cell of
# three visits can swing from 0% to 67% empty on one airport.
MIN_RELIABLE_VISITS = 5


def open_readonly(path: str) -> sqlite3.Connection:
    """Open the cache so that any write raises, rather than trusting the code not to.

    The URI form percent-encodes the path, which matters here: the repository lives
    under a directory with a space in its name.
    """
    return sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def visits(conn: sqlite3.Connection) -> list:
    """Every logged visit as `(country, icao, fetched_at, status, flight_count, day_half)`."""
    return conn.execute(
        """
        SELECT COALESCE(sa.country, '?'), sl.airport_icao, sl.fetched_at, sl.status,
               COALESCE(sl.flight_count, 0), sl.day_half
        FROM schedules_fetch_log sl
        JOIN schedules_airports sa ON sa.icao = sl.airport_icao
        ORDER BY sl.airport_icao, sl.fetched_at
        """
    ).fetchall()


def summarise(rows: list) -> dict:
    """Aggregate visits into `{(country, sweep): stats}`; sweeps as in the module doc.

    Rows per ok counts stored rows over successful visits only, the unit §4.1.7 ranks
    on; empty and error visits count toward the visit total and their own rates.
    """
    resolved: dict = defaultdict(int)
    cells: dict = defaultdict(
        lambda: {"visits": 0, "ok": 0, "empty": 0, "error": 0, "rows": 0, "half1": 0}
    )
    for country, icao, fetched_at, status, flight_count, day_half in sorted(
        rows, key=lambda r: (r[1], r[2])
    ):
        cell = cells[(country, resolved[icao] + 1)]
        cell["visits"] += 1
        cell["half1"] += 1 if day_half == 1 else 0
        if status == "ok":
            cell["ok"] += 1
            cell["rows"] += flight_count
        elif status == "empty":
            cell["empty"] += 1
        else:
            cell["error"] += 1
            continue
        resolved[icao] += 1
    return dict(cells)


def _cell_text(cell: dict | None) -> str:
    if not cell:
        return f"{'':>28}"
    n = cell["visits"]
    flag = "*" if n < MIN_RELIABLE_VISITS else " "
    per_ok = f"{cell['rows'] / cell['ok']:.1f}" if cell["ok"] else "-"
    return (
        f"{n:>4}{flag} {100 * cell['ok'] / n:>4.0f}% {100 * cell['empty'] / n:>4.0f}% "
        f"{per_ok:>6} {100 * cell['half1'] / n:>4.0f}%"
    )


def render(summary: dict) -> list:
    """The report as lines: one row per country, one column group per sweep."""
    if not summary:
        return ["No schedules visits logged yet."]
    sweeps = sorted({sweep for _, sweep in summary})
    countries = sorted(
        {country for country, _ in summary},
        key=lambda c: -sum(v["visits"] for (cc, _), v in summary.items() if cc == c),
    )
    header = f"{'country':<14}" + "".join(f"  {'sweep ' + str(k):^28}" for k in sweeps)
    sub = f"{'':<14}" + "".join(
        f"  {'visits':>5} {'ok':>5} {'empty':>5} {'rows/ok':>6} {'half1':>5}"
        for _ in sweeps
    )
    lines = [header, sub]
    for country in countries:
        lines.append(
            f"{country[:14]:<14}"
            + "".join(f"  {_cell_text(summary.get((country, k)))}" for k in sweeps)
        )
    totals = {}
    for (_, sweep), cell in summary.items():
        total = totals.setdefault(sweep, dict.fromkeys(cell, 0))
        for key, value in cell.items():
            total[key] += value
    lines.append(
        f"{'all':<14}" + "".join(f"  {_cell_text(totals.get(k))}" for k in sweeps)
    )
    lines.append(
        f"* fewer than {MIN_RELIABLE_VISITS} visits: shown, not reliable. "
        "Sweeps alternate day-halves (see half1), so compare sweep k with k+2."
    )
    return lines


def main() -> int:
    conn = open_readonly(SQLITE_PATH)
    try:
        print(
            "Blackout-set yield by sweep, from schedules_fetch_log (ARCHITECTURE.md §4.1.7)"
        )
        print(
            "A sweep is each airport's k-th resolved visit. Read-only: this spends no API units.\n"
        )
        for line in render(summarise(visits(conn))):
            print(line)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
