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
  let archivePollGeneration = 0;

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

  function archiveModeValue(analysis) {
    const mode = String(analysis?.requested_mode || "idle").trim().toLowerCase();
    const target = String(analysis?.target || "local").trim().toLowerCase();
    if (target === "local") return mode === "immediate" ? "immediate_local" : "idle_local";
    if (target === "external") return "external_agent";
    return "human_followup";
  }

  function archiveModePayload(value) {
    if (value === "immediate_local") return { analysis_mode: "immediate", analysis_target: "local" };
    if (value === "idle_local") return { analysis_mode: "idle", analysis_target: "local" };
    if (value === "external_agent") return { analysis_mode: "manual", analysis_target: "external" };
    return { analysis_mode: "manual", analysis_target: "human" };
  }

  function syncArchiveModeUi(modeSelect, modelSelect, analyzeBtn) {
    const localMode = ["immediate_local", "idle_local"].includes(String(modeSelect?.value || ""));
    if (modelSelect) modelSelect.disabled = !localMode;
    if (analyzeBtn) {
      analyzeBtn.disabled = !localMode;
      analyzeBtn.title = localMode ? "Run local Sentinel analysis now" : "Immediate analysis is available only for local-model modes.";
    }
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

  async function runControlAction(button, busyText, action) {
    const originalText = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = busyText;
    }
    try {
      await action();
    } catch (error) {
      setStatus(String(error && error.message ? error.message : error), true);
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
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

  async function saveArchiveSettings(archiveId, body, { successText = "Archive settings updated." } = {}) {
    setStatus(`Saving settings for ${archiveId}...`);
    await fetchJson(`/ui/api/sentinel/archives/${encodeURIComponent(archiveId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await loadStatus({ quiet: true });
    setStatus(successText);
  }

  function findArchive(archiveId) {
    const archives = Array.isArray(currentPayload?.archives) ? currentPayload.archives : [];
    return archives.find((item) => String(item?.archive_id || "") === String(archiveId || "")) || null;
  }

  async function pollArchiveAnalysis(archiveId) {
    const generation = ++archivePollGeneration;
    for (let attempt = 0; attempt < 300; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 2000));
      if (generation !== archivePollGeneration) return;
      await loadStatus({ quiet: true });
      const archive = findArchive(archiveId);
      const status = String(archive?.analysis?.status || "").toLowerCase();
      if (!["pending", "running"].includes(status)) {
        const failed = status.includes("failed");
        const detail = archive?.analysis?.last_error || archive?.analysis?.last_summary || "";
        setStatus(
          failed
            ? `Analysis failed for ${archiveId}${detail ? `: ${detail}` : "."}`
            : `Analysis ${status || "finished"} for ${archiveId}${detail ? `: ${detail}` : "."}`,
          failed
        );
        return;
      }
    }
    setStatus(`Analysis is still running for ${archiveId}. Refresh to check its latest status.`);
  }

  async function requestArchiveAnalysis(archiveId, { analysisModel, preserve } = {}) {
    setStatus(`Starting immediate analysis for ${archiveId}...`);
    const payload = await fetchJson(`/ui/api/sentinel/archives/${encodeURIComponent(archiveId)}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ analysis_model: analysisModel || "coder", preserve: Boolean(preserve) }),
    });
    await loadStatus({ quiet: true });
    setStatus(
      payload?.already_running
        ? `Analysis is already running for ${archiveId}.`
        : `Immediate analysis started for ${archiveId}.`
    );
    void pollArchiveAnalysis(archiveId).catch((error) => setStatus(String(error && error.message ? error.message : error), true));
  }

  async function purgeArchive(archiveId) {
    const ok = window.confirm(`Permanently erase archived workspace ${archiveId}? This deletes the archived workspace tree, task metadata, and findings log.`);
    if (!ok) return;
    await fetchJson(`/ui/api/sentinel/archives/${encodeURIComponent(archiveId)}`, { method: "DELETE" });
    await loadStatus();
    setStatus(`Archived workspace ${archiveId} was erased.`);
  }

  async function reviewArchiveFinding(archiveId, findingTs, verdict) {
    const note = window.prompt(`Add an optional note for marking this finding ${verdict}.`, "") || "";
    await fetchJson(`/ui/api/sentinel/archives/${encodeURIComponent(archiveId)}/findings/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ finding_ts: findingTs, verdict, note }),
    });
    await loadStatus();
    setStatus(`Finding marked ${verdict}.`);
  }

  function renderArchiveFindings(items, archiveId) {
    const wrap = document.createElement("div");
    wrap.className = "archive-findings";
    if (!Array.isArray(items) || !items.length) {
      const empty = document.createElement("div");
      empty.className = "archive-empty";
      empty.textContent = "No findings recorded yet.";
      wrap.appendChild(empty);
      return wrap;
    }
    items.slice(0, 6).forEach((item) => {
      const row = document.createElement("div");
      row.className = "archive-finding";
      const head = document.createElement("div");
      head.className = "archive-finding-head";
      const meta = document.createElement("div");
      meta.className = "hint";
      meta.textContent = `${fmtTs(item.ts)} · ${item.kind || item.actor || "finding"}`;
      head.appendChild(meta);
      if (item.review && typeof item.review === "object") {
        head.appendChild(badge(`reviewed: ${item.review.verdict || "reviewed"}`, "warn"));
      }
      const summary = document.createElement("div");
      summary.className = "summary";
      summary.textContent = item.summary || item.text || "";
      row.appendChild(head);
      row.appendChild(summary);
      if (item.review && typeof item.review === "object") {
        const review = document.createElement("div");
        review.className = "archive-finding-review";
        review.textContent = `${item.review.verdict || "reviewed"} by ${item.review.actor || "admin"} at ${fmtTs(item.review.ts)}${item.review.note ? ` · ${item.review.note}` : ""}`;
        row.appendChild(review);
      }
      if (item.ts) {
        const actions = document.createElement("div");
        actions.className = "archive-finding-actions";
        const supersedeBtn = document.createElement("button");
        supersedeBtn.type = "button";
        supersedeBtn.textContent = "Mark superseded";
        supersedeBtn.addEventListener("click", () => {
          void reviewArchiveFinding(archiveId, item.ts, "superseded").catch((error) => setStatus(String(error), true));
        });
        const invalidBtn = document.createElement("button");
        invalidBtn.type = "button";
        invalidBtn.textContent = "Mark invalid";
        invalidBtn.addEventListener("click", () => {
          void reviewArchiveFinding(archiveId, item.ts, "invalid").catch((error) => setStatus(String(error), true));
        });
        actions.appendChild(supersedeBtn);
        actions.appendChild(invalidBtn);
        row.appendChild(actions);
      }
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
        ["External brief", pathValues.external_brief],
      ].forEach(([label, value]) => {
        if (!value) return;
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
          { value: "immediate_local", label: "Analyze immediately (Local model)" },
          { value: "idle_local", label: "Analyze on idle (Local model)" },
          { value: "human_followup", label: "Human follow-up" },
          { value: "external_agent", label: "Flag for external agent" },
        ],
        archiveModeValue(analysis)
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
      card.appendChild(renderArchiveFindings(item.findings, item.archive_id));

      const actions = document.createElement("div");
      actions.className = "archive-actions";
      const saveBtn = document.createElement("button");
      saveBtn.type = "button";
      saveBtn.textContent = "Save settings";
      saveBtn.addEventListener("click", () => {
        const modePayload = archiveModePayload(modeSelect.value);
        void runControlAction(saveBtn, "Saving...", () => saveArchiveSettings(item.archive_id, {
          analysis_mode: modePayload.analysis_mode,
          analysis_target: modePayload.analysis_target,
          analysis_model: modelSelect.disabled ? "" : modelSelect.value,
          preserve: preserveBox.checked,
        }));
      });
      const analyzeBtn = document.createElement("button");
      analyzeBtn.type = "button";
      analyzeBtn.textContent = "Analyze now";
      analyzeBtn.addEventListener("click", () => {
        const modePayload = archiveModePayload(modeSelect.value);
        if (modePayload.analysis_target !== "local") return;
        void runControlAction(analyzeBtn, "Starting...", () => requestArchiveAnalysis(item.archive_id, {
          analysisModel: modelSelect.value,
          preserve: preserveBox.checked,
        }));
      });
      modeSelect.addEventListener("change", () => {
        syncArchiveModeUi(modeSelect, modelSelect, analyzeBtn);
        if (modeSelect.value === "immediate_local") {
          void runControlAction(analyzeBtn, "Starting...", () => requestArchiveAnalysis(item.archive_id, {
            analysisModel: modelSelect.value,
            preserve: preserveBox.checked,
          }));
        }
      });
      syncArchiveModeUi(modeSelect, modelSelect, analyzeBtn);
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

  async function loadStatus({ quiet = false } = {}) {
    if (!quiet) setStatus("Loading Sentinel status...");
    try {
      const payload = await fetchJson("/ui/api/sentinel/status?limit=200");
      renderPayload(payload);
      if (!quiet) setStatus("");
    } catch (error) {
      setStatus(String(error), true);
      if (quiet) throw error;
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
