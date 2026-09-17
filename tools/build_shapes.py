"""Build the aircraft icon bundle used by the Phase 5 Map UI.

Outputs:
  web/public/assets/aircraft_shapes.json   — { shapes, icaoToShape, iataToIcao, defaultShape }
  web/public/assets/aircraft/details/*.bmp — side-profile silhouettes
  web/public/assets/aircraft/ATTRIBUTION.md

Sources (data assets, not linked code):
  - wiedehopf/tar1090 `markers.js`   GPL-2.0+  top-down shapes, and tar1090's own
                                              type → shape tables
  - Mictronics/aircraft-database     ODC-By    ICAO type descriptions (e.g. L2T-M), the
                                              fallback for types tar1090 does not list
                                              by designator
  - rikgale/VRSOperatorFlags          GPL-3.0   side-profile BMPs (Silhouettes.zip)

Each type gets the shape tar1090 would draw for a live aircraft of that type: by
type designator, then by type description and wake category, then by the
description's first letter. Only the ADS-B emitter category is left out, since a
route has nothing equivalent.

A failed or empty download of either shape source aborts the build and leaves the
existing bundle untouched. The previous version logged the failure and wrote an
empty shapes table with exit status 0 — which is how the map ran on fallback icons
unnoticed after tar1090 replaced `aircraft_shapes.js` with `markers.js`.

Run:  python3 tools/build_shapes.py
Env:  NEO4J_* and NEO4J_STAGING_* (read through ingest/config.py and
      ingest/sources/config.py)
"""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

from neo4j import GraphDatabase
from neo4j.exceptions import DriverError, Neo4jError

# Allow `import ingest.config` when running from project root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingest.config import neo4j_config
from ingest.sources.config import staging_neo4j_config

TAR1090_MARKERS_URL = (
    "https://raw.githubusercontent.com/wiedehopf/tar1090/master/html/markers.js"
)
MICTRONICS_TYPES_URL = (
    "https://raw.githubusercontent.com/Mictronics/aircraft-database/main/"
    "icao_aircraft_types.zip"
)
VRS_SILHOUETTES_URL = (
    "https://github.com/rikgale/VRSOperatorFlags/raw/main/Silhouettes.zip"
)

OUT_DIR = ROOT / "web" / "public" / "assets"
SHAPES_JSON = OUT_DIR / "aircraft_shapes.json"
DETAILS_DIR = OUT_DIR / "aircraft" / "details"
ATTRIBUTION = OUT_DIR / "aircraft" / "ATTRIBUTION.md"

# tar1090's defaults.js: OutlineADSBColor '#000000', outlineWidth 0.90.
OUTLINE_COLOR = "#000000"
OUTLINE_WIDTH = 0.9

# Drawn when a code has no ICAO type, or its type has no shape. tar1090 falls back
# to 'unknown'; every aircraft here flies a scheduled route, so an airliner is the
# honest generic.
DEFAULT_SHAPE = "airliner"

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

# IATA codes for a sub-variant (winglets, sharklets, freighter, series) of an ICAO
# type. OpenFlights routes.dat uses them but planes.dat has no row for them, so no
# :Aircraft node carries the mapping. Source: Wikipedia, "List of aircraft type
# designators", retrieved 2026-09-16.
#
# IATA *group* codes (CRJ, ERJ, DH8, M80, 32S, CNA, …) are deliberately absent. They
# name a family rather than one type, no source consulted maps them, and choosing
# a representative type from memory would be a guess.
IATA_VARIANT_CODES = {
    "313": "A310",
    "32A": "A320",
    "32B": "A321",
    "32C": "A318",
    "33X": "A332",
    "73C": "B733",
    "73H": "B738",
    "73J": "B739",
    "73W": "B737",
    "74E": "B744",
    "74H": "B748",
    "74N": "B748",
    "74Y": "B744",
    "75W": "B752",
    "76W": "B763",
    "77X": "B77L",
    "AB4": "A30B",
    "CNT": "F406",
    "D1C": "DC10",
    "E75": "E75S",
    "L4T": "L410",
    "M1F": "MD11",
}


def http_get(url: str, *, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "build_shapes.py"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# --- markers.js ------------------------------------------------------------------

_JS_TOKEN = re.compile(
    r"""(?P<skip>\s+|//[^\n]*|/\*.*?\*/)
      |(?P<str>'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")
      |(?P<num>-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)
      |(?P<name>[A-Za-z_$][\w$]*)
      |(?P<punct>[{}\[\]:,])""",
    re.DOTALL | re.VERBOSE,
)
_JS_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.DOTALL)
_JS_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v"}


