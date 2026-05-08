(() => {
  const els = {
    status: document.getElementById("status"),
    form: document.getElementById("taskForm"),
    diagnostics: document.getElementById("diagnostics"),
    title: document.getElementById("taskTitle"),
    prompt: document.getElementById("taskPrompt"),
    taskType: document.getElementById("taskType"),
    model: document.getElementById("model"),
    scheduleMode: document.getElementById("scheduleMode"),
    maxRuns: document.getElementById("maxRuns"),
    delayFields: document.getElementById("delayFields"),
    delayHours: document.getElementById("delayHours"),
    delayMinutes: document.getElementById("delayMinutes"),
    delaySeconds: document.getElementById("delaySeconds"),
    runAtFields: document.getElementById("runAtFields"),
    runAt: document.getElementById("runAt"),
    intervalFields: document.getElementById("intervalFields"),
    intervalHours: document.getElementById("intervalHours"),
    intervalMinutes: document.getElementById("intervalMinutes"),
    intervalSeconds: document.getElementById("intervalSeconds"),
    cronFields: document.getElementById("cronFields"),
    cron: document.getElementById("cron"),
    tier: document.getElementById("tier"),
    maxTurns: document.getElementById("maxTurns"),
    maxRuntime: document.getElementById("maxRuntime"),
    tools: document.getElementById("tools"),
    resetForm: document.getElementById("resetForm"),
    refresh: document.getElementById("refresh"),
    statusFilter: document.getElementById("statusFilter"),
    tasks: document.getElementById("tasks"),
    detailEmpty: document.getElementById("detailEmpty"),
    detail: document.getElementById("detail"),
    detailTitle: document.getElementById("detailTitle"),
    detailMeta: document.getElementById("detailMeta"),
    detailBadges: document.getElementById("detailBadges"),
    detailPrompt: document.getElementById("detailPrompt"),
    runs: document.getElementById("runs"),
    refreshRuns: document.getElementById("refreshRuns"),
    cancelTask: document.getElementById("cancelTask"),
  };

  const state = {
    capabilities: null,
    models: [],
    tasks: [],
    selectedId: "",
  };

  function setStatus(text, isError = false, diagnostic = null) {
    if (!els.status) return;
    els.status.textContent = text || "";
    els.status.className = `hint status${isError ? " error" : text ? " ok" : ""}`;
    if (els.diagnostics) {
      if (isError && diagnostic) {
        els.diagnostics.style.display = "block";
        els.diagnostics.textContent = diagnostic;
      } else {
        els.diagnostics.style.display = "none";
        els.diagnostics.textContent = "";
      }
    }
  }

  async function fetchJson(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const resp = await fetch(url, { ...options, headers, credentials: "same-origin" });
    const text = await resp.text();
    let payload = {};
    if (text) {
      try { payload = JSON.parse(text); } catch (error) { payload = { detail: text }; }
    }
    if (!resp.ok) {
      const detail = payload?.detail || payload?.error || `HTTP ${resp.status}`;
      // Attach raw response for diagnostics
      const diagnostic = [
        `Status: ${resp.status} ${resp.statusText}`,
        `URL: ${url}`,
        `Detail: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`,
        text ? `Raw: ${text}` : null
      ].filter(Boolean).join("\n");
      setStatus(typeof detail === "string" ? detail : JSON.stringify(detail), true, diagnostic);
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return payload;
  }

  function fmtTs(ts) {
    if (!ts) return "never";
    try { return new Date(Number(ts) * 1000).toLocaleString(); } catch (error) { return String(ts); }
  }

  function durationParts(prefix) {
    const h = Number(els[`${prefix}Hours`]?.value || 0);
    const m = Number(els[`${prefix}Minutes`]?.value || 0);
    const s = Number(els[`${prefix}Seconds`]?.value || 0);
    return Math.max(0, Math.floor(h * 3600 + m * 60 + s));
  }

  function statusBadge(value) {
    const span = document.createElement("span");
    const status = String(value || "unknown");
    span.className = `badge ${status}`;
    span.textContent = status;
    return span;
  }

  function badge(text, cls = "") {
    const span = document.createElement("span");
    span.className = `badge ${cls}`;
    span.textContent = text;
    return span;
  }

  function taskMeta(task) {
    const meta = task?.metadata || {};
    const bits = [];
    if (meta.model) bits.push(`model ${meta.model}`);
    if (task.kind) bits.push(task.kind);
    if (task.next_run_ts) bits.push(`next ${fmtTs(task.next_run_ts)}`);
    if (task.last_run_ts) bits.push(`last ${fmtTs(task.last_run_ts)}`);
    return bits.join(" - ");
  }

  function renderTasks() {
    if (!els.tasks) return;
    els.tasks.innerHTML = "";
    if (!state.tasks.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "No scheduled tasks.";
      els.tasks.appendChild(empty);
      renderDetail(null);
      return;
    }
    for (const task of state.tasks) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `task-item${task.id === state.selectedId ? " selected" : ""}`;
      const title = document.createElement("div");
      title.className = "task-title";
      title.textContent = task.title || task.id;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = taskMeta(task);
      const badges = document.createElement("div");
      badges.className = "badges";
      badges.appendChild(statusBadge(task.status));
      badges.appendChild(badge(`${task.run_count || 0} run${Number(task.run_count || 0) === 1 ? "" : "s"}`, task.last_ok ? "ok" : ""));
      if (task.last_ok === false) badges.appendChild(badge("last failed", "error"));
      btn.appendChild(title);
      btn.appendChild(meta);
      btn.appendChild(badges);
      btn.addEventListener("click", () => selectTask(task.id));
      els.tasks.appendChild(btn);
    }
  }

  function renderDetail(task) {
    if (!task) {
      if (els.detailEmpty) els.detailEmpty.hidden = false;
      if (els.detail) els.detail.hidden = true;
      if (els.cancelTask) els.cancelTask.disabled = true;
      return;
    }
    if (els.detailEmpty) els.detailEmpty.hidden = true;
    if (els.detail) els.detail.hidden = false;
    if (els.cancelTask) els.cancelTask.disabled = ["completed", "cancelled"].includes(String(task.status));
    if (els.detailTitle) els.detailTitle.textContent = task.title || task.id;
    if (els.detailMeta) {
      const meta = task.metadata || {};
      els.detailMeta.textContent = [
        `id ${task.id}`,
        `agent ${task.agent || ""}`,
        `model ${meta.model || ""}`,
        `created ${fmtTs(task.created_ts)}`,
      ].filter(Boolean).join(" - ");
    }
    if (els.detailBadges) {
      els.detailBadges.innerHTML = "";
      els.detailBadges.appendChild(statusBadge(task.status));
      if (task.next_run_ts) els.detailBadges.appendChild(badge(`next ${fmtTs(task.next_run_ts)}`, "enabled"));
      if (task.max_runs) els.detailBadges.appendChild(badge(`max ${task.max_runs}`));
      const tools = task.metadata?.tools;
      if (Array.isArray(tools)) els.detailBadges.appendChild(badge(`${tools.length} tools`));
    }
    if (els.detailPrompt) els.detailPrompt.textContent = task.prompt || "";
  }

  function renderRuns(runs) {
    if (!els.runs) return;
    els.runs.innerHTML = "";
    if (!Array.isArray(runs) || !runs.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "No runs yet.";
      els.runs.appendChild(empty);
      return;
    }
    for (const run of runs) {
      const wrap = document.createElement("div");
      wrap.className = "run";
      const top = document.createElement("div");
      top.className = "row";
      top.style.justifyContent = "space-between";
      const title = document.createElement("strong");
      title.textContent = run.ok === true ? "Completed" : run.ok === false ? "Failed" : "Running";
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = `${fmtTs(run.started_ts)} - due ${fmtTs(run.due_ts)}`;
      top.appendChild(title);
      top.appendChild(meta);
      wrap.appendChild(top);
      const output = document.createElement("div");
      output.className = "pre";
      output.textContent = run.error || run.output_text || JSON.stringify(run.payload || {}, null, 2);
      wrap.appendChild(output);
      els.runs.appendChild(wrap);
    }
  }

  function renderTaskTypes() {
    if (!els.taskType || !state.capabilities) return;
    els.taskType.innerHTML = "";
    for (const item of state.capabilities.task_types || []) {
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = item.enabled ? item.label : `${item.label} (future)`;
      opt.disabled = !item.enabled;
      els.taskType.appendChild(opt);
    }
  }

  function renderModels() {
    if (!els.model) return;
    const current = els.model.value;
    els.model.innerHTML = "";
    const preferred = ["default", "fast", "reasoning", "coder", "long"];
    const seen = new Set();
    const add = (id, label) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = label || id;
      els.model.appendChild(opt);
    };
    for (const id of preferred) add(id, id);
    for (const model of state.models) add(model.id, model.label || model.id);
    if (current && seen.has(current)) els.model.value = current;
    else els.model.value = "default";
  }

  function renderTools() {
    if (!els.tools || !state.capabilities) return;
    const tier = Number(els.tier?.value || 0);
    const defaults = new Set(["tool_manifest", "current_time", "web_browse"]);
    const existing = new Set(Array.from(els.tools.querySelectorAll("input:checked")).map((el) => el.value));
    els.tools.innerHTML = "";
    for (const tool of state.capabilities.tools || []) {
      if (Number(tool.tier) > tier) continue;
      const label = document.createElement("label");
      label.className = "tool";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = tool.name;
      input.checked = existing.has(tool.name) || (!existing.size && defaults.has(tool.name));
      const text = document.createElement("span");
      text.textContent = tool.name;
      if (tool.description) {
        const small = document.createElement("small");
        small.textContent = tool.description;
        text.appendChild(small);
      }
      label.appendChild(input);
      label.appendChild(text);
      els.tools.appendChild(label);
    }
  }

  function updateScheduleFields() {
    const mode = els.scheduleMode?.value || "delay";
    if (els.delayFields) els.delayFields.hidden = mode !== "delay";
    if (els.runAtFields) els.runAtFields.hidden = mode !== "run_at";
    if (els.intervalFields) els.intervalFields.hidden = mode !== "interval";
    if (els.cronFields) els.cronFields.hidden = mode !== "cron";
  }

  function selectedTools() {
    return Array.from(els.tools?.querySelectorAll("input:checked") || []).map((el) => el.value);
  }

  function createPayload() {
    const mode = els.scheduleMode?.value || "delay";
    const payload = {
      task_type: els.taskType?.value || "llm",
      title: els.title?.value.trim() || "",
      prompt: els.prompt?.value.trim() || "",
      model: els.model?.value || "default",
      tier: Number(els.tier?.value || 0),
      tools: selectedTools(),
      max_turns: Number(els.maxTurns?.value || 20),
      max_runtime_sec: Number(els.maxRuntime?.value || 300),
      metadata: { ui: "scheduled_tasks" },
    };
    const maxRuns = Number(els.maxRuns?.value || 0);
    if (maxRuns > 0) payload.max_runs = maxRuns;
    if (mode === "delay") {
      payload.delay_seconds = durationParts("delay");
    } else if (mode === "run_at") {
      if (!els.runAt?.value) throw new Error("run time required");
      payload.run_at = new Date(els.runAt.value).toISOString();
    } else if (mode === "interval") {
      const seconds = durationParts("interval");
      if (seconds < 60) throw new Error("interval must be at least 60 seconds");
      payload.interval_seconds = seconds;
      if (!payload.max_runs) delete payload.max_runs;
    } else if (mode === "cron") {
      payload.cron = els.cron?.value.trim() || "";
      if (!payload.cron) throw new Error("cron expression required");
      if (!payload.max_runs) delete payload.max_runs;
    }
    if (!payload.prompt) throw new Error("task required");
    if (mode === "delay" && payload.delay_seconds <= 0) throw new Error("timer must be greater than zero");
    return payload;
  }

  async function loadCapabilities() {
    state.capabilities = await fetchJson("/ui/api/agent-tasks/capabilities");
    renderTaskTypes();
    renderTools();
  }

  async function loadModels() {
    try {
      const payload = await fetchJson("/ui/api/models");
      state.models = Array.isArray(payload.data) ? payload.data : [];
      renderModels();
    } catch (error) {
      setStatus(`Failed to load models: ${error.message}`, true);
    }
  }

  async function loadTasks({ keepSelection = true } = {}) {
    const status = els.statusFilter?.value || "";
    const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
    const payload = await fetchJson(`/ui/api/agent-tasks${suffix}`);
    state.tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    if (!keepSelection || !state.tasks.some((task) => task.id === state.selectedId)) {
      state.selectedId = state.tasks[0]?.id || "";
    }
    renderTasks();
    if (state.selectedId) await selectTask(state.selectedId, { rerenderList: false });
  }

  async function selectTask(id, { rerenderList = true } = {}) {
    state.selectedId = id || "";
    const task = state.tasks.find((item) => item.id === state.selectedId);
    renderDetail(task || null);
    if (rerenderList) renderTasks();
    if (task) await loadRuns(task.id);
  }

  async function loadRuns(id = state.selectedId) {
    if (!id) return;
    const payload = await fetchJson(`/ui/api/agent-tasks/${encodeURIComponent(id)}/runs`);
    if (payload.task) {
      state.tasks = state.tasks.map((task) => (task.id === id ? payload.task : task));
      renderDetail(payload.task);
      renderTasks();
    }
    renderRuns(payload.runs || []);
  }

  async function createTask(event) {
    event.preventDefault();
    try {
      setStatus("Creating task...");
      const payload = createPayload();
      const result = await fetchJson("/ui/api/agent-tasks", { method: "POST", body: JSON.stringify(payload) });
      state.selectedId = result.task?.id || "";
      setStatus("Task created.");
      els.form?.reset();
      if (els.delayMinutes) els.delayMinutes.value = "10";
      if (els.maxTurns) els.maxTurns.value = "20";
      if (els.maxRuntime) els.maxRuntime.value = "300";
      updateScheduleFields();
      renderTools();
      await loadTasks({ keepSelection: true });
    } catch (error) {
      setStatus(error.message || String(error), true, error.stack || error);
    }
  }

  async function cancelSelected() {
    if (!state.selectedId) return;
    try {
      setStatus("Cancelling future runs...");
      const payload = await fetchJson(`/ui/api/agent-tasks/${encodeURIComponent(state.selectedId)}/cancel`, { method: "POST" });
      if (payload.task) state.tasks = state.tasks.map((task) => (task.id === state.selectedId ? payload.task : task));
      setStatus("Task cancelled.");
      renderTasks();
      renderDetail(payload.task || state.tasks.find((task) => task.id === state.selectedId));
    } catch (error) {
      setStatus(error.message || String(error), true, error.stack || error);
    }
  }

  function resetForm() {
    els.form?.reset();
    if (els.delayMinutes) els.delayMinutes.value = "10";
    if (els.maxTurns) els.maxTurns.value = "20";
    if (els.maxRuntime) els.maxRuntime.value = "300";
    updateScheduleFields();
    renderTools();
  }

  async function init() {
    updateScheduleFields();
    els.scheduleMode?.addEventListener("change", updateScheduleFields);
    els.tier?.addEventListener("change", renderTools);
    els.form?.addEventListener("submit", createTask);
    els.resetForm?.addEventListener("click", resetForm);
    els.refresh?.addEventListener("click", () => loadTasks({ keepSelection: true }).catch((error) => setStatus(error.message, true)));
    els.statusFilter?.addEventListener("change", () => loadTasks({ keepSelection: false }).catch((error) => setStatus(error.message, true)));
    els.refreshRuns?.addEventListener("click", () => loadRuns().catch((error) => setStatus(error.message, true)));
    els.cancelTask?.addEventListener("click", cancelSelected);

    try {
      setStatus("Loading scheduled tasks...");
      await loadCapabilities();
      await loadModels();
      await loadTasks({ keepSelection: false });
      setStatus("");
    } catch (error) {
      setStatus(error.message || String(error), true, error.stack || error);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
