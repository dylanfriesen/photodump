const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, body };
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const isVideo = (f) => /\.(webm|mp4)$/i.test(f || '');
const isReel = (f) => /\.mp4$/i.test(f || '');

let CONFIG = null;
let CURRENT = null;   // item open in the lightbox
let SELECTED = [];    // ordered image ids for the reel builder
let REEL_MODE = false;
let IMAGES = [];      // last gallery payload, so the filmstrip can look ids up
let NODE = { online: false, current: null, queued: 0 };

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

/* ---------- boot ---------- */
async function boot() {
  const { body } = await api('/api/config');
  CONFIG = body;

  $('mode').innerHTML = Object.entries(body.modes)
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join('');

  const ratio = { portrait: '4:5', square: '1:1', story: '9:16', landscape: '3:2' };
  $('aspect').innerHTML = body.aspects
    .map((a, i) => `<span data-value="${esc(a)}" class="${i === 0 ? 'on' : ''}">${esc(ratio[a] || a)}</span>`).join('');

  ['aspect', 'ex-target', 'ex-anchor', 're-timing'].forEach((id) => initSeg($(id)));

  const chips = body.starters.map((s, i) => `<button class="chip" data-starter="${i}">${esc(s.name)}</button>`).join('');
  $('starters').innerHTML = chips;
  $('empty-starters').innerHTML = chips;

  $('node-bars').innerHTML = [0, 1, 2, 3, 4].map((i) => `<i style="height:${6 + i * 2}px"></i>`).join('');

  syncHint(); syncCounts(); syncFeather(); syncSelection();
  await Promise.all([refreshRefs(), refreshRecipes(), refreshGallery(), refreshJobs()]);
  pollStatus();
  probeNode();
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

async function pollStatus() {
  const { body } = await api('/api/status');
  NODE = body;
  const state = body.current != null ? 'rendering' : body.online ? 'ready' : 'asleep';
  document.body.dataset.node = state;
  $('node-dot').innerHTML = DOT[state];
  $('node-text').textContent = state === 'rendering'
    ? `rendering job #${body.current} · ${body.queued} queued`
    : state === 'ready'
      ? `node ready · ${body.queued} queued`
      : `desktop asleep · ${body.queued} queued, will drain on wake`;

  const tint = state === 'rendering' ? 'var(--lime)' : state === 'ready' ? 'var(--teal)' : 'var(--violet)';
  [...$('node-bars').children].forEach((b, i) => {
    b.style.background = i < body.queued ? tint : 'rgba(255,255,255,.09)';
  });
  $('queue-badge').textContent = body.queued;
  $('queue-badge').style.color = body.queued > 0 ? tint : 'var(--mute-4)';

  const hint = state === 'asleep'
    ? 'Desktop is asleep — this will sit in the queue and drain on wake.'
    : state === 'rendering'
      ? `Node is busy with job #${body.current} — yours starts next.`
      : 'Node is ready — this starts immediately.';
  document.querySelectorAll('[data-queue-hint]').forEach((e) => { e.textContent = hint; });
  document.querySelectorAll('[data-animate-hint]').forEach((e) => {
    e.textContent = state === 'asleep' ? 'queues to the desktop · drains on wake' : 'starts on the desktop shortly';
  });

  const badge = $('queue-tab-badge');
  badge.hidden = !body.queued;
  badge.textContent = body.queued;

  const busy = body.current != null;
  if (pollStatus.last !== undefined && pollStatus.last !== `${body.current}|${body.queued}`) {
    refreshGallery(); refreshJobs();
  }
  pollStatus.last = `${body.current}|${body.queued}`;
  setTimeout(pollStatus, busy ? 3000 : 10000);
}

async function probeNode() {
  const { ok, body } = await api('/api/node');
  if (!ok) return;
  if (body.checkpoints?.length) {
    $('checkpoint').innerHTML = '<option value="">default</option>' +
      body.checkpoints.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  }
  if (!body.has_ipadapter) {
    const opt = $('workflow').querySelector('[value="ipadapter"]');
    if (opt) { opt.disabled = true; opt.textContent += ' — node pack not installed'; }
  }
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
  syncCounts();
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
  if (!v.subject_a.trim() || !v.subject_b.trim()) { alert('Both sources need a value.'); return; }
  e.currentTarget.disabled = true;
  await api('/api/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(v),
  });
  e.currentTarget.disabled = false;
  refreshJobs();
  pollStatus.last = undefined;
};

function applyStarter(i) {
  const s = CONFIG.starters[i];
  $('subject-a').value = s.subject_a;
  $('subject-b').value = s.subject_b;
  $('mode').value = s.mode;
  $('extra').value = s.extra || '';
  syncHint();
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
  const { body } = await api('/api/recipes');
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
};

/* ---------- references ---------- */
async function refreshRefs() {
  const { body } = await api('/api/refs');
  const imgs = body.filter((r) => r.kind !== 'audio');
  const opts = imgs.map((r) => `<option value="${r.id}">${esc(r.label)} (${esc(r.kind)})</option>`).join('');
  $('ref').innerHTML = '<option value="">none</option>' + opts;
  $('ex-ref').innerHTML = opts || '<option value="">upload one under References</option>';
  $('re-audio').innerHTML = '<option value="">no music</option>' +
    body.filter((r) => r.kind === 'audio').map((r) => `<option value="${r.id}">${esc(r.label)}</option>`).join('');

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
  if (!r.ok) { alert('Upload failed: ' + (await r.text())); return; }
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

/* ---------- gallery ---------- */
function tileMarkup(i) {
  const vid = isVideo(i.filename);
  const at = SELECTED.indexOf(i.id);
  const cls = [at >= 0 ? 'picked' : '', vid ? 'unpickable' : ''].filter(Boolean).join(' ');
  const media = vid
    ? `<video src="/out/${esc(i.filename)}" muted loop playsinline preload="metadata"></video>`
    : `<img src="/thumbs/${esc(i.filename)}.jpg" alt="" loading="lazy">`;
  return `
    <figure class="${cls}" data-img='${esc(JSON.stringify(i))}'>
      <div class="art">
        ${media}
        ${vid ? `<span class="badge-motion"><i></i>${isReel(i.filename) ? '.mp4' : '.webm'}</span>` : ''}
        ${i.favourite && at < 0 ? '<span class="star">&#9733;</span>' : ''}
        ${at >= 0 ? `<span class="pick">${at + 1}</span>` : ''}
      </div>
      <figcaption><span>#${i.id}</span><span class="dim">${i.seed ? `seed ${i.seed}` : ''}</span></figcaption>
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

async function refreshGallery() {
  const fav = $('only-fav').checked ? '?favourites=true' : '';
  const { body } = await api('/api/images' + fav);
  IMAGES = body;
  const empty = body.length === 0;
  $('gallery-empty').hidden = !empty;
  $('grid').hidden = empty;
  $('grid').innerHTML = body.map(tileMarkup).join('');
  [...$('grid').children].forEach(sizeTile);

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
  const at = SELECTED.indexOf(img.id);
  if (at >= 0) SELECTED.splice(at, 1); else SELECTED.push(img.id);
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
    const by = Object.fromEntries(IMAGES.map((i) => [i.id, i]));
    $('strip-shots').innerHTML = SELECTED.map((id, k) => {
      const im = by[id];
      return `${k ? '<span class="cut">&#9679;</span>' : ''}
        <div class="shot" data-shot="${id}">
          ${im ? `<img src="/thumbs/${esc(im.filename)}.jpg" alt="">` : ''}
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
  const at = SELECTED.indexOf(+id);
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
$('re-transition').onchange = syncSelection;

$('sel-clear').onclick = () => { SELECTED = []; syncSelection(); refreshGallery(); };

$('sel-favs').onclick = async () => {
  const { body } = await api('/api/images?favourites=true');
  SELECTED = body.filter((i) => !isVideo(i.filename)).map((i) => i.id).reverse();
  syncSelection();
  refreshGallery();
};

async function buildReel(btn) {
  if (SELECTED.length < 2) { alert('Pick at least two stills.'); return; }
  btn.disabled = true;
  const { ok, body } = await api('/api/reels', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      image_ids: SELECTED,
      bpm: $('re-timing').value === 'bpm' ? +$('re-bpm').value : null,
      beats_per_shot: +$('re-beats').value,
      seconds: +$('re-seconds').value,
      motion: $('re-motion').value,
      transition: $('re-transition').value,
      audio_ref_id: $('re-audio').value ? +$('re-audio').value : null,
    }),
  });
  btn.disabled = false;
  if (!ok) { alert(body.detail || 'reel failed'); return; }
  refreshJobs();
  pollStatus.last = undefined;
}
$('btn-reel').onclick = (e) => buildReel(e.currentTarget);
document.querySelector('[data-build-reel]').onclick = (e) => buildReel(e.currentTarget);

/* ---------- extend ---------- */
$('btn-extend').onclick = async (e) => {
  const refId = $('ex-ref').value;
  if (!refId) { alert('Upload a source image under References first.'); return; }
  const scene = $('ex-prompt').value.trim();
  if (!scene) { alert('Describe the finished scene so the model knows what to paint.'); return; }
  e.currentTarget.disabled = true;
  await api('/api/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt: scene, workflow: 'outpaint', ref_id: +refId,
      count: +$('ex-count').value,
      extend_target: $('ex-target').value,
      extend_anchor: $('ex-anchor').value,
      feathering: +$('ex-feather').value,
      steps: +$('steps').value, cfg: +$('cfg').value,
      checkpoint: $('checkpoint').value || null,
    }),
  });
  e.currentTarget.disabled = false;
  refreshJobs();
  pollStatus.last = undefined;
};

