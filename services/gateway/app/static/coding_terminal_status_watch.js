(() => {
  const TERMINAL = new Set(["failed", "paused", "interrupted", "stopped", "completed"]);
  const ACTIVE = new Set(["queued", "running", "stopping", "pausing"]);
  const POLL_MS = 4000;
  const GRACE_MS = 30000;

  const statusEl = document.getElementById("agentStatus");
  const refreshBtn = document.getElementById("refreshTasks");
  if (!statusEl || !refreshBtn) return;

  let timer = null;
  let deadline = 0;
  let lastStatus = "";

  function statusValue() {
    return String(statusEl.textContent || "").trim().toLowerCase();
  }

  function stopTimer() {
    if (timer) window.clearInterval(timer);
    timer = null;
    deadline = 0;
  }

  function tick() {
    const status = statusValue();
    if (ACTIVE.has(status)) {
      stopTimer();
      return;
    }
    if (!TERMINAL.has(status) || Date.now() >= deadline) {
      stopTimer();
      return;
    }
    if (document.visibilityState !== "visible" || refreshBtn.disabled) return;
    refreshBtn.click();
  }

  function observeStatus() {
    const status = statusValue();
    if (status === lastStatus) return;
    lastStatus = status;
    if (ACTIVE.has(status)) {
      stopTimer();
      return;
    }
    if (!TERMINAL.has(status)) return;
    deadline = Date.now() + GRACE_MS;
    if (!timer) timer = window.setInterval(tick, POLL_MS);
  }

  const observer = new MutationObserver(observeStatus);
  observer.observe(statusEl, { childList: true, characterData: true, subtree: true });
  observeStatus();
  window.addEventListener("beforeunload", stopTimer);
})();
