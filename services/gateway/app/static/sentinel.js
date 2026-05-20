(() => {
  const statusEl = document.getElementById("status");
  const runtimeMetaEl = document.getElementById("runtimeMeta");
  const summaryGridEl = document.getElementById("summaryGrid");
  const recurringEl = document.getElementById("recurring");
  const eventsEl = document.getElementById("events");
  const archivesEl = document.getElementById("archives");
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
    try {
      return new Date(value * 1000).toLocaleString();
    } catch (error) {
      return String(ts);
    }
  }

  function badge(text, cls) {
    const span = document.createElement("span");
    span.className = `badge ${cls || "info"}`;
    span.textContent = text;
    return span;
  }

  function createSelect(options, value) {
    const select = document.createElement("select");
    options.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      select.appendChild(option);
    });
    select.value = value;
    return select;
  }

  function createControlLabel(text, control) {
    const label = document.createElement("label");
    label.className = "control";
    const title = document.createElement("span");
    title.textContent = text;
    label.appendChild(title);
    label.appendChild(control);
    return label;
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
      ["Archived pending", summary?.archives?.pending ?? 0],
      ["Archived analyzed", summary?.archives?.analyzed ?? 0],
      ["Archived purged", summary?.archives?.purged ?? 0],
      ["Task failures", summary?.scheduled_tasks?.failed ?? 0],
      ["Backend issues", summary?.resources?.backend_issues ?? 0],
      ["Queue pressure", summary?.resources?.queue_pressure ?? 0],
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

  async function saveArchiveSettings(archiveId, body, { runScan = false, successText = "Archive settings updated." } = {}) {
    await fetchJson(`/ui/api/sentinel/archives/${encodeURIComponent(archiveId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (runScan) {
      await fetchJson("/ui/api/sentinel/scan", { method: "POST" });
    }
    await loadStatus();
    setStatus(successText);
  }

  async function purgeArchive(archiveId) {
    const ok = window.confirm(`Permanently erase archived workspace ${archiveId}? This deletes the archived workspace tree, task metadata, and findings log.`);
    if (!ok) return;
    await fetchJson(`/ui/api/sentinel/archives/${encodeURIComponent(archiveId)}`, { method: "DELETE" });
    await loadStatus();
    setStatus(`Archived workspace ${archiveId} was erased.`);
  }

  function renderArchiveFindings(items) {
    const wrap = document.createElement("div");
    wrap.className = "archive-findings";
    if (!Array.isArray(items) || !items.length) {
      const empty = document.createElement("div");
      empty.className = "archive-empty";
      empty.textContent = "No findings recorded yet.";
      wrap.appendChild(empty);
      return wrap;
    }
    items.slice(0, 4).forEach((item) => {
      const row = document.createElement("div");
      row.className = "archive-finding";
      const meta = document.createElement("div");
      meta.className = "hint";
      meta.textContent = `${fmtTs(item.ts)} · ${item.kind || item.actor || "finding"}`;
      const summary = document.createElement("div");
      summary.className = "summary";
      summary.textContent = item.summary || item.text || "";
      row.appendChild(meta);
      row.appendChild(summary);
      wrap.appendChild(row);
    });
    return wrap;
  }

  function renderArchives(items, modelChoices) {
    if (!archivesEl) return;
    archivesEl.innerHTML = "";
    const models = Array.isArray(modelChoices) && modelChoices.length ? modelChoices : ["coder", "default", "fast"];
    if (!Array.isArray(items) || !items.length) {
      const empty = document.createElement("div");
      empty.className = "archive-empty";
      empty.textContent = "No archived workspaces recorded yet.";
      archivesEl.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const card = document.createElement("div");
      card.className = "archive-card";

      const top = document.createElement("div");
      top.className = "event-top";
      const left = document.createElement("div");
      const title = document.createElement("div");
      title.className = "archive-title";
      title.textContent = item.archive_id || item.task_id || "Archived workspace";
      const meta = document.createElement("div");
      meta.className = "hint";
      meta.style.marginTop = "4px";
      meta.textContent = [
        item.task_id || "",
        item.owner ? `owner ${item.owner}` : "",
        item.reason || "",
        `archived ${fmtTs(item.archived_at)}`,
      ].filter(Boolean).join(" · ");
      left.appendChild(title);
      left.appendChild(meta);
      top.appendChild(left);
      top.appendChild(badge((item.analysis || {}).status || "pending", ((item.analysis || {}).status || "pending").includes("failed") ? "error" : "info"));
      card.appendChild(top);

      if (item.prompt) {
        const prompt = document.createElement("div");
        prompt.className = "summary";
        prompt.textContent = item.prompt;
        card.appendChild(prompt);
      }

      const paths = document.createElement("div");
      paths.className = "archive-paths";
      const pathValues = item.paths && typeof item.paths === "object" ? item.paths : {};
      [
        ["Workspace", pathValues.workspace],
        ["Task file", pathValues.task],
        ["Manifest", pathValues.manifest],
        ["Findings", pathValues.findings],
      ].forEach(([label, value]) => {
        const line = document.createElement("div");
        line.className = "archive-path";
        line.textContent = `${label}: ${value || "missing"}`;
        paths.appendChild(line);
      });
      card.appendChild(paths);

      const controls = document.createElement("div");
      controls.className = "archive-controls";
      const analysis = item.analysis && typeof item.analysis === "object" ? item.analysis : {};
      const retention = item.retention && typeof item.retention === "object" ? item.retention : {};
      const modeSelect = createSelect(
        [
          { value: "manual", label: "Manual only" },
          { value: "idle", label: "Analyze on idle" },
          { value: "immediate", label: "Analyze immediately" },
        ],
        analysis.requested_mode || "idle"
      );
      const targetSelect = createSelect(
        [
          { value: "local", label: "Local model" },
          { value: "human", label: "Human follow-up" },
          { value: "external", label: "External agent" },
          { value: "none", label: "Do not analyze" },
        ],
        analysis.target || "local"
      );
      const modelSelect = createSelect(
        models.map((value) => ({ value, label: value })),
        analysis.local_model || "coder"
      );
      const preserveWrap = document.createElement("label");
      preserveWrap.className = "control";
      const preserveText = document.createElement("span");
      preserveText.textContent = "Preserve archive";
      const preserveBox = document.createElement("input");
      preserveBox.type = "checkbox";
      preserveBox.checked = Boolean(retention.preserve);
      preserveWrap.appendChild(preserveText);
      preserveWrap.appendChild(preserveBox);
      controls.appendChild(createControlLabel("Analysis mode", modeSelect));
      controls.appendChild(createControlLabel("Analysis target", targetSelect));
      controls.appendChild(createControlLabel("Local model", modelSelect));
      controls.appendChild(preserveWrap);
      card.appendChild(controls);

      const retentionMeta = document.createElement("div");
      retentionMeta.className = "hint";
      retentionMeta.style.marginTop = "8px";
      retentionMeta.textContent = retention.preserve
        ? "Retention: preserved until manually erased."
        : `Retention: delete after ${fmtTs(retention.delete_after_ts)}.`;
      card.appendChild(retentionMeta);

      const findingsTitle = document.createElement("div");
      findingsTitle.className = "hint";
      findingsTitle.style.marginTop = "10px";
      findingsTitle.textContent = "Findings log";
      card.appendChild(findingsTitle);
      card.appendChild(renderArchiveFindings(item.findings));

      const actions = document.createElement("div");
      actions.className = "archive-actions";
      const saveBtn = document.createElement("button");
      saveBtn.type = "button";
      saveBtn.textContent = "Save settings";
      saveBtn.addEventListener("click", () => {
        void saveArchiveSettings(item.archive_id, {
          analysis_mode: modeSelect.value,
          analysis_target: targetSelect.value,
          analysis_model: modelSelect.value,
          preserve: preserveBox.checked,
        });
      });
      const analyzeBtn = document.createElement("button");
      analyzeBtn.type = "button";
      analyzeBtn.textContent = "Analyze now";
      analyzeBtn.addEventListener("click", async () => {
        const body = {
          analysis_mode: "immediate",
          analysis_target: targetSelect.value,
          analysis_model: modelSelect.value,
          preserve: preserveBox.checked,
        };
        if (targetSelect.value !== "local") {
          await saveArchiveSettings(item.archive_id, body, {
            runScan: false,
            successText: "Archive updated. Automatic immediate analysis currently runs only for target=local.",
          });
          return;
        }
        await saveArchiveSettings(item.archive_id, body, {
          runScan: true,
          successText: `Sentinel queued immediate analysis for ${item.archive_id}.`,
        });
      });
      const purgeBtn = document.createElement("button");
      purgeBtn.type = "button";
      purgeBtn.textContent = "Erase archive";
      purgeBtn.addEventListener("click", () => {
        void purgeArchive(item.archive_id).catch((error) => setStatus(String(error), true));
      });
      actions.appendChild(saveBtn);
      actions.appendChild(analyzeBtn);
      actions.appendChild(purgeBtn);
      card.appendChild(actions);
      archivesEl.appendChild(card);
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
    renderArchives(payload?.archives || [], payload?.archive_model_choices || []);
  }

  async function loadStatus() {
    setStatus("Loading Sentinel status...");
    try {
      const payload = await fetchJson("/ui/api/sentinel/status?limit=200");
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