import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env() -> None:
    """Load KEY=VALUE pairs from the repo .env without overriding existing env.

    Added 2026-09-13 alongside the fail-fast below, and required by it. Nothing on
    this path ever loaded `.env` — RUNBOOK documents a bare `python3
    ingest/main.py` — so production ingest worked *only* because the password was
    hardcoded. Removing the default without this would have broken the documented
    rollback path rather than hardening it. Mirrors `ingest/sources/config.py`.
    """
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


def neo4j_config():
    """Read Neo4j connection config from environment.

    The password has no fallback (2026-07-05 review S4): it defaulted to the
    literal `aviation`, so a missing credential silently produced a *working*
    connection using a value published in this repository. URI and user keep
    defaults — they are addresses, not secrets.
    """
    load_env()
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise SystemExit("NEO4J_PASSWORD missing (set it in .env)")
    return {
        "uri": os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        "user": os.environ.get("NEO4J_USER", "neo4j"),
        "password": password,
    }
