import { api } from './api.js';

const DEBOUNCE_MS = 220;

function debounced(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    return new Promise((resolve) => {
      t = setTimeout(() => resolve(fn(...args)), ms);
    });
  };
}

// Generic autocomplete bound to an <input>. `fetchOptions(q, signal)` returns an array of
// {label, value, sub?} objects. `onPick(option)` is called when one is selected.
function attachAutocomplete(input, fetchOptions, onPick) {
  const list = document.createElement('div');
  list.className = 'autocomplete-list';
  list.hidden = true;
  input.parentElement.appendChild(list);

  let controller = null;
  let activeIdx = -1;
  let options = [];

  const close = () => {
    list.hidden = true;
    list.innerHTML = '';
    activeIdx = -1;
    options = [];
  };

  const render = () => {
    list.innerHTML = '';
    options.forEach((opt, i) => {
      const div = document.createElement('div');
      div.className = 'item' + (i === activeIdx ? ' active' : '');
      div.innerHTML = `<div>${opt.label}</div>${opt.sub ? `<div style="color:#9ca3af;font-size:11px">${opt.sub}</div>` : ''}`;
      div.addEventListener('mousedown', (e) => {
        e.preventDefault();
        pick(i);
      });
      list.appendChild(div);
    });
    list.hidden = options.length === 0;
  };

  const pick = (i) => {
    const opt = options[i];
    if (!opt) return;
    input.value = opt.value;
    input.dataset.label = opt.label;
    close();
    onPick(opt);
  };

  const fetch = debounced(async (q) => {
    if (controller) controller.abort();
    controller = new AbortController();
    if (!q || q.length < 1) {
      close();
      return;
    }
    try {
      const opts = await fetchOptions(q, controller.signal);
      options = opts;
      activeIdx = opts.length > 0 ? 0 : -1;
      render();
    } catch (e) {
      if (e.name !== 'AbortError') console.error('autocomplete fetch:', e);
    }
  }, DEBOUNCE_MS);

  input.addEventListener('input', (e) => {
    fetch(e.target.value.trim());
  });

  input.addEventListener('keydown', (e) => {
    if (list.hidden) return;
    if (e.key === 'ArrowDown') {
      activeIdx = Math.min(activeIdx + 1, options.length - 1);
      render();
      e.preventDefault();
    } else if (e.key === 'ArrowUp') {
      activeIdx = Math.max(activeIdx - 1, 0);
      render();
      e.preventDefault();
    } else if (e.key === 'Enter') {
      if (activeIdx >= 0) {
        pick(activeIdx);
        e.preventDefault();
      }
    } else if (e.key === 'Escape') {
      close();
    }
  });

  input.addEventListener('blur', () => setTimeout(close, 150));
}

function airportOptions(results) {
  return results.map((a) => ({
    value: a.iata,
    label: `${a.iata} — ${a.name}`,
    sub: `${a.city}, ${a.country}`,
  }));
}

function aircraftOptions(results) {
  return results.map((a) => ({
    value: a.iata,
    label: `${a.iata} — ${a.name}`,
    sub: a.family ? `Family ${a.family} (ICAO ${a.icao})` : `ICAO ${a.icao}`,
  }));
}

function countryOptions(results) {
  return results.map((c) => ({ value: c, label: c }));
}

