/* Reel cards, preview dialog and the list controller used by Gallery + Dashboard. */
(function () {
  const {el, api, toast} = App;
  const STATUS = {done: ['Ready', 'badge-ok'], failed: ['Failed', 'badge-danger'], pending: ['Queued', 'badge-warn'], processing: ['Rendering', 'badge-warn']};

  function icon(cls) { return el('i', {class: cls}); }

  function card(item, {onDelete}) {
    const ready = item.status === 'done' && item.video_url;
    const thumb = el('button', {class: 'reel-thumb', type: 'button', 'aria-label': ready ? 'Preview ' + item.title : item.title});
    if (item.thumbnail_url) {
      const img = el('img', {src: item.thumbnail_url, alt: '', loading: 'lazy'});
      img.onerror = () => { img.remove(); thumb.prepend(el('div', {class: 'thumb-empty'}, icon('fa-solid fa-film'))); };
      thumb.append(img);
    } else thumb.append(el('div', {class: 'thumb-empty'}, icon('fa-solid fa-film')));
    thumb.append(el('span', {class: 'thumb-badge', text: item.platform_short}));
    if (ready) {
      if (item.duration_label) thumb.append(el('span', {class: 'thumb-duration', text: item.duration_label}));
      thumb.append(el('span', {class: 'thumb-play'}, icon('fa-solid fa-circle-play')));
      thumb.onclick = () => preview(item);
    } else if (item.status === 'failed') {
      thumb.append(el('div', {class: 'thumb-state'}, icon('fa-solid fa-triangle-exclamation'), el('span', {text: item.error_message || 'This reel failed to generate.'})));
      thumb.disabled = true;
    } else {
      thumb.append(el('div', {class: 'thumb-state'}, icon('fa-solid fa-spinner fa-spin'), el('span', {text: 'Still rendering…'})));
      thumb.onclick = () => { window.location.href = window.REEL_URLS.create + '?reel=' + encodeURIComponent(item.id); };
    }

    const [statusText, statusCls] = STATUS[item.status] || STATUS.pending;
    const time = el('time', {datetime: item.created_at, 'data-relative': ''});
    const meta = el('p', {class: 'reel-meta'},
      el('span', {text: item.aspect_ratio || ''}), el('span', {text: item.resolution || ''}),
      ...(item.duration_label ? [el('span', {text: item.duration_label})] : []));
    const footer = el('div', {class: 'reel-footer'}, el('span', {}, 'Created ', time), el('span', {class: 'badge ' + statusCls, text: statusText}));

    const actions = el('div', {class: 'reel-actions'});
    const pv = el('button', {class: 'btn btn-ghost btn-preview', type: 'button'}, icon('fa-solid fa-play'), ' Preview');
    pv.disabled = !ready; pv.onclick = () => preview(item);
    actions.append(pv);
    if (ready) {
      const dl = el('a', {class: 'btn btn-ghost btn-icon', href: item.download_url, 'aria-label': 'Download ' + item.title, title: 'Download'}, icon('fa-solid fa-download'));
      actions.append(dl);
    }
    const rm = el('button', {class: 'btn btn-ghost btn-icon', type: 'button', 'aria-label': 'Delete ' + item.title, title: 'Delete'}, icon('fa-regular fa-trash-can'));
    rm.onclick = () => onDelete(item);
    actions.append(rm);

    const title = el('h3', {class: 'reel-title', text: item.title, title: item.title});
    const platform = el('p', {class: 'reel-meta'}, el('span', {text: item.platform_label}));
    const article = el('article', {class: 'reel-card', 'data-id': item.id}, thumb, el('div', {class: 'reel-body'}, title, platform, meta, footer, actions));
    return article;
  }

  function preview(item) {
    const dlg = document.createElement('dialog');
    dlg.className = 'modal preview';
    const video = el('video', {controls: '', autoplay: '', playsinline: '', src: item.video_url});
    if (item.thumbnail_url) video.setAttribute('poster', item.thumbnail_url);
    if (item.width && item.height) video.style.aspectRatio = item.width + ' / ' + item.height;
    video.onerror = () => toast("We couldn't load this video. The file may be missing.", 'error');
    const close = el('button', {class: 'btn btn-ghost btn-icon modal-close', type: 'button', 'aria-label': 'Close'}, icon('fa-solid fa-xmark'));
    close.onclick = () => dlg.close();
    const info = el('div', {class: 'preview-info'},
      el('h2', {text: item.title}),
      el('p', {class: 'muted', style: 'margin:0 0 .9rem', text: [item.platform_label, item.aspect_ratio, item.resolution, item.duration_label].filter(Boolean).join(' · ')}),
      el('div', {class: 'result-actions', style: 'margin:0'},
        el('a', {class: 'btn btn-primary', href: item.download_url}, icon('fa-solid fa-download'), ' Download'),
        el('a', {class: 'btn btn-ghost', href: item.detail_url}, 'Open details')));
    dlg.append(close, el('div', {class: 'preview-stage'}, video), info);
    dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener('close', () => { video.pause(); video.removeAttribute('src'); video.load(); dlg.remove(); });
    document.body.appendChild(dlg);
    dlg.showModal();
  }

  /* ---- list controller ---- */
  function mount({root, perPage = 12, compact = false, filters = () => ({}), loadMoreBtn = null, loadMoreWrap = null}) {
    let page = 1, items = [], pollTimer = null, gen = 0;

    function skeletons() {
      root.replaceChildren(el('div', {class: 'reel-grid'}, ...Array.from({length: compact ? 4 : 8}, () =>
        el('div', {class: 'skeleton'}, el('div', {class: 'sk-thumb'}, ''), el('div', {class: 'sk-line'}), el('div', {class: 'sk-line short'})))));
    }
    function stateBox(iconCls, title, text, button) {
      const box = el('div', {class: 'state-box'}, el('div', {class: 'state-icon'}, icon(iconCls)), el('h2', {text: title}), el('p', {class: 'muted', text: text}));
      if (button) box.append(button);
      return box;
    }
    function render(total) {
      clearTimeout(pollTimer);
      if (loadMoreWrap) loadMoreWrap.hidden = !(items.length < total);
      if (!items.length) {
        const f = filters(); const filtered = !!(f.q || (f.platform && f.platform !== 'all'));
        if (filtered) {
          const clear = el('button', {class: 'btn btn-ghost', type: 'button', text: 'Clear filters'});
          clear.onclick = () => document.dispatchEvent(new CustomEvent('reels:clear-filters'));
          root.replaceChildren(stateBox('fa-solid fa-magnifying-glass', 'No reels match', 'Try a different search or platform.', clear));
        } else {
          root.replaceChildren(stateBox('fa-regular fa-clapperboard', compact ? 'No reels yet.' : 'Your gallery is empty.',
            compact ? 'Your generated reels will appear here.' : 'Create your first reel and it will appear here.',
            el('a', {class: 'btn btn-accent btn-lg', href: window.REEL_URLS.create, text: 'Create Your First Reel'})));
        }
        return;
      }
      const grid = el('div', {class: 'reel-grid'}, ...items.map(it => card(it, {onDelete: remove})));
      root.replaceChildren(grid);
      App.hydrateTimes(root);
      if (items.some(i => i.status === 'pending' || i.status === 'processing')) pollTimer = setTimeout(() => load(true), 6000);
    }
    async function load(reset, {silent = false} = {}) {
      const my = ++gen;
      if (reset) { page = 1; if (!silent) skeletons(); }
      const f = filters();
      const params = new URLSearchParams({per_page: perPage, page, sort: f.sort || 'newest'});
      if (f.q) params.set('q', f.q);
      if (f.platform && f.platform !== 'all') params.set('platform', f.platform);
      try {
        const data = await api('/api/reels?' + params);
        if (my !== gen) return;
        items = reset ? data.items : items.concat(data.items);
        render(data.total);
      } catch (e) {
        if (my !== gen) return;
        const retry = el('button', {class: 'btn btn-primary', type: 'button', text: 'Try again'});
        retry.onclick = () => load(true);
        root.replaceChildren(stateBox('fa-solid fa-triangle-exclamation', "We couldn't load your reels", e.message, retry));
        if (loadMoreWrap) loadMoreWrap.hidden = true;
      }
    }
    async function remove(item) {
      const ok = await App.confirm({title: 'Delete this reel?', message: '"' + item.title + '" will be permanently removed. This can\'t be undone.', confirmLabel: 'Delete', danger: true});
      if (!ok) return;
      try {
        await api('/api/reel/' + encodeURIComponent(item.id), {method: 'DELETE'});
        toast('Reel deleted', 'success');
        load(true, {silent: true});
      } catch (e) { toast(e.message, 'error'); }
    }
    if (loadMoreBtn) loadMoreBtn.onclick = async () => { loadMoreBtn.disabled = true; page += 1; await load(false); loadMoreBtn.disabled = false; };
    load(true);
    return {reload: () => load(true)};
  }

  window.ReelUI = {card, preview, mount};
})();
