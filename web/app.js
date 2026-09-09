const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, body };
};

let CONFIG = null;
let CURRENT = null; // image open in the lightbox

// ---------- boot ----------

async function boot() {
  const { body } = await api('/api/config');
  CONFIG = body;

  $('mode').innerHTML = Object.entries(body.modes)
    .map(([k, v]) => `<option value="${k}">${v.label}</option>`).join('');
  $('aspect').innerHTML = body.aspects
    .map((a) => `<option value="${a}">${a}</option>`).join('');
  $('starters').innerHTML = body.starters
    .map((s, i) => `<span class="chip" data-starter="${i}">${s.name}</span>`).join('');

  syncHint();
  await Promise.all([refreshRefs(), refreshRecipes(), refreshGallery(), refreshJobs()]);
  pollStatus();
  probeNode();
}

function syncHint() {
  const m = CONFIG.modes[$('mode').value];
  $('mode-hint').textContent = m ? m.hint : '';
}

// ---------- render node status ----------

async function pollStatus() {
  const { body } = await api('/api/status');
  const dot = $('node-dot');
  const busy = body.current != null;
  dot.className = 'dot ' + (busy ? 'busy' : body.online ? 'on' : 'off');
  $('node-text').textContent = body.online
    ? (busy ? `rendering job #${body.current} · ${body.queued} queued` : `node ready · ${body.queued} queued`)
    : `desktop asleep · ${body.queued} queued, will drain on wake`;
  $('queue-badge').textContent = body.queued;

  // Cheap change-detection: refresh the grid only when work has moved.
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
      body.checkpoints.map((c) => `<option value="${c}">${c}</option>`).join('');
  }
  if (!body.has_ipadapter) {
    const opt = $('workflow').querySelector('[value="ipadapter"]');
    if (opt) { opt.disabled = true; opt.textContent += ' — node pack not installed'; }
  }
}

// ---------- forge ----------

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

$('mode').onchange = syncHint;

$('btn-preview').onclick = async () => {
  const { body } = await api('/api/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(formValues()),
  });
  $('preview').hidden = false;
  $('preview').textContent = `+ ${body.prompt}\n\n- ${body.negative}`;
};

$('btn-generate').onclick = async (e) => {
  const v = formValues();
  if (!v.subject_a.trim() || !v.subject_b.trim()) {
    alert('Both sources need a value.'); return;
  }
  e.target.disabled = true;
  await api('/api/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(v),
  });
  e.target.disabled = false;
  refreshJobs();
  pollStatus.last = undefined;
};

$('starters').onclick = (e) => {
  const i = e.target.dataset.starter;
  if (i === undefined) return;
  const s = CONFIG.starters[i];
  $('subject-a').value = s.subject_a;
  $('subject-b').value = s.subject_b;
  $('mode').value = s.mode;
  $('extra').value = s.extra || '';
  syncHint();
};

// ---------- recipes ----------

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
    `<span class="chip" data-recipe='${JSON.stringify(r).replace(/'/g, '&#39;')}'>${r.name}<span class="x" data-del="${r.id}">×</span></span>`
  ).join('');
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

// ---------- references ----------

async function refreshRefs() {
  const { body } = await api('/api/refs');
  const opts = body.map((r) => `<option value="${r.id}">${r.label} (${r.kind})</option>`).join('');
  $('ref').innerHTML = '<option value="">none</option>' + opts;
  $('ex-ref').innerHTML = opts || '<option value="">upload one in References</option>';
  $('ref-grid').innerHTML = body.map((r) => `
    <figure data-ref="${r.id}">
      <img src="/refs/${r.filename}" alt="${r.label}" loading="lazy">
      <figcaption>${r.label} · ${r.kind} <span class="x" data-del="${r.id}">×</span></figcaption>
    </figure>`).join('') || '<p class="empty">No references yet.</p>';
}

$('ref-form').onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData();
  fd.append('file', $('ref-file').files[0]);
  fd.append('label', $('ref-label').value);
  fd.append('kind', $('ref-kind').value);
  const r = await fetch('/api/refs', { method: 'POST', body: fd });
  if (!r.ok) { alert('Upload failed: ' + (await r.text())); return; }
  e.target.reset();
  refreshRefs();
};

$('ref-grid').onclick = async (e) => {
  if (!e.target.dataset.del) return;
  if (!confirm('Delete this reference?')) return;
  await api(`/api/refs/${e.target.dataset.del}`, { method: 'DELETE' });
  refreshRefs();
};

// ---------- gallery ----------

