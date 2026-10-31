# Runbook — Starting & Operating the Servers

Copy-paste commands, organized per data source. The two stacks are fully isolated and can run simultaneously. All commands from the repo root.

## 1. OpenFlights stack (old source — 2014 baseline)

```bash
# 1. Database — Neo4j bolt :7687, browser http://localhost:7474
docker compose up -d

# 2. API server → http://localhost:8080
./scripts/run-server.sh openflights

# (re)ingest baseline data — only needed after a wipe, or to roll back from a cut-over
python3 ingest/main.py

# Stop
docker compose down
```

## 2. OpenSky stack (new source — live pipeline)

```bash
# 1. Database — Neo4j bolt :7688, browser http://localhost:7475
docker compose -f docker-compose.staging.yml up -d

# 2. API server → http://localhost:8081
./scripts/run-server.sh opensky

# One-time setup after a staging wipe (constraints, airports, aircraft, lookup tables)
python3 ingest/sources/main.py

# Rebuild the graph from the accumulated SQLite cache (idempotent);
# restart the opensky server afterwards to load the new graph — needed only for a
# hand-run rebuild; the daily 11:00 refresh agent restarts running servers itself (§4)
python3 ingest/sources/load_graph.py

# Manual fetch run (normally automatic — see §4)
python3 ingest/sources/fetch_flights.py

# Stop
docker compose -f docker-compose.staging.yml down
```

## 3. Running both at once

```bash
docker compose up -d && docker compose -f docker-compose.staging.yml up -d
./scripts/run-server.sh openflights > /tmp/api-openflights.log 2>&1 &
./scripts/run-server.sh opensky     > /tmp/api-opensky.log     2>&1 &

curl -s localhost:8080/health        # openflights
curl -s localhost:8081/health        # opensky
```

## 4. Scheduled background jobs — the complete list

**Three things run on a schedule:**

| Job | What | When | Logs |
|---|---|---|---|
| launchd agent `com.aircraft-route-planner.fetch` | `fetch_flights.py` — fetches ~125 airports' flight data from OpenSky into SQLite (~3,750 of 4,000 daily credits) | daily 09:00 (runs on next wake if the Mac was asleep) | `ingest/data/fetch.log` |
| launchd agent `com.aircraft-route-planner.schedules` | `fetch_schedules.py` — AeroDataBox schedules for the 443 ADS-B-blackout airports (China/Russia/CIS), ~19 calls a day against the free tier's 600 units per period (2 units a call) | daily 10:00 | `ingest/data/schedules.log` |
| launchd agent `com.aircraft-route-planner.refresh` | `refresh.py` — rebuilds the staging graph from the SQLite cache, reconciles stale edges, then restarts whichever API servers are already running so the rebuild reaches clients. Starts the staging Neo4j container for the load and stops it afterwards unless the OpenSky API is listening. Skips quietly if a fetcher is still writing (ARCHITECTURE.md §4.1.5 *Refresh chain*) | daily 11:00 | `ingest/data/refresh.log`, servers to `ingest/data/api-<profile>.log` |

**Unit accounting is the provider's, not ours** (ARCHITECTURE.md §4.1.8, since 2026-10-03). The
fetcher reads `X-RateLimit-API-Units-Remaining`/`-Limit`/`-Reset` from each response and keeps the
last value in the one-row `schedules_quota` table. It holds no unit cost, cap or period of its own.
The ones it held until then were both wrong, which is why it sat idle from 2026-09-21 with its own
counter showing 281 units left:

- a call costs **2 units, not 1** — the 600 units are ~300 calls;
- the period is the **subscription anniversary (the 5th, ~13:35 UTC), not the calendar month**.

Do not estimate spend from `schedules_fetch_log`: `SUM(attempts)` counts calls, not units.

The schedules job reports `--- run OK: N units left, resets <date> ---`. Once the provider reports
`ADB_UNIT_RESERVE` (20) units or fewer it stops, and until the reset every run reports
`run OK: unit quota exhausted — resumes <date>` and exits 0 — that is the **expected** end state
each period, not a fault, so it deliberately does not alarm. Those runs make no call, except one
every 7 days when the stored row expires and is re-learnt from a single 429. At 19 calls a
day that state is reached about 16 days into the period (pacing is an open decision in §4.1.8). An
`UNPRODUCTIVE` exit means it had budget and still collected nothing, which is a real problem; its
detail line separates empty windows from errors, since they point at different causes.

Reading the start line and the stored row:

- `units unknown` — nothing usable is stored (first run, or the row is more than 7 days old). The
  run proceeds and the first response sets the figure.
