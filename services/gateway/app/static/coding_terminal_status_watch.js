(() => {
  // Sentinel can legitimately resurrect a workspace after the Coding UI has
  // already observed a failed/paused state. Keep a lightweight supervisory
  // watch on those recoverable states instead of treating them as final.
  const RECOVERABLE = new Set(["failed", "paused", "interrupted", "stopped"]);
  const ACTIVE = new Set(["queued", "running", "stopping", "pausing"]);
  const FINAL = new Set(["completed"]);
  const SUPERVISORY_POLL_MS = 15000;
  const TASK_ID_RE = /\bcode_[a-f0-9]{12}\b/i;

  const statusEl = document.getElementById("agentStatus");
  const refreshBtn = document.getElementById("refreshTasks");
  if (!statusEl || !refreshBtn) return;

  let timer = null;
  let lastStatus = "";

  function statusValue() {
    return String(statusEl.textContent || "").trim().toLowerCase();
  }

  function selectedTaskId() {
    const activeCard = document.querySelector(".task-item.active");
    if (!activeCard) return "";
    const match = String(activeCard.textContent || "").match(TASK_ID_RE);
    return match ? match[0] : "";
  }

  function stopTimer() {
    if (timer) window.clearInterval(timer);
    timer = null;
  }

  async function tick() {
    const status = statusValue();
    if (!RECOVERABLE.has(status)) {
      stopTimer();
      return;
    }
    if (document.visibilityState !== "visible") return;

    const taskId = selectedTaskId();
    if (!taskId) return;

    try {
      const resp = await fetch(`/ui/api/coding/tasks/${encodeURIComponent(taskId)}`, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!resp.ok) return;
      const payload = await resp.json();
      const task = payload && payload.task && typeof payload.task === "object" ? payload.task : null;
      const agent = task && task.agent && typeof task.agent === "object" ? task.agent : null;
      const serverStatus = String((agent && agent.status) || "").trim().toLowerCase();
      if (!serverStatus || serverStatus === status || refreshBtn.disabled) return;

      // A status transition occurred out-of-band (most importantly Sentinel's
      // failed -> queued recovery). Let the main UI perform one canonical full
      // refresh; its existing active-run poller takes over from there.
      refreshBtn.click();
    } catch (_error) {
      // Supervisory recovery visibility is best-effort. The normal UI remains
      // usable and the next interval will retry.
    }
  }

  function updateWatch() {
    const status = statusValue();
    if (ACTIVE.has(status) || FINAL.has(status) || !RECOVERABLE.has(status)) {
      stopTimer();
      return;
    }
    if (!timer) timer = window.setInterval(tick, SUPERVISORY_POLL_MS);
  }

  function observeStatus() {
    const status = statusValue();
    if (status === lastStatus) return;
    lastStatus = status;
    updateWatch();
  }

  const observer = new MutationObserver(observeStatus);
  observer.observe(statusEl, { childList: true, characterData: true, subtree: true });
  observeStatus();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") updateWatch();
  });
  window.addEventListener("beforeunload", stopTimer);
})();
