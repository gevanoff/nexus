(() => {
  const statusEl = document.getElementById("admin_models_status");
  const listEl = document.getElementById("admin_models_list");
  const refreshEl = document.getElementById("admin_models_refresh");

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

  function badge(text, tone) {
    const el = document.createElement("span");
    el.className = `model-admin-badge${tone ? ` ${tone}` : ""}`;
    el.textContent = text;
    return el;
  }

  function row(left, middle, badges) {
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
        if (model.unavailable_reason) badges.push({ text: model.unavailable_reason, tone: "yellow" });
        if (model.cache_only) badges.push({ text: "cache only", tone: "yellow" });
        if (model.advertised) badges.push({ text: "advertised", tone: "" });
        const aliasText = Array.isArray(model.aliases) && model.aliases.length ? `aliases: ${model.aliases.join(", ")}` : model.provider || "";
        modelGroup.appendChild(row(`${model.backend || ""}:${model.model || ""}`, aliasText, badges));
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

  if (refreshEl) refreshEl.addEventListener("click", () => void load());
  void load();
})();