- A row whose `reset_at` has passed counts as a full limit again.
- A timeout, connection reset or per-second throttle carries no unit headers and leaves the row
  as it was, so `observed_at` is the time of the last response that did.
- A quota stop logs the provider's own message: `  <ICAO>: HTTP 429: {"message":"You have exceeded
  the MONTHLY quota for API Units ..."}`.

A window with no departures is `empty`, not an error: the API answers **HTTP 204** with no body.
Before 2026-09-17 those were logged as `  <ICAO>: JSONDecodeError: Expecting value: line 1 column 1
(char 0)` and recorded as errors, so old logs overstate the error rate. Error lines now carry the HTTP
status when a response arrived — `JSONDecodeError (HTTP 200)` is a genuinely malformed body, while a
line with no status is a network failure.

```bash
tail -20 ingest/data/schedules.log                       # last schedules run
sqlite3 -readonly ingest/data/opensky_cache.db \
  "SELECT remaining, unit_limit, reset_at, observed_at FROM schedules_quota;"
```

**The restart step was broken until 2026-09-19** and is worth knowing about, because both causes recur.
launchd gives a job the bare `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, which holds no Go toolchain, so the
chain stopped a running server and could not start it again (`exec: go: not found` in
`ingest/data/api-<profile>.log`, then `--- run RESTART_FAILED ... ---`). `run-server.sh` now resolves the
binary itself — set `GO_BIN` if Go lives somewhere unusual. The second cause: `go run` compiles before it
binds, and the build cache is keyed partly on the environment, so the first run under launchd rebuilt
everything (103s, against what was then a 90s budget) and the chain called it failed mid-compile.
`refresh.STARTUP_BUDGET_SECONDS` is 180 for that reason; expect one slow restart after a Go upgrade.
A failed restart alarms and names the profile and port, so it is loud rather than silent.

**Neo4j runs only when needed (since 2026-10-31).** The refresh starts `aircraft-route-neo4j-staging`
for the load and stops it after every run unless the OpenSky API is listening on :8081. That includes
a container you started by hand, so close any Neo4j browser or cypher-shell work before 11:00, or keep
the API up. For ad-hoc use, start containers as in §1–§2; production is never started or stopped by a
job. Docker Desktop must be open: the job does not launch it, and an unreachable daemon alarms. After a
staging wipe the job alarms until you recreate the container with
`docker compose -f docker-compose.staging.yml up -d`.

Nothing else is scheduled: `sweep_stale.py` (stale-edge deletion), the production container, and the
*initial* start of an API server are all manual. As of 2026-09-13 the chain rebuilds the graph **and
restarts any server already running** (§4.1.5 *Refresh chain*), so a server you leave up picks up
each day's reconciliation on its own; one you stop stays stopped.

**First unattended run confirmed, 2026-09-13.** The chain fired on its own and logged
`run OK: 50253 edges served, 1783 marked stale; no API server running, nothing to restart` — the
last clause being correct, since no server was up. The ordering gate worked as designed on real
timings: the schedules fetch last wrote at 04:45:37Z and the rebuild started at 05:15:26Z, ~30
minutes later, well clear of the 5-minute quiet window.

**It fired at 10:45 local, not 10:30** — launchd runs an overdue agent late after sleep rather than
skipping it, which is documented behaviour and harmless in itself. But it left only ~7 minutes of
margin over the schedules fetch instead of 22, so **the slot moved to 11:00 on 2026-09-13**, restoring
~50 minutes. Widening `UPSTREAM_QUIET_MINUTES` was the alternative and was rejected: it would make the
gate defer more often, trading a rare risk for frequent noise. If a schedules run ever does overrun
the gap, the rebuild defers quietly to the next day — the designed outcome, not a fault.

### Stale-edge reconciliation

`load_graph.py` marks ROUTE edges its validity filter rejects (`r.stale = <timestamp>`) and
clears the mark on edges it re-validates. It never deletes. The Go server excludes marked edges
via `WHERE r.stale IS NULL` — but **only when it loads the graph, which is once at startup**
(CLAUDE.md: "Graph loaded once at startup, shared read-only"). Deletion is therefore still
housekeeping and the sweeper can be paused freely, but **a marked edge keeps being served until that
server restarts** — which the daily 11:00 chain now takes care of for any server left running.

Normally you do nothing: `refresh.py` rebuilds and restarts, and marks reach clients within a day.
To publish a reconciliation out of band, run the two steps by hand:

```bash
python3 ingest/sources/load_graph.py      # mark/clear in Neo4j
./scripts/run-server.sh opensky           # restart to pick it up (by design — see below)
```

There is no reload path **by design** (ARCH-1, decided 2026-09-12 — ARCHITECTURE.md §4.1.5 *Refresh
chain* and §12.1): refresh is by process restart. Since 2026-09-13 the chain does both halves
unattended at 11:00, so a mark is visible to clients within a day without you doing anything —
provided the server was running. Confirm it took by checking freshness:

```bash
curl -s http://localhost:8081/health | python3 -m json.tool | grep graph_
# graph_loaded_at should be today's 11:00-ish run; graph_age_seconds counts up from it
```

Corrected 2026-09-10: this section previously claimed marking took effect immediately, which is true
of the database and false of any already-running server. Corrected again 2026-09-13: the restart is
no longer manual.

```bash
python3 ingest/sources/sweep_stale.py            # report only — safe, deletes nothing
python3 ingest/sources/sweep_stale.py --delete   # delete edges marked > 60 days ago
```

The 60-day grace is ~2x the longest tier cycle (tail, 30.7d): an edge unobserved for one cycle
may simply not have been revisited yet, and re-observation clears the mark. Do not shorten it
without re-reading ARCHITECTURE.md §4.1.6 — a grace below one full tail cycle deletes routes that
are merely waiting their turn in the queue.

If the served graph looks unexpectedly small, check the split before assuming data loss:

```bash
set -a; source .env; set +a
docker exec -i aircraft-route-neo4j-staging cypher-shell \
  -u "$NEO4J_STAGING_USER" -p "$NEO4J_STAGING_PASSWORD" \
  "MATCH ()-[r:ROUTE]->() RETURN r.source AS source, r.stale IS NULL AS served, count(*) AS n ORDER BY source, served;"
