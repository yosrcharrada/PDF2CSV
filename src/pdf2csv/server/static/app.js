/* ==========================================================================
   PDF2CSV — interface behaviour

   Plain ES2020, no framework, no build step. That is a delivery decision, not
   a taste one: this ships inside an embeddable Python distribution on a
   locked-down desktop, and a toolchain that needs Node to produce a bundle is
   a toolchain that cannot be rebuilt by whoever maintains this in two years.

   All user-supplied text — filenames, cell values, error messages — is written
   with textContent and never with innerHTML. A statement containing a stray
   angle bracket is not exotic, and there is no reason to be parsing client
   documents as markup.
   ========================================================================== */

'use strict';

const api = {
  health:   () => fetch('/api/health').then(unwrap),
  jobs:     () => fetch('/api/jobs?limit=8').then(unwrap),
  job:      (id) => fetch(`/api/jobs/${id}`).then(unwrap),
  preview:  (id, table = 0) =>
    fetch(`/api/jobs/${id}/preview?limit=500&table=${table}`).then(unwrap),
  reveal:   (id) => fetch(`/api/jobs/${id}/reveal`, { method: 'POST' }).then(unwrap),
  upload(file, onProgress) {
    // XHR rather than fetch: upload progress is the one thing fetch still
    // cannot report, and a 40 MB scan over a slow disk needs a moving bar.
    return new Promise((resolve, reject) => {
      const body = new FormData();
      body.append('file', file, file.name);

      const request = new XMLHttpRequest();
      request.open('POST', '/api/jobs');
      request.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      });
      request.addEventListener('load', () => {
        let payload = {};
        try { payload = JSON.parse(request.responseText); } catch { /* keep {} */ }
        if (request.status >= 200 && request.status < 300) resolve(payload);
        else reject(new Error(payload.detail || `Upload failed (${request.status})`));
      });
      request.addEventListener('error', () =>
        reject(new Error('Lost contact with the application. Is its window still open?')));
      request.send(body);
    });
  },
};

async function unwrap(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

const $ = (id) => document.getElementById(id);

const state = {
  job: null,
  stream: null,
  ticker: null,
  outputPath: null,
  table: 0,          // which table of a multi-table document is on screen
};

/* ---------- Theme --------------------------------------------------------- */

const theme = {
  init() {
    const saved = localStorage.getItem('pdf2csv-theme');
    if (saved) document.documentElement.dataset.theme = saved;
    $('theme-toggle').addEventListener('click', theme.toggle);
  },
  toggle() {
    const root = document.documentElement;
    const dark = root.dataset.theme === 'dark'
      || (root.dataset.theme !== 'light'
          && window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.dataset.theme = dark ? 'light' : 'dark';
    localStorage.setItem('pdf2csv-theme', root.dataset.theme);
  },
};

/* ---------- Views --------------------------------------------------------- */

const VIEWS = ['view-idle', 'view-working', 'view-result', 'view-failed'];

function show(view) {
  VIEWS.forEach((id) => { $(id).hidden = (id !== view); });
  $('history').hidden = (view !== 'view-idle');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ---------- Formatting ---------------------------------------------------- */

function formatBytes(bytes) {
  if (!bytes) return '0 KB';
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

function formatDuration(seconds) {
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/* ---------- Toasts -------------------------------------------------------- */

function toast(message, kind = 'error') {
  const node = element('div', `toast ${kind}`);
  node.appendChild(icon(kind === 'error' ? 'alert' : 'info'));
  node.appendChild(element('span', null, message));
  $('toasts').appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .3s';
    setTimeout(() => node.remove(), 320);
  }, kind === 'error' ? 7000 : 4000);
}

/* ---------- Startup ------------------------------------------------------- */

async function boot() {
  theme.init();
  wireDropzone();
  wireButtons();

  try {
    const health = await api.health();
    $('version').textContent = `v${health.version}`;
    state.outputPath = health.paths.output;
    $('output-folder').hidden = false;

    $('dropzone-note').textContent = health.ocr.available
      ? `Bank statements, ledgers and journals. Scanned documents work too. Up to ${health.limits.max_upload_mb} MB.`
      : `Bank statements, ledgers and journals. Up to ${health.limits.max_upload_mb} MB.`;

    if (!health.ocr.available) showOcrBanner(health.ocr.reason);
  } catch {
    toast('Could not reach the application. Try closing this tab and starting it again.');
  }

  refreshHistory();
}

function showOcrBanner(reason) {
  const banner = element('div', 'banner');
  banner.appendChild(icon('alert'));
  const body = element('div');
  body.appendChild(element('strong', null, 'Scanned documents cannot be read on this install. '));
  body.appendChild(document.createTextNode(
    `${reason || ''} PDFs that contain real text will still work normally.`));
  banner.appendChild(body);
  $('banner-slot').appendChild(banner);
}

/* ---------- Choosing a file ----------------------------------------------- */

function wireDropzone() {
  const zone = $('dropzone');
  const input = $('file-input');

  zone.addEventListener('submit', (event) => event.preventDefault());
  zone.addEventListener('click', () => input.click());
  zone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      input.click();
    }
  });

  input.addEventListener('change', () => {
    if (input.files.length) startJob(input.files[0]);
    input.value = '';
  });

  // Counter, not a boolean: dragleave fires when the pointer crosses onto a
  // child element, and a boolean flag makes the highlight flicker.
  let depth = 0;
  ['dragenter', 'dragover'].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      if (name === 'dragenter') depth += 1;
      zone.classList.add('is-dragging');
    });
  });
  ['dragleave', 'drop'].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      depth = name === 'drop' ? 0 : Math.max(0, depth - 1);
      if (depth === 0) zone.classList.remove('is-dragging');
    });
  });

  zone.addEventListener('drop', (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) startJob(file);
  });

  // Dropping anywhere else must not make the browser navigate away from the
  // app to display the PDF — which looks exactly like the tool crashing.
  ['dragover', 'drop'].forEach((name) =>
    window.addEventListener(name, (event) => event.preventDefault()));
}

