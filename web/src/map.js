import L from 'leaflet';
import { shapeFor, ensureLoaded } from './shapes.js';

// Canvas renderer batches all polylines onto a single <canvas>. Far cheaper
// than per-arc <path> elements when a search returns dozens of routes.
const arcRenderer = L.canvas({ padding: 0.5 });

const TARGET_STYLE = {
  color: '#2563eb',
  weight: 3,
  opacity: 0.9,
  renderer: arcRenderer,
};

const NON_TARGET_STYLE = {
  color: '#9ca3af',
  weight: 2,
  opacity: 0.6,
  dashArray: '8,6',
  renderer: arcRenderer,
};

const ARC_VERTICES = 48;
const ICON_PX = 26;

let map = null;
let routeLayer = null;
const layerByRouteId = new Map();

function toRad(deg) {
  return (deg * Math.PI) / 180;
}

function toDeg(rad) {
  return (rad * 180) / Math.PI;
}

function bearing(a, b) {
  const φ1 = toRad(a.lat);
  const φ2 = toRad(b.lat);
  const Δλ = toRad(b.lon - a.lon);
  const y = Math.sin(Δλ) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
}

// Spherical-linear interpolated great-circle path between two airports.
// Returns an array of [lat, lon] pairs with longitudes UNWRAPPED — meaning
// the lon values may run past ±180. Leaflet (with `worldCopyJump: true`)
// renders them correctly across the antimeridian without the "long stretch"
// that bare `L.Polyline` shows when consecutive lon values jump by ~360°.
//
// `bulgeDegrees` (default 0) shifts the midpoint perpendicular to the chord
// by that many degrees, peaking at t=0.5 and decaying to 0 at the endpoints.
// Use a small fixed value (≤ ~3°) so the bulge stays bounded regardless of
// the chord length — long-haul legs (LAX→SYD) and short hops both get the
// same visual spacing when they're fanned for shared-leg disambiguation.
function arcCoords(from, to, { vertices = 48, bulgeDegrees = 0 } = {}) {
  const φ1 = toRad(from.lat);
  const φ2 = toRad(to.lat);
  const λ1 = toRad(from.lon);
  const λ2 = toRad(to.lon);

  // Unit vectors on the sphere.
  const ax = Math.cos(φ1) * Math.cos(λ1);
  const ay = Math.cos(φ1) * Math.sin(λ1);
  const az = Math.sin(φ1);
  const bx = Math.cos(φ2) * Math.cos(λ2);
  const by = Math.cos(φ2) * Math.sin(λ2);
  const bz = Math.sin(φ2);

  const dot = Math.max(-1, Math.min(1, ax * bx + ay * by + az * bz));
  const ω = Math.acos(dot);

  if (ω === 0 || !Number.isFinite(ω)) {
    return [
      [from.lat, from.lon],
      [to.lat, to.lon],
    ];
  }

  const sinω = Math.sin(ω);
  const out = [];
  let prevLon = from.lon;

  for (let i = 0; i <= vertices; i++) {
    const t = i / vertices;
    const A = Math.sin((1 - t) * ω) / sinω;
    const B = Math.sin(t * ω) / sinω;
    const x = A * ax + B * bx;
    const y = A * ay + B * by;
    const z = A * az + B * bz;
    const lat = toDeg(Math.atan2(z, Math.sqrt(x * x + y * y)));
    let lon = toDeg(Math.atan2(y, x));

    // Unwrap relative to the previous vertex — keeps the polyline continuous
    // when the path crosses ±180°.
    while (lon - prevLon > 180) lon -= 360;
    while (lon - prevLon < -180) lon += 360;
    out.push([lat, lon]);
    prevLon = lon;
  }

  if (bulgeDegrees !== 0 && out.length >= 3) {
    const [lat1, lon1] = out[0];
    const [lat2, lon2] = out[out.length - 1];
    const dx = lon2 - lon1;
    const dy = lat2 - lat1;
    const len = Math.sqrt(dx * dx + dy * dy);
    if (len > 0) {
      // Perpendicular unit vector in lon/lat space, rotated 90° CCW. The
      // displacement is in degrees and is independent of chord length — so
      // long chords get the same modest visual offset as short ones.
      const nx = -dy / len;
      const ny = dx / len;
      for (let i = 1; i < out.length - 1; i++) {
        const t = i / (out.length - 1);
        const b = bulgeDegrees * Math.sin(Math.PI * t);
        out[i][0] += ny * b;
        out[i][1] += nx * b;
      }
    }
  }
  return out;
}

