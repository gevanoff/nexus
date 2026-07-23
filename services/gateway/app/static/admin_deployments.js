(() => {
  const state = {
    capabilities: null,
    jobs: [],
    selectedJobId: "",
    refreshTimer: null,
    loading: false,
  };

  const el = (id) => document.getElementById(id);
  const banner = el("banner");
  const configuredValue = el("configuredValue");
  const controllerValue = el("controllerValue");
  const activeValue = el("activeValue");
  const endpointValue = el("endpointValue");
  const hostSelect = el("hostSelect");
  const branchSelect = el("branchSelect");
  const componentList = el("componentList");
  const reasonInput = el("reasonInput");
  const deploymentForm = el("deploymentForm");
  const submitButton = el("submitButton");
  const formHint = el("formHint");
  const jobsBody = el("jobsBody");
  const refreshButton = el("refreshButton");
  const autoRefresh = el("autoRefresh");
  const detailRefresh = el("detailRefresh");
  const detailTitle = el("detailTitle");
  const detailSummary = el("detailSummary");
  const detailLog = el("detailLog");

  function showBanner(message, kind = "") {
    if (!message) {
      banner.hidden = true;
      banner.textContent = "";
      banner.className = "banner";
      return;
    }
    banner.hidden = false;
    banner.textContent = message;
    banner.className = `banner ${kind}`.trim();
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload?.detail;
      const message = typeof detail === "string"
        ? detail
        : detail?.message || payload?.message || `Request failed with HTTP ${response.status}`;
      throw new Error(message);
    }
    return payload;
  }

  function statusHtml(label, kind) {
    return `<span class="dot ${kind || ""}"></span>${escapeHtml(label)}`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function formatTime(value) {
    const seconds = Number(value || 0);
    if (!seconds) return "—";
    return new Date(seconds * 1000).toLocaleString();
  }

  function duration(job) {
    const started = Number(job?.started_at || 0);
    if (!started) return "not started";
    const end = Number(job?.finished_at || 0) || Date.now() / 1000;
    const total = Math.max(0, Math.round(end - started));
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return `${minutes}m ${seconds}s`;
  }

  function topologyForHost(host) {
    const mapping = state.capabilities?.topology_components;
    if (mapping && Array.isArray(mapping[host])) return mapping[host];
    return state.capabilities?.allowed_components || [];
  }

  function renderCapabilities() {
    const capabilities = state.capabilities || {};
    const hosts = Array.isArray(capabilities.allowed_hosts) ? capabilities.allowed_hosts : [];
    const branches = Array.isArray(capabilities.allowed_branches) ? capabilities.allowed_branches : ["main"];

    hostSelect.innerHTML = hosts.length
      ? `<option value="">Select a host</option>${hosts.map((host) => `<option value="${escapeHtml(host)}">${escapeHtml(host)}</option>`).join("")}`
      : '<option value="">No hosts available</option>';
    branchSelect.innerHTML = branches.map((branch) => `<option value="${escapeHtml(branch)}">${escapeHtml(branch)}</option>`).join("");
    renderComponents();
  }

  function renderComponents() {
    const host = hostSelect.value;
    const components = topologyForHost(host);
    if (!host) {
      componentList.innerHTML = '<div class="muted">Select a host.</div>';
      return;
    }
    if (!components.length) {
      componentList.innerHTML = '<div class="muted">No deployable components are assigned to this host.</div>';
      return;
    }
    componentList.innerHTML = components
      .map((component) => `
        <label class="component-option">
          <input type="checkbox" name="component" value="${escapeHtml(component)}" />
          <span>${escapeHtml(component)}</span>
        </label>`)
      .join("");
  }

  function renderJobs() {
    if (!state.jobs.length) {
      jobsBody.innerHTML = '<tr><td colspan="5" class="empty">No deployment jobs have been recorded.</td></tr>';
      return;
    }
    jobsBody.innerHTML = state.jobs.map((job) => {
      const status = String(job.status || "unknown");
      return `
        <tr data-job-id="${escapeHtml(job.id)}">
          <td><span class="job-status ${escapeHtml(status)}">${escapeHtml(status)}</span></td>
          <td>${escapeHtml(job.host)}</td>
          <td>${escapeHtml((job.components || []).join(", "))}</td>
          <td>${escapeHtml(formatTime(job.created_at))}</td>
          <td>${escapeHtml(job.requested_by || "—")}</td>
        </tr>`;
    }).join("");
    jobsBody.querySelectorAll("tr[data-job-id]").forEach((row) => {
      row.addEventListener("click", () => selectJob(row.dataset.jobId));
    });
  }

  function renderSummary(payload) {
    const configured = !!payload.configured;
    const reachable = !!payload.controller_reachable;
    configuredValue.innerHTML = statusHtml(configured ? "Configured" : "Not configured", configured ? "ok" : "bad");
    controllerValue.innerHTML = statusHtml(reachable ? "Reachable" : "Unavailable", reachable ? "ok" : "bad");
    endpointValue.textContent = payload.configuration?.base_url || "—";

    const active = state.jobs.filter((job) => ["queued", "running"].includes(String(job.status)));
    activeValue.textContent = active.length ? `${active.length} queued or running` : "Idle";

    submitButton.disabled = !configured || !reachable;
    formHint.textContent = configured && reachable
      ? "Deployments are serialized by the controller."
      : "Deployment submission is disabled until the controller is reachable.";
  }

  async function loadStatus({ quiet = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    refreshButton.disabled = true;
    try {
      const payload = await api("/ui/api/admin/deployments/status?limit=50");
      state.capabilities = payload.capabilities || null;
      state.jobs = Array.isArray(payload.deployments) ? payload.deployments : [];
      renderCapabilities();
      renderJobs();
      renderSummary(payload);
      if (Array.isArray(payload.errors) && payload.errors.length && !quiet) {
        showBanner(payload.errors.join("; "), "error");
      } else if (!payload.configured && !quiet) {
        showBanner(payload.error || "Deployment Control is not configured.", "error");
      } else if (!quiet) {
        showBanner("");
      }
      if (state.selectedJobId) await loadJob(state.selectedJobId, { quiet: true });
    } catch (error) {
      configuredValue.innerHTML = statusHtml("Unknown", "warn");
      controllerValue.innerHTML = statusHtml("Unavailable", "bad");
      submitButton.disabled = true;
      if (!quiet) showBanner(error.message, "error");
    } finally {
      state.loading = false;
      refreshButton.disabled = false;
      scheduleRefresh();
    }
  }

  function scheduleRefresh() {
    if (state.refreshTimer) window.clearTimeout(state.refreshTimer);
    if (!autoRefresh.checked) return;
    const busy = state.jobs.some((job) => ["queued", "running"].includes(String(job.status)));
    state.refreshTimer = window.setTimeout(() => loadStatus({ quiet: true }), busy ? 3000 : 15000);
  }

  async function selectJob(jobId) {
    state.selectedJobId = String(jobId || "");
    detailRefresh.disabled = !state.selectedJobId;
    await loadJob(state.selectedJobId);
  }

  async function loadJob(jobId, { quiet = false } = {}) {
    if (!jobId) return;
    try {
      const job = await api(`/ui/api/admin/deployments/${encodeURIComponent(jobId)}`);
      detailTitle.textContent = `Deployment ${String(job.id || jobId).slice(0, 12)}`;
      detailSummary.textContent = `${job.status || "unknown"} · ${job.host || "unknown host"} · ${duration(job)} · ${formatTime(job.created_at)}`;
      const lines = Array.isArray(job.log_tail) ? job.log_tail : [];
      const header = [
        `status: ${job.status || "unknown"}`,
        `host: ${job.host || ""}`,
        `components: ${(job.components || []).join(", ")}`,
        `branch: ${job.branch || ""}`,
        `requested_by: ${job.requested_by || ""}`,
        `reason: ${job.reason || ""}`,
        `return_code: ${job.return_code ?? ""}`,
        job.error ? `error: ${job.error}` : "",
      ].filter(Boolean);
      detailLog.textContent = [...header, "", ...lines].join("\n") || "No log output recorded.";
    } catch (error) {
      if (!quiet) showBanner(error.message, "error");
    }
  }

  function selectedComponents() {
    return Array.from(componentList.querySelectorAll('input[name="component"]:checked')).map((input) => input.value);
  }

  deploymentForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const host = hostSelect.value;
    const components = selectedComponents();
    const branch = branchSelect.value || "main";
    const reason = reasonInput.value.trim();
    if (!host) return showBanner("Select a target host.", "error");
    if (!components.length) return showBanner("Select at least one component.", "error");

    const summary = `Deploy ${components.join(", ")} to ${host} from ${branch}?`;
    if (!window.confirm(summary)) return;

    submitButton.disabled = true;
    showBanner("Submitting deployment…");
    try {
      const job = await api("/ui/api/admin/deployments", {
        method: "POST",
        body: JSON.stringify({ host, components, branch, environment: "prod", reason }),
      });
      reasonInput.value = "";
      showBanner(`Deployment ${String(job.id || "").slice(0, 12)} queued for ${host}.`, "success");
      state.selectedJobId = job.id || "";
      await loadStatus({ quiet: true });
      if (state.selectedJobId) await loadJob(state.selectedJobId, { quiet: true });
    } catch (error) {
      showBanner(error.message, "error");
    } finally {
      submitButton.disabled = false;
    }
  });

  hostSelect.addEventListener("change", renderComponents);
  refreshButton.addEventListener("click", () => loadStatus());
  detailRefresh.addEventListener("click", () => loadJob(state.selectedJobId));
  autoRefresh.addEventListener("change", scheduleRefresh);
  window.addEventListener("beforeunload", () => {
    if (state.refreshTimer) window.clearTimeout(state.refreshTimer);
  });

  loadStatus();
})();
