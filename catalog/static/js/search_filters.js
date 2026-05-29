// static/js/search_filters.js

import { setupPeriodicTable } from './periodic.js';

document.addEventListener("DOMContentLoaded", () => {

  const compField = document.getElementById('composition-json');
  const searchForm = document.getElementById('search-form');
  const elementModal = document.getElementById('elementModal');
  const elementTrigger = document.getElementById('element-modal-trigger');
  const baseTriggerLabel = elementTrigger ? (elementTrigger.dataset.label || elementTrigger.textContent).trim() : '';
  let submittedCompositionValue = compField ? (compField.value || '[]') : '[]';
  let compositionDirty = false;
  let initialSelections = [];
  if (compField && compField.value) {
    try {
      const parsed = JSON.parse(compField.value);
      if (Array.isArray(parsed)) {
        initialSelections = parsed;
      }
    } catch {}
  }

  const summaryRoot = document.getElementById('composition-summary');
  const summaryList = summaryRoot ? summaryRoot.querySelector('[data-summary-list]') : null;
  const summaryEmpty = summaryRoot ? summaryRoot.querySelector('[data-summary-empty]') : null;
  let autoSubmitReady = false;
  let submitTimer = null;
  let inflightController = null;
  const currentQueryString = () => {
    if (!searchForm) return '';
    const params = new URLSearchParams(new FormData(searchForm));
    return params.toString();
  };
  let lastSubmittedQuery = currentQueryString();
  const resultsContainer = document.getElementById('browse-results');
  const bindResultRowClicks = () => {
    if (!resultsContainer) return;
    resultsContainer.querySelectorAll('.browse-row-clickable').forEach((row) => {
      if (row.dataset.clickBound === 'true') return;
      row.dataset.clickBound = 'true';
      row.addEventListener('click', (event) => {
        if (event.target.closest('a, button, input, select, textarea, label')) {
          return;
        }
        const href = row.dataset.href;
        if (href) {
          window.location.href = href;
        }
      });
    });
  };
  const submitNow = () => {
    if (!searchForm) return;
    const nextQuery = currentQueryString();
    if (nextQuery === lastSubmittedQuery) return;
    lastSubmittedQuery = nextQuery;
    const baseUrl = searchForm.getAttribute('action') || window.location.pathname;
    const url = `${baseUrl}?${nextQuery}`;

    if (!resultsContainer) {
      if (typeof searchForm.requestSubmit === 'function') {
        searchForm.requestSubmit();
        return;
      }
      searchForm.submit();
      return;
    }

    if (inflightController) {
      inflightController.abort();
    }
    inflightController = new AbortController();

    fetch(url, {
      method: 'GET',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      signal: inflightController.signal,
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Request failed: ${response.status}`);
        }
        return response.json();
      })
      .then((payload) => {
        if (!payload || typeof payload.results_html !== 'string') return;
        resultsContainer.innerHTML = payload.results_html;
        bindResultRowClicks();
        updateSearchModeIndicator(payload.search_mode);
        window.history.replaceState({}, '', url);
        if (payload.view_mode) {
          document.body.dataset.browseViewMode = payload.view_mode;
        }
      })
      .catch((error) => {
        if (error.name !== 'AbortError') {
          window.location.assign(url);
        }
      });
  };

  const scheduleSubmit = (delay = 250) => {
    if (!searchForm || !autoSubmitReady) return;
    if (submitTimer) {
      window.clearTimeout(submitTimer);
    }
    submitTimer = window.setTimeout(() => {
      submitNow();
    }, delay);
  };

  const updateElementTriggerLabel = (count = 0) => {
    if (!elementTrigger || !baseTriggerLabel) return;
    elementTrigger.textContent = count ? `${baseTriggerLabel} (${count})` : baseTriggerLabel;
  };

  const updateSearchModeIndicator = (mode) => {
    const indicator = document.getElementById('search-mode-indicator');
    if (!indicator) return;
    indicator.dataset.mode = mode || 'none';
    if (mode === 'semantic') {
      indicator.innerHTML = '<span class="badge bg-primary" title="Ranked by vector similarity across composition, structure, and synthesis embeddings.">AI</span>';
    } else if (mode === 'auid') {
      indicator.innerHTML = '<span class="badge bg-warning text-dark" title="Semantic search unavailable — matching against material ID instead.">Fallback</span>';
    } else {
      indicator.innerHTML = '';
    }
  };

  const renderSummary = (items = []) => {
    if (!summaryRoot) return;
    if (!items.length) {
      if (summaryList) {
        summaryList.innerHTML = '';
        summaryList.classList.add('d-none');
      }
      if (summaryEmpty) {
        summaryEmpty.classList.remove('d-none');
      }
      updateElementTriggerLabel(0);
      return;
    }

    if (summaryEmpty) {
      summaryEmpty.classList.add('d-none');
    }
    if (summaryList) {
      summaryList.classList.remove('d-none');
      summaryList.innerHTML = '';
      items.forEach(([sym, ratio]) => {
        const li = document.createElement('li');
        li.className = 'composition-chip';
        const symSpan = document.createElement('span');
        symSpan.className = 'chip-symbol';
        symSpan.textContent = sym;
        li.appendChild(symSpan);
        if (ratio !== null && ratio !== undefined && ratio !== '') {
          const ratioSpan = document.createElement('span');
          ratioSpan.className = 'chip-ratio';
          ratioSpan.textContent = `: ${ratio}`;
          li.appendChild(ratioSpan);
        }
        summaryList.appendChild(li);
      });
    }
    updateElementTriggerLabel(items.length);
  };

  renderSummary(initialSelections);

  // Init periodic table with restored selections
  setupPeriodicTable({
    requireSelection: false,
    showRatios: true,
    enforceRatios: false,
    initialSelections,
    onChange: (items) => {
      renderSummary(items);
      const serialized = JSON.stringify(items ?? []);
      compositionDirty = serialized !== submittedCompositionValue;
    }
  });

  if (elementModal && searchForm) {
    elementModal.addEventListener('hidden.bs.modal', () => {
      if (!compositionDirty) return;
      submittedCompositionValue = compField ? (compField.value || '[]') : '[]';
      compositionDirty = false;
      submitNow();
    });
  }

  // Live updates for top-level form controls
  const searchInput = document.getElementById('search');
  const structureSelect = document.getElementById('structure_family');
  const availabilityChecks = [
    document.getElementById('has_literature'),
    document.getElementById('has_experiments'),
    document.getElementById('has_computational'),
  ].filter(Boolean);
  const affiliationChecks = Array.from(
    searchForm ? searchForm.querySelectorAll('input[name="affiliations"]') : []
  );

  if (searchInput) {
    searchInput.addEventListener('input', () => scheduleSubmit(550));
  }
  const stepsInput = document.getElementById('steps-q');
  if (stepsInput) {
    stepsInput.addEventListener('input', () => scheduleSubmit(550));
  }
  const viewRadios = document.querySelectorAll('input[name="view"]');
  viewRadios.forEach((radio) => {
    radio.addEventListener('change', () => {
      if (!searchForm) return;
      const params = new URLSearchParams(new FormData(searchForm));
      const baseUrl = searchForm.getAttribute('action') || window.location.pathname;
      window.location.assign(`${baseUrl}?${params.toString()}`);
    });
  });
  if (structureSelect) {
    structureSelect.addEventListener('change', () => scheduleSubmit(250));
  }
  availabilityChecks.forEach((cb) => {
    cb.addEventListener('change', () => scheduleSubmit(250));
  });
  affiliationChecks.forEach((cb) => {
    cb.addEventListener('change', () => scheduleSubmit(250));
  });

  // --- Temperature Slider---
  const minSlider  = document.getElementById('temp-min-slider');
  const maxSlider  = document.getElementById('temp-max-slider');
  const minHidden  = document.getElementById('temp-min');
  const maxHidden  = document.getElementById('temp-max');
  const minInput   = document.getElementById('temp-min-input');
  const maxInput   = document.getElementById('temp-max-input');
  const label      = document.getElementById('temp-range-label');
  const fill       = document.getElementById('temp-range-fill');

  const hasSliderElements = [
    minSlider,
    maxSlider,
    minHidden,
    maxHidden,
    minInput,
    maxInput,
    label,
    fill,
  ].every(Boolean);

  if (hasSliderElements) {
    const minV = parseInt(minSlider.min, 10);
    const maxV = parseInt(maxSlider.max, 10);
    const span = maxV - minV;

    function clamp(x, lo, hi) {
      if (Number.isNaN(x)) return lo;
      return Math.min(hi, Math.max(lo, x));
    }

    function pct(val) {
      return ((val - minV) / span) * 100;
    }

    function clampAndRender() {
      let a = parseInt(minSlider.value, 10);
      let b = parseInt(maxSlider.value, 10);
      a = clamp(a, minV, maxV);
      b = clamp(b, minV, maxV);
      if (a > b) [a, b] = [b, a];

      minSlider.value = a;
      maxSlider.value = b;
      minInput.value = a;
      maxInput.value = b;
      minHidden.value = a;
      maxHidden.value = b;
      label.textContent = `${a}°C – ${b}°C`;

      const left = pct(a);
      const right = 100 - pct(b);
      fill.style.left  = `${left}%`;
      fill.style.right = `${right}%`;
      scheduleSubmit(700);
    }

    function syncFromInputs() {
      let a = clamp(parseInt(minInput.value, 10), minV, maxV);
      let b = clamp(parseInt(maxInput.value, 10), minV, maxV);
      if (a > b) [a, b] = [b, a];
      minSlider.value = a;
      maxSlider.value = b;
      clampAndRender();
    }

    minInput.addEventListener('change', syncFromInputs);
    maxInput.addEventListener('change', syncFromInputs);
    minInput.addEventListener('blur', syncFromInputs);
    maxInput.addEventListener('blur', syncFromInputs);
    [minSlider, maxSlider].forEach(el => {
      el.addEventListener('input', clampAndRender);
      el.addEventListener('change', () => scheduleSubmit(250));
    });
    clampAndRender();

    const tempToggle = document.getElementById('temp-filter-enabled');
    const tempWrapper = document.getElementById('temp-filter-controls');
    if (tempToggle) {
      const syncTempFilterState = () => {
        const enabled = tempToggle.checked;
        [minSlider, maxSlider, minInput, maxInput].forEach((el) => {
          el.disabled = !enabled;
        });
        // Also disable the hidden inputs so `temp_min` / `temp_max` are
        // omitted from the submitted query string when the filter is off.
        // Browsers exclude `disabled` fields from form submission, which
        // keeps the URL clean and prevents the filter from looking active.
        minHidden.disabled = !enabled;
        maxHidden.disabled = !enabled;
        if (tempWrapper) {
          tempWrapper.classList.toggle('opacity-50', !enabled);
        }
      };
      syncTempFilterState();
      tempToggle.addEventListener('change', () => {
        syncTempFilterState();
        scheduleSubmit(150);
      });
    }
  }

  autoSubmitReady = true;
  bindResultRowClicks();

});
