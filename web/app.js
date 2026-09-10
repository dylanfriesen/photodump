const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  try {
    const r = await fetch(url, { ...opts, signal: opts?.signal || AbortSignal.timeout(120000) });
    const body = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, body };
  } catch {
    return { ok: false, status: 0, body: { detail: 'Connection lost. Please try again.' } };
  }
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const isVideo = (f) => /\.(webm|mp4)$/i.test(f || '');
const isReel = (f) => /\.mp4$/i.test(f || '');
const isMobile = () => window.matchMedia('(max-width: 900px)').matches;

let CONFIG = null;
let CURRENT = null;   // item open in the lightbox
let SELECTED = [];    // ordered shot keys ('image:5' / 'ref:3')
let REEL_MODE = false;
let CR_REFS = [];      // ordered reference ids for the Create task
const NODE_CAPS = { has_ipadapter: true };   // until /api/node says otherwise
let CREATE_BUSY = false;    // a generate request is in flight
let CREATE_BLOCKED = false; // the resolved workflow cannot run on this node
let IMAGES = [];      // normalised tiles currently in the grid
let REFS = [];        // uploaded references, for the 'my photos' source
let NODE = { online: false, current: null, queued: 0 };

/* ---------- toasts ----------
   Every queueing action used to be silent on success and silent on failure
   too - `await api(...)` with no check. So a 500 looked exactly like a job
   that queued fine, and a job that queued fine looked like nothing happened. */
