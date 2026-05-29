document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.field-errors').forEach(span => {
    const name = span.dataset.for;
    const msg  = (span.textContent || '').trim();
    if (!name || !msg) return;

    const input = document.querySelector(`[name="${CSS.escape(name)}"]`);
    if (!input) return;

    input.classList.add('is-invalid');
    input.setAttribute('data-bs-title', msg);

    const tip = new bootstrap.Tooltip(input, {
      trigger: 'manual',
      container: 'body',
      placement: 'bottom',                  // ⬅️ always below
      popperConfig: (defaultCfg) => ({
        ...defaultCfg,
        modifiers: [
          ...(defaultCfg?.modifiers || []),
          { name: 'offset', options: { offset: [0, 8] } },      // a little gap
          { name: 'flip',   options: { fallbackPlacements: [] } } // don't flip up/right/left
        ]
      }),
    });

    try { tip.show(); } catch (_) {}

    // Optional: hide once user starts fixing the field
    input.addEventListener('input', () => { try { tip.hide(); } catch (_) {} }, { once: true });
    input.addEventListener('blur',  () => { try { tip.hide(); } catch (_) {} });
    input.addEventListener('focus', () => { try { tip.show(); } catch (_) {} });
  });
});