(() => {
  const state = {
    capabilities: null,
    jobs: [],
    selectedJobId: "",
    refreshTimer: null,
    loading: false,
    canSubmit: false,
    form: {
      host: "",
      branch: "main",
      components: new Set(),
    },
    hostScroll: new Map(),
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
  const deploymentEffects = el("deploymentEffects");
  const reasonInput = el("reasonInput");
  const deploymentForm = el("deploymentForm");
  const submitButton = el("submitButton");
  const formHint = el("formHint");
  const jobsByHost = el("jobsByHost");
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

  function componentOverlayEffects(host, components) {
    const hostOverlays = state.capabilities?.component_overlays?.[host];
    if (!hostOverlays || typeof hostOverlays !== "object") return [];
    const effects = [];
    for (const component of components) {
      const overlays = Array.isArray(hostOverlays[component]) ? hostOverlays[component] : [];
      for (const overlay of overlays) {
        if (!overlay || typeof overlay !== "object") continue;
        effects.push({
          component,
          supportingComponent: String(overlay.component || "supporting configuration"),
          description: String(overlay.description || "Supporting deployment configuration will be applied."),
          restartsComponent: overlay.restarts_component === true,
        });
      }
    }
    return effects;
  }

  function selectedComponents() {
    return Array.from(componentList.querySelectorAll('input[name="component"]:checked'))
      .map((input) => input.value);
  }

  function renderDeploymentEffects() {
    const host = String(hostSelect.value || "");
    const effects = componentOverlayEffects(host, Array.from(state.form.components));
    if (!effects.length) {
      deploymentEffects.hidden = true;
      deploymentEffects.innerHTML = "";
      return;
    }
    deploymentEffects.hidden = false;
    deploymentEffects.innerHTML = effects
      .map((effect) => {
        const behavior = effect.restartsComponent
          ? `${effect.supportingComponent} will also be restarted.`
          : `${effect.supportingComponent} supplies configuration only and will not be restarted unless explicitly selected.`;
        return `<strong>${escapeHtml(effect.component)} supporting overlay:</strong> ${escapeHtml(effect.description)} ${escapeHtml(behavior)}`;
      })
      .join("<br />");
  }

  function captureFormState() {
    state.form.host = String(hostSelect.value || "");
    state.form.branch = String(branchSelect.value || "main");
    state.form.components = new Set(selectedComponents());
  }

  function captureHostScroll() {
    jobsByHost.querySelectorAll(".host-job-panel[data-host]").forEach((panel) => {
      const scroller = panel.querySelector(".host-job-scroll");
      if (scroller) state.hostScroll.set(panel.dataset.host || "unknown", scroller.scrollTop);
    });
  }

  function restoreHostScroll() {
    jobsByHost.querySelectorAll(".host-job-panel[data-host]").forEach((panel) => {
      const scroller = panel.querySelector(".host-job-scroll");
      if (!scroller) return;
      const saved = Number(state.hostScroll.get(panel.dataset.host || "unknown") || 0);
      scroller.scrollTop = saved;
    });
  }

  function markSelectedJobRows() {
    jobsByHost.querySelectorAll("tr[data-job-id]").forEach((row) => {
      const selected = !!state.selectedJobId && row.dataset.jobId === state.selectedJobId;
      row.classList.toggle("selected", selected);
      row.setAttribute("aria-selected", selected ? "true" : "false");
    });
  }

  function renderCapabilities() {
    const capabilities = state.capabilities || {};
    const hosts = Array.isArray(capabilities.allowed_hosts) ? capabilities.allowed_hosts : [];
    const branches = Array.isArray(capabilities.allowed_branches) ? capabilities.allowed_branches : ["main"];
    const previousHost = state.form.host;
    const previousBranch = state.form.branch || "main";

    hostSelect.innerHTML = hosts.length
      ? `<option value="">Select a host</option>${hosts.map((host) => `<option value="${escapeHtml(host)}">${escapeHtml(host)}</option>`).join("")}`
      : '<option value="">No hosts available</option>';
    hostSelect.value = hosts.includes(previousHost) ? previousHost : "";
    state.form.host = hostSelect.value;

    branchSelect.innerHTML = branches
      .map((branch) => `<option value="${escapeHtml(branch)}">${escapeHtml(branch)}</option>`)
      .join("");
    branchSelect.value = branches.includes(previousBranch) ? previousBranch : (branches[0] || "main");
    state.form.branch = branchSelect.value;
    renderComponents();
  }

  function renderComponents() {
    const host = hostSelect.value;
    const components = topologyForHost(host);
    if (!host) {
      state.form.components.clear();
      componentList.innerHTML = '<div class="muted">Select a host.</div>';
      renderDeploymentEffects();
      return;
    }
    if (!components.length) {
      state.form.components.clear();
      componentList.innerHTML = '<div class="muted">No deployable components are assigned to this host.</div>';
      renderDeploymentEffects();
      return;
    }

    const valid = new Set(components);
    state.form.components = new Set(
      Array.from(state.form.components).filter((component) => valid.has(component)),
    );
    componentList.innerHTML = components
      .map((component) => `
        <label class="component-option">
          <input type="checkbox" name="component" value="${escapeHtml(component)}" ${state.form.components.has(component) ? "checked" : ""} />
          <span>${escapeHtml(component)}</span>
        </label>`)
      .join("");
    componentList.querySelectorAll('input[name="component"]').forEach((input) => {
      input.addEventListener("change", () => {
        if (input.checked) state.form.components.add(input.value);
        else state.form.components.delete(input.value);
        renderDeploymentEffects();
      });
    });
    renderDeploymentEffects();
  }

  function renderJobs() {
    captureHostScroll();
    if (!state.jobs.length) {
      jobsByHost.innerHTML = '<div class="empty">No deployment jobs have been recorded.</div>';
      return;
    }

    const grouped = new Map();
    for (const job of state.jobs) {
      const host = String(job.host || "unknown");
      if (!grouped.has(host)) grouped.set(host, []);
      grouped.get(host).push(job);
    }

    jobsByHost.innerHTML = Array.from(grouped.entries())
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([host, jobs]) => {
        jobs.sort((left, right) => Number(right.created_at || 0) - Number(left.created_at || 0));
        const active = jobs.filter((job) => ["queued", "running"].includes(String(job.status))).length;
        const rows = jobs.map((job) => {
          const status = String(job.status || "unknown");
          const selected = String(job.id || "") === state.selectedJobId;
          return `
            <tr data-job-id="${escapeHtml(job.id)}" class="${selected ? "selected" : ""}" aria-selected="${selected ? "true" : "false"}">
              <td><span class="job-status ${escapeHtml(status)}">${escapeHtml(status)}</span></td>
              <td>${escapeHtml((job.components || []).join(", "))}</td>
              <td>${escapeHtml(formatTime(job.created_at))}</td>
              <td>${escapeHtml(job.requested_by || "—")}</td>
            </tr>`;
        }).join("");
        const countLabel = active
          ? `${jobs.length} total · ${active} active`
          : `${jobs.length} deployment${jobs.length === 1 ? "" : "s"}`;
        return `
          <section class="host-job-panel" data-host="${escapeHtml(host)}">
            <div class="host-job-header">
              <strong>${escapeHtml(host)}</strong>
              <span class="host-job-count">${escapeHtml(countLabel)}</span>
            </div>
            <div class="host-job-scroll">
              <table>
                <thead><tr><th>Status</th><th>Components</th><th>Requested</th><th>Started by</th></tr></thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
          </section>`;
      })
      .join("");

    jobsByHost.querySelectorAll("tr[data-job-id]").forEach((row) => {
      row.addEventListener("click", () => selectJob(row.dataset.jobId));
    });
    restoreHostScroll();
    markSelectedJobRows();
  }

  function renderSummary(payload) {
    const configured = !!payload.configured;
    const reachable = !!payload.controller_reachable;
    configuredValue.innerHTML = statusHtml(configured ? "Configured" : "Not configured", configured ? "ok" : "bad");
    controllerValue.innerHTML = statusHtml(reachable ? "Reachable" : "Unavailable", reachable ? "ok" : "bad");
    endpointValue.textContent = payload.configuration?.base_url || "—";

    const active = state.jobs.filter((job) => ["queued", "running"].includes(String(job.status)));
    activeValue.textContent = active.length ? `${active.length} queued or running` : "Idle";

    state.canSubmit = configured && reachable;
    submitButton.disabled = !state.canSubmit;
    formHint.textContent = state.canSubmit
      ? "Deployments are serialized by the controller."
      : "Deployment submission is disabled until the controller is reachable.";
  }

  async function loadStatus({ quiet = false } = {}) {
    if (state.loading) return;
    captureFormState();
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
      state.canSubmit = false;
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
    markSelectedJobRows();
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

  deploymentForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    captureFormState();
    const host = state.form.host;
    const components = Array.from(state.form.components);
    const branch = state.form.branch || "main";
    const reason = reasonInput.value.trim();
    if (!host) return showBanner("Select a target host.", "error");
    if (!components.length) return showBanner("Select at least one component.", "error");
    if (!state.canSubmit) return showBanner("Deployment Control is not currently reachable.", "error");

    const effects = componentOverlayEffects(host, components);
    const confirmationLines = [`Deploy ${components.join(", ")} to ${host} from ${branch}?`];
    for (const effect of effects) {
      confirmationLines.push(
        effect.restartsComponent
          ? `${effect.supportingComponent} will also be restarted.`
          : `${effect.supportingComponent} configuration will be applied without restarting that component.`,
      );
    }
    if (!window.confirm(confirmationLines.join("\n\n"))) return;

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
      submitButton.disabled = !state.canSubmit;
    }
  });

  hostSelect.addEventListener("change", () => {
    state.form.host = hostSelect.value;
    state.form.components.clear();
    renderComponents();
  });
  branchSelect.addEventListener("change", () => {
    state.form.branch = branchSelect.value || "main";
  });
  refreshButton.addEventListener("click", () => loadStatus());
  detailRefresh.addEventListener("click", () => loadJob(state.selectedJobId));
  autoRefresh.addEventListener("change", scheduleRefresh);
  window.addEventListener("beforeunload", () => {
    if (state.refreshTimer) window.clearTimeout(state.refreshTimer);
  });

  loadStatus();
})();
