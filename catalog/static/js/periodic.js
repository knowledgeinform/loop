export function setupPeriodicTable({
  requireSelection = true,
  showRatios = true,
  enforceRatios = true,
  initialSelections = null,
  onChange = null,
  lockSelection = false,
} = {}) {
  const grid         = document.getElementById('ptable');
  const selectedWrap = document.getElementById('selected-elements');
  const hasField     = document.getElementById('has-element');       // validity only
  const compField    = document.getElementById('composition-json');  // JSON round-trip (optional)
  const orderInput   = document.getElementById('element-order');     // existing

  if (!grid || !selectedWrap) {
    console.warn('Periodic table mount points are missing. Skipping setup.');
    return null;
  }

  // Controller returned to callers. Methods are no-ops until the async element
  // load finishes; `ready` resolves once they are wired (used by batch upload).
  const controller = {
    ready: null,
    select: () => {},
    deselect: () => {},
    clear: () => {},
    setSelections: () => {},
    getSelections: () => [],
  };

  controller.ready = (async () => {
    let elements = [];
    try {
      const defaultElementsUrl = new URL('./elements.json', import.meta.url).href;
      const response = await fetch(window.ELEMENTS_URL || defaultElementsUrl);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      elements = await response.json();
    } catch (err) {
      console.error('Unable to load periodic table data.', err);
      return;
    }

    // state: Map<sym, number|null>
    const state = new Map();
    let clickOrder = [];

    const changeHandler = typeof onChange === 'function' ? onChange : null;
    const isLocked = lockSelection || window.LOCK_COMPOSITION === true;

    function currentList() {
      return Array.from(state.entries()).map(([s, r]) => [s, r]);
    }

    function emitChange() {
      if (changeHandler) {
        changeHandler(currentList());
      }
    }

    function syncHidden() {
      // 1) validity flag
      if (hasField) {
        hasField.value = clickOrder.length ? '1' : '';
        hasField.required = requireSelection;
        hasField.setCustomValidity('');
      }
      // 2) element order (your existing behavior)
      if (orderInput) orderInput.value = clickOrder.join(',');

      // 3) JSON payload for search page (if present)
      if (compField) {
        compField.value = JSON.stringify(currentList());
      }

      emitChange();
    }

    // Build the grid
    elements.forEach(el => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-outline-secondary element-btn';
      btn.textContent = el.symbol;
      btn.style.gridColumn = el.col;
      btn.style.gridRow = el.row;
      btn.dataset.symbol = el.symbol;
      if (isLocked) {
        btn.disabled = true;
        btn.setAttribute('aria-disabled', 'true');
        btn.style.pointerEvents = 'none';
        btn.style.opacity = '0.85';
      }
      grid.appendChild(btn);
    });
    
    function addRow(sym) {
      if (selectedWrap.querySelector(`[data-sym="${sym}"]`)) return;

      const row = document.createElement('div');
      row.className = 'input-group mb-2 align-items-center';
      row.dataset.sym = sym;

      // symbol
      let html = `<span class="input-group-text">${sym}</span>`;

      // ratio box
      if (showRatios) {
        // NOTE: no min="0" since we want strictly > 0 (we’ll enforce via JS)
        html += `<input id="ratio-${sym}" type="text" inputmode="decimal" class="form-control">`;
      }

      // remove btn
      if (!isLocked) {
        html += `<button type="button" class="btn btn-outline-danger">&times;</button>`;
      }
      row.innerHTML = html;

      if (showRatios) {
        const ratioInput = row.querySelector('input');

        if (enforceRatios) {
          ratioInput.name = `ratio_${sym}`;
          ratioInput.required = true;
        } else {
          ratioInput.removeAttribute('name');
          ratioInput.required = false;
        }
        if (isLocked) {
          ratioInput.readOnly = true;
          ratioInput.setAttribute('aria-readonly', 'true');
          ratioInput.style.pointerEvents = 'none';
          ratioInput.tabIndex = -1;
        }

        // Nice messages on submit if something’s wrong
        ratioInput.addEventListener('invalid', () => {
          const raw = ratioInput.value.trim();
          const num = raw === '' ? null : Number(raw);
          if (raw === '' && enforceRatios) {
            ratioInput.setCustomValidity(`Please enter a ratio for ${sym}.`);
          } else if (!Number.isFinite(num)) {
            ratioInput.setCustomValidity('Enter a number.');
          } else if (num <= 0) {
            ratioInput.setCustomValidity('Ratio must be greater than 0.');
          } else {
            ratioInput.setCustomValidity('');
          }
        });

        // Realtime validation — this is what catches 0
        const validate = () => {
          const raw = ratioInput.value.trim();
          const num = raw === '' ? null : Number(raw);

          if (raw === '') {
            // Empty: allowed only if not enforcing
            state.set(sym, null);
            if (enforceRatios) {
              ratioInput.setCustomValidity(`Please enter a ratio for ${sym}.`);
            } else {
              ratioInput.setCustomValidity('');
            }
          } else if (!Number.isFinite(num)) {
            state.set(sym, null);
            ratioInput.setCustomValidity('Enter a number.');
          } else if (num <= 0) {
            state.set(sym, null);
            ratioInput.setCustomValidity('Ratio must be greater than 0.');
          } else {
            state.set(sym, num);
            ratioInput.setCustomValidity('');
          }

          // show message immediately if user has interacted
          ratioInput.reportValidity();
          syncHidden();
        };

        ratioInput.addEventListener('input', validate);
        ratioInput.addEventListener('blur', validate);
      }

      // remove
      const removeButton = row.querySelector('button');
      if (removeButton && !isLocked) {
        removeButton.addEventListener('click', () => {
          const btn = grid.querySelector(`.element-btn[data-symbol="${sym}"]`);
          if (btn) btn.classList.remove('selected');
          clickOrder = clickOrder.filter(s => s !== sym);
          state.delete(sym);
          row.remove();
          syncHidden();
        });
      }

      selectedWrap.appendChild(row);
    }

    function removeRow(sym) {
      const row = selectedWrap.querySelector(`[data-sym="${sym}"]`);
      if (row) row.remove();
    }

    function select(sym, ratio = null) {
      if (state.has(sym)) return;
      state.set(sym, ratio);
      clickOrder.push(sym);
      const btn = grid.querySelector(`.element-btn[data-symbol="${sym}"]`);
      if (btn) btn.classList.add('selected');
      addRow(sym);
      if (showRatios && ratio != null) {
        const ratioInput = selectedWrap.querySelector(`#ratio-${sym}`);
        if (ratioInput) {
          ratioInput.value = ratio;
          ratioInput.dispatchEvent(new Event('input', { bubbles: true }));
        }
      } else {
        syncHidden();
      }
    }

    function deselect(sym) {
      if (!state.has(sym)) return;
      state.delete(sym);
      clickOrder = clickOrder.filter(s => s !== sym);
      const btn = grid.querySelector(`.element-btn[data-symbol="${sym}"]`);
      if (btn) btn.classList.remove('selected');
      removeRow(sym);
      syncHidden();
    }

    // Clicks on the periodic table
    if (!isLocked) {
      grid.addEventListener('click', e => {
        if (!e.target.matches('.element-btn')) return;
        const sym = e.target.dataset.symbol;
        if (state.has(sym)) {
          deselect(sym);
        } else {
          select(sym, null);
        }
      });
    }

    // HYDRATE from saved JSON if present
    let hydrated = null;
    if (Array.isArray(initialSelections)) {
      hydrated = initialSelections;
    } else if (compField && compField.value) {
      try { hydrated = JSON.parse(compField.value); } catch { hydrated = null; }
    }

    if (Array.isArray(hydrated)) {
      for (const [sym, ratio] of hydrated) {
        select(sym, ratio ?? null);
      }
    }

    // Expose imperative controls now that the grid + handlers exist.
    controller.select = select;
    controller.deselect = deselect;
    controller.getSelections = currentList;
    controller.clear = () => {
      for (const sym of Array.from(state.keys())) deselect(sym);
    };
    controller.setSelections = (list) => {
      controller.clear();
      (Array.isArray(list) ? list : []).forEach((entry) => {
        if (Array.isArray(entry)) {
          select(entry[0], entry[1] ?? null);
        } else if (entry && typeof entry === 'object') {
          select(entry.symbol || entry.element, entry.ratio ?? entry.value ?? null);
        }
      });
    };

    // Initial sync
    syncHidden();
  })();

  return controller;
}
