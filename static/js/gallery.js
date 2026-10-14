(function () {
  const search = document.getElementById('searchInput');
  const sort = document.getElementById('sortSelect');
  const chips = document.querySelectorAll('#platformChips .chip');
  const state = {q: '', platform: 'all', sort: 'newest'};

  const params = new URLSearchParams(location.search);
  state.q = params.get('q') || ''; state.sort = params.get('sort') === 'oldest' ? 'oldest' : 'newest';
  const p = params.get('platform'); if (p && document.querySelector('[data-platform="' + CSS.escape(p) + '"]')) state.platform = p;
  search.value = state.q; sort.value = state.sort;
  chips.forEach(c => c.setAttribute('aria-pressed', c.dataset.platform === state.platform));

  const ctl = ReelUI.mount({
    root: document.getElementById('galleryRoot'), perPage: 12,
    filters: () => state,
    loadMoreBtn: document.getElementById('loadMoreBtn'), loadMoreWrap: document.getElementById('loadMoreWrap'),
  });

  function sync() {
    const u = new URLSearchParams();
    if (state.q) u.set('q', state.q);
    if (state.platform !== 'all') u.set('platform', state.platform);
    if (state.sort !== 'newest') u.set('sort', state.sort);
    history.replaceState(null, '', u.toString() ? '?' + u : location.pathname);
    ctl.reload();
  }
  let t;
  search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { state.q = search.value.trim(); sync(); }, 250); });
  sort.addEventListener('change', () => { state.sort = sort.value; sync(); });
  chips.forEach(c => c.addEventListener('click', () => {
    state.platform = c.dataset.platform;
    chips.forEach(x => x.setAttribute('aria-pressed', x === c));
    sync();
  }));
  document.addEventListener('reels:clear-filters', () => {
    state.q = ''; state.platform = 'all'; search.value = '';
    chips.forEach(x => x.setAttribute('aria-pressed', x.dataset.platform === 'all'));
    sync();
  });
})();
