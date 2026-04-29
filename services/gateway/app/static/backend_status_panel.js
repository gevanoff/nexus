(() => {
  const panelSelector = 'details[data-backend-status]';
  const styleId = 'backend-status-shared-styles';

  function ensureSharedStyles() {
    if (document.getElementById(styleId)) return;
    const style = document.createElement('style');
    style.id = styleId;
    style.textContent = `
      .status-list { display: flex; flex-direction: column; gap: 8px; }
      .status-row { display: flex; flex-direction: column; gap: 6px; padding: 10px; border-radius: 10px; border: 1px solid rgba(231,237,246,0.08); background: linear-gradient(180deg, rgba(20,28,38,0.9), rgba(14,20,28,0.9)); }
      .status-row.ok { background: rgba(62, 197, 126, 0.12); border-color: rgba(62, 197, 126, 0.28); }
      .status-row.warn { background: rgba(255, 200, 50, 0.12); border-color: rgba(255, 200, 50, 0.28); }
      .status-row.bad { background: rgba(255, 90, 90, 0.12); border-color: rgba(255, 90, 90, 0.28); }
      .status-row.traded { background: rgba(111,184,255,0.14); border-color: rgba(111,184,255,0.34); }
      .status-row.inactive-unhealthy { background: rgba(224,0,255,0.30); border-color: rgba(255,0,255,0.80); }
      .status-row.inactive-unknown { background: rgba(169,180,195,0.08); border-color: rgba(169,180,195,0.18); }
      .status-row.tier-crucial { box-shadow: inset 4px 0 0 rgba(111,184,255,0.92), 0 0 0 1px rgba(111,184,255,0.16); }
      .status-row-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
      .status-name { font-weight: 600; }
      .status-name-alias { font-weight: 400; color: #93a4ba; }
      .status-badges { display: flex; gap: 6px; flex-wrap: wrap; }
      .status-badge { font-size: 11px; padding: 4px 8px; border-radius: 999px; border: 1px solid transparent; }
      .status-badge.ok { background: rgba(111,184,255,0.18); color: #cfe7ff; border-color: rgba(111,184,255,0.4); }
      .status-badge.warn { background: rgba(255,200,50,0.12); color: #f6d98b; border-color: rgba(255,200,50,0.3); }
      .status-badge.bad { background: rgba(255,120,120,0.12); color: #ffb6b6; border-color: rgba(255,120,120,0.3); }
      .status-badge.green { background: rgba(111,221,168,0.12); color: #b9efd8; border-color: rgba(111,221,168,0.34); }
      .status-badge.blue { background: rgba(111,184,255,0.16); color: #cfe7ff; border-color: rgba(111,184,255,0.42); }
      .status-badge.purple { background: rgba(224,0,255,0.45); color: #ffd0ff; border-color: rgba(255,0,255,0.95); }
      .status-badge.grey { background: rgba(169,180,195,0.09); color: #c5cfdd; border-color: rgba(169,180,195,0.26); }
      .status-badge.red { background: rgba(255,120,120,0.12); color: #ffb6b6; border-color: rgba(255,120,120,0.3); }
      .status-badge.yellow { background: rgba(255,200,50,0.12); color: #f6d98b; border-color: rgba(255,200,50,0.3); }
      .status-badge.crucial { background: rgba(111,184,255,0.16); color: #d7ecff; border-color: rgba(111,184,255,0.52); }
      .status-detail { font-size: 12px; color: #a9b4c3; }
      .status-meta { font-size: 12px; color: #a9b4c3; margin-bottom: 8px; }
      .status-aliases { font-size: 12px; color: #b7c4d6; }
      .status-error { font-size: 12px; color: #ffb6b6; }
      .status-empty { font-size: 12px; color: #93a4ba; }
      .backend-status-links { display: flex; justify-content: flex-end; margin: 8px 0; }
      .backend-status-resources-link { color: #cfe7ff; text-decoration: none; border: 1px solid rgba(111,184,255,0.34); border-radius: 999px; padding: 5px 9px; font-size: 12px; background: rgba(111,184,255,0.10); }
      .backend-status-resources-link:hover { border-color: rgba(111,184,255,0.58); }
      .loading-indicator { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #93a4ba; }
      .loading-indicator::before {
        content: "";
        width: 12px;
        height: 12px;
        border-radius: 50%;
        border: 2px solid rgba(231,237,246,0.18);
        border-top-color: #6fb8ff;
        animation: backend-status-spin 0.8s linear infinite;
      }
      @keyframes backend-status-spin { to { transform: rotate(360deg); } }
    `;
    document.head.appendChild(style);
  }

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

  function lifecycleColorClass(color) {
    const value = String(color || '').toLowerCase();
    if (['green', 'blue', 'purple', 'grey', 'red', 'yellow'].includes(value)) return value;
    return 'grey';
  }

  function lifecycleRowClass(status, color) {
    if (status === 'traded_out_working') return 'traded';
    if (status === 'inactive_unhealthy') return 'inactive-unhealthy';
    if (status === 'inactive_unknown') return 'inactive-unknown';
    if (status === 'active_ready') return 'ok';
    if (status === 'active_unhealthy') return 'bad';
    if (color === 'green') return 'ok';
    if (color === 'red') return 'bad';
    return 'warn';
  }

  function isInactiveLifecycleStatus(status) {
    return ['traded_out_working', 'inactive_unhealthy', 'inactive_unknown'].includes(status);
  }

  function tierClassName(tier) {
    const value = String(tier || '').trim().toLowerCase();
    if (!value) return '';
    return `tier-${value.replace(/[^a-z0-9_-]/g, '_')}`;
  }

  const backendLabels = {
    gpu_fast: 'SDXL-Turbo',
    gpu_heavy: 'InvokeAI',
    lighton_ocr: 'LightOnOCR',
    personaplex: 'PersonaPlex',
    skyreels_v2: 'SkyReels-V2',
  };

  function renderBackendRow(backend, { displayName, missing } = {}) {
    const row = document.createElement('div');
    row.className = 'status-row';

    const header = document.createElement('div');
    header.className = 'status-row-header';

    const nameContainer = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'status-name';
    const resolvedName = displayName || backend.backend_class || 'unknown';
    name.textContent = resolvedName;
    if (backend.backend_class && displayName && displayName !== backend.backend_class) {
      const alias = document.createElement('span');
      alias.className = 'status-name-alias';
      alias.textContent = ` (${backend.backend_class})`;
      name.appendChild(alias);
    }
    nameContainer.appendChild(name);

    if (backend.hostname) {
      const hostEl = document.createElement('div');
      hostEl.className = 'status-host';
      hostEl.textContent = `on ${backend.hostname}`;
      hostEl.style.fontSize = '13px';
      hostEl.style.fontWeight = '600';
      hostEl.style.color = '#6fb8ff';
      hostEl.style.marginTop = '2px';
      nameContainer.appendChild(hostEl);
    }
    header.appendChild(nameContainer);

    const badges = document.createElement('div');
    badges.className = 'status-badges';

    if (missing) {
      row.classList.add('warn');
      const missingBadge = document.createElement('span');
      missingBadge.className = 'status-badge warn';
      missingBadge.textContent = 'Not configured';
      badges.appendChild(missingBadge);
    } else {
      const lifecycleStatus = String(backend.status || backend.lifecycle_status || '').trim();
      const lifecycleLabel = String(backend.status_label || '').trim();
      const lifecycleColor = lifecycleColorClass(backend.status_color);
      const inactiveLifecycle = isInactiveLifecycleStatus(lifecycleStatus);
      const tier = String(backend.tier || backend.lifecycle?.tier || '').trim().toLowerCase();
      const rowTierClass = tierClassName(tier);
      if (rowTierClass) row.classList.add(rowTierClass);

      if (lifecycleStatus || lifecycleLabel) {
        const state = document.createElement('span');
        state.className = `status-badge ${lifecycleColor}`;
        state.textContent = lifecycleLabel || lifecycleStatus.replace(/_/g, ' ');
        badges.appendChild(state);
        row.classList.add(lifecycleRowClass(lifecycleStatus, lifecycleColor));
      }

      if (tier === 'crucial') {
        const tierBadge = document.createElement('span');
        tierBadge.className = 'status-badge crucial';
        tierBadge.textContent = 'Crucial';
        badges.appendChild(tierBadge);
      }

      if (backend.active === true) {
        const activeBadge = document.createElement('span');
        activeBadge.className = 'status-badge green';
        activeBadge.textContent = 'Active';
        badges.appendChild(activeBadge);
      } else if (backend.active === false) {
        const activeBadge = document.createElement('span');
        activeBadge.className = 'status-badge grey';
        activeBadge.textContent = 'Inactive';
        badges.appendChild(activeBadge);
      }

      const healthy = document.createElement('span');
      const isHealthy = backend.healthy === true;
      healthy.className = `status-badge ${inactiveLifecycle ? 'grey' : isHealthy ? 'green' : backend.healthy === false ? 'red' : 'yellow'}`;
      healthy.textContent = inactiveLifecycle ? 'Health skipped' : backend.healthy == null ? 'Health unknown' : isHealthy ? 'Healthy' : 'Unhealthy';
      badges.appendChild(healthy);

      const ready = document.createElement('span');
      const isReady = backend.ready === true;
      ready.className = `status-badge ${inactiveLifecycle ? 'grey' : isReady ? 'green' : backend.ready === false ? 'red' : 'yellow'}`;
      ready.textContent = inactiveLifecycle ? 'Readiness stopped' : backend.ready == null ? 'Readiness unknown' : isReady ? 'Ready' : 'Not ready';
      badges.appendChild(ready);

      if (!inactiveLifecycle) {
        if (!lifecycleStatus && backend.healthy === false && backend.ready === false) {
          row.classList.add('bad');
        } else if (!lifecycleStatus && isHealthy && isReady) {
          row.classList.add('ok');
        } else if (!lifecycleStatus) {
          row.classList.add('warn');
        }
      } else if (lifecycleStatus === 'traded_out_working' && backend.last_ready_at) {
        const lastReady = document.createElement('span');
        lastReady.className = 'status-badge blue';
        lastReady.textContent = `Last ready ${formatTimestamp(Number(backend.last_ready_at || 0))}`;
        badges.appendChild(lastReady);
      } else if (lifecycleStatus === 'inactive_unhealthy' && backend.last_unhealthy_at) {
        const lastBad = document.createElement('span');
        lastBad.className = 'status-badge purple';
        lastBad.textContent = `Last unhealthy ${formatTimestamp(Number(backend.last_unhealthy_at || 0))}`;
        badges.appendChild(lastBad);
      } else if (lifecycleStatus === 'inactive_unknown') {
        const unknown = document.createElement('span');
        unknown.className = 'status-badge grey';
        unknown.textContent = 'Never ready';
        badges.appendChild(unknown);
      }
    }

    header.appendChild(badges);
    row.appendChild(header);

    const detail = document.createElement('div');
    detail.className = 'status-detail';
    if (missing) {
      detail.textContent = 'Not configured in the backend registry.';
    } else {
      const capabilities = Array.isArray(backend.capabilities) ? backend.capabilities.join(', ') : 'unknown';
      const lastCheckValue = Number(backend.last_checked_at || backend.last_check || 0);
      const detailParts = [`Capabilities: ${capabilities}`, `Last check: ${lastCheckValue > 0 ? formatTimestamp(lastCheckValue) : '--'}`];
      const lastReady = Number(backend.last_ready_at || backend.last_confirmed_working_at || 0);
      const lastUnhealthy = Number(backend.last_unhealthy_at || 0);
      const lastStopped = Number(backend.last_stopped_at || 0);
      if (backend.active === false && lastReady > 0) detailParts.push(`Last ready: ${formatTimestamp(lastReady)}`);
      if (backend.active === false && lastUnhealthy > 0 && lastUnhealthy > lastReady) detailParts.push(`Last unhealthy: ${formatTimestamp(lastUnhealthy)}`);
      if (backend.active === false && lastStopped > 0) detailParts.push(`Stopped: ${formatTimestamp(lastStopped)}`);
      if (backend.inflight) detailParts.push(`${backend.inflight} running`);
      detail.textContent = detailParts.join(' • ');
    }
    row.appendChild(detail);

    const aliasEntries = Array.isArray(backend.aliases) ? backend.aliases : [];
    if (aliasEntries.length > 0) {
      const aliasDetail = document.createElement('div');
      aliasDetail.className = 'status-aliases';
      const aliasText = aliasEntries
        .map((alias) => `${alias.name} → ${alias.target}`)
        .filter(Boolean)
        .join(', ');
      aliasDetail.textContent = `Aliases: ${aliasText}`;
      row.appendChild(aliasDetail);
    }

    let errorText = String(backend.error || backend.health_error || backend.last_health_error || backend.last_action_error || '');
    const lifecycleStatus = String(backend.status || backend.lifecycle_status || '').trim();
    if (backend.active === false && lifecycleStatus === 'traded_out_working') {
      errorText = String(backend.last_action_error || '');
    } else if (backend.active === false && lifecycleStatus === 'inactive_unknown') {
      errorText = String(backend.last_action_error || '');
    } else if (backend.active === false && lifecycleStatus === 'inactive_unhealthy') {
      errorText = String(backend.last_action_error || backend.last_health_error || backend.health_error || backend.error || '');
    }
    if (errorText) {
      const err = document.createElement('div');
      err.className = 'status-error';
      err.textContent = errorText;
      row.appendChild(err);
    }

    return row;
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
    let metaEl = panel.querySelector('.backend-status-meta');

    if (listEl) {
      listEl.classList.add('status-list');
    }
    if (!metaEl && listEl) {
      metaEl = document.createElement('div');
      metaEl.className = 'backend-status-meta';
      listEl.parentNode.insertBefore(metaEl, listEl);
    }
    if (listEl && !panel.querySelector('.backend-status-resources-link')) {
      const linkRow = document.createElement('div');
      linkRow.className = 'backend-status-links';
      const link = document.createElement('a');
      link.className = 'backend-status-resources-link';
      link.href = '/ui/resources';
      link.textContent = 'Open Resources';
      linkRow.appendChild(link);
      listEl.parentNode.insertBefore(linkRow, listEl);
    }
    if (spinnerEl) {
      spinnerEl.classList.remove('hint');
      spinnerEl.classList.add('loading-indicator');
      spinnerEl.textContent = 'Refreshing…';
    }
    if (errorEl) {
      errorEl.classList.add('status-error');
    }

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
        empty.className = 'status-empty';
        empty.textContent = 'No matching backends configured.';
        listEl.appendChild(empty);
        return;
      }

      backends.forEach((backend) => {
        const key = String(backend?.backend_class || '').trim();
        const displayName = backendLabels[key] || key || 'unknown';
        listEl.appendChild(renderBackendRow(backend || { backend_class: key }, { displayName, missing: !backend }));
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
        const aliasConfig = payload?.alias_config || {};
        const allBackends = Array.isArray(payload?.backends) ? payload.backends : [];
        const filtered = classes.length
          ? allBackends.filter((item) => classes.includes(String(item?.backend_class || '').trim()))
          : allBackends;

        if (metaEl) {
          const source = String(aliasConfig.source || 'defaults');
          const configuredPath = String(aliasConfig.configured_path || '');
          const err = String(aliasConfig.error || '');
          metaEl.textContent = err
            ? `Alias config: ${source} • ${configuredPath || 'no explicit path'} • ${err}`
            : `Alias config: ${source}${configuredPath ? ` • ${configuredPath}` : ''}`;
          metaEl.className = `backend-status-meta${err ? ' status-error' : ''}`;
        }
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
          empty.className = 'status-empty';
          empty.textContent = 'Unable to load backend status.';
          listEl.appendChild(empty);
        }
        if (metaEl) {
          metaEl.textContent = '';
          metaEl.className = 'backend-status-meta';
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
      if (!panel.open) {
        panel.open = true;
        return;
      }
      startPolling();
    });

    if (refreshEl) {
      refreshEl.addEventListener('click', () => {
        void loadStatus();
      });
    }

    panel.open = true;
    startPolling();
  }

  function initAll() {
    ensureSharedStyles();
    document.querySelectorAll(panelSelector).forEach((panel) => initPanel(panel));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();
