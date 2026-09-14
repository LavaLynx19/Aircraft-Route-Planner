import { shapeFor, resolveICAO } from './shapes.js';

const popoverEl = () => document.getElementById('popover');

function showPopover(html, ev) {
  const el = popoverEl();
  el.innerHTML = html;
  el.hidden = false;
  const pad = 12;
  const rect = el.getBoundingClientRect();
  let x = ev.clientX + 14;
  let y = ev.clientY + 14;
  if (x + rect.width + pad > window.innerWidth) x = ev.clientX - rect.width - 14;
  if (y + rect.height + pad > window.innerHeight) y = ev.clientY - rect.height - 14;
  el.style.left = `${x}px`;
  el.style.top = `${y}px`;
}

function hidePopover() {
  popoverEl().hidden = true;
}

function airlineLabel(leg) {
  if (leg.airline_name && leg.airline) return `${leg.airline_name} (${leg.airline})`;
  return leg.airline_name || leg.airline || 'unknown';
}

function flightLabel(leg) {
  return leg.flight_number ? ` · ${leg.flight_number}` : '';
}

function legPopoverHTML(leg) {
  const icao = resolveICAO(leg.aircraft);
  const bmpSrc = icao ? `/assets/aircraft/details/${icao}.bmp` : null;
  return `
    <div class="label">${leg.from} → ${leg.to}</div>
    <div class="sub">${airlineLabel(leg)}${flightLabel(leg)} · ${Math.round(leg.distance_km).toLocaleString()} km</div>
    <div class="sub">Aircraft: ${leg.aircraft}${icao ? ` (${icao})` : ''}</div>
    ${bmpSrc ? `<img src="${bmpSrc}" alt="" onerror="this.remove()" />` : ''}
  `;
}

function silhouetteDataUrl(iataCode, color) {
  const { svg } = shapeFor(iataCode);
  // Inline color so it survives the data: URL (currentColor doesn't propagate
  // through background-image).
  const colored = svg.replace(/fill="currentColor"/g, `fill="${color}"`);
  return `data:image/svg+xml;utf8,${encodeURIComponent(colored)}`;
}

function legRowHTML(leg) {
  const muted = leg.is_target ? '' : 'non-target';
  const color = leg.is_target ? '#2563eb' : '#9ca3af';
  const url = silhouetteDataUrl(leg.aircraft, color);
  return `
    <div class="leg ${muted}" data-from="${leg.from}" data-to="${leg.to}">
      <div class="silhouette" style="background-image:url('${url}')"></div>
      <div class="text">
        <div>${leg.aircraft} ${leg.from}→${leg.to}</div>
        <div class="sub">${airlineLabel(leg)}${flightLabel(leg)}</div>
      </div>
      <div>${Math.round(leg.distance_km).toLocaleString()} km</div>
    </div>
  `;
}

// Render airport-mode results: an array of routes + the airports map.
export function renderRouteResults(container, routes, airports, { onRouteClick }) {
  container.innerHTML = '';
  if (!routes || routes.length === 0) {
    container.innerHTML = `<div class="empty">No routes found.</div>`;
    return;
  }

  routes.forEach((route, idx) => {
    const card = document.createElement('div');
    card.className = 'route-card';
    const last = route.path[route.path.length - 1];
    card.innerHTML = `
      <div class="header">
        <div class="title">${route.path.join(' → ')}</div>
        <div class="meta">${route.hops} hop${route.hops === 1 ? '' : 's'} · ${Math.round(route.total_distance_km).toLocaleString()} km</div>
      </div>
      <div class="legs">${route.legs.map(legRowHTML).join('')}</div>
    `;
    card.addEventListener('click', () => {
      document.querySelectorAll('.route-card.active').forEach((x) => x.classList.remove('active'));
      card.classList.add('active');
      const coords = route.path
        .map((iata) => {
          const ap = airports[iata];
          return ap && Number.isFinite(ap.lat) && Number.isFinite(ap.lon) ? [ap.lat, ap.lon] : null;
        })
        .filter(Boolean);
      onRouteClick(coords, route, idx);
    });
    card.querySelectorAll('.leg').forEach((row, legIdx) => {
      const leg = route.legs[legIdx];
      row.addEventListener('mouseenter', (e) => showPopover(legPopoverHTML(leg), e));
      row.addEventListener('mousemove', (e) => showPopover(legPopoverHTML(leg), e));
      row.addEventListener('mouseleave', hidePopover);
    });
    container.appendChild(card);
  });
}

// Render country-mode results: an array of CountryRouteGroup.
export function renderCountryResults(container, groups, airports, { onGroupClick }) {
  container.innerHTML = '';
  if (!groups || groups.length === 0) {
    container.innerHTML = `<div class="empty">No routes found.</div>`;
    return;
  }

  groups.forEach((g, idx) => {
    const card = document.createElement('div');
    card.className = 'route-card';
    card.innerHTML = `
      <div class="header">
        <div class="title">${g.from.iata} → ${g.to.iata}</div>
        <div class="meta">${g.routes.length} aircraft</div>
      </div>
      <div class="sub" style="color:#6b7280;font-size:11px;margin-bottom:6px">
        ${g.from.city} → ${g.to.city}
      </div>
      <div class="legs">
        ${g.routes.map((r) => `
          <div class="leg" data-aircraft="${r.aircraft}">
            <div class="silhouette" style="background-image:url('${silhouetteDataUrl(r.aircraft, '#2563eb')}')"></div>
            <div class="text">
              <div>${r.aircraft}</div>
              <div class="sub">${airlineLabel(r)}${flightLabel(r)}</div>
            </div>
            <div>${Math.round(r.distance_km).toLocaleString()} km</div>
          </div>
        `).join('')}
      </div>
    `;
    card.addEventListener('click', () => {
      document.querySelectorAll('.route-card.active').forEach((x) => x.classList.remove('active'));
      card.classList.add('active');
      const a = airports[g.from.iata] || g.from;
      const b = airports[g.to.iata] || g.to;
      const coords = [];
      if (a && Number.isFinite(a.lat) && Number.isFinite(a.lon)) coords.push([a.lat, a.lon]);
      if (b && Number.isFinite(b.lat) && Number.isFinite(b.lon)) coords.push([b.lat, b.lon]);
      onGroupClick(coords, g, idx);
    });
    container.appendChild(card);
  });
}

export function renderError(container, err) {
  container.innerHTML = `<div class="error"><b>${err.code || 'Error'}:</b> ${err.message || 'Unknown error'}</div>`;
}
