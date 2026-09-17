# Aircraft Silhouette Attribution

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

- Top-down shapes bundled: **28**
- ICAO types with a shape: **199**
- IATA → ICAO mapping entries: **242**
- Side-profile BMPs (ICAO-keyed): **2052**

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
