/* Shared helpers: API calls, toasts, dialogs, relative times, nav. */
(function () {
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  class ApiError extends Error {
    constructor(message, status, code, data) { super(message); this.status = status; this.code = code; this.data = data || {}; }
  }

  async function api(url, opts = {}) {
    const headers = {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrf()};
    const init = {method: opts.method || 'GET', headers, credentials: 'same-origin'};
    if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
    let res;
    try { res = await fetch(url, init); }
    catch (e) { throw new ApiError("Can't reach the server. Check your connection and try again.", 0, 'network'); }
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON body */ }
    if (!res.ok) {
      const fallback = res.status >= 500 ? 'Something went wrong on our side. Please try again.' : 'That request could not be completed.';
      throw new ApiError(data.message || fallback, res.status, data.error, data);
    }
    return data;
  }

  /* ---- toasts ---- */
  const ICONS = {success: 'fa-circle-check', error: 'fa-circle-exclamation', warning: 'fa-triangle-exclamation', info: 'fa-circle-info'};
  function toast(message, type = 'info', ms = 4500) {
    const region = document.getElementById('toastRegion');
    const el = document.createElement('div');
    el.className = 'toast toast-' + type;
    const icon = document.createElement('i'); icon.className = 'fa-solid ' + (ICONS[type] || ICONS.info);
    const text = document.createElement('span'); text.textContent = message;
    el.append(icon, text);
    region.appendChild(el);
    setTimeout(() => { el.classList.add('leaving'); setTimeout(() => el.remove(), 220); }, ms);
  }

  /* ---- dialogs ---- */
  function dialog(build) {
    const dlg = document.createElement('dialog');
    dlg.className = 'modal';
    const body = document.createElement('div'); body.className = 'modal-body';
    dlg.appendChild(body);
    build(body, dlg);
    dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener('close', () => setTimeout(() => dlg.remove(), 50));
    document.body.appendChild(dlg);
    dlg.showModal();
    return dlg;
  }
  function el(tag, props = {}, ...kids) {
    const n = document.createElement(tag);
    Object.entries(props).forEach(([k, v]) => { if (k === 'class') n.className = v; else if (k === 'text') n.textContent = v; else n.setAttribute(k, v); });
    kids.forEach(k => n.append(k));
    return n;
  }
  function confirmDialog({title, message, confirmLabel = 'Confirm', danger = false}) {
    return new Promise(resolve => {
      let result = false;
      const dlg = dialog((body, d) => {
        const yes = el('button', {class: 'btn ' + (danger ? 'btn-danger' : 'btn-primary'), type: 'button', text: confirmLabel});
        const no = el('button', {class: 'btn btn-ghost', type: 'button', text: 'Cancel'});
        yes.onclick = () => { result = true; d.close(); };
        no.onclick = () => d.close();
        body.append(el('h2', {text: title}), el('p', {class: 'muted', text: message}), el('div', {class: 'modal-actions'}, no, yes));
      });
      dlg.addEventListener('close', () => resolve(result));
    });
  }
  function promptDialog({title, label, value = '', confirmLabel = 'Save'}) {
    return new Promise(resolve => {
      let result = null;
      const dlg = dialog((body, d) => {
        const input = el('input', {class: 'input', type: 'text', maxlength: '200', id: 'promptInput'}); input.value = value;
        const form = el('form', {});
        const yes = el('button', {class: 'btn btn-primary', type: 'submit', text: confirmLabel});
        const no = el('button', {class: 'btn btn-ghost', type: 'button', text: 'Cancel'});
        no.onclick = () => d.close();
        form.onsubmit = e => { e.preventDefault(); result = input.value; d.close(); };
        form.append(el('label', {for: 'promptInput', text: label}), input, el('div', {class: 'modal-actions'}, no, yes));
        body.append(el('h2', {text: title}), form);
        setTimeout(() => { input.focus(); input.select(); }, 30);
      });
      dlg.addEventListener('close', () => resolve(result));
    });
  }

  /* ---- relative time ---- */
  function relativeTime(iso) {
    const d = new Date(iso); if (isNaN(d)) return '';
    const s = Math.round((Date.now() - d) / 1000);
    if (s < 60) return 'Just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    const startToday = new Date(); startToday.setHours(0, 0, 0, 0);
    if (d >= startToday) return Math.floor(s / 3600) + (Math.floor(s / 3600) === 1 ? ' hour ago' : ' hours ago');
    const startYesterday = new Date(startToday - 864e5);
    if (d >= startYesterday) return 'Yesterday';
    if (s < 7 * 86400) return Math.floor(s / 86400) + ' days ago';
    return d.toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric'});
  }
  function hydrateTimes(root = document) {
    root.querySelectorAll('time[data-relative]').forEach(t => { t.textContent = relativeTime(t.getAttribute('datetime')); t.title = new Date(t.getAttribute('datetime')).toLocaleString(); });
  }

  window.App = {api, ApiError, toast, confirm: confirmDialog, prompt: promptDialog, dialog, el, relativeTime, hydrateTimes};

  document.addEventListener('DOMContentLoaded', () => {
    hydrateTimes();
    const toggle = document.getElementById('navToggle'), links = document.getElementById('navLinks');
    if (toggle) toggle.addEventListener('click', () => { const open = links.classList.toggle('open'); toggle.setAttribute('aria-expanded', open); });
    try {
      JSON.parse(document.getElementById('flash-data').textContent).forEach(([cat, msg]) =>
        toast(msg, {danger: 'error', success: 'success', warning: 'warning', info: 'info'}[cat] || 'info', 6000));
    } catch (e) { /* ignore */ }
  });
})();