```

A large marked-stale count is normal after a fetch gap: those edges failed the validity filter
and were previously being served only because the loader had no authority to retract them. The API servers keep running until stopped or reboot. Neither container has a restart policy, so neither comes back on its own after a reboot; the refresh starts staging when it needs it.

```bash
launchctl list | grep aircraft-route-planner                                    # is it loaded? (2nd column = last exit status)
tail -20 ingest/data/fetch.log                                                  # last run output
launchctl unload ~/Library/LaunchAgents/com.aircraft-route-planner.fetch.plist  # pause daily fetch
launchctl load   ~/Library/LaunchAgents/com.aircraft-route-planner.fetch.plist  # resume

# Accumulation progress
sqlite3 ingest/data/opensky_cache.db \
  "SELECT count(*) FROM raw_flights; SELECT count(DISTINCT window_begin) FROM fetch_log WHERE status='ok';"
```

### Full Disk Access — required, and the thing most likely to break this job

The repo lives under `~/Desktop`, which macOS protects with TCC. A launchd-spawned interpreter
has no Full Disk Access and — unlike a GUI app — cannot prompt for it, so it dies instantly with
`can't open file ...: [Errno 1] Operation not permitted`. launchd still writes `fetch.log`
itself (it holds the entitlement), so the failure looks like a normal empty run unless you read
the log. This silently killed 57 consecutive daily runs, 2026-07-06 → 2026-08-31.

The grant is **per-binary**, which is why the plist names the interpreter by absolute path
instead of the `/usr/bin/python3` shim — the granted identity must match what actually execs:

```
System Settings → Privacy & Security → Full Disk Access → + → Cmd+Shift+G →
  /Library/Developer/CommandLineTools/usr/bin/python3
```

An Xcode Command Line Tools major bump can move that 3.9 framework path and re-break the grant.
The staleness check below is what catches it.

### Moving the project directory — the plists hold absolute paths

All three plists name the script, `WorkingDirectory` and both log paths absolutely. Move or rename the
repo and every job fails before Python starts: `launchctl list` shows last exit **78** (`EX_CONFIG`),
and nothing reaches the logs, because they point at the old location too. The cause is only in the
system log:

```bash
log show --last 24h --style compact \
  --predicate 'eventMessage CONTAINS "aircraft-route-planner"' | grep -i 'could not initialize'
# … Service could not initialize: Unable to set current working directory. path = <old path>
```

This happened when the repo moved from `~/Desktop/Antigravity/` to `~/Desktop/My Projects/`: every
2026-09-15 run was lost. Full Disk Access did **not** need re-granting — the grant is keyed to the
interpreter binary, which did not move, and the 2026-09-16 runs then succeeded unattended.

