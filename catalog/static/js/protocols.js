// Shared protocol helpers used by both the per-user management page
// (/account/protocols/) and the upload form's Synthesis Process section.
//
// All requests are same-origin JSON against the routes declared in
// catalog/urls.py; mutating calls require a CSRF token which the caller
// must pass in from a Django-rendered {% csrf_token %} form.

let cachedList = null;

function apiPath(path) {
  return `${window.LOOP_BASE_PATH || ''}${path}`;
}

async function jsonFetch(url, options = {}) {
  const resp = await fetch(url, {
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  let payload = null;
  try { payload = await resp.json(); } catch (_) { payload = null; }
  if (!resp.ok) {
    const msg = (payload && (payload.message || payload.error)) || `HTTP ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    err.code = payload && payload.error;
    err.payload = payload;
    throw err;
  }
  return payload;
}

function invalidate() { cachedList = null; }

export async function loadUserProtocols({ force = false } = {}) {
  if (!force && cachedList) return cachedList;
  const data = await jsonFetch(apiPath('/api/protocols/'));
  cachedList = Array.isArray(data.protocols) ? data.protocols : [];
  return cachedList;
}

export async function saveProtocol(payload, csrfToken) {
  const data = await jsonFetch(apiPath('/api/protocols/create/'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken,
    },
    body: JSON.stringify(payload),
  });
  invalidate();
  return data.protocol;
}

export async function updateProtocol(id, payload, csrfToken) {
  const data = await jsonFetch(apiPath(`/api/protocols/${encodeURIComponent(id)}/update/`), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken,
    },
    body: JSON.stringify(payload),
  });
  invalidate();
  return data.protocol;
}

export async function deleteProtocol(id, csrfToken) {
  const data = await jsonFetch(apiPath(`/api/protocols/${encodeURIComponent(id)}/delete/`), {
    method: 'POST',
    headers: { 'X-CSRFToken': csrfToken },
  });
  invalidate();
  return data;
}