function wireButtons() {
  $('start-over').addEventListener('click', reset);
  $('failed-retry').addEventListener('click', reset);

  $('dl-csv').addEventListener('click', () => download('csv'));
  $('dl-xlsx').addEventListener('click', () => download('xlsx'));
  $('dl-json').addEventListener('click', () => download('json'));

  $('output-folder').addEventListener('click', async () => {
    if (state.job) {
      const result = await api.reveal(state.job.id).catch(() => null);
      if (result?.opened) return;
    }
    if (state.outputPath) {
      await navigator.clipboard?.writeText(state.outputPath).catch(() => {});
      toast(`Saved files are in ${state.outputPath} — path copied.`, 'info');
    }
  });
}

function download(kind) {
  if (!state.job) return;
  // A hidden link rather than window.open: no popup blocker, no blank tab
  // flashing open and shut, and the Content-Disposition filename is honoured.
  const link = document.createElement('a');
  link.href = `/api/jobs/${state.job.id}/download/${kind}?table=${state.table}`;
  link.download = '';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function reset() {
  stopStream();
  state.job = null;
  $('banner-slot').querySelectorAll('.upload-error').forEach((n) => n.remove());
  show('view-idle');
  refreshHistory();
}

/* ---------- Running a job -------------------------------------------------- */

async function startJob(file) {
  if (!/\.pdf$/i.test(file.name)) {
    toast('That is not a PDF. Please choose a file ending in .pdf');
    return;
  }

  show('view-working');
  $('working-filename').textContent = file.name;
  $('working-message').textContent = 'Uploading…';
  $('working-hint').textContent = '';
  setProgress(0);
  startTicker();

  try {
    const job = await api.upload(file, (fraction) => {
      // The upload is a small slice of the whole; the rest is extraction.
      setProgress(fraction * 0.04);
      if (fraction >= 1) $('working-message').textContent = 'Reading the document…';
    });
    state.job = job;
    listen(job.id);
  } catch (error) {
    stopTicker();
    showFailure(error.message);
  }
}

function listen(jobId) {
  stopStream();
  const stream = new EventSource(`/api/jobs/${jobId}/events`);
  state.stream = stream;

  stream.addEventListener('message', (event) => {
    const tick = JSON.parse(event.data);
    setProgress(tick.percent);
    $('working-message').textContent = tick.message;
    if (tick.stage === 'ocr') {
      $('working-hint').textContent =
        'This page is a scan, so it is being read with character recognition. '
        + 'That takes a few seconds per page.';
    }
  });

  stream.addEventListener('complete', (event) => {
    stopStream();
    stopTicker();
    finish(JSON.parse(event.data));
  });

  stream.addEventListener('error', () => {
    // EventSource retries by itself; fall back to polling only if the job is
    // genuinely unreachable, so a momentary blip does not look like a failure.
    stopStream();
    pollUntilDone(jobId);
  });
}

async function pollUntilDone(jobId) {
  for (let attempt = 0; attempt < 900; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 700));
    let job;
    try {
      job = await api.job(jobId);
    } catch {
      continue;
    }
    setProgress(job.percent);
    $('working-message').textContent = job.message;
    if (job.status === 'done' || job.status === 'failed') {
      stopTicker();
      finish(job);
      return;
    }
  }
  stopTicker();
  showFailure('This document is taking much longer than expected.');
}

function stopStream() {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
}

function setProgress(fraction) {
  const percent = Math.round(Math.min(Math.max(fraction, 0), 1) * 100);
  $('working-bar').style.width = `${percent}%`;
  $('working-bar-wrap').setAttribute('aria-valuenow', String(percent));
}

function startTicker() {
  stopTicker();
  const began = Date.now();
  state.ticker = setInterval(() => {
    $('working-elapsed').textContent = formatDuration((Date.now() - began) / 1000);
  }, 250);
}

function stopTicker() {
  if (state.ticker) {
    clearInterval(state.ticker);
    state.ticker = null;
  }
}

/* ---------- Results -------------------------------------------------------- */

async function finish(job) {
  state.job = job;
  state.table = 0;

  if (job.status === 'failed' || !job.result) {
    showFailure(job.error || 'The document could not be read.');
    return;
  }

  renderStats(job);
  renderTablePicker(job);
  show('view-result');
  await showTable(0);
}

/** Render one table of the document: its checks, its rows, its downloads. */
async function showTable(index) {
  const job = state.job;
  if (!job?.result) return;

  state.table = index;
  const tables = job.result.tables || [];
  const chosen = tables[index];

  // Each table carries its own reconciliation report, so the verdict and the
  // checks follow the selection rather than always describing the first one.
  const checks = chosen ? chosen.checks : job.result.checks;
  renderVerdict(job, checks || job.result.checks);
  renderChecks(checks || job.result.checks);

  document.querySelectorAll('#tables-list .table-chip').forEach((chip, i) => {
    chip.setAttribute('aria-selected', String(i === index));
  });

  try {
    const payload = await api.preview(job.id, index);
    renderPreview(payload, payload.flags || []);
  } catch (error) {
    toast(error.message);
  }
}

function renderTablePicker(job) {
  const tables = job.result.tables || [];
  const bar = $('tables-bar');

  // One table is the ordinary case; a picker for a single item is clutter.
  if (tables.length < 2) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;

  $('tables-title').textContent = `${tables.length} tables in this document`;
  $('tables-hint').textContent =
    'Pick one to see its rows and its checks. Each downloads separately; '
    + 'the Excel file contains all of them.';

  const list = $('tables-list');
  list.replaceChildren();

  tables.forEach((table, index) => {
    const chip = element('button', 'table-chip');
    chip.type = 'button';
    chip.setAttribute('role', 'tab');
    chip.setAttribute('aria-selected', String(index === state.table));

    chip.appendChild(element('span', 'chip-name', table.label));

    const meta = element('span', 'chip-meta');
    const dot = element('span', 'chip-dot');
    if (!table.passed) dot.classList.add('fail');
    meta.appendChild(dot);
    const pages = table.pages.length > 1
      ? `pages ${table.pages[0]}–${table.pages[table.pages.length - 1]}`
      : `page ${table.pages[0] ?? '?'}`;
    meta.appendChild(element('span', null,
      `${table.n_rows}×${table.n_columns} · ${pages}`));
    chip.appendChild(meta);

    chip.addEventListener('click', () => showTable(index));
    list.appendChild(chip);
  });
}

