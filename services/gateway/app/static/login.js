(() => {
  const $ = (id) => document.getElementById(id);

  function param(name, url) {
    const u = url || window.location.search;
    const p = new URLSearchParams(u);
    return p.get(name) || "";
  }

  async function doLogin() {
    const username = ($('username').value || '').trim();
    const password = ($('password').value || '').trim();
    const meta = $('meta');
    meta.textContent = '';
    if (!username || !password) {
      meta.textContent = 'username and password required';
      return;
    }
    $('login').disabled = true;
    try {
      const resp = await fetch('/ui/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const text = await resp.text();
      if (!resp.ok) {
        meta.textContent = `Login failed: HTTP ${resp.status} ${text}`;
        return;
      }
      // On success, redirect to 'next' param or UI root
      const next = param('next') || '/ui';
      window.location.href = next;
    } catch (e) {
      meta.textContent = String(e);
    } finally {
      $('login').disabled = false;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('login').addEventListener('click', () => void doLogin());
    const qNext = param('next');
    if (qNext) {
      try { history.replaceState(null, '', '/ui/login'); } catch (e) {}
    }
  });
})();