function toast(text, kind = 'ok', ms = 4200) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="dot"></span><span>${esc(text)}</span>
                  <span class="spacer"></span><button aria-label="dismiss">&times;</button>`;
  const close = () => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 200);
  };
  el.querySelector('button').onclick = close;
  $('toasts').appendChild(el);
  setTimeout(close, ms);
}

/* Queue something and say so. Returns the parsed body, or null on failure -
   callers that need to keep going check for null. */
async function queue(url, payload, describe) {
  const { ok, body } = await api(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!ok) {
    toast(body.detail || body.error || 'Could not queue that.', 'bad', 7000);
    return null;
  }
  const ids = body.queued || [];
  const local = url === '/api/reels' || url.endsWith('/deliver');
  const where = NODE.online || local ? '' : ' — waiting for ComfyUI to reconnect';
  toast(`${describe(ids)}${where}`, NODE.online ? 'ok' : 'teal');
  refreshJobs();
  pollStatus.last = undefined;
  return body;
}
let PROGRESS = null;   // live progress for the job in flight, or null

/* ---------- segmented control ----------
   The design replaced several <select>s with segmented controls. app.js reads
   these by `.value`, so each one gets a value property and emits `change` —
   the rest of the code stays unaware it is not an input. */
function initSeg(el) {
  if (!el || el.dataset.segReady) return;
  el.dataset.segReady = '1';
  Object.defineProperty(el, 'value', {
    configurable: true,
    get() { return el.querySelector('span.on')?.dataset.value ?? ''; },
    set(v) {
      el.querySelectorAll('span').forEach((s) => s.classList.toggle('on', s.dataset.value === v));
    },
  });
  el.addEventListener('click', (e) => {
    const s = e.target.closest('span[data-value]');
    if (!s || s.classList.contains('on')) return;
    el.value = s.dataset.value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

/* ---------- form memory ----------
   Phones evict background tabs aggressively, and losing a half-written
   prompt to a tab reload is the kind of small loss that stops you using a
   tool from your phone at all. */
const REMEMBER = ['subject-a', 'subject-b', 'mode', 'extra', 'negative',
                  'ex-prompt', 're-bpm', 're-beats', 're-seconds',
                  'cr-prompt', 'cr-negative', 'cr-quality', 'cr-mode'];
const isCheck = (el) => el && el.type === 'checkbox';
const STORE_KEY = 'photodump.form.v1';

function saveForm() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(
      Object.fromEntries(REMEMBER.map((id) => {
        const el = $(id);
        return [id, isCheck(el) ? el.checked : el.value];
      }))));
  } catch { /* private mode, quota - not worth surfacing */ }
}

function restoreForm() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || '{}'); } catch { return; }
  if (!saved || typeof saved !== 'object') return;
  for (const id of REMEMBER) {
    const el = $(id), v = saved[id];
    if (!el) continue;
    if (isCheck(el)) { if (typeof v === 'boolean') el.checked = v; }
    else if (typeof v === 'string') el.value = v;
  }
}

/* ---------- boot ---------- */
async function boot() {
  const { ok, body } = await api('/api/config');
  if (!ok) { toast('Cannot reach Photodump. Retrying…', 'bad'); setTimeout(boot, 5000); return; }
  CONFIG = body;

  $('mode').innerHTML = Object.entries(body.modes)
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join('');

  const ratio = { portrait: '4:5', square: '1:1', story: '9:16', landscape: '3:2' };
  $('aspect').innerHTML = body.aspects
    .map((a, i) => `<span data-value="${esc(a)}" class="${i === 0 ? 'on' : ''}">${esc(ratio[a] || a)}</span>`).join('');

  $('cr-aspect').innerHTML = $('aspect').innerHTML;
  ['aspect', 'cr-aspect', 'ex-target', 'ex-anchor', 're-timing', 're-source'].forEach((id) => initSeg($(id)));

  const chips = body.starters.map((s, i) => `<button class="chip" data-starter="${i}">${esc(s.name)}</button>`).join('');
  $('starters').innerHTML = chips;
  $('empty-starters').innerHTML = chips;

  $('node-bars').innerHTML = [0, 1, 2, 3, 4].map((i) => `<i style="height:${6 + i * 2}px"></i>`).join('');

  restoreForm();
  // Selects and checkboxes fire change, not input - listening to only one
  // meant those fields were never persisted.
  REMEMBER.forEach((id) => {
    $(id).addEventListener('input', saveForm);
    $(id).addEventListener('change', saveForm);
  });

  switchScreen('studio');
  syncHint(); syncCounts(); syncFeather(); syncSelection();
  await Promise.all([refreshRefs(), refreshRecipes(), refreshGallery(), refreshJobs()]);
  pollStatus();
  probeNode();
  refreshPreflight();
}

function syncHint() {
  const m = CONFIG.modes[$('mode').value];
  $('mode-hint').textContent = m ? m.hint : '';
}

/* ---------- render node ---------- */
const DOT = {
  asleep: '<span class="breathe"></span>',
  ready: '<span class="steady"></span>',
  rendering: '<span class="lvl"></span><span class="lvl"></span><span class="lvl"></span>',
};

/* ---------- progress ----------
   The node reports a real step count over its websocket; the server turns
   that into a percent. `estimated` means nothing has reported yet and the
   number is derived from how long this kind of job usually takes - shown
   dimmer, and never presented as a measurement. */
function fmtDuration(s) {
  if (s == null) return '';
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

function renderProgress(p) {
  const row = $('node-prog');
  const rule = document.querySelector('.node-rule');
  const fill = $('node-fill');
  const changed = (p?.job_id ?? null) !== (PROGRESS?.job_id ?? null);
  PROGRESS = p;
  if (changed) refreshJobs();

  if (!p) {
    row.hidden = true;
    fill.style.width = '0';
    rule.classList.remove('measured', 'estimated');
    rule.removeAttribute('aria-valuenow');
    rule.removeAttribute('aria-valuetext');
    document.title = 'photodump';
    return;
  }

  // A job can be in flight with no percentage - a re-attached render, or the
  // first of its kind, where there is no honest number to show. The phase and
  // the elapsed clock still are, so the row stays and only the bar goes.
  row.hidden = false;
  const known = p.percent != null;
  const pct = known ? Math.round(p.percent) : 0;
  $('prog-pct').textContent = known ? `${pct}%` : '—';
  $('prog-pct').style.color = known && !p.estimated ? 'var(--lime)' : 'var(--mute-2)';
  $('prog-stage').textContent = [
    p.steps ? `step ${p.step}/${p.steps}` : '',
    p.stage,
    known && p.estimated ? '(estimate)' : '',
  ].filter(Boolean).join(' · ');
  $('prog-eta').textContent = p.eta != null
    ? `~${fmtDuration(p.eta)} left`
    : `${fmtDuration(p.elapsed)} elapsed`;

  fill.style.width = `${pct}%`;
  if (known) rule.setAttribute('aria-valuenow', String(pct));
  else rule.removeAttribute('aria-valuenow');
  fill.parentElement.setAttribute('aria-valuetext', `${$('prog-stage').textContent}; ${$('prog-eta').textContent}`);
  rule.classList.toggle('measured', known && !p.estimated);
  rule.classList.toggle('estimated', known && !!p.estimated);

  // The queue list only re-renders when the queue itself changes, so the bar
  // on the running row is nudged directly rather than rebuilding the list
  // 50 times a minute.
  const bar = document.querySelector(`.job .bar[data-bar="${p.job_id}"]`);
  if (bar) {
    bar.hidden = !known;
    bar.firstElementChild.style.width = `${pct}%`;
    bar.classList.toggle('estimated', !!p.estimated);
  }
  const stage = document.querySelector(`[data-job-progress="${p.job_id}"]`);
  if (stage) stage.textContent = ` · ${$('prog-stage').textContent} · ${known ? pct + '% · ' : ''}${$('prog-eta').textContent}`;
  // Percentage in the tab title, so a backgrounded tab still answers
  // "is it done yet" without switching to it.
  document.title = known ? `${pct}% · photodump` : 'photodump';
}

async function pollStatus() {
  const { ok, body } = await api('/api/status', { signal: AbortSignal.timeout(10000) });
  if (!ok) {
    $('node-text').textContent = 'Connection lost · reconnecting…';
    renderProgress(null);
    setTimeout(pollStatus, 5000);
    return;
  }
  NODE = body;
  const state = body.current != null ? 'rendering' : body.online ? 'ready' : 'asleep';
  document.body.dataset.node = state;
  $('node-dot').innerHTML = DOT[state];
  $('node-text').textContent = state === 'rendering'
    ? `rendering job #${body.current} · ${body.queued} queued`
    : state === 'ready'
      ? `node ready · ${body.queued} queued`
      : `ComfyUI unavailable · ${body.queued} queued · retrying`;
  $('node-text').title = body.connection_error || '';
  renderProgress(body.progress);

  const tint = state === 'rendering' ? 'var(--lime)' : state === 'ready' ? 'var(--teal)' : 'var(--violet)';
  [...$('node-bars').children].forEach((b, i) => {
    b.style.background = i < body.queued ? tint : 'rgba(255,255,255,.09)';
  });
  $('queue-badge').textContent = body.queued;
  $('queue-badge').style.color = body.queued > 0 ? tint : 'var(--mute-4)';

  const hint = state === 'asleep'
    ? (body.connection_error || 'Waiting for ComfyUI to reconnect. Queued jobs will start automatically.')
    : state === 'rendering'
      ? `Node is busy with job #${body.current} — yours starts next.`
      : 'Node is ready — this starts immediately.';
  document.querySelectorAll('[data-queue-hint]').forEach((e) => { e.textContent = hint; });
  document.querySelectorAll('[data-animate-hint]').forEach((e) => {
    e.textContent = state === 'asleep' ? 'waiting for ComfyUI · starts automatically when reachable' : 'starts on the desktop shortly';
  });

  const badge = $('queue-tab-badge');
  badge.hidden = !body.queued;
  badge.textContent = body.queued;
  const mbadge = $('mnav-queue');
  mbadge.hidden = !body.queued;
  mbadge.textContent = body.queued;

  /* Re-check capability when the desktop comes back, not on every tick. */
  if (pollStatus.wasOnline === false && body.online) { probeNode(); refreshPreflight(); }
  pollStatus.wasOnline = body.online;

  const busy = body.current != null;
  if (pollStatus.last !== undefined && pollStatus.last !== `${body.current}|${body.queued}`) {
    refreshGallery(); refreshJobs();
  }
  pollStatus.last = `${body.current}|${body.queued}`;
  // A percentage that updates every 3s looks stuck; 1.2s is smooth and is
  // still one cheap request against a loopback server.
  setTimeout(pollStatus, busy ? 1200 : 10000);
}

