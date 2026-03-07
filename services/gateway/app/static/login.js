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
      try {
        if (window.GatewayAuth) window.GatewayAuth.clearApiKey();
      } catch (e) {}
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

  async function useApiKey() {
    const apiKey = ($('apiKey').value || '').trim();
    const meta = $('meta');
    meta.textContent = '';
    if (!apiKey) {
      meta.textContent = 'API key required';
      return;
    }
    $('useApiKey').disabled = true;
    try {
      const result = window.GatewayAuth ? await window.GatewayAuth.validateApiKey(apiKey) : { ok: false, detail: 'Auth client unavailable' };
      if (!result || !result.ok) {
        const detail = result && result.detail ? result.detail : 'API key validation failed';
        meta.textContent = `API key failed: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`;
        return;
      }
      try {
        if (window.GatewayAuth) window.GatewayAuth.setApiKey(apiKey);
      } catch (e) {}
      const next = param('next') || '/ui';
      window.location.href = next;
    } catch (e) {
      meta.textContent = String(e);
    } finally {
      $('useApiKey').disabled = false;
    }
  }

  function clearStoredApiKey() {
    const meta = $('meta');
    try {
      if (window.GatewayAuth) window.GatewayAuth.clearApiKey();
      $('apiKey').value = '';
      meta.textContent = 'Saved API key cleared';
    } catch (e) {
      meta.textContent = String(e);
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('login').addEventListener('click', () => void doLogin());
    $('useApiKey').addEventListener('click', () => void useApiKey());
    $('clearApiKey').addEventListener('click', () => clearStoredApiKey());
    $('password').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        void doLogin();
      }
    });
    $('apiKey').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        void useApiKey();
      }
    });
    try {
      const existing = window.GatewayAuth ? window.GatewayAuth.getApiKey() : '';
      if (existing) {
        $('apiKey').value = existing;
        $('meta').textContent = 'A saved API key is available for this browser.';
      }
    } catch (e) {}
    const qNext = param('next');
    if (qNext) {
      try { history.replaceState(null, '', '/ui/login'); } catch (e) {}
    }
  });
})();