function renderVerdict(job, checks) {
  const failures = checks.filter((c) => !c.passed && c.severity === 'error');
  const warnings = checks.filter((c) => !c.passed && c.severity === 'warning');

  const verdict = $('verdict');
  verdict.classList.remove('is-review', 'is-fail');

  const swap = (name) => {
    const holder = verdict.querySelector('.verdict-icon');
    holder.replaceChildren(icon(name));
  };

  if (failures.length) {
    verdict.classList.add('is-fail');
    swap('alert');
    $('verdict-title').textContent = failures.length === 1
      ? 'One check did not pass'
      : `${failures.length} checks did not pass`;
    $('verdict-detail').textContent =
      'The figures below may be incomplete. Read the checks before using this file.';
  } else if (warnings.length) {
    verdict.classList.add('is-review');
    swap('alert');
    $('verdict-title').textContent = warnings.length === 1
      ? 'Worth a quick look'
      : `${warnings.length} things worth a quick look`;
    $('verdict-detail').textContent =
      'The totals add up. A few cells are highlighted below for you to confirm.';
  } else {
    swap('check');
    $('verdict-title').textContent = 'All checks passed';
    $('verdict-detail').textContent =
      'The totals in the document match the rows that were extracted.';
  }

  $('dl-xlsx').hidden = !job.downloads?.xlsx;
}

function renderStats(job) {
  const { result } = job;
  const document_ = result.document;

  const entries = [
    ['Rows', result.n_rows],
    ['Columns', result.columns.length],
    ['Pages', document_.n_pages],
  ];
  if (document_.n_scanned > 0) entries.push(['Scanned pages', document_.n_scanned]);
  entries.push(['Took', formatDuration(document_.duration_seconds)]);
  if (document_.profile && document_.profile !== 'generic') {
    entries.push(['Format', document_.profile]);
  }

  const strip = $('stats');
  strip.replaceChildren();
  entries.forEach(([label, value]) => {
    const stat = element('span', 'stat');
    stat.appendChild(element('span', null, label));
    stat.appendChild(element('b', null, value));
    strip.appendChild(stat);
  });
}

function renderChecks(checks) {
  const list = $('checks');
  list.replaceChildren();

  // Failures first: the analyst needs the problem, not a list to scroll.
  const ordered = [...checks].sort((a, b) => {
    const rank = (c) => (c.passed ? 2 : (c.severity === 'error' ? 0 : 1));
    return rank(a) - rank(b);
  });

  const failing = checks.filter((c) => !c.passed).length;
  $('checks-count').textContent = failing
    ? `${failing} of ${checks.length} need attention`
    : `${checks.length} passed`;

  ordered.forEach((check) => {
    const item = element('li', `check sev-${check.passed ? 'ok' : check.severity}`);
    item.setAttribute('role', 'button');
    item.setAttribute('tabindex', '0');
    item.setAttribute('aria-expanded', 'false');

    const badge = element('span', 'check-icon');
    badge.appendChild(icon(check.passed ? 'check' : (check.severity === 'info' ? 'info' : 'alert')));
    item.appendChild(badge);
    item.appendChild(element('span', 'check-title', check.title));

    const caret = icon('chevron');
    caret.classList.add('check-caret');
    item.appendChild(caret);

    const detail = element('div', 'check-detail', check.detail);
    detail.hidden = true;
    item.appendChild(detail);

    let hint = null;
    if (check.hint) {
      hint = element('div', 'check-hint', check.hint);
      hint.hidden = true;
      item.appendChild(hint);
    }

    const toggle = () => {
      const open = item.getAttribute('aria-expanded') === 'true';
      item.setAttribute('aria-expanded', String(!open));
      detail.hidden = open;
      if (hint) hint.hidden = open;
    };
    item.addEventListener('click', toggle);
    item.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        toggle();
      }
    });

    // Anything that failed starts open. Making someone click to discover what
    // is wrong is the wrong default when what is wrong is the whole point.
    if (!check.passed) toggle();

    list.appendChild(item);
  });
}

