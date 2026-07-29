(() => {
  const TASKS_PATH = "/ui/api/coding/tasks";
  const ACTIVE_AGENT_STATUSES = new Set(["queued", "running", "stopping", "pausing"]);
  let capturedTasks = [];
  let refreshPending = false;
  let reportInFlight = false;

  function agentInfo(task) {
    return task && task.agent && typeof task.agent === "object" ? task.agent : {};
  }

  function taskIntegration(task) {
    return task && task.integration && typeof task.integration === "object" ? task.integration : null;
  }

  function taskTitle(task) {
    const integration = taskIntegration(task);
    if (integration) return integration.display_name || integration.service_name || task.branch_name || task.id;
    return task.branch_name || task.id;
  }

  function agentIsActive(task) {
    return ACTIVE_AGENT_STATUSES.has(String(agentInfo(task).status || "").toLowerCase());
  }

  function taskNeedsAttention(task) {
    const taskStatus = String((task && task.status) || "").toLowerCase();
    const agentStatus = String(agentInfo(task).status || "").toLowerCase();
    return taskStatus === "error" || ["failed", "paused", "interrupted", "stopped"].includes(agentStatus);
  }

  function visibleTasks() {
    const search = document.getElementById("taskSearch");
    const filter = document.getElementById("taskFilter");
    const query = String(search && search.value || "").trim().toLowerCase();
    const mode = String(filter && filter.value || "all");
    return capturedTasks.filter((task) => {
      const agentStatus = String(agentInfo(task).status || "").toLowerCase();
      const taskStatus = String(task && task.status || "").toLowerCase();
      if (mode === "active" && !agentIsActive(task)) return false;
      if (mode === "attention" && !taskNeedsAttention(task)) return false;
      if (mode === "ready" && taskStatus !== "ready") return false;
      if (mode === "completed" && agentStatus !== "completed") return false;
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

  function selectedTaskId() {
    const active = document.querySelector("#tasks .task-item.active[data-debug-task-id]");
    return active ? String(active.dataset.debugTaskId || "") : "";
  }

  function setStatus(message, isError = false) {
    const status = document.getElementById("status");
    if (!status) return;
    status.textContent = message || "";
    status.className = isError ? "hint status error" : "hint status";
  }

  function pageIsBusy() {
    const refresh = document.getElementById("refreshTasks");
    return Boolean(refresh && refresh.disabled);
  }

  function refreshBindings() {
    refreshPending = false;
    const items = Array.from(document.querySelectorAll("#tasks .task-item"));
    const tasks = visibleTasks();
    items.forEach((item, index) => {
      const task = tasks[index];
      if (task && task.id) item.dataset.debugTaskId = String(task.id);
      else delete item.dataset.debugTaskId;
    });
    const button = document.getElementById("debugReportBtn");
    if (button) {
      const shouldDisable = reportInFlight || pageIsBusy() || !selectedTaskId();
      if (button.disabled !== shouldDisable) button.disabled = shouldDisable;
    }
  }

  function scheduleRefresh() {
    if (refreshPending) return;
    refreshPending = true;
    window.setTimeout(refreshBindings, 0);
  }

  function updateCapturedTask(task) {
    if (!task || !task.id) return;
    const index = capturedTasks.findIndex((item) => item && item.id === task.id);
    if (index >= 0) capturedTasks[index] = task;
    else capturedTasks.unshift(task);
    scheduleRefresh();
  }

  function captureTaskPayload(payload) {
    if (Array.isArray(payload && payload.tasks)) {
      capturedTasks = payload.tasks;
      scheduleRefresh();
    }
    if (payload && payload.task) updateCapturedTask(payload.task);
  }

  function watchTaskResponses() {
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const response = await originalFetch(input, init);
      try {
        const rawUrl = typeof input === "string" ? input : input && input.url;
        const url = new URL(rawUrl, window.location.origin);
        if (response.ok && url.pathname.startsWith(TASKS_PATH) && !url.pathname.endsWith("/debug-report")) {
          response.clone().json().then(captureTaskPayload).catch(() => {});
        }
      } catch (error) {
        // Diagnostics are best-effort and must never interfere with workspace requests.
      }
      return response;
    };
  }

  function filenameFromResponse(response, taskId) {
    const disposition = String(response.headers.get("Content-Disposition") || "");
    const match = disposition.match(/filename="?([^";]+)"?/i);
    return match && match[1] ? match[1] : `nexus-${taskId}-debug-report.md`;
  }

  function showReport(report, taskId) {
    const output = document.getElementById("output");
    const title = document.getElementById("outputTitle");
    if (output) output.textContent = report;
    if (title) title.textContent = `debug report · ${taskId}`;
    const panel = output && output.closest("details");
    if (panel) panel.open = true;
  }

  function downloadReport(report, filename) {
    const blob = new Blob([report], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.hidden = true;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function generateDebugReport() {
    const taskId = selectedTaskId();
    if (!taskId || reportInFlight || pageIsBusy()) return;
    reportInFlight = true;
    const button = document.getElementById("debugReportBtn");
    if (button) {
      button.disabled = true;
      button.textContent = "Generating report…";
    }
    setStatus("Collecting bounded, redacted workspace diagnostics…");
    try {
      const response = await window.fetch(`${TASKS_PATH}/${encodeURIComponent(taskId)}/debug-report`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      const report = await response.text();
      if (!response.ok) throw new Error(report || `HTTP ${response.status}`);
      showReport(report, taskId);
      downloadReport(report, filenameFromResponse(response, taskId));
      setStatus(`Debug report generated for ${taskId}, shown in Output, and downloaded.`);
    } catch (error) {
      setStatus(`Debug report failed: ${String(error && error.message ? error.message : error)}`, true);
    } finally {
      reportInFlight = false;
      if (button) button.textContent = "Debug report";
      scheduleRefresh();
    }
  }

  function installButton() {
    const toolbar = document.querySelector(".toolbar");
    if (!toolbar || document.getElementById("debugReportBtn")) return;
    const button = document.createElement("button");
    button.id = "debugReportBtn";
    button.type = "button";
    button.dataset.uiRole = "secondary";
    button.textContent = "Debug report";
    button.title = "Generate and download a bounded, redacted debugging report for the selected workspace";
    button.disabled = true;
    button.addEventListener("click", generateDebugReport);
    const copyButton = document.getElementById("copyOutput");
    toolbar.insertBefore(button, copyButton || null);
  }

  function start() {
    watchTaskResponses();
    installButton();
    const taskList = document.getElementById("tasks");
    if (taskList) {
      new MutationObserver(scheduleRefresh).observe(taskList, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["class"],
      });
    }
    const refresh = document.getElementById("refreshTasks");
    if (refresh) {
      new MutationObserver(scheduleRefresh).observe(refresh, {
        attributes: true,
        attributeFilter: ["disabled"],
      });
    }
    [document.getElementById("taskSearch"), document.getElementById("taskFilter")].forEach((element) => {
      if (!element) return;
      element.addEventListener("input", scheduleRefresh);
      element.addEventListener("change", scheduleRefresh);
    });
    scheduleRefresh();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
