"""Build the aircraft silhouette bundle used by the Phase 5 Map UI.

Outputs:
  web/public/assets/aircraft_shapes.json   — { shapes, iataToIcao, default }
  web/public/assets/aircraft/details/*.bmp — side-profile silhouettes
  web/public/assets/aircraft/ATTRIBUTION.md

Sources (data assets, not linked code):
  - wiedehopf/tar1090            GPL-2.0+   top-down SVG path data, ICAO-keyed
  - RexKramer1/AircraftShapesSVG GPL-3.0    supplementary top-down SVGs
  - rikgale/VRSOperatorFlags     GPL-3.0    side-profile BMPs (Silhouettes.zip)

The script is idempotent — re-run it to pick up upstream updates.

Run:  python tools/build_shapes.py
Env:  NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD (reuses ingest/config.py)
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

# Allow `import ingest.config` when running from project root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingest.config import neo4j_config  # noqa: E402

TAR1090_URL = (
    "https://raw.githubusercontent.com/wiedehopf/tar1090/master/html/aircraft_shapes.js"
)
REXKRAMER_REPO_API = (
    "https://api.github.com/repos/RexKramer1/AircraftShapesSVG/contents/SVG"
)
REXKRAMER_RAW = (
    "https://raw.githubusercontent.com/RexKramer1/AircraftShapesSVG/main/SVG"
)
VRS_SILHOUETTES_URL = (
    "https://github.com/rikgale/VRSOperatorFlags/raw/main/Silhouettes.zip"
)

OUT_DIR = ROOT / "web" / "public" / "assets"
SHAPES_JSON = OUT_DIR / "aircraft_shapes.json"
DETAILS_DIR = OUT_DIR / "aircraft" / "details"
ATTRIBUTION = OUT_DIR / "aircraft" / "ATTRIBUTION.md"

GENERIC_ARROW = (
    '<svg viewBox="-12 -12 24 24" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M0 -10 L8 8 L0 4 L-8 8 Z" fill="currentColor" '
    'stroke="#1e3a8a" stroke-width="0.75"/></svg>'
)

# OpenFlights route equipment fields sometimes carry generic family IATA codes
# rather than a specific variant. Map each known family code to the variant we
# expect to find in iataToIcao (which is populated from Aircraft nodes). Keep
# this in sync with FAMILY_FALLBACKS in web/src/shapes.js.
FAMILY_IATA_ALIASES = {
    "380": "388",
    "350": "359",
    "330": "332",
    "340": "343",
    "310": "310",
    "320": "320",
    "707": "703",
    "717": "717",
    "727": "722",
    "737": "738",
    "747": "744",
    "757": "752",
    "767": "763",
    "777": "77W",
    "787": "789",
}


def http_get(url: str, *, accept: str = "*/*", timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "build_shapes.py", "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_tar1090(js_text: str) -> dict[str, dict]:
    """Extract { ICAO: { svg, noRotate? } } from tar1090's JS shapes file.

    tar1090's `aircraft_shapes.js` defines a JS object literal — the exact
    variable name and structure has shifted over time, so we parse defensively:
    grab each `'CODE': { ... },` block and pull svg/noRotate fields.
    """
    out: dict[str, dict] = {}
    # Match 'CODE': { ... } or "CODE": { ... } — non-greedy block.
    entry_re = re.compile(
        r"""['"]([A-Z0-9]{2,6})['"]\s*:\s*\{(.*?)\}\s*,?\s*(?=['"][A-Z0-9]|\}\s*[;)])""",
        re.DOTALL,
    )
    svg_re = re.compile(r"""svg\s*:\s*['"](.+?)['"]""", re.DOTALL)
    norot_re = re.compile(r"""noRotate\s*:\s*(true|1)""")

    for m in entry_re.finditer(js_text):
        code = m.group(1).upper()
        body = m.group(2)
        svg_m = svg_re.search(body)
        if not svg_m:
            continue
        svg = svg_m.group(1).encode().decode("unicode_escape")
        entry: dict = {"svg": svg}
        if norot_re.search(body):
            entry["noRotate"] = True
        out[code] = entry
    return out


def fetch_rexkramer() -> dict[str, dict]:
    """Pull supplementary SVGs from RexKramer1/AircraftShapesSVG.

    Lists the SVG dir via the GitHub Contents API, downloads each <ICAO>.svg.
    """
    out: dict[str, dict] = {}
    try:
        listing = json.loads(http_get(REXKRAMER_REPO_API, accept="application/vnd.github+json"))
    except Exception as e:
        print(f"  ! RexKramer listing failed ({e}); skipping supplement", file=sys.stderr)
        return out

    for entry in listing:
        name = entry.get("name", "")
        if not name.lower().endswith(".svg"):
            continue
        code = name.rsplit(".", 1)[0].upper()
        try:
            svg = http_get(f"{REXKRAMER_RAW}/{name}").decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  ! RexKramer {name} fetch failed: {e}", file=sys.stderr)
            continue
        out[code] = {"svg": svg.strip()}
    return out