async function refreshGallery() {
  const fav = $('only-fav').checked ? '?favourites=true' : '';
  const { body } = await api('/api/images' + fav);
  $('gallery-empty').hidden = body.length > 0;
  $('grid').innerHTML = body.map((i) => `
    <figure data-img='${JSON.stringify(i).replace(/'/g, '&#39;')}'>
      <img src="/thumbs/${i.filename}.jpg" alt="" loading="lazy">
      ${i.favourite ? '<span class="star">★</span>' : ''}
      <figcaption>#${i.id} · seed ${i.seed}</figcaption>
    </figure>`).join('');
}

$('only-fav').onchange = refreshGallery;

$('grid').onclick = (e) => {
  const fig = e.target.closest('[data-img]');
  if (fig) openLightbox(JSON.parse(fig.dataset.img));
};

// ---------- queue ----------

async function refreshJobs() {
  const { body } = await api('/api/jobs');
  $('jobs').innerHTML = body.map((j) => `
    <div class="job">
      <span class="st ${j.status}">${j.status}</span>
      <span class="txt" title="${(j.prompt || '').replace(/"/g, '&quot;')}">#${j.id} ${j.error || j.prompt}</span>
      ${j.status === 'queued' ? `<button class="small ghost" data-cancel="${j.id}">cancel</button>` : ''}
    </div>`).join('') || '<p class="empty">Queue is empty.</p>';
}

$('jobs').onclick = async (e) => {
  if (!e.target.dataset.cancel) return;
  await api(`/api/jobs/${e.target.dataset.cancel}`, { method: 'DELETE' });
  refreshJobs();
};

// ---------- lightbox ----------

function openLightbox(img) {
  CURRENT = img;
  $('lightbox').hidden = false;
  $('lb-img').src = `/out/${img.filename}`;
  $('lb-prompt').textContent = img.prompt || '';
  $('lb-download').href = `/out/${img.filename}`;
  $('lb-download').download = img.filename;
  $('lb-fav').textContent = img.favourite ? '★ Favourited' : '☆ Favourite';
  renderCaption(img.caption, img.hashtags);
}

function renderCaption(caption, tags) {
  $('lb-caption').innerHTML = caption
    ? `${caption}<span class="tags">${tags || ''}</span>`
    : '<span class="empty">No caption drafted.</span>';
}

$('lb-close').onclick = () => { $('lightbox').hidden = true; };
$('lightbox').onclick = (e) => { if (e.target.id === 'lightbox') $('lightbox').hidden = true; };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('lightbox').hidden = true; });

$('lb-fav').onclick = async () => {
  const { body } = await api(`/api/images/${CURRENT.id}/favourite`, { method: 'POST' });
  CURRENT.favourite = body.favourite;
  $('lb-fav').textContent = body.favourite ? '★ Favourited' : '☆ Favourite';
  refreshGallery();
};

$('lb-caption-btn').onclick = async (e) => {
  e.target.disabled = true;
  e.target.textContent = 'Drafting…';
  const { ok, body } = await api(`/api/images/${CURRENT.id}/caption`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(formValues()),
  });
  e.target.disabled = false;
  e.target.textContent = 'Draft caption';
  if (!ok) { renderCaption('', ''); alert(body.error || 'caption failed'); return; }
  renderCaption(body.caption, body.hashtags.join(' '));
  refreshGallery();
};

$('lb-delete').onclick = async () => {
  if (!confirm('Delete this image?')) return;
  await api(`/api/images/${CURRENT.id}`, { method: 'DELETE' });
  $('lightbox').hidden = true;
  refreshGallery();
};

// ---------- extend / outpaint ----------

$('btn-extend').onclick = async (e) => {
  const refId = $('ex-ref').value;
  if (!refId) { alert('Upload a source image under References first.'); return; }
  const scene = $('ex-prompt').value.trim();
  if (!scene) { alert('Describe the finished scene so the model knows what to paint.'); return; }
  e.target.disabled = true;
  await api('/api/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt: scene,
      workflow: 'outpaint',
      ref_id: +refId,
      count: +$('ex-count').value,
      extend_target: $('ex-target').value,
      extend_anchor: $('ex-anchor').value,
      feathering: +$('ex-feather').value,
      steps: +$('steps').value,
      cfg: +$('cfg').value,
      checkpoint: $('checkpoint').value || null,
    }),
  });
  e.target.disabled = false;
  refreshJobs();
  pollStatus.last = undefined;
};

// ---------- task switcher ----------

document.querySelector('.tabs.sub').onclick = (e) => {
  const task = e.target.dataset.task;
  if (!task) return;
  document.querySelectorAll('.tabs.sub button').forEach((b) => b.classList.toggle('active', b.dataset.task === task));
  document.querySelectorAll('.task').forEach((d) => { d.hidden = d.dataset.task !== task; });
};

// ---------- tabs ----------

document.querySelector('.tabs').onclick = (e) => {
  const tab = e.target.dataset.tab;
  if (!tab) return;
  document.querySelectorAll('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab').forEach((d) => { d.hidden = d.dataset.tab !== tab; });
};

boot();