async function refreshPreflight() {
  const el = $('preflight');
  const { ok, body } = await api('/api/preflight');
  if (!ok || !body.online) { el.hidden = true; return; }
  el.hidden = false;
  const good = body.ok;
  el.classList.toggle('ok', good);
  el.classList.toggle('bad', !good);
  $('pf-summary').innerHTML = good
    ? `<b>${body.ready}/${body.total}</b> workflows ready on the node`
    : `<b>${body.total - body.ready}</b> of ${body.total} workflows blocked \u2014 click for detail`;
  $('pf-body').innerHTML = body.workflows.map((w) => {
    const why = [
      w.missing_nodes.length ? `missing nodes: ${w.missing_nodes.join(', ')}` : '',
      ...w.missing_models.map((m) => `${m.field} ${JSON.stringify(m.want)} not among the node's ${m.count} option(s)`),
    ].filter(Boolean);
    return `<div class="pf-row ${w.ok ? 'ok' : 'bad'}">
      <div class="top"><span class="tick">${w.ok ? 'ready' : 'blocked'}</span><span>${esc(w.label)}</span></div>
      ${why.map((t) => `<span class="why">${esc(t)}</span>`).join('')}
    </div>`;
  }).join('');
}

async function probeNode() {
  const { ok, body } = await api('/api/node');
  if (!ok) return;
  if (body.checkpoints?.length) {
    $('checkpoint').innerHTML = '<option value="">default</option>' +
      body.checkpoints.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  }
  NODE_CAPS.has_ipadapter = body.has_ipadapter !== false;
  if (!NODE_CAPS.has_ipadapter) {
    const note = ' — node pack not installed';
    const opt = $('workflow').querySelector('[value="ipadapter"]');
    if (opt) { opt.disabled = true; opt.textContent += note; }
    // Create can reach the same workflow, so it needs the same guard.
    const cr = $('cr-mode').querySelector('[value="ipadapter_multi"]');
    if (cr) { cr.disabled = true; cr.textContent += note; }
  }
  syncCreate();
}

/* ---------- form ---------- */
function formValues() {
  return {
    subject_a: $('subject-a').value,
    subject_b: $('subject-b').value,
    mode: $('mode').value,
    extra: $('extra').value,
    negative: $('negative').value,
    aspect: $('aspect').value,
    count: +$('count').value,
    steps: +$('steps').value,
    cfg: +$('cfg').value,
    denoise: +$('denoise').value,
    ip_weight: +$('ip-weight').value,
    workflow: $('workflow').value || null,
    checkpoint: $('checkpoint').value || null,
    ref_id: $('ref').value ? +$('ref').value : null,
  };
}

function syncCounts() {
  $('gen-qty').textContent = `×${$('count').value}`;
  $('ex-qty').textContent = `×${$('ex-count').value}`;
  // The advanced panel hides seven controls; show how many are off-default.
  const off = [
    $('ref').value !== '', $('workflow').value !== '', $('checkpoint').value !== '',
    +$('denoise').value !== 0.65, +$('ip-weight').value !== 0.7,
    +$('steps').value !== 30, +$('cfg').value !== 5,
  ].filter(Boolean).length;
  const badge = $('adv-count');
  badge.hidden = off === 0;
  badge.textContent = `${off} set`;
}

function syncFeather() {
  const v = +$('ex-feather').value, pct = (v / 120) * 100;
  $('feather-val').textContent = `${v}px`;
  const s = $('ex-feather').closest('.slider');
  s.querySelector('.fill').style.width = `${pct}%`;
  s.querySelector('.knob').style.left = `${pct}%`;
}

document.addEventListener('click', (e) => {
  const step = e.target.closest('[data-step]');
  if (!step) return;
  const input = step.parentElement.querySelector('input');
  const next = Math.max(+input.min || 1, Math.min(+input.max || 99, (+input.value || 0) + (+step.dataset.step)));
  input.value = next;
  // Fire input so every listener sees it - syncCounts alone missed Create's
  // badge, and any future stepper would have inherited the same bug.
  input.dispatchEvent(new Event('input', { bubbles: true }));
});
['count', 'ex-count', 'ref', 'workflow', 'checkpoint', 'denoise', 'ip-weight', 'steps', 'cfg']
  .forEach((id) => { $(id).addEventListener('input', syncCounts); $(id).addEventListener('change', syncCounts); });
$('ex-feather').oninput = syncFeather;
$('mode').onchange = syncHint;

$('btn-preview').onclick = async () => {
  const { body } = await api('/api/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(formValues()),
  });
  $('preview').hidden = false;
  $('preview-count').textContent = `${body.tokens} tokens`;
  $('preview-body').innerHTML = (body.groups || []).map((g, i) => `
    <div class="tg" data-g="${i}">
      <span class="name">${esc(g.name)}</span>
      <div class="tags">${g.tags.map((t) => `<span>${esc(t)}</span>`).join('')}</div>
    </div>`).join('');
  $('preview-copy').onclick = () => navigator.clipboard?.writeText(body.prompt);
};

$('btn-generate').onclick = async (e) => {
  const v = formValues();
  if (!v.subject_a.trim() || !v.subject_b.trim()) {
    toast('Both sources need a value.', 'bad');
    ($('subject-a').value.trim() ? $('subject-b') : $('subject-a')).focus();
    return;
  }
  const btn = e.currentTarget;
  btn.disabled = true;
  await queue('/api/generate', v, (ids) =>
    `Queued ${ids.length} render${ids.length === 1 ? '' : 's'} · #${ids[0]}${ids.length > 1 ? `–#${ids[ids.length - 1]}` : ''}`);
  btn.disabled = false;
};

function applyStarter(i) {
  const s = CONFIG.starters[i];
  $('subject-a').value = s.subject_a;
  $('subject-b').value = s.subject_b;
  $('mode').value = s.mode;
  $('extra').value = s.extra || '';
  syncHint();
  saveForm();
  switchTask('fuse');
}
$('starters').onclick = (e) => {
  const i = e.target.closest('[data-starter]')?.dataset.starter;
  if (i !== undefined) applyStarter(+i);
};
$('empty-starters').onclick = (e) => {
  const i = e.target.closest('[data-starter]')?.dataset.starter;
  if (i !== undefined) applyStarter(+i);
};

/* ---------- recipes ---------- */
$('btn-save-recipe').onclick = async () => {
  const v = formValues();
  const name = prompt('Recipe name:', `${v.subject_a} x ${v.subject_b}`);
  if (!name) return;
  await api('/api/recipes', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...v, name }),
  });
  refreshRecipes();
};

async function refreshRecipes() {
  const { ok, body } = await api('/api/recipes');
  if (!ok || !Array.isArray(body)) return;
  $('recipes').innerHTML = body.map((r) =>
    `<span class="chip recipe" data-recipe='${esc(JSON.stringify(r))}'>
       <span>${esc(r.name)}</span><span class="x" data-del="${r.id}">&times;</span>
     </span>`).join('') || '<span class="hint">None saved yet.</span>';
}