function renderPreview(table, flags) {
  const grid = $('preview');
  grid.replaceChildren();

  if (!table.rows.length) {
    grid.replaceChildren();
    $('preview-count').textContent = '';
    const holder = element('div', 'empty-state', 'No rows were found in this document.');
    grid.appendChild(holder);
    return;
  }

  // Flags are keyed for O(1) lookup; a 500-row table times a dozen columns is
  // 6000 cells, and scanning a flag list per cell is visibly slow.
  const flagged = new Map();
  flags.forEach((flag) => {
    const key = `${flag.row} ${flag.column}`;
    if (!flagged.has(key)) flagged.set(key, flag);
  });

  // Whole-number columns should not read as 5.00; money columns should.
  const decimals = table.columns.map((_, index) =>
    table.kinds[index] === 'number'
    && table.rows.some((row) => typeof row[index] === 'number' && !Number.isInteger(row[index]))
      ? 2 : 0);

  const head = element('thead');
  const headRow = element('tr');
  headRow.appendChild(element('th', 'rownum', '#'));
  table.columns.forEach((name, index) => {
    headRow.appendChild(element('th', table.kinds[index] === 'number' ? 'num' : null, name));
  });
  head.appendChild(headRow);
  grid.appendChild(head);

  const body = element('tbody');
  table.rows.forEach((row, rowIndex) => {
    const absoluteRow = table.offset + rowIndex;
    const tr = element('tr');
    tr.appendChild(element('td', 'rownum', absoluteRow + 1));

    row.forEach((value, columnIndex) => {
      const isNumber = table.kinds[columnIndex] === 'number';
      const cell = element('td', isNumber ? 'num' : null);

      if (value === null || value === '') {
        cell.textContent = '—';
        cell.classList.add('empty');
      } else if (isNumber && typeof value === 'number') {
        cell.textContent = value.toLocaleString(undefined, {
          minimumFractionDigits: decimals[columnIndex],
          maximumFractionDigits: decimals[columnIndex],
        });
      } else {
        cell.textContent = String(value);
      }

      const flag = flagged.get(`${absoluteRow} ${table.columns[columnIndex]}`);
      if (flag) {
        cell.classList.add(`flag-${flag.severity}`);
        cell.title = flag.value
          ? `${flag.reason}\n\nThe PDF showed: ${flag.value}`
          : flag.reason;
      }

      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
  grid.appendChild(body);

  $('preview-count').textContent = `${table.total.toLocaleString()} rows`;
  const foot = $('preview-foot');
  foot.hidden = !table.truncated;
  if (table.truncated) {
    foot.textContent =
      `Showing the first ${table.rows.length.toLocaleString()} of `
      + `${table.total.toLocaleString()} rows. The download contains all of them.`;
  }
}

function showFailure(message) {
  stopStream();
  stopTicker();
  $('failure-detail').textContent = message;
  show('view-failed');
}

/* ---------- History -------------------------------------------------------- */

async function refreshHistory() {
  let jobs = [];
  try {
    ({ jobs } = await api.jobs());
  } catch {
    return;
  }

  // Shown only on the idle view, and only when there is something to show.
  const finished = jobs.filter((job) => job.status === 'done' && job.result);
  $('history').hidden = finished.length === 0 || $('view-idle').hidden;
  if (!finished.length) return;

  const list = $('history-list');
  list.replaceChildren();

  finished.forEach((job) => {
    const item = element('button', 'history-item');
    item.type = 'button';
    item.appendChild(icon('file'));
    item.appendChild(element('span', 'history-name', job.filename));
    item.appendChild(element('span', 'history-meta',
      `${job.result.n_rows} rows · ${formatBytes(job.size_bytes)}`));

    const pill = element('span', `pill ${job.result.passed ? 'ok' : 'review'}`,
      job.result.passed ? 'Passed' : 'Review');
    item.appendChild(pill);

    item.addEventListener('click', async () => {
      state.job = job;
      await finish(job);
    });
    list.appendChild(item);
  });
}

document.addEventListener('DOMContentLoaded', boot);
