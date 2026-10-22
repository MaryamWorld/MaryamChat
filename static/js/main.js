/* Shared helpers: API wrapper (adds CSRF token), DOM helpers, toasts, time formatting. */
(function () {
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  async function api(path, options = {}) {
    const opts = { method: 'GET', credentials: 'same-origin', ...options };
    opts.headers = { Accept: 'application/json', ...(options.headers || {}) };
    if (opts.body !== undefined && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
      opts.headers['Content-Type'] = 'application/json';
    }
    if (opts.method !== 'GET') opts.headers['X-CSRF-Token'] = csrf();
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new Error('Network error. Check your connection and try again.');
    }
    if (res.status === 401) {
      window.location.href = '/login';
      throw new Error('Please log in again.');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON body */ }
    if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status}).`);
    return data;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text; // textContent: user content is never parsed as HTML
    return node;
  }

  function toast(message, kind) {
    const box = document.getElementById('toasts');
    const node = el('div', 'toast ' + (kind || ''), message);
    box.appendChild(node);
    setTimeout(() => node.remove(), 5000);
  }

  const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const fmtDay = (iso) => new Date(iso).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  const sameDay = (a, b) => new Date(a).toDateString() === new Date(b).toDateString();
  const initial = (name) => (name || '?').trim().charAt(0).toUpperCase();

  const d = document.body.dataset;
  window.CW = {
    api, el, toast, fmtTime, fmtDay, sameDay, initial,
    me: { id: Number(d.userId), username: d.username, displayName: d.displayName },
  };
})();