/* ---------- queue ---------- */
async function refreshJobs() {
  const { body } = await api('/api/jobs');
  const n = (s) => body.filter((j) => j.status === s).length;
  $('queue-meta').textContent =
    `${n('queued')} queued · ${n('running')} running · ${n('failed')} failed`;

  $('jobs').innerHTML = body.map((j) => {
    const params = (() => { try { return JSON.parse(j.params); } catch { return {}; } })();
    const kind = params.workflow === 'reel' ? 'reel'
      : params.workflow === 'outpaint' ? 'extend'
      : params.workflow === 'wan_i2v' ? 'animate' : 'fuse';
    const failed = j.status === 'failed';
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
        <span class="jmeta">${esc(kind)}${params.aspect ? ` · ${esc(params.aspect)}` : ''}${j.attempts > 1 ? ` · attempt ${j.attempts}` : ''}</span>
      </div>
      <div class="act">
        ${j.status === 'queued' ? `<button data-cancel="${j.id}">cancel</button>` : ''}
      </div>
    </div>`;
  }).join('') || '<p class="hint">Queue is empty.</p>';
}

$('jobs').onclick = async (e) => {
  if (!e.target.dataset.cancel) return;
  await api(`/api/jobs/${e.target.dataset.cancel}`, { method: 'DELETE' });
  refreshJobs();
  pollStatus.last = undefined;
};

/* ---------- lightbox ---------- */
function openLightbox(img) {
  CURRENT = img;
  $('lightbox').hidden = false;
  const vid = isVideo(img.filename);
  $('lb-img').hidden = vid;
  $('lb-vid').hidden = !vid;
  $('animate-box').hidden = true;
  $('lb-animate').hidden = vid;   // a clip is already the output
  if (vid) { $('lb-vid').src = `/out/${img.filename}`; $('lb-img').removeAttribute('src'); }
  else { $('lb-img').src = `/out/${img.filename}`; $('lb-vid').removeAttribute('src'); }

  $('lb-id').textContent = `#${img.id}`;
  $('lb-meta').textContent = [img.seed ? `seed ${img.seed}` : '', img.filename].filter(Boolean).join(' · ');
  $('lb-prompt').textContent = img.prompt || '';
  $('lb-download').href = `/out/${img.filename}`;
  $('lb-download').download = img.filename;
  setFav(img.favourite);
  renderCaption(img.caption, img.hashtags);
}

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
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('lightbox').hidden) closeLb(); });

$('lb-fav').onclick = async () => {
  const { body } = await api(`/api/images/${CURRENT.id}/favourite`, { method: 'POST' });
  CURRENT.favourite = body.favourite;
  setFav(body.favourite);
  refreshGallery();
};

$('lb-caption-btn').onclick = async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true; btn.textContent = 'Drafting…';
  const { ok, body } = await api(`/api/images/${CURRENT.id}/caption`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(formValues()),
  });
  btn.disabled = false; btn.textContent = 'Draft caption';
  if (!ok) { alert(body.error || 'caption failed'); return; }
  renderCaption(body.caption, body.hashtags.join(' '));
  refreshGallery();
};

$('lb-animate').onclick = () => { $('animate-box').hidden = !$('animate-box').hidden; };

$('an-go').onclick = async (e) => {
  const motion = $('an-prompt').value.trim();
  if (!motion) { alert('Describe the motion you want.'); return; }
  e.currentTarget.disabled = true;
  await api('/api/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt: motion,
      negative_full: 'static, still image, frozen, jpeg artifacts, watermark, text',
      workflow: 'wan_i2v', src_image_id: CURRENT.id, count: 1,
      seconds: +$('an-seconds').value, video_size: $('an-size').value,
    }),
  });
  e.currentTarget.disabled = false;
  closeLb();
  refreshJobs();
  pollStatus.last = undefined;
};

$('lb-delete').onclick = async () => {
  if (!confirm('Delete this image?')) return;
  await api(`/api/images/${CURRENT.id}`, { method: 'DELETE' });
  closeLb();
  refreshGallery();
};

/* ---------- tabs ---------- */
function switchTask(task) {
  document.querySelectorAll('.tabs.sub button').forEach((b) => b.classList.toggle('active', b.dataset.task === task));
  document.querySelectorAll('.task').forEach((d) => { d.hidden = d.dataset.task !== task; });
  REEL_MODE = task === 'reel';
  document.querySelector('.output').classList.toggle('picking', REEL_MODE);
  $('picking-bar').hidden = !REEL_MODE;
  if (REEL_MODE) switchTab('gallery');
  syncSelection();
  refreshGallery();
}

function switchTab(tab) {
  document.querySelectorAll('.tabs:not(.sub) > button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab').forEach((d) => { d.hidden = d.dataset.tab !== tab; });
  document.querySelectorAll('[data-gallery-only]').forEach((e) => { e.hidden = tab !== 'gallery'; });
}

document.querySelector('.tabs.sub').onclick = (e) => {
  const t = e.target.closest('button')?.dataset.task;
  if (t) switchTask(t);
};
document.querySelector('.tabs:not(.sub)').onclick = (e) => {
  const t = e.target.closest('button')?.dataset.tab;
  if (t) switchTab(t);
};

boot();