// Fan-out config: only legs shared between routes get a bulge. Fan width
// scales with chord length so long-haul shared legs (LAX→SYD) get a generous
// spread and short hops a proportionally smaller one. Capped at FAN_MAX_DEG
// so transpacific chords don't bend off the visible map.
const FAN_FRACTION = 0.55; // fan-width as fraction of chord length (deg)
const FAN_MAX_DEG = 42; // hard cap for very long chords
const FAN_MIN_DEG = 10; // floor so 2-route fans on short legs stay visible

// Shortest signed lat/lon chord between two points (handles ±180° wrap).
function chordLen(from, to) {
  let dlon = to.lon - from.lon;
  while (dlon > 180) dlon -= 360;
  while (dlon < -180) dlon += 360;
  const dlat = to.lat - from.lat;
  return Math.sqrt(dlon * dlon + dlat * dlat);
}

// Walk every leg of every route and index which routes use each (from, to)
// edge. Used by bulgeForLeg() to decide whether a leg needs to be fanned and
// at what slot.
function buildLegShares(routes) {
  const shares = new Map();
  routes.forEach((route, ridx) => {
    route.legs.forEach((leg) => {
      const key = `${leg.from}|${leg.to}`;
      if (!shares.has(key)) shares.set(key, []);
      shares.get(key).push(ridx);
    });
  });
  return shares;
}

// Returns the per-leg bulge in degrees. Unique legs return 0 (clean great-
// circle). Legs shared by N routes get evenly-spaced slots across a fan width
// that scales with chord length but is clamped to [FAN_MIN_DEG, FAN_MAX_DEG].
function bulgeForLeg(shares, leg, routeIdx, from, to) {
  const users = shares.get(`${leg.from}|${leg.to}`);
  if (!users || users.length <= 1) return 0;
  const slot = users.indexOf(routeIdx);
  if (slot < 0) return 0;
  const fanWidth = Math.min(
    FAN_MAX_DEG,
    Math.max(FAN_MIN_DEG, chordLen(from, to) * FAN_FRACTION)
  );
  const step = fanWidth / (users.length - 1);
  const center = (users.length - 1) / 2;
  return (slot - center) * step;
}

// Where to place the aircraft icon along the arc (0=start, 1=end). For unique
// legs returns 0.5 (geometric midpoint). For shared legs returns a slot-based
// position so each route's icon sits at a different point along the leg,
// preventing them from piling up on top of each other at the midpoint.
function iconTForLeg(shares, leg, routeIdx) {
  const users = shares.get(`${leg.from}|${leg.to}`);
  if (!users || users.length <= 1) return 0.5;
  const slot = users.indexOf(routeIdx);
  if (slot < 0) return 0.5;
  // Spread icons across most of the leg so they never overlap. Clamp inside
  // [0.10, 0.90] to keep them off the endpoint airport markers.
  const lo = 0.1;
  const hi = 0.9;
  return lo + ((slot + 0.5) / users.length) * (hi - lo);
}

function aircraftIcon(iataCode, bearingDeg, isTarget) {
  const { svg, rotate, width, height } = shapeFor(iataCode);
  // Silhouettes are drawn nose-up (north), so rotating by the leg's bearing points
  // them along it. Size follows the aircraft: shapeFor scales each type relative to
  // the standard marker, as tar1090 does, so an A380 draws larger than a Dash 8.
  const rot = rotate ? bearingDeg : 0;
  const color = isTarget ? '#2563eb' : '#6b7280';
  const w = Math.round(ICON_PX * width);
  const h = Math.round(ICON_PX * height);
  // Force fill via inline replacement so the marker renders even if `color`
  // inheritance is broken by Leaflet's marker CSS chain.
  const colored = svg
    .replace(/fill="currentColor"/g, `fill="${color}"`)
    .replace(/<svg /, `<svg width="${w}" height="${h}" `);
  const html = `<div class="aircraft-marker-wrap" style="transform:rotate(${rot}deg);width:${w}px;height:${h}px;">${colored}</div>`;
  return L.divIcon({
    className: 'aircraft-marker',
    html,
    iconSize: [w, h],
    iconAnchor: [w / 2, h / 2],
  });
}