def _unescape(body: str) -> str:
    def sub(m: re.Match) -> str:
        esc = m.group(1)
        if esc[0] in "ux" and len(esc) > 1:
            return chr(int(esc[1:], 16))
        return _JS_SIMPLE_ESCAPES.get(esc, esc)

    return _JS_ESCAPE.sub(sub, body)


class JsLiteral:
    """Read top-level constant literals out of a JavaScript source file.

    Handles exactly the syntax tar1090's icon tables use — quoted strings, numbers,
    true/false/null, nested objects and arrays, comments, trailing commas, and
    references to other top-level constants (`'ULAC': _ulac`) — and raises
    ValueError on anything else rather than guessing. Only the literal being read
    is tokenized, so the functions elsewhere in the file are never touched.
    """

    def __init__(self, source: str) -> None:
        self.src = source

    def assignment(self, name: str) -> object:
        m = re.search(
            rf"(?:^|[\s;])(?:(?:let|const|var)\s+)?{re.escape(name)}\s*=(?!=)\s*",
            self.src,
        )
        if not m:
            raise ValueError(f"{name} is not assigned in markers.js")
        value, _ = self._value(m.end())
        return value

    def _token(self, pos: int) -> tuple:
        while True:
            m = _JS_TOKEN.match(self.src, pos)
            if not m:
                raise ValueError(
                    f"unexpected {self.src[pos : pos + 20]!r} at offset {pos}"
                )
            if m.lastgroup != "skip":
                return m.lastgroup, m.group(), m.end()
            pos = m.end()

    def _value(self, pos: int) -> tuple:
        kind, text, pos = self._token(pos)
        if kind == "str":
            return _unescape(text[1:-1]), pos
        if kind == "num":
            return float(text), pos
        if kind == "name":
            if text in ("true", "false"):
                return text == "true", pos
            if text == "null":
                return None, pos
            return self.assignment(text), pos
        if text == "{":
            return self._object(pos)
        if text == "[":
            return self._array(pos)
        raise ValueError(f"unexpected {text!r} at offset {pos}")

    def _object(self, pos: int) -> tuple:
        out: dict = {}
        while True:
            kind, text, pos = self._token(pos)
            if kind == "punct" and text == "}":
                return out, pos
            if kind == "str":
                key = _unescape(text[1:-1])
            elif kind in ("name", "num"):
                key = text
            else:
                raise ValueError(f"expected a key, got {text!r} at offset {pos}")
            _, text, pos = self._token(pos)
            if text != ":":
                raise ValueError(f"expected ':' after {key!r} at offset {pos}")
            out[key], pos = self._value(pos)
            kind, text, nxt = self._token(pos)
            if kind == "punct" and text == ",":
                pos = nxt
            elif not (kind == "punct" and text == "}"):
                raise ValueError(f"expected ',' or '}}', got {text!r} at offset {pos}")

    def _array(self, pos: int) -> tuple:
        out: list = []
        while True:
            kind, text, nxt = self._token(pos)
            if kind == "punct" and text == "]":
                return out, nxt
            value, pos = self._value(pos)
            out.append(value)
            kind, text, nxt = self._token(pos)
            if kind == "punct" and text == ",":
                pos = nxt
            elif not (kind == "punct" and text == "]"):
                raise ValueError(f"expected ',' or ']', got {text!r} at offset {pos}")


def _num(x: float) -> str:
    return f"{x:g}"


def _as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def render_shape(shape: dict) -> str:
    """tar1090's svgShapeToSVG, with fill left as currentColor for the map to set.

    Width and height are left off: web/src/map.js sets them per marker.
    """
    stroke = OUTLINE_WIDTH * shape.get("strokeScale", 1)
    if "svg" in shape:
        # Mirrors tar1090, whose JS String.replace swaps the first occurrence only.
        return (
            shape["svg"]
            .replace("fillColor", "currentColor", 1)
            .replace("strokeColor", OUTLINE_COLOR, 1)
            .replace("strokeWidth", _num(stroke), 1)
            .replace("SIZE", "", 1)
        )
    aspect = ' preserveAspectRatio="none"' if shape.get("noAspect") else ""
    transform = f' transform="{shape["transform"]}"' if shape.get("transform") else ""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{shape["viewBox"]}"{aspect}>',
        f"<g{transform}>",
    ]
    for d in _as_list(shape.get("path")):
        parts.append(
            f'<path paint-order="stroke" fill="currentColor" stroke="{OUTLINE_COLOR}" '
            f'stroke-width="{_num(2 * stroke)}" d="{d}"/>'
        )
    accent_width = 0.6 * (shape.get("accentMult") or 1) * stroke
    for d in _as_list(shape.get("accent")):
        parts.append(
            f'<path fill="none" stroke="{OUTLINE_COLOR}" '
            f'stroke-width="{_num(accent_width)}" d="{d}"/>'
        )
    parts.append("</g></svg>")
    return "".join(parts)


