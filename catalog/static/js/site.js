/* LOOP — site behaviours. Bespoke replacements for the small Bootstrap JS
 * surface we removed: tabs, modals, dropdowns, and inline field errors.
 * No framework dependency. */
(function () {
  'use strict';

  /* --------------------------------------------------------------- Tabs
   * Markup: <button data-bs-toggle="tab" data-bs-target="#pane">…</button>
   * with sibling .tab-pane elements. Click + arrow-key support. */
  function activateTab(btn) {
    var sel = btn.getAttribute('data-bs-target') || btn.getAttribute('href');
    if (!sel) return;
    var pane = document.querySelector(sel);
    if (!pane) return;

    var tablist = btn.closest('[role="tablist"], .nav-tabs, .nav');
    if (!tablist) {
      // Inline trigger outside the tab bar: activate the real nav tab instead
      // so the bar's active state stays in sync.
      var real = document.querySelector(
        '[role="tablist"] [data-bs-toggle="tab"][data-bs-target="' + sel + '"], ' +
        '.nav-tabs [data-bs-toggle="tab"][data-bs-target="' + sel + '"]'
      );
      if (real && real !== btn) { activateTab(real); return; }
    }
    var group = tablist ? tablist.querySelectorAll('[data-bs-toggle="tab"]') : [btn];
    group.forEach(function (b) {
      b.classList.remove('active');
      b.setAttribute('aria-selected', 'false');
      b.setAttribute('tabindex', '-1');
    });
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    btn.removeAttribute('tabindex');

    var content = pane.parentElement;
    if (content) {
      content.querySelectorAll(':scope > .tab-pane').forEach(function (p) {
        p.classList.remove('show', 'active');
      });
    }
    pane.classList.add('show', 'active');
  }

  document.addEventListener('click', function (e) {
    var tab = e.target.closest('[data-bs-toggle="tab"]');
    if (tab) {
      e.preventDefault();
      activateTab(tab);
    }
  });

  document.addEventListener('keydown', function (e) {
    var tab = e.target.closest('[data-bs-toggle="tab"]');
    if (!tab) return;
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    var tablist = tab.closest('[role="tablist"], .nav-tabs, .nav');
    if (!tablist) return;
    var tabs = Array.prototype.slice.call(tablist.querySelectorAll('[data-bs-toggle="tab"]'));
    var i = tabs.indexOf(tab);
    var next = e.key === 'ArrowRight' ? tabs[i + 1] || tabs[0] : tabs[i - 1] || tabs[tabs.length - 1];
    if (next) {
      e.preventDefault();
      activateTab(next);
      next.focus();
    }
  });

  /* -------------------------------------------------------------- Modals
   * Markup: Bootstrap-structured .modal divs. Open via
   * [data-bs-toggle="modal"][data-bs-target="#id"]; close via
   * [data-bs-dismiss="modal"], backdrop click, or Escape. */
  var openModal = null;
  var lastFocused = null;
  var backdropEl = null;

  function trapFocusables(root) {
    return Array.prototype.slice.call(root.querySelectorAll(
      'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
    )).filter(function (el) { return el.offsetParent !== null; });
  }

  function showModal(modal) {
    if (!modal || openModal) return;
    lastFocused = document.activeElement;
    backdropEl = document.createElement('div');
    backdropEl.className = 'modal-backdrop-el';
    document.body.appendChild(backdropEl);
    backdropEl.addEventListener('click', hideModal);

    modal.classList.add('is-open');
    modal.removeAttribute('aria-hidden');
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    document.body.classList.add('modal-open');
    openModal = modal;

    var focusables = trapFocusables(modal);
    (focusables[0] || modal).focus();
  }

  function hideModal() {
    if (!openModal) return;
    var closed = openModal;
    closed.classList.remove('is-open');
    closed.setAttribute('aria-hidden', 'true');
    closed.removeAttribute('aria-modal');
    document.body.classList.remove('modal-open');
    if (backdropEl) { backdropEl.remove(); backdropEl = null; }
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    openModal = null;
    // Bootstrap-compatible close event for listeners (e.g. search_filters.js).
    closed.dispatchEvent(new CustomEvent('hidden.bs.modal', { bubbles: true }));
  }

  // Shared modal API for page scripts (focus trap, backdrop, Escape included).
  window.Loop = window.Loop || {};
  window.Loop.showModal = showModal;
  window.Loop.hideModal = hideModal;

  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-bs-toggle="modal"]');
    if (trigger) {
      e.preventDefault();
      var sel = trigger.getAttribute('data-bs-target');
      if (sel) showModal(document.querySelector(sel));
      return;
    }
    if (e.target.closest('[data-bs-dismiss="modal"]')) {
      e.preventDefault();
      hideModal();
    }
  });

  document.addEventListener('keydown', function (e) {
    if (!openModal) return;
    if (e.key === 'Escape') { hideModal(); return; }
    if (e.key === 'Tab') {
      var f = trapFocusables(openModal);
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  /* ------------------------------------------------------------ Dropdowns
   * [data-bs-toggle="dropdown"] toggles a sibling .dropdown-menu.
   * Click toggles; choosing an item, clicking outside, or Escape closes.
   * Mouse users also get hover-open with a short leave delay. */
  function setMenu(toggle, open) {
    var menu = toggle.parentElement.querySelector('.dropdown-menu');
    if (!menu) return;
    menu.classList.toggle('show', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function closeAllMenus(except) {
    document.querySelectorAll('.dropdown-menu.show').forEach(function (m) {
      var toggle = m.parentElement.querySelector('[data-bs-toggle="dropdown"]');
      if (toggle && toggle !== except) setMenu(toggle, false);
    });
  }

  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('[data-bs-toggle="dropdown"]');
    if (toggle) {
      e.preventDefault();
      var menu = toggle.parentElement.querySelector('.dropdown-menu');
      var isOpen = menu && menu.classList.contains('show');
      closeAllMenus(toggle);
      setMenu(toggle, !isOpen);
      return;
    }
    // Selecting an item (or clicking anywhere else) closes every menu.
    closeAllMenus(null);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var open = document.querySelector('.dropdown-menu.show');
    if (!open) return;
    var toggle = open.parentElement.querySelector('[data-bs-toggle="dropdown"]');
    closeAllMenus(null);
    if (toggle) toggle.focus();
  });

  if (window.matchMedia && window.matchMedia('(hover: hover)').matches) {
    var hoverCloseTimer = null;
    document.addEventListener('mouseover', function (e) {
      var wrap = e.target.closest('.nav-dropdown, .dropdown');
      var toggle = wrap && wrap.querySelector('[data-bs-toggle="dropdown"]');
      if (toggle) {
        clearTimeout(hoverCloseTimer);
        closeAllMenus(toggle);
        setMenu(toggle, true);
      }
    });
    document.addEventListener('mouseout', function (e) {
      var wrap = e.target.closest('.nav-dropdown, .dropdown');
      if (!wrap || wrap.contains(e.relatedTarget)) return;
      hoverCloseTimer = setTimeout(function () { closeAllMenus(null); }, 250);
    });
  }

  /* ------------------------------------------------ Copyable code samples
   * Every non-empty <pre> block site-wide gets a Copy button. */
  function addCopyButtons() {
    document.querySelectorAll('pre').forEach(function (pre) {
      if (pre.closest('.code-block')) return;
      var content = (pre.innerText || '').trim();
      if (!content) return;
      // The button must live OUTSIDE the scrollable <pre>: an absolutely
      // positioned child of a scroll container scrolls with the content and
      // clicking it scrolls the code sideways. Anchor it to a wrapper instead.
      var wrap = document.createElement('div');
      wrap.className = 'code-block';
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);
      var COPY_ICON = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/></svg>';
      var CHECK_ICON = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5"/></svg>';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'code-copy-btn';
      btn.innerHTML = COPY_ICON;
      btn.setAttribute('aria-label', 'Copy code sample to clipboard');
      btn.addEventListener('click', function () {
        var code = pre.querySelector('code');
        var text = (code ? code.innerText : pre.innerText).trim();
        var done = function () {
          btn.innerHTML = CHECK_ICON;
          btn.classList.add('copied');
          setTimeout(function () {
            btn.innerHTML = COPY_ICON;
            btn.classList.remove('copied');
          }, 1600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done);
        } else {
          var ta = document.createElement('textarea');
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          ta.remove();
          done();
        }
      });
      wrap.appendChild(btn);
    });
  }

  /* ------------------------------------------------------- Docs scrollspy
   * The Developer Center's "On this page" rail highlights the section in
   * view. */
  function initDeveloperDocs() {
    var nav = document.querySelector('.developer-docs-nav');
    if (nav && 'IntersectionObserver' in window) {
      var links = Array.prototype.slice.call(nav.querySelectorAll('a[href^="#"]'));
      var byId = {};
      links.forEach(function (a) { byId[a.getAttribute('href').slice(1)] = a; });
      var setActive = function (id) {
        links.forEach(function (a) { a.classList.remove('active'); });
        if (byId[id]) byId[id].classList.add('active');
      };
      var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) setActive(entry.target.id);
        });
      }, { rootMargin: '-20% 0px -70% 0px' });
      links.forEach(function (a) {
        var section = document.getElementById(a.getAttribute('href').slice(1));
        if (section) observer.observe(section);
      });
    }

  }

  function initPageEnhancements() {
    addCopyButtons();
    initDeveloperDocs();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initPageEnhancements);
  } else {
    initPageEnhancements();
  }

  /* --------------------------------------------------- Inline field errors
   * Server marks invalid fields with <span class="field-errors" data-for="name">msg</span>.
   * Show an accessible inline message under the field (replaces the old
   * Bootstrap tooltip). */
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.field-errors').forEach(function (span) {
      var name = span.dataset.for;
      var msg = (span.textContent || '').trim();
      if (!name || !msg) return;
      var input = document.querySelector('[name="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]');
      if (!input) return;

      input.classList.add('is-invalid');
      input.setAttribute('aria-invalid', 'true');

      var note = document.createElement('div');
      note.className = 'form-text field-error-text';
      note.style.color = 'var(--danger)';
      note.textContent = msg;
      var id = 'err-' + name.replace(/[^a-z0-9_-]/gi, '-');
      note.id = id;
      input.setAttribute('aria-describedby', id);
      (input.closest('.form-group, .mb-3, .mb-2') || input.parentElement).appendChild(note);

      input.addEventListener('input', function () {
        input.classList.remove('is-invalid');
        input.removeAttribute('aria-invalid');
        note.remove();
      }, { once: true });
    });
  });

  /* ------------------------------------------------- Trial comparison rows
   * Markup: <tr class="trial-row" data-xrd-url="…"> followed immediately by a
   * <tr class="trial-drawer" hidden>. Clicking (or Enter/Space on) the row
   * toggles the drawer and lazy-loads the trial's XRD pattern on first open.
   * Used by the material comparison table and the route page. */
  function loadTrialXrd(row, drawer) {
    if (row.dataset.loaded === '1') return;
    row.dataset.loaded = '1';
    var holder = drawer.querySelector('.trial-xrd');
    var noticeEl = drawer.querySelector('.trial-xrd-notice');
    var url = row.dataset.xrdUrl;
    if (!holder || !url) return;
    holder.setAttribute('data-state', 'loading');
    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        holder.innerHTML = '';
        if (data && data.ok && data.plot_url) {
          var img = document.createElement('img');
          img.className = 'trial-xrd-img';
          img.alt = 'XRD pattern for ' + (row.dataset.trialLabel || 'trial');
          img.loading = 'lazy';
          img.src = data.plot_url;
          holder.appendChild(img);
          holder.setAttribute('data-state', 'ready');
        } else {
          holder.setAttribute('data-state', 'empty');
        }
        if (noticeEl) {
          var msg = (data && data.notice) || '';
          if (data && data.ok && !data.plot_url && !msg) {
            msg = 'No plottable XRD data for this trial.';
          } else if (data && !data.ok && !msg) {
            msg = 'Plot unavailable for this trial.';
          }
          if (msg) { noticeEl.textContent = msg; noticeEl.hidden = false; }
        }
      })
      .catch(function () {
        holder.innerHTML = '';
        holder.setAttribute('data-state', 'empty');
        if (noticeEl) {
          noticeEl.textContent = 'Could not load the XRD pattern.';
          noticeEl.hidden = false;
        }
      });
  }

  function toggleTrialRow(row) {
    var drawer = row.nextElementSibling;
    if (!drawer || !drawer.classList.contains('trial-drawer')) return;
    if (drawer.hasAttribute('hidden')) {
      drawer.removeAttribute('hidden');
      row.setAttribute('aria-expanded', 'true');
      row.classList.add('is-open');
      loadTrialXrd(row, drawer);
    } else {
      drawer.setAttribute('hidden', '');
      row.setAttribute('aria-expanded', 'false');
      row.classList.remove('is-open');
    }
  }

  document.addEventListener('click', function (e) {
    var row = e.target.closest('.trial-row');
    if (!row) return;
    if (e.target.closest('a, button, input, label, [data-noexpand]')) return;
    toggleTrialRow(row);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
    var row = e.target.closest('.trial-row');
    if (!row || row !== document.activeElement) return;
    e.preventDefault();
    toggleTrialRow(row);
  });
})();
