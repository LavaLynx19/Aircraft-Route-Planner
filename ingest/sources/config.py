"""Shared config for the staging data-source pipeline (Phase 6.2).

All scripts in ingest/sources/ target the STAGING Neo4j and the local
SQLite cache. Reads the repo .env directly so launchd/cron runs work
without a shell profile.
"""

import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(REPO_ROOT, "ingest", "data")
SQLITE_PATH = os.path.join(DATA_DIR, "opensky_cache.db")
OURAIRPORTS_CSV = os.path.join(DATA_DIR, "ourairports_airports.csv")
COUNTRIES_CSV = os.path.join(DATA_DIR, "ourairports_countries.csv")
AIRCRAFT_DB_CSV = os.path.join(DATA_DIR, "tar1090_aircraft.csv.gz")
AIRLINE_MAP_JSON = os.path.join(DATA_DIR, "icao_airline_map.json")

OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
COUNTRIES_URL = "https://davidmegginson.github.io/ourairports-data/countries.csv"
AIRCRAFT_DB_URL = (
    "https://github.com/wiedehopf/tar1090-db/raw/refs/heads/csv/aircraft.csv.gz"
)

OSK_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
OSK_API_BASE = "https://opensky-network.org/api"

# AeroDataBox (via RapidAPI) — schedules source for the ADS-B blackout regions
# where OpenSky has no receiver coverage (ARCHITECTURE.md §4.1.5).
ADB_API_HOST = "aerodatabox.p.rapidapi.com"
ADB_API_BASE = f"https://{ADB_API_HOST}"
# ICAO prefixes with measured ADS-B blackout: Z = China/Mongolia/DPRK, U = Russia/CIS.
BLACKOUT_ICAO_PREFIXES = ("Z", "U")


def load_env() -> None:
    """Load KEY=VALUE pairs from the repo .env without overriding existing env."""
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def staging_neo4j_config() -> dict:
    """Staging Neo4j connection config (never production).

    The password has no fallback (2026-09-10 review L2). Defaulting to a literal
    meant a missing `.env` silently produced a *working* connection with a known
    credential — harmless on a local-only container, and precisely the thing that
    stops being harmless the first time this runs anywhere shared. Mirrors the
    fail-fast `opensky_config` and `aerodatabox_config` already use rather than
    inventing a second convention.

    URI and user keep defaults: they are addresses, not secrets.
    """
    load_env()
    password = os.environ.get("NEO4J_STAGING_PASSWORD")
    if not password:
        raise SystemExit("NEO4J_STAGING_PASSWORD missing (set it in .env)")
    return {
        "uri": os.environ.get("NEO4J_STAGING_URI", "bolt://localhost:7688"),
        "user": os.environ.get("NEO4J_STAGING_USER", "neo4j"),
        "password": password,
    }


# Deliberate shadow: parent ingest/ modules do `from config import neo4j_config`.
# When they are imported under the sources/ context (sys.path puts this dir first),
# they resolve HERE and get staging — so nothing run from sources/ can ever touch
# production Neo4j.
neo4j_config = staging_neo4j_config


def servers_config() -> dict:
    """API server profiles → port, as `scripts/run-server.sh` defines them.

    Defaults mirror that script exactly (openflights 8080, opensky 8081) so the
    refresh chain and a manual launch cannot disagree about where a server lives.
    """
    load_env()
    return {
        "openflights": int(os.environ.get("OPENFLIGHTS_API_PORT", "8080")),
        "opensky": int(os.environ.get("OPENSKY_API_PORT", "8081")),
    }


def opensky_config() -> dict:
    """OpenSky OAuth2 client credentials + fetch tuning knobs."""
    load_env()
    client_id = os.environ.get("OSK_CLIENT_ID")
    client_secret = os.environ.get("OSK_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("OSK_CLIENT_ID / OSK_CLIENT_SECRET missing (set them in .env)")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        # Flat 30 credits/request (live-verified 2026-07-05); 125 req ≈ 3,750 of 4,000/day.
        "max_requests": int(os.environ.get("OSK_MAX_REQUESTS", "125")),
        "credit_floor": int(os.environ.get("OSK_CREDIT_FLOOR", "200")),
        # Tiered cadence (§4.1.5): the top `hub_size` airports by traffic are
        # resampled every `hub_cycle_days`; the remainder share what is left of
        # the daily budget. Replaces the flat FETCH_AIRPORT_LIMIT=930 cap, which
        # was a refresh-rate figure masquerading as a coverage figure.
        "hub_size": int(os.environ.get("OSK_HUB_SIZE", "300")),
        "hub_cycle_days": int(os.environ.get("OSK_HUB_CYCLE_DAYS", "7")),
    }


def aerodatabox_config() -> dict:
    """AeroDataBox/RapidAPI key + schedules fetch tuning knobs."""
    load_env()
    key = os.environ.get("AERODATABOX_API_KEY")
    if not key:
        raise SystemExit("AERODATABOX_API_KEY missing (set it in .env)")
    return {
        "api_key": key,
        # RapidAPI Basic tier: 600 units/month, recurring, 1 unit per airport call.
        # 19 calls/day ≈ 578/month, giving a ~23-day sweep of the 439 blackout airports.
        "monthly_units": int(os.environ.get("ADB_MONTHLY_UNITS", "600")),
        "daily_calls": int(os.environ.get("ADB_DAILY_CALLS", "19")),
        # Held back so a long month cannot leave zero units for a manual probe.
        "unit_reserve": int(os.environ.get("ADB_UNIT_RESERVE", "20")),
    }
