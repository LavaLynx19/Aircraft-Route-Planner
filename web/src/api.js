// One function per backend endpoint. All return the parsed { data, meta } envelope
// or throw an Error with .code/.message populated from the API's ErrorResponse.

const BASE = ''; // relative; Vite proxies /routes etc. to :8080 in dev

async function request(path, params, signal) {
  const url = new URL(BASE + path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
    }
  }
  const resp = await fetch(url.pathname + url.search, { signal });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(body?.error?.message || `HTTP ${resp.status}`);
    err.code = body?.error?.code || 'HTTP_ERROR';
    err.status = resp.status;
    throw err;
  }
  return body;
}

export const api = {
  routes: (params, signal) => request('/routes', params, signal),
  routesFrom: (params, signal) => request('/routes/from', params, signal),
  routesCountries: (params, signal) => request('/routes/countries', params, signal),
  airports: (q, signal) => request('/airports/search', { q }, signal),
  aircraft: (q, signal) => request('/aircraft/search', { q }, signal),
  countries: (q, signal) => request('/countries/search', { q }, signal),
  health: (signal) => request('/health', {}, signal),
};