// Inputs and segmented buttons are rendered from `state`, never left to their
// markup defaults. The two scopes share one state object but build their own
// markup, so a field that ignored state showed a placeholder while the search
// still used the old value — a country search silently kept the airport tab's
// aircraft filter (found in the 2026-09-19 Phase 5 verification), and the mode
// and max-hops buttons re-rendered as Inclusive/2 whatever had been picked.
const escapeAttr = (v) =>
  String(v ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

// Build the controls UI inside `container`. Returns an object with `getState()`,
// `setScope(s)`, and a `submit` callback invoked when the user clicks Search.
export function buildControls(container, { onSubmit, scope = 'airport' }) {
  let currentScope = scope;

  const state = {
    aircraft: '',
    from: '',
    to: '',
    fromCountry: '',
    toCountry: '',
    mode: 'inclusive',
    maxHops: 2,
  };

  const render = () => {
    container.innerHTML = '';
    if (currentScope === 'airport') {
      container.innerHTML = `
        <div>
          <label for="ac">Aircraft</label>
          <div class="field"><input id="ac" placeholder="e.g. A380, 388" autocomplete="off" value="${escapeAttr(state.aircraft)}" /></div>
        </div>
        <div class="row">
          <div class="field"><label for="from">From</label><input id="from" placeholder="LHR" autocomplete="off" value="${escapeAttr(state.from)}" /></div>
          <div class="field"><label for="to">To</label><input id="to" placeholder="SYD" autocomplete="off" value="${escapeAttr(state.to)}" /></div>
        </div>
        <div>
          <label>Max hops</label>
          <div class="segmented" id="hops">
            <button data-hops="0"${state.maxHops === 0 ? ' class="active"' : ''}>0</button>
            <button data-hops="1"${state.maxHops === 1 ? ' class="active"' : ''}>1</button>
            <button data-hops="2"${state.maxHops === 2 ? ' class="active"' : ''}>2</button>
            <button data-hops="3"${state.maxHops === 3 ? ' class="active"' : ''}>3</button>
          </div>
        </div>
        <div>
          <label>Mode</label>
          <div class="segmented" id="mode">
            <button data-mode="strict"${state.mode === 'strict' ? ' class="active"' : ''}>Strict</button>
            <button data-mode="inclusive"${state.mode === 'inclusive' ? ' class="active"' : ''}>Inclusive</button>
          </div>
        </div>
        <button id="search-btn">Search</button>
      `;
      attachAutocomplete(
        container.querySelector('#ac'),
        async (q, signal) => aircraftOptions((await api.aircraft(q, signal)).data || []),
        (opt) => (state.aircraft = opt.value)
      );
      attachAutocomplete(
        container.querySelector('#from'),
        async (q, signal) => airportOptions((await api.airports(q, signal)).data || []),
        (opt) => (state.from = opt.value)
      );
      attachAutocomplete(
        container.querySelector('#to'),
        async (q, signal) => airportOptions((await api.airports(q, signal)).data || []),
        (opt) => (state.to = opt.value)
      );

      // also allow plain typed IATA codes
      container.querySelector('#ac').addEventListener('change', (e) => (state.aircraft = e.target.value.trim()));
      container.querySelector('#from').addEventListener('change', (e) => (state.from = e.target.value.trim().toUpperCase()));
      container.querySelector('#to').addEventListener('change', (e) => (state.to = e.target.value.trim().toUpperCase()));
    } else {
      container.innerHTML = `
        <div class="row">
          <div class="field"><label for="fc">From country</label><input id="fc" placeholder="United Kingdom" autocomplete="off" value="${escapeAttr(state.fromCountry)}" /></div>
          <div class="field"><label for="tc">To country</label><input id="tc" placeholder="Australia" autocomplete="off" value="${escapeAttr(state.toCountry)}" /></div>
        </div>
        <div>
          <label for="ac2">Aircraft (optional)</label>
          <div class="field"><input id="ac2" placeholder="any" autocomplete="off" value="${escapeAttr(state.aircraft)}" /></div>
        </div>
        <button id="search-btn">Search</button>
      `;
      attachAutocomplete(
        container.querySelector('#fc'),
        async (q, signal) => countryOptions((await api.countries(q, signal)).data || []),
        (opt) => (state.fromCountry = opt.value)
      );
      attachAutocomplete(
        container.querySelector('#tc'),
        async (q, signal) => countryOptions((await api.countries(q, signal)).data || []),
        (opt) => (state.toCountry = opt.value)
      );
      attachAutocomplete(
        container.querySelector('#ac2'),
        async (q, signal) => aircraftOptions((await api.aircraft(q, signal)).data || []),
        (opt) => (state.aircraft = opt.value)
      );
      container.querySelector('#ac2').addEventListener('change', (e) => (state.aircraft = e.target.value.trim()));
      container.querySelector('#fc').addEventListener('change', (e) => (state.fromCountry = e.target.value.trim()));
      container.querySelector('#tc').addEventListener('change', (e) => (state.toCountry = e.target.value.trim()));
    }

    const hopsGroup = container.querySelector('#hops');
    if (hopsGroup) {
      hopsGroup.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-hops]');
        if (!b) return;
        hopsGroup.querySelectorAll('button').forEach((x) => x.classList.remove('active'));
        b.classList.add('active');
        state.maxHops = parseInt(b.dataset.hops, 10);
      });
    }

    const modeGroup = container.querySelector('#mode');
    if (modeGroup) {
      modeGroup.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-mode]');
        if (!b) return;
        modeGroup.querySelectorAll('button').forEach((x) => x.classList.remove('active'));
        b.classList.add('active');
        state.mode = b.dataset.mode;
      });
    }

    container.querySelector('#search-btn').addEventListener('click', () => onSubmit(state, currentScope));
  };

  render();

  return {
    setScope(s) {
      currentScope = s;
      render();
    },
    getState: () => state,
  };
}
