(function () {
  const STORAGE_KEY = 'gateway_api_key';
  const originalFetch = window.fetch.bind(window);

  function emitAuthChanged() {
    try {
      window.dispatchEvent(new CustomEvent('gateway-auth-changed', {
        detail: { hasApiKey: !!getApiKey() },
      }));
    } catch (e) {
      // ignore event dispatch failures
    }
  }

  function getApiKey() {
    try {
      return String(window.localStorage.getItem(STORAGE_KEY) || '').trim();
    } catch (e) {
      return '';
    }
  }

  function setApiKey(token) {
    const value = String(token || '').trim();
    try {
      if (!value) {
        window.localStorage.removeItem(STORAGE_KEY);
      } else {
        window.localStorage.setItem(STORAGE_KEY, value);
      }
    } catch (e) {
      // ignore storage failures
    }
    emitAuthChanged();
  }

  function clearApiKey() {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch (e) {
      // ignore storage failures
    }
    emitAuthChanged();
  }

  function resolveUrl(input) {
    try {
      if (typeof input === 'string' || input instanceof URL) {
        return new URL(String(input), window.location.href);
      }
      if (input && typeof input.url === 'string') {
        return new URL(String(input.url), window.location.href);
      }
    } catch (e) {
      return null;
    }
    return null;
  }

  function shouldAttachApiKey(input) {
    const url = resolveUrl(input);
    if (!url) return false;
    if (url.origin !== window.location.origin) return false;
    return url.pathname.startsWith('/ui/api/');
  }

  function mergeAuthHeaders(sourceHeaders) {
    const headers = new Headers(sourceHeaders || {});
    const token = getApiKey();
    if (!token) return headers;
    if (!headers.has('X-Session-Token') && !headers.has('x-session-token') && !headers.has('Authorization') && !headers.has('authorization')) {
      headers.set('X-Session-Token', token);
    }
    return headers;
  }

  async function authFetch(input, init) {
    if (!shouldAttachApiKey(input)) {
      return originalFetch(input, init);
    }

    if (input instanceof Request) {
      const headers = mergeAuthHeaders(input.headers);
      const wrapped = new Request(input, { headers });
      return originalFetch(wrapped, init);
    }

    const options = Object.assign({}, init || {});
    options.headers = mergeAuthHeaders(options.headers);
    return originalFetch(input, options);
  }

  async function validateApiKey(token) {
    const value = String(token || '').trim();
    if (!value) {
      return { ok: false, detail: 'API key is required' };
    }
    try {
      const resp = await originalFetch('/ui/api/auth/me', {
        method: 'GET',
        credentials: 'same-origin',
        headers: { 'X-Session-Token': value },
      });
      let payload = null;
      try {
        payload = await resp.json();
      } catch (e) {
        payload = null;
      }
      if (!resp.ok) {
        return { ok: false, status: resp.status, detail: payload || `HTTP ${resp.status}` };
      }
      return { ok: !!(payload && payload.authenticated), payload };
    } catch (e) {
      return { ok: false, detail: String(e && e.message ? e.message : e) };
    }
  }

  window.fetch = authFetch;
  window.GatewayAuth = {
    storageKey: STORAGE_KEY,
    getApiKey,
    setApiKey,
    clearApiKey,
    validateApiKey,
  };
  emitAuthChanged();
})();