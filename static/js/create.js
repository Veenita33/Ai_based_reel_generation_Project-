/* Create-reel workflow: format cards -> clips/script -> upload (real progress) -> real stage polling -> preview. */
(function () {
  const {el, api, toast} = App;
  const $ = id => document.getElementById(id);
  const form = $('reelForm'), formView = $('formView'), progressView = $('progressView'), resultView = $('resultView');
  const URLS = window.REEL_URLS;
  const isAuth = form.dataset.authenticated === '1';
  const MAX_CLIPS = +form.dataset.maxClips;
  const DRAFT_KEY = 'reelDraft';
  const STAGES = ['voiceover', 'clips', 'rendering', 'finalizing'];

  const CFG = window.NARRATION_CFG || {};
  const AI_ON = form.dataset.ai === '1';
  const EXT = CFG.extensions || {video: [], image: [], audio: []};
  const LIMIT_MB = CFG.limitsMb || {video: 150, image: 20, audio: 50};

  let items = [];           // ordered media: {key, kind, name, size, duration, url, file|null, remote:bool, li}
  let draft = null;         // {id, sig} once the server holds our media + a script
  let approved = false, aiBusy = false, aiTimer = null, keySeq = 0;
  let pollTimer = null, polling = false, uploading = false;
  /* ------------------------------ view switching ------------------------------ */
  function show(view) {
    formView.hidden = view !== 'form';
    progressView.hidden = view !== 'progress';
    resultView.hidden = view !== 'result';
    window.scrollTo({top: 0, behavior: 'smooth'});
  }

  /* ------------------------------ platform / format ------------------------------ */
  const radios = [...form.querySelectorAll('input[name="platform"]')];
  const customPanel = $('customPanel'), wIn = $('customW'), hIn = $('customH');
  const selected = () => radios.find(r => r.checked);
  function dims() {
    const r = selected();
    return r.dataset.custom ? {w: +wIn.value || 0, h: +hIn.value || 0} : {w: +r.dataset.w, h: +r.dataset.h};
  }
  function gcd(a, b) { return b ? gcd(b, a % b) : a; }
  function refreshFormat() {
    const r = selected(), {w, h} = dims(), custom = !!r.dataset.custom;
    customPanel.hidden = !custom;
    const g = gcd(w, h) || 1;
    const ratio = custom ? (w && h ? (w / g) + ':' + (h / g) : '—') : r.dataset.ratio;
    const orient = !w || !h ? '—' : w === h ? 'Square' : h > w ? 'Vertical' : 'Landscape';
    $('specRatio').textContent = ratio;
    $('specRes').textContent = w && h ? w + ' × ' + h : '—';
    $('specOrient').textContent = custom ? orient : r.dataset.orientation;
    $('specRec').textContent = r.dataset.rec ? 'Under ' + r.dataset.rec + ' sec' : 'Your choice';
    const box = $('ratioBox'), max = 64;
    if (w && h) { const s = max / Math.max(w, h); box.style.width = Math.max(14, w * s) + 'px'; box.style.height = Math.max(14, h * s) + 'px'; }
    $('sumPlatform').textContent = r.dataset.label;
    $('sumFormat').textContent = ratio + (w && h ? ' · ' + w + '×' + h : '');
  }
  radios.forEach(r => r.addEventListener('change', refreshFormat));
  [wIn, hIn].forEach(i => i.addEventListener('input', refreshFormat));
  form.querySelectorAll('[data-preset]').forEach(b => b.addEventListener('click', () => { wIn.value = b.dataset.w; hIn.value = b.dataset.h; refreshFormat(); }));

  /* ------------------------------ media (videos, images, audio) ------------------------------ */
  const fileInput = $('fileInput'), mediaList = $('mediaList'), dz = $('dropzone');
  const fmtSize = b => b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB';
  const fmtTime = t => !isFinite(t) || t <= 0 ? '' : Math.floor(t / 60) + ':' + String(Math.round(t % 60)).padStart(2, '0');
  const extOf = n => (n.split('.').pop() || '').toLowerCase();
  const KIND_LABEL = {video: 'Video', image: 'Image', audio: 'Audio'};
  const KIND_ICON = {video: 'fa-film', image: 'fa-image', audio: 'fa-music'};

  function kindOf(name) {
    const e = extOf(name);
    return ['video', 'image', 'audio'].find(k => EXT[k].includes(e)) || null;   // same rule the server uses
  }
  const signature = () => items.map(i => i.key).join('|');
  const isDraftValid = () => !!draft && draft.sig === signature();

  function addFiles(list) {
    const skipped = [];
    const room = MAX_CLIPS - items.length;
    let added = 0;
    [...list].forEach(f => {
      const kind = kindOf(f.name);
      if (!kind) { skipped.push(f.name + ' (unsupported type)'); return; }
      if (f.size > LIMIT_MB[kind] * 1048576) { skipped.push(f.name + ' (over ' + LIMIT_MB[kind] + ' MB)'); return; }
      if (f.size === 0) { skipped.push(f.name + ' (empty)'); return; }
      if (added >= room) { skipped.push(f.name + ' (limit of ' + MAX_CLIPS + ' files)'); return; }
      items.push(makeItem({kind, name: f.name, size: f.size, file: f, url: URL.createObjectURL(f), remote: false}));
      added++;
    });
    if (skipped.length) toast('Skipped: ' + skipped.slice(0, 3).join(', ') + (skipped.length > 3 ? ' and ' + (skipped.length - 3) + ' more' : ''), 'warning', 7000);
    mediaChanged();
  }

  function makeItem(o) {
    const item = Object.assign({key: o.key || 'n' + (++keySeq), duration: o.duration || null, remote: false, file: null}, o);
    item.li = buildRow(item);
    return item;
  }

  function buildRow(item) {
    let preview;
    if (item.kind === 'image') preview = el('img', {class: 'media-thumb', src: item.url, alt: item.name, loading: 'lazy'});
    else if (item.kind === 'video') preview = el('video', {class: 'media-thumb', src: item.url, controls: '', preload: 'metadata', playsinline: '', muted: ''});
    else preview = el('audio', {class: 'media-audio', src: item.url, controls: '', preload: 'metadata'});
    preview.addEventListener('error', () => { if (item.kind === 'image') preview.replaceWith(el('div', {class: 'media-thumb media-fallback'}, el('i', {class: 'fa-solid ' + KIND_ICON[item.kind]}))); });
    const meta = el('span', {class: 'media-sub'});
    const refreshMeta = () => { meta.textContent = [KIND_LABEL[item.kind], fmtTime(item.duration), fmtSize(item.size)].filter(Boolean).join(' · '); };
    refreshMeta();
    if (item.kind !== 'image') preview.addEventListener('loadedmetadata', () => { item.duration = preview.duration; refreshMeta(); });

    const handle = el('span', {class: 'drag-handle', title: 'Drag to reorder', 'aria-hidden': 'true'}, el('i', {class: 'fa-solid fa-grip-vertical'}));
    const li = el('li', {class: 'media-item kind-' + item.kind},
      handle, el('span', {class: 'clip-n', 'data-n': ''}), preview,
      el('span', {class: 'media-info'}, el('span', {class: 'clip-name', text: item.name, title: item.name}), meta),
      el('span', {class: 'media-actions'}));
    const act = li.querySelector('.media-actions');
    const btn = (icon, label, fn) => { const b = el('button', {class: 'btn btn-ghost btn-icon', type: 'button', 'aria-label': label}, el('i', {class: icon})); b.onclick = fn; act.append(b); return b; };
    item.up = btn('fa-solid fa-arrow-up', 'Move up', () => move(item, -1));
    item.down = btn('fa-solid fa-arrow-down', 'Move down', () => move(item, 1));
    btn('fa-solid fa-xmark', 'Remove ' + item.name, () => remove(item));

    // drag & drop reordering: only starts from the handle so video/audio controls stay usable
    handle.addEventListener('mousedown', () => { li.draggable = true; });
    handle.addEventListener('touchstart', () => { li.draggable = true; }, {passive: true});
    li.addEventListener('dragstart', e => { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', item.key); li.classList.add('dragging'); });
    li.addEventListener('dragend', () => { li.draggable = false; li.classList.remove('dragging'); document.querySelectorAll('.media-item.drop-target').forEach(n => n.classList.remove('drop-target')); });
    li.addEventListener('dragover', e => { if (mediaList.querySelector('.dragging')) { e.preventDefault(); li.classList.add('drop-target'); } });
    li.addEventListener('dragleave', () => li.classList.remove('drop-target'));
    li.addEventListener('drop', e => {
      const from = items.findIndex(i => i.key === e.dataTransfer.getData('text/plain'));
      const to = items.indexOf(item);
      if (from < 0 || from === to) return;
      e.preventDefault(); e.stopPropagation();
      items.splice(to, 0, items.splice(from, 1)[0]);
      mediaChanged();
    });
    return li;
  }

  function move(item, d) {
    const i = items.indexOf(item), j = i + d;
    if (j < 0 || j >= items.length) return;
    [items[i], items[j]] = [items[j], items[i]];
    mediaChanged();
  }
  function remove(item) {
    items.splice(items.indexOf(item), 1);
    if (item.url && !item.remote) URL.revokeObjectURL(item.url);
    mediaChanged();
  }

  function mediaChanged() {
    mediaList.replaceChildren(...items.map(i => i.li));
    items.forEach((it, idx) => {
      it.li.querySelector('.clip-n').textContent = idx + 1;
      it.up.disabled = idx === 0; it.down.disabled = idx === items.length - 1;
    });
    const count = k => items.filter(i => i.kind === k).length;
    const parts = ['video', 'image', 'audio'].filter(k => count(k)).map(k => count(k) + ' ' + k + (count(k) > 1 ? 's' : ''));
    $('sumClips').textContent = parts.length ? parts.join(' · ') : '0';
    $('imageOptions').hidden = !count('image');
    $('useAudioRow').hidden = !count('audio');
    if (items.length) setError('files', '');
    if (draft && !isDraftValid()) {
      // order/files changed since the AI saw them: keep the script text, but a fresh analysis is needed
      $('lengthHint').hidden = true;
      if (!aiBusy) showNote('Your media changed since the narration was written. Generate again to update it, or keep editing the text.');
    }
    refreshScriptUi();
  }

  function showNote(msg) {
    const box = $('aiNotes'); box.replaceChildren(el('li', {text: msg})); box.hidden = false;
  }

  fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
  ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => { if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) { e.preventDefault(); dz.classList.add('dragover'); } }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('dragover'); }));
  dz.addEventListener('drop', e => { if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files); });
  // dropping files anywhere else on the page must not navigate away from the form
  ['dragover', 'drop'].forEach(ev => window.addEventListener(ev, e => { if (e.dataTransfer && [...e.dataTransfer.types].includes('Files') && !dz.contains(e.target)) e.preventDefault(); }));

  // Files restored from the server (after "Adjust narration") are fetched back when a new upload is needed.
  async function materialize() {
    for (const it of items) {
      if (it.file) continue;
      const res = await fetch(it.url, {credentials: 'same-origin'});
      if (!res.ok) throw new Error('media_expired');
      const blob = await res.blob();
      it.file = new File([blob], it.name, {type: blob.type});
    }
  }

  /* ------------------------------ narration script editor ------------------------------ */
  const textEl = $('text');
  const wordsIn = t => (t.trim().match(/\S+/g) || []).length;

  function settings() {
    const dur = $('aiDuration').value === 'custom' ? +$('aiDurCustom').value : +$('aiDuration').value;
    return {
      duration_seconds: dur, language: $('aiLanguage').value, tone: $('aiTone').value, tone_custom: $('aiToneCustom').value.trim(),
      style: $('aiStyle').value, audience: $('aiAudience').value, audience_custom: $('aiAudienceCustom').value.trim(),
      speed: $('aiSpeed').value, instructions: $('aiInstructions').value.trim()};
  }
  function estimateSeconds(words) {   // mirrors catalog.estimate_seconds on the server
    const E = CFG.estimate || {speeds: {normal: [130, 160]}, languages: {}, tones: {}};
    const s = settings(), sp = E.speeds[s.speed] || E.speeds.normal;
    const f = (E.languages[s.language] || 1) * (E.tones[s.tone] || 1);
    return words / (((sp[0] + sp[1]) / 2) * f) * 60;
  }

  function setBadge(kind) {
    const b = $('scriptBadge');
    const map = {ai: ['AI generated', 'badge-ai'], edited: ['Edited', 'badge-edit'], approved: ['Approved \u2713', 'badge-ok']};
    if (!kind) { b.hidden = true; return; }
    b.hidden = false; b.textContent = map[kind][0]; b.className = 'script-badge ' + map[kind][1];
  }

  function refreshScriptUi() {
    const text = textEl.value, w = wordsIn(text), est = estimateSeconds(w);
    $('charCount').textContent = text.length;
    $('wordCount').textContent = w;
    $('estDur').textContent = w ? fmtTime(Math.max(1, est)) : '0:00';
    $('regenBtn').hidden = !AI_ON || !draft;
    $('approveBtn').disabled = !text.trim();
    // flag a script that will not come out near the requested length
    const want = settings().duration_seconds, tol = CFG.tolerance || 5, hint = $('lengthHint');
    const off = w >= 5 && draft && isDraftValid() && want && Math.abs(est - want) > tol;
    hint.hidden = !off;
    if (off) {
      $('lengthHintText').textContent = 'Spoken, this will last about ' + Math.round(est) + ' seconds, not ' + want + '.';
      $('adjustBtn').textContent = 'Adjust narration to approximately ' + want + ' seconds';
    }
  }

  textEl.addEventListener('input', () => {
    if (textEl.value.trim()) setError('text', '');
    if (approved || draft) { approved = false; setBadge(draft ? 'edited' : null); }
    refreshScriptUi();      // editing never triggers a regeneration
  });
  $('editBtn').addEventListener('click', () => { textEl.focus(); textEl.setSelectionRange(textEl.value.length, textEl.value.length); });
  $('clearBtn').addEventListener('click', () => { textEl.value = ''; approved = false; setBadge(null); $('aiNotes').hidden = true; refreshScriptUi(); textEl.focus(); });
  $('copyBtn').addEventListener('click', async () => {
    if (!textEl.value) return;
    try { await navigator.clipboard.writeText(textEl.value); toast('Narration copied.', 'success', 2200); }
    catch (e) { textEl.select(); document.execCommand && document.execCommand('copy'); toast('Narration copied.', 'success', 2200); }
  });
  $('approveBtn').addEventListener('click', () => {
    if (!textEl.value.trim()) { setError('text', 'Write or generate a narration first.'); return; }
    approved = true; setBadge('approved');
    toast('Narration approved. Pick a voice, then generate your reel.', 'success');
    $('voiceBlock').scrollIntoView({behavior: 'smooth', block: 'start'});
  });

  // AI settings UI
  $('aiDuration').addEventListener('change', () => { $('aiDurCustomWrap').hidden = $('aiDuration').value !== 'custom'; refreshScriptUi(); });
  ['aiDurCustom', 'aiSpeed', 'aiLanguage', 'aiTone'].forEach(id => $(id).addEventListener('input', refreshScriptUi));
  $('aiAudience').addEventListener('change', () => { $('aiAudienceCustom').hidden = $('aiAudience').value !== 'custom'; });
  [['musicVol', 'musicVolOut'], ['origVol', 'origVolOut']].forEach(([a, o]) => $(a).addEventListener('input', () => { $(o).textContent = $(a).value + '%'; }));

  /* ------------------------------ AI narration job ------------------------------ */
  const AI_STAGES = ['analyzing', 'understanding', 'writing', 'ready'];
  function setAiBusy(b) {
    aiBusy = b; $('generateBtn').disabled = b; $('regenBtn').disabled = b; $('adjustBtn').disabled = b;
  }
  function aiError(msg) { $('aiErrorMsg').textContent = msg; $('aiError').hidden = !msg; }
  function paintAi(stage, uploadPct) {
    const wrap = $('aiProgress'); wrap.hidden = false;
    const bar = $('aiBar'), fill = bar.firstElementChild, note = $('aiUploadNote');
    const stages = document.querySelectorAll('#aiStages li');
    if (uploadPct != null) {
      bar.classList.remove('indeterminate'); fill.style.width = uploadPct + '%';
      note.textContent = 'Uploading your media\u2026 ' + uploadPct + '%';
      stages.forEach(li => li.className = '');
      return;
    }
    const idx = Math.max(AI_STAGES.indexOf(stage), 0);
    bar.classList.toggle('indeterminate', stage !== 'ready'); fill.style.width = stage === 'ready' ? '100%' : '';
    note.textContent = '';
    stages.forEach((li, i) => { li.className = stage === 'ready' || i < idx ? 'done' : i === idx ? 'active' : ''; });
  }

  function validateAiInputs() {
    const s = settings();
    if (!items.length) { setError('files', 'Add at least one video, image or audio file first.'); $('mediaList').scrollIntoView({behavior: 'smooth', block: 'center'}); return null; }
    if (!(s.duration_seconds >= CFG.minSeconds && s.duration_seconds <= CFG.maxSeconds)) { toast('Narration length must be between ' + CFG.minSeconds + ' and ' + CFG.maxSeconds + ' seconds.', 'error'); return null; }
    if (s.audience === 'custom' && !s.audience_custom) { toast('Describe your target audience.', 'error'); return null; }
    return s;
  }

  async function generateNarration() {
    if (aiBusy || uploading) return;
    if (!AI_ON) { toast("AI narration isn't set up on this server yet.", 'warning'); return; }
    if (form.dataset.locked === '1') { showLimitModal(); return; }
    const s = validateAiInputs(); if (!s) return;
    aiError('');
    if (isDraftValid()) return regenerate(s);          // same media: no need to upload again
    try { await materialize(); } catch (e) { aiError('Your earlier upload has expired. Please add your files again.'); return; }

    const fd = new FormData();
    fd.append('csrf_token', form.elements.csrf_token.value);
    Object.entries(s).forEach(([k, v]) => fd.append(k, v));
    items.forEach(i => fd.append('files', i.file, i.name));
    setAiBusy(true); paintAi(null, 0);
    const sig = signature();
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/narration/generate');
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.setRequestHeader('X-CSRFToken', document.querySelector('meta[name="csrf-token"]').content);
    xhr.upload.onprogress = ev => { if (ev.lengthComputable) paintAi(null, Math.round(ev.loaded / ev.total * 100)); };
    xhr.upload.onload = () => paintAi('analyzing');
    xhr.onload = () => {
      let d = {}; try { d = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
      if (xhr.status === 201 && d.draft_id) { draft = {id: d.draft_id, sig}; pollDraft(d.draft_id); return; }
      aiFailed(xhr.status, d);
    };
    xhr.onerror = xhr.ontimeout = () => { setAiBusy(false); $('aiProgress').hidden = true; aiError('Upload failed. Check your connection and try again.'); };
    xhr.send(fd);
  }

  function aiFailed(status, d) {
    setAiBusy(false); $('aiProgress').hidden = true;
    if (status === 401 && d.error === 'free_limit_reached') { form.dataset.locked = '1'; showLimitModal(d); return; }
    if (d.fields && d.fields.files) setError('files', d.fields.files);
    aiError(d.message || (status === 413 ? 'Those files are too big. Try fewer or smaller files.' : 'AI narration is temporarily unavailable. Please try again.'));
  }

  async function regenerate(s, extra) {
    setAiBusy(true); paintAi('analyzing'); aiError('');
    try {
      await api('/api/narration/' + draft.id + '/regenerate', {method: 'POST', json: Object.assign({}, s, extra || {})});
      pollDraft(draft.id);
    } catch (e) {
      setAiBusy(false); $('aiProgress').hidden = true;
      if (e.status === 404 || e.status === 410) { draft = null; aiError('Your uploaded files have expired. Please generate again.'); refreshScriptUi(); }
      else aiError(e.message);
    }
  }

  function pollDraft(id) {
    clearTimeout(aiTimer);
    let failures = 0;
    const tick = async () => {
      try {
        const d = await api('/api/narration/' + encodeURIComponent(id) + '/status');
        failures = 0;
        if (d.status === 'ready') return aiReady(d);
        if (d.status === 'failed') { setAiBusy(false); $('aiProgress').hidden = true; aiError(d.error_message || 'AI narration is temporarily unavailable. Please try again.'); refreshScriptUi(); return; }
        paintAi(d.stage);
      } catch (e) {
        if (e.status === 404) { setAiBusy(false); draft = null; $('aiProgress').hidden = true; aiError('That narration could not be found. Please generate it again.'); return; }
        if (++failures === 4) toast('Having trouble reaching the server. Still trying\u2026', 'warning');
      }
      aiTimer = setTimeout(tick, 1500);
    };
    tick();
  }

  function aiReady(d) {
    paintAi('ready');
    setTimeout(() => { $('aiProgress').hidden = true; }, 900);
    setAiBusy(false);
    textEl.value = d.script || '';
    approved = false; setBadge('ai');
    const box = $('aiNotes');
    if (d.notes && d.notes.length) { box.replaceChildren(...d.notes.map(n => el('li', {text: n}))); box.hidden = false; } else box.hidden = true;
    refreshScriptUi();
    toast('Narration ready. Edit it, regenerate, or approve it.', 'success');
    textEl.scrollIntoView({behavior: 'smooth', block: 'center'});
  }

  $('generateBtn').addEventListener('click', generateNarration);
  $('regenBtn').addEventListener('click', () => {
    if (aiBusy) return;
    const s = validateAiInputs(); if (!s) return;
    if (isDraftValid()) regenerate(s); else generateNarration();
  });
  $('adjustBtn').addEventListener('click', () => {
    if (aiBusy || !draft) return;
    const s = validateAiInputs(); if (!s) return;
    regenerate(s, {mode: 'adjust', script: textEl.value, measured_seconds: Math.round(estimateSeconds(wordsIn(textEl.value)))});
  });

  // Restore a server-side draft into the form (used by "Adjust narration" on a finished reel).
  async function restoreFromDraft(id) {
    const d = await api('/api/narration/' + encodeURIComponent(id) + '/status');
    items.forEach(i => { if (i.url && !i.remote) URL.revokeObjectURL(i.url); });
    items = d.media.map(m => makeItem({key: m.id, kind: m.kind, name: m.name, size: m.size, duration: m.duration, url: m.preview_url, remote: true}));
    const st = d.settings || {};
    const presets = [...$('aiDuration').options].map(o => o.value);
    if (st.duration_seconds) {
      if (presets.includes(String(st.duration_seconds))) $('aiDuration').value = String(st.duration_seconds);
      else { $('aiDuration').value = 'custom'; $('aiDurCustom').value = st.duration_seconds; }
      $('aiDurCustomWrap').hidden = $('aiDuration').value !== 'custom';
    }
    [['aiLanguage', 'language'], ['aiTone', 'tone'], ['aiStyle', 'style'], ['aiSpeed', 'speed'], ['aiAudience', 'audience']].forEach(([id2, k]) => { if (st[k]) $(id2).value = st[k]; });
    $('aiToneCustom').value = st.tone_custom || ''; $('aiInstructions').value = st.instructions || '';
    $('aiAudienceCustom').value = st.audience_custom || ''; $('aiAudienceCustom').hidden = $('aiAudience').value !== 'custom';
    if (!textEl.value.trim()) textEl.value = d.script || '';
    draft = {id: d.draft_id, sig: items.map(i => i.key).join('|')};
    mediaChanged(); setBadge('edited');
    return d;
  }

  /* ------------------------------ errors ------------------------------ */
  function setError(field, msg) {
    const box = form.querySelector('[data-error-for="' + field + '"]');
    if (box) box.textContent = msg || '';
    const input = field === 'text' ? textEl : field === 'files' ? dz : null;
    if (input) input.classList.toggle('is-invalid', !!msg);
  }
  function clearErrors() { ['platform', 'files', 'text', 'voice'].forEach(f => setError(f, '')); }
  function showFieldErrors(fields) { Object.entries(fields || {}).forEach(([k, v]) => setError(k, v)); }

  function validate() {
    clearErrors();
    let ok = true;
    if (!items.some(i => i.kind === 'video' || i.kind === 'image')) { setError('files', 'Add at least one video or image.'); ok = false; }
    if (!textEl.value.trim()) { setError('text', 'Write the narration you want spoken over your media, or generate it with AI.'); ok = false; }
    if (selected().dataset.custom) {
      const {w, h} = dims(), lo = +wIn.min, hi = +wIn.max;
      if (w < lo || h < lo || w > hi || h > hi) { setError('platform', 'Custom width and height must be between ' + lo + ' and ' + hi + ' pixels.'); ok = false; }
    }
    if (!ok) { const first = form.querySelector('.field-error:not(:empty)'); if (first) first.scrollIntoView({behavior: 'smooth', block: 'center'}); }
    return ok;
  }

  /* ------------------------------ guest limit modal ------------------------------ */
  function saveDraft() {
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
        title: $('title').value, text: textEl.value, platform: selected().value, width: wIn.value, height: hIn.value, voice: $('voice').value, song: $('song').value, ai: settings()}));
    } catch (e) { /* storage unavailable: fine, this is only a convenience */ }
  }
  function showLimitModal(urls) {
    if (isAuth) { toast("You've reached your reel limit for this month.", 'warning'); return; }
    saveDraft();
    const login = (urls && urls.login_url) || URLS.login, reg = (urls && urls.register_url) || URLS.register;
    App.dialog((body, d) => {
      const later = el('button', {class: 'btn btn-ghost', type: 'button', text: 'Not now'}); later.onclick = () => d.close();
      body.append(
        el('h2', {text: "You've created your free reel 🎉"}),
        el('p', {text: 'Want to create more? Create an account or sign in to continue generating reels.'}),
        el('p', {class: 'muted', text: "We'll keep your script and settings, and your free reel will be added to your gallery."}),
        el('div', {class: 'modal-actions'}, later, el('a', {class: 'btn btn-ghost', href: login, text: 'Sign In'}), el('a', {class: 'btn btn-accent', href: reg, text: 'Create Account'})));
    });
  }
  function restoreDraft() {
    if (!isAuth) return;
    let d; try { d = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || 'null'); sessionStorage.removeItem(DRAFT_KEY); } catch (e) { return; }
    if (!d) return;
    $('title').value = d.title || ''; textEl.value = d.text || '';
    const r = radios.find(x => x.value === d.platform); if (r) r.checked = true;
    if (d.width) wIn.value = d.width; if (d.height) hIn.value = d.height;
    if (d.voice) $('voice').value = d.voice; if (d.song !== undefined) $('song').value = d.song;
    if (d.ai) { const a = d.ai; if (a.language) $('aiLanguage').value = a.language; if (a.tone) $('aiTone').value = a.tone; if (a.style) $('aiStyle').value = a.style; if (a.speed) $('aiSpeed').value = a.speed; $('aiInstructions').value = a.instructions || ''; }
    refreshFormat(); refreshScriptUi();
    toast('Welcome! We kept your script and settings. Add your media again to continue.', 'success', 7000);
  }

  /* ------------------------------ submit / upload ------------------------------ */
  const submitBtn = $('submitBtn');
  form.addEventListener('submit', e => {
    e.preventDefault();
    if (uploading) return;
    if (form.dataset.locked === '1') { showLimitModal(); return; }
    if (aiBusy) { toast('Hold on - your narration is still being created.', 'warning'); return; }
    if (!validate()) return;
    if (isDraftValid()) { upload(); return; }
    materialize().then(upload).catch(() => { setError('files', 'Your earlier upload has expired. Please add your files again.'); });
  });

  function upload() {
    const fd = new FormData();
    ['csrf_token', 'title', 'text', 'voice', 'song', 'music_volume', 'original_volume'].forEach(n => fd.append(n, form.elements[n].value));
    fd.append('image_seconds', $('imageSeconds').value); fd.append('image_fit', $('imageFit').value);
    fd.append('use_audio', $('useAudio').checked ? '1' : '0');
    const r = selected(); fd.append('platform', r.value);
    if (r.dataset.custom) { fd.append('width', wIn.value); fd.append('height', hIn.value); }
    if (isDraftValid()) fd.append('draft_id', draft.id);          // media is already on the server: nothing to upload
    else items.forEach(i => fd.append('files', i.file, i.name));

    uploading = true; submitBtn.disabled = true;
    resetProgress(); show('progress');
    const bar = $('uploadBar'), fill = bar.firstElementChild;
    const guard = ev => { ev.preventDefault(); ev.returnValue = ''; };
    window.addEventListener('beforeunload', guard);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', URLS.create);
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.setRequestHeader('X-CSRFToken', document.querySelector('meta[name="csrf-token"]').content);
    xhr.upload.onprogress = ev => { if (ev.lengthComputable) fill.style.width = Math.round(ev.loaded / ev.total * 100) + '%'; };
    xhr.upload.onload = () => { bar.classList.add('indeterminate'); $('progressTitle').textContent = 'Creating your reel'; $('uploadNote').textContent = 'Upload complete. This page updates as each step finishes.'; };
    const finish = () => { uploading = false; submitBtn.disabled = false; window.removeEventListener('beforeunload', guard); };
    const back = () => { finish(); show('form'); };

    xhr.onload = () => {
      let data = {}; try { data = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
      finish();
      if (xhr.status === 201 && data.reel_id) {
        history.replaceState(null, '', URLS.create + '?reel=' + encodeURIComponent(data.reel_id));
        poll(data.reel_id);
      } else if (xhr.status === 401 && data.error === 'free_limit_reached') {
        form.dataset.locked = '1'; show('form'); showLimitModal(data);
      } else if (xhr.status === 400 && data.fields) {
        show('form'); showFieldErrors(data.fields); toast(data.message, 'error');
      } else {
        show('form');
        toast(data.message || (xhr.status === 413 ? 'Those files are too big. Try fewer or smaller files.' : "We couldn't start your reel right now. Please try again."), 'error');
      }
    };
    xhr.onerror = () => { back(); toast("Upload failed - check your connection and try again.", 'error'); };
    xhr.ontimeout = xhr.onerror;
    xhr.send(fd);
  }

  /* ------------------------------ real progress ------------------------------ */
  function resetProgress() {
    $('uploadBar').classList.remove('indeterminate'); $('uploadBar').firstElementChild.style.width = '0';
    $('progressTitle').textContent = 'Uploading your media'; $('uploadNote').textContent = 'Keep this tab open until the upload finishes.';
    $('progressFail').hidden = true; $('uploadBar').hidden = false;
    document.querySelectorAll('#stageList li').forEach(li => li.className = '');
  }
  function paintStage(stage) {
    const idx = Math.max(STAGES.indexOf(stage), 0);   // "queued" -> first step is active
    document.querySelectorAll('#stageList li').forEach((li, i) => { li.className = i < idx ? 'done' : i === idx ? 'active' : ''; });
  }
  function poll(reelId) {
    clearTimeout(pollTimer);
    resetProgress(); show('progress');
    $('progressTitle').textContent = 'Creating your reel';
    $('uploadNote').textContent = 'This page updates as each step finishes. You can safely refresh.';
    $('uploadBar').classList.add('indeterminate');
    let failures = 0;
    const tick = async () => {
      try {
        const d = await api('/api/reel/' + encodeURIComponent(reelId) + '/status');
        failures = 0;
        if (d.status === 'done') return showResult(d);
        if (d.status === 'failed') return showFailure(d.error_message);
        paintStage(d.stage);
      } catch (e) {
        if (e.status === 404) { history.replaceState(null, '', URLS.create); toast("We couldn't find that reel.", 'error'); return show('form'); }
        if (++failures === 4) toast('Having trouble reaching the server. Still trying…', 'warning');
      }
      pollTimer = setTimeout(tick, 2500);
    };
    tick();
  }
  function showFailure(msg) {
    $('uploadBar').hidden = true;
    $('progressTitle').textContent = "Your reel didn't finish";
    $('progressFailMsg').textContent = msg || "We couldn't generate your reel right now. Please try again.";
    $('progressFail').hidden = false;
    history.replaceState(null, '', URLS.create);
  }
  $('tryAgainBtn').addEventListener('click', () => show('form'));

  function showResult(d) {
    $('resultTitle').textContent = d.title;
    $('resPlatform').textContent = d.platform_label;
    $('resFormat').textContent = d.aspect_ratio;
    $('resRes').textContent = d.resolution || '—';
    $('resDur').textContent = d.duration_label || '—';
    const v = $('resultVideo'); v.src = d.video_url; if (d.thumbnail_url) v.poster = d.thumbnail_url;
    if (d.width && d.height) v.style.aspectRatio = d.width + ' / ' + d.height;
    $('resDownload').href = d.download_url;
    $('resGallery').hidden = !isAuth;
    $('guestSave').hidden = isAuth;
    $('resRegister').hidden = isAuth; $('resRegister').href = URLS.registerSave;
    const w = d.duration_warning;
    $('resultWarn').hidden = !w;
    $('resultAdjustBtn').hidden = !(w && w.draft_id && AI_ON);
    if (w) { $('resultWarnMsg').textContent = w.message; $('resultAdjustBtn').textContent = 'Adjust narration to about ' + w.requested_seconds + ' seconds'; $('resultAdjustBtn').onclick = () => adjustFromResult(w); }
    show('result');
  }
  function clearItems() { items.forEach(i => { if (i.url && !i.remote) URL.revokeObjectURL(i.url); }); items = []; mediaChanged(); }
  function discardDraft() {
    if (!draft) return;
    const id = draft.id; draft = null;
    api('/api/narration/' + encodeURIComponent(id), {method: 'DELETE'}).catch(() => { /* best effort: it expires on its own */ });
  }
  async function adjustFromResult(w) {
    try {
      await restoreFromDraft(w.draft_id);
      history.replaceState(null, '', URLS.create);
      show('form');
      const s = settings(); if (!validateAiInputs()) return;
      regenerate(s, {mode: 'adjust', script: textEl.value, measured_seconds: w.actual_seconds});
    } catch (e) { toast('Those files have expired, so the narration can no longer be adjusted. Create the reel again.', 'warning', 7000); }
  }
  $('createAnotherBtn').addEventListener('click', () => {
    if (!isAuth) { window.location.href = URLS.create; return; }   // re-render with fresh trial state
    $('resultVideo').pause(); history.replaceState(null, '', URLS.create);
    discardDraft(); clearItems(); $('title').value = ''; textEl.value = ''; approved = false; setBadge(null); $('aiNotes').hidden = true; $('lengthHint').hidden = true;
    refreshScriptUi(); clearErrors();
    show('form');
  });

  /* ------------------------------ init ------------------------------ */
  refreshFormat();
  restoreDraft();
  mediaChanged();
  const resume = new URLSearchParams(location.search).get('reel');
  if (resume && /^[0-9a-f-]{36}$/i.test(resume)) poll(resume);
})();