def shape_for_type(
    icao: str, designators: dict, descriptions: dict, types: dict
) -> tuple | None:
    """(shape name, scale) for a type, following tar1090's getBaseMarker.

    Skips the steps that need a live aircraft (ADS-B emitter category, AIS, the
    novelty modes) and returns None where tar1090 would fall through to them.
    """
    if icao in designators:
        name, scale = designators[icao][:2]
        return name, scale
    info = types.get(icao) or {}
    desc, wtc = info.get("desc") or "", info.get("wtc") or ""
    if len(desc) == 3:
        if len(wtc) == 1 and f"{desc}-{wtc}" in descriptions:
            name, scale = descriptions[f"{desc}-{wtc}"][:2]
            return name, scale
        if desc in descriptions:
            name, scale = descriptions[desc][:2]
            return name, scale
        if desc[0] in descriptions:
            return descriptions[desc[0]][0], 1.0
    return None


def fetch_tar1090() -> tuple:
    js = JsLiteral(http_get(TAR1090_MARKERS_URL).decode("utf-8"))
    shapes = js.assignment("shapes")
    designators = js.assignment("TypeDesignatorIcons")
    descriptions = js.assignment("TypeDescriptionIcons")
    for label, table in (
        ("shapes", shapes),
        ("TypeDesignatorIcons", designators),
        ("TypeDescriptionIcons", descriptions),
    ):
        if not isinstance(table, dict) or not table:
            raise ValueError(f"{label} parsed empty")
    return shapes, designators, descriptions


def fetch_type_descriptions() -> dict:
    with zipfile.ZipFile(io.BytesIO(http_get(MICTRONICS_TYPES_URL))) as zf:
        types = json.loads(zf.read("icao_aircraft_types.json"))
    if not isinstance(types, dict) or not types:
        raise ValueError("icao_aircraft_types.json is empty or not an object")
    return types


# --- IATA → ICAO -----------------------------------------------------------------


def graph_iata_to_icao(label: str, cfg: dict) -> dict | None:
    """IATA → ICAO from one graph's :Aircraft nodes, or None if it is unreachable."""
    try:
        with GraphDatabase.driver(
            cfg["uri"], auth=(cfg["user"], cfg["password"])
        ) as drv:
            records, _, _ = drv.execute_query(
                "MATCH (a:Aircraft) RETURN a.iata AS iata, a.icao AS icao"
            )
    except (DriverError, Neo4jError) as e:
        print(
            f"  ! {label} graph unavailable ({type(e).__name__}); skipping it",
            file=sys.stderr,
        )
        return None
    return {r["iata"]: r["icao"].upper() for r in records if r["iata"] and r["icao"]}


def fetch_iata_to_icao() -> dict:
    """Both graphs' :Aircraft nodes, then the fixed tables above.

    Both graphs, because each knows types the other lacks: production carries the
    2014 OpenFlights set, staging adds post-2014 designators (A20N, B38M, BCS3…).
    """
    merged: dict = {}
    for label, config in (
        ("production", neo4j_config),
        ("staging", staging_neo4j_config),
    ):
        found = graph_iata_to_icao(label, config())
        if found is None:
            continue
        conflicts = sorted(k for k, v in found.items() if merged.get(k, v) != v)
        if conflicts:
            print(
                f"  ! {label} disagrees with an earlier graph on {conflicts}; keeping the earlier value"
            )
        for iata, icao in found.items():
            merged.setdefault(iata, icao)
        print(f"      {label}: {len(found)} aircraft types")
    if not merged:
        raise ValueError(
            "no graph reachable; refusing to write an empty IATA → ICAO table"
        )

    added = 0
    for iata, icao in IATA_VARIANT_CODES.items():
        if iata not in merged:
            merged[iata] = icao
            added += 1
    for family_code, variant in FAMILY_IATA_ALIASES.items():
        if family_code not in merged and variant in merged:
            merged[family_code] = merged[variant]
            added += 1
    print(f"      +{added} from the variant and family tables")
    return merged


# --- output ----------------------------------------------------------------------


def fetch_silhouettes() -> None:
    """Extract Silhouettes.zip BMPs into DETAILS_DIR, keeping existing files on failure."""
    DETAILS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(http_get(VRS_SILHOUETTES_URL))) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".bmp"):
                    continue
                target = DETAILS_DIR / f"{Path(name).stem.upper()}.bmp"
                target.write_bytes(zf.read(name))
    except (OSError, zipfile.BadZipFile) as e:
        print(
            f"  ! Silhouettes.zip unavailable ({e}); keeping the BMPs already on disk",
            file=sys.stderr,
        )


