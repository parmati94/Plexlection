/**
 * API client.
 *
 * URLs are always relative — the Vite dev proxy and nginx both map /api to the
 * backend, so there is never an environment-specific base URL to configure.
 */

async function apiFetch(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  // Don't retry a 401 — the session is gone, go get a new one.
  if (res.status === 401) {
    window.location.replace('/login.html');
    return new Promise(() => {}); // never resolves; freezes callers during navigation
  }

  const text = await res.text();
  const data = text ? JSON.parse(text) : {};

  if (!res.ok) {
    const err = new Error(data.detail ?? `Request failed (${res.status})`);
    err.status = res.status;
    err.payload = data; // guard/busy responses carry a diff or run we want to show
    throw err;
  }
  return data;
}

const qs = (params) => {
  const clean = Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== '' && v !== null && v !== undefined),
  );
  const s = new URLSearchParams(clean).toString();
  return s ? `?${s}` : '';
};

export const api = {
  health: () => apiFetch('/api/health'),

  auth: {
    status: () => apiFetch('/api/auth/status'),
    login: (username, password) =>
      apiFetch('/api/auth/login', { method: 'POST', body: { username, password } }),
    logout: () => apiFetch('/api/auth/logout', { method: 'POST' }),
  },

  settings: {
    get: () => apiFetch('/api/settings'),
    // Partial patches are deep-merged server-side; secrets sent back as the
    // mask mean "unchanged".
    update: (patch) => apiFetch('/api/settings', { method: 'PUT', body: patch }),
    test: (service, instance) =>
      apiFetch(
        `/api/settings/test/${service}${instance ? `?instance=${encodeURIComponent(instance)}` : ''}`,
        { method: 'POST' },
      ),
    plexSections: () => apiFetch('/api/settings/plex/sections'),
    schedule: () => apiFetch('/api/settings/schedule'),
  },

  paths: {
    health: () => apiFetch('/api/paths'),
    test: (mappings, samples = []) =>
      apiFetch('/api/paths/test', { method: 'POST', body: { mappings, samples } }),
    browse: (path) => apiFetch(`/api/paths/browse${qs({ path })}`),
  },

  items: {
    list: (params = {}) => apiFetch(`/api/items${qs(params)}`),
    stats: () => apiFetch('/api/items/stats'),
    get: (id) => apiFetch(`/api/items/${id}`),
  },

  facts: {
    // The payload the rule builder generates itself from.
    registry: () => apiFetch('/api/facts/registry'),
    coverage: () => apiFetch('/api/facts/coverage'),
    values: (key, q = '') => apiFetch(`/api/facts/${encodeURIComponent(key)}/values${qs({ q })}`),
  },

  rules: {
    list: () => apiFetch('/api/rules'),
    get: (id) => apiFetch(`/api/rules/${id}`),
    create: (body) => apiFetch('/api/rules', { method: 'POST', body }),
    update: (id, body) => apiFetch(`/api/rules/${id}`, { method: 'PUT', body }),
    remove: (id) => apiFetch(`/api/rules/${id}`, { method: 'DELETE' }),
    // Called on every keystroke in the builder — one compiled query, so cheap.
    preview: (body) => apiFetch('/api/rules/preview', { method: 'POST', body }),
    explain: (body) => apiFetch('/api/rules/explain', { method: 'POST', body }),
    matches: (id) => apiFetch(`/api/rules/${id}/matches`),
  },

  collections: {
    list: () => apiFetch('/api/collections'),
    // Ignores the guards — a preview should always show what a refused sync
    // would have done.
    diff: (id) => apiFetch(`/api/collections/${id}/diff`, { method: 'POST' }),
    sync: (id, { dryRun = null, force = false } = {}) =>
      apiFetch(`/api/collections/${id}/sync${qs({ dry_run: dryRun, force })}`, { method: 'POST' }),
    syncAll: () => apiFetch('/api/collections/sync-all', { method: 'POST' }),
    unsync: (id, stripAll = false) =>
      apiFetch(`/api/collections/${id}/unsync${qs({ strip_all: stripAll })}`, { method: 'POST' }),
    history: (id) => apiFetch(`/api/collections/${id}/history`),
    uploadPoster: (id, file) => {
      // multipart, so it opts out of the JSON wrapper deliberately.
      const form = new FormData();
      form.append('file', file);
      return fetch(`/api/collections/${id}/poster`, {
        method: 'POST', body: form, credentials: 'same-origin',
      }).then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail ?? `Upload failed (${r.status})`);
        return d;
      });
    },
    removePoster: (id) => apiFetch(`/api/collections/${id}/poster`, { method: 'DELETE' }),
  },

  scan: {
    state: () => apiFetch('/api/scan/state'),
    runs: (limit = 20) => apiFetch(`/api/scan/runs${qs({ limit })}`),
    start: (body = {}) => apiFetch('/api/scan', { method: 'POST', body }),
    discover: () => apiFetch('/api/scan/discover', { method: 'POST' }),
    cancel: () => apiFetch('/api/scan/cancel', { method: 'POST' }),
  },
};
