import { setupPeriodicTable } from './periodic.js';
import { setupSpacegroupUI } from './spacegroup.js';

document.addEventListener('DOMContentLoaded', () => {
  const lockByContext =
    window.LOCK_COMPOSITION === true ||
    Boolean(document.querySelector('input[name="material_auid"]'));

  function getElements() {
    const f = document.getElementById("composition-json");
    if (!f || !f.value) return [];
    try {
      const data = JSON.parse(f.value);
      if (Array.isArray(data)) return data;
      if (data && typeof data === "object") return Object.entries(data);
      return [];
    } catch { return []; }
  }
  function getStructureFamily() {
    const sf = document.getElementById("structure_family");
    return sf ? sf.value : "other";
  }

  const sgUI = setupSpacegroupUI({ getElements, getStructureFamily });

  const ptableController = setupPeriodicTable({
    requireSelection: true,
    showRatios: true,
    enforceRatios: true,
    lockSelection: lockByContext,
    onChange: () => { if (sgUI) sgUI.rebuildSiteRows(); },
  });

  const modalElement = document.getElementById('csvPreviewModal');
  const confirmBtn = document.getElementById('confirm-upload-btn');
  const bootstrapGlobal = window.bootstrap || window.Bootstrap;
  const csrfInput = document.querySelector('#csv-upload-form input[name="csrfmiddlewaretoken"]');

  if (!modalElement || !bootstrapGlobal) {
    return;
  }

  const modalInstance = bootstrapGlobal.Modal.getOrCreateInstance(modalElement);
  const dataset = modalElement.dataset || {};
  let pendingUploadId = dataset.uploadId || '';
  const cancelUrl = dataset.cancelUrl || '';
  const csrfToken = csrfInput ? csrfInput.value : null;

  if (!pendingUploadId || !cancelUrl || !csrfToken) {
    return;
  }

  let confirmed = false;
  let cancelling = false;

  const clearPendingState = () => {
    pendingUploadId = '';
  };

  const buildPayload = () => {
    const params = new URLSearchParams();
    params.append('upload_id', pendingUploadId);
    params.append('csrfmiddlewaretoken', csrfToken);
    return params;
  };

  const redirectAfterCancel = () => {
    const url = new URL(window.location.href);
    url.searchParams.set('cancelled', '1');
    url.hash = '';
    window.location.href = url.toString();
  };

  const cancelPendingUpload = () => {
    if (!pendingUploadId || cancelling) {
      return;
    }
    cancelling = true;
    const body = buildPayload().toString();

    fetch(cancelUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
      },
      body,
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Cancel upload failed with status ${response.status}`);
        }
        return response.json().catch(() => ({}));
      })
      .then(() => {
        clearPendingState();
        redirectAfterCancel();
      })
      .catch((error) => {
        console.error(error);
      })
      .finally(() => {
        cancelling = false;
      });
  };

  const sendBeaconCancel = () => {
    if (!pendingUploadId || !('sendBeacon' in navigator)) {
      return;
    }
    const body = buildPayload().toString();
    const blob = new Blob([body], { type: 'application/x-www-form-urlencoded' });
    navigator.sendBeacon(cancelUrl, blob);
    clearPendingState();
  };

  if (confirmBtn) {
    confirmBtn.addEventListener('click', () => {
      confirmed = true;
      if (document.activeElement === confirmBtn) {
        confirmBtn.blur();
      }
      clearPendingState();
    });
  }

  modalElement.addEventListener('hidden.bs.modal', () => {
    if (!confirmed && pendingUploadId) {
      cancelPendingUpload();
    }
  });

  window.addEventListener('beforeunload', () => {
    if (!confirmed && pendingUploadId) {
      sendBeaconCancel();
    }
  });
});