Repoint the four path-bearing keys in each plist and reload it. The three string keys take a plain
`plutil -replace`:

```bash
P=~/Library/LaunchAgents/com.aircraft-route-planner.fetch.plist
plutil -replace WorkingDirectory  -string "<repo>/ingest/sources"        "$P"
plutil -replace StandardOutPath   -string "<repo>/ingest/data/fetch.log" "$P"
plutil -replace StandardErrorPath -string "<repo>/ingest/data/fetch.log" "$P"
```

`ProgramArguments` needs care: `plutil -replace ProgramArguments.1 -string <script>` **inserts** at
index 1 instead of overwriting, leaving the old path as a third argument (observed 2026-09-15; harmless
only because the scripts ignore arguments). Follow it with `plutil -remove ProgramArguments.2`, check
with `plutil -p "$P"` that exactly two arguments remain, then `launchctl unload "$P"` and
`launchctl load "$P"`. Unload stops a job that is mid-run, so wait for a running job to finish first.

### Verifying the job — a shell run proves nothing

`python3 ingest/sources/fetch_flights.py` in Terminal succeeds regardless of TCC, because
Terminal carries its own grant. Only the launchd context is a valid test:

```bash
launchctl kickstart -k gui/$(id -u)/com.aircraft-route-planner.fetch   # force a real run now
tail -20 ingest/data/fetch.log                                         # expect "--- run started ... ---", no EPERM
```

Each run prints `--- run started <ts> ---` and one of three outcomes:

| Marker | Exit | Meaning |
|---|---|---|
| `--- run OK ---` | 0 | collected data normally |
| `--- run OK: nothing due this window ---` | 0 | every due airport was already fetched — quiet by design |
| `--- run UNPRODUCTIVE: ... ---` | 1 | ran fine but collected **nothing**; check the OpenSky credit budget first, then the API credential |
| `--- run FAILED: ... ---` | 1 | crashed; the exception is on the line |

Indented `  <ICAO>: <ExceptionName>: <msg>` lines are per-airport network failures. A handful is
normal transient noise. That airport is recorded `error`, and because a failed attempt still counts as
a visit it is retried at its next turn in the queue rather than on the next run; `ok` and `empty` both
mark its window collected. Many in one run points at connectivity or a bad credential, and
if none succeed the run ends `UNPRODUCTIVE`.

Both non-zero outcomes also raise a macOS notification. `UNPRODUCTIVE` exists because a
credit-floor or rate-limit stop produces no *errors* — without it, a run that harvests nothing
is indistinguishable from a healthy one in both the log and the exit code.

Note a kickstart spends the day's ~3,750 credits, and OpenSky's quota is not calendar-day
aligned — it rolls off ~24h after each spend. A manual kickstart therefore pushes the next
scheduled run inside that shadow and can make it come back `UNPRODUCTIVE`; this happened on
2026-09-03. Prefer letting the 09:00 run do its thing over kickstarting.

It recurred on 2026-09-15: a kickstart at 23:11 local, meant to recover a day lost to a moved project
directory (see above), left the next morning's run just 2 airports before the credit floor. A missed
OpenSky day cannot be bought back, only moved. AeroDataBox is different — its quota is per period (reset on the 5th), so a
recovery run there does restore the missed day.

### Staleness check — has the cache stopped growing?

```bash
sqlite3 ingest/data/opensky_cache.db \
  "SELECT max(fetched_at) AS last_ok, count(DISTINCT window_begin) AS windows
   FROM fetch_log WHERE status='ok';"
```

`last_ok` older than ~2 days means the job is dead — check `fetch.log` and the FDA grant first.

## 5. Map UI (works with either stack)

```bash
cd web && npm run dev            # http://localhost:5173, proxies API to :8080 (openflights)
```

To point the UI at the OpenSky API, change the proxy target in `web/vite.config.js` to `:8081`. Production build (`npm run build`) is served automatically by whichever Go server runs, when `web/dist/` exists.

## 6. Production cut-over (Phase 6.4 — deferred until dataset maturity, ~Nov 2026)

```bash
# DESTRUCTIVE: wipes production Neo4j (:7687) and re-ingests from the OpenSky cache
python3 ingest/sources/promote.py --yes
# then restart: ./scripts/run-server.sh openflights   (now serving OpenSky data on :8080)
# rollback: python3 ingest/main.py
```

## 7. Tests & verification

```bash
go test ./...                    # unit tests (no Neo4j needed)
./scripts/verify.sh              # end-to-end against a running :8080 server (OpenFlights-era expectations)
```