export function initMap(elementId) {
  map = L.map(elementId, {
    worldCopyJump: true,
    minZoom: 2,
  }).setView([20, 0], 2);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 18,
  }).addTo(map);

  routeLayer = L.layerGroup().addTo(map);
  return map;
}

export function clear() {
  if (routeLayer) routeLayer.clearLayers();
  layerByRouteId.clear();
}

function validCoord(ap) {
  return ap && Number.isFinite(ap.lat) && Number.isFinite(ap.lon);
}

// Renders a list of routes. `airports` is the IATA→AirportMeta map from response.meta.airports.
// `onLegHover(leg, event)` is called when a leg's aircraft icon is hovered.
export async function renderRoutes(routes, airports, { onLegHover, onLegOut } = {}) {
  await ensureLoaded();
  clear();
  if (!routes || routes.length === 0) return;

  const allCoords = [];
  const seenAirports = new Set();

  const shares = buildLegShares(routes);

  routes.forEach((route, ridx) => {
    const layers = [];
    try {
      for (const leg of route.legs) {
        const a = airports[leg.from];
        const b = airports[leg.to];
        if (!validCoord(a) || !validCoord(b)) continue;
        const from = { lat: a.lat, lon: a.lon };
        const to = { lat: b.lat, lon: b.lon };

        const bulgeDegrees = bulgeForLeg(shares, leg, ridx, from, to);
        const coords = arcCoords(from, to, { vertices: ARC_VERTICES, bulgeDegrees });
        const style = leg.is_target ? TARGET_STYLE : NON_TARGET_STYLE;
        const arc = L.polyline(coords, style);
        arc._baseStyle = style;
        arc.addTo(routeLayer);
        layers.push(arc);

        // Icon at a slot-specific t along the arc — keeps icons spread out
        // along shared legs instead of piling up at the midpoint. The bearing
        // comes from the local tangent so it follows whatever bulge applied.
        const t = iconTForLeg(shares, leg, ridx);
        const midIdx = Math.max(1, Math.min(coords.length - 2, Math.round(t * (coords.length - 1))));
        const mid = coords[midIdx];
        const tangentA = coords[Math.max(0, midIdx - 2)];
        const tangentB = coords[Math.min(coords.length - 1, midIdx + 2)];
        const brg = bearing(
          { lat: tangentA[0], lon: tangentA[1] },
          { lat: tangentB[0], lon: tangentB[1] }
        );
        const icon = aircraftIcon(leg.aircraft || '', brg, leg.is_target);
        const marker = L.marker([mid[0], mid[1]], { icon, riseOnHover: true });
        if (onLegHover) marker.on('mouseover', (e) => onLegHover(leg, e));
        if (onLegOut) marker.on('mouseout', (e) => onLegOut(leg, e));
        marker.addTo(routeLayer);
        layers.push(marker);

        allCoords.push([from.lat, from.lon], [to.lat, to.lon]);
        seenAirports.add(leg.from);
        seenAirports.add(leg.to);
      }
    } catch (err) {
      console.warn(`renderRoutes: route ${ridx} failed`, err, route);
    }
    layerByRouteId.set(ridx, layers);
  });

  for (const iata of seenAirports) {
    const ap = airports[iata];
    if (!ap) continue;
    const dot = L.circleMarker([ap.lat, ap.lon], {
      radius: 4,
      color: '#111827',
      weight: 1,
      fillColor: '#fff',
      fillOpacity: 1,
    }).bindTooltip(`<b>${ap.iata}</b> &mdash; ${ap.name}, ${ap.city}`, { direction: 'top' });
    dot.addTo(routeLayer);
  }

  if (allCoords.length > 0) {
    map.fitBounds(L.latLngBounds(allCoords), { padding: [40, 40], maxZoom: 6 });
  }
}

