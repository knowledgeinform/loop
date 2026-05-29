// Shared precursor helpers used by both the per-user management page
// (/account/precursors/) and the upload form's Weighing step.
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
    err.existing = payload && payload.existing;
    err.payload = payload;
    throw err;
  }
  return payload;
}

function invalidate() { cachedList = null; }

export async function loadUserPrecursors({ force = false } = {}) {
  if (!force && cachedList) return cachedList;
  const data = await jsonFetch(apiPath('/api/precursors/'));
  cachedList = Array.isArray(data.precursors) ? data.precursors : [];
  return cachedList;
}

export async function savePrecursor(payload, csrfToken) {
  const data = await jsonFetch(apiPath('/api/precursors/create/'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken,
    },
    body: JSON.stringify(payload),
  });
  invalidate();
  return data.precursor;
}

// Convenience: save with a browser confirm() on duplicate-CAS collisions.
export async function savePrecursorWithConfirm(payload, csrfToken, confirmFn = window.confirm) {
  try {
    return await savePrecursor(payload, csrfToken);
  } catch (exc) {
    if (exc && exc.status === 409 && exc.code === 'duplicate_cas') {
      if (!confirmFn(exc.message + '\n\nSave anyway?')) throw exc;
      return await savePrecursor({ ...payload, force: true }, csrfToken);
    }
    throw exc;
  }
}

export async function updatePrecursor(id, payload, csrfToken) {
  const data = await jsonFetch(apiPath(`/api/precursors/${encodeURIComponent(id)}/update/`), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken,
    },
    body: JSON.stringify(payload),
  });
  invalidate();
  return data.precursor;
}

export async function updatePrecursorWithConfirm(id, payload, csrfToken, confirmFn = window.confirm) {
  try {
    return await updatePrecursor(id, payload, csrfToken);
  } catch (exc) {
    if (exc && exc.status === 409 && exc.code === 'duplicate_cas') {
      if (!confirmFn(exc.message + '\n\nSave anyway?')) throw exc;
      return await updatePrecursor(id, { ...payload, force: true }, csrfToken);
    }
    throw exc;
  }
}

export async function deletePrecursor(id, csrfToken) {
  const data = await jsonFetch(apiPath(`/api/precursors/${encodeURIComponent(id)}/delete/`), {
    method: 'POST',
    headers: { 'X-CSRFToken': csrfToken },
  });
  invalidate();
  return data;
}

export async function lookupCAS(cas) {
  const q = encodeURIComponent((cas || '').trim());
  if (!q) return null;
  return await jsonFetch(apiPath(`/api/precursors/cas-lookup/?cas=${q}`));
}