$('recipes').onclick = async (e) => {
  if (e.target.dataset.del) {
    await api(`/api/recipes/${e.target.dataset.del}`, { method: 'DELETE' });
    return refreshRecipes();
  }
  const chip = e.target.closest('[data-recipe]');
  if (!chip) return;
  const r = JSON.parse(chip.dataset.recipe);
  $('subject-a').value = r.subject_a;
  $('subject-b').value = r.subject_b;
  $('mode').value = r.mode;
  $('extra').value = r.extra || '';
  $('negative').value = r.negative || '';
  syncHint();
  saveForm();
};

/* ---------- references ---------- */
async function refreshRefs() {
  const { ok, body } = await api('/api/refs');
  if (!ok || !Array.isArray(body)) return;
  const imgs = body.filter((r) => r.kind !== 'audio');
  REFS = imgs;
  // Rebuilding these selects wipes whatever the other tasks had chosen, so
  // remember and restore. Picking a Create reference used to silently retarget
  // Extend at a different photograph.
  const keep = { ref: $('ref').value, 'ex-ref': $('ex-ref').value, 're-audio': $('re-audio').value };
  const opts = imgs.map((r) => `<option value="${r.id}">${esc(r.label)} (${esc(r.kind)})</option>`).join('');
  $('ref').innerHTML = '<option value="">none</option>' + opts;
  $('ex-ref').innerHTML = opts || '<option value="">upload one under References</option>';
  $('re-audio').innerHTML = '<option value="">no music</option>' +
    body.filter((r) => r.kind === 'audio').map((r) => `<option value="${r.id}">${esc(r.label)}</option>`).join('');
  for (const [id, was] of Object.entries(keep)) {
    if (was && $(id).querySelector(`option[value="${was}"]`)) $(id).value = was;
  }

  renderRefPicker(imgs);
  $('refs-meta').textContent = `${body.length} reference${body.length === 1 ? '' : 's'}`;
  $('ref-grid').innerHTML = body.map((r) => `
    <figure data-ref="${r.id}">
      <div class="art">
        ${r.kind === 'audio' ? '' : `<img src="/refs/${esc(r.filename)}" alt="${esc(r.label)}" loading="lazy">`}
        <span class="kind ${esc(r.kind)}">${esc(r.kind)}</span>
        <span class="x" data-del="${r.id}">&times;</span>
      </div>
      <span class="label">${esc(r.label)}</span>
    </figure>`).join('') || '<p class="hint">No references yet.</p>';
  syncCounts();
}

$('ref-file').onchange = (e) => {
  $('drop-text').textContent = e.target.files[0]?.name || 'drop an image, or browse';
};

$('ref-form').onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData();
  fd.append('file', $('ref-file').files[0]);
  fd.append('label', $('ref-label').value);
  fd.append('kind', $('ref-kind').value);
  const r = await fetch('/api/refs', { method: 'POST', body: fd });
  if (!r.ok) { toast(`Upload failed: ${await r.text()}`, 'bad', 7000); return; }
  toast(`Uploaded ${$('ref-label').value || $('ref-file').files[0].name}`);
  e.target.reset();
  $('drop-text').textContent = 'drop an image, or browse';
  refreshRefs();
};

$('ref-grid').onclick = async (e) => {
  if (!e.target.dataset.del) return;
  if (!confirm('Delete this reference?')) return;
  await api(`/api/refs/${e.target.dataset.del}`, { method: 'DELETE' });
  refreshRefs();
};

/* ---------- create: free-form prompt, any number of references ---------- */
function renderRefPicker(imgs) {
  CR_REFS = CR_REFS.filter((id) => imgs.some((r) => r.id === id));
  $('cr-refs').innerHTML = imgs.length
    ? imgs.map((r) => {
        const at = CR_REFS.indexOf(r.id);
        return `<figure class="${at >= 0 ? 'on' : ''}" data-pick="${r.id}">
          <img src="/refs/${esc(r.filename)}" alt="${esc(r.label)}" loading="lazy">
          ${at >= 0 ? `<span class="n">${at + 1}</span>` : ''}
          <span class="lbl">${esc(r.label)}</span>
        </figure>`;
      }).join('')
    : '<span class="empty">No references yet \u2014 upload some under References.</span>';
  syncCreate();
}

/* Which workflow this will actually resolve to. Mirrors comfy.build() - the
   control on screen has to match it, or the user is tuning a value the graph
   never reads. */
function resolvedCreateMode() {
  const n = CR_REFS.length;
  if (!n) return 'txt2img';
  const chosen = $('cr-mode').value;
  if (chosen) return chosen === 'ipadapter' && n > 1 ? 'ipadapter_multi' : chosen;
  return n > 1 ? 'ipadapter_multi' : 'img2img';
}

/* The button is disabled for two independent reasons - a capability problem, or
   a request already in flight. Deriving it from only one of them let a control
   change release the in-flight lock and allow a double submit. */
function updateCreateButton() {
  $('btn-create').disabled = CREATE_BUSY || CREATE_BLOCKED;
}

function syncCreate() {
  const n = CR_REFS.length;

  // Normalise the selection FIRST. Deriving controls before this used a stale
  // mode: adding a second reference while 'keep composition' was selected left
  // denoise on screen and the guard un-run, while the payload used IP-Adapter.
  const single = $('cr-mode').querySelector('[value="img2img"]');
  if (single) {
    single.disabled = n > 1;
    single.textContent = n > 1
      ? 'keep composition — one reference only'
      : 'keep composition (single ref)';
  }
  if (n > 1 && $('cr-mode').value === 'img2img') $('cr-mode').value = '';

  const mode = resolvedCreateMode();
  const styleMode = mode.startsWith('ipadapter');

  $('cr-ref-count').textContent = n ? `${n} selected` : '';
  $('cr-ip-row').hidden = n === 0;
  // img2img strength is denoise; IP-Adapter strength is ip_weight.
  $('cr-ipweight-wrap').hidden = !styleMode;
  $('cr-denoise-wrap').hidden = styleMode;
  $('cr-qty').textContent = `\u00d7${$('cr-count').value}`;

  const warn = [];
  if (styleMode && !NODE_CAPS.has_ipadapter) {
    warn.push('The node has no IP-Adapter pack installed — this would fail.');
  }
  const hint = $('cr-ref-hint');
  hint.textContent = warn.length ? warn.join(' ')
    : n > 1 ? `${n} references blended as one style reference.`
    : 'Click to add, in order. Several can be combined — they are blended as one style reference.';
  hint.style.color = warn.length ? 'var(--amber)' : '';
  CREATE_BLOCKED = warn.length > 0;
  updateCreateButton();
}

$('cr-refs').onclick = (e) => {
  const fig = e.target.closest('[data-pick]');
  if (!fig) return;
  const id = +fig.dataset.pick;
  const at = CR_REFS.indexOf(id);
  if (at >= 0) CR_REFS.splice(at, 1); else CR_REFS.push(id);
  paintPicks();            // update in place; do not rebuild the DOM
};

/* Toggle selection state on the existing tiles. Rebuilding innerHTML on every
   click detached the node mid-interaction and threw away focus and scroll. */
function paintPicks() {
  $('cr-refs').querySelectorAll('[data-pick]').forEach((fig) => {
    const at = CR_REFS.indexOf(+fig.dataset.pick);
    fig.classList.toggle('on', at >= 0);
    let n = fig.querySelector('.n');
    if (at >= 0) {
      if (!n) { n = document.createElement('span'); n.className = 'n'; fig.prepend(n); }
      n.textContent = at + 1;
    } else if (n) {
      n.remove();
    }
  });
  syncCreate();
}

function createValues() {
  const body = {
    prompt: $('cr-prompt').value.trim(),
    quality: $('cr-quality').checked,
    negative: $('cr-negative').value,
    aspect: $('cr-aspect').value,
    count: +$('cr-count').value,
    steps: +$('steps').value,
    cfg: +$('cfg').value,
    checkpoint: $('checkpoint').value || null,
  };
  if (CR_REFS.length) {
    const mode = resolvedCreateMode();
    body.ref_ids = CR_REFS;
    body.workflow = mode;               // send what we resolved, not what was typed
    if (mode.startsWith('ipadapter')) body.ip_weight = +$('cr-ipweight').value;
    else body.denoise = +$('cr-denoise').value;
  }
  return body;
}

$('btn-cr-preview').onclick = () => {
  const v = createValues();
  if (!v.prompt) { alert('Write a prompt first.'); return; }
  const pos = v.quality ? `${CONFIG.quality}, ${v.prompt}` : v.prompt;
  const el = $('cr-preview');
  el.hidden = false;
  el.textContent = `+ ${pos}\n\n- ${v.negative || '(defaults)'}` +
    (CR_REFS.length
      ? `\n\n${CR_REFS.length} reference(s) via ${v.workflow} at ` +
        (v.ip_weight !== undefined ? `weight ${v.ip_weight}` : `denoise ${v.denoise}`)
      : '');
};

$('btn-create').onclick = async () => {
  const v = createValues();
  if (!v.prompt) { alert('Write a prompt first.'); return; }
  // Re-check here as well as in syncCreate: the button is only a hint, and the
  // resolved workflow can change between renders of the panel.
  if (CREATE_BLOCKED) { alert($('cr-ref-hint').textContent); return; }
  if (CREATE_BUSY) return;
  CREATE_BUSY = true;
  updateCreateButton();
  try {
    const { ok, body } = await api('/api/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(v),
    });
    if (!ok) { alert(body.detail || 'could not queue'); return; }
    refreshJobs();
    pollStatus.last = undefined;
  } finally {
    CREATE_BUSY = false;
    updateCreateButton();
  }
};

['cr-count', 'cr-ipweight', 'cr-denoise'].forEach((id) => { $(id).oninput = syncCreate; });
$('cr-mode').onchange = syncCreate;

/* ---------- gallery ---------- */
function tileMarkup(i) {
  const vid = isVideo(i.filename);
  const at = SELECTED.indexOf(i.key);
  const cls = [at >= 0 ? 'picked' : '', vid ? 'unpickable' : ''].filter(Boolean).join(' ');
  const media = vid
    ? `<video src="/out/${esc(i.filename)}" muted loop playsinline preload="metadata"></video>`
    : `<img src="${esc(i.thumb)}" alt="" loading="lazy">`;
  return `
    <figure class="${cls}" data-img='${esc(JSON.stringify(i))}'>
      <div class="art">
        ${media}
        ${vid ? `<span class="badge-motion"><i></i>${isReel(i.filename) ? '.mp4' : '.webm'}</span>` : ''}
        ${i.favourite && at < 0 ? '<span class="star">&#9733;</span>' : ''}
        ${at >= 0 ? `<span class="pick">${at + 1}</span>` : ''}
      </div>
      ${i.isRef ? '<span class="upload-tag">upload</span>' : ''}
      <figcaption><span>${i.isRef ? esc(i.label) : '#' + i.id}</span><span class="dim">${i.isRef ? '' : (i.seed ? `seed ${i.seed}` : '')}</span></figcaption>
    </figure>`;
}

/* Masonry: #grid uses 1px rows, so each tile spans its own measured height. */
function sizeTile(fig) {
  const media = fig.querySelector('img, video');
  if (!media) return;
  const apply = () => {
    const w = media.naturalWidth || media.videoWidth;
    const h = media.naturalHeight || media.videoHeight;
    if (!w || !h) return;
    const colW = fig.clientWidth || 200;
    fig.style.gridRowEnd = `span ${Math.ceil(colW * (h / w)) + 32}`;
  };
  if (media.tagName === 'IMG') {
    media.complete ? apply() : media.addEventListener('load', apply, { once: true });
  } else {
    media.addEventListener('loadedmetadata', apply, { once: true });
  }
}

function asTile(i) {
  return { ...i, key: `image:${i.id}`, src: 'image', thumb: `/thumbs/${i.filename}.jpg` };
}
function refAsTile(r) {
  return { key: `ref:${r.id}`, src: 'ref', id: r.id, filename: r.filename,
           thumb: `/refs/${r.filename}`, label: r.label, isRef: true, favourite: 0 };
}

