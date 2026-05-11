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
    agentMaxTurns: document.getElementById("agentMaxTurns"),
    agentAutoCommit: document.getElementById("agentAutoCommit"),
    configMeta: document.getElementById("configMeta"),
    refreshTasks: document.getElementById("refreshTasks"),
    tasks: document.getElementById("tasks"),
    taskCount: document.getElementById("taskCount"),
    selectedTitle: document.getElementById("selectedTitle"),
    selectedMeta: document.getElementById("selectedMeta"),
    selectedStatus: document.getElementById("selectedStatus"),
    selectedPrompt: document.getElementById("selectedPrompt"),
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
    tasks: [],
    selectedId: "",
    createMode: "agent",
    busy: false,
    pollTimer: null,
    outputHistory: [],
    diffSummary: null,
    changeSummary: null,
    lastTreePayload: null,
  };

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

  function shortCommit(value) {
    const text = String(value || "").trim();
    return text ? text.slice(0, 12) : "";
  }

  function badgeClass(status) {
    const value = String(status || "").toLowerCase();
    if (value === "ready" || value === "completed") return "ready";
    if (value === "error" || value === "failed") return "error";
    if (value === "running" || value === "queued" || value === "stopping") return "running";
    if (value === "interrupted") return "error";
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
    return status === "queued" || status === "running" || status === "stopping";
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
    const tokenRe = /(coding_[A-Za-z0-9_]+|function=[A-Za-z_][A-Za-z0-9_]*|[A-Za-z]:[\\/][A-Za-z0-9._ -]+(?:[\\/][A-Za-z0-9._ -]+)+|(?:^|[\s"'=])(?:\.{0,2}\/)?[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)+(?:[A-Za-z0-9._-])|(?:backend|model|turns|turn|ok|path|cwd|argv|returncode|summary|error|status)=)/g;
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
    const tasks = state.tasks || [];
    if (els.taskCount) els.taskCount.textContent = String(tasks.length);
    if (!tasks.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "No workspaces yet.";
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
      commitMeta.textContent = commit ? `commit ${commit}` : "";
      button.appendChild(status);
      button.appendChild(agentBadge);
      button.appendChild(title);
      button.appendChild(meta);
      if (commitMeta.textContent) button.appendChild(commitMeta);
      if (prompt.textContent) button.appendChild(prompt);
      const actions = document.createElement("div");
      actions.className = "task-actions";
      const runBtn = document.createElement("button");
      runBtn.type = "button";
      runBtn.className = "task-icon-btn task-run-btn";
      runBtn.title = "Run agent for this workspace";
      runBtn.setAttribute("aria-label", "Run agent for this workspace");
      runBtn.disabled = state.busy || agentIsActive(task);
      runBtn.innerHTML = "<svg viewBox='0 0 24 24'><path d='M8 5v14l11-7z'/></svg>";
      runBtn.addEventListener("click", (event) => runTaskButtonAction(event, () => startAgentRun(task.id)));
      const stopBtn = document.createElement("button");
      stopBtn.type = "button";
      stopBtn.className = "task-icon-btn task-stop-btn";
      stopBtn.title = "Stop agent for this workspace";
      stopBtn.setAttribute("aria-label", "Stop agent for this workspace");
      stopBtn.disabled = state.busy || !agentIsActive(task);
      stopBtn.innerHTML = "<svg viewBox='0 0 24 24'><path d='M7 7h10v10H7z'/></svg>";
      stopBtn.addEventListener("click", (event) => runTaskButtonAction(event, () => stopAgentRun(task.id)));
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
      els.prBtn,
      els.loadTree,
      els.readFile,
      els.writeFile,
      els.sendWorkspaceMessage,
    ].forEach((button) => {
      if (button) button.disabled = disabled || state.busy;
    });
    [els.runCommand, els.commitBtn, els.pushBtn, els.prBtn, els.writeFile].forEach((button) => {
      if (button && activeAgent) button.disabled = true;
    });
    if (!task) {
      if (els.selectedTitle) els.selectedTitle.textContent = "No workspace selected";
      if (els.selectedMeta) els.selectedMeta.textContent = "";
      if (els.selectedPrompt) els.selectedPrompt.textContent = "";
      if (els.selectedStatus) {
        els.selectedStatus.className = "badge pending";
        els.selectedStatus.textContent = "idle";
      }
      renderAgent(null);
      renderWorkspaceChat(null);
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
      const commit = shortCommit(task.last_commit || task.last_checkpoint_commit);
      if (commit) bits.push(`commit ${commit}`);
      els.selectedMeta.textContent = bits.join(" | ");
    }
    if (els.selectedPrompt) {
      els.selectedPrompt.textContent = integration && deployment && deployment.reason
        ? `${task.prompt || ""}\n\nRecommended lane: ${deployment.reason}`
        : task.prompt || "";
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
    renderAgent(task);
    renderWorkspaceChat(task);
  }

  function renderWorkspaceChat(task) {
    if (!els.workspaceChat) return;
    const messages = task && Array.isArray(task.guidance_messages) ? task.guidance_messages : [];
    els.workspaceChat.innerHTML = "";
    if (els.workspaceChatMeta) {
      els.workspaceChatMeta.textContent = messages.length ? `${messages.length} message${messages.length === 1 ? "" : "s"}` : "No guidance yet";
    }
    if (els.workspaceChatStatus) {
      if (!task) els.workspaceChatStatus.textContent = "";
      else if (agentIsActive(task)) els.workspaceChatStatus.textContent = "Sent messages are read by the active agent on a later turn.";
      else els.workspaceChatStatus.textContent = "Sending a message starts another continuation run.";
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
      wrap.className = "workspace-message";
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
    if (type === "queued") return `${time} queued model=${event.model || ""} turns=${event.max_turns || ""}`;
    if (type === "started") return `${time} started backend=${event.backend || ""} model=${event.upstream_model || ""}`;
    if (type === "turn_started") return `${time} turn ${event.turn || ""}`;
    if (type === "assistant") {
      const calls = Array.isArray(event.tool_calls) ? event.tool_calls.map((item) => item && item.name).filter(Boolean).join(", ") : "";
      const content = String(event.content || "").trim();
      return `${time} assistant${calls ? ` tools=[${calls}]` : ""}${content ? `\n${content}` : ""}`;
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
    if (type === "no_change_audit") return `${time} no-change audit\n${event.summary || ""}`;
    if (type === "guidance_seen") return `${time} guidance seen count=${event.count || 0}\n${event.summary || ""}`;
    if (type === "backend_retry") {
      const attempt = `${event.attempt || "?"}/${event.max_retries || "?"}`;
      return `${time} backend retry turn=${event.turn || ""} attempt=${attempt} delay=${event.delay_sec || 0}s\n${event.error || ""}`;
    }
    if (type === "checkpoint") {
      const commit = String(event.commit || "").slice(0, 12);
      const changed = event.changed ? "changed" : "clean";
      const stateText = event.ok ? "saved" : "failed";
      return `${time} checkpoint ${stateText} ${changed}${commit ? ` commit=${commit}` : ""}${event.error ? `\n${event.error}` : ""}`;
    }
    if (type === "interrupted") return `${time} interrupted\n${event.summary || ""}`;
    if (type === "commit") {
      const commit = shortCommit(event.commit || (event.result && event.result.last_commit));
      return `${time} ${event.skipped ? "commit skipped" : "committed"} ${event.message || ""}${commit ? ` commit=${commit}` : ""}${event.summary ? `\n${event.summary}` : ""}`;
    }
    if (type === "completed") return `${time} completed\n${event.summary || ""}`;
    if (type === "failed") return `${time} failed\n${event.summary || event.error || ""}`;
    if (type === "stopped") return `${time} stopped\n${event.summary || ""}`;
    if (type === "stop_requested") return `${time} stop requested`;
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
      if (agent.turn || agent.max_turns) bits.push(`turn ${agent.turn || 0}/${agent.max_turns || "?"}`);
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

  function selectTask(taskId) {
    state.selectedId = String(taskId || "");
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
    if (els.baseBranch && !els.baseBranch.value) els.baseBranch.value = payload.default_base_branch || "main";
    if (els.agentMaxTurns && payload.agent_max_turns_limit) els.agentMaxTurns.max = String(payload.agent_max_turns_limit);
    if (els.agentMaxTurns && !els.agentMaxTurns.value) els.agentMaxTurns.value = payload.agent_max_turns || 1000;
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
      if (payload.agent_max_turns) bits.push(`agent turns: ${payload.agent_max_turns}`);
      bits.push(payload.agent_max_runtime_sec > 0 ? `runtime: ${payload.agent_max_runtime_sec}s` : "runtime: unlimited");
      if (payload.agent_checkpoint_commits) bits.push("checkpoint commits on");
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

  async function loadTasks({ keepSelection = true } = {}) {
    const payload = await fetchJson("/ui/api/coding/tasks");
    state.tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    if (!keepSelection || !state.tasks.some((task) => task.id === state.selectedId)) {
      state.selectedId = state.tasks[0] ? state.tasks[0].id : "";
    }
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
    };
  }

  function agentOptionsBody() {
    const turns = els.agentMaxTurns ? Number(els.agentMaxTurns.value || 0) : 0;
    const body = {
      auto_commit: !!(els.agentAutoCommit && els.agentAutoCommit.checked),
    };
    if (Number.isFinite(turns) && turns > 0) body.max_turns = Math.trunc(turns);
    return body;
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
    };
  }

  async function createModelIntegration() {
    const body = modelIntegrationBody();
    if (!body.model) throw new Error("Model is required");
    setBusy(true);
    try {
      setStatus("Creating model integration workspace...");
      const payload = await fetchJson("/ui/api/coding/model-integrations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const task = payload.task;
      await loadTasks({ keepSelection: false });
      if (task && task.id) selectTask(task.id);
      setOutput("model integration", task || payload);
      setStatus(task && task.status === "error" ? "Model integration workspace created with errors." : "Model integration workspace ready.", task && task.status === "error");
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function createAndRunModelIntegration() {
    const body = { ...modelIntegrationBody(), ...agentOptionsBody() };
    if (!body.model) throw new Error("Model is required");
    setBusy(true);
    try {
      setStatus("Creating model integration workspace and starting agent...");
      const payload = await fetchJson("/ui/api/coding/model-integrations/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const task = payload.task;
      await loadTasks({ keepSelection: false });
      if (task && task.id) selectTask(task.id);
      setOutput("model integration run", task || payload);
      setStatus(task && task.status === "error" ? "Model integration workspace created with errors." : "Model integration agent run started.", task && task.status === "error");
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
    if (!task) return;
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
    const task = selectedTask();
    const shouldPoll = !!(task && agentIsActive(task));
    if (shouldPoll && !state.pollTimer) {
      state.pollTimer = window.setInterval(async () => {
        try {
          if (selectedTask() && agentIsActive(selectedTask())) {
            await refreshSelected();
          } else {
            updatePolling();
          }
        } catch (error) {
          setStatus(String(error && error.message ? error.message : error), true);
        }
      }, 3000);
    } else if (!shouldPoll && state.pollTimer) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function startAgentRun(taskId) {
    const task = taskId ? taskById(taskId) : selectedTask();
    if (!task) return;
    state.selectedId = task.id;
    setBusy(true);
    try {
      setStatus("Starting coding agent...");
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/agent-run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(agentOptionsBody()),
      });
      const fresh = payload.task;
      state.tasks = state.tasks.map((item) => (item.id === task.id ? fresh : item));
      renderTasks();
      renderSelected();
      setOutput("agent run", fresh || payload);
      setStatus("Agent run started.");
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
        body: JSON.stringify({ message, run: !active, ...agentOptionsBody() }),
      });
      const fresh = payload.task;
      if (fresh) state.tasks = state.tasks.map((item) => (item.id === task.id ? fresh : item));
      if (els.workspaceChatInput) els.workspaceChatInput.value = "";
      renderTasks();
      renderSelected();
      setOutput(payload.started ? "workspace message and run" : "workspace message", fresh || payload);
      setStatus(payload.started ? "Workspace message sent and continuation run started." : "Workspace message sent.");
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function stopAgentRun(taskId) {
    const task = taskId ? taskById(taskId) : selectedTask();
    if (!task) return;
    state.selectedId = task.id;
    setBusy(true);
    try {
      setStatus("Stopping coding agent...");
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/agent-stop`, { method: "POST" });
      const fresh = payload.task;
      state.tasks = state.tasks.map((item) => (item.id === task.id ? fresh : item));
      renderTasks();
      renderSelected();
      setOutput("agent stop", fresh || payload);
      setStatus("Stop requested.");
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
    const ok = window.confirm(`Delete workspace ${task.id}?`);
    if (!ok) return;
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}`, { method: "DELETE" });
      setOutput("delete", payload);
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
    if (prompt && els.taskPrompt && !els.taskPrompt.value) els.taskPrompt.value = prompt;
    setCreateMode(params.get("create") || "agent");
    renderSelected();
    await loadConfig();
    await loadTasks({ keepSelection: false });
    if (state.selectedId) {
      await loadDiffSummary({ quiet: true, taskId: state.selectedId });
      await loadChanges({ quiet: true });
      if (els.filesPanel && els.filesPanel.open) await loadTree();
    }
  }

  wire("refreshTasks", async () => loadTasks({ keepSelection: true }));
  wire("createAndRun", createAndRun);
  wire("createTask", createTask);
  wire("createAndRunModelIntegration", createAndRunModelIntegration);
  wire("createModelIntegration", createModelIntegration);
  wire("statusBtn", runStatus);
  wire("diffBtn", runDiff);
  wire("briefBtn", runAgentBrief);
  wire("sendWorkspaceMessage", sendWorkspaceMessage);
  wire("runCommand", runCommand);
  wire("commitBtn", commitTask);
  wire("pushBtn", pushTask);
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
    els.workspaceChatInput.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        sendWorkspaceMessage().catch((error) => setStatus(String(error && error.message ? error.message : error), true));
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    init().catch((error) => setStatus(String(error && error.message ? error.message : error), true));
  });
  window.addEventListener("beforeunload", () => {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
  });
})();
