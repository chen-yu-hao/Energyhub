/* Energyhub's offline browser client. All displayed energies come from the API. */
'use strict';
(() => {
  const API = '/api/energyhub';
  const $ = id => document.getElementById(id);
  const form = $('taskForm');
  const terminal = new Set(['completed', 'failed', 'cancelled']);
  const state = { capabilities: null, config: null, jobs: [], total: 0, job: null, files: { tgz: null, ref: null }, reports: new Map(), reportErrors: new Map(), busy: false, connected: false, refreshing: false, initial: true, page: 'new', taskId: null, renderKey: '', historyLimit: 100, searchTimer: null };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const number = (value, digits = 10) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
  const integer = id => Number($(id).value);
  const selected = name => form.elements[name].value;
  const date = timestamp => timestamp ? new Date(timestamp * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—';
  const datetime = timestamp => timestamp ? new Date(timestamp * 1000).toLocaleString() : '—';
  const title = job => job.name || job.dataset || `Task ${job.task_id.slice(0, 8)}`;
  const statusLabel = status => ({ queued: 'Queued', running: 'Running', cancelling: 'Stopping', completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled' }[status] || 'Unknown');
  const badge = status => `<span class="status-badge"><span class="status-dot ${escape(status)}"></span>${escape(statusLabel(status))}</span>`;
  const duration = seconds => { if (!Number.isFinite(seconds) || seconds < 0) return '—'; if (seconds < 60) return `${Math.floor(seconds)}s`; if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`; return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`; };
  const basisLabel = (basis, family = 'cc', pair = '34') => {
    const value = String(basis ?? '').trim();
    const canonical = /^(aug-)?cc-pv(t|q|5)z$/i.exec(value);
    // Molecular reports contain the actual basis; its own prefix is authoritative.
    if (canonical) return `${canonical[1] ? 'aug-' : ''}cc-pV${canonical[2].toUpperCase()}Z`;
    const prefix = family === 'aug' ? 'aug-' : '';
    if (value.toUpperCase() === 'CBS') return `${prefix}cc-pV[${pair === '45' ? 'Q5' : 'TQ'}]Z → CBS`;
    const cardinal = { '3zeta': 'T', '4zeta': 'Q', '5zeta': '5' }[value.toLowerCase()];
    return cardinal ? `${prefix}cc-pV${cardinal}Z` : value || '—';
  };
  const modelLabel = job => `${job.method} / ${basisLabel(job.basis, job.basis_family, job.cbs_pair)}`;
  function showError(id, error) { $(id).textContent = error || ''; $(id).hidden = !error; }
  let toastTimer;
  function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500); }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.body instanceof FormData ? 120000 : 35000);
    try {
      const response = await fetch(`${API}${path}`, { ...options, signal: controller.signal, headers: { Accept: 'application/json', ...options.headers } });
      const contentType = response.headers.get('content-type') || '';
      const payload = contentType.includes('application/json') ? await response.json() : await response.text();
      if (!response.ok) throw new Error(typeof payload === 'object' ? payload.error || `Request failed (${response.status})` : `Server returned ${response.status}. Please check that Energyhub is running.`);
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The server took too long to respond. Check the task list before submitting again.');
      if (error instanceof TypeError) throw new Error('Cannot reach Energyhub. Check the server connection and try again.');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function chosenSettings() {
    const threadBudget = integer('threadBudget');
    const parallel = $('parallelEnabled').checked;
    const threads = parallel ? integer('taskThreads') : threadBudget;
    const slots = parallel ? Math.max(1, Math.floor(threadBudget / threads)) : 1;
    return { method: selected('method'), basis: selected('basis'), basis_family: selected('basis_family'), cbs_pair: selected('cbs_pair'), local_method: 'canonical', pool_size: slots, task_threads: threads, thread_budget: threadBudget, memory_pool_mb: integer('memoryPool') };
  }
  function validSettings(options) {
    for (const [key, value] of Object.entries(options)) if (typeof value === 'number' && (!Number.isSafeInteger(value) || value < 1)) return 'Resource values must be positive whole numbers.';
    if (options.task_threads > options.thread_budget) return 'Threads per structure cannot exceed the thread pool size.';
    if (options.memory_pool_mb < options.pool_size) return 'The memory pool must provide at least 1 MB per concurrent structure.';
    if (state.config) {
      if (options.pool_size > state.config.pool_size) return `This allocation needs ${options.pool_size} concurrent slots; the server allows ${state.config.pool_size}. Increase threads per structure, reduce pool threads, or adjust server settings.`;
      if (options.memory_pool_mb > state.config.memory_pool_mb) return `The task memory pool exceeds the server’s ${state.config.memory_pool_mb} MB limit.`;
      if (options.thread_budget > (state.config.thread_pool_size || state.config.cpu_count || Infinity)) return 'The task thread pool exceeds the server CPU thread budget.';
      if (options.task_threads > (state.config.cpu_count || Infinity)) return 'Threads per structure exceeds the number of logical CPUs on the server.';
    }
    return null;
  }
  function updateForm() {
    const options = chosenSettings();
    const isCBS = options.basis === 'CBS';
    $('fullLabel').textContent = modelLabel(options);
    $('cbsInfo').hidden = !isCBS;
    $('cbsPairChoices').hidden = !isCBS;
    $('cbsPairLabel').textContent = options.cbs_pair === '45' ? '4ζ → 5ζ' : '3ζ → 4ζ';
    $('cbsBasisDescription').textContent = `Each structure is calculated at ${options.cbs_pair === '45' ? '4ζ and 5ζ' : '3ζ and 4ζ'}.`;
    for (const card of document.querySelectorAll('.basis-card')) {
      const input = card.querySelector('input');
      if (input.value !== 'CBS') card.querySelector('.option-foot').textContent = basisLabel(input.value, options.basis_family);
    }
    $('parallelState').textContent = $('parallelEnabled').checked ? 'On' : 'Off';
    $('taskThreads').disabled = !$('parallelEnabled').checked;
    if (!$('parallelEnabled').checked && Number.isFinite(options.thread_budget)) $('taskThreads').value = options.thread_budget;
    document.querySelectorAll('[data-step^="taskThreads:"]').forEach(button => { button.disabled = !$('parallelEnabled').checked; });
    const safe = Number.isFinite(options.pool_size) && Number.isFinite(options.task_threads) && options.task_threads > 0;
    const slots = safe ? options.pool_size : 0;
    const perMemory = slots ? Math.floor(options.memory_pool_mb / slots) : 0;
    const idle = safe ? Math.max(0, options.thread_budget - slots * options.task_threads) : 0;
    $('allocationSummary').innerHTML = `<span><strong>${slots || '—'}</strong> at a time</span><span><strong>${safe ? options.task_threads : '—'}</strong> threads each</span><span><strong>${Number.isFinite(perMemory) ? perMemory.toLocaleString() : '—'}</strong> MB each</span>${idle ? `<span><strong>${idle}</strong> threads idle</span>` : ''}`;
    $('allocationLanes').innerHTML = Array.from({ length: Math.min(slots, 8) }, (_, i) => `<span>Structure ${i + 1} · ${options.task_threads}t</span>`).join('') + (slots > 8 ? `<span>+${slots - 8} more</span>` : '');
    $('footerSummary').textContent = `${isCBS ? '2 basis sets · ' : ''}${slots || '—'} structure${slots === 1 ? '' : 's'} at a time`;
    const capability = state.capabilities?.methods?.find(item => item.name === options.method);
    let notice = '';
    if (capability && !capability.available) notice = capability.reason || 'This method is unavailable in the configured PySCF environment.';
    else if (capability && !capability.open_shell) notice = `${options.method} supports closed-shell structures only in this PySCF environment. Open-shell input will be rejected explicitly.`;
    showError('methodNotice', notice);
    $('submitTask').disabled = state.busy || !state.connected || !capability?.available;
  }
  function setRadio(name, value) { for (const input of form.querySelectorAll(`input[name="${name}"]`)) if (input.value === String(value) && !input.disabled) input.checked = true; }
  function updateCapabilities() {
    for (const input of form.querySelectorAll('input[name="method"]')) {
      const capability = state.capabilities.methods.find(item => item.name === input.value);
      input.disabled = !capability?.available;
      input.closest('.option-card').title = capability?.reason || '';
      document.querySelector(`[data-method-status="${input.value}"]`).textContent = capability?.available ? (capability.open_shell ? 'Closed & open shell' : 'Closed shell only') : 'Unavailable';
    }
    if (form.querySelector('input[name="method"]:checked')?.disabled) {
      const available = form.querySelector('input[name="method"]:not(:disabled)');
      if (available) available.checked = true;
    }
    for (const [field, capabilityKey] of [['basis', 'basis'], ['basis_family', 'basis_families'], ['cbs_pair', 'cbs_pairs']]) {
      const entries = state.capabilities[capabilityKey];
      if (!Array.isArray(entries)) continue;
      const names = entries.map(item => typeof item === 'string' ? item : item.id || item.value || item.name);
      for (const input of form.querySelectorAll(`input[name="${field}"]`)) input.disabled = !names.includes(input.value);
      if (form.querySelector(`input[name="${field}"]:checked`)?.disabled) { const first = form.querySelector(`input[name="${field}"]:not(:disabled)`); if (first) first.checked = true; }
    }
    $('engineLabel').textContent = state.capabilities.pyscf_version ? `PySCF ${state.capabilities.pyscf_version} · connected` : 'PySCF unavailable';
    $('engineDot').className = `status-dot ${state.capabilities.pyscf_version ? 'completed' : 'failed'}`;
  }
  function updateConfig() {
    const config = state.config;
    $('threadBudget').max = config.thread_pool_size || config.cpu_count || Number.MAX_SAFE_INTEGER;
    $('taskThreads').max = config.cpu_count || config.thread_pool_size || Number.MAX_SAFE_INTEGER;
    $('memoryPool').max = config.memory_pool_mb;
    $('globalPoolSummary').textContent = `Server pool: ${config.thread_pool_size ?? config.cpu_count ?? '—'} CPU threads · ${config.pool_size} concurrent slots · ${config.memory_pool_mb.toLocaleString()} MB. In use: ${config.slots_used} slots, ${config.memory_used_mb.toLocaleString()} MB.`;
    if (state.initial) {
      $('threadBudget').value = Math.max(1, Math.min(config.pool_size, config.thread_pool_size || config.cpu_count || 1));
      $('memoryPool').value = Math.min(4096, config.memory_pool_mb);
    }
  }
  function savePreset() {
    try { localStorage.setItem('energyhub.preset.v1', JSON.stringify({ ...chosenSettings(), parallel: $('parallelEnabled').checked, per_structure_threads: integer('taskThreads') })); toast('Preset saved in this browser. It will load for your next visit.'); }
    catch (_) { toast('This browser does not allow saving local preferences.'); }
  }
  function restorePreset() {
    try {
      const preset = JSON.parse(localStorage.getItem('energyhub.preset.v1') || 'null');
      if (!preset || typeof preset !== 'object') return;
      for (const field of ['method', 'basis', 'basis_family', 'cbs_pair']) if (preset[field]) setRadio(field, preset[field]);
      for (const [id, key] of [['threadBudget', 'thread_budget'], ['taskThreads', 'per_structure_threads'], ['memoryPool', 'memory_pool_mb']]) if (Number.isSafeInteger(preset[key]) && preset[key] > 0) $(id).value = Math.min(preset[key], Number($(id).max) || Infinity);
      if (typeof preset.parallel === 'boolean') $('parallelEnabled').checked = preset.parallel;
    } catch (_) { /* A corrupt or unavailable preference store must not block work. */ }
  }
  function setFile(kind, file) {
    const valid = kind === 'tgz' ? /\.(tgz|tar\.gz)$/i.test(file.name) : /\.ref$/i.test(file.name);
    if (!valid) { showError('formError', `Choose a ${kind === 'tgz' ? '.tgz or .tar.gz archive' : '.ref reference template'}.`); return; }
    if (kind === 'tgz' && file.size === 0) { showError('formError', 'The geometry archive cannot be empty.'); return; }
    if (state.config?.max_upload_bytes && file.size > state.config.max_upload_bytes) { showError('formError', 'This file exceeds the server upload limit.'); return; }
    state.files[kind] = file;
    const label = $(kind === 'tgz' ? 'archiveFileLabel' : 'referenceFileLabel');
    label.textContent = `${file.name} · ${file.size < 1024 ? `${file.size} B` : file.size < 1048576 ? `${(file.size / 1024).toFixed(1)} KB` : `${(file.size / 1048576).toFixed(1)} MB`}`;
    document.querySelector(`[data-upload="${kind}"]`).classList.add('has-file');
    document.querySelector(`[data-remove="${kind}"]`).hidden = false;
    showError('formError', '');
  }
  function removeFile(kind) {
    state.files[kind] = null;
    $(kind === 'tgz' ? 'archiveFile' : 'referenceFile').value = '';
    $(kind === 'tgz' ? 'archiveFileLabel' : 'referenceFileLabel').textContent = kind === 'tgz' ? 'Drop .tgz / .tar.gz or browse' : 'Drop .ref or browse';
    document.querySelector(`[data-upload="${kind}"]`).classList.remove('has-file');
    document.querySelector(`[data-remove="${kind}"]`).hidden = true;
  }
  function renderHistory() {
    const query = $('taskSearch').value.trim().toLowerCase();
    const jobs = state.jobs.filter(job => `${title(job)} ${job.dataset || ''} ${job.task_id} ${job.method}`.toLowerCase().includes(query));
    $('taskCount').textContent = `${state.total} ${state.total === 1 ? 'task' : 'tasks'}`;
    $('taskList').innerHTML = jobs.length ? jobs.map(job => `<a class="task-link${state.taskId === job.task_id ? ' selected' : ''}" href="#task/${encodeURIComponent(job.task_id)}"${state.taskId === job.task_id ? ' aria-current="page"' : ''}><span class="task-link-top"><span class="task-name">${escape(title(job))}</span><span class="task-date">${escape(date(job.created_at))}</span></span><div class="task-method">${escape(modelLabel(job))}</div><div class="task-status"><span class="status-dot ${escape(job.state)}"></span>${escape(statusLabel(job.state))}</div></a>`).join('') : `<p class="empty-sidebar">${query ? 'No matching tasks.' : 'Your calculations will appear here.<br>Create your first task to get started.'}</p>`;
    if (state.total > state.jobs.length) $('taskList').insertAdjacentHTML('beforeend', '<button type="button" id="loadMoreTasks" class="button button-soft">Load more tasks</button>');
    $('newTaskLink').classList.toggle('selected', state.page === 'new');
  }
  function renderResults() {
    const jobs = state.jobs.filter(job => job.state === 'completed');
    $('resultsContent').innerHTML = jobs.length ? jobs.map(job => `<article class="result-card"><div><h3><a href="#task/${encodeURIComponent(job.task_id)}">${escape(title(job))}</a></h3><p class="mono">${escape(modelLabel(job))}</p><p>${escape(datetime(job.finished_at))}${job.report?.molecularEnergies ? ` · ${job.report.molecularEnergies.length} structures` : ''}</p></div><div class="button-row"><a class="button button-soft" href="#task/${encodeURIComponent(job.task_id)}">View</a><a class="button button-primary" href="${API}/jobs/${encodeURIComponent(job.task_id)}/result" download>Download .ref ↓</a></div></article>`).join('') : '<div class="empty-state"><span class="empty-icon" aria-hidden="true">↗</span><h3>No completed results yet</h3><p>Completed calculations appear here with their energies<br>and a ready-to-use DFThub reference file.</p><a href="#new" class="button button-soft">Create a task</a></div>';
    if (state.total > state.jobs.length) $('resultsContent').insertAdjacentHTML('beforeend', '<p class="help">Showing recently loaded tasks. Load more in the Tasks sidebar to see older results.</p>');
  }
  function molecularTable(molecules) {
    return `<div class="table-wrap"><table><thead><tr><th scope="col">Structure</th><th scope="col" class="numeric">Total energy (Eₕ)</th><th scope="col" class="numeric">Time</th><th scope="col">Reference</th></tr></thead><tbody>${molecules.map(molecule => `<tr><td>${escape(molecule.name)}</td><td class="numeric">${number(molecule.energy_hartree, 12)}</td><td class="numeric">${escape(duration(molecule.elapsed_s))}</td><td>${escape(molecule.details?.reference_type || '—')}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function provenance(molecule, index) {
    const details = molecule.details || {};
    const detailLines = component => `<p>Hartree–Fock: <span class="mono">${number(component.hf_hartree, 12)} Eₕ</span><br>Correlation: <span class="mono">${number(component.correlation_hartree, 12)} Eₕ</span>${typeof component.perturbative_correction_hartree === 'number' ? `<br>Perturbative correction: <span class="mono">${number(component.perturbative_correction_hartree, 12)} Eₕ</span>` : ''}${typeof component.quadruples_bracket_hartree === 'number' ? `<br>[Q] intermediate: <span class="mono">${number(component.quadruples_bracket_hartree, 12)} Eₕ</span><br>The total energy uses the (Q) correction; [Q] is not added again.` : ''}</p>`;
    return `<details id="provenance-${index}"><summary class="detail-summary">${escape(molecule.name)} · ${escape(details.reference_type || 'reference')} · PySCF ${escape(details.pyscf_version || 'unknown')}</summary><div class="provenance-box">${detailLines(details)}${details.components ? Object.entries(details.components).map(([basis, component]) => `<h4 style="margin-top:14px">${escape(basis)}</h4>${detailLines(component)}`).join('') : ''}<details id="raw-${index}"><summary class="detail-summary">Full calculation details</summary><pre>${escape(JSON.stringify(details, null, 2))}</pre></details></div></details>`;
  }
  function renderJob(force = false) {
    const job = state.job;
    if (!job || state.page !== 'task') return;
    const report = state.reports.get(job.task_id) || job.report;
    const key = JSON.stringify([job, report, state.reportErrors.get(job.task_id)]);
    if (!force && key === state.renderKey) { updateElapsed(); return; }
    state.renderKey = key;
    const opened = Array.from($('jobContent').querySelectorAll('details[open]')).map(detail => detail.id);
    const progress = job.progress || {};
    const molecules = report?.molecularEnergies || [];
    const completed = job.state === 'completed' ? molecules.length || progress.completed || 0 : progress.completed || 0;
    const total = progress.total || (job.state === 'completed' ? completed : null);
    const percent = job.state === 'completed' ? 100 : total ? Math.min(100, Math.floor(completed / total * 100)) : null;
    const explanation = job.state === 'queued' ? 'Waiting for available server resources.' : job.state === 'cancelling' ? 'Stopping the calculation and its worker processes…' : job.state === 'completed' ? 'All required structures converged. Your reference file is ready.' : job.state === 'cancelled' ? 'This task was stopped. No complete reference file was published.' : job.state === 'failed' ? 'The calculation could not complete. Details are shown below.' : total ? `${completed} of ${total} structures completed. Progress updates after each complete structure.` : 'Preparing input and starting the calculation…';
    const rows = report?.reference_rows || [];
    $('jobContent').innerHTML = `<div class="panel-heading"><div><h2 id="jobTitle">${escape(title(job))}</h2><p class="job-subtitle">${escape(modelLabel(job))}</p></div>${badge(job.state)}</div><div class="job-metadata"><span>Created ${escape(datetime(job.created_at))}</span><span>Elapsed <span id="jobElapsed">—</span></span><span title="${escape(job.task_id)}">ID ${escape(job.task_id.slice(0, 8))}</span></div><div class="job-progress"><div class="progress-caption"><strong>${escape(statusLabel(job.state))}</strong><span>${percent === null ? (terminal.has(job.state) ? '—' : job.state === 'queued' ? 'Waiting' : 'Preparing') : `${percent}%`}</span></div><div class="progress-track${percent === null ? ' indeterminate' : ''}" role="progressbar" aria-label="Completed structures" aria-valuemin="0" aria-valuemax="100"${percent === null ? '' : ` aria-valuenow="${percent}"`}><span style="width:${percent === null ? 25 : percent}%"></span></div><p class="progress-detail">${escape(explanation)}</p></div>${job.error ? `<div class="notice error" role="alert">${escape(job.error)}</div>` : ''}<div class="allocation"><div class="allocation-summary"><span><strong>${job.pool_size}</strong> at a time</span><span><strong>${job.task_threads}</strong> threads each</span><span><strong>${Number(job.memory_mb || Math.floor(job.memory_pool_mb / job.pool_size)).toLocaleString()}</strong> MB each</span></div></div><div class="job-metadata">${job.archive_filename ? `<span>Archive: ${escape(job.archive_filename)}</span>` : ''}${job.reference_filename ? `<span>Reference: ${escape(job.reference_filename)}</span>` : ''}</div><div class="job-actions">${job.state === 'completed' ? `<a class="button button-primary" id="downloadReference" href="${API}/jobs/${encodeURIComponent(job.task_id)}/result" download>Download .ref ↓</a><a class="button button-soft" id="downloadReport" href="${API}/jobs/${encodeURIComponent(job.task_id)}/report?download=1" download>JSON report ↓</a><button class="button button-soft" id="downloadCSV" type="button"${molecules.length ? '' : ' disabled'}>Energies CSV ↓</button>` : !terminal.has(job.state) ? `<button class="button button-danger" id="cancelTask" type="button"${job.state === 'cancelling' ? ' disabled' : ''}>${job.state === 'cancelling' ? 'Stopping…' : 'Stop task'}</button>` : ''}<button class="button button-soft" id="reuseTask" type="button">Use these settings</button><button class="button button-soft" id="showTaskLog" type="button">View log</button></div>${molecules.length ? `<h3 class="section-title">Molecular energies <span class="muted">· hartree</span></h3>${molecularTable(molecules)}<h3 class="section-title">Calculation details</h3>${molecules.map(provenance).join('')}` : progress.molecule ? `<h3 class="section-title">Latest completed structure</h3>${molecularTable([progress.molecule])}` : ''}${rows.length ? `<h3 class="section-title">Reference values</h3><div class="table-wrap"><table><thead><tr><th scope="col">Weighted reaction</th><th scope="col" class="numeric">Reference</th><th scope="col">Unit</th><th scope="col" class="numeric">Ratio</th></tr></thead><tbody>${rows.map(row => `<tr><td>${escape(row.pairs.map(pair => `${pair.coefficient} × ${pair.name}`).join(' + '))}</td><td class="numeric">${number(row.value, 8)}</td><td>${escape(row.unit || (row.ratio == null ? 'Hartree' : 'kcal/mol'))}</td><td class="numeric">${escape(row.ratio ?? '—')}</td></tr>`).join('')}</tbody></table></div>` : ''}${state.reportErrors.has(job.task_id) ? `<p class="notice error">${escape(state.reportErrors.get(job.task_id))} <button type="button" class="button button-soft" id="retryReport">Retry report</button></p>` : ''}<div id="taskLog" hidden></div>`;
    for (const id of opened) if ($(id)) $(id).open = true;
    updateElapsed();
  }
  function updateElapsed() { if ($('jobElapsed') && state.job) $('jobElapsed').textContent = state.job.started_at ? duration((state.job.finished_at || Date.now() / 1000) - state.job.started_at) : 'Not started'; }
  async function loadReport(id) {
    if (state.reports.has(id)) return;
    try { const report = await request(`/jobs/${encodeURIComponent(id)}/report`); state.reports.set(id, report); state.reportErrors.delete(id); }
    catch (error) { state.reportErrors.set(id, error.message); }
    if (state.taskId === id) renderJob(true);
  }
  async function loadJob() {
    const id = state.taskId;
    if (!id) return;
    try {
      const job = await request(`/jobs/${encodeURIComponent(id)}`);
      if (state.taskId !== id) return;
      state.job = job;
      renderJob();
      if (job.state === 'completed' && !state.reports.has(id) && !state.reportErrors.has(id)) await loadReport(id);
    } catch (error) {
      if (state.taskId === id) $('jobContent').innerHTML = `<div class="empty-state"><h2 id="jobTitle">Task unavailable</h2><p>${escape(error.message)}</p><button class="button button-soft" type="button" id="retryTask">Try again</button></div>`;
    }
  }
  function route() {
    const hash = location.hash.slice(1) || 'new';
    const taskMatch = /^task\/([a-zA-Z0-9_-]+)$/.exec(hash);
    state.page = taskMatch ? 'task' : ['new', 'results', 'guide'].includes(hash) ? hash : 'new';
    state.taskId = taskMatch ? taskMatch[1] : null;
    state.renderKey = '';
    for (const page of ['new', 'job', 'results', 'guide']) $(`${page}View`).hidden = page === 'job' ? state.page !== 'task' : state.page !== page;
    for (const link of document.querySelectorAll('[data-nav]')) { if (link.dataset.nav === (state.page === 'task' ? 'results' : state.page)) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current'); }
    renderHistory();
    if (state.page === 'results') renderResults();
    if (state.page === 'task') { $('jobContent').innerHTML = '<p class="muted">Loading task…</p>'; loadJob(); }
    document.title = `${state.page === 'guide' ? 'Guide' : state.page === 'results' ? 'Results' : state.page === 'task' ? 'Task' : 'New task'} · CC energy`;
  }
  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const query = $('taskSearch').value.trim();
      const historyRequests = Array.from({ length: Math.ceil(state.historyLimit / 200) }, (_, page) => request(`/jobs?limit=${Math.min(200, state.historyLimit - page * 200)}&offset=${page * 200}${query ? `&q=${encodeURIComponent(query)}` : ''}`));
      const [jobsResult, configResult] = await Promise.allSettled([Promise.all(historyRequests), request('/config')]);
      if (jobsResult.status === 'rejected') throw jobsResult.reason;
      if (configResult.status === 'rejected') throw configResult.reason;
      state.jobs = jobsResult.value.flatMap(page => page.jobs);
      state.total = jobsResult.value[0].total ?? state.jobs.length;
      state.config = configResult.value;
      state.connected = Boolean(state.capabilities);
      showError('connectionNotice', '');
      updateConfig();
      renderHistory();
      if (state.page === 'results') renderResults();
      if (state.page === 'task') await loadJob();
    } catch (error) { state.connected = false; showError('connectionNotice', `${error.message} Reconnecting automatically…`); }
    finally { state.refreshing = false; updateForm(); }
  }
  async function loadSample(button) {
    button.disabled = true;
    try {
      const responses = await Promise.all([fetch('/static/examples/H2.tgz'), fetch('/static/examples/H2.ref')]);
      if (responses.some(response => !response.ok)) throw new Error('Example files could not be loaded. Please try again.');
      const blobs = await Promise.all(responses.map(response => response.blob()));
      setFile('tgz', new File([blobs[0]], 'H2.tgz', { type: 'application/gzip' }));
      setFile('ref', new File([blobs[1]], 'H2.ref', { type: 'text/plain' }));
      if (!$('taskName').value.trim()) $('taskName').value = 'H₂ reference example';
      toast('Example files loaded. Review your settings, then select Run CC energy.');
    } catch (error) { showError('formError', error.message); }
    finally { button.disabled = false; }
  }
  async function submit(event) {
    event.preventDefault();
    if (state.busy) return;
    showError('formError', '');
    if (!state.files.tgz || !state.files.ref) { showError('formError', 'Choose both a geometry archive and a .ref template. The reference file may be empty.'); return; }
    const options = chosenSettings();
    const issue = validSettings(options);
    if (issue) { showError('formError', issue); return; }
    if (state.config?.max_upload_bytes && state.files.tgz.size + state.files.ref.size + 4096 > state.config.max_upload_bytes) { showError('formError', 'The combined files exceed the server upload limit.'); return; }
    const data = new FormData();
    data.append('tgz', state.files.tgz);
    data.append('ref', state.files.ref);
    data.append('name', $('taskName').value.trim());
    for (const [key, value] of Object.entries(options)) data.append(key, String(value));
    state.busy = true;
    $('submitTask').textContent = 'Submitting…';
    updateForm();
    try {
      const job = await request('/jobs', { method: 'POST', body: data });
      state.jobs.unshift(job);
      state.total += 1;
      location.hash = `task/${job.task_id}`;
      toast('Task submitted. You can follow its progress here.');
      $('taskName').value = '';
      removeFile('tgz'); removeFile('ref');
      await refresh();
    } catch (error) { showError('formError', error.message); }
    finally { state.busy = false; $('submitTask').innerHTML = 'Run CC energy <span aria-hidden="true">→</span>'; updateForm(); }
  }
  async function cancelTask(button) {
    const job = state.job;
    if (!job || terminal.has(job.state)) return;
    button.disabled = true; button.textContent = 'Stopping…';
    try { const result = await request(`/jobs/${encodeURIComponent(job.task_id)}/cancel`, { method: 'POST' }); if (state.taskId === job.task_id) { state.job = result; renderJob(true); } await refresh(); }
    catch (error) { toast(error.message); button.disabled = false; button.textContent = 'Stop task'; }
  }
  function reuseTask() {
    const job = state.job;
    if (!job) return;
    for (const field of ['method', 'basis', 'basis_family', 'cbs_pair']) if (job[field]) setRadio(field, job[field]);
    $('threadBudget').value = job.thread_budget || job.pool_size * job.task_threads;
    $('taskThreads').value = job.task_threads;
    $('memoryPool').value = job.memory_pool_mb;
    $('parallelEnabled').checked = job.pool_size > 1;
    $('taskName').value = `${title(job)} — repeat`.slice(0, 120);
    updateForm(); location.hash = 'new'; toast('Settings copied. Choose the archive and reference files to run again.');
  }
  function csvDownload() {
    const report = state.reports.get(state.taskId) || state.job?.report;
    if (!report?.molecularEnergies) return;
    const csvCell = value => { let str = String(value ?? ''); if (typeof value === 'string' && /^[=+\-@\t\r]/.test(str)) str = "'" + str; return `"${str.replace(/"/g, '""')}"`; };
    const rows = [['structure', 'method', 'basis', 'energy_hartree', 'hf_hartree', 'correlation_hartree', 'perturbative_correction_hartree', 'pyscf_version', 'reference_type', 'elapsed_s'], ...report.molecularEnergies.map(item => [item.name, item.method, basisLabel(item.basis, state.job?.basis_family, state.job?.cbs_pair), item.energy_hartree, item.details?.hf_hartree, item.details?.correlation_hartree, item.details?.perturbative_correction_hartree, item.details?.pyscf_version, item.details?.reference_type, item.elapsed_s])];
    const blob = new Blob(['\uFEFF' + rows.map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n'], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = `${state.job?.dataset || state.taskId}.energies.csv`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function showLog(button) {
    const id = state.taskId;
    button.disabled = true;
    try {
      const payload = await request(`/jobs/${encodeURIComponent(id)}/log?lines=200`);
      if (state.taskId !== id) return;
      $('taskLog').hidden = false;
      $('taskLog').innerHTML = `<h3 class="section-title">Worker log${payload.truncated ? ' · last 200 lines' : ''}</h3><pre class="reference-preview">${escape(payload.text || 'No log output yet.')}</pre>`;
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  }
  function openSettings() {
    if (!state.config) { toast('Connect to the server before changing resource pools.'); return; }
    $('globalSlots').value = state.config.pool_size;
    $('globalThreads').value = state.config.thread_pool_size || state.config.cpu_count || 1;
    $('globalThreads').max = state.config.cpu_count || Number.MAX_SAFE_INTEGER;
    $('globalMemory').value = state.config.memory_pool_mb;
    $('settingsUsage').textContent = `Currently reserved: ${state.config.thread_used || 0} threads · ${state.config.slots_used} slots · ${state.config.memory_used_mb} MB. ${state.config.cpu_count || 'Unknown'} logical CPUs detected.`;
    showError('settingsError', ''); $('settingsDialog').showModal();
  }
  async function saveSettings(event) {
    event.preventDefault();
    $('saveSettings').disabled = true;
    try {
      state.config = { ...state.config, ...await request('/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pool_size: integer('globalSlots'), memory_pool_mb: integer('globalMemory'), thread_pool_size: integer('globalThreads') }) }) };
      updateConfig(); updateForm(); $('settingsDialog').close(); toast('Server resource pools updated.');
    } catch (error) { showError('settingsError', error.message); }
    finally { $('saveSettings').disabled = false; }
  }
  let previousParallelThreads = 1;
  $('parallelEnabled').addEventListener('change', () => {
    if ($('parallelEnabled').checked) $('taskThreads').value = Math.min(previousParallelThreads, integer('threadBudget'));
    else previousParallelThreads = integer('taskThreads') || 1;
  });
  form.addEventListener('submit', submit);
  form.addEventListener('input', updateForm);
  form.addEventListener('change', updateForm);
  $('savePreset').addEventListener('click', savePreset);
  $('openSettings').addEventListener('click', openSettings);
  $('closeSettings').addEventListener('click', () => $('settingsDialog').close());
  $('cancelSettings').addEventListener('click', () => $('settingsDialog').close());
  $('settingsForm').addEventListener('submit', saveSettings);
  $('taskSearch').addEventListener('input', () => { renderHistory(); clearTimeout(state.searchTimer); state.searchTimer = setTimeout(refresh, 300); });
  window.addEventListener('hashchange', route);
  for (const [kind, id] of [['tgz', 'archiveFile'], ['ref', 'referenceFile']]) {
    $(id).addEventListener('change', () => { if ($(id).files[0]) setFile(kind, $(id).files[0]); });
    const drop = document.querySelector(`[data-upload="${kind}"]`);
    drop.addEventListener('dragover', event => { event.preventDefault(); drop.classList.add('dragover'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
    drop.addEventListener('drop', event => { event.preventDefault(); drop.classList.remove('dragover'); if (event.dataTransfer.files.length !== 1) { showError('formError', 'Drop one file into each upload area.'); return; } setFile(kind, event.dataTransfer.files[0]); });
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.step) { const [id, delta] = button.dataset.step.split(':'); const input = $(id); input.value = Math.min(Number(input.max) || Infinity, Math.max(Number(input.min) || 1, (Number(input.value) || 1) + Number(delta))); updateForm(); }
    if (button.dataset.remove) removeFile(button.dataset.remove);
    if (button.id === 'cancelTask') cancelTask(button);
    if (button.id === 'loadSample') loadSample(button);
    if (button.id === 'reuseTask') reuseTask();
    if (button.id === 'downloadCSV') csvDownload();
    if (button.id === 'showTaskLog') showLog(button);
    if (button.id === 'retryTask') loadJob();
    if (button.id === 'retryReport') loadReport(state.taskId);
    if (button.id === 'loadMoreTasks') { state.historyLimit += 100; refresh(); }
  });
  async function boot() {
    route(); updateForm();
    try {
      state.capabilities = await request('/methods');
      updateCapabilities(); await refresh(); restorePreset();
    } catch (error) { showError('connectionNotice', `${error.message} Reconnecting automatically…`); $('engineLabel').textContent = 'Engine unavailable'; $('engineDot').className = 'status-dot failed'; }
    finally { state.initial = false; updateForm(); }
    setInterval(async () => {
      if (document.hidden || state.busy) return;
      if (!state.capabilities) { try { state.capabilities = await request('/methods'); updateCapabilities(); } catch (_) { /* The reconnect banner is refreshed below. */ } }
      await refresh();
    }, 3500);
    setInterval(updateElapsed, 1000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  }
  boot();
})();
