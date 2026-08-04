/* LOOP — shared batch-upload page behaviours: drag-and-drop file boxes,
 * upload-mode field visibility, and the live manifest preview. The preview's
 * key column comes from data-key-column on #manifest-preview-body. */
(function () {
  'use strict';

  // Wire every drag-and-drop file box on the page.
  document.querySelectorAll('.batch-dropzone').forEach(function (dropzone) {
    var input = document.getElementById(dropzone.dataset.input) ||
                document.querySelector('input[type="file"][name="' + dropzone.dataset.input.replace(/^id_/, '') + '"]');
    var label = document.getElementById(dropzone.dataset.label);
    if (!input || !label) return;

    input.classList.add('d-none');

    function fmtSize(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / 1048576).toFixed(1) + ' MB';
    }
    function show() {
      if (!input.files.length) {
        dropzone.classList.remove('is-filled');
        label.textContent = '';
        return;
      }
      var f = input.files[0];
      dropzone.classList.add('is-filled');
      label.innerHTML =
        '<span class="dropzone-check">&#10003;</span>' +
        '<span class="dropzone-name"></span> ' +
        '<span class="dropzone-size text-muted small fw-normal"></span>' +
        '<div class="small text-muted mt-1">Click to choose a different file.</div>';
      label.querySelector('.dropzone-name').textContent = f.name;
      label.querySelector('.dropzone-size').textContent = '(' + fmtSize(f.size) + ')';
    }

    dropzone.addEventListener('click', function () { input.click(); });
    input.addEventListener('change', show);

    ['dragenter', 'dragover'].forEach(function (evt) {
      dropzone.addEventListener(evt, function (e) {
        e.preventDefault(); dropzone.classList.add('bg-light');
      });
    });
    ['dragleave', 'drop'].forEach(function (evt) {
      dropzone.addEventListener(evt, function (e) {
        e.preventDefault(); dropzone.classList.remove('bg-light');
      });
    });
    dropzone.addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files; show();
      }
    });
  });

  // Show only the file inputs the selected upload mode needs (experimental
  // page only; the literature page has no mode radios).
  var manifestFiles = document.getElementById('manifest-files');
  var archiveFiles = document.getElementById('archive-files');
  var modeInputs = document.querySelectorAll('input[name="upload_mode"]');
  function syncFieldVisibility() {
    if (!modeInputs.length) return;
    var selected = document.querySelector('input[name="upload_mode"]:checked');
    var mode = selected ? selected.value : null;
    if (manifestFiles) manifestFiles.classList.toggle('d-none', mode === 'zip_only');
    if (archiveFiles) archiveFiles.classList.toggle('d-none', mode === 'with_existing_zip');
  }
  modeInputs.forEach(function (radio) {
    radio.addEventListener('change', syncFieldVisibility);
  });
  syncFieldVisibility();

  // Replace the example rows with a live preview of the chosen manifest.
  var manifestInput = document.getElementById('id_manifest');
  var previewBody = document.getElementById('manifest-preview-body');
  var previewCaption = document.getElementById('manifest-preview-caption');
  var previewCount = document.getElementById('manifest-preview-count');
  var previewHint = document.getElementById('manifest-preview-hint');
  var MAX_PREVIEW_ROWS = 100;
  var KEY_COLUMN = (previewBody && previewBody.dataset.keyColumn) || 'Batch ID';

  var exampleRowsHTML = previewBody ? previewBody.innerHTML : '';
  var exampleHintHTML = previewHint ? previewHint.innerHTML : '';

  function parseCSV(text) {
    var rows = [], row = [], field = '', inQuotes = false, i = 0;
    while (i < text.length) {
      var c = text[i];
      if (inQuotes) {
        if (c === '"') {
          if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
          inQuotes = false; i++; continue;
        }
        field += c; i++; continue;
      }
      if (c === '"') { inQuotes = true; i++; continue; }
      if (c === ',') { row.push(field); field = ''; i++; continue; }
      if (c === '\r') { i++; continue; }
      if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; i++; continue; }
      field += c; i++;
    }
    if (field.length || row.length) { row.push(field); rows.push(row); }
    return rows;
  }

  function colIndex(header, name) {
    var target = name.toLowerCase();
    for (var i = 0; i < header.length; i++) {
      if (String(header[i]).trim().toLowerCase() === target) return i;
    }
    return -1;
  }

  function cell(text) {
    var td = document.createElement('td');
    var code = document.createElement('code');
    if (!text) { code.classList.add('text-muted'); code.textContent = '—'; }
    else { code.textContent = text; }
    td.appendChild(code);
    return td;
  }

  function renderParsed(rows, filename) {
    if (!rows.length) { return false; }
    var header = rows[0];
    var keyIdx = colIndex(header, KEY_COLUMN);
    var compIdx = colIndex(header, 'Target composition');
    if (keyIdx === -1 || compIdx === -1) {
      previewCaption.textContent = 'Your manifest';
      previewCount.textContent = filename;
      previewBody.innerHTML = '';
      var tr = document.createElement('tr');
      var td = document.createElement('td');
      td.colSpan = 2; td.className = 'text-warning small';
      td.textContent = 'Couldn’t find “' + KEY_COLUMN + '” and “Target composition” columns — check your headers.';
      tr.appendChild(td); previewBody.appendChild(tr);
      previewHint.textContent = 'Preview parsed from ' + filename + '.';
      return true;
    }
    var data = rows.slice(1).filter(function (r) {
      return r.some(function (v) { return String(v).trim() !== ''; });
    });
    previewBody.innerHTML = '';
    data.slice(0, MAX_PREVIEW_ROWS).forEach(function (r) {
      var tr = document.createElement('tr');
      tr.appendChild(cell(String(r[keyIdx] == null ? '' : r[keyIdx]).trim()));
      tr.appendChild(cell(String(r[compIdx] == null ? '' : r[compIdx]).trim()));
      previewBody.appendChild(tr);
    });
    previewBody.dataset.state = 'manifest';
    previewCaption.textContent = 'Your manifest';
    previewCount.textContent = data.length + (data.length === 1 ? ' row' : ' rows');
    var extra = data.length > MAX_PREVIEW_ROWS
      ? ' (showing first ' + MAX_PREVIEW_ROWS + ')' : '';
    previewHint.textContent = 'Preview parsed from ' + filename + extra + '.';
    return true;
  }

  function showNonCSV(filename) {
    previewBody.innerHTML = '';
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = 2; td.className = 'text-muted small';
    td.textContent = "Couldn't preview this file here — your rows will still be validated when you click Scan / Preview.";
    tr.appendChild(td); previewBody.appendChild(tr);
    previewBody.dataset.state = 'manifest';
    previewCaption.textContent = 'Your manifest';
    previewCount.textContent = filename;
    previewHint.textContent = filename + ' selected.';
  }

  function previewXLSX(file) {
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var wb = XLSX.read(new Uint8Array(reader.result), { type: 'array' });
        var ws = wb.Sheets[wb.SheetNames[0]];
        var rows = XLSX.utils.sheet_to_json(ws, { header: 1, blankrows: false, defval: '' });
        if (!renderParsed(rows, file.name)) { showNonCSV(file.name); }
      } catch (err) {
        showNonCSV(file.name);
      }
    };
    reader.onerror = function () { showNonCSV(file.name); };
    reader.readAsArrayBuffer(file);
  }

  function restoreExample() {
    previewBody.innerHTML = exampleRowsHTML;
    previewBody.dataset.state = 'example';
    previewCaption.textContent = 'Example rows';
    previewCount.textContent = 'example only';
    previewHint.innerHTML = exampleHintHTML;
  }

  if (manifestInput && previewBody) {
    manifestInput.addEventListener('change', function () {
      if (!manifestInput.files.length) { restoreExample(); return; }
      var file = manifestInput.files[0];
      if (/\.csv$/i.test(file.name)) {
        var reader = new FileReader();
        reader.onload = function () {
          if (!renderParsed(parseCSV(String(reader.result)), file.name)) {
            showNonCSV(file.name);
          }
        };
        reader.onerror = function () { showNonCSV(file.name); };
        reader.readAsText(file);
      } else if (/\.xlsx?$/i.test(file.name) && window.XLSX) {
        previewXLSX(file);
      } else {
        showNonCSV(file.name);
      }
    });
  }
})();
