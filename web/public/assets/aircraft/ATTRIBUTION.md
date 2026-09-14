# Aircraft Silhouette Attribution

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

- Top-down shape entries (ICAO-keyed): **0**
- Side-profile BMPs (ICAO-keyed): **2052**
- IATA → ICAO mapping entries (from Neo4j): **218**

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