async function refreshGallery() {
  const fav = $('only-fav').checked ? '?favourites=true' : '';
  const { ok, body: raw } = await api('/api/images' + fav);
  if (!ok || !Array.isArray(raw)) return;
  // Uploaded photos only join the grid while picking shots for a reel.
  const src = REEL_MODE ? $('re-source').value : 'renders';
  const body = [
    ...(src === 'uploads' ? [] : raw.map(asTile)),
    ...(src === 'renders' ? [] : REFS.map(refAsTile)),
  ];
  IMAGES = body;
  const empty = body.length === 0;
  $('gallery-empty').hidden = !empty;
  $('grid').hidden = empty;

  // Rebuilding the grid restarts every lazy image load, which reads as a
  // flash across the whole pane. The poll calls this whenever a job changes
  // state, so skip the rebuild unless a tile, a star or a pick actually moved.
  const sig = JSON.stringify([body.map((i) => [i.key, i.favourite]), SELECTED]);
  if (sig !== refreshGallery.sig) {
    refreshGallery.sig = sig;
    $('grid').innerHTML = body.map(tileMarkup).join('');
    [...$('grid').children].forEach(sizeTile);
  }
  // Keep captions and recipes current without restarting image/video loads.
  [...$('grid').children].forEach((fig, index) => { fig.dataset.img = JSON.stringify(body[index]); });

  const favs = body.filter((i) => i.favourite).length;
  $('gallery-meta').textContent = `${body.length} item${body.length === 1 ? '' : 's'} · ${favs} favourite${favs === 1 ? '' : 's'}`;
  const badge = $('gallery-badge');
  badge.hidden = empty;
  badge.textContent = body.length;
  syncSelection();
}

$('only-fav').onchange = refreshGallery;

$('grid').onclick = (e) => {
  const fig = e.target.closest('[data-img]');
  if (!fig) return;
  const img = JSON.parse(fig.dataset.img);
  if (!REEL_MODE) return openLightbox(img);
  if (isVideo(img.filename)) return;   // clips and reels can't be shots
  const at = SELECTED.indexOf(img.key);
  if (at >= 0) SELECTED.splice(at, 1); else SELECTED.push(img.key);
  syncSelection();
  refreshGallery();
};

/* ---------- reel selection ---------- */
function shotSeconds() {
  return $('re-timing').value === 'bpm'
    ? (60 / (+$('re-bpm').value || 120)) * (+$('re-beats').value || 1)
    : (+$('re-seconds').value || 2);
}

function totalSeconds(shot, n) {
  // Crossfades overlap, so they shorten the reel - mirrors reels.total_seconds().
  return $('re-transition').value === 'crossfade' && n > 1
    ? shot * n - Math.min(0.5, shot / 3) * (n - 1)
    : shot * n;
}

function syncSelection() {
  const n = SELECTED.length;
  const shot = shotSeconds();
  const total = totalSeconds(shot, n);
  $('sel-count').textContent = n;
  $('re-length').innerHTML = n
    ? `<span>${n} shots &times; ${shot.toFixed(2)}s =</span> <b>${total.toFixed(1)}s</b>
       <span class="spacer"></span>
       <span class="ticks">${'<i></i>'.repeat(Math.min(n, 8))}</span>`
    : '<span>Pick stills in the gallery to see the length.</span>';

  $('picking-count').textContent = `${n} / ${IMAGES.filter((i) => !isVideo(i.filename)).length} picked`;
  $('strip').hidden = !REEL_MODE || n === 0;
  if (REEL_MODE && n) {
    const by = Object.fromEntries(IMAGES.map((i) => [i.key, i]));
    $('strip-shots').innerHTML = SELECTED.map((key, k) => {
      const im = by[key];
      return `${k ? '<span class="cut">&#9679;</span>' : ''}
        <div class="shot" data-shot="${esc(key)}">
          ${im ? `<img src="${esc(im.thumb)}" alt="">` : ''}
          <span class="no">${k + 1}</span>
          <span class="secs">${shot.toFixed(2)}s</span>
        </div>`;
    }).join('');
    $('strip-total').innerHTML =
      `<b>${total.toFixed(1)}s</b><span>${n} &times; ${shot.toFixed(2)}s${$('re-timing').value === 'bpm' ? ` · ${$('re-bpm').value}bpm` : ''}</span>`;
  }
}

$('strip-shots').onclick = (e) => {
  const id = e.target.closest('[data-shot]')?.dataset.shot;
  if (!id) return;
  const at = SELECTED.indexOf(id);
  if (at >= 0) SELECTED.splice(at, 1);
  syncSelection();
  refreshGallery();
};

$('re-timing').onchange = () => {
  const bpm = $('re-timing').value === 'bpm';
  $('re-bpm-row').hidden = !bpm;
  $('re-secs-row').hidden = bpm;
  syncSelection();
};
['re-bpm', 're-beats', 're-seconds'].forEach((id) => { $(id).oninput = syncSelection; });
$('re-source').onchange = () => { refreshGallery(); };
$('re-transition').onchange = syncSelection;

$('sel-clear').onclick = () => { SELECTED = []; syncSelection(); refreshGallery(); };

$('sel-favs').onclick = async () => {
  const { ok, body } = await api('/api/images?favourites=true');
  if (!ok) { toast(body.detail || 'Could not load favourites.', 'bad'); return; }
  SELECTED = body.filter((i) => !isVideo(i.filename)).map((i) => `image:${i.id}`).reverse();
  syncSelection();
  refreshGallery();
};

async function buildReel(btn) {
  if (SELECTED.length < 2) {
    toast('Pick at least two stills in the gallery first.', 'bad');
    if (isMobile()) switchTab('gallery');
    return;
  }
  btn.disabled = true;
  // Reels build here on kanto, so this one really does start now.
  const body = await queue('/api/reels', {
    shots: SELECTED.map((k) => { const [src, id] = k.split(':'); return { src, id: +id }; }),
    bpm: $('re-timing').value === 'bpm' ? +$('re-bpm').value : null,
    beats_per_shot: +$('re-beats').value,
    seconds: +$('re-seconds').value,
    motion: $('re-motion').value,
    transition: $('re-transition').value,
    audio_ref_id: $('re-audio').value ? +$('re-audio').value : null,
  }, () => `Building a ${SELECTED.length}-shot reel now`);
  btn.disabled = false;
  if (body) switchTab('queue');
}
$('btn-reel').onclick = (e) => buildReel(e.currentTarget);
document.querySelector('[data-build-reel]').onclick = (e) => buildReel(e.currentTarget);

/* ---------- extend ---------- */
$('btn-extend').onclick = async (e) => {
  const refId = $('ex-ref').value;
  if (!refId) {
    toast('Upload a source image under References first.', 'bad');
    switchTab('refs');
    return;
  }
  const scene = $('ex-prompt').value.trim();
  if (!scene) {
    toast('Describe the finished scene so the model knows what to paint.', 'bad');
    $('ex-prompt').focus();
    return;
  }
  const btn = e.currentTarget;
  btn.disabled = true;
  await queue('/api/generate', {
    prompt: scene, workflow: 'outpaint', ref_id: +refId,
    count: +$('ex-count').value,
    extend_target: $('ex-target').value,
    extend_anchor: $('ex-anchor').value,
    feathering: +$('ex-feather').value,
    steps: +$('steps').value, cfg: +$('cfg').value,
    checkpoint: $('checkpoint').value || null,
  }, (ids) => `Queued ${ids.length} extend${ids.length === 1 ? '' : 's'} · #${ids[0]}`);
  btn.disabled = false;
};

