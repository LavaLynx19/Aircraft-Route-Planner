// Resolves IATA equipment codes (as returned by the API in leg.aircraft) to
// SVG silhouettes. Looks up in this order:
//   1. the generated bundle at /assets/aircraft_shapes.json (tools/build_shapes.py):
//      IATA → ICAO type → the tar1090 top-down shape tar1090 itself would draw
//      for that type
//   2. BUILTIN_SHAPES below — three hand-tuned silhouettes (narrow, wide-twin,
//      quad) so the map shows real plane shapes when the bundle is missing or has
//      no shape for the type.
//   3. the bundle's default shape, else a generic plane silhouette.

// Hand-tuned silhouettes — north-up, fuselage runs along the Y axis. The
// fill="currentColor" hook lets map.js swap in the target / non-target color.
const wrap = (path) =>
  `<svg viewBox="-16 -16 32 32" xmlns="http://www.w3.org/2000/svg"><path d="${path}" fill="currentColor"/></svg>`;

const PATH_NARROW =
  'M0 -13 C0.8 -12 1.3 -10 1.3 -2 L11 3.5 L11 5.5 L1.3 1.5 L1.3 8 L3.5 10.5 L3.5 11.5 L0.8 10 L0.4 13 L-0.4 13 L-0.8 10 L-3.5 11.5 L-3.5 10.5 L-1.3 8 L-1.3 1.5 L-11 5.5 L-11 3.5 L-1.3 -2 C-1.3 -10 -0.8 -12 0 -13 Z';
const PATH_WIDE =
  'M0 -14 C1 -13 1.7 -10 1.7 -2 L13 4 L13 6 L1.7 1.5 L1.7 8 L4 11 L4 12 L1 10.5 L0.5 13.5 L-0.5 13.5 L-1 10.5 L-4 12 L-4 11 L-1.7 8 L-1.7 1.5 L-13 6 L-13 4 L-1.7 -2 C-1.7 -10 -1 -13 0 -14 Z';
const PATH_QUAD =
  'M0 -14 C1.2 -13 2 -10 2 -2 L15 4 L15 6 L2 1.5 L2 8 L4.5 11.5 L4.5 12.5 L1.2 11 L0.6 14 L-0.6 14 L-1.2 11 L-4.5 12.5 L-4.5 11.5 L-2 8 L-2 1.5 L-15 6 L-15 4 L-2 -2 C-2 -10 -1.2 -13 0 -14 Z';

const SVG_NARROW = wrap(PATH_NARROW);
const SVG_WIDE = wrap(PATH_WIDE);
const SVG_QUAD = wrap(PATH_QUAD);

const GENERIC_PLANE_SVG = SVG_NARROW;

const BUILTIN_SHAPES = Object.fromEntries(
  [
    // Narrow-body
    ...['A318', 'A319', 'A320', 'A321', 'A19N', 'A20N', 'A21N'].map((c) => [c, SVG_NARROW]),
    ...['B712', 'B722', 'B732', 'B733', 'B734', 'B735', 'B736', 'B737', 'B738', 'B739'].map((c) => [c, SVG_NARROW]),
    ...['B37M', 'B38M', 'B39M', 'B752', 'B753'].map((c) => [c, SVG_NARROW]),
    ...['E170', 'E190', 'E195', 'E290', 'E295', 'E75S', 'E135', 'E145'].map((c) => [c, SVG_NARROW]),
    ...['BCS1', 'BCS3', 'CRJ1', 'CRJ2', 'CRJ7', 'CRJ9', 'CRJX'].map((c) => [c, SVG_NARROW]),
    ...['MD81', 'MD82', 'MD83', 'MD87', 'MD88', 'MD90', 'DC91', 'DC92', 'DC93', 'DC94', 'DC95'].map((c) => [c, SVG_NARROW]),
    // Wide-body twin
    ...['A306', 'A30B', 'A310', 'A332', 'A333', 'A338', 'A339', 'A359', 'A35K'].map((c) => [c, SVG_WIDE]),
    ...['B762', 'B763', 'B764', 'B772', 'B773', 'B77L', 'B77W', 'B778', 'B779'].map((c) => [c, SVG_WIDE]),
    ...['B788', 'B789', 'B78X', 'DC10', 'MD11', 'L101'].map((c) => [c, SVG_WIDE]),
    // Quad heavy
    ...['A124', 'A225', 'A342', 'A343', 'A345', 'A346', 'A388'].map((c) => [c, SVG_QUAD]),
    ...['B703', 'B741', 'B742', 'B743', 'B744', 'B748', 'B74D', 'B74S', 'B74R', 'BLCF'].map((c) => [c, SVG_QUAD]),
    ...['IL62', 'IL76', 'IL86', 'IL96'].map((c) => [c, SVG_QUAD]),
  ].map(([c, svg]) => [c, { svg }])
);

