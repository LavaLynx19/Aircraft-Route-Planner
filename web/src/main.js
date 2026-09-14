import './styles.css';
import { api } from './api.js';
import { buildControls } from './controls.js';
import {
  initMap,
  renderRoutes,
  renderCountryGroups,
  fitRoute,
  highlightRoute,
} from './map.js';
import { renderRouteResults, renderCountryResults, renderError } from './sidebar.js';

const resultsEl = document.getElementById('results');
const controlsEl = document.getElementById('controls');

initMap('map');

let currentScope = 'airport';

async function handleSubmit(state, scope) {
  resultsEl.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    if (scope === 'airport') {
      if (!state.aircraft || !state.from) {
        renderError(resultsEl, { code: 'MISSING_INPUT', message: 'Aircraft and origin are required.' });
        return;
      }
      const params = {
        aircraft: state.aircraft,
        from: state.from,
        max_hops: state.maxHops,
        mode: state.mode,
      };
      let resp;
      if (state.to) {
        resp = await api.routes({ ...params, to: state.to });
      } else {
        resp = await api.routesFrom(params);
      }
      const airports = resp.meta?.airports || {};
      await renderRoutes(resp.data || [], airports);
      renderRouteResults(resultsEl, resp.data || [], airports, {
        onRouteClick: (coords, _route, idx) => {
          highlightRoute(idx);
          fitRoute(coords);
        },
      });
    } else {
      if (!state.fromCountry || !state.toCountry) {
        renderError(resultsEl, { code: 'MISSING_INPUT', message: 'Both countries are required.' });
        return;
      }
      const resp = await api.routesCountries({
        from: state.fromCountry,
        to: state.toCountry,
        aircraft: state.aircraft || undefined,
      });
      const airports = resp.meta?.airports || {};
      await renderCountryGroups(resp.data || [], airports);
      renderCountryResults(resultsEl, resp.data || [], airports, {
        onGroupClick: (coords, _g, idx) => {
          highlightRoute(idx);
          fitRoute(coords);
        },
      });
    }
  } catch (err) {
    renderError(resultsEl, err);
  }
}

const controls = buildControls(controlsEl, { onSubmit: handleSubmit, scope: currentScope });

// Scope tab wiring.
document.querySelectorAll('#scope-tabs button').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#scope-tabs button').forEach((x) => x.classList.remove('active'));
    btn.classList.add('active');
    currentScope = btn.dataset.scope;
    controls.setScope(currentScope);
    resultsEl.innerHTML = '';
  });
});