/* ---------- queue ---------- */
async function refreshJobs() {
  const { ok, body } = await api('/api/jobs');
  if (!ok || !Array.isArray(body)) return;
  const n = (s) => body.filter((j) => j.status === s).length;
  $('queue-meta').textContent =
    `${n('queued')} queued · ${n('running')} running · ${n('failed')} failed`;

  $('jobs').innerHTML = body.map((j) => {
    const params = (() => { try { return JSON.parse(j.params); } catch { return {}; } })();
    const kind = params.workflow === 'reel' ? 'reel'
      : params.workflow === 'deliver' ? 'Instagram encode'
      : params.workflow === 'outpaint' ? 'extend'
      : params.workflow === 'wan_i2v' ? 'animate' : 'fuse';
    const failed = j.status === 'failed';
    const running = j.status === 'running' && PROGRESS && PROGRESS.job_id === j.id;
    return `
    <div class="job ${j.status}">
      <div class="st">
        <span class="pill ${j.status}"><i></i><span>${j.status}</span></span>
        <span class="jid">#${j.id}</span>
      </div>
      <div class="mid">
        ${failed
          ? `<div class="err">${esc(j.error)}</div>`
          : `<span class="prompt">${esc(j.prompt)}</span>`}
        <span class="jmeta">${esc(kind)}${params.aspect ? ` · ${esc(params.aspect)}` : ''}${j.attempts > 1 ? ` · attempt ${j.attempts}` : ''}${
          running ? `<span data-job-progress="${j.id}"> · ${esc(PROGRESS.stage)}${PROGRESS.steps ? ` ${PROGRESS.step}/${PROGRESS.steps}` : ''}</span>` : ''}</span>
      </div>
      <div class="act">
        ${j.status === 'queued' ? `<button data-cancel="${j.id}">cancel</button>` : ''}
      </div>
      ${running ? `
      <div class="bar ${PROGRESS.estimated ? 'estimated' : ''}" data-bar="${j.id}" style="grid-column:2/-1" ${PROGRESS.percent == null ? 'hidden' : ''}>
        <b style="width:${Math.round(PROGRESS.percent || 0)}%"></b>
      </div>` : ''}
      ${(j.status === 'failed' || j.status === 'cancelled') ? `
      <div class="fixes" style="grid-column:2/-1">
        <button data-requeue="${j.id}" class="warn">requeue</button>
        ${j.error ? `<button data-copyerr="${j.id}">copy error</button>` : ''}
      </div>` : ''}
    </div>`;
  }).join('') || '<p class="hint">Queue is empty.</p>';
}

$('jobs').onclick = async (e) => {
  const d = e.target.dataset;
  if (d.cancel) {
    await api(`/api/jobs/${d.cancel}`, { method: 'DELETE' });
  } else if (d.requeue) {
    const { ok, body } = await api(`/api/jobs/${d.requeue}/requeue`, { method: 'POST' });
    if (!ok) { toast(body.detail || 'Could not requeue.', 'bad'); return; }
    toast(`Job #${d.requeue} back on the queue`);
  } else if (d.copyerr) {
    const j = (await api('/api/jobs')).body.find((x) => x.id === +d.copyerr);
    navigator.clipboard?.writeText(j?.error || '');
    e.target.textContent = 'copied';
    setTimeout(() => { e.target.textContent = 'copy error'; }, 1200);
    return;
  } else return;
  refreshJobs();
  pollStatus.last = undefined;
};

/* ---------- lightbox ---------- */
/* Stills and clips both page with the arrow keys. The gallery is the whole
   point of the app; opening one image and having to close it to see the next
   is the single most repeated action here. */
function pageable() {
  return IMAGES.filter((i) => !i.isRef);
}

function stepLightbox(delta) {
  const list = pageable();
  const at = list.findIndex((i) => i.key === CURRENT?.key);
  const next = list[at + delta];
  if (next) openLightbox(next);
}

function syncLbNav() {
  const list = pageable();
  const at = list.findIndex((i) => i.key === CURRENT?.key);
  $('lb-prev').disabled = at <= 0;
  $('lb-next').disabled = at < 0 || at >= list.length - 1;
  $('lb-prev').hidden = $('lb-next').hidden = list.length < 2;
}

function openLightbox(img) {
  CURRENT = img;
  $('lightbox').hidden = false;
  $('deliver-box').hidden = true;
  const vid = isVideo(img.filename);
  $('lb-img').hidden = vid;
  $('lb-vid').hidden = !vid;
  $('animate-box').hidden = true;
  $('lb-animate').hidden = vid;      // a clip is already the output
  $('lb-deliver').hidden = !vid;     // ...but a clip is what you deliver
  if (vid) { $('lb-vid').src = `/out/${img.filename}`; $('lb-img').removeAttribute('src'); }
  else { $('lb-img').src = `/out/${img.filename}`; $('lb-vid').removeAttribute('src'); }

  $('lb-id').textContent = `#${img.id}`;
  $('lb-meta').textContent = [img.seed ? `seed ${img.seed}` : '', img.filename].filter(Boolean).join(' · ');
  $('lb-prompt').textContent = img.prompt || '';
  $('lb-download').href = `/out/${img.filename}`;
  $('lb-download').download = img.filename;
  setFav(img.favourite);
  renderCaption(img.caption, img.hashtags);
  syncLbNav();

  // "Reuse these settings" only means something for a render made from a
  // recipe; extends and clips have no source A / source B to put back.
  const recipe = (() => {
    try { return JSON.parse(img.params || '{}').recipe || null; } catch { return null; }
  })();
  $('lb-reuse').hidden = !(recipe && recipe.subject_a);
  $('lb-reuse').onclick = () => {
    $('subject-a').value = recipe.subject_a || '';
    $('subject-b').value = recipe.subject_b || '';
    $('mode').value = recipe.mode || 'design_fusion';
    $('extra').value = recipe.extra || '';
    syncHint();
    saveForm();
    closeLb();
    switchTask('fuse');
    if (isMobile()) switchScreen('studio');
    toast(`Loaded the recipe from #${img.id}`);
  };
}

