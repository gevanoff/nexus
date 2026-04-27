(() => {
  function handle401(resp) {
    if (resp && resp.status === 401) {
      const back = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/ui/login?next=${back}`;
      return true;
    }
    return false;
  }

  function ensureStatusEl(container) {
    if (!container) return null;
    let el = container.querySelector(".lifecycle-status");
    if (!el) {
      el = document.createElement("div");
      el.className = "hint lifecycle-status";
      el.style.marginTop = "8px";
      container.appendChild(el);
    }
    return el;
  }

  function labelDecision(payload) {
    const decision = String(payload?.decision || "").replace(/_/g, " ");
    if (!decision) return "";
    if (payload?.ok === true && (payload.decision === "already_active" || payload.decision === "activate" || payload.decision === "swap")) {
      return `Lifecycle: ${decision}.`;
    }
    const msg = String(payload?.message || "").trim();
    return msg ? `Lifecycle: ${decision}. ${msg}` : `Lifecycle: ${decision}.`;
  }

  async function ensureBackend(backendClass, routeKind, statusEl) {
    if (!backendClass) return;
    if (statusEl) statusEl.textContent = `Lifecycle: checking ${backendClass}...`;
    try {
      const resp = await fetch("/ui/api/lifecycle/ensure", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          backend_class: backendClass,
          route_kind: routeKind || "",
          reason: `ui:${window.location.pathname}`,
        }),
      });
      if (handle401(resp)) return;
      if (!resp.ok) {
        if (resp.status === 503) {
          if (statusEl) statusEl.textContent = "Lifecycle manager is not available.";
          return;
        }
        throw new Error(`HTTP ${resp.status}`);
      }
      const payload = await resp.json();
      if (statusEl) {
        statusEl.textContent = labelDecision(payload);
        statusEl.className = payload?.ok === false ? "hint error lifecycle-status" : "hint lifecycle-status";
      }
    } catch (error) {
      if (statusEl) {
        statusEl.textContent = `Lifecycle check failed: ${String(error?.message || error)}`;
        statusEl.className = "hint error lifecycle-status";
      }
    }
  }

  function init() {
    document.querySelectorAll("[data-lifecycle-ensure]").forEach((el) => {
      const backends = String(el.getAttribute("data-lifecycle-ensure") || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
      if (!backends.length) return;
      const routeKind = String(el.getAttribute("data-lifecycle-route") || "").trim();
      const statusEl = ensureStatusEl(el);
      backends.forEach((backendClass) => {
        void ensureBackend(backendClass, routeKind, statusEl);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
