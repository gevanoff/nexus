(() => {
  const els = {
    status: document.getElementById("status"),
    repoUrl: document.getElementById("repoUrl"),
    baseBranch: document.getElementById("baseBranch"),
    branchName: document.getElementById("branchName"),
    taskPrompt: document.getElementById("taskPrompt"),
    createTask: document.getElementById("createTask"),
    configMeta: document.getElementById("configMeta"),
    refreshTasks: document.getElementById("refreshTasks"),
    tasks: document.getElementById("tasks"),
    taskCount: document.getElementById("taskCount"),
    selectedTitle: document.getElementById("selectedTitle"),
    selectedMeta: document.getElementById("selectedMeta"),
    selectedStatus: document.getElementById("selectedStatus"),
    selectedPrompt: document.getElementById("selectedPrompt"),
    statusBtn: document.getElementById("statusBtn"),
    diffBtn: document.getElementById("diffBtn"),
    briefBtn: document.getElementById("briefBtn"),
    deleteTask: document.getElementById("deleteTask"),
    commandInput: document.getElementById("commandInput"),
    commandCwd: document.getElementById("commandCwd"),
    runCommand: document.getElementById("runCommand"),
    commitMessage: document.getElementById("commitMessage"),
    commitBtn: document.getElementById("commitBtn"),
    pushBtn: document.getElementById("pushBtn"),
    prTitle: document.getElementById("prTitle"),
    prBody: document.getElementById("prBody"),
    prBtn: document.getElementById("prBtn"),
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
    busy: false,
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
      button.disabled = state.busy;
    });
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

  function badgeClass(status) {
    const value = String(status || "").toLowerCase();
    if (value === "ready") return "ready";
    if (value === "error") return "error";
    return "pending";
  }

  function selectedTask() {
    return state.tasks.find((task) => task && task.id === state.selectedId) || null;
  }

  function setOutput(title, value) {
    if (els.outputTitle) els.outputTitle.textContent = title || "";
    if (!els.output) return;
    if (typeof value === "string") {
      els.output.textContent = value;
    } else {
      els.output.textContent = JSON.stringify(value, null, 2);
    }
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
      const button = document.createElement("button");
      button.type = "button";
      button.className = `task-item ${task.id === state.selectedId ? "active" : ""}`;
      const status = document.createElement("span");
      status.className = `badge ${badgeClass(task.status)}`;
      status.textContent = task.status || "unknown";
      const title = document.createElement("div");
      title.style.marginTop = "6px";
      title.style.fontWeight = "700";
      title.textContent = task.branch_name || task.id;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${task.base_branch || "base"} -> ${task.id || ""}`;
      const prompt = document.createElement("div");
      prompt.className = "meta";
      prompt.textContent = String(task.prompt || "").slice(0, 140);
      button.appendChild(status);
      button.appendChild(title);
      button.appendChild(meta);
      if (prompt.textContent) button.appendChild(prompt);
      button.addEventListener("click", () => selectTask(task.id));
      els.tasks.appendChild(button);
    }
  }

  function renderSelected() {
    const task = selectedTask();
    const disabled = !task;
    [
      els.statusBtn,
      els.diffBtn,
      els.briefBtn,
      els.deleteTask,
      els.runCommand,
      els.commitBtn,
      els.pushBtn,
      els.prBtn,
      els.loadTree,
      els.readFile,
      els.writeFile,
    ].forEach((button) => {
      if (button) button.disabled = disabled || state.busy;
    });
    if (!task) {
      if (els.selectedTitle) els.selectedTitle.textContent = "No workspace selected";
      if (els.selectedMeta) els.selectedMeta.textContent = "";
      if (els.selectedPrompt) els.selectedPrompt.textContent = "";
      if (els.selectedStatus) {
        els.selectedStatus.className = "badge pending";
        els.selectedStatus.textContent = "idle";
      }
      return;
    }
    if (els.selectedTitle) els.selectedTitle.textContent = task.branch_name || task.id;
    if (els.selectedMeta) {
      els.selectedMeta.textContent = `${task.repo_url || ""} | base ${task.base_branch || ""} | updated ${fmtTime(task.updated_at)}`;
    }
    if (els.selectedPrompt) els.selectedPrompt.textContent = task.prompt || "";
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
  }

  function selectTask(taskId) {
    state.selectedId = String(taskId || "");
    renderTasks();
    renderSelected();
    if (state.selectedId) loadTree().catch((error) => setStatus(String(error.message || error), true));
  }

  async function loadConfig() {
    const payload = await fetchJson("/ui/api/coding/config");
    state.config = payload;
    if (els.repoUrl && !els.repoUrl.value) els.repoUrl.value = payload.default_repo_url || "";
    if (els.baseBranch && !els.baseBranch.value) els.baseBranch.value = payload.default_base_branch || "main";
    if (els.configMeta) {
      const bits = [];
      bits.push(payload.git_token_configured ? "git token configured" : "no git token");
      if (payload.preferred_coding_model) bits.push(`model: ${payload.preferred_coding_model}`);
      bits.push(payload.gh_cli_available ? "gh available" : "gh unavailable");
      bits.push(`commands: ${(payload.allowed_commands || []).join(", ")}`);
      els.configMeta.textContent = bits.join(" | ");
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

  async function createTask() {
    setBusy(true);
    try {
      const body = {
        repo_url: els.repoUrl ? els.repoUrl.value.trim() : "",
        base_branch: els.baseBranch ? els.baseBranch.value.trim() : "",
        branch_name: els.branchName ? els.branchName.value.trim() : "",
        prompt: els.taskPrompt ? els.taskPrompt.value.trim() : "",
      };
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

  async function refreshSelected() {
    const task = selectedTask();
    if (!task) return;
    const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}`);
    const fresh = payload.task;
    state.tasks = state.tasks.map((item) => (item.id === task.id ? fresh : item));
    renderTasks();
    renderSelected();
  }

  async function runStatus() {
    const task = selectedTask();
    if (!task) return;
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/status`);
      setOutput("git status", resultText(payload.result));
      await refreshSelected();
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
      if (payload.staged_stat && payload.staged_stat.stdout) parts.push(`staged stat:\n${payload.staged_stat.stdout}`);
      if (payload.staged_diff && payload.staged_diff.stdout) parts.push(`staged diff:\n${payload.staged_diff.stdout}`);
      if (payload.stat && payload.stat.stdout) parts.push(`stat:\n${payload.stat.stdout}`);
      if (payload.diff && payload.diff.stdout) parts.push(`diff:\n${payload.diff.stdout}`);
      setOutput("diff", parts.join("\n\n") || JSON.stringify(payload, null, 2));
      await refreshSelected();
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
      await refreshSelected();
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
      await refreshSelected();
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  async function deleteTask() {
    const task = selectedTask();
    if (!task) return;
    const ok = window.confirm(`Delete workspace ${task.id}?`);
    if (!ok) return;
    setBusy(true);
    try {
      const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}`, { method: "DELETE" });
      setOutput("delete", payload);
      state.selectedId = "";
      await loadTasks({ keepSelection: false });
    } finally {
      setBusy(false);
      renderSelected();
    }
  }

  function renderTree(payload) {
    if (!els.fileList) return;
    els.fileList.innerHTML = "";
    const path = String(payload.path || "");
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
      button.innerHTML = `<span>${type === "dir" ? "dir" : "file"}</span><span></span><span>${size}</span>`;
      button.children[1].textContent = item.path || item.name || "";
      button.addEventListener("click", () => {
        if (type === "dir") {
          if (els.treePath) els.treePath.value = item.path || "";
          loadTree().catch((error) => setStatus(String(error.message || error), true));
        } else {
          if (els.filePath) els.filePath.value = item.path || "";
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

  async function loadTree() {
    const task = selectedTask();
    if (!task) return;
    const path = els.treePath ? els.treePath.value.trim() : "";
    const payload = await fetchJson(`/ui/api/coding/tasks/${encodeURIComponent(task.id)}/tree?path=${encodeURIComponent(path)}`);
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
      await loadTree();
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
    renderSelected();
    await loadConfig();
    await loadTasks({ keepSelection: false });
    if (state.selectedId) {
      await loadTree();
    }
  }

  wire("refreshTasks", async () => loadTasks({ keepSelection: true }));
  wire("createTask", createTask);
  wire("statusBtn", runStatus);
  wire("diffBtn", runDiff);
  wire("briefBtn", runAgentBrief);
  wire("runCommand", runCommand);
  wire("commitBtn", commitTask);
  wire("pushBtn", pushTask);
  wire("prBtn", openPr);
  wire("deleteTask", deleteTask);
  wire("loadTree", loadTree);
  wire("readFile", readFile);
  wire("writeFile", writeFile);
  wire("copyOutput", copyOutput);

  document.addEventListener("DOMContentLoaded", () => {
    init().catch((error) => setStatus(String(error && error.message ? error.message : error), true));
  });
})();