// OpenFlights route equipment fields sometimes carry generic family IATA codes
// (e.g. "380", "777", "350") rather than a specific variant ("388", "77W").
// These mappings let shapes.js fall back to a representative variant when the
// family code isn't directly in iataToIcao. Keep in sync with the same table
// in tools/build_shapes.py.
const FAMILY_FALLBACKS = {
  '380': '388',
  '350': '359',
  '330': '332',
  '340': '343',
  '310': '310',
  '320': '320',
  '707': '703',
  '717': '717',
  '727': '722',
  '737': '738',
  '747': '744',
  '757': '752',
  '767': '763',
  '777': '77W',
  '787': '789',
};

// tar1090 draws a standard airliner 32px wide. Bundle shape dimensions and
// per-type scales are relative to that, so dividing by it keeps aircraft sizes in
// proportion at whatever marker size the map uses.
const TAR1090_BASE_PX = 32;

const EMPTY = { shapes: {}, icaoToShape: {}, iataToIcao: {}, defaultShape: null };

let cached = null;

async function load() {
  if (cached) return cached;
  try {
    const resp = await fetch('/assets/aircraft_shapes.json', { cache: 'no-cache' });
    if (!resp.ok) throw new Error(`shapes fetch failed: ${resp.status}`);
    const data = await resp.json();
    cached = {
      shapes: data.shapes || {},
      icaoToShape: data.icaoToShape || {},
      iataToIcao: data.iataToIcao || {},
      defaultShape: data.defaultShape || null,
    };
  } catch (e) {
    // Built-in silhouettes still work, but say so: a silently empty bundle is how
    // the map once ran on fallback icons without anyone noticing.
    console.warn('aircraft shape bundle unavailable, using built-in silhouettes:', e);
    cached = EMPTY;
  }
  return cached;
}

load();

function lookupICAO(data, iataCode) {
  if (!iataCode) return null;
  return (
    data.iataToIcao[iataCode] ||
    data.iataToIcao[FAMILY_FALLBACKS[iataCode]] ||
    null
  );
}

export function resolveICAO(iataCode) {
  if (!cached) return null;
  return lookupICAO(cached, iataCode);
}

function fromBundle(data, name, scale) {
  const shape = name ? data.shapes[name] : null;
  if (!shape || !shape.svg) return null;
  return {
    svg: shape.svg,
    rotate: !shape.noRotate,
    width: (shape.w * scale) / TAR1090_BASE_PX,
    height: (shape.h * scale) / TAR1090_BASE_PX,
  };
}

// Returns { svg, rotate, icao, width, height } for an IATA leg.aircraft code.
// width and height are multiples of the standard marker size.
export function shapeFor(iataCode) {
  const data = cached || EMPTY;
  const icao = lookupICAO(data, iataCode);
  if (icao) {
    const ref = data.icaoToShape[icao];
    const bundled = ref ? fromBundle(data, ref[0], ref[1]) : null;
    if (bundled) {
      return { ...bundled, icao };
    }
    const builtin = BUILTIN_SHAPES[icao];
    if (builtin) {
      return { svg: builtin.svg, rotate: true, icao, width: 1, height: 1 };
    }
  }
  const fallback = fromBundle(data, data.defaultShape, 1);
  if (fallback) {
    return { ...fallback, icao };
  }
  return { svg: GENERIC_PLANE_SVG, rotate: true, icao, width: 1, height: 1 };
}

export async function ensureLoaded() {
  return load();
}
