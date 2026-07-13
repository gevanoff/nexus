(() => {
  const els = {
    status: document.getElementById("status"),
    createMode: document.getElementById("createMode"),
    createModeAgentPanel: document.getElementById("createModeAgentPanel"),
    createModeModelIntegrationPanel: document.getElementById("createModeModelIntegrationPanel"),
    repoModeTitle: document.getElementById("repoModeTitle"),
    repoModeHint: document.getElementById("repoModeHint"),
    repoUrlLabel: document.getElementById("repoUrlLabel"),
    taskPromptLabel: document.getElementById("taskPromptLabel"),
    repoUrl: document.getElementById("repoUrl"),
    baseBranch: document.getElementById("baseBranch"),
    branchName: document.getElementById("branchName"),
    taskPrompt: document.getElementById("taskPrompt"),
    createTask: document.getElementById("createTask"),
    createAndRun: document.getElementById("createAndRun"),
    modelIntegrationRepoUrl: document.getElementById("modelIntegrationRepoUrl"),
    modelIntegrationModel: document.getElementById("modelIntegrationModel"),
    modelIntegrationRuntime: document.getElementById("modelIntegrationRuntime"),
    modelIntegrationRouteKind: document.getElementById("modelIntegrationRouteKind"),
    modelIntegrationServiceName: document.getElementById("modelIntegrationServiceName"),
    modelIntegrationBranchName: document.getElementById("modelIntegrationBranchName"),
    modelIntegrationPrompt: document.getElementById("modelIntegrationPrompt"),
    createModelIntegration: document.getElementById("createModelIntegration"),
    createAndRunModelIntegration: document.getElementById("createAndRunModelIntegration"),
    modelIntegrationMeta: document.getElementById("modelIntegrationMeta"),
    agentAutoCommit: document.getElementById("agentAutoCommit"),
    configMeta: document.getElementById("configMeta"),
    refreshTasks: document.getElementById("refreshTasks"),
    tasks: document.getElementById("tasks"),
    taskCount: document.getElementById("taskCount"),
    workspaceStats: document.getElementById("workspaceStats"),
    taskSearch: document.getElementById("taskSearch"),
    taskFilter: document.getElementById("taskFilter"),
    agentMaxCycles: document.getElementById("agentMaxCycles"),
    agentMaxRuntimeMinutes: document.getElementById("agentMaxRuntimeMinutes"),
    agentContextResetCycles: document.getElementById("agentContextResetCycles"),
    selectedTitle: document.getElementById("selectedTitle"),
    selectedMeta: document.getElementById("selectedMeta"),
    selectedModelLine: document.getElementById("selectedModelLine"),
    selectedStatus: document.getElementById("selectedStatus"),
    selectedPrompt: document.getElementById("selectedPrompt"),
    workspaceModelInput: document.getElementById("workspaceModelInput"),
    workspaceModelHint: document.getElementById("workspaceModelHint"),
    saveWorkspaceModel: document.getElementById("saveWorkspaceModel"),
    trackCurrentCoderModel: document.getElementById("trackCurrentCoderModel"),
    runSelectedAgent: document.getElementById("runSelectedAgent"),
    pauseSelectedAgent: document.getElementById("pauseSelectedAgent"),
    archiveTaskBtn: document.getElementById("archiveTaskBtn"),
    purgeTaskBtn: document.getElementById("purgeTaskBtn"),
    workspaceChat: document.getElementById("workspaceChat"),
    workspaceChatInput: document.getElementById("workspaceChatInput"),
    workspaceChatMeta: document.getElementById("workspaceChatMeta"),
    workspaceChatStatus: document.getElementById("workspaceChatStatus"),
    sendWorkspaceMessage: document.getElementById("sendWorkspaceMessage"),
    statusBtn: document.getElementById("statusBtn"),
    diffBtn: document.getElementById("diffBtn"),
    briefBtn: document.getElementById("briefBtn"),
    agentStatus: document.getElementById("agentStatus"),
    agentMeta: document.getElementById("agentMeta"),
    agentLog: document.getElementById("agentLog"),
    planProgressText: document.getElementById("planProgressText"),
    planProgressBar: document.getElementById("planProgressBar"),
    planGoal: document.getElementById("planGoal"),
    projectPlan: document.getElementById("projectPlan"),
    planNote: document.getElementById("planNote"),
    runHistory: document.getElementById("runHistory"),
    runHistoryMeta: document.getElementById("runHistoryMeta"),
    publishFeedback: document.getElementById("publishFeedback"),
    commandInput: document.getElementById("commandInput"),
    commandCwd: document.getElementById("commandCwd"),
    runCommand: document.getElementById("runCommand"),
    commitMessage: document.getElementById("commitMessage"),
    commitBtn: document.getElementById("commitBtn"),
    pushBtn: document.getElementById("pushBtn"),
    prTitle: document.getElementById("prTitle"),
    prBody: document.getElementById("prBody"),
    prBtn: document.getElementById("prBtn"),
    filesPanel: document.getElementById("filesPanel"),
    changeSummary: document.getElementById("changeSummary"),
    workspaceChanges: document.getElementById("workspaceChanges"),
    pendingChanges: document.getElementById("pendingChanges"),
    treePath: document.getElementById("treePath"),
    loadTree: document.getElementById("loadTree"),
    fileList: document.getElementById("fileList"),
    filePath: document.getElementById("filePath"),
    fileContent: document.getElementById("fileContent"),
    readFile: document.getElementById("readFile"),
    writeFile: document.getElementById("writeFile"),
    output: document.getElementById("output"),
    outputTitle: document.getElementById("outputTitle"),
    copyOutput: document.getElementById("copyOutput"),
  };

  const state = {
    config: null,
    modelDefaults: null,
    modelCatalog: null,
    modelCatalogLoadedAt: 0,
    tasks: [],
    selectedId: "",
    createMode: "agent",
    busy: false,
    pollTimer: null,
    outputHistory: [],
    diffSummary: null,
    changeSummary: null,
    lastTreePayload: null,
    taskSearch: "",
    taskFilter: "all",
  };

  const STORAGE_PREFIX = "nexus.coding.v2";

  const CREATE_MODE_PROFILES = {
    agent: {
      title: "New Agent Run",
      hint: "Create a standard repository-backed coding workspace for implementation work.",
      repoLabel: "Repository",
      promptLabel: "Task brief",
      promptPlaceholder: "Describe the coding task",
      defaultPrompt: () => "",
    },
    review_audit: {
      title: "New Review or Audit Run",
      hint: "Seed a repository workspace for code review, regression hunting, change audit, or deployment-risk analysis.",
      repoLabel: "Repository or PR checkout",
      promptLabel: "Review scope",
      promptPlaceholder: "What should be reviewed or audited? Leave blank to use the default review brief.",
      defaultPrompt: () => [
        "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",
        "Prioritize concrete findings over summaries.",
        "Inspect relevant diffs, changed files, and targeted checks before concluding.",
      ].join(" "),
    },
    ops_diagnostics: {
      title: "New Ops or Diagnostics Sandbox",
      hint: "Seed a repository workspace for runtime investigation, smoke tests, configuration drift checks, topology review, or deployment diagnostics.",
      repoLabel: "Repository or ops repo",
      promptLabel: "Investigation brief",
      promptPlaceholder: "What should be investigated? Leave blank to use the default diagnostics brief.",
      defaultPrompt: () => [
        "Investigate this workspace for operational issues.",
        "Focus on runtime health, smoke tests, config drift, logs, topology/resource alignment, and actionable remediation steps.",
        "If you identify a clear, low-risk repository fix, implement the smallest viable change and validate it instead of stopping at diagnosis alone.",
      ].join(" "),
    },
  };

  function setStatus(text, isError) {
    if (!els.status) return;
    els.status.textContent = text || "";
    els.status.className = isError ? "hint status error" : "hint status";
  }

  function handle401(resp) {
    if (resp && resp.status === 401) {
      const back = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/ui/login?next=${back}`;
      return true;
    }
    return false;
  }

  function setBusy(value) {
    state.busy = !!value;
    document.querySelectorAll("button").forEach((button) => {
      if (button.id === "copyOutput") return;
      if (button.closest(".focused-nav-wrap")) return;
      button.disabled = state.busy;
    });
    if (!state.busy) {
      renderTasks();
      renderSelected();
    }
  }

  async function fetchJson(url, options) {
    const resp = await fetch(url, {
      credentials: "same-origin",
      ...(options || {}),
      headers: {
        ...(options && options.headers ? options.headers : {}),
      },
    });
    if (handle401(resp)) throw new Error("authentication required");
    const text = await resp.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (error) {
      payload = { raw: text };
    }
    if (!resp.ok) {
      const detail = payload && payload.detail ? payload.detail : payload && payload.raw ? payload.raw : `HTTP ${resp.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return payload;
  }

  function fmtTime(ts) {
    const value = Number(ts || 0);
    if (!Number.isFinite(value) || value <= 0) return "";
    try {
      return new Date(value * 1000).toLocaleString();
    } catch (error) {
      return "";
    }
  }

  function fmtDuration(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds || 0)));
    if (!Number.isFinite(value) || value <= 0) return "0s";
    const days = Math.floor(value / 86400);
    const hours = Math.floor((value % 86400) / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const secs = value % 60;
    const parts = [];
    if (days) parts.push(`${days}d`);
    if (hours) parts.push(`${hours}h`);
    if (minutes && parts.length < 2) parts.push(`${minutes}m`);
    if (!parts.length) parts.push(`${secs}s`);
    return parts.join(" ");
  }

  function storageGet(key, fallback = "") {
    try {
      const value = window.localStorage.getItem(`${STORAGE_PREFIX}.${key}`);
      return value === null ? fallback : value;
    } catch (error) {
      return fallback;
    }
  }

  function storageSet(key, value) {
    try {
      window.localStorage.setItem(`${STORAGE_PREFIX}.${key}`, String(value === undefined || value === null ? "" : value));
    } catch (error) {
      // Persistence is best-effort when storage is disabled.
    }
  }

  function fmtRelativeTime(ts) {
    const value = Number(ts || 0);
    if (!Number.isFinite(value) || value <= 0) return "";
    const delta = Math.round(value - Date.now() / 1000);
    const abs = Math.abs(delta);
    const units = abs >= 86400 ? [86400, "day"] : abs >= 3600 ? [3600, "hour"] : abs >= 60 ? [60, "minute"] : [1, "second"];
    const amount = Math.round(delta / units[0]);
    try {
      return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(amount, units[1]);
    } catch (error) {
      return fmtTime(ts);
    }
  }

  function taskNeedsAttention(task) {
    const taskStatus = String((task && task.status) || "").toLowerCase();
    const agentStatus = String(agentInfo(task).status || "").toLowerCase();
    return taskStatus === "error" || ["failed", "paused", "interrupted", "stopped"].includes(agentStatus);
  }

  function filteredTasks() {
    const query = String(state.taskSearch || "").trim().toLowerCase();
    const filter = String(state.taskFilter || "all");
    return (state.tasks || []).filter((task) => {
      const agentStatus = String(agentInfo(task).status || "").toLowerCase();
      const taskStatus = String(task.status || "").toLowerCase();
      if (filter === "active" && !agentIsActive(task)) return false;
      if (filter === "attention" && !taskNeedsAttention(task)) return false;
      if (filter === "ready" && taskStatus !== "ready") return false;
      if (filter === "completed" && agentStatus !== "completed") return false;
      if (!query) return true;
      const haystack = [
        task.id,
        taskTitle(task),
        task.repo_url,
        task.branch_name,
        task.base_branch,
        task.prompt,
        task.coding_model,
        agentInfo(task).summary,
      ].map((value) => String(value || "").toLowerCase()).join("\n");
      return haystack.includes(query);
    });
  }

  function renderWorkspaceStats() {
    if (!els.workspaceStats) return;
    const all = state.tasks || [];
    const counts = {
      active: all.filter(agentIsActive).length,
      attention: all.filter(taskNeedsAttention).length,
      completed: all.filter((task) => String(agentInfo(task).status || "").toLowerCase() === "completed").length,
    };
    els.workspaceStats.innerHTML = "";
    [
      ["active", counts.active, "running"],
      ["attention", counts.attention, "attention"],
      ["completed", counts.completed, ""],
    ].forEach(([label, value, cls]) => {
      const item = document.createElement("span");
      item.className = `workspace-stat ${cls}`.trim();
      item.textContent = `${value} ${label}`;
      els.workspaceStats.appendChild(item);
    });
  }

  function shortCommit(value) {
    const text = String(value || "").trim();
    return text ? text.slice(0, 12) : "";
  }

  function badgeClass(status) {
    const value = String(status || "").toLowerCase();
    if (value === "ready" || value === "completed") return "ready";
    if (value === "error" || value === "failed" || value === "blocked" || value === "interrupted") return "error";
    if (value === "running" || value === "queued" || value === "stopping" || value === "pausing") return "running";
    return "pending";
  }

  function taskIntegration(task) {
    return task && task.integration && typeof task.integration === "object" ? task.integration : null;
  }

  function taskTitle(task) {
    const integration = taskIntegration(task);
    if (integration) {
      return integration.display_name || integration.service_name || task.branch_name || task.id;
    }
    return task.branch_name || task.id;
  }

  function setSelectOptions(select, options, selectedValue) {
    if (!select) return;
    select.innerHTML = "";
    (options || []).forEach((entry) => {
      const option = document.createElement("option");
      if (entry && typeof entry === "object") {
        option.value = entry.value;
        option.textContent = entry.label;
      } else {
        option.value = String(entry || "");
        option.textContent = String(entry || "");
      }
      select.appendChild(option);
    });
    if (selectedValue !== undefined && selectedValue !== null) select.value = String(selectedValue);
  }

  function codingModelConfig() {
    return state.modelCatalog && typeof state.modelCatalog === "object"
      ? state.modelCatalog
      : {};
  }

  function workspaceModelValue(task) {
    return String((task && task.coding_model) || "").trim() || "coder";
  }

  function compactModelName(value) {
    const text = String(value || "").trim();
    if (!text) return "unknown";
    const tail = text.includes("/") ? text.split("/").filter(Boolean).slice(-1)[0] : text;
    return tail.length > 34 ? `...${tail.slice(-31)}` : tail;
  }

  function workspaceModelIdentity(task) {
    const agent = agentInfo(task);
    const policy = task && task.model_policy && typeof task.model_policy === "object" ? task.model_policy : null;
    const selected = String((policy && policy.selected_model) || workspaceModelValue(task) || "coder").trim() || "coder";
    const resolved = String((policy && policy.resolved_model) || agent.upstream_model || selected).trim();
    const backend = String(agent.backend || (policy && policy.backend) || "").trim();
    const status = String((policy && policy.status_label) || (policy && policy.status) || "").trim();
    let label = selected;
    if (policy && policy.tracks_coder) label = `coder -> ${compactModelName(resolved || "current")}`;
    else if (resolved && resolved !== selected) label = `${compactModelName(selected)} -> ${compactModelName(resolved)}`;
    else label = compactModelName(selected);
    const title = [
      `Workspace model: ${selected}`,
      resolved ? `Resolved upstream: ${resolved}` : "",
      backend ? `Backend: ${backend}` : "",
      status ? `Policy: ${status}` : "",
      policy && policy.run_policy ? `Run policy: ${policy.run_policy}` : "",
    ].filter(Boolean).join("\n");
    return { label, title, backend, resolved, status, policy };
  }

  function modelBadge(task) {
    const info = workspaceModelIdentity(task);
    const badge = document.createElement("span");
    badge.className = "badge model-badge";
    badge.textContent = info.label;
    badge.title = info.title;
    return badge;
  }

  function codingModelOptions(selectedValue) {
    const config = codingModelConfig();
    const options = Array.isArray(config.options) && config.options.length
      ? config.options.map((item) => ({ value: String(item.value || ""), label: String(item.label || item.value || "") }))
      : [{ value: "coder", label: "Track current coder" }];
    const selected = String(selectedValue || "coder").trim() || "coder";
    if (selected && !options.some((item) => item.value === selected)) {
      options.push({ value: selected, label: `Custom: ${selected}` });
    }
    return options;
  }

  function modelOptionForValue(value) {
    const selected = String(value || "coder").trim() || "coder";
    const options = Array.isArray(codingModelConfig().options) ? codingModelConfig().options : [];
    return options.find((item) => String(item && item.value || "") === selected) || null;
  }

  function modelPolicyForValue(value, task) {
    const selected = String(value || "coder").trim() || "coder";
    const taskPolicy = task && task.model_policy && typeof task.model_policy === "object" ? task.model_policy : null;
    if (taskPolicy && String(taskPolicy.selected_model || "coder") === selected) return taskPolicy;
    const config = codingModelConfig();
    const option = modelOptionForValue(selected);
    if (selected.toLowerCase() === "coder" || selected.toLowerCase() === "auto") {
      return {
        selected_model: "coder",
        resolved_model: config.current_coder_model || "",
        tracks_coder: true,
        status: "tracking",
        status_label: "Tracking current coder",
        run_policy: "immediate",
        warning: "",
      };
    }
    if (option && String(option.run_policy || "") === "idle_only") {
      const active = config.active_huge_model || "none";
      return {
        selected_model: selected,
        resolved_model: selected,
        tracks_coder: false,
        status: "idle_only",
        status_label: "Idle only",
        run_policy: "idle_only",
        warning: `This workspace is pinned to ${selected}, but the loaded coder model is ${active}. It will only run during idle periods after that huge model is loaded. Switch this workspace to coder to track the current loaded model.`,
        recommended_model: "coder",
      };
    }
    if (option && String(option.kind || "") === "alias") {
      return {
        selected_model: selected,
        resolved_model: String(option.model || option.upstream_model || selected),
        tracks_coder: false,
        status: "alias",
        status_label: "Alias",
        run_policy: String(option.run_policy || "immediate"),
        warning: "",
        backend: String(option.backend || ""),
      };
    }
    return {
      selected_model: selected,
      resolved_model: selected,
      tracks_coder: false,
      status: option && option.status ? String(option.status) : "custom",
      status_label: option && option.status ? String(option.status) : "Custom",
      run_policy: option && option.run_policy ? String(option.run_policy) : "immediate",
      warning: "",
    };
  }

  function renderWorkspaceModelOptions(task) {
    if (!els.workspaceModelInput) return;
    const selected = document.activeElement === els.workspaceModelInput
      ? String(els.workspaceModelInput.value || "coder")
      : workspaceModelValue(task);
    setSelectOptions(els.workspaceModelInput, codingModelOptions(selected), selected);
  }

  function renderWorkspaceModelHint(task) {
    if (!els.workspaceModelHint) return;
    const value = els.workspaceModelInput ? String(els.workspaceModelInput.value || "coder") : workspaceModelValue(task);
    const policy = modelPolicyForValue(value, task);
    const warning = String(policy.warning || "").trim();
    els.workspaceModelHint.hidden = !warning;
    els.workspaceModelHint.textContent = warning;
    if (els.trackCurrentCoderModel) {
      els.trackCurrentCoderModel.hidden = !warning;
      els.trackCurrentCoderModel.disabled = state.busy || !task || agentIsActive(task);
    }
  }

  function createModeProfile(mode) {
    return CREATE_MODE_PROFILES[mode] || CREATE_MODE_PROFILES.agent;
  }

  function updateRepositoryModeUi(mode) {
    const profile = createModeProfile(mode);
    if (els.repoModeTitle) els.repoModeTitle.textContent = profile.title;
    if (els.repoModeHint) els.repoModeHint.textContent = profile.hint;
    if (els.repoUrlLabel) els.repoUrlLabel.textContent = profile.repoLabel;
    if (els.taskPromptLabel) els.taskPromptLabel.textContent = profile.promptLabel;
    if (els.taskPrompt) els.taskPrompt.placeholder = profile.promptPlaceholder;
  }

  function buildModePrompt(mode, prompt) {
    const trimmed = String(prompt || "").trim();
    if (trimmed) return trimmed;
    return createModeProfile(mode).defaultPrompt();
  }

  function setCreateMode(mode) {
    const value = ["review_audit", "ops_diagnostics", "model_integration"].includes(mode) ? mode : "agent";
    state.createMode = value;
    if (els.createMode) els.createMode.value = value;
    if (els.createModeAgentPanel) els.createModeAgentPanel.hidden = value === "model_integration";
    if (els.createModeModelIntegrationPanel) els.createModeModelIntegrationPanel.hidden = value !== "model_integration";
    if (value !== "model_integration") updateRepositoryModeUi(value);
  }

  function selectedTask() {
    return state.tasks.find((task) => task && task.id === state.selectedId) || null;
  }

  function taskById(taskId) {
    const id = String(taskId || "");
    return state.tasks.find((task) => task && task.id === id) || null;
  }

  function agentInfo(task) {
    return task && task.agent && typeof task.agent === "object" ? task.agent : { status: "idle", events: [] };
  }

  function agentIsActive(task) {
    const status = String(agentInfo(task).status || "").toLowerCase();
    return status === "queued" || status === "running" || status === "stopping" || status === "pausing";
  }

  function setOutput(title, value) {
    if (els.outputTitle) els.outputTitle.textContent = title || "";
    if (!els.output) return;
    let body = "";
    if (typeof value === "string") {
      body = value;
    } else {
      body = JSON.stringify(value, null, 2);
    }
    const time = new Date().toLocaleTimeString();
    state.outputHistory.push({ title: title || "output", body, time });
    state.outputHistory = state.outputHistory.slice(-40);
    els.output.textContent = state.outputHistory
      .map((item) => `# ${item.title} @ ${item.time}\n${item.body || ""}`.trim())
      .join("\n\n---\n\n");
    els.output.scrollTop = els.output.scrollHeight;
  }

  async function runTaskButtonAction(event, action) {
    event.stopPropagation();
    try {
      setStatus("");
      await action();
    } catch (error) {
      setStatus(String(error && error.message ? error.message : error), true);
    }
  }

  function setPublishFeedback(text, kind) {
    if (!els.publishFeedback) return;
    els.publishFeedback.textContent = text || "No publish action yet.";
    els.publishFeedback.className = `publish-feedback ${kind || ""}`.trim();
  }

  function resultText(result) {
    if (!result || typeof result !== "object") return "";
    const bits = [];
    if (Array.isArray(result.argv)) bits.push(`$ ${result.argv.join(" ")}`);
    if (result.returncode !== undefined && result.returncode !== null) bits.push(`returncode: ${result.returncode}`);
    if (result.duration_ms !== undefined) bits.push(`duration_ms: ${result.duration_ms}`);
    if (result.stdout) bits.push(`\nstdout:\n${result.stdout}`);
    if (result.stderr) bits.push(`\nstderr:\n${result.stderr}`);
    if (!bits.length) return JSON.stringify(result, null, 2);
    return bits.join("\n");
  }

  function pushPermissionHint(result) {
    const stderr = String((result && result.stderr) || "");
    const stdout = String((result && result.stdout) || "");
    const text = `${stderr}\n${stdout}`.toLowerCase();
    if ((text.includes("permission to") && text.includes("denied")) || text.includes("returned error: 403")) {
      return "GitHub rejected the push. Update your saved GitHub token in User Settings with write access to this repository, typically Contents: read/write and Pull requests: read/write for gevanoff/nexus.";
    }
    return "";
  }

  function commandOk(result) {
    return !!(result && typeof result === "object" && result.ok);
  }

  function parseArgv(input) {
    const text = String(input || "").trim();
    if (!text) return [];
    const out = [];
    let current = "";
    let quote = "";
    let escaping = false;
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      if (escaping) {
        current += ch;
        escaping = false;
        continue;
      }
      if (ch === "\\") {
        escaping = true;
        continue;
      }
      if (quote) {
        if (ch === quote) {
          quote = "";
        } else {
          current += ch;
        }
        continue;
      }
      if (ch === "'" || ch === '"') {
        quote = ch;
        continue;
      }
      if (/\s/.test(ch)) {
        if (current) {
          out.push(current);
          current = "";
        }
        continue;
      }
      current += ch;
    }
    if (escaping) current += "\\";
    if (quote) throw new Error("Unclosed quote in command");
    if (current) out.push(current);
    return out;
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function highlightAgentLine(line) {
    const raw = String(line || "");
    const timeMatch = raw.match(/^(\d{1,2}:\d{2}:\d{2}(?:\s?[AP]M)?)(.*)$/);
    let head = "";
    let rest = raw;
    if (timeMatch) {
      head = `<span class="agent-ts">${escapeHtml(timeMatch[1])}</span>`;
      rest = timeMatch[2] || "";
    }
    if (/^\s*thinking\b/i.test(rest)) {
      return `${head}<span class="agent-thinking">${escapeHtml(rest)}</span>`;
    }
    const tokenRe = /(coding_[A-Za-z0-9_]+|function=[A-Za-z_][A-Za-z0-9_]*|[A-Za-z]:[\\/][A-Za-z0-9._ -]+(?:[\\/][A-Za-z0-9._ -]+)+|(?:^|[\s"'=])(?:\.{0,2}\/)?[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)+(?:[A-Za-z0-9._-])|(?:backend|model|cycle|ok|path|cwd|argv|returncode|summary|error|status)=)/g;
    let out = head;
    let last = 0;
    for (const match of rest.matchAll(tokenRe)) {
      const token = match[0];
      const index = Number(match.index || 0);
      out += escapeHtml(rest.slice(last, index));
      const lead = token.match(/^\s+/);
      const prefix = lead ? lead[0] : "";
      const value = token.slice(prefix.length);
      let cls = "agent-key";
      if (value.startsWith("coding_") || value.startsWith("function=")) cls = "agent-fn";
      else if (value.includes("/") || value.includes("\\")) cls = "agent-path";
      else if (value === "error=" || value === "status=" || value === "ok=") cls = "agent-state";
      out += escapeHtml(prefix) + `<span class="${cls}">${escapeHtml(value)}</span>`;
      last = index + token.length;
    }
    out += escapeHtml(rest.slice(last));
    return out;
  }

  function highlightAgentLog(text) {
    return String(text || "").split("\n").map(highlightAgentLine).join("\n");
  }

  function renderTasks() {
    if (!els.tasks) return;
    els.tasks.innerHTML = "";
    const tasks = filteredTasks();
    const total = (state.tasks || []).length;
    if (els.taskCount) els.taskCount.textContent = tasks.length === total ? String(total) : `${tasks.length} / ${total}`;
    renderWorkspaceStats();
    if (!tasks.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = total ? "No workspaces match the current search and filter." : "No workspaces yet.";
      els.tasks.appendChild(empty);
      return;
    }
    for (const task of tasks) {
      const integration = taskIntegration(task);
      const deployment = integration && integration.deployment_target && typeof integration.deployment_target === "object"
        ? integration.deployment_target
        : null;
      const button = document.createElement("div");
      button.className = `task-item ${task.id === state.selectedId ? "active" : ""}`;
      button.setAttribute("role", "button");
      button.tabIndex = 0;
      const status = document.createElement("span");
      status.className = `badge ${badgeClass(task.status)}`;
      status.textContent = task.status || "unknown";
      const agent = agentInfo(task);
      const agentStatus = String(agent.status || "idle");
      const agentBadge = document.createElement("span");
      agentBadge.className = `badge ${badgeClass(agentStatus)}`;
      agentBadge.style.marginLeft = "6px";
      agentBadge.textContent = `agent ${agentStatus}`;
      const title = document.createElement("div");
      title.style.marginTop = "6px";
      title.style.fontWeight = "700";
      title.textContent = taskTitle(task);
      const meta = document.createElement("div");
      meta.className = "meta";
      if (integration) {
        const metaBits = [];
        if (integration.model_id) metaBits.push(integration.model_id);
        metaBits.push(`${integration.route_kind || "route"} via ${integration.runtime || "runtime"}`);
        if (deployment && deployment.host) metaBits.push(`target ${deployment.host}`);
        metaBits.push(task.branch_name || task.id || "");
        meta.textContent = metaBits.join(" | ");
      } else {
        meta.textContent = `${task.base_branch || "base"} -> ${task.id || ""}`;
      }
      const prompt = document.createElement("div");
      prompt.className = "meta";
      prompt.textContent = integration
        ? String((deployment && deployment.reason) || task.prompt || "").slice(0, 160)
        : String(task.prompt || "").slice(0, 140);
      const commit = shortCommit(task.last_commit || task.last_checkpoint_commit);
      const commitMeta = document.createElement("div");
      commitMeta.className = "meta commit-meta";
      const updated = fmtRelativeTime(task.updated_at);
      commitMeta.textContent = [commit ? `commit ${commit}` : "", updated ? `updated ${updated}` : ""].filter(Boolean).join(" | ");
      commitMeta.title = fmtTime(task.updated_at);
      button.appendChild(status);
      button.appendChild(agentBadge);
      button.appendChild(modelBadge(task));
      button.appendChild(title);
      button.appendChild(meta);
      if (commitMeta.textContent) button.appendChild(commitMeta);
      if (prompt.textContent) button.appendChild(prompt);
      const actions = document.createElement("div");
      actions.className = "task-actions";
      const runBtn = document.createElement("button");
      runBtn.type = "button";
      runBtn.className = "task-icon-btn task-run-btn";
      const previousAgentStatus = String(agent.status || "idle").toLowerCase();
      const runLabel = ["paused", "failed", "interrupted", "stopped"].includes(previousAgentStatus) ? "Continue agent for this workspace" : "Run agent for this workspace";
      runBtn.title = runLabel;
      runBtn.setAttribute("aria-label", runLabel);
      runBtn.disabled = state.busy || agentIsActive(task);
      runBtn.innerHTML = "<svg viewBox='0 0 24 24'><path d='M8 5v14l11-7z'/></svg>";
      runBtn.addEventListener("click", (event) => runTaskButtonAction(event, () => startAgentRun(task.id)));
      const stopBtn = document.createElement("button");
      stopBtn.type = "button";
      stopBtn.className = "task-icon-btn task-stop-btn";
      stopBtn.title = "Pause agent for this workspace";
      stopBtn.setAttribute("aria-label", "Pause agent for this workspace");
      stopBtn.disabled = state.busy || !agentIsActive(task);
      stopBtn.innerHTML = "<svg viewBox='0 0 24 24'><path d='M7 5h4v14H7V5zm6 0h4v14h-4V5z'/></svg>";
      stopBtn.addEventListener("click", (event) => runTaskButtonAction(event, () => stopAgentRun(task.id)));
      const archiveBtn = document.createElement("button");
      archiveBtn.type = "button";
      archiveBtn.className = "task-icon-btn task-archive-btn";
      archiveBtn.title = "Archive workspace for forensics";
      archiveBtn.setAttribute("aria-label", "Archive workspace for forensics");
      archiveBtn.innerHTML = "<svg viewBox='0 0 24 24'><path d='M4 4h16v4H4V4zm1 6h14v9a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-9zm4 2v2h6v-2H9z'/></svg>";
      archiveBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        archiveTask(task.id);
      });
      const trashBtn = document.createElement("button");
      trashBtn.type = "button";
      trashBtn.className = "task-icon-btn task-delete-btn";
      trashBtn.title = "Delete workspace";
      trashBtn.setAttribute("aria-label", "Delete workspace");
      trashBtn.innerHTML = "<svg viewBox='0 0 24 24'><path d='M3 6h18v2H3V6zm4 4v8a2 2 0 002 2h6a2 2 0 002-2V10h-2v8h-6v-8H7zm2-4h6V5a1 1 0 00-1-1h-4a1 1 0 00-1 1v1z'/></svg>";
      trashBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteTask(task.id);
      });
      actions.appendChild(runBtn);
      actions.appendChild(stopBtn);
      actions.appendChild(archiveBtn);
      actions.appendChild(trashBtn);
      button.appendChild(actions);
      button.addEventListener("click", () => selectTask(task.id));
      button.addEventListener("keydown", (event) => {
        if (event.target !== button) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectTask(task.id);
        }
      });
      els.tasks.appendChild(button);
    }
  }

  function renderSelected() {
    const task = selectedTask();
    const disabled = !task;
    const activeAgent = task ? agentIsActive(task) : false;
    [
      els.statusBtn,
      els.diffBtn,
      els.briefBtn,
      els.runCommand,
      els.commitBtn,
      els.pushBtn,
      els.archiveTaskBtn,
      els.purgeTaskBtn,
      els.prBtn,
      els.loadTree,
      els.readFile,
      els.writeFile,
      els.sendWorkspaceMessage,
      els.saveWorkspaceModel,
      els.runSelectedAgent,
      els.pauseSelectedAgent,
    ].forEach((button) => {
      if (button) button.disabled = disabled || state.busy;
    });
    if (els.workspaceChatInput) els.workspaceChatInput.disabled = disabled || state.busy;
    [els.runCommand, els.commitBtn, els.pushBtn, els.prBtn, els.writeFile].forEach((button) => {
      if (button && activeAgent) button.disabled = true;
    });
    if (!task) {
      if (els.selectedTitle) els.selectedTitle.textContent = "No workspace selected";
      if (els.selectedMeta) els.selectedMeta.textContent = "";
      if (els.selectedModelLine) els.selectedModelLine.innerHTML = "";
      if (els.selectedPrompt) els.selectedPrompt.textContent = "";
      renderWorkspaceModelOptions(null);
      renderWorkspaceModelHint(null);
      if (els.selectedStatus) {
        els.selectedStatus.className = "badge pending";
        els.selectedStatus.textContent = "idle";
      }
      renderAgent(null);
      renderWorkspaceChat(null);
      renderProjectPlan(null);
      renderRunHistory(null);
      return;
    }
    const integration = taskIntegration(task);
    const deployment = integration && integration.deployment_target && typeof integration.deployment_target === "object"
      ? integration.deployment_target
      : null;
    if (els.selectedTitle) els.selectedTitle.textContent = taskTitle(task);
    if (els.selectedMeta) {
      const bits = integration
        ? [
            integration.model_id || task.source_url || task.repo_url || "",
            `${integration.route_kind || "route"} via ${integration.runtime || "runtime"}`,
            deployment && deployment.host ? `target ${deployment.host}` : "",
            deployment && deployment.backend_display_name ? deployment.backend_display_name : "",
            `updated ${fmtTime(task.updated_at)}`,
          ].filter(Boolean)
        : [`${task.repo_url || ""}`, `base ${task.base_branch || ""}`, `updated ${fmtTime(task.updated_at)}`];
      if (task.elapsed_runtime_sec !== undefined) bits.push(`workspace runtime ${fmtDuration(task.elapsed_runtime_sec)}`);
      const policy = task.model_policy && typeof task.model_policy === "object" ? task.model_policy : null;
      const modelLabel = policy && policy.tracks_coder
        ? `coder -> ${policy.resolved_model || "current"}`
        : (task.coding_model || "coder");
      bits.push(`workspace model ${modelLabel}`);
      const commit = shortCommit(task.last_commit || task.last_checkpoint_commit);
      if (commit) bits.push(`commit ${commit}`);
      els.selectedMeta.textContent = bits.join(" | ");
    }
    if (els.selectedModelLine) {
      els.selectedModelLine.innerHTML = "";
      els.selectedModelLine.appendChild(modelBadge(task));
      const identity = workspaceModelIdentity(task);
      if (identity.backend) {
        const backendBadge = document.createElement("span");
        backendBadge.className = "badge pending";
        backendBadge.textContent = identity.backend;
        els.selectedModelLine.appendChild(backendBadge);
      }
      if (identity.status) {
        const statusBadge = document.createElement("span");
        statusBadge.className = "badge pending";
        statusBadge.textContent = identity.status;
        els.selectedModelLine.appendChild(statusBadge);
      }
    }
    renderWorkspaceModelOptions(task);
    renderWorkspaceModelHint(task);
    if (els.selectedPrompt) {
      const promptBits = [];
      if (integration && deployment && deployment.reason) promptBits.push(`${task.prompt || ""}\n\nRecommended lane: ${deployment.reason}`.trim());
      else if (task.prompt) promptBits.push(task.prompt || "");
      if (task.metadata_error && typeof task.metadata_error === "object") {
        promptBits.push(
          [
            `Metadata repair note: ${task.metadata_error.message || "Task metadata needed repair."}`,
            task.metadata_error.detail ? `Detail: ${task.metadata_error.detail}` : "",
            task.metadata_error.quarantined_path ? `Quarantined original: ${task.metadata_error.quarantined_path}` : "",
          ].filter(Boolean).join("\n")
        );
      }
      els.selectedPrompt.textContent = promptBits.join("\n\n");
    }
    if (els.selectedStatus) {
      els.selectedStatus.className = `badge ${badgeClass(task.status)}`;
      els.selectedStatus.textContent = task.status || "unknown";
    }
    if (els.commitMessage && !els.commitMessage.value) {
      els.commitMessage.value = task.prompt ? String(task.prompt).split("\n")[0].slice(0, 120) : "";
    }
    if (els.prTitle && !els.prTitle.value) {
      els.prTitle.value = task.prompt ? String(task.prompt).split("\n")[0].slice(0, 120) : task.branch_name || "";
    }
    if (els.prBody && !els.prBody.value) {
      els.prBody.value = task.prompt || "";
    }
    if (els.saveWorkspaceModel) els.saveWorkspaceModel.disabled = state.busy || activeAgent;
    if (els.trackCurrentCoderModel) els.trackCurrentCoderModel.disabled = state.busy || activeAgent;
    if (els.runSelectedAgent) {
      const previousStatus = String(agentInfo(task).status || "idle").toLowerCase();
      els.runSelectedAgent.textContent = ["paused", "failed", "interrupted", "stopped"].includes(previousStatus) ? "Continue agent" : "Run agent";
      els.runSelectedAgent.disabled = state.busy || activeAgent;
    }
    if (els.pauseSelectedAgent) els.pauseSelectedAgent.disabled = state.busy || !activeAgent;
    renderAgent(task);
    renderWorkspaceChat(task);
    renderProjectPlan(task);
    renderRunHistory(task);
  }

  function renderProjectPlan(task) {
    const plan = task && task.project_plan && typeof task.project_plan === "object" ? task.project_plan : { goal: "", items: [], counts: {} };
    const items = Array.isArray(plan.items) ? plan.items : [];
    const counts = plan.counts && typeof plan.counts === "object" ? plan.counts : {};
    const total = Number(counts.total || items.length || 0);
    const done = Number(counts.done || 0);
    const percent = total ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
    if (els.planProgressText) els.planProgressText.textContent = total ? `${done} of ${total} milestones complete` : "No milestones yet";
    if (els.planProgressBar) {
      els.planProgressBar.style.width = `${percent}%`;
      const track = els.planProgressBar.parentElement;
      if (track) track.setAttribute("aria-valuenow", String(percent));
    }
    if (els.planGoal) els.planGoal.textContent = String(plan.goal || (task && task.prompt) || "Select a workspace to view its project plan.");
    if (els.planNote) {
      const bits = [];
      if (plan.note) bits.push(String(plan.note));
      if (plan.updated_at) bits.push(`updated ${fmtRelativeTime(plan.updated_at)}${plan.updated_by ? ` by ${plan.updated_by}` : ""}`);
      els.planNote.textContent = bits.join(" | ");
      els.planNote.title = plan.updated_at ? fmtTime(plan.updated_at) : "";
    }
    if (!els.projectPlan) return;
    els.projectPlan.innerHTML = "";
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = task
        ? "The coding agent will create a durable milestone plan when the work spans several steps."
        : "No workspace selected.";
      els.projectPlan.appendChild(empty);
      return;
    }
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "plan-item";
      const status = document.createElement("span");
      status.className = `badge plan-status ${badgeClass(item.status)}`;
      status.textContent = String(item.status || "pending").replace(/_/g, " ");
      const content = document.createElement("div");
      const title = document.createElement("div");
      title.className = "plan-item-title";
      title.textContent = item.title || item.id || "Milestone";
      content.appendChild(title);
      if (item.summary) {
        const summary = document.createElement("div");
        summary.className = "plan-item-summary";
        summary.textContent = item.summary;
        content.appendChild(summary);
      }
      row.appendChild(status);
      row.appendChild(content);
      els.projectPlan.appendChild(row);
    }
  }

  function renderRunHistory(task) {
    if (!els.runHistory) return;
    const runs = task && Array.isArray(task.agent_runs) ? task.agent_runs.slice().reverse() : [];
    els.runHistory.innerHTML = "";
    if (els.runHistoryMeta) els.runHistoryMeta.textContent = runs.length ? `${runs.length} retained run${runs.length === 1 ? "" : "s"}` : "No runs yet";
    if (!runs.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = task ? "No durable run history yet." : "Select a workspace to view run history.";
      els.runHistory.appendChild(empty);
      return;
    }
    for (const run of runs.slice(0, 30)) {
      const row = document.createElement("div");
      row.className = "run-history-item";
      const status = document.createElement("span");
      status.className = `badge ${badgeClass(run.status)}`;
      status.textContent = run.status || "unknown";
      const summary = document.createElement("div");
      summary.className = "run-history-summary";
      const headline = document.createElement("div");
      headline.textContent = run.summary || run.error || run.prompt || "Run recorded";
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = [
        run.model ? `model ${run.model}` : "",
        run.cycle !== undefined ? `cycle ${run.cycle}/${run.max_cycles || "?"}` : "",
        run.duration_ms ? fmtDuration(Number(run.duration_ms) / 1000) : "",
        run.commit ? `commit ${shortCommit(run.commit)}` : "",
      ].filter(Boolean).join(" | ");
      summary.appendChild(headline);
      summary.appendChild(meta);
      const time = document.createElement("span");
      time.className = "meta";
      time.textContent = fmtRelativeTime(run.finished_at || run.started_at || run.created_at);
      time.title = fmtTime(run.finished_at || run.started_at || run.created_at);
      row.appendChild(status);
      row.appendChild(summary);
      row.appendChild(time);
      els.runHistory.appendChild(row);
    }
  }

  function workspaceConversationItems(task) {
    if (!task) return [];
    const items = [];
    const messages = Array.isArray(task.guidance_messages) ? task.guidance_messages : [];
    for (const item of messages) {
      items.push({
        ts: Number(item.ts || 0),
        role: "user",
        actor: item.actor || item.role || "user",
        content: item.content || "",
        run_id: item.run_id || "",
      });
    }
    const agent = agentInfo(task);
    const events = Array.isArray(agent.events) ? agent.events : [];
    for (const event of events) {
      const type = String(event && event.type ? event.type : "");
      if (!["completed", "failed", "paused", "stopped", "interrupted", "no_change_audit"].includes(type)) continue;
      const summary = String((event && (event.summary || event.error)) || "").trim();
      if (!summary) continue;
      items.push({
        ts: Number(event.ts || agent.finished_at || agent.last_event_at || 0),
        role: "assistant",
        actor: type === "completed" ? "Nexus Coding Agent" : `Nexus Coding Agent (${type})`,
        content: summary,
        run_id: event.run_id || agent.run_id || "",
      });
    }
    if (agentIsActive(task)) {
      const last = events.length ? events[events.length - 1] : null;
      const lastType = last && last.type ? String(last.type) : String(agent.status || "running");
      items.push({
        ts: Number((last && last.ts) || agent.last_event_at || Date.now() / 1000),
        role: "assistant",
        actor: "Nexus Coding Agent",
        content: `Working on this now. Latest state: ${lastType}.`,
        run_id: agent.run_id || "",
        transient: true,
      });
    }
    items.sort((a, b) => (a.ts || 0) - (b.ts || 0));
    return items;
  }

  function renderWorkspaceChat(task) {
    if (!els.workspaceChat) return;
    const messages = workspaceConversationItems(task);
    els.workspaceChat.innerHTML = "";
    if (els.workspaceChatMeta) {
      els.workspaceChatMeta.textContent = messages.length ? `${messages.length} message${messages.length === 1 ? "" : "s"}` : "No messages yet";
    }
    if (els.workspaceChatStatus) {
      if (!task) els.workspaceChatStatus.textContent = "";
      else if (agentIsActive(task)) els.workspaceChatStatus.textContent = `Sent messages are read by the active agent during its next work cycle. Replies appear here. Next run model: ${workspaceModelValue(task)}.`;
      else els.workspaceChatStatus.textContent = `Sending a message starts a continuation run and the agent reply appears here. Press Enter to send, Shift+Enter for a new line.`;
    }
    if (!messages.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = task ? "No workspace messages yet." : "Select a workspace to chat with its coding agent.";
      els.workspaceChat.appendChild(empty);
      return;
    }
    for (const item of messages.slice(-40)) {
      const wrap = document.createElement("div");
      wrap.className = `workspace-message ${item.role === "assistant" ? "workspace-message-assistant" : "workspace-message-user"}${item.transient ? " workspace-message-transient" : ""}`;
      const meta = document.createElement("div");
      meta.className = "workspace-message-meta";
      const actor = document.createElement("span");
      actor.textContent = item.actor || item.role || "user";
      const time = document.createElement("span");
      time.textContent = fmtTime(item.ts) || "";
      const body = document.createElement("div");
      body.className = "workspace-message-body";
      body.textContent = item.content || "";
      meta.appendChild(actor);
      meta.appendChild(time);
      wrap.appendChild(meta);
      wrap.appendChild(body);
      els.workspaceChat.appendChild(wrap);
    }
    els.workspaceChat.scrollTop = els.workspaceChat.scrollHeight;
  }

  function eventLine(event) {
    if (!event || typeof event !== "object") return "";
    const time = event.ts ? new Date(Number(event.ts) * 1000).toLocaleTimeString() : "";
    const type = String(event.type || "event");
    const cycle = event.cycle || "";
    if (type === "queued") return `${time} queued model=${event.model || ""}`;
    if (type === "started") return `${time} started backend=${event.backend || ""} model=${event.upstream_model || ""}`;
    if (type === "cycle_started") return `${time} work cycle ${cycle}`;
    if (type === "assistant") {
      const calls = Array.isArray(event.tool_calls) ? event.tool_calls.map((item) => item && item.name).filter(Boolean).join(", ") : "";
      const content = String(event.content || "").trim();
      const label = calls ? `assistant output tools=[${calls}]` : "assistant output (unverified)";
      return `${time} ${label}${content ? `\n${content}` : ""}`;
    }
    if (type === "thinking") return `${time} thinking\n${event.thinking || event.summary || ""}`;
    if (type === "tool_started") return `${time} tool ${event.name || ""} ${JSON.stringify(event.args || {})}`;
    if (type === "tool_finished") {
      const result = event.result || {};
      const ok = result && Object.prototype.hasOwnProperty.call(result, "ok") ? ` ok=${!!result.ok}` : "";
      let detail = "";
      if (result && typeof result === "object") {
        if (result.summary) detail = String(result.summary);
        else if (result.error) detail = typeof result.error === "string" ? result.error : JSON.stringify(result.error);
        else if (result.stdout) detail = String(result.stdout).slice(-1200);
        else if (result.path) detail = String(result.path);
      }
      return `${time} tool ${event.name || ""} finished${ok}${detail ? `\n${detail}` : ""}`;
    }
    if (type === "review") return `${time} reviewed status and diff`;
    if (type === "no_tool_call") return `${time} no tool call count=${event.count || ""}\n${event.summary || ""}`;
    if (type === "no_tool_call_limit") return `${time} no tool call limit\n${event.summary || ""}`;
    if (type === "no_change_audit") return `${time} no-change audit\n${event.summary || ""}`;
    if (type === "guidance_seen") return `${time} guidance seen count=${event.count || 0}\n${event.summary || ""}`;
    if (type === "semantic_reroute") return `${time} semantic reroute ${event.previous_backend || ""} -> ${event.backend || ""}\n${event.summary || ""}`;
    if (type === "backend_retry") {
      const attempt = `${event.attempt || "?"}/${event.max_retries || "?"}`;
      return `${time} backend retry cycle=${cycle} attempt=${attempt} delay=${event.delay_sec || 0}s\n${event.error || ""}`;
    }
    if (type === "checkpoint") {
      const commit = String(event.commit || "").slice(0, 12);
      const changed = event.changed ? "changed" : "clean";
      const stateText = event.ok ? "saved" : "failed";
      return `${time} checkpoint ${stateText} ${changed}${commit ? ` commit=${commit}` : ""}${event.error ? `\n${event.error}` : ""}`;
    }
    if (type === "plan_updated") return `${time} project plan updated\n${event.summary || ""}`;
    if (type === "context_reset") return `${time} context compacted cycle=${cycle} reason=${event.reason || ""}`;
    if (type === "budget_exhausted") return `${time} run horizon reached cycle=${cycle}\n${event.summary || ""}`;
    if (type === "interrupted") return `${time} interrupted\n${event.summary || ""}`;
    if (type === "commit") {
      const commit = shortCommit(event.commit || (event.result && event.result.last_commit));
      return `${time} ${event.skipped ? "commit skipped" : "committed"} ${event.message || ""}${commit ? ` commit=${commit}` : ""}${event.summary ? `\n${event.summary}` : ""}`;
    }
    if (type === "completed") return `${time} completed\n${event.summary || ""}`;
    if (type === "failed") return `${time} failed\n${event.summary || event.error || ""}`;
    if (type === "paused" || type === "stopped") return `${time} paused\n${event.summary || ""}`;
    if (type === "pause_requested" || type === "stop_requested") return `${time} pause requested`;
    return `${time} ${type} ${JSON.stringify(event)}`;
  }

  function renderAgent(task) {
    const agent = agentInfo(task);
    const status = String(agent.status || "idle");
    if (els.agentStatus) {
      els.agentStatus.className = `badge ${badgeClass(status)}`;
      els.agentStatus.textContent = status;
    }
    if (els.agentMeta) {
      const bits = [];
      if (agent.model) bits.push(`model ${agent.model}`);
      if (agent.backend) bits.push(`backend ${agent.backend}`);
      if (agent.upstream_model) bits.push(`upstream ${agent.upstream_model}`);
      if (agent.elapsed_runtime_sec !== undefined) bits.push(`run time ${fmtDuration(agent.elapsed_runtime_sec)}`);
      if (agent.cycle !== undefined) bits.push(`cycle ${agent.cycle}/${agent.max_cycles || "?"}`);
      if (agent.max_runtime_sec) bits.push(`time budget ${fmtDuration(agent.max_runtime_sec)}`);
      if (agent.context_reset_cycles) bits.push(`compact every ${agent.context_reset_cycles} cycles`);
      if (agent.last_event_at) bits.push(`updated ${fmtTime(agent.last_event_at)}`);
      if (agent.auto_commit) bits.push("auto-commit");
      const commit = shortCommit(task && (task.last_commit || task.last_checkpoint_commit));
      if (commit) bits.push(`commit ${commit}`);
      els.agentMeta.textContent = bits.join(" | ");
    }
    if (els.agentLog) {
      const lines = Array.isArray(agent.events) ? agent.events.map(eventLine).filter(Boolean) : [];
      if (agent.summary && !lines.some((line) => line.includes(agent.summary))) lines.push(`summary:\n${agent.summary}`);
      if (agent.error && status === "failed") lines.push(`error:\n${agent.error}`);
      els.agentLog.innerHTML = highlightAgentLog(lines.join("\n\n") || "No agent run yet.");
      els.agentLog.scrollTop = els.agentLog.scrollHeight;
    }
    updatePolling();
  }

  function renderChangeSummary(diffSummary, pendingSummary) {
    if (!els.changeSummary) return;
    const counts = diffSummary && diffSummary.counts && typeof diffSummary.counts === "object" ? diffSummary.counts : {};
    const pendingCounts = pendingSummary && pendingSummary.counts && typeof pendingSummary.counts === "object" ? pendingSummary.counts : {};
    const added = Number(counts.added || 0);
    const modified = Number(counts.modified || 0);
    const removed = Number(counts.removed || 0);
    const renamed = Number(counts.renamed || 0);
    const untracked = Number(counts.untracked || 0);
    const total = Number(counts.total || 0);
    const pendingTotal = Number(pendingCounts.total || 0);
    els.changeSummary.innerHTML = "";
    const addChip = (label, value, cls) => {
      const span = document.createElement("span");
      span.className = `change-chip ${cls || ""}`.trim();
      span.textContent = `${value} ${label}`;
      els.changeSummary.appendChild(span);
    };
    addChip("added", added, "added");
    addChip("modified", modified, "modified");
    addChip("removed", removed, "removed");
    if (renamed) addChip("renamed", renamed, "renamed");
    if (untracked) addChip("untracked", untracked, "untracked");
    if (pendingTotal) addChip("pending", pendingTotal, "modified");
    if (!total && !pendingTotal) {
      const clean = document.createElement("span");
      clean.className = "meta";
      clean.textContent = "clean";
      els.changeSummary.appendChild(clean);
    }
  }

  function renderWorkspaceChanges(summary) {
    if (!els.workspaceChanges) return;
    els.workspaceChanges.innerHTML = "";
    const files = Array.isArray(summary && summary.files) ? summary.files : [];
    if (!files.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "No changes relative to the base branch.";
      els.workspaceChanges.appendChild(empty);
      return;
    }
    for (const item of files.slice(0, 200)) {
      const row = document.createElement("div");
      row.className = "pending-change-row";
      const chip = document.createElement("span");
      const kind = String(item.kind || "modified");
      chip.className = `change-chip ${kind}`;
      chip.textContent = String(item.status || kind || "M");
      const targetPath = String(item.path || "");
      const displayPath = item.previous_path ? `${String(item.previous_path)} -> ${targetPath}` : targetPath;
      const removed = kind === "removed";
      const path = document.createElement(removed ? "div" : "button");
      if (!removed) path.type = "button";
      path.className = "pending-change-path";
      path.textContent = displayPath;
      if (!removed) {
        path.addEventListener("click", () => {
          if (els.filePath) els.filePath.value = targetPath;
          readFile().catch((error) => setStatus(String(error && error.message ? error.message : error), true));
        });
      }
      row.appendChild(chip);
      row.appendChild(path);
      els.workspaceChanges.appendChild(row);
    }
  }

  function renderPendingChanges(summary) {
    if (!els.pendingChanges) return;
    els.pendingChanges.innerHTML = "";
    const files = Array.isArray(summary && summary.files) ? summary.files : [];
    if (!files.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "No uncommitted changes.";
      els.pendingChanges.appendChild(empty);
      return;
    }
    for (const item of files.slice(0, 200)) {
      const row = document.createElement("div");
      row.className = "pending-change-row";
      const chip = document.createElement("span");
      const kind = String(item.kind || "modified");
      chip.className = `change-chip ${kind}`;
      chip.textContent = String(item.status || kind || "M");
      const path = document.createElement("button");
      path.type = "button";
      path.className = "pending-change-path";
      path.textContent = String(item.path || "");
      path.addEventListener("click", () => {
        if (els.filePath) els.filePath.value = String(item.path || "");
        readFile().catch((error) => setStatus(String(error && error.message ? error.message : error), true));
      });
      row.appendChild(chip);
      row.appendChild(path);
      els.pendingChanges.appendChild(row);
    }
  }

  function changeEntryMap() {
    const out = new Map();
    const mergeFiles = (files) => {
      for (const item of files) {
        if (!item || typeof item !== "object") continue;
        const path = String(item.path || "");
        if (!path) continue;
        out.set(path, item);
      }
    };
    mergeFiles(Array.isArray(state.diffSummary && state.diffSummary.files) ? state.diffSummary.files : []);
    mergeFiles(Array.isArray(state.changeSummary && state.changeSummary.files) ? state.changeSummary.files : []);
    return out;
  }

  function resetFilesPanel(taskId) {
    state.diffSummary = null;
    state.changeSummary = null;
    state.lastTreePayload = null;
    renderChangeSummary(null, null);
    renderWorkspaceChanges(null);
    renderPendingChanges(null);
    if (els.fileList) {
      els.fileList.innerHTML = "";
      const loading = document.createElement("div");
      loading.className = "hint";
      loading.style.padding = "10px";
      loading.textContent = taskId ? `Loading files for ${taskId}...` : "No workspace selected.";
      els.fileList.appendChild(loading);
    }
  }

  function saveWorkspaceDraft(taskId) {
    const id = String(taskId || "").trim();
    if (!id || !els.workspaceChatInput) return;
    storageSet(`draft.message.${id}`, els.workspaceChatInput.value || "");
  }

  function restoreWorkspaceDraft(taskId) {
    if (!els.workspaceChatInput) return;
    const id = String(taskId || "").trim();
    els.workspaceChatInput.value = id ? storageGet(`draft.message.${id}`, "") : "";
  }

  function selectTask(taskId) {
    if (state.selectedId && state.selectedId !== String(taskId || "")) saveWorkspaceDraft(state.selectedId);
    state.selectedId = String(taskId || "");
    storageSet("selectedTask", state.selectedId);
    try {
      const url = new URL(window.location.href);
      if (state.selectedId) url.searchParams.set("task", state.selectedId);
      else url.searchParams.delete("task");
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    } catch (error) {
      // URL state is best-effort.
    }
    restoreWorkspaceDraft(state.selectedId);
    setPublishFeedback("No publish action yet.");
    resetFilesPanel(state.selectedId);
    renderTasks();
    renderSelected();
    if (state.selectedId) {
      loadDiffSummary({ taskId: state.selectedId }).catch((error) => setStatus(String(error.message || error), true));
      loadChanges({ taskId: state.selectedId }).catch((error) => setStatus(String(error.message || error), true));
      if (els.filesPanel && els.filesPanel.open) {
        loadTree({ taskId: state.selectedId }).catch((error) => setStatus(String(error.message || error), true));
      }
    }
  }

  async function loadConfig() {
    const payload = await fetchJson("/ui/api/coding/config");
    state.config = payload;
    if (els.repoUrl && !els.repoUrl.value) els.repoUrl.value = payload.default_repo_url || "";
    if (els.modelIntegrationRepoUrl && !els.modelIntegrationRepoUrl.value) els.modelIntegrationRepoUrl.value = payload.default_repo_url || "";
    if (els.baseBranch && !els.baseBranch.value) els.baseBranch.value = payload.default_base_branch || "main";
    if (els.agentMaxCycles && !els.agentMaxCycles.value) {
      els.agentMaxCycles.value = storageGet("horizon.maxCycles", payload.agent_max_cycles_per_run || 1000);
    }
    if (els.agentMaxRuntimeMinutes && !els.agentMaxRuntimeMinutes.value) {
      const defaultMinutes = Math.max(1, Math.round(Number(payload.agent_max_runtime_sec || 21600) / 60));
      els.agentMaxRuntimeMinutes.value = storageGet("horizon.maxRuntimeMinutes", defaultMinutes);
    }
    if (els.agentContextResetCycles && !els.agentContextResetCycles.value) {
      els.agentContextResetCycles.value = storageGet("horizon.contextResetCycles", payload.agent_context_reset_cycles || 12);
    }
    setSelectOptions(
      els.modelIntegrationRuntime,
      [{ value: "auto", label: "Auto detect" }].concat((payload.model_integration_runtimes || []).filter((value) => value !== "auto").map((value) => ({ value, label: value }))),
      "auto"
    );
    setSelectOptions(
      els.modelIntegrationRouteKind,
      [{ value: "", label: "Auto detect" }].concat((payload.model_integration_route_kinds || []).map((value) => ({ value, label: value }))),
      ""
    );
    if (els.configMeta) {
      const bits = [];
      bits.push(payload.git_token_configured ? "git token configured" : "no git token");
      if (payload.preferred_coding_model) bits.push(`model: ${payload.preferred_coding_model}`);
      if (payload.agent_checkpoint_commits) bits.push("checkpoint commits on");
      bits.push(`horizon: ${payload.agent_max_cycles_per_run || 1000} cycles / ${fmtDuration(payload.agent_max_runtime_sec || 21600)}`);
      bits.push(`context compaction: ${payload.agent_context_reset_cycles || 12} cycles`);
      bits.push(payload.gh_cli_available ? "gh available" : "gh unavailable");
      bits.push(`commands: ${(payload.allowed_commands || []).join(", ")}`);
      els.configMeta.textContent = bits.join(" | ");
    }
    if (els.modelIntegrationMeta) {
      const hostLines = Array.isArray(payload.model_integration_host_lanes)
        ? payload.model_integration_host_lanes.map((lane) => `${lane.label}: ${lane.summary}`)
        : [];
      els.modelIntegrationMeta.textContent = hostLines.length
        ? `Auto lanes: ${hostLines.join(" || ")}`
        : "Auto detection uses the tracked Nexus topology to suggest the host lane.";
    }
  }

  async function loadModelDefaults() {
    const payload = await fetchJson("/ui/api/model-defaults");
    state.modelDefaults = payload && typeof payload === "object" ? payload : {};
    return state.modelDefaults;
  }

  function defaultCodingModel() {
    const shared = state.modelDefaults && state.modelDefaults.coding && state.modelDefaults.coding.model
      ? String(state.modelDefaults.coding.model).trim()
      : "";
    if (shared) return shared;
    const preferred = state.config && state.config.preferred_coding_model
      ? String(state.config.preferred_coding_model).trim()
      : "";
    return preferred || "coder";
  }

  async function loadModelCatalog({ force = false } = {}) {
    if (!force && state.modelCatalog && Date.now() - state.modelCatalogLoadedAt < 30000) {
      return state.modelCatalog;
    }
    const payload = await fetchJson("/ui/api/model-catalogs");
    state.modelCatalog = payload && payload.coding && typeof payload.coding === "object" ? payload.coding : {};
    state.modelCatalogLoadedAt = Date.now();
    renderTasks();
    renderSelected();
    return state.modelCatalog;
  }

  async function loadTasks({ keepSelection = true } = {}) {
    await loadModelCatalog();
    const payload = await fetchJson("/ui/api/coding/tasks");
    state.tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    if (!keepSelection || !state.tasks.some((task) => task.id === state.selectedId)) {
      const params = new URLSearchParams(window.location.search);
      const preferred = params.get("task") || storageGet("selectedTask", "");
      state.selectedId = state.tasks.some((task) => task.id === preferred) ? preferred : (state.tasks[0] ? state.tasks[0].id : "");
    }
    storageSet("selectedTask", state.selectedId);
    restoreWorkspaceDraft(state.selectedId);
    renderTasks();
    renderSelected();
  }

  function workspaceBody() {
    const mode = state.createMode === "model_integration" ? "agent" : state.createMode;
    return {
      repo_url: els.repoUrl ? els.repoUrl.value.trim() : "",
      base_branch: els.baseBranch ? els.baseBranch.value.trim() : "",
      branch_name: els.branchName ? els.branchName.value.trim() : "",
      prompt: buildModePrompt(mode, els.taskPrompt ? els.taskPrompt.value.trim() : ""),
      coding_model: selectedWorkspaceModelValue(),
    };
  }

  function agentOptionsBody() {
    const maxCycles = Math.max(4, Math.min(500, Number.parseInt(els.agentMaxCycles && els.agentMaxCycles.value ? els.agentMaxCycles.value : "80", 10) || 80));
    const maxRuntimeMinutes = Math.max(1, Math.min(1440, Number.parseInt(els.agentMaxRuntimeMinutes && els.agentMaxRuntimeMinutes.value ? els.agentMaxRuntimeMinutes.value : "360", 10) || 360));
    const contextResetCycles = Math.max(4, Math.min(100, Number.parseInt(els.agentContextResetCycles && els.agentContextResetCycles.value ? els.agentContextResetCycles.value : "12", 10) || 12));
    storageSet("horizon.maxCycles", maxCycles);
    storageSet("horizon.maxRuntimeMinutes", maxRuntimeMinutes);
    storageSet("horizon.contextResetCycles", contextResetCycles);
    return {
      auto_commit: !!(els.agentAutoCommit && els.agentAutoCommit.checked),
      max_cycles: maxCycles,
      max_runtime_sec: maxRuntimeMinutes * 60,
      context_reset_cycles: contextResetCycles,
    };
  }

  function selectedWorkspaceModelValue() {
    const selected = els.workspaceModelInput ? String(els.workspaceModelInput.value || "").trim() : "";
    return selected || defaultCodingModel();
  }

  function modelIntegrationBody() {
    return {
      repo_url: els.modelIntegrationRepoUrl ? els.modelIntegrationRepoUrl.value.trim() : "",
      model: els.modelIntegrationModel ? els.modelIntegrationModel.value.trim() : "",
      preferred_runtime: els.modelIntegrationRuntime ? els.modelIntegrationRuntime.value.trim() : "auto",
      route_kind: els.modelIntegrationRouteKind ? els.modelIntegrationRouteKind.value.trim() : "",
      service_name: els.modelIntegrationServiceName ? els.modelIntegrationServiceName.value.trim() : "",
      base_branch: els.baseBranch ? els.baseBranch.value.trim() : "",
      branch_name: els.modelIntegrationBranchName ? els.modelIntegrationBranchName.value.trim() : "",
      prompt: els.modelIntegrationPrompt ? els.modelIntegrationPrompt.value.trim() : "",
      coding_model: selectedWorkspaceModelValue(),
    };
  }

  async function createModelIntegration() {
    const body = modelIntegrationBody();
    if (!body.model) throw new Error("Model is required");
    if (!body.repo_url) throw new Error("Destination GitHub repository is required for model integration workspaces.");
    setBusy(true);
    try {
      setStatus("Creating model integration workspace...");
      const payload = await fetchJson("/ui/api/coding/model-integrations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const task = payload.task;
      storageSet("draft.modelIntegrationPrompt", "");
      if (els.modelIntegrationPrompt) els.modelIntegrationPrompt.value = "";
      await loadTasks({ keepSelection: false });
      if (task && task.id) selectTask(task.id);
      setOutput("model integration", task || payload);
      setStatus(task && task.status === "error" ? `Model integration workspace error: ${task.error || "see output"}` : "Model integration workspace ready.", task && task.status === "error");
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function createAndRunModelIntegration() {
    const body = { ...modelIntegrationBody(), ...agentOptionsBody() };
    if (!body.model) throw new Error("Model is required");
    if (!body.repo_url) throw new Error("Destination GitHub repository is required for model integration workspaces.");
    setBusy(true);
    try {
      setStatus("Creating model integration workspace and starting agent...");
      const payload = await fetchJson("/ui/api/coding/model-integrations/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const task = payload.task;
      storageSet("draft.modelIntegrationPrompt", "");
      if (els.modelIntegrationPrompt) els.modelIntegrationPrompt.value = "";
      await loadTasks({ keepSelection: false });
      if (task && task.id) selectTask(task.id);
      setOutput("model integration run", task || payload);
      setStatus(task && task.status === "error" ? `Model integration workspace error: ${task.error || "see output"}` : "Model integration agent run started.", task && task.status === "error");
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function createTask() {
    setBusy(true);
    try {
      const body = workspaceBody();
      setStatus("Creating workspace...");
      const payload = await fetchJson("/ui/api/coding/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const task = payload.task;
      storageSet("draft.taskPrompt", "");
      if (els.taskPrompt) els.taskPrompt.value = "";
      await loadTasks({ keepSelection: false });
      if (task && task.id) selectTask(task.id);
      setOutput("create", task || payload);
      setStatus(task && task.status === "error" ? "Workspace created with errors." : "Workspace ready.", task && task.status === "error");
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function createAndRun() {
    setBusy(true);
    try {
      const body = { ...workspaceBody(), ...agentOptionsBody() };
      if (!String(body.prompt || "").trim()) throw new Error("Task brief is required");
      setStatus("Creating workspace and starting agent...");
      const payload = await fetchJson("/ui/api/coding/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const task = payload.task;
      storageSet("draft.taskPrompt", "");
      if (els.taskPrompt) els.taskPrompt.value = "";
      await loadTasks({ keepSelection: false });
      if (task && task.id) selectTask(task.id);
      setOutput("agent run", task || payload);
      setStatus(task && task.status === "error" ? "Workspace created with errors." : "Agent run started.", task && task.status === "error");
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function refreshSelected() {
    const task = selectedTask();
    if (!task) throw new Error("Select a workspace first");
    const taskId = String(task.id || "");
    const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}`);
    const fresh = payload.task;
    if (state.selectedId !== taskId) return;
    state.tasks = state.tasks.map((item) => (item.id === task.id ? fresh : item));
    renderTasks();
    renderSelected();
    await loadDiffSummary({ quiet: true, taskId });
    await loadChanges({ quiet: true, taskId });
  }

  async function loadDiffSummary({ quiet = false, taskId } = {}) {
    const selectedTaskId = String(taskId || state.selectedId || "");
    if (!selectedTaskId) {
      state.diffSummary = null;
      renderChangeSummary(null, state.changeSummary);
      renderWorkspaceChanges(null);
      return null;
    }
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(selectedTaskId)}/diff`);
      if (state.selectedId !== selectedTaskId) return null;
      state.diffSummary = payload && payload.changes ? payload.changes : null;
      renderChangeSummary(state.diffSummary, state.changeSummary);
      renderWorkspaceChanges(state.diffSummary);
      if (els.filesPanel && els.filesPanel.open) {
        renderTree(state.lastTreePayload || { path: els.treePath ? els.treePath.value.trim() : "", entries: [] });
      }
      return state.diffSummary;
    } catch (error) {
      if (!quiet) throw error;
      return null;
    }
  }

  async function loadChanges({ quiet = false, taskId } = {}) {
    const selectedTaskId = String(taskId || state.selectedId || "");
    if (!selectedTaskId) {
      state.changeSummary = null;
      renderChangeSummary(state.diffSummary, null);
      renderPendingChanges(null);
      return null;
    }
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(selectedTaskId)}/changes`);
      if (state.selectedId !== selectedTaskId) return null;
      state.changeSummary = payload.result || null;
      renderChangeSummary(state.diffSummary, state.changeSummary);
      renderPendingChanges(state.changeSummary);
      if (els.filesPanel && els.filesPanel.open) renderTree(state.lastTreePayload || { path: els.treePath ? els.treePath.value.trim() : "", entries: [] });
      return state.changeSummary;
    } catch (error) {
      if (!quiet) throw error;
      return null;
    }
  }

  function updatePolling() {
    const shouldPoll = (state.tasks || []).some(agentIsActive);
    if (shouldPoll && !state.pollTimer) {
      state.pollTimer = window.setInterval(async () => {
        try {
          const selectedWasActive = !!(selectedTask() && agentIsActive(selectedTask()));
          const selectedId = state.selectedId;
          await loadTasks({ keepSelection: true });
          const selectedIsActive = !!(selectedTask() && agentIsActive(selectedTask()));
          if (selectedId && selectedWasActive && !selectedIsActive) {
            await loadDiffSummary({ quiet: true, taskId: selectedId });
            await loadChanges({ quiet: true, taskId: selectedId });
          }
          updatePolling();
        } catch (error) {
          setStatus(String(error && error.message ? error.message : error), true);
        }
      }, 4000);
    } else if (!shouldPoll && state.pollTimer) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function startAgentRun(taskId) {
    const task = taskId ? taskById(taskId) : selectedTask();
    if (!task) return;
    const codingModel = taskId && task.id !== state.selectedId ? workspaceModelValue(task) : selectedWorkspaceModelValue();
    if (task.id !== state.selectedId) selectTask(task.id);
    state.selectedId = task.id;
    setBusy(true);
    try {
      setStatus("Starting coding agent...");
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/agent-run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...agentOptionsBody(), coding_model: codingModel }),
      });
      const fresh = payload.task;
      state.tasks = state.tasks.map((item) => (item.id === task.id ? fresh : item));
      renderTasks();
      renderSelected();
      setOutput("agent run", fresh || payload);
      const agentStatus = String((fresh && fresh.agent && fresh.agent.status) || "");
      setStatus(agentStatus === "idle_waiting" ? "Workspace is waiting for an idle period with the pinned huge model loaded." : "Agent run started.");
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function sendWorkspaceMessage() {
    const task = selectedTask();
    if (!task) return;
    const message = els.workspaceChatInput ? els.workspaceChatInput.value.trim() : "";
    if (!message) throw new Error("Workspace message is empty");
    const active = agentIsActive(task);
    setBusy(true);
    try {
      setStatus(active ? "Sending guidance to active run..." : "Starting continuation run...");
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, run: !active, coding_model: selectedWorkspaceModelValue(), ...agentOptionsBody() }),
      });
      const fresh = payload.task;
      if (fresh) state.tasks = state.tasks.map((item) => (item.id === task.id ? fresh : item));
      if (els.workspaceChatInput) els.workspaceChatInput.value = "";
      storageSet(`draft.message.${task.id}`, "");
      renderTasks();
      renderSelected();
      setOutput(payload.started ? "workspace message and run" : "workspace message", fresh || payload);
      const agentStatus = String((fresh && fresh.agent && fresh.agent.status) || "");
      setStatus(payload.started && agentStatus === "idle_waiting"
        ? "Workspace message saved; run is waiting for an idle period with the pinned huge model loaded."
        : (payload.started ? "Workspace message sent and continuation run started." : "Workspace message sent."));
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function saveWorkspaceModel() {
    const task = selectedTask();
    if (!task) return;
    const codingModel = selectedWorkspaceModelValue();
    setBusy(true);
    try {
      setStatus(codingModel ? `Saving workspace model ${codingModel}...` : "Clearing workspace model override...");
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/model`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coding_model: codingModel }),
      });
      const fresh = payload.task;
      if (fresh) state.tasks = state.tasks.map((item) => (item.id === task.id ? fresh : item));
      renderTasks();
      renderSelected();
      setOutput("workspace model", fresh || payload);
      setStatus(codingModel ? "Workspace model updated." : "Workspace model override cleared.");
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function trackCurrentCoderModel() {
    if (!els.workspaceModelInput) return;
    els.workspaceModelInput.value = "coder";
    renderWorkspaceModelHint(selectedTask());
    await saveWorkspaceModel();
  }

  async function stopAgentRun(taskId) {
    const task = taskId ? taskById(taskId) : selectedTask();
    if (!task) return;
    if (task.id !== state.selectedId) selectTask(task.id);
    state.selectedId = task.id;
    setBusy(true);
    try {
      setStatus("Pausing coding agent...");
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/agent-pause`, { method: "POST" });
      const fresh = payload.task;
      state.tasks = state.tasks.map((item) => (item.id === task.id ? fresh : item));
      renderTasks();
      renderSelected();
      setOutput("agent pause", fresh || payload);
      setStatus("Pause requested.");
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function runStatus() {
    const task = selectedTask();
    if (!task) return;
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/status`);
      setOutput("git status", resultText(payload.result));
      await refreshSelected();
      await loadChanges({ quiet: true });
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function runDiff() {
    const task = selectedTask();
    if (!task) return;
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/diff`);
      const parts = [];
      if (payload.scope === "base_branch") {
        parts.push(
          [
            `branch: ${payload.branch_name || ""}`,
            `base branch: ${payload.base_branch || ""}`,
            `base ref: ${payload.base_ref || ""}`,
            `merge base: ${payload.merge_base || payload.compare_ref || ""}`,
          ].join("\n")
        );
      }
      if (payload.committed_stat && payload.committed_stat.stdout) parts.push(`committed vs base stat:\n${payload.committed_stat.stdout}`);
      if (payload.committed_diff && payload.committed_diff.stdout) parts.push(`committed vs base diff:\n${payload.committed_diff.stdout}`);
      if (payload.stat && payload.stat.stdout) parts.push(`workspace vs base stat:\n${payload.stat.stdout}`);
      if (payload.diff && payload.diff.stdout) parts.push(`workspace vs base diff:\n${payload.diff.stdout}`);
      if (payload.staged_stat && payload.staged_stat.stdout) parts.push(`staged stat:\n${payload.staged_stat.stdout}`);
      if (payload.staged_diff && payload.staged_diff.stdout) parts.push(`staged diff:\n${payload.staged_diff.stdout}`);
      if (payload.worktree_stat && payload.worktree_stat.stdout) parts.push(`unstaged stat:\n${payload.worktree_stat.stdout}`);
      if (payload.worktree_diff && payload.worktree_diff.stdout) parts.push(`unstaged diff:\n${payload.worktree_diff.stdout}`);
      if (payload.error) parts.push(`warning:\n${payload.error}`);
      setOutput("diff vs base", parts.join("\n\n") || JSON.stringify(payload, null, 2));
      await refreshSelected();
      await loadChanges({ quiet: true });
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function runAgentBrief() {
    const task = selectedTask();
    if (!task) return;
    const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/agent-brief`);
    setOutput("agent brief", payload.brief || payload);
  }

  async function runCommand() {
    const task = selectedTask();
    if (!task) return;
    const argv = parseArgv(els.commandInput ? els.commandInput.value : "");
    if (!argv.length) throw new Error("Command is empty");
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/command`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ argv, cwd: els.commandCwd ? els.commandCwd.value.trim() : "" }),
      });
      setOutput("command", resultText(payload.result));
      await refreshSelected();
      await loadChanges({ quiet: true });
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function commitTask() {
    const task = selectedTask();
    if (!task) return;
    const message = els.commitMessage ? els.commitMessage.value.trim() : "";
    if (!message) throw new Error("Commit message is required");
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      setOutput("commit", payload);
      if (payload && payload.ok) {
        setPublishFeedback(`Commit succeeded${payload.last_commit ? `: ${String(payload.last_commit).slice(0, 12)}` : "."}`, "ok");
      } else {
        const error = payload && payload.error ? String(payload.error) : "Commit did not complete.";
        setPublishFeedback(error, "error");
      }
      await refreshSelected();
      await loadChanges({ quiet: true });
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function pushTask() {
    const task = selectedTask();
    if (!task) return;
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/push`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ remote: "origin" }),
      });
      setOutput("push", resultText(payload.result));
      if (commandOk(payload.result)) {
        setPublishFeedback("Push succeeded. The branch is available on origin.", "ok");
      } else {
        const hint = pushPermissionHint(payload.result);
        const stderr = String((payload.result && payload.result.stderr) || "").trim();
        setPublishFeedback(hint || stderr || "Push failed.", "error");
      }
      await refreshSelected();
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function openPr() {
    const task = selectedTask();
    if (!task) return;
    const title = els.prTitle ? els.prTitle.value.trim() : "";
    if (!title) throw new Error("PR title is required");
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/pull-request`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, body: els.prBody ? els.prBody.value : "", draft: true }),
      });
      setOutput("pull request", resultText(payload.result || payload));
      if ((payload.result || payload).ok) {
        const url = (payload.result || payload).url || (payload.result || payload).stdout || "";
        setPublishFeedback(`Draft PR created${url ? `: ${url}` : "."}`, "ok");
      } else {
        const error = (payload.result || payload).error || "Draft PR creation failed.";
        setPublishFeedback(String(error), "error");
      }
      await refreshSelected();
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function deleteTask(taskId) {
    const task = taskId ? state.tasks.find((t) => t.id === taskId) : selectedTask();
    if (!task) return;
    const ok = window.confirm(`Delete workspace ${task.id}? This removes the workspace and task metadata and cannot be undone.`);
    if (!ok) return;
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}`, { method: "DELETE" });
      setOutput("delete", payload);
      setPublishFeedback(`Deleted ${task.id}.`, "ok");
      if (task.id === state.selectedId) state.selectedId = "";
      await loadTasks({ keepSelection: false });
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function archiveTask(taskId) {
    const task = taskId ? state.tasks.find((t) => t.id === taskId) : selectedTask();
    if (!task) return;
    const ok = window.confirm(`Archive workspace ${task.id} for forensics? This removes it from the active task list but preserves the task file and workspace contents.`);
    if (!ok) return;
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/archive`, { method: "POST" });
      setOutput("archive", payload);
      setPublishFeedback(`Archived ${task.id}${payload.archive_id ? ` as ${payload.archive_id}` : "."}`, "ok");
      if (task.id === state.selectedId) state.selectedId = "";
      await loadTasks({ keepSelection: false });
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  function renderTree(payload) {
    if (!els.fileList) return;
    state.lastTreePayload = payload || null;
    els.fileList.innerHTML = "";
    const path = String(payload.path || "");
    const changes = changeEntryMap();
    if (path) {
      const up = path.split("/").filter(Boolean);
      up.pop();
      const button = document.createElement("button");
      button.type = "button";
      button.className = "file-row";
      button.innerHTML = "<span>dir</span><span>..</span><span></span>";
      button.addEventListener("click", () => {
        if (els.treePath) els.treePath.value = up.join("/");
        loadTree().catch((error) => setStatus(String(error.message || error), true));
      });
      els.fileList.appendChild(button);
    }
    const entries = Array.isArray(payload.entries) ? payload.entries : [];
    for (const item of entries) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "file-row";
      const type = String(item.type || "");
      const size = item.size !== undefined ? `${item.size} B` : "";
      const relPath = String(item.path || item.name || "");
      const change = changes.get(relPath);
      const changeChip = change
        ? `<span class="change-chip ${escapeHtml(String(change.kind || "modified"))}">${escapeHtml(String(change.status || change.kind || "M"))}</span>`
        : "";
      button.innerHTML = `<span>${type === "dir" ? "dir" : "file"}</span><span class="file-row-main"><span class="file-row-name"></span>${changeChip}</span><span>${size}</span>`;
      button.querySelector(".file-row-name").textContent = relPath;
      button.addEventListener("click", () => {
        if (type === "dir") {
          if (els.treePath) els.treePath.value = relPath;
          loadTree().catch((error) => setStatus(String(error.message || error), true));
        } else {
          if (els.filePath) els.filePath.value = relPath;
          readFile().catch((error) => setStatus(String(error.message || error), true));
        }
      });
      els.fileList.appendChild(button);
    }
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.style.padding = "10px";
      empty.textContent = "No entries.";
      els.fileList.appendChild(empty);
    }
  }

  async function loadTree({ taskId } = {}) {
    const selectedTaskId = String(taskId || state.selectedId || "");
    if (!selectedTaskId) return;
    const path = els.treePath ? els.treePath.value.trim() : "";
    const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(selectedTaskId)}/tree?path=${encodeURIComponent(path)}`);
    if (state.selectedId !== selectedTaskId) return;
    renderTree(payload);
  }

  async function readFile() {
    const task = selectedTask();
    if (!task) return;
    const path = els.filePath ? els.filePath.value.trim() : "";
    if (!path) throw new Error("File path is required");
    const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/file?path=${encodeURIComponent(path)}`);
    if (els.fileContent) els.fileContent.value = payload.content || "";
    setOutput("read file", `${payload.path || path}\n${payload.size || 0} bytes`);
  }

  async function writeFile() {
    const task = selectedTask();
    if (!task) return;
    const path = els.filePath ? els.filePath.value.trim() : "";
    if (!path) throw new Error("File path is required");
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/file`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, content: els.fileContent ? els.fileContent.value : "" }),
      });
      setOutput("write file", payload);
      await refreshSelected();
      await loadChanges({ quiet: true });
      if (els.filesPanel && els.filesPanel.open) await loadTree();
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function copyOutput() {
    const text = els.output ? els.output.textContent || "" : "";
    if (!text) return;
    await navigator.clipboard.writeText(text);
    setStatus("Copied output.");
  }

  function wire(id, fn) {
    const el = els[id];
    if (!el) return;
    el.addEventListener("click", async () => {
      try {
        setStatus("");
        await fn();
      } catch (error) {
        setStatus(String(error && error.message ? error.message : error), true);
      }
    });
  }

  async function init() {
    const params = new URLSearchParams(window.location.search);
    const prompt = params.get("prompt") || "";
    if (els.taskPrompt && !els.taskPrompt.value) els.taskPrompt.value = prompt || storageGet("draft.taskPrompt", "");
    if (els.modelIntegrationPrompt && !els.modelIntegrationPrompt.value) {
      els.modelIntegrationPrompt.value = storageGet("draft.modelIntegrationPrompt", "");
    }
    state.taskSearch = storageGet("taskSearch", "");
    state.taskFilter = storageGet("taskFilter", "all") || "all";
    if (els.taskSearch) els.taskSearch.value = state.taskSearch;
    if (els.taskFilter) els.taskFilter.value = state.taskFilter;
    setCreateMode(params.get("create") || "agent");
    renderSelected();
    await Promise.all([loadConfig(), loadModelCatalog({ force: true }), loadModelDefaults()]);
    if (els.workspaceModelInput && !String(els.workspaceModelInput.value || "").trim()) {
      const preferred = defaultCodingModel();
      renderWorkspaceModelOptions(null);
      els.workspaceModelInput.value = preferred;
      renderWorkspaceModelHint(null);
    }
    await loadTasks({ keepSelection: false });
    if (state.selectedId) {
      await loadDiffSummary({ quiet: true, taskId: state.selectedId });
      await loadChanges({ quiet: true });
      if (els.filesPanel && els.filesPanel.open) await loadTree();
    }
  }

  wire("refreshTasks", async () => {
    await Promise.all([loadConfig(), loadModelCatalog({ force: true }), loadModelDefaults()]);
    await loadTasks({ keepSelection: true });
  });
  wire("createAndRun", createAndRun);
  wire("createTask", createTask);
  wire("createAndRunModelIntegration", createAndRunModelIntegration);
  wire("createModelIntegration", createModelIntegration);
  wire("statusBtn", runStatus);
  wire("diffBtn", runDiff);
  wire("briefBtn", runAgentBrief);
  wire("runSelectedAgent", () => startAgentRun());
  wire("pauseSelectedAgent", () => stopAgentRun());
  wire("sendWorkspaceMessage", sendWorkspaceMessage);
  wire("saveWorkspaceModel", saveWorkspaceModel);
  wire("trackCurrentCoderModel", trackCurrentCoderModel);
  wire("runCommand", runCommand);
  wire("commitBtn", commitTask);
  wire("pushBtn", pushTask);
  wire("archiveTaskBtn", archiveTask);
  wire("purgeTaskBtn", deleteTask);
  wire("prBtn", openPr);
  wire("loadTree", loadTree);
  wire("readFile", readFile);
  wire("writeFile", writeFile);
  wire("copyOutput", copyOutput);

  if (els.createMode) {
    els.createMode.addEventListener("change", () => {
      setCreateMode(els.createMode ? els.createMode.value : "agent");
    });
  }

  if (els.workspaceModelInput) {
    els.workspaceModelInput.addEventListener("change", () => renderWorkspaceModelHint(selectedTask()));
  }

  if (els.filesPanel) {
    els.filesPanel.addEventListener("toggle", () => {
      if (els.filesPanel.open && selectedTask()) {
        const taskId = state.selectedId;
        loadDiffSummary({ quiet: true, taskId })
          .then(() => loadChanges({ quiet: true, taskId }))
          .then(() => loadTree({ taskId }))
          .catch((error) => setStatus(String(error && error.message ? error.message : error), true));
      }
    });
  }

  if (els.workspaceChatInput) {
    els.workspaceChatInput.addEventListener("input", () => saveWorkspaceDraft(state.selectedId));
    els.workspaceChatInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.isComposing) {
        event.preventDefault();
        sendWorkspaceMessage().catch((error) => setStatus(String(error && error.message ? error.message : error), true));
      }
    });
  }

  if (els.taskSearch) {
    els.taskSearch.addEventListener("input", () => {
      state.taskSearch = els.taskSearch.value || "";
      storageSet("taskSearch", state.taskSearch);
      renderTasks();
    });
  }

  if (els.taskFilter) {
    els.taskFilter.addEventListener("change", () => {
      state.taskFilter = els.taskFilter.value || "all";
      storageSet("taskFilter", state.taskFilter);
      renderTasks();
    });
  }

  if (els.taskPrompt) els.taskPrompt.addEventListener("input", () => storageSet("draft.taskPrompt", els.taskPrompt.value || ""));
  if (els.modelIntegrationPrompt) {
    els.modelIntegrationPrompt.addEventListener("input", () => storageSet("draft.modelIntegrationPrompt", els.modelIntegrationPrompt.value || ""));
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const tag = String(event.target && event.target.tagName || "").toLowerCase();
      if (!["input", "textarea", "select"].includes(tag) && els.taskSearch) {
        event.preventDefault();
        els.taskSearch.focus();
      }
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && els.workspaceChatInput === document.activeElement) {
      event.preventDefault();
      sendWorkspaceMessage().catch((error) => setStatus(String(error && error.message ? error.message : error), true));
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    init().catch((error) => setStatus(String(error && error.message ? error.message : error), true));
  });
  window.addEventListener("beforeunload", () => {
    saveWorkspaceDraft(state.selectedId);
    if (state.pollTimer) window.clearInterval(state.pollTimer);
  });
})();
