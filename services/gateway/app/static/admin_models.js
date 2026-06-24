(() => {
  const statusEl = document.getElementById("admin_models_status");
  const listEl = document.getElementById("admin_models_list");
  const refreshEl = document.getElementById("admin_models_refresh");
  let hugeLanePollTimer = null;

  function handle401(resp) {
    if (resp && resp.status === 401) {
      const back = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/ui/login?next=${back}`;
      return true;
    }
    return false;
  }

  function formatTimestamp(ts) {
    if (!Number.isFinite(ts) || ts <= 0) return "";
    try {
      return new Date(ts * 1000).toLocaleString();
    } catch (error) {
      return "";
    }
  }

  function formatDuration(value) {
    const sec = Number(value || 0);
    if (!Number.isFinite(sec) || sec <= 0) return "0s";
    if (sec < 90) return `${Math.round(sec)}s`;
    return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  }

  function badge(text, tone) {
    const el = document.createElement("span");
    el.className = `model-admin-badge${tone ? ` ${tone}` : ""}`;
    el.textContent = text;
    return el;
  }

  function row(left, middle, badges, actions) {
    const el = document.createElement("div");
    el.className = "model-admin-row";
    const name = document.createElement("div");
    name.className = "model-admin-name";
    name.textContent = left;
    const detail = document.createElement("div");
    detail.textContent = middle;
    detail.className = "model-admin-muted";
    const badgeWrap = document.createElement("div");
    badgeWrap.className = "model-admin-badges";
    (badges || []).forEach((item) => badgeWrap.appendChild(badge(item.text, item.tone)));
    (actions || []).forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = item.text;
      button.dataset.uiRole = item.role || "secondary";
      button.disabled = item.disabled === true;
      button.addEventListener("click", item.onClick);
      badgeWrap.appendChild(button);
    });
    el.appendChild(name);
    el.appendChild(detail);
    el.appendChild(badgeWrap);
    return el;
  }

  function group(title) {
    const el = document.createElement("div");
    el.className = "model-admin-group";
    const heading = document.createElement("div");
    heading.className = "model-admin-title";
    heading.textContent = title;
    el.appendChild(heading);
    return el;
  }

  function scheduleHugeLanePoll(active) {
    if (!active) {
      if (hugeLanePollTimer) window.clearTimeout(hugeLanePollTimer);
      hugeLanePollTimer = null;
      return;
    }
    if (hugeLanePollTimer) return;
    hugeLanePollTimer = window.setTimeout(() => {
      hugeLanePollTimer = null;
      void load();
    }, 5000);
  }

  function renderHugeLane(lane) {
    if (!lane || lane.enabled === false) {
      scheduleHugeLanePoll(false);
      return;
    }
    const hugeGroup = group("MLX Huge Model");
    hugeGroup.classList.add("model-admin-huge");
    const statusBadges = [
      {
        text: lane.status_label || lane.status || "unknown",
        tone: lane.status === "ready" ? "green" : lane.status === "switching" ? "yellow" : "red",
      },
    ];
    if (lane.target_model) statusBadges.push({ text: `target ${lane.target_model}`, tone: "yellow" });
    if (lane.error) statusBadges.push({ text: String(lane.error).slice(0, 120), tone: "red" });
    hugeGroup.appendChild(row(lane.active_model || "No active huge model", lane.route_model ? `routing ${lane.route_model}` : "", statusBadges));

    if (lane.status === "switching") {
      const progress = document.createElement("div");
      const elapsed = Number(lane.elapsed_sec || 0);
      const estimate = Math.max(1, Number(lane.estimated_load_sec || 120));
      progress.className = "model-admin-muted";
      progress.textContent = `Loading ${formatDuration(elapsed)} / ${formatDuration(estimate)}`;
      const track = document.createElement("div");
      track.className = "model-admin-progress";
      const fill = document.createElement("span");
      fill.style.width = `${Math.max(0, Math.min(100, (elapsed / estimate) * 100)).toFixed(0)}%`;
      track.appendChild(fill);
      progress.appendChild(track);
      hugeGroup.appendChild(progress);
    }

    const candidates = Array.isArray(lane.candidates) ? lane.candidates : [];
    candidates.forEach((candidate) => {
      const badges = [];
      if (candidate.active) badges.push({ text: "active", tone: "green" });
      if (candidate.target) badges.push({ text: "loading", tone: "yellow" });
      if (candidate.cache_state) badges.push({ text: candidate.cache_state, tone: candidate.cache_state === "cached" ? "green" : "yellow" });
      const actions = [];
      if (!candidate.active && !candidate.target) {
        actions.push({
          text: "Switch",
          role: "secondary",
          disabled: candidate.cache_state !== "cached" || lane.status === "switching",
          onClick: () => void switchHugeLane(candidate.model, false),
        });
      }
      const details = [candidate.model];
      if (candidate.estimated_load_sec) details.push(`est ${formatDuration(candidate.estimated_load_sec)}`);
      if (candidate.estimated_memory_gb) details.push(`~${candidate.estimated_memory_gb} GB`);
      hugeGroup.appendChild(row(candidate.label || candidate.model || "unknown", details.join(" · "), badges, actions));
    });
    listEl.appendChild(hugeGroup);
    scheduleHugeLanePoll(lane.status === "switching");
  }

  function render(payload) {
    if (!listEl) return;
    listEl.innerHTML = "";
    if (statusEl) {
      const updated = formatTimestamp(Number(payload?.generated_at));
      statusEl.textContent = updated ? `Updated ${updated}` : "";
    }

    const aliases = Array.isArray(payload?.aliases) ? payload.aliases : [];
    const models = Array.isArray(payload?.models) ? payload.models : [];
    const backends = Array.isArray(payload?.backends) ? payload.backends : [];

    renderHugeLane(payload?.mlx_huge_lane || null);

    const aliasGroup = group("Aliases");
    if (!aliases.length) {
      aliasGroup.appendChild(row("None", "", []));
    } else {
      aliases.forEach((alias) => {
        const effective = `${alias.effective_backend || alias.backend}:${alias.effective_model || ""}`;
        const configured = `${alias.backend || ""}:${alias.configured_model || ""}`;
        const badges = [{ text: alias.visible ? "visible" : "hidden", tone: alias.visible ? "green" : "red" }];
        if (alias.unavailable_reason) badges.push({ text: alias.unavailable_reason, tone: "yellow" });
        aliasGroup.appendChild(row(alias.alias || "", `${configured} -> ${effective}`, badges));
      });
    }
    listEl.appendChild(aliasGroup);

    const modelGroup = group("Models");
    if (!models.length) {
      modelGroup.appendChild(row("None", "", []));
    } else {
      models.forEach((model) => {
        const badges = [{ text: model.selectable ? "selectable" : "not selectable", tone: model.selectable ? "green" : "red" }];
        if (model.cache_state) badges.push({ text: model.cache_state, tone: model.cache_state === "cached" ? "green" : "yellow" });
        const activity = model.fetch_activity && typeof model.fetch_activity === "object" ? model.fetch_activity : null;
        if (activity?.status === "active") badges.push({ text: "downloading", tone: "green" });
        if (activity?.status === "stalled") badges.push({ text: "stalled/stopped", tone: "red" });
        if (activity?.status === "unknown") badges.push({ text: "fetch unknown", tone: "yellow" });
        if (model.unavailable_reason) badges.push({ text: model.unavailable_reason, tone: "yellow" });
        if (model.cache_only) badges.push({ text: "cache only", tone: "yellow" });
        if (model.advertised) badges.push({ text: "advertised", tone: "" });
        const aliasText = Array.isArray(model.aliases) && model.aliases.length ? `aliases: ${model.aliases.join(", ")}` : model.provider || "";
        const actions = [];
        if (model.provider === "mlx" && model.cache_state === "fetching" && activity?.status !== "active") {
          actions.push({
            text: "Restart fetch",
            role: "secondary",
            onClick: () => void restartFetch(model.backend || "local_mlx", model.model || ""),
          });
        }
        modelGroup.appendChild(row(`${model.backend || ""}:${model.model || ""}`, aliasText, badges, actions));
      });
    }
    listEl.appendChild(modelGroup);

    const backendGroup = group("Backends");
    if (!backends.length) {
      backendGroup.appendChild(row("None", "", []));
    } else {
      backends.forEach((backend) => {
        const badges = [
          {
            text: backend.ready === true ? "ready" : backend.ready === false ? "not ready" : "unknown",
            tone: backend.ready === true ? "green" : backend.ready === false ? "red" : "yellow",
          },
        ];
        if (backend.error) badges.push({ text: String(backend.error).slice(0, 80), tone: "red" });
        backendGroup.appendChild(row(backend.backend || "", backend.hostname || backend.base_url || "", badges));
      });
    }
    listEl.appendChild(backendGroup);
  }

  async function load() {
    if (statusEl) statusEl.textContent = "Loading...";
    if (refreshEl) refreshEl.disabled = true;
    try {
      const resp = await fetch("/ui/api/admin/models", { method: "GET", credentials: "same-origin" });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        if (statusEl) statusEl.textContent = resp.status === 403 ? "Admin required." : `HTTP ${resp.status}: ${text}`;
        if (listEl) listEl.innerHTML = "";
        return;
      }
      render(JSON.parse(text));
    } catch (error) {
      if (statusEl) statusEl.textContent = `Error: ${String(error)}`;
    } finally {
      if (refreshEl) refreshEl.disabled = false;
    }
  }

  async function restartFetch(backend, model) {
    if (!model) return;
    if (statusEl) statusEl.textContent = `Starting prefetch for ${model}...`;
    try {
      const resp = await fetch("/ui/api/admin/models/prefetch", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ backend, model }),
      });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        if (statusEl) statusEl.textContent = `Prefetch restart failed: HTTP ${resp.status}: ${text}`;
        return;
      }
      const payload = JSON.parse(text);
      if (statusEl) statusEl.textContent = `Prefetch started for ${model}${payload.pid ? ` (pid ${payload.pid})` : ""}.`;
      window.setTimeout(() => void load(), 1500);
    } catch (error) {
      if (statusEl) statusEl.textContent = `Prefetch restart failed: ${String(error)}`;
    }
  }

  async function switchHugeLane(model, confirmed) {
    const modelId = String(model || "").trim();
    if (!modelId) return;
    if (statusEl) statusEl.textContent = `Switching MLX huge model to ${modelId}...`;
    try {
      const resp = await fetch("/ui/api/mlx/huge-lane/switch", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelId, confirmed }),
      });
      if (handle401(resp)) return;
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const detail = payload?.detail;
        const message = typeof detail === "string" ? detail : detail?.error ? `${detail.error}: ${detail.cache_state || ""}` : `HTTP ${resp.status}`;
        throw new Error(message);
      }
      if (payload?.decision === "requires_confirmation" && !confirmed) {
        const ok = window.confirm(payload.message || `Switch MLX huge model to ${modelId}?`);
        if (ok) return switchHugeLane(modelId, true);
        if (statusEl) statusEl.textContent = "MLX huge model switch cancelled.";
        return;
      }
      await load();
    } catch (error) {
      if (statusEl) statusEl.textContent = `MLX huge model switch failed: ${String(error?.message || error)}`;
    }
  }

  if (refreshEl) refreshEl.addEventListener("click", () => void load());
  void load();
})();