// Render country-mode airport-pair groups. Each group has from/to AirportMeta and a routes array.
export async function renderCountryGroups(groups, airports) {
  await ensureLoaded();
  clear();
  if (!groups || groups.length === 0) return;

  const allCoords = [];
  const seenAirports = new Set();

  groups.forEach((g, gidx) => {
    const a = airports[g.from.iata] || g.from;
    const b = airports[g.to.iata] || g.to;
    if (!validCoord(a) || !validCoord(b)) {
      layerByRouteId.set(gidx, []);
      return;
    }
    const from = { lat: a.lat, lon: a.lon };
    const to = { lat: b.lat, lon: b.lon };
    // Country-mode groups are one-per-airport-pair, so no fan-out — clean
    // great-circles read cleaner with the per-aircraft list in the sidebar.
    const coords = arcCoords(from, to, { vertices: ARC_VERTICES });
    const arc = L.polyline(coords, TARGET_STYLE);
    arc._baseStyle = TARGET_STYLE;
    arc.addTo(routeLayer);
    layerByRouteId.set(gidx, [arc]);

    allCoords.push([from.lat, from.lon], [to.lat, to.lon]);
    seenAirports.add(g.from.iata);
    seenAirports.add(g.to.iata);
  });

  for (const iata of seenAirports) {
    const ap = airports[iata];
    if (!ap) continue;
    const dot = L.circleMarker([ap.lat, ap.lon], {
      radius: 4,
      color: '#111827',
      weight: 1,
      fillColor: '#fff',
      fillOpacity: 1,
    }).bindTooltip(`<b>${ap.iata}</b> &mdash; ${ap.name}, ${ap.city}`, { direction: 'top' });
    dot.addTo(routeLayer);
  }

  if (allCoords.length > 0) {
    map.fitBounds(L.latLngBounds(allCoords), { padding: [40, 40], maxZoom: 6 });
  }
}

// Zoom the map to fit a single route or group by its airport coords.
export function fitRoute(coords) {
  if (!map || !coords) return;
  const valid = coords.filter(
    (c) => Array.isArray(c) && Number.isFinite(c[0]) && Number.isFinite(c[1])
  );
  if (valid.length === 0) return;
  if (valid.length === 1) {
    map.setView(valid[0], 6);
    return;
  }
  map.fitBounds(L.latLngBounds(valid), { padding: [60, 60], maxZoom: 7 });
}

// Highlight one route (bolder + brighter); fade all others. Pass null to clear.
export function highlightRoute(ridx) {
  if (!routeLayer) return;
  const selectedLayers = layerByRouteId.get(ridx);

  for (const [rid, layers] of layerByRouteId.entries()) {
    const isSelected = rid === ridx;
    const isFaded = ridx != null && !isSelected;
    for (const layer of layers) {
      if (layer instanceof L.Path) {
        const base = layer._baseStyle || TARGET_STYLE;
        if (isSelected) {
          layer.setStyle({ weight: base.weight + 2, opacity: 1 });
        } else if (isFaded) {
          layer.setStyle({ weight: base.weight, opacity: 0.18 });
        } else {
          layer.setStyle({ weight: base.weight, opacity: base.opacity });
        }
      } else if (layer instanceof L.Marker) {
        if (isSelected) {
          layer.setOpacity(1);
          layer.setZIndexOffset(1000);
        } else if (isFaded) {
          layer.setOpacity(0.2);
          layer.setZIndexOffset(0);
        } else {
          layer.setOpacity(1);
          layer.setZIndexOffset(0);
        }
      }
    }
  }

  if (selectedLayers && selectedLayers.length > 0) {
    // Bring the selected route to the front (canvas path order matters less,
    // but for markers it surfaces the icons over faded ones).
    selectedLayers.forEach((l) => {
      if (l.bringToFront) l.bringToFront();
    });
  }
}

export function clearHighlight() {
  highlightRoute(null);
}