def fetch_iata_to_icao() -> dict[str, str]:
    """Query Neo4j for every aircraft type's IATA → ICAO mapping."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("  ! neo4j driver not installed; iataToIcao will be empty", file=sys.stderr)
        return {}

    cfg = neo4j_config()
    out: dict[str, str] = {}
    with GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"])) as drv:
        with drv.session() as s:
            for rec in s.run("MATCH (a:Aircraft) RETURN a.iata AS iata, a.icao AS icao"):
                iata = rec["iata"]
                icao = rec["icao"]
                if iata and icao:
                    out[iata] = icao.upper()
    return out


def emit_shapes_json(shapes: dict[str, dict], iata_to_icao: dict[str, str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "shapes": shapes,
        "iataToIcao": iata_to_icao,
        "default": GENERIC_ARROW,
    }
    SHAPES_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def fetch_silhouettes() -> int:
    """Download VRSOperatorFlags Silhouettes.zip and extract BMPs to DETAILS_DIR."""
    DETAILS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        zip_bytes = http_get(VRS_SILHOUETTES_URL)
    except Exception as e:
        print(f"  ! Silhouettes.zip fetch failed: {e}; popovers will omit BMPs", file=sys.stderr)
        return 0

    count = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".bmp"):
                continue
            stem = Path(name).stem.upper()
            target = DETAILS_DIR / f"{stem}.bmp"
            with zf.open(name) as src, open(target, "wb") as dst:
                dst.write(src.read())
            count += 1
    return count


def write_attribution(shape_count: int, bmp_count: int, iata_count: int) -> None:
    ATTRIBUTION.parent.mkdir(parents=True, exist_ok=True)
    ATTRIBUTION.write_text(
        f"""# Aircraft Silhouette Attribution

The silhouettes bundled with the Map UI are derived from three open-source
projects. All three are GPL-licensed, so the merged asset set in
`web/public/assets/aircraft/` is itself GPL-3.0. The Go server and Python
ingestion code are **unaffected** — silhouettes are data assets used at
runtime by the frontend, not linked into the compiled binaries.

## Sources

| Project | License | Provides |
|---|---|---|
| [wiedehopf/tar1090](https://github.com/wiedehopf/tar1090) | GPL-2.0+ | Top-down SVG path data (`aircraft_shapes.js`), ICAO-keyed |
| [RexKramer1/AircraftShapesSVG](https://github.com/RexKramer1/AircraftShapesSVG) | GPL-3.0 | Supplementary top-down SVGs (gap-fill) |
| [rikgale/VRSOperatorFlags](https://github.com/rikgale/VRSOperatorFlags) | GPL-3.0 | Side-profile BMPs (`Silhouettes.zip`, 85×20 px) |

## Bundle stats

- Top-down shape entries (ICAO-keyed): **{shape_count}**
- Side-profile BMPs (ICAO-keyed): **{bmp_count}**
- IATA → ICAO mapping entries (from Neo4j): **{iata_count}**

The merged shapes table is keyed by ICAO type designator (e.g. `A388`,
`B77W`). The API returns IATA equipment codes (e.g. `388`, `77W`) on each
leg; the `iataToIcao` table in `aircraft_shapes.json` resolves between them.

## Regenerating

Run from project root:

```
python tools/build_shapes.py
```

The script is idempotent — re-running fetches the latest upstream sources
and overwrites the bundle. The merged dataset remains GPL-3.0.
""",
        encoding="utf-8",
    )


def main() -> int:
    print(f"[1/5] Fetching tar1090 shapes from {TAR1090_URL}")
    tar_shapes: dict[str, dict] = {}
    try:
        js_text = http_get(TAR1090_URL).decode("utf-8", errors="replace")
        tar_shapes = parse_tar1090(js_text)
        print(f"      Parsed {len(tar_shapes)} ICAO entries from tar1090")
    except Exception as e:
        print(f"  ! tar1090 fetch failed: {e}", file=sys.stderr)

    print("[2/5] Fetching RexKramer1 supplementary SVGs")
    rex_shapes = fetch_rexkramer()
    print(f"      Fetched {len(rex_shapes)} supplementary SVGs")

    merged: dict[str, dict] = dict(tar_shapes)
    added = 0
    for code, entry in rex_shapes.items():
        if code not in merged:
            merged[code] = entry
            added += 1
    print(f"      Merged: {len(merged)} total ({added} added by RexKramer1 gap-fill)")

    print("[3/5] Querying Neo4j for IATA → ICAO mapping")
    iata_to_icao = fetch_iata_to_icao()
    print(f"      Got {len(iata_to_icao)} mappings from Aircraft nodes")
    family_added = 0
    for family_code, variant in FAMILY_IATA_ALIASES.items():
        if family_code not in iata_to_icao and variant in iata_to_icao:
            iata_to_icao[family_code] = iata_to_icao[variant]
            family_added += 1
    if family_added:
        print(f"      +{family_added} family-IATA aliases (e.g. 380→A388)")

    print(f"[4/5] Writing {SHAPES_JSON}")
    emit_shapes_json(merged, iata_to_icao)

    print(f"[5/5] Extracting Silhouettes.zip → {DETAILS_DIR}")
    bmp_count = fetch_silhouettes()
    print(f"      Extracted {bmp_count} BMP files")

    write_attribution(len(merged), bmp_count, len(iata_to_icao))
    print(f"      Wrote {ATTRIBUTION}")

    print("\nDone. Reload the Map UI to pick up the new shapes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