$('lb-prev').onclick = () => stepLightbox(-1);
$('lb-next').onclick = () => stepLightbox(1);

function setFav(on) {
  const b = $('lb-fav');
  b.classList.toggle('off', !on);
  b.lastElementChild.textContent = on ? 'Favourited' : 'Favourite';
}

function renderCaption(caption, tags) {
  const list = (tags || '').split(/\s+/).filter(Boolean);
  $('lb-caption').innerHTML = caption
    ? `<p class="text">${esc(caption)}</p>
       ${list.length ? `<div class="tags">${list.map((t) => `<span>${esc(t)}</span>`).join('')}</div>` : ''}
       <div class="foot"><span>${list.length} tags · ${caption.length} characters</span></div>`
    : '<div class="none">No caption drafted.</div>';
}

const closeLb = () => { $('lightbox').hidden = true; $('lb-vid').pause?.(); };
$('lb-close').onclick = closeLb;
$('lightbox').onclick = (e) => { if (e.target.id === 'lightbox') closeLb(); };
document.addEventListener('keydown', (e) => {
  if ($('lightbox').hidden) return;
  if (e.key === 'Escape') return closeLb();
  if (e.target.closest('input, textarea, select, video, [contenteditable="true"]')) return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); stepLightbox(-1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); stepLightbox(1); }
});

$('lb-fav').onclick = async () => {
  const { ok, body } = await api(`/api/images/${CURRENT.id}/favourite`, { method: 'POST' });
  if (!ok) { toast(body.detail || 'Could not save favourite.', 'bad'); return; }
  CURRENT.favourite = body.favourite;
  setFav(body.favourite);
  refreshGallery();
};

$('lb-caption-btn').onclick = async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true; btn.textContent = 'Drafting…';
  // No body: the server drafts from the recipe this image was rendered with.
  // Sending the studio form here captioned old images with whatever was
  // currently typed in the panel.
  const { ok, body } = await api(`/api/images/${CURRENT.id}/caption`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  });
  btn.disabled = false; btn.textContent = 'Draft caption';
  if (!ok) { toast(body.error || 'Caption drafting failed.', 'bad', 7000); return; }
  renderCaption(body.caption, body.hashtags.join(' '));
  refreshGallery();
};

$('lb-animate').onclick = () => { $('animate-box').hidden = !$('animate-box').hidden; };

$('lb-deliver').onclick = () => {
  $('deliver-box').hidden = !$('deliver-box').hidden;
  $('animate-box').hidden = true;
};

$('deliver-box').onclick = async (e) => {
  const target = e.target.closest('[data-target]')?.dataset.target;
  if (!target) return;
  const body = await queue(`/api/images/${CURRENT.id}/deliver`, { target },
    (ids) => `Encoding #${CURRENT.id} as ${target} · job #${ids[0]}`);
  if (body) closeLb();
};

$('an-go').onclick = async (e) => {
  const motion = $('an-prompt').value.trim();
  if (!motion) { toast('Describe the motion you want.', 'bad'); $('an-prompt').focus(); return; }
  const btn = e.currentTarget;
  btn.disabled = true;
  const body = await queue('/api/generate', {
    prompt: motion,
    negative_full: 'static, still image, frozen, jpeg artifacts, watermark, text',
    workflow: 'wan_i2v', src_image_id: CURRENT.id, count: 1,
    seconds: +$('an-seconds').value, video_size: $('an-size').value,
  }, (ids) => `Queued a clip from #${CURRENT.id} · job #${ids[0]}`);
  btn.disabled = false;
  if (body) closeLb();
};

$('lb-delete').onclick = async () => {
  if (!confirm('Delete this image?')) return;
  const list = pageable();
  const at = list.findIndex((i) => i.key === CURRENT.key);
  const gone = CURRENT.id;
  const { ok, body } = await api(`/api/images/${CURRENT.id}`, { method: 'DELETE' });
  if (!ok) { toast(body.detail || 'Could not delete this image.', 'bad'); return; }
  await refreshGallery();
  // Stay in the lightbox on whatever moved into this slot - deleting a run of
  // duds is one keystroke per image that way instead of four clicks.
  const after = pageable();
  const next = after[Math.min(at, after.length - 1)];
  if (next) openLightbox(next); else closeLb();
  toast(`Deleted #${gone}`);
};

/* ---------- tabs ---------- */
function switchTask(task) {
  document.querySelectorAll('.tabs.sub button').forEach((b) => b.classList.toggle('active', b.dataset.task === task));
  document.querySelectorAll('.task').forEach((d) => { d.hidden = d.dataset.task !== task; });
  REEL_MODE = task === 'reel';
  document.querySelector('.output').classList.toggle('picking', REEL_MODE);
  $('picking-bar').hidden = !REEL_MODE;
  // On desktop the gallery sits beside the panel, so jump straight to it.
  // On mobile that would hide the reel controls you just opened, so the
  // user taps Gallery in the bottom nav when ready to pick.
  if (REEL_MODE && !isMobile()) switchTab('gallery');
  syncSelection();
  refreshGallery();
}

function switchTab(tab) {
  document.querySelectorAll('.tabs:not(.sub) > button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  if (isMobile()) {
    document.body.dataset.screen = tab;
    document.querySelectorAll('.mnav button').forEach((b) =>
      b.classList.toggle('active', b.dataset.screen === tab));
  }
  document.querySelectorAll('.tab').forEach((d) => { d.hidden = d.dataset.tab !== tab; });
  document.querySelectorAll('[data-gallery-only]').forEach((e) => { e.hidden = tab !== 'gallery'; });
}

/* ---------- mobile screens ---------- */
function switchScreen(screen) {
  document.body.dataset.screen = screen;
  document.querySelectorAll('.mnav button').forEach((b) =>
    b.classList.toggle('active', b.dataset.screen === screen));
  if (screen !== 'studio') switchTab(screen);
}

document.querySelector('.mnav').onclick = (e) => {
  const s = e.target.closest('button')?.dataset.screen;
  if (s) switchScreen(s);
};

document.querySelector('.tabs.sub').onclick = (e) => {
  const t = e.target.closest('button')?.dataset.task;
  if (t) switchTask(t);
};
document.querySelector('.tabs:not(.sub)').onclick = (e) => {
  const t = e.target.closest('button')?.dataset.tab;
  if (t) switchTab(t);
};

boot();
