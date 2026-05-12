(() => {
  const statusEl = document.getElementById("status");
  const runtimeMetaEl = document.getElementById("runtimeMeta");
  const summaryGridEl = document.getElementById("summaryGrid");
  const recurringEl = document.getElementById("recurring");
  const eventsEl = document.getElementById("events");
  const refreshEl = document.getElementById("refresh");
  const scanNowEl = document.getElementById("scanNow");
  const categoryFilterEl = document.getElementById("categoryFilter");
  const levelFilterEl = document.getElementById("levelFilter");

  let currentPayload = null;

  function setStatus(text, isError) {
    if (!statusEl) return;
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint status error" : "hint status";
  }

  function handle401(resp) {
    if (resp && resp.status === 401) {
      const back = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/ui/login?next=${back}`;
      return true;
    }
    return false;
  }

  function fmtTs(ts) {
    const value = Number(ts || 0);
    if (!Number.isFinite(value) || value <= 0) return "never";
    try { return new Date(value * 1000).toLocaleString(); } catch (error) { return String(ts); }
  }

  function badge(text, cls) {
    const span = document.createElement("span");
    span.className = `badge ${cls || "info"}`;
    span.textContent = text;
    return span;
  }

  async function fetchJson(url, options = {}) {
    const resp = await fetch(url, { credentials: "same-origin", ...options });
    if (handle401(resp)) throw new Error("authentication required");
    const text = await resp.text();
    const payload = text ? JSON.parse(text) : {};
    if (!resp.ok) {
      throw new Error(payload?.detail || payload?.error || `HTTP ${resp.status}`);
    }
    return payload;
  }

  function renderSummary(summary) {
    if (!summaryGridEl) return;
    summaryGridEl.innerHTML = "";
    const cards = [
      ["Coding attention", summary?.coding?.attention ?? 0],
      ["Auto-resumes", summary?.coding?.actions ?? 0],
      ["Task failures", summary?.scheduled_tasks?.failed ?? 0],
      ["Overdue tasks", summary?.scheduled_tasks?.overdue ?? 0],
      ["Backend issues", summary?.resources?.backend_issues ?? 0],
      ["Resource pressure", summary?.resources?.resource_pressure ?? 0],
      ["Queue pressure", summary?.resources?.queue_pressure ?? 0],
      ["Notifications", summary?.coding?.notifications ?? 0],
    ];
    cards.forEach(([label, value]) => {
      const card = document.createElement("div");
      card.className = "stat";
      const title = document.createElement("div");
      title.className = "stat-label";
      title.textContent = label;
      const val = document.createElement("div");
      val.className = "stat-value";
      val.textContent = String(value);
      card.appendChild(title);
      card.appendChild(val);
      summaryGridEl.appendChild(card);
    });
  }

  function renderRecurring(items) {
    if (!recurringEl) return;
    recurringEl.innerHTML = "";
    if (!Array.isArray(items) || !items.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "No recurring issues recorded yet.";
      recurringEl.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "recurring-item";
      const top = document.createElement("div");
      top.className = "row";
      top.style.justifyContent = "space-between";
      const title = document.createElement("div");
      title.style.fontWeight = "650";
      title.textContent = item.title || `${item.category} issue`;
      const count = badge(`${item.hit_count || 0} hits`, "warn");
      top.appendChild(title);
      top.appendChild(count);
      const meta = document.createElement("div");
      meta.className = "hint";
      meta.style.marginTop = "6px";
      meta.textContent = `${item.category || "general"} · last seen ${fmtTs(item.last_seen_ts)}`;
      const summary = document.createElement("div");
      summary.className = "summary";
      summary.textContent = item.summary || "";
      row.appendChild(top);
      row.appendChild(meta);
      row.appendChild(summary);
      recurringEl.appendChild(row);
    });
  }

  function renderEvents(items) {
    if (!eventsEl) return;
    eventsEl.innerHTML = "";
    const category = String(categoryFilterEl?.value || "").trim();
    const level = String(levelFilterEl?.value || "").trim();
    const filtered = (Array.isArray(items) ? items : []).filter((item) => {
      if (category && item.category !== category) return false;
      if (level && item.level !== level) return false;
      return true;
    });
    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "No Sentinel events match the current filters.";
      eventsEl.appendChild(empty);
      return;
    }
    filtered.forEach((item) => {
      const card = document.createElement("div");
      card.className = `event-card level-${item.level || "info"}`;
      const top = document.createElement("div");
      top.className = "event-top";
      const left = document.createElement("div");
      const title = document.createElement("div");
      title.className = "event-title";
      title.textContent = item.title || "Sentinel event";
      const meta = document.createElement("div");
      meta.className = "hint";
      meta.style.marginTop = "4px";
      meta.textContent = `${item.category || "general"} · ${item.event_type || "note"} · ${fmtTs(item.ts)}`;
      left.appendChild(title);
      left.appendChild(meta);
      top.appendChild(left);
      top.appendChild(badge(item.level || "info", item.level || "info"));
      const summary = document.createElement("div");
      summary.className = "summary";
      summary.textContent = item.summary || "";
      const reasoning = document.createElement("div");
      reasoning.className = "reasoning";
      reasoning.textContent = item.reasoning || "";
      card.appendChild(top);
      card.appendChild(summary);
      if (reasoning.textContent) card.appendChild(reasoning);
      eventsEl.appendChild(card);
    });
  }

  function renderPayload(payload) {
    currentPayload = payload;
    const runtime = payload?.runtime && typeof payload.runtime === "object" ? payload.runtime : {};
    if (runtimeMetaEl) {
      runtimeMetaEl.textContent = [
        runtime.running ? "running" : "stopped",
        `started ${fmtTs(runtime.started_at)}`,
        `last tick ${fmtTs(runtime.last_tick_finished_at)}`,
        runtime.last_error ? `last error: ${runtime.last_error}` : "no runtime error",
      ].join(" · ");
    }
    renderSummary(runtime.last_summary || {});
    renderRecurring(payload?.recurring || []);
    renderEvents(payload?.events || []);
  }

  async function loadStatus() {
    setStatus("Loading Sentinel status...");
    try {
      const payload = await fetchJson("/ui/api/sentinel/status?limit=160");
      renderPayload(payload);
      setStatus("");
    } catch (error) {
      setStatus(String(error), true);
    }
  }

  async function runScan() {
    setStatus("Running Sentinel check...");
    try {
      await fetchJson("/ui/api/sentinel/scan", { method: "POST" });
      await loadStatus();
      setStatus("Sentinel check completed.");
    } catch (error) {
      setStatus(String(error), true);
    }
  }

  if (refreshEl) refreshEl.addEventListener("click", () => { void loadStatus(); });
  if (scanNowEl) scanNowEl.addEventListener("click", () => { void runScan(); });
  if (categoryFilterEl) categoryFilterEl.addEventListener("change", () => renderEvents(currentPayload?.events || []));
  if (levelFilterEl) levelFilterEl.addEventListener("change", () => renderEvents(currentPayload?.events || []));

  void loadStatus();
})();