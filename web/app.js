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
let CR_REF_ROWS = [];  // the reference records behind the picker
const CR_CLEAN = {};   // ref id -> [[x, y, w, h], ...] fractions to fill before img2img
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
function toast(text, kind = 'ok', ms) {
  // Failures stay long enough to read the error; everything else is a nod.
  ms ??= kind === 'bad' ? 7000 : 4200;
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  // Job ids read as ids: mono, in the toast's own colour.
  const body = esc(text).replace(/#\d+/g, (m) => `<span class="jid">${m}</span>`);
  el.innerHTML = `<span class="dot"></span><span class="spacer">${body}</span>
                  <button aria-label="dismiss">&times;</button>`;
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
  const local = url === '/api/reels' || url === '/api/carousel' || url.endsWith('/deliver');
  const where = NODE.online || local ? '' : ' — waiting for ComfyUI to reconnect';
  toast(`${describe(ids)}${where}`, local || !NODE.online ? 'teal' : 'ok');
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
      el.querySelectorAll('span[data-value]').forEach((s) => {
        const on = s.dataset.value === v;
        s.classList.toggle('on', on);
        s.setAttribute('aria-checked', String(on));
        s.tabIndex = on ? 0 : -1;
      });
    },
  });
  el.addEventListener('click', (e) => {
    const s = e.target.closest('span[data-value]');
    if (!s || s.classList.contains('on')) return;
    el.value = s.dataset.value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  });
  el.setAttribute('role', 'radiogroup');
  el.setAttribute('aria-label', el.closest('.f')?.querySelector('.cap')?.textContent || el.id);
  el.querySelectorAll('span[data-value]').forEach((s) => s.setAttribute('role', 'radio'));
  el.value = el.value;
  el.addEventListener('keydown', (e) => {
    const options = [...el.querySelectorAll('span[data-value]')];
    const at = options.indexOf(e.target);
    if (at < 0) return;
    let next = at;
    if (['ArrowRight', 'ArrowDown'].includes(e.key)) next = (at + 1) % options.length;
    else if (['ArrowLeft', 'ArrowUp'].includes(e.key)) next = (at + options.length - 1) % options.length;
    else if (![' ', 'Enter'].includes(e.key)) return;
    e.preventDefault();
    options[next].click();
    options[next].focus();
  });
}

/* ---------- form memory ----------
   Phones evict background tabs aggressively, and losing a half-written
   prompt to a tab reload is the kind of small loss that stops you using a
   tool from your phone at all. */
const REMEMBER = ['subject-a', 'subject-b', 'mode', 'extra', 'negative',
                  'ex-prompt', 're-bpm', 're-beats', 're-seconds',
                  'cr-prompt', 'cr-negative', 'cr-quality', 'cr-mode',
                  'cr-p2-add', 'cr-p2-avoid', 'an-backend', 'an-size'];
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
  ['aspect', 'cr-aspect', 'ex-target', 'ex-anchor', 're-timing', 're-source', 're-carousel-target', 'an-backend', 'an-size']
    .forEach((id) => initSeg($(id)));
  $('cr-quality-sample').textContent = `${body.quality.split(',').slice(0, 3).join(',')}…`;

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
  placeSampler('create');
  syncHint(); syncCounts(); syncFeather(); syncSelection(); syncAnimate();
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
  paused: '<span class="pz"></span><span class="pz"></span>',
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

  document.querySelector('.node').classList.toggle('has-prog', !!p);
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
  $('prog-pct').style.color = !known ? 'var(--mute-4)' : p.estimated ? 'var(--mute)' : 'var(--lime)';
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

let statusTimer;
let statusPending = false;
async function pollStatus() {
  if (statusPending) return;
  clearTimeout(statusTimer);
  statusPending = true;
  const { ok, body } = await api('/api/status', { signal: AbortSignal.timeout(10000) });
  statusPending = false;
  if (!ok) {
    $('node-text').textContent = 'Connection lost · reconnecting…';
    renderProgress(null);
    statusTimer = setTimeout(pollStatus, 5000);
    return;
  }
  NODE = body;
  const state = body.current != null ? 'rendering' : body.online ? 'ready' : 'asleep';
  document.body.dataset.node = state;
  document.body.dataset.queue = body.queue_paused ? 'paused' : '';
  // A paused queue with nothing in flight shows the pause glyph; a render that
  // is still finishing keeps its level bars, because it really is rendering.
  $('node-dot').innerHTML = DOT[body.queue_paused && state !== 'rendering' ? 'paused' : state];
  // Scheduled and paused work is not waiting on the node, so it is named
  // separately - otherwise an idle node with five held jobs reads as stuck.
  const extra = [
    body.scheduled ? `${body.scheduled} scheduled` : '',
    body.paused_jobs ? `${body.paused_jobs} paused` : '',
  ].filter(Boolean).join(' · ');
  const tail = `${body.queued} queued${extra ? ` · ${extra}` : ''}`;
  $('node-text').textContent = body.queue_paused
    ? `queue paused · ${tail}`
    : state === 'rendering'
      ? `rendering job #${body.current} · ${tail}`
      : state === 'ready'
        ? `node ready · ${tail}`
        : `ComfyUI unavailable · ${tail} · retrying`;
  const qp = $('queue-pause');
  if (qp) qp.textContent = body.queue_paused ? 'resume queue' : 'pause queue';
  $('queue-state')?.classList.toggle('on', !!body.queue_paused);
  // The node card says *why* it is unavailable, in words. "Asleep" and "awake
  // but ComfyUI is not listening" look identical from kanto otherwise.
  const sub = body.queue_paused ? 'nothing new starts until you resume the queue'
    : state === 'asleep' ? (body.connection_error || '') : '';
  $('node-sub').textContent = sub;
  $('node-sub').hidden = !sub;
  renderProgress(body.progress);

  const tint = body.queue_paused ? 'var(--amber)'
    : state === 'rendering' ? 'var(--lime)' : state === 'ready' ? 'var(--teal)' : 'var(--violet)';
  [...$('node-bars').children].forEach((b, i) => {
    b.style.background = i < body.queued ? tint : 'rgba(255,255,255,.09)';
  });
  $('queue-badge').textContent = body.queued;
  $('queue-badge').style.color = body.queued > 0 ? tint : 'var(--mute-4)';

  const hint = body.queue_paused
    ? 'The queue is paused — this waits until you resume it.'
    : state === 'asleep'
      ? 'ComfyUI is unreachable — this queues and drains when it reconnects.'
      : state === 'rendering'
        ? `Node is busy with job #${body.current} — yours starts after it.`
        : 'Node is ready — this starts immediately.';
  document.querySelectorAll('[data-queue-hint]').forEach((e) => { e.textContent = hint; });
  document.querySelectorAll('[data-animate-hint]').forEach((e) => {
    e.textContent = hint;
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
  statusTimer = setTimeout(pollStatus, busy ? 1200 : 10000);
}

async function refreshPreflight() {
  const el = $('preflight');
  const { ok, body } = await api('/api/preflight');
  if (!ok || !body.online) { el.hidden = true; return; }
  el.hidden = false;
  const good = body.ok;
  el.classList.toggle('ok', good);
  el.classList.toggle('bad', !good);
  $('pf-summary').textContent = good
    ? `${body.ready}/${body.total} workflows ready on the node`
    : `${body.total - body.ready} of ${body.total} workflows blocked${el.open ? '' : ' \u2014 click for detail'}`;
  el.ontoggle = () => { if (!good) $('pf-summary').textContent = `${body.total - body.ready} of ${body.total} workflows blocked${el.open ? '' : ' \u2014 click for detail'}`; };
  // Blocked first: they are the reason anyone opens this.
  const rows = [...body.workflows].sort((a, b) => a.ok - b.ok);
  $('pf-body').innerHTML = rows.map((w) => {
    const why = [
      w.missing_nodes.length ? `missing nodes: ${w.missing_nodes.join(', ')}` : '',
      ...w.missing_models.map((m) => `${m.field} ${JSON.stringify(m.want)} not among the node's ${m.count} option(s)`),
    ].filter(Boolean);
    return `<div class="pf-row ${w.ok ? 'ok' : 'bad'}">
      <span class="tick">${w.ok ? '\u2713' : '\u2715'}</span>
      <span class="col"><span class="label">${esc(w.label)}</span>
      ${why.map((t) => `<span class="why">${esc(t)}</span>`).join('')}</span>
    </div>`;
  }).join('');
}

async function probeNode() {
  const { ok, body } = await api('/api/node');
  if (!ok) return;
  if (body.checkpoints?.length) {
    const selected = $('checkpoint').value;
    $('checkpoint').innerHTML = '<option value="">default</option>' +
      body.checkpoints.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
    if (body.checkpoints.includes(selected)) $('checkpoint').value = selected;
  }
  NODE_CAPS.has_ipadapter = body.has_ipadapter !== false;
  for (const opt of [$('workflow').querySelector('[value="ipadapter"]'),
                     $('cr-mode').querySelector('[value="ipadapter_multi"]')]) {
    if (!opt) continue;
    opt.dataset.label ||= opt.textContent;
    opt.disabled = !NODE_CAPS.has_ipadapter;
    opt.textContent = opt.dataset.label + (opt.disabled ? ' — node pack not installed' : '');
  }
  syncCounts();
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
  // The reference panel hides four controls; show how many are off-default.
  const off = [
    $('ref').value !== '', $('workflow').value !== '',
    +$('denoise').value !== 0.65, +$('ip-weight').value !== 0.7,
  ].filter(Boolean).length;
  const badge = $('adv-count');
  badge.hidden = off === 0;
  badge.textContent = `${off} set`;
  // Collapsed, the sampler still says what it is set to.
  const ckpt = $('checkpoint').value.replace(/\.safetensors$/, '') || 'default checkpoint';
  $('sampler-sum').textContent = `${$('steps').value} · ${$('cfg').value} · ${ckpt}`;
}

/* One sampler serves Create, Fuse and Extend. It is a single DOM node moved
   into the active task, so #steps/#cfg/#checkpoint stay single-instance. Reel
   builds on kanto and has no sampler, so it keeps the node where it was. */
function placeSampler(task) {
  const slot = document.querySelector(`.task[data-task="${task}"] .sampler-slot`);
  if (slot && !slot.contains($('sampler'))) slot.appendChild($('sampler'));
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
  CR_REF_ROWS = imgs;
  CR_REFS = CR_REFS.filter((id) => imgs.some((r) => r.id === id));
  $('cr-refs').innerHTML = imgs.length
    ? imgs.map((r) => {
        const at = CR_REFS.indexOf(r.id);
        return `<figure class="${at >= 0 ? 'on' : ''}" data-pick="${r.id}" role="button" tabindex="0" aria-label="${esc(r.label || 'Reference ' + r.id)}" aria-pressed="${at >= 0}">
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

const refRow = (id) => CR_REF_ROWS.find((r) => r.id === id);
const refSpan = (n) => (n === 1 ? 'ref #1' : `refs #1–${n}`);

function syncSlider(id) {
  const el = $(id);
  const pct = ((+el.value - +el.min) / (+el.max - +el.min)) * 100;
  const s = el.closest('.slider');
  s.querySelector('.fill').style.width = `${pct}%`;
  s.querySelector('.knob').style.left = `${pct}%`;
  $(`${id}-val`).textContent = (+el.value).toFixed(2);
}

/* The plan card: what the references will actually do, in words, next to the
   references themselves. Before this the mode had to be inferred from which
   inputs happened to be visible. */
function paintPlan(mode, n) {
  const plan = $('cr-plan');
  const twoPass = mode === 'img2img' && $('cr-stylematch').checked;
  const thumbs = (ids, many) => `<span class="thumbs${many ? ' many' : ''}">${ids.map((id, i) => {
    const r = refRow(id);
    return `<span>${r ? `<img src="/refs/${esc(r.filename)}" alt="">` : ''}${many ? '' : `<b>${i + 1}</b>`}</span>`;
  }).join('')}</span><span class="arrow">→</span>`;
  let cls = '', lead, name, desc;
  if (mode === 'txt2img') {
    lead = '<span class="ph"></span>';
    name = 'text to image';
    desc = 'No reference — the prompt alone decides the picture.';
  } else if (mode === 'img2img') {
    cls = 'lime';
    lead = thumbs(CR_REFS.slice(0, 1), false);
    name = twoPass ? 'keep composition · style match' : 'keep composition';
    desc = `Copies the layout of ref #1 and repaints it at denoise ${(+$('cr-denoise').value).toFixed(2)}` +
      (twoPass ? ', then fixes colour in a 2nd pass.' : '.');
  } else {
    cls = 'teal';
    lead = thumbs(CR_REFS.slice(0, 4), true);
    name = 'style only';
    desc = `Borrows the look of ${refSpan(n)}${n > 1 ? ', blended' : ''}. The composition comes from your prompt.`;
  }
  if ($('cr-hires').checked && mode !== 'ipadapter' && !mode.startsWith('ipadapter')) desc += ' Upscaled 1.5× at the end.';
  plan.className = `plan ${cls}`;
  plan.innerHTML = `${lead}<span class="t"><span class="mode">${name}</span><span class="desc">${esc(desc)}</span></span>`;
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
      : 'keep composition (img2img)';
  }
  if (n > 1 && $('cr-mode').value === 'img2img') $('cr-mode').value = '';

  const mode = resolvedCreateMode();
  const styleMode = mode.startsWith('ipadapter');
  const twoPass = mode === 'img2img' && $('cr-stylematch').checked;

  $('cr-refs').classList.toggle('has-on', n > 0);
  $('cr-ref-count').textContent = `${n} selected`;
  $('cr-ref-count').style.color = n ? 'var(--lime)' : '';
  $('cr-ip-row').hidden = n === 0;
  // The checkbox is a shortcut for the dropdown's two real choices.
  $('cr-keep').checked = mode === 'img2img';
  $('cr-keep').disabled = n > 1;
  $('cr-keep-label').textContent = n > 1 ? 'keep composition — one reference only' : 'Keep composition';
  // img2img strength is denoise; IP-Adapter strength is ip_weight.
  $('cr-ipweight-wrap').hidden = !styleMode;
  $('cr-denoise-wrap').hidden = styleMode;
  $('cr-stylematch-wrap').hidden = styleMode || n === 0;
  $('cr-p2-row').hidden = styleMode || n === 0 || !$('cr-stylematch').checked;
  // IP-Adapter graphs have no upscale tail; txt2img and img2img both do.
  $('cr-hires-wrap').hidden = styleMode;
  syncSlider('cr-denoise');
  syncSlider('cr-ipweight');
  syncClean(mode === 'img2img' && n === 1 ? CR_REFS[0] : null);
  $('cr-qty').textContent = `×${$('cr-count').value}${twoPass ? ' · two passes each' : ''}`;
  paintPlan(mode, n);

  const warn = [];
  if (styleMode && !NODE_CAPS.has_ipadapter) {
    warn.push('The node has no IP-Adapter pack installed — this would fail. Install it on the render node, or drop back to a single reference in keep composition mode.');
  }
  $('cr-ref-hint').textContent = n > 1
    ? `${n} references blended as one style reference.`
    : 'Click to add, in order. Several can be combined — they are blended as one style reference.';
  $('cr-warn').hidden = !warn.length;
  $('cr-warn-text').textContent = warn.join(' ');
  CREATE_BLOCKED = warn.length > 0;
  $('cr-block').hidden = !CREATE_BLOCKED;
  const qh = document.querySelector('.task[data-task="create"] [data-queue-hint]');
  if (qh) qh.hidden = CREATE_BLOCKED;
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
$('cr-refs').onkeydown = (e) => {
  if (![' ', 'Enter'].includes(e.key) || !e.target.matches('[data-pick]')) return;
  e.preventDefault();
  e.target.click();
};

/* Toggle selection state on the existing tiles. Rebuilding innerHTML on every
   click detached the node mid-interaction and threw away focus and scroll. */
function paintPicks() {
  $('cr-refs').querySelectorAll('[data-pick]').forEach((fig) => {
    const at = CR_REFS.indexOf(+fig.dataset.pick);
    fig.classList.toggle('on', at >= 0);
    fig.setAttribute('aria-pressed', String(at >= 0));
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
    else {
      body.denoise = +$('cr-denoise').value;
      // Pass 1 must stay low or it re-renders in the checkpoint's own style
      // instead of the reference's; pass 2 is where colour gets corrected.
      body.second_pass = $('cr-stylematch').checked;
      if (body.second_pass) {
        body.second_pass_prompt_add = $('cr-p2-add').value.trim();
        body.second_pass_negative_add = $('cr-p2-avoid').value.trim();
      }
      const boxes = CR_CLEAN[CR_REFS[0]] || [];
      if (mode === 'img2img' && boxes.length) body.clean_regions = boxes;
    }
  }
  if (!(body.workflow || '').startsWith('ipadapter')) body.hires = $('cr-hires').checked;
  return body;
}

const needPrompt = () => {
  toast('Write a prompt first.', 'bad');
  if (isMobile()) switchScreen('studio');
  $('cr-prompt').focus();
};

$('btn-cr-preview').onclick = () => {
  const v = createValues();
  if (!v.prompt) return needPrompt();
  const pos = v.quality ? `${CONFIG.quality}, ${v.prompt}` : v.prompt;
  const mode = resolvedCreateMode();
  const plan = {
    txt2img: 'text to image', img2img: 'keep composition', ipadapter_multi: 'style only', ipadapter: 'style only',
  }[mode] || mode;
  $('cr-preview-box').hidden = false;
  $('cr-preview-count').textContent = `${pos.split(',').filter((t) => t.trim()).length} tags`;
  $('cr-preview-copy').onclick = () => navigator.clipboard?.writeText(pos);
  const el = $('cr-preview');
  el.textContent = `+ ${pos}\n\n\u2212 ${v.negative || '(defaults)'}` +
    (CR_REFS.length
      ? `\n\n${CR_REFS.length} reference(s) via ${v.workflow} at ` +
        (v.ip_weight !== undefined ? `weight ${v.ip_weight}` : `denoise ${v.denoise}`) +
        (v.second_pass ? ' + a 2nd colour-correcting pass' : '') +
        (v.second_pass_prompt_add ? `\n  pass 2 adds: ${v.second_pass_prompt_add}` : '') +
        (v.second_pass_negative_add ? `\n  pass 2 avoids: ${v.second_pass_negative_add}` : '') +
        (v.clean_regions ? `\n  ${v.clean_regions.length} region(s) filled out of the reference first` : '')
      : '') +
    (v.hires ? `\n\nupscaled 1.5\u00d7${v.second_pass ? ' on the final pass' : ''} in a refine pass` : '') +
    `\n\nplan \u00b7 ${plan} \u00b7 ${$('cr-aspect').querySelector('span.on')?.textContent || v.aspect} \u00b7 \u00d7${v.count}` +
    (v.second_pass ? ' \u00b7 two passes each' : '');
};

$('btn-create').onclick = async () => {
  const v = createValues();
  if (!v.prompt) return needPrompt();
  // Re-check here as well as in syncCreate: the button is only a hint, and the
  // resolved workflow can change between renders of the panel.
  if (CREATE_BLOCKED) { toast($('cr-warn-text').textContent, 'bad'); return; }
  if (CREATE_BUSY) return;
  CREATE_BUSY = true;
  updateCreateButton();
  try {
    // queue() confirms with a toast, or shows the server's error in one.
    await queue('/api/generate', v, (ids) =>
      `Queued ${ids.length} render${ids.length === 1 ? '' : 's'} · #${ids[0]}` +
      (v.second_pass ? ' · pass 2 queues as each finishes' : ''));
  } finally {
    CREATE_BUSY = false;
    updateCreateButton();
  }
};

['cr-count', 'cr-ipweight', 'cr-denoise'].forEach((id) => { $(id).oninput = syncCreate; });
$('cr-mode').onchange = syncCreate;
$('cr-stylematch').onchange = syncCreate;
$('cr-hires').onchange = syncCreate;
$('cr-keep').onchange = () => {
  $('cr-mode').value = $('cr-keep').checked ? 'img2img' : 'ipadapter_multi';
  $('cr-mode').dispatchEvent(new Event('change', { bubbles: true }));
};

/* ---------- create: clean lettering out of an img2img reference ----------
   img2img at 0.45 copies everything in the source, so a reference screenshot's
   name text, watermark and carousel buttons come out as garbled fake text.
   Boxes are stored as fractions of the image and filled on kanto by the same
   code the worker runs (imageops.fill_regions), so the preview is exact. */
let CLEAN_REF = null;     // ref id the pad is showing
let CLEAN_PREVIEW = null; // object URL while previewing the fill
let CLEAN_REV = 0;        // bumped on any box or reference change
let CLEAN_DRAG = null;    // the one pointer allowed to draw

function exitCleanPreview() {
  if (!CLEAN_PREVIEW) return;
  URL.revokeObjectURL(CLEAN_PREVIEW);
  CLEAN_PREVIEW = null;
  $('cr-clean').classList.remove('previewing');
  $('cr-clean-wrap').classList.remove('previewing');
  $('cr-clean-title').textContent = 'clean up the reference';
  $('cr-clean-note').hidden = true;
  $('btn-clean-clear').hidden = false;
  $('btn-clean-preview').textContent = 'Preview fill';
  const ref = CR_REF_ROWS.find((r) => r.id === CLEAN_REF);
  if (ref) $('cr-clean-img').src = `/refs/${ref.filename}`;
}

function syncClean(refId) {
  $('cr-clean-wrap').hidden = refId === null;
  if (refId === null) { exitCleanPreview(); CLEAN_REV++; CLEAN_REF = null; return; }
  if (refId !== CLEAN_REF) {
    exitCleanPreview();
    CLEAN_REV++;
    CLEAN_REF = refId;
    const ref = CR_REF_ROWS.find((r) => r.id === refId);
    if (ref) $('cr-clean-img').src = `/refs/${ref.filename}`;
  }
  paintCleanBoxes();
}

function paintCleanBoxes() {
  const boxes = CR_CLEAN[CLEAN_REF] || [];
  $('cr-clean-boxes').innerHTML = boxes.map(([x, y, w, h], i) =>
    `<i data-box="${i}" title="remove" style="left:${x * 100}%;top:${y * 100}%;width:${w * 100}%;height:${h * 100}%"></i>`).join('');
  $('cr-clean-count').textContent = boxes.length ? `${boxes.length} box${boxes.length > 1 ? 'es' : ''}` : '';
  $('btn-clean-preview').disabled = !boxes.length && !CLEAN_PREVIEW;
  $('btn-clean-clear').disabled = !boxes.length;
}

function padPoint(e) {
  const r = $('cr-clean').getBoundingClientRect();
  return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
          Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
}

$('cr-clean').addEventListener('pointerdown', (e) => {
  // One drawing pointer at a time: a second finger's events used to finish
  // the first finger's box too, producing boxes large enough to erase the subject.
  if (CLEAN_PREVIEW || CLEAN_REF === null || CLEAN_DRAG !== null || e.button > 0) return;
  const hit = e.target.closest('[data-box]');
  if (hit) {
    CR_CLEAN[CLEAN_REF].splice(+hit.dataset.box, 1);
    CLEAN_REV++;
    paintCleanBoxes();
    return;
  }
  e.preventDefault();
  const pad = $('cr-clean');
  const [x0, y0] = padPoint(e);
  const refId = CLEAN_REF;
  CLEAN_DRAG = e.pointerId;
  // Starting a box already changes what a pending preview would show; a
  // response landing mid-drag must not be displayed once the box commits.
  CLEAN_REV++;
  const ghost = document.createElement('i');
  ghost.className = 'drawing';
  $('cr-clean-boxes').append(ghost);
  pad.setPointerCapture(e.pointerId);

  const rect = (ev) => {
    const [x1, y1] = padPoint(ev);
    return [Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0)];
  };
  const move = (ev) => {
    if (ev.pointerId !== CLEAN_DRAG) return;
    const [x, y, w, h] = rect(ev);
    Object.assign(ghost.style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` });
  };
  const up = (ev) => {
    if (ev.pointerId !== CLEAN_DRAG) return;
    CLEAN_DRAG = null;
    pad.removeEventListener('pointermove', move);
    pad.removeEventListener('pointerup', up);
    pad.removeEventListener('pointercancel', up);
    pad.removeEventListener('lostpointercapture', up);
    ghost.remove();
    const box = rect(ev);
    // A click or a tiny slip is not a box - it would fill a pixel-wide sliver.
    // Nor does a drag count if the reference changed under it.
    if (ev.type === 'pointerup' && refId === CLEAN_REF && box[2] > 0.01 && box[3] > 0.01) {
      (CR_CLEAN[refId] ||= []).push(box.map((v) => Math.round(v * 10000) / 10000));
      CLEAN_REV++;
    }
    if (refId === CLEAN_REF) paintCleanBoxes();
  };
  pad.addEventListener('pointermove', move);
  pad.addEventListener('pointerup', up);
  pad.addEventListener('pointercancel', up);
  pad.addEventListener('lostpointercapture', up);
});

$('btn-clean-preview').onclick = async (e) => {
  if (CLEAN_PREVIEW) { exitCleanPreview(); paintCleanBoxes(); return; }
  if (CLEAN_DRAG !== null) return;
  const boxes = CR_CLEAN[CLEAN_REF] || [];
  if (!boxes.length) return;
  const btn = e.currentTarget;
  btn.disabled = true;
  // Generation uses whatever is selected when Generate is pressed, so a
  // preview that lands after the boxes or reference changed would show
  // something that will not be rendered. Such responses are dropped.
  const rev = CLEAN_REV, refId = CLEAN_REF;
  try {
    const r = await fetch(`/api/refs/${refId}/clean-preview`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ regions: boxes }),
    });
    if (rev !== CLEAN_REV || refId !== CLEAN_REF) return;
    if (!r.ok) { toast(`Preview failed: ${await r.text()}`, 'bad'); return; }
    const blob = await r.blob();
    if (rev !== CLEAN_REV || refId !== CLEAN_REF) return;
    CLEAN_PREVIEW = URL.createObjectURL(blob);
    $('cr-clean-img').src = CLEAN_PREVIEW;
    $('cr-clean').classList.add('previewing');
    $('cr-clean-wrap').classList.add('previewing');
    $('cr-clean-title').textContent = 'filled result';
    $('cr-clean-note').hidden = false;
    $('btn-clean-clear').hidden = true;
    btn.textContent = 'Back to boxes';
  } catch {
    toast('Could not load the fill preview. Please try again.', 'bad');
  } finally {
    btn.disabled = !CLEAN_PREVIEW && !(CR_CLEAN[CLEAN_REF] || []).length;
  }
};

$('btn-clean-clear').onclick = () => {
  exitCleanPreview();
  CLEAN_REV++;
  CR_CLEAN[CLEAN_REF] = [];
  paintCleanBoxes();
};

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

/* Masonry: #grid uses 1px rows, so each tile spans its own measured height.

   The span is a pixel count, so it is only correct for the column width it
   was measured at. Column width changes on every window resize and at the
   1180px and 900px breakpoints, where the grid drops from four columns to
   three to two - so a span computed once, at image load, leaves every tile
   claiming its old height. That reads as tiles overlapping or floating in
   gaps: the images rescale (width:100%) but the rows they sit in do not.

   So the aspect ratio is remembered on the element and the span recomputed
   from it, which needs no reload and works for tiles already on screen. */
function layoutTile(fig) {
  const ratio = parseFloat(fig.dataset.ratio || '');
  if (!ratio) return;
  const colW = fig.clientWidth;
  if (!colW) return;                   // hidden tab: measuring now would give 0
  const span = `span ${Math.ceil(colW * ratio) + 32}`;
  // Only write when it actually changes. A write can shift the scrollbar,
  // which resizes the grid, which would call us straight back.
  if (fig.style.gridRowEnd !== span) fig.style.gridRowEnd = span;
}

function sizeTile(fig) {
  const media = fig.querySelector('img, video');
  if (!media) return;
  const apply = () => {
    const w = media.naturalWidth || media.videoWidth;
    const h = media.naturalHeight || media.videoHeight;
    if (!w || !h) return;
    fig.dataset.ratio = h / w;
    layoutTile(fig);
  };
  if (media.tagName === 'IMG') {
    media.complete ? apply() : media.addEventListener('load', apply, { once: true });
  } else {
    media.addEventListener('loadedmetadata', apply, { once: true });
  }
}

let relayoutPending = false;
function relayoutGrid() {
  if (relayoutPending) return;
  relayoutPending = true;
  requestAnimationFrame(() => {
    relayoutPending = false;
    [...$('grid').children].forEach(layoutTile);
  });
}

// Covers resize, zoom, breakpoint crossings, and the gallery tab becoming
// visible - at which point tiles that measured 0 wide finally have a width.
new ResizeObserver(relayoutGrid).observe($('grid'));

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

  // Cover choices follow the cut order; keep the pick while it still exists.
  const cover = $('re-cover').value;
  $('re-cover').innerHTML = '<option value="">none</option>' +
    SELECTED.map((_, k) => `<option value="${k}">shot ${k + 1}</option>`).join('');
  if (cover !== '' && +cover < n) $('re-cover').value = cover;

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
$('re-audio').onchange = () => { $('re-audio-start-row').hidden = !$('re-audio').value; };
$('re-hook').oninput = () => { $('re-hook-row').hidden = !$('re-hook').value.trim(); };

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
    audio_start: $('re-audio').value ? (+$('re-audio-start').value || 0) : 0,
    hook: $('re-hook').value.trim(),
    hook_seconds: +$('re-hook-secs').value || 2.5,
    cover_shot: $('re-cover').value === '' ? null : +$('re-cover').value,
  }, () => `Building a ${SELECTED.length}-shot reel now`);
  btn.disabled = false;
  if (body) switchTab('queue');
}
$('btn-reel').onclick = (e) => buildReel(e.currentTarget);
document.querySelector('[data-build-reel]').onclick = (e) => buildReel(e.currentTarget);

/* A carousel uses the same picked stills as a reel, exported as slides. */
$('btn-carousel').onclick = async (e) => {
  if (!SELECTED.length) {
    toast('Pick the stills in the gallery first, in slide order.', 'bad');
    if (isMobile()) switchTab('gallery');
    return;
  }
  if (SELECTED.length > 20) { toast('Instagram carousels hold at most 20 slides.', 'bad'); return; }
  const btn = e.currentTarget;
  btn.disabled = true;
  const target = $('re-carousel-target').value;
  const body = await queue('/api/carousel', {
    shots: SELECTED.map((k) => { const [src, id] = k.split(':'); return { src, id: +id }; }),
    target,
  }, () => `Exporting ${SELECTED.length} ${target === 'square' ? '1:1' : '4:5'} carousel slides now`);
  btn.disabled = false;
  if (body) switchTab('queue');
};

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
// SQLite writes UTC as 'YYYY-MM-DD HH:MM:SS' with no zone marker.
const fromUtc = (t) => (t ? new Date(`${String(t).replace(' ', 'T')}Z`) : null);
const toUtc = (d) => d.toISOString().slice(0, 16).replace('T', ' ');
const hhmm = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

function ago(t) {
  const d = fromUtc(t);
  if (!d || isNaN(d)) return '';
  const s = Math.max(0, (Date.now() - d) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// "tomorrow 01:00 · 2026-09-15 06:00 UTC": local time for reading, UTC because
// that is what the server stores and compares.
function whenLocal(t) {
  const d = fromUtc(t);
  if (!d || isNaN(d)) return String(t);
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(d) - day(new Date())) / 86400000);
  const label = diff === 0 ? 'today' : diff === 1 ? 'tomorrow'
    : d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
  return `${label} ${hhmm(d)} · ${String(t).slice(0, 16)} UTC`;
}

const SERVER_SIDE = new Set(['reel', 'deliver', 'carousel']);
const parseParams = (j) => { try { return JSON.parse(j.params); } catch { return {}; } };

function jobKind(params) {
  const w = params.workflow;
  if (w === 'reel') return 'reel';
  if (w === 'deliver') return 'Instagram encode';
  if (w === 'carousel') return 'carousel';
  if (w === 'outpaint') return 'extend';
  if (w === 'wan_i2v') return params.video_backend === 'ltx' ? 'animate · LTX' : 'animate';
  return params.free_prompt ? 'create' : 'fuse';
}

let JOBS = [];

function jobRow(j, pass) {
  const params = parseParams(j);
  const kind = jobKind(params);
  const server = SERVER_SIDE.has(params.workflow);
  const failed = j.status === 'failed';
  const running = j.status === 'running' && PROGRESS && PROGRESS.job_id === j.id;
  const size = params.workflow === 'wan_i2v' ? params.video_size : params.aspect;

  const meta = [esc(kind)];
  if (size && !server) meta.push(esc(size));
  if (j.attempts > 1) meta.push(`attempt ${j.attempts}`);
  if (pass === 1) meta.push(j.status === 'done' ? `done ${ago(j.finished_at)}` : 'pass 1 of 2 · pass 2 queues when this finishes');
  else if (pass === 2) meta.push(`from #${j.src_job_id}'s output`);
  else if (j.status === 'done') meta.push(`done ${ago(j.finished_at)}`);
  if (failed) meta.push(`failed ${ago(j.finished_at)}`);
  if (j.status === 'cancelled' && j.finished_at) meta.push(`cancelled ${ago(j.finished_at)}`);
  if (j.status === 'paused') meta.push(j.started_at ? 'paused · VRAM released' : 'held before it started');
  if (j.not_before && j.status === 'queued') meta.push(`<span class="sched">scheduled ${esc(whenLocal(j.not_before))}</span>`);
  if (running) {
    meta.push(`<span class="live" data-job-progress="${j.id}">${esc(PROGRESS.stage || '')}${PROGRESS.steps ? ` ${PROGRESS.step}/${PROGRESS.steps}` : ''}</span>`);
  }
  if (server && j.status === 'running') meta.push('server-side, no GPU');

  // Server-side jobs never read the control column, so they get no pause or
  // stop - offering one would do nothing. They finish in seconds anyway.
  const acts = [];
  if (j.status === 'running' && !server) acts.push(`<button data-pause="${j.id}">pause</button>`, `<button data-stop="${j.id}">stop</button>`);
  if (j.status === 'queued') {
    acts.push(pass === 2 && !j.not_before
      ? `<button data-pause="${j.id}" class="plain">hold</button>`
      : `<button data-hold="${j.id}">hold until…</button>`);
    acts.push(`<button data-stop="${j.id}">stop</button>`);
  }
  if (j.status === 'paused') acts.push(`<button data-resume="${j.id}">resume</button>`, `<button data-stop="${j.id}">stop</button>`);
  if (failed || j.status === 'cancelled') {
    acts.push(`<button data-requeue="${j.id}">requeue</button>`);
    if (j.error) acts.push(`<button data-copyerr="${j.id}">copy error</button>`);
  }
  if (pass && j.status === 'done' && j.out_filename && !isVideo(j.out_filename)) {
    acts.push(`<img class="jthumb" src="/thumbs/${esc(j.out_filename)}.jpg" alt="">`);
  }

  return `
    <div class="job ${j.status}${server ? ' server' : ''}">
      <div class="st">
        <span class="pill ${j.status}"><i></i><span>${j.status}</span></span>
        <span class="jid">#${j.id}${pass ? ` <span class="pass">pass ${pass}</span>` : ''}</span>
      </div>
      <div class="mid">
        ${failed ? `<div class="err">${esc(j.error)}</div>` : `<span class="prompt">${esc(j.prompt)}</span>`}
        ${running ? `
        <div class="bar ${PROGRESS.estimated ? 'estimated' : ''}" data-bar="${j.id}" ${PROGRESS.percent == null ? 'hidden' : ''}>
          <b style="width:${Math.round(PROGRESS.percent || 0)}%"></b>
        </div>` : ''}
        <span class="jmeta">${meta.join(' · ')}</span>
      </div>
      <div class="act">${acts.join('')}</div>
    </div>`;
}

async function refreshJobs() {
  const { ok, body } = await api('/api/jobs');
  if (!ok || !Array.isArray(body)) return;
  JOBS = body;
  const n = (s) => body.filter((j) => j.status === s).length;
  $('queue-meta').textContent =
    `${n('queued')} queued · ${n('running')} running · ${n('failed')} failed`;

  // Style match is one request that becomes two jobs: pass 2 is created from
  // pass 1's output when pass 1 finishes. Frame them together.
  const byId = new Map(body.map((j) => [j.id, j]));
  const isPass1 = (j) => !!parseParams(j).second_pass;
  const pass2Of = new Map();
  for (const j of body) {
    const parent = j.src_job_id != null && byId.get(j.src_job_id);
    if (parent && isPass1(parent) && j.batch_id && j.batch_id === parent.batch_id &&
        parseParams(j).workflow === 'img2img' && !isPass1(j)) pass2Of.set(parent.id, j);
  }
  const children = new Set([...pass2Of.values()].map((j) => j.id));

  const out = [];
  for (const j of body) {
    if (children.has(j.id)) continue;        // drawn inside its pass-1 group
    if (!isPass1(j)) { out.push(jobRow(j)); continue; }
    const second = pass2Of.get(j.id);
    out.push(`<div class="smgroup"><div class="side"><span>style match</span></div><div class="rows">
      ${jobRow(j, 1)}${second ? jobRow(second, 2) : ''}</div></div>`);
  }
  $('jobs').innerHTML = out.join('') || '<p class="hint">Queue is empty.</p>';
}

// Queue-level pause. Distinct from pausing a job: whatever is already on the
// GPU runs to completion, nothing new is dispatched after it.
// Guarded: a cached index.html served alongside a fresh app.js would leave
// this element missing, and an unguarded throw here kills every handler
// defined after it - the whole page, not just this button.
if ($('queue-pause')) $('queue-pause').onclick = async () => {
  const on = !NODE.queue_paused;
  const { ok, body } = await api(`/api/queue/${on ? 'pause' : 'resume'}`, { method: 'POST' });
  if (!ok) { toast(body.detail || 'Could not change the queue.', 'bad'); return; }
  toast(on ? 'Queue paused — the current render will finish' : 'Queue resumed', on ? 'warn' : 'ok');
  pollStatus.last = undefined;
  pollStatus();
  refreshJobs();
};

/* ---------- hold until ---------- */
let HOLD_JOB = null;

function holdPresets() {
  const now = new Date();
  const at = (days, h) => { const d = new Date(now); d.setDate(d.getDate() + days); d.setHours(h, 0, 0, 0); return d; };
  const tonight = at(now.getHours() < 1 ? 0 : 1, 1);   // the next 01:00
  const inH = (d) => `in ${Math.max(1, Math.round((d - now) / 3600000))}h`;
  const six = new Date(now.getTime() + 6 * 3600000);
  const nine = at(1, 9);
  return [
    { label: 'tonight 1:00', when: tonight, aside: inH(tonight) },
    { label: 'in 6 hours', when: six, aside: hhmm(six) },
    { label: 'tomorrow 9:00', when: nine, aside: inH(nine) },
  ];
}

function openHold(btn, id) {
  const j = JOBS.find((x) => x.id === id);
  HOLD_JOB = id;
  const presets = holdPresets();
  $('hold-list').innerHTML =
    `<button type="button" data-hold-pause><span>hold until I resume</span><em>no time</em></button>` +
    presets.map((p, i) => `<button type="button" data-hold-at="${i}"><span>${p.label}</span><em>${p.aside}</em></button>`).join('') +
    (j?.not_before ? '<button type="button" data-hold-clear><span>clear schedule · run when ready</span></button>' : '');
  $('hold-list').presets = presets;
  const d = presets[0].when;
  const pad = (x) => String(x).padStart(2, '0');
  $('hold-at').value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const pop = $('hold-pop');
  pop.hidden = false;
  const r = btn.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  pop.style.left = `${Math.max(16, Math.min(r.right - w, innerWidth - w - 16))}px`;
  pop.style.top = `${r.bottom + 6 + h > innerHeight - 16 ? Math.max(16, r.top - h - 6) : r.bottom + 6}px`;
  pop.querySelector('button')?.focus();
}

const closeHold = () => { $('hold-pop').hidden = true; HOLD_JOB = null; };

async function schedule(id, when) {
  const { ok, body } = await api(`/api/jobs/${id}/schedule`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ not_before: when ? toUtc(when) : null }),
  });
  if (!ok) { toast(body.detail || 'Could not schedule that job.', 'bad'); return; }
  toast(when ? `Job #${id} held until ${hhmm(when)}` : `Job #${id} will run when the node is ready`, 'teal');
  refreshJobs();
  pollStatus.last = undefined;
}

$('hold-pop').onclick = async (e) => {
  const id = HOLD_JOB;
  if (id == null) return;
  const b = e.target.closest('button');
  if (!b) return;
  if (b.id === 'hold-go') {
    const when = $('hold-at').value ? new Date($('hold-at').value) : null;
    if (!when || isNaN(when)) { toast('Pick a time to hold until.', 'bad'); return; }
    if (when < new Date()) { toast('That time has already passed.', 'bad'); return; }
    closeHold();
    return schedule(id, when);
  }
  closeHold();
  if ('holdPause' in b.dataset) {
    const { ok, body } = await api(`/api/jobs/${id}/pause`, { method: 'POST' });
    if (!ok) { toast(body.detail || 'Could not hold.', 'bad'); return; }
    toast(`Job #${id} held`, 'warn');
    refreshJobs();
    pollStatus.last = undefined;
  } else if ('holdClear' in b.dataset) {
    schedule(id, null);
  } else if (b.dataset.holdAt != null) {
    schedule(id, $('hold-list').presets[+b.dataset.holdAt].when);
  }
};
document.addEventListener('click', (e) => {
  if ($('hold-pop').hidden || e.target.closest('#hold-pop, [data-hold]')) return;
  closeHold();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('hold-pop').hidden) closeHold(); });
$('jobs').closest('.tab').addEventListener('scroll', () => { if (!$('hold-pop').hidden) closeHold(); });

$('jobs').onclick = async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  const d = b.dataset;
  if (d.hold) {
    if (HOLD_JOB === +d.hold && !$('hold-pop').hidden) return closeHold();
    return openHold(b, +d.hold);
  }
  if (d.cancel) {
    await api(`/api/jobs/${d.cancel}`, { method: 'DELETE' });
  } else if (d.pause) {
    const { ok, body } = await api(`/api/jobs/${d.pause}/pause`, { method: 'POST' });
    if (!ok) { toast(body.detail || 'Could not pause.', 'bad'); return; }
    // A running job pauses at the node's next poll, not instantly. Saying so
    // stops it looking broken for the couple of seconds in between.
    toast(body.pending ? `Job #${d.pause} stopping at the node…` : `Job #${d.pause} held`, 'warn');
  } else if (d.resume) {
    const { ok, body } = await api(`/api/jobs/${d.resume}/resume`, { method: 'POST' });
    if (!ok) { toast(body.detail || 'Could not resume.', 'bad'); return; }
    toast(`Job #${d.resume} back on the queue`);
  } else if (d.stop) {
    const { ok, body } = await api(`/api/jobs/${d.stop}/stop`, { method: 'POST' });
    if (!ok) { toast(body.detail || 'Could not stop.', 'bad'); return; }
    toast(`Job #${d.stop} stopped`, 'bad');
  } else if (d.requeue) {
    const { ok, body } = await api(`/api/jobs/${d.requeue}/requeue`, { method: 'POST' });
    if (!ok) { toast(body.detail || 'Could not requeue.', 'bad'); return; }
    toast(`Job #${d.requeue} back on the queue`);
  } else if (d.copyerr) {
    const j = JOBS.find((x) => x.id === +d.copyerr);
    try {
      await navigator.clipboard.writeText(j?.error || '');
    } catch { toast('Could not copy the error. Select the error text to copy it.', 'bad'); return; }
    b.textContent = 'copied';
    setTimeout(() => { b.textContent = 'copy error'; }, 1200);
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
  $('lb-pos').textContent = at >= 0 ? `${at + 1} of ${list.length}` : '';
  $('lb-keys').hidden = list.length < 2;
}

function openLightbox(img) {
  const opening = $('lightbox').hidden;
  if (opening) lbReturnFocus = document.activeElement;
  CURRENT = img;
  $('lightbox').hidden = false;
  if (opening) $('lb-close').focus({ preventScroll: true });
  $('deliver-box').hidden = true;
  const vid = isVideo(img.filename);
  $('lb-img').hidden = vid;
  $('lb-vid').hidden = !vid;
  $('animate-box').hidden = true;
  $('lb-animate').hidden = vid;      // a clip is already the output
  // Clips and stills both deliver; a still becomes a 1080-wide JPEG.
  $('lb-deliver').querySelector('span').textContent = vid ? 'Make Instagram mp4' : 'Make Instagram JPEG';
  $('deliver-meta').textContent = vid ? 'h.264 · 1080 wide · faststart' : 'jpeg q95 · 1080 wide';
  $('deliver-hint').textContent = vid ? 'Fits and pads — nothing is cropped.'
    : 'Crops when within 4% of the ratio (a 680×856 render loses 6px), pads otherwise.';
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

  const params = parseParams(img);
  const recipe = params.recipe;
  const create = params.free_prompt && !vid && !img.src_image_id && params.workflow !== 'outpaint';
  $('lb-reuse').hidden = !create && !(recipe && recipe.subject_a);
  $('lb-reuse').onclick = () => {
    const extraNegative = (img.negative || '').startsWith(CONFIG.negative)
      ? img.negative.slice(CONFIG.negative.length).replace(/^,\s*/, '') : img.negative || '';
    $('steps').value = params.steps ?? 30;
    $('cfg').value = params.cfg ?? 5;
    const checkpoint = params.checkpoint || '';
    if (checkpoint && ![...$('checkpoint').options].some((o) => o.value === checkpoint)) {
      $('checkpoint').add(new Option(checkpoint, checkpoint));
    }
    $('checkpoint').value = checkpoint;
    if (create) {
      $('cr-prompt').value = img.prompt || '';
      $('cr-quality').checked = false; // saved prompt already contains its quality tags
      $('cr-negative').value = extraNegative;
      $('cr-aspect').value = params.aspect || 'portrait';
      $('cr-count').value = 1;
      let refs;
      try { refs = JSON.parse(img.ref_ids || '[]'); } catch { refs = []; }
      if (!refs.length && img.ref_id) refs = [img.ref_id];
      CR_REFS = refs.filter((id) => CR_REF_ROWS.some((r) => r.id === id));
      $('cr-mode').value = params.workflow === 'ipadapter' ? 'ipadapter_multi' : params.workflow || '';
      $('cr-denoise').value = params.denoise ?? 0.65;
      $('cr-ipweight').value = params.ip_weight ?? 0.7;
      $('cr-stylematch').checked = !!params.second_pass;
      $('cr-p2-add').value = params.second_pass_prompt_add || '';
      $('cr-p2-avoid').value = params.second_pass_negative_add || '';
      $('cr-hires').checked = !!params.hires;
      exitCleanPreview();
      CLEAN_REV++;
      if (CR_REFS.length === 1) CR_CLEAN[CR_REFS[0]] = params.clean_regions || [];
      paintPicks();
      if (refs.length !== CR_REFS.length) toast('Some references were deleted. Choose replacements before generating.', 'warn');
    } else {
      $('subject-a').value = recipe.subject_a || '';
      $('subject-b').value = recipe.subject_b || '';
      $('mode').value = recipe.mode || 'design_fusion';
      $('extra').value = recipe.extra || '';
      $('negative').value = extraNegative;
      $('aspect').value = params.aspect || 'portrait';
      $('ref').value = img.ref_id || '';
      $('workflow').value = params.workflow || '';
      $('denoise').value = params.denoise ?? 0.65;
      $('ip-weight').value = params.ip_weight ?? 0.7;
      syncHint();
    }
    syncCounts();
    saveForm();
    closeLb();
    switchTask(create ? 'create' : 'fuse');
    if (isMobile()) switchScreen('studio');
    toast(`Loaded the recipe from #${img.id}`);
  };
}

$('lb-prev').onclick = () => stepLightbox(-1);
$('lb-next').onclick = () => stepLightbox(1);

// A horizontal touch swipe pages stills; vertical drags keep scrolling the
// detail sheet. Ignore the video's native controls and additional fingers.
let lbTouch = null;
$('lb-stage').addEventListener('pointerdown', (e) => {
  if (e.pointerType !== 'touch' || !e.isPrimary || e.target.closest('button, video')) return;
  lbTouch = { id: e.pointerId, x: e.clientX, y: e.clientY, key: CURRENT?.key };
});
$('lb-stage').addEventListener('pointerup', (e) => {
  if (!lbTouch || lbTouch.id !== e.pointerId) return;
  const { x, y, key } = lbTouch;
  lbTouch = null;
  const dx = e.clientX - x, dy = e.clientY - y;
  if (CURRENT?.key === key && Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) {
    stepLightbox(dx < 0 ? 1 : -1);
  }
});
$('lb-stage').addEventListener('pointercancel', () => { lbTouch = null; });

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

let lbReturnFocus = null;
const closeLb = () => {
  $('lightbox').hidden = true;
  $('lb-vid').pause?.();
  lbReturnFocus?.focus?.({ preventScroll: true });
};
$('lb-close').onclick = closeLb;
$('lightbox').onclick = (e) => { if (e.target.id === 'lightbox') closeLb(); };
document.addEventListener('keydown', (e) => {
  if ($('lightbox').hidden) return;
  if (e.key === 'Escape') return closeLb();
  if (e.key === 'Tab') {
    const controls = [...$('lightbox').querySelectorAll('button, a[href], input, textarea, select, [tabindex="0"]')]
      .filter((el) => !el.disabled && el.getClientRects().length);
    const next = controls[(controls.indexOf(document.activeElement) + (e.shiftKey ? -1 : 1) + controls.length) % controls.length];
    if (next) { e.preventDefault(); next.focus(); }
    return;
  }
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

function syncAnimate() {
  // Both backends accept these six canvases; the frame multiple is enforced
  // by the backend builder. Keep the chosen model explicit in the request.
  if (!$('an-backend').value) $('an-backend').value = 'wan';
  if (!$('an-size').value) $('an-size').value = 'story_540';
  const ltx = $('an-backend').value === 'ltx';
  $('an-go').textContent = `Queue ${ltx ? 'LTX' : 'WAN'} clip`;
}
$('an-backend').addEventListener('change', syncAnimate);
$('an-size').addEventListener('change', syncAnimate);
$('lb-animate').onclick = () => {
  $('animate-box').hidden = !$('animate-box').hidden;
  $('deliver-box').hidden = true;
  syncAnimate();
};

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
  if (!$('an-seconds').reportValidity()) return;
  const btn = e.currentTarget;
  btn.disabled = true;
  const body = await queue('/api/generate', {
    prompt: motion,
    quality: false,
    negative_full: 'static, still image, frozen, jpeg artifacts, watermark, text',
    workflow: 'wan_i2v', src_image_id: CURRENT.id, count: 1,
    seconds: +$('an-seconds').value, video_size: $('an-size').value,
    video_backend: $('an-backend').value,
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
  placeSampler(task);
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
