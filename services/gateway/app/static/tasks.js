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
    runNowTask: document.getElementById("runNowTask"),
    cancelTask: document.getElementById("cancelTask"),
    taskEditModal: document.getElementById("taskEditModal"),
    taskEditTitle: document.getElementById("taskEditTitle"),
    taskEditHint: document.getElementById("taskEditHint"),
    closeTaskEditModal: document.getElementById("closeTaskEditModal"),
    cancelTaskEdit: document.getElementById("cancelTaskEdit"),
    saveTaskEdit: document.getElementById("saveTaskEdit"),
    taskEditToolsSection: document.getElementById("taskEditToolsSection"),
    taskEditTools: document.getElementById("taskEditTools"),
    taskEditModelSection: document.getElementById("taskEditModelSection"),
    taskEditModel: document.getElementById("taskEditModel"),
  };

  const state = {
    capabilities: null,
    models: [],
    tasks: [],
    selectedId: "",
    taskEdit: null,
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

  function badgeButton(text, cls = "", onClick = null, title = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `badge ${cls}`;
    button.textContent = text;
    if (title) button.title = title;
    if (typeof onClick === "function") button.addEventListener("click", onClick);
    return button;
  }

  function modelOptionLabel(model) {
    if (!model || typeof model !== "object") return "";
    const id = String(model.id || "").trim();
    const explicit = String(model.label || "").trim();
    if (explicit) return explicit;
    const target = String(model.resolved_model || model.upstream_model || "").trim();
    const backend = String(model.backend || model.backend_class || "").trim();
    if ((model.is_alias || model.is_runtime_selector) && target && backend) return `${id} -> ${target} (${backend})`;
    if ((model.is_alias || model.is_runtime_selector) && target) return `${id} -> ${target}`;
    return id;
  }

  function canonicalizeTaskModelId(value) {
    const raw = String(value || "").trim();
    const normalized = raw.toLowerCase().replace(/-/g, "_");
    if (!normalized) return raw;
    if (["mlx", "local_mlx", "mlx_default"].includes(normalized)) return "mlx";
    if (["vllm", "local_vllm", "vllm_default", "ollama", "ollama_default"].includes(normalized)) return "vllm";
    if (["vllm_fast", "local_vllm_fast"].includes(normalized)) return "vllm_fast";
    return raw;
  }

  function orderedModelEntries(current = "") {
    const currentId = canonicalizeTaskModelId(current);
    const preferred = ["default", "fast", "reasoning", "coder", "long"];
    const byId = new Map();
    for (const model of state.models || []) {
      const id = String(model?.id || "").trim();
      if (!id || byId.has(id)) continue;
      byId.set(id, model);
    }
    const ordered = [];
    for (const id of preferred) {
      if (byId.has(id)) {
        ordered.push(byId.get(id));
        byId.delete(id);
      }
    }
    if (currentId && byId.has(currentId)) {
      ordered.push(byId.get(currentId));
      byId.delete(currentId);
    }
    const remaining = Array.from(byId.values()).sort((a, b) => String(a.id || "").localeCompare(String(b.id || "")));
    ordered.push(...remaining);
    if (currentId && !ordered.some((item) => String(item?.id || "") === currentId)) {
      ordered.push({ id: currentId, label: currentId });
    }
    return ordered;
  }

  function updateTaskInState(task) {
    if (!task?.id) return;
    state.tasks = state.tasks.map((item) => (item.id === task.id ? task : item));
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
      if (els.runNowTask) els.runNowTask.disabled = true;
      if (els.cancelTask) els.cancelTask.disabled = true;
      return;
    }
    if (els.detailEmpty) els.detailEmpty.hidden = true;
    if (els.detail) els.detail.hidden = false;
    if (els.runNowTask) els.runNowTask.disabled = ["running"].includes(String(task.status));
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
      const status = String(task.status || "unknown");
      if (["enabled", "disabled"].includes(status)) {
        els.detailBadges.appendChild(badgeButton(status, status, toggleSelectedEnabled, `${status === "enabled" ? "Disable" : "Enable"} task`));
      } else {
        els.detailBadges.appendChild(statusBadge(status));
      }
      if (task.next_run_ts) els.detailBadges.appendChild(badgeButton(`next ${fmtTs(task.next_run_ts)}`, "enabled", editSelectedNextRun, "Edit next run time"));
      if (task.max_runs) els.detailBadges.appendChild(badge(`max ${task.max_runs}`));
      const modelId = canonicalizeTaskModelId(String(task.metadata?.model || "default"));
      const modelEntry = (state.models || []).find((item) => String(item?.id || "") === modelId);
      els.detailBadges.appendChild(badgeButton(modelOptionLabel(modelEntry) || modelId, "", editSelectedModel, "Edit task model"));
      const tools = task.metadata?.tools;
      if (Array.isArray(tools)) els.detailBadges.appendChild(badgeButton(`${tools.length} tools`, "", editSelectedTools, "Edit allowed tools"));
    }
    if (els.detailPrompt) els.detailPrompt.textContent = task.prompt || "";
  }

  function renderToolList(target, tier, selected = []) {
    if (!target || !state.capabilities) return;
    const current = new Set(Array.isArray(selected) ? selected : []);
    const visibleTools = (state.capabilities.tools || [])
      .filter((tool) => Number(tool.tier) <= Number(tier || 0))
      .sort((a, b) => {
        const aDefault = a.default_enabled === false ? 0 : 1;
        const bDefault = b.default_enabled === false ? 0 : 1;
        return (bDefault - aDefault) || String(a.name || "").localeCompare(String(b.name || ""));
      });
    target.innerHTML = "";
    for (const tool of visibleTools) {
      const label = document.createElement("label");
      label.className = "tool";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = tool.name;
      input.checked = current.has(tool.name);
      const text = document.createElement("span");
      text.textContent = tool.name;
      const details = [];
      if (tool.category) details.push(tool.category);
      if (tool.description) details.push(tool.description);
      if (tool.default_enabled === false && tool.default_reason) details.push(tool.default_reason);
      if (details.length) {
        const small = document.createElement("small");
        small.textContent = details.join(" - ");
        text.appendChild(small);
      }
      label.appendChild(input);
      label.appendChild(text);
      target.appendChild(label);
    }
  }

  function populateModelSelect(target, current = "default") {
    if (!target) return;
    target.innerHTML = "";
    const currentId = canonicalizeTaskModelId(current || "default");
    const seen = new Set();
    const add = (id, label) => {
      const value = String(id || "").trim();
      if (!value || seen.has(value)) return;
      seen.add(value);
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label || value;
      target.appendChild(opt);
    };
    for (const model of orderedModelEntries(currentId)) add(model.id, modelOptionLabel(model) || model.id);
    if (!seen.has(currentId)) add(currentId, currentId);
    target.value = seen.has(currentId) ? currentId : "default";
    window.NexusSelectMarquee?.refresh(target);
  }

  function closeTaskEditModal() {
    state.taskEdit = null;
    if (els.taskEditModal) els.taskEditModal.hidden = true;
    if (els.taskEditToolsSection) els.taskEditToolsSection.hidden = true;
    if (els.taskEditModelSection) els.taskEditModelSection.hidden = true;
  }

  function openTaskEditModal(config) {
    state.taskEdit = config;
    if (els.taskEditTitle) els.taskEditTitle.textContent = config.title || "Edit task";
    if (els.taskEditHint) els.taskEditHint.textContent = config.hint || "";
    if (els.taskEditToolsSection) els.taskEditToolsSection.hidden = config.mode !== "tools";
    if (els.taskEditModelSection) els.taskEditModelSection.hidden = config.mode !== "model";
    if (config.mode === "tools") renderToolList(els.taskEditTools, config.tier, config.selectedTools || []);
    if (config.mode === "model") populateModelSelect(els.taskEditModel, config.model || "default");
    if (els.taskEditModal) els.taskEditModal.hidden = false;
  }

  function parseUserRunAtInput(raw) {
    const value = String(raw || "").trim();
    if (!value) return "";
    if (/^\d+$/.test(value)) return value;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) throw new Error("invalid date/time");
    return parsed.toISOString();
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
    const current = canonicalizeTaskModelId(els.model.value);
    els.model.innerHTML = "";
    const seen = new Set();
    const add = (id, label) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = label || id;
      els.model.appendChild(opt);
    };
    for (const model of orderedModelEntries(current || "default")) add(model.id, modelOptionLabel(model) || model.id);
    if (current && seen.has(current)) els.model.value = current;
    else els.model.value = "default";
  }

  function renderTools() {
    if (!els.tools || !state.capabilities) return;
    const tier = Number(els.tier?.value || 0);
    const existing = new Set(Array.from(els.tools.querySelectorAll("input:checked")).map((el) => el.value));
    const selected = existing.size
      ? Array.from(existing)
      : (state.capabilities.tools || [])
        .filter((tool) => Number(tool.tier) <= tier && tool.default_enabled !== false)
        .map((tool) => tool.name);
    renderToolList(els.tools, tier, selected);
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

  function selectedModalTools() {
    return Array.from(els.taskEditTools?.querySelectorAll("input:checked") || []).map((el) => el.value);
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

  async function persistTaskDetailUpdate(message, request) {
    try {
      setStatus(message);
      const payload = await request();
      if (payload.task) updateTaskInState(payload.task);
      renderTasks();
      renderDetail(payload.task || state.tasks.find((task) => task.id === state.selectedId));
      closeTaskEditModal();
      return payload;
    } catch (error) {
      setStatus(error.message || String(error), true, error.stack || error);
      throw error;
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

  async function toggleSelectedEnabled() {
    if (!state.selectedId) return;
    try {
      const task = state.tasks.find((item) => item.id === state.selectedId);
      const status = String(task?.status || "");
      if (!status || !["enabled", "disabled"].includes(status)) return;
      setStatus(status === "enabled" ? "Disabling task..." : "Enabling task...");
      const payload = await fetchJson(`/ui/api/agent-tasks/${encodeURIComponent(state.selectedId)}/toggle-enabled`, { method: "POST" });
      if (payload.task) state.tasks = state.tasks.map((item) => (item.id === state.selectedId ? payload.task : item));
      setStatus(status === "enabled" ? "Task disabled." : "Task enabled.");
      renderTasks();
      renderDetail(payload.task || state.tasks.find((item) => item.id === state.selectedId));
    } catch (error) {
      setStatus(error.message || String(error), true, error.stack || error);
    }
  }

  async function editSelectedNextRun() {
    if (!state.selectedId) return;
    const task = state.tasks.find((item) => item.id === state.selectedId);
    if (!task?.next_run_ts) return;
    const current = new Date(Number(task.next_run_ts) * 1000).toISOString();
    const raw = window.prompt("Enter the next run time. Examples: 2026-05-10T09:30, 2026-05-10T09:30Z, or a Unix timestamp.", current);
    if (raw == null) return;
    try {
      const runAt = parseUserRunAtInput(raw);
      if (!runAt) return;
      setStatus("Updating next run time...");
      const payload = await fetchJson(`/ui/api/agent-tasks/${encodeURIComponent(state.selectedId)}/next-run`, {
        method: "POST",
        body: JSON.stringify({ run_at: runAt }),
      });
      if (payload.task) state.tasks = state.tasks.map((item) => (item.id === state.selectedId ? payload.task : item));
      setStatus("Next run time updated.");
      renderTasks();
      renderDetail(payload.task || state.tasks.find((item) => item.id === state.selectedId));
    } catch (error) {
      setStatus(error.message || String(error), true, error.stack || error);
    }
  }

  async function editSelectedTools() {
    if (!state.selectedId) return;
    const task = state.tasks.find((item) => item.id === state.selectedId);
    const meta = task?.metadata || {};
    openTaskEditModal({
      mode: "tools",
      title: "Edit task tools",
      hint: `Update the allowed tools for this scheduled task. Tier ${Number(meta.tier || 0)} tools only.`,
      tier: Number(meta.tier || 0),
      selectedTools: Array.isArray(meta.tools) ? meta.tools : [],
    });
  }

  async function editSelectedModel() {
  const taskId = document.querySelector('#edit-button').dataset.taskId;
  if (taskId) {
    await fetchJson('/api/tasks/edit', { method: 'POST', body: JSON.stringify({ id: taskId }) });
    await fetchTasks();
  }
  document.getElementById('edit-button').addEventListener('click', editSelectedModel);
    if (!state.selectedId) return;
    const task = state.tasks.find((item) => item.id === state.selectedId);
    const meta = task?.metadata || {};
    openTaskEditModal({
      mode: "model",
      title: "Edit task model",
      hint: "Choose which model future runs of this task should use.",
      model: String(meta.model || "default"),
    });
  }

  async function runSelectedNow() {
    if (!state.selectedId) return;
    try {
      setStatus("Queueing task to run now...");
      const payload = await fetchJson(`/ui/api/agent-tasks/${encodeURIComponent(state.selectedId)}/run-now`, { method: "POST" });
      if (payload.task) state.tasks = state.tasks.map((task) => (task.id === state.selectedId ? payload.task : task));
      setStatus("Task queued to run now.");
      renderTasks();
      renderDetail(payload.task || state.tasks.find((task) => task.id === state.selectedId));
      await loadRuns(state.selectedId);
    } catch (error) {
      setStatus(error.message || String(error), true, error.stack || error);
    }
  }

  async function saveTaskEdit() {
    if (!state.selectedId || !state.taskEdit) return;
    if (state.taskEdit.mode === "tools") {
      const tools = selectedModalTools();
      await persistTaskDetailUpdate("Updating task tools...", () => fetchJson(`/ui/api/agent-tasks/${encodeURIComponent(state.selectedId)}/tools`, {
        method: "POST",
        body: JSON.stringify({ tools }),
      }));
      setStatus("Task tools updated.");
      return;
    }
    if (state.taskEdit.mode === "model") {
      const model = String(els.taskEditModel?.value || "").trim();
      if (!model) {
        setStatus("model is required", true);
        return;
      }
      await persistTaskDetailUpdate("Updating task model...", () => fetchJson(`/ui/api/agent-tasks/${encodeURIComponent(state.selectedId)}/model`, {
        method: "POST",
        body: JSON.stringify({ model }),
      }));
      setStatus("Task model updated.");
    }
  }

  function resetForm() {
    els.form?.reset();
    if (els.delayMinutes) els.delayMinutes.value = "10";
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
    els.runNowTask?.addEventListener("click", runSelectedNow);
    els.cancelTask?.addEventListener("click", cancelSelected);
    els.closeTaskEditModal?.addEventListener("click", closeTaskEditModal);
    els.cancelTaskEdit?.addEventListener("click", closeTaskEditModal);
    els.saveTaskEdit?.addEventListener("click", () => saveTaskEdit().catch(() => {}));
    els.taskEditModal?.addEventListener("click", (event) => {
      if (event.target === els.taskEditModal) closeTaskEditModal();
    });

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