def write_attribution(stats: dict) -> None:
    ATTRIBUTION.parent.mkdir(parents=True, exist_ok=True)
    ATTRIBUTION.write_text(
        f"""# Aircraft Silhouette Attribution

The silhouettes bundled with the Map UI are derived from GPL-licensed projects,
so the merged asset set in `web/public/assets/aircraft/` is itself GPL-3.0. The Go
server and Python ingestion code are **unaffected** — silhouettes are data assets
used at runtime by the frontend, not linked into the compiled binaries.

## Sources

| Project | License | Provides |
|---|---|---|
| [wiedehopf/tar1090](https://github.com/wiedehopf/tar1090) | GPL-2.0+ | Top-down SVG shapes and the type → shape tables (`html/markers.js`) |
| [Mictronics/aircraft-database](https://github.com/Mictronics/aircraft-database) | ODC-By | ICAO type descriptions (`icao_aircraft_types.json`), used to pick a shape for types tar1090 does not list by designator |
| [rikgale/VRSOperatorFlags](https://github.com/rikgale/VRSOperatorFlags) | GPL-3.0 | Side-profile BMPs (`Silhouettes.zip`, 85×20 px) |

IATA variant codes absent from the graph (e.g. `73H` → `B738`) follow Wikipedia's
"List of aircraft type designators".

## Bundle stats

- Top-down shapes bundled: **{stats["shapes"]}**
- ICAO types with a shape: **{stats["types"]}**
- IATA → ICAO mapping entries: **{stats["iata"]}**
- Side-profile BMPs (ICAO-keyed): **{stats["bmps"]}**

The API returns IATA equipment codes (e.g. `388`, `77W`) on each leg.
`aircraft_shapes.json` resolves them through `iataToIcao`, then `icaoToShape`, to
one of the shapes; the same ICAO code names the side-profile BMP.

## Regenerating

Run from project root:

```
python3 tools/build_shapes.py
```

The script is idempotent — re-running fetches the latest upstream sources and
overwrites the bundle. The merged dataset remains GPL-3.0.
""",
        encoding="utf-8",
    )


def main() -> int:
    print(f"[1/5] Fetching tar1090 shapes and type tables from {TAR1090_MARKERS_URL}")
    try:
        shapes, designators, descriptions = fetch_tar1090()
    except (OSError, ValueError) as e:
        print(
            f"  ! tar1090 markers.js failed: {e}; bundle left unchanged",
            file=sys.stderr,
        )
        return 1
    print(
        f"      {len(shapes)} shapes, {len(designators)} designators, "
        f"{len(descriptions)} descriptions"
    )

    print(f"[2/5] Fetching ICAO type descriptions from {MICTRONICS_TYPES_URL}")
    try:
        types = fetch_type_descriptions()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        print(
            f"  ! type descriptions failed: {e}; bundle left unchanged", file=sys.stderr
        )
        return 1
    print(f"      {len(types)} ICAO types")

    print("[3/5] Querying Neo4j for IATA → ICAO mapping")
    try:
        iata_to_icao = fetch_iata_to_icao()
    except ValueError as e:
        print(f"  ! {e}; bundle left unchanged", file=sys.stderr)
        return 1

    icao_to_shape: dict = {}
    unshaped: list = []
    for icao in sorted(set(iata_to_icao.values())):
        found = shape_for_type(icao, designators, descriptions, types)
        if found and found[0] in shapes:
            icao_to_shape[icao] = [found[0], found[1]]
        else:
            unshaped.append(icao)
    names = {name for name, _ in icao_to_shape.values()} | {DEFAULT_SHAPE}
    bundle_shapes = {}
    for name in sorted(names):
        shape = shapes[name]
        entry = {"svg": render_shape(shape), "w": shape["w"], "h": shape["h"]}
        if shape.get("noRotate"):
            entry["noRotate"] = True
        bundle_shapes[name] = entry
    print(
        f"      {len(icao_to_shape)} ICAO types → {len(bundle_shapes)} shapes; "
        f"no shape (default drawn): {unshaped or 'none'}"
    )

    print(f"[4/5] Writing {SHAPES_JSON}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SHAPES_JSON.write_text(
        json.dumps(
            {
                "defaultShape": DEFAULT_SHAPE,
                "iataToIcao": iata_to_icao,
                "icaoToShape": icao_to_shape,
                "shapes": bundle_shapes,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"[5/5] Extracting Silhouettes.zip → {DETAILS_DIR}")
    fetch_silhouettes()
    bmps = len(list(DETAILS_DIR.glob("*.bmp")))
    print(f"      {bmps} BMP files on disk")

    write_attribution(
        {
            "shapes": len(bundle_shapes),
            "types": len(icao_to_shape),
            "iata": len(iata_to_icao),
            "bmps": bmps,
        }
    )
    print(f"      Wrote {ATTRIBUTION}")
    print("\nDone. Reload the Map UI to pick up the new shapes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
