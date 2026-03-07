(function () {
  const STORAGE_KEY = 'gateway_api_key';
  const originalFetch = window.fetch.bind(window);
  const TITLE_BY_ID = {
    model: 'Choose which chat model to use for the next message.',
    loadModels: 'Refresh the list of available chat models.',
    appsBtn: 'Open the applications menu.',
    clear: 'Clear the chat message editor without sending.',
    clearChat: 'Clear the visible chat transcript from this page.',
    resetSession: 'Start a fresh chat session and clear the current conversation state.',
    settingsBtn: 'Open user settings.',
    send: 'Send the current chat message. Press Enter to send or Shift+Enter for a new line.',
    attachBtn: 'Attach one or more files to the next chat message.',
    apiKeyStatus: 'Shows whether this browser is currently sending a saved personal API key to UI requests.',
    login: 'Sign in with your username and password.',
    useApiKey: 'Use a personal API key instead of password login for this browser.',
    clearApiKey: 'Remove any saved browser API key from this device.',
    generate: 'Generate output using the values currently entered on this page.',
    run: 'Run OCR using the current image URL.',
    openUi: 'Open the PersonaPlex web interface in a new tab.',
    refreshUsers: 'Reload the current user list.',
    applyBulk: 'Apply the selected bulk action to the checked users.',
    settingsClose: 'Close settings.',
    settings_cancel: 'Close settings without saving changes.',
    settings_save: 'Save the current settings changes.',
    settings_create_api_key: 'Create a new personal API key for this account.',
    settings_forget_browser_api_key: 'Remove the saved browser API key from this browser only.',
    backendStatusRefresh: 'Refresh backend health and readiness information.',
    recordAudio: 'Start recording a voice sample from your microphone.',
    stopRecording: 'Stop the current voice-sample recording.',
    clearRecording: 'Discard the current recorded voice sample.',
  };
  const INPUT_TO_ACTION = {
    username: 'login',
    password: 'login',
    apiKey: 'useApiKey',
    imageUrl: 'run',
    settings_api_key_name: 'settings_create_api_key',
    settings_profile_tone: 'settings_save',
    settings_current_password: 'settings_save',
    settings_new_password: 'settings_save',
    settings_confirm_password: 'settings_save',
    bulkPassword: 'applyBulk',
    deleteConfirm: 'applyBulk',
    voiceName: 'generate',
    speed: 'generate',
    duration: 'generate',
    model: 'generate',
  };
  const DEFAULT_ACTION_IDS = ['useApiKey', 'login', 'generate', 'run', 'settings_create_api_key', 'settings_save', 'applyBulk'];

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

  function isVisible(el) {
    try {
      return !!el && !el.hidden && el.getClientRects && el.getClientRects().length > 0;
    } catch (e) {
      return false;
    }
  }

  function findDefaultAction(target) {
    const fieldId = target && target.id ? String(target.id) : '';
    const preferredId = INPUT_TO_ACTION[fieldId] || '';
    if (preferredId) {
      const button = document.getElementById(preferredId);
      if (button && !button.disabled && isVisible(button)) return button;
    }

    const form = target && target.closest ? target.closest('form') : null;
    if (form) {
      const submit = form.querySelector('button[type="submit"], input[type="submit"]');
      if (submit && !submit.disabled) return submit;
    }

    for (const id of DEFAULT_ACTION_IDS) {
      const candidate = document.getElementById(id);
      if (candidate && !candidate.disabled && isVisible(candidate)) return candidate;
    }
    return null;
  }

  function enhanceTitles() {
    const nodes = document.querySelectorAll('button, a.menu-item, select, input, textarea, [data-section]');
    nodes.forEach((el) => {
      if (el.getAttribute('title')) return;
      const id = el.id ? String(el.id) : '';
      if (id && TITLE_BY_ID[id]) {
        el.setAttribute('title', TITLE_BY_ID[id]);
        return;
      }
      if (el.classList.contains('backend-status-refresh')) {
        el.setAttribute('title', 'Refresh backend health and readiness information.');
        return;
      }
      if (el.hasAttribute('data-section')) {
        const label = String(el.textContent || '').trim();
        if (label) el.setAttribute('title', `Open the ${label} settings section.`);
        return;
      }
      const text = String(el.getAttribute('aria-label') || el.textContent || el.getAttribute('placeholder') || '').trim();
      if (text) el.setAttribute('title', text);
    });
  }

  function installDefaultEnterBehavior() {
    document.addEventListener('keydown', (event) => {
      if (event.defaultPrevented) return;
      if (event.key !== 'Enter') return;
      if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      if (target.tagName === 'TEXTAREA') return;
      if (target.tagName !== 'INPUT') return;
      const type = String(target.getAttribute('type') || 'text').toLowerCase();
      if (['checkbox', 'radio', 'range', 'file'].includes(type)) return;
      const action = findDefaultAction(target);
      if (!action) return;
      event.preventDefault();
      action.click();
    });
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
  document.addEventListener('DOMContentLoaded', () => {
    enhanceTitles();
    installDefaultEnterBehavior();
  });
  emitAuthChanged();
})();