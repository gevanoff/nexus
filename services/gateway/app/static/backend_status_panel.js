(() => {
  const panelSelector = 'details[data-backend-status]';

  function handle401(resp) {
    if (resp && resp.status === 401) {
      const back = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/ui/login?next=${back}`;
      return true;
    }
    return false;
  }

  function formatTimestamp(tsSeconds) {
    if (!Number.isFinite(tsSeconds)) return '--';
    try {
      return new Date(tsSeconds * 1000).toLocaleTimeString();
    } catch (e) {
      return '--';
    }
  }

  function initPanel(panel) {
    if (!panel) return;
    if (panel.dataset.backendStatusInit === '1') return;
    panel.dataset.backendStatusInit = '1';

    const classes = String(panel.getAttribute('data-backends') || '')
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);

    const updatedEl = panel.querySelector('.backend-status-updated');
    const spinnerEl = panel.querySelector('.backend-status-spinner');
    const errorEl = panel.querySelector('.backend-status-error');
    const refreshEl = panel.querySelector('.backend-status-refresh');
    const listEl = panel.querySelector('.backend-status-list');

    let timer = null;

    function stopPolling() {
      if (timer) {
        window.clearInterval(timer);
        timer = null;
      }
    }

    function renderRows(backends) {
      if (!listEl) return;
      listEl.innerHTML = '';

      if (!Array.isArray(backends) || !backends.length) {
        const empty = document.createElement('div');
        empty.className = 'hint';
        empty.textContent = 'No matching backends configured.';
        listEl.appendChild(empty);
        return;
      }

      backends.forEach((backend) => {
        const row = document.createElement('div');
        row.style.padding = '6px 0';
        row.style.borderBottom = '1px solid rgba(231,237,246,0.08)';

        const name = String(backend?.backend_class || 'unknown');
        const healthy = backend?.healthy === true ? 'healthy' : backend?.healthy === false ? 'unhealthy' : 'health unknown';
        const ready = backend?.ready === true ? 'ready' : backend?.ready === false ? 'not ready' : 'readiness unknown';

        const head = document.createElement('div');
        head.textContent = `${name}: ${healthy}, ${ready}`;
        row.appendChild(head);

        if (backend?.error || backend?.health_error) {
          const err = document.createElement('div');
          err.className = 'hint error';
          err.textContent = String(backend.error || backend.health_error || '');
          row.appendChild(err);
        }

        listEl.appendChild(row);
      });
    }

    async function loadStatus() {
      if (refreshEl) refreshEl.disabled = true;
      if (spinnerEl) spinnerEl.hidden = false;
      if (errorEl) errorEl.hidden = true;

      try {
        const resp = await fetch('/ui/api/backend_status', { credentials: 'same-origin' });
        if (handle401(resp)) return;
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }

        const payload = await resp.json();
        const allBackends = Array.isArray(payload?.backends) ? payload.backends : [];
        const filtered = classes.length
          ? allBackends.filter((item) => classes.includes(String(item?.backend_class || '').trim()))
          : allBackends;

        renderRows(filtered);
        if (updatedEl) {
          updatedEl.textContent = `Last updated: ${formatTimestamp(Number(payload?.generated_at || 0))}`;
        }
      } catch (e) {
        if (errorEl) {
          errorEl.textContent = `Failed to load backend status: ${String(e?.message || e)}`;
          errorEl.hidden = false;
        }
        if (listEl) {
          listEl.innerHTML = '';
          const empty = document.createElement('div');
          empty.className = 'hint';
          empty.textContent = 'Unable to load backend status.';
          listEl.appendChild(empty);
        }
      } finally {
        if (refreshEl) refreshEl.disabled = false;
        if (spinnerEl) spinnerEl.hidden = true;
      }
    }

    function startPolling() {
      stopPolling();
      void loadStatus();
      timer = window.setInterval(() => {
        void loadStatus();
      }, 30000);
    }

    panel.addEventListener('toggle', () => {
      if (panel.open) {
        startPolling();
      } else {
        stopPolling();
      }
    });

    if (refreshEl) {
      refreshEl.addEventListener('click', () => {
        void loadStatus();
      });
    }

    if (panel.open) {
      startPolling();
    }
  }

  function initAll() {
    document.querySelectorAll(panelSelector).forEach((panel) => initPanel(panel));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();
