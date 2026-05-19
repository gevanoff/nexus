(() => {
  const hostsEl = document.getElementById("hosts");
  const controlPlaneEl = document.getElementById("control_plane");
  const coreServicesEl = document.getElementById("core_services");
  const backendsEl = document.getElementById("backends");
  const statusEl = document.getElementById("status");
  const refreshEl = document.getElementById("refresh");
  const CACHE_KEY = "nexus.resources.status.v1";
  const POLL_INTERVAL_MS = 30000;
  const STALE_AFTER_POLLS = 3;
  let currentUserIsAdmin = false;
  let currentPollIntervalSec = POLL_INTERVAL_MS / 1000;

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

  function fmtMb(value) {
    const mb = Number(value || 0);
    if (!Number.isFinite(mb) || mb <= 0) return "0 GB";
    return `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GB`;
  }

  function pct(used, total) {
    const u = Number(used || 0);
    const t = Number(total || 0);
    if (!t) return 0;
    return Math.max(0, Math.min(100, (u / t) * 100));
  }

  function bar(used, total) {
    const p = pct(used, total);
    const outer = document.createElement("div");
    outer.className = "bar";
    const fill = document.createElement("div");
    fill.className = `bar-fill ${p >= 90 ? "bad" : p >= 75 ? "warn" : ""}`;
    fill.style.width = `${p.toFixed(0)}%`;
    outer.appendChild(fill);
    return outer;
  }

  function appendMemoryRow(card, memory) {
    const total = Number(memory?.total_mb || 0);
    const available = Number(memory?.available_mb || 0);
    let used = Number(memory?.used_mb || 0);
    if (total > 0 && used <= 0 && available > 0) used = Math.max(0, total - available);
    if (!total) return false;
    const row = document.createElement("div");
    row.style.marginTop = "10px";
    row.innerHTML = `<div class="meta">System RAM · ${fmtMb(used)} / ${fmtMb(total)}</div>`;
    row.appendChild(bar(used, total));
    card.appendChild(row);
    return true;
  }

  function badge(text, cls) {
    const el = document.createElement("span");
    el.className = `badge ${cls || ""}`.trim();
    el.textContent = text;
    return el;
  }

  function formatTimestamp(tsSeconds) {
    const ts = Number(tsSeconds || 0);
    if (!Number.isFinite(ts) || ts <= 0) return "";
    try {
      return new Date(ts * 1000).toLocaleTimeString();
    } catch (error) {
      return "";
    }
  }

  function freshnessText(tsSeconds) {
    const ts = Number(tsSeconds || 0);
    if (!Number.isFinite(ts) || ts <= 0) return "";
    const ageMs = Date.now() - ts * 1000;
    if (!Number.isFinite(ageMs) || ageMs < 0) return `refreshed ${formatTimestamp(ts)}`;
    const ageSec = Math.floor(ageMs / 1000);
    if (ageSec < 60) return `refreshed ${ageSec}s ago`;
    const ageMin = Math.floor(ageSec / 60);
    if (ageMin < 60) return `refreshed ${ageMin}m ago`;
    const ageHr = Math.floor(ageMin / 60);
    if (ageHr < 36) return `refreshed ${ageHr}h ago`;
    return `refreshed ${formatTimestamp(ts)}`;
  }

  function updatePollInterval(payload) {
    const settings = payload?.settings && typeof payload.settings === "object" ? payload.settings : {};
    const value = Number(settings.poll_interval_sec || settings.health_poll_interval_sec || 0);
    if (Number.isFinite(value) && value > 0) currentPollIntervalSec = value;
  }

  function isStale(tsSeconds) {
    const ts = Number(tsSeconds || 0);
    if (!Number.isFinite(ts) || ts <= 0) return false;
    const ageSec = (Date.now() / 1000) - ts;
    return Number.isFinite(ageSec) && ageSec > currentPollIntervalSec * STALE_AFTER_POLLS;
  }

  function staleText(tsSeconds) {
    if (!isStale(tsSeconds)) return "";
    const ageSec = Math.max(0, Math.floor((Date.now() / 1000) - Number(tsSeconds || 0)));
    if (ageSec < 3600) return `stale ${Math.max(1, Math.floor(ageSec / 60))}m`;
    return `stale ${Math.floor(ageSec / 3600)}h`;
  }

  function effectiveBackendLastCheck(backend) {
    return Math.max(
      Number(backend?.last_checked_at || 0),
      Number(backend?.last_check || 0),
      Number(backend?.gateway_health?.last_check || 0),
    );
  }

  function loadCachedPayload() {
    try {
      const raw = window.localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const payload = JSON.parse(raw);
      if (!payload || typeof payload !== "object") return null;
      return payload;
    } catch (error) {
      return null;
    }
  }

  function saveCachedPayload(payload) {
    if (!payload || typeof payload !== "object") return;
    try {
      window.localStorage.setItem(CACHE_KEY, JSON.stringify(payload));
    } catch (error) {
      // Ignore storage quota/private-mode failures. The live view still works.
    }
  }

  function safeStatusClass(value) {
    return String(value || "inactive_unknown").replace(/[^a-z0-9_-]/gi, "_");
  }

  function statusBadgeClass(backend) {
    const color = String(backend?.status_color || "").toLowerCase();
    if (["green", "blue", "purple", "grey", "red", "yellow"].includes(color)) return color;
    if (backend?.ready === true) return "green";
    if (backend?.ready === false && backend?.active) return "red";
    return "grey";
  }

  function capabilityList(backend) {
    const values = Array.isArray(backend?.capabilities) ? backend.capabilities : [];
    return values.map((item) => String(item || "").trim()).filter(Boolean);
  }

  function canonicalBackendClass(value) {
    const normalized = String(value || "").trim();
    if (normalized === "mlx" || normalized === "mlx-coder" || normalized === "mlx_coder") return "local_mlx";
    return normalized;
  }

  function backendAnchorId(value) {
    const canonical = canonicalBackendClass(value);
    const safe = canonical.replace(/[^a-z0-9_-]/gi, "_");
    return safe ? `backend-${safe}` : "";
  }

  function requestedBackendAnchor() {
    try {
      const params = new URLSearchParams(window.location.search || "");
      const fromQuery = params.get("backend") || params.get("backend_class") || "";
      if (fromQuery) return backendAnchorId(fromQuery);
      const hash = String(window.location.hash || "").replace(/^#/, "");
      if (hash.startsWith("backend-")) return hash;
      if (hash) return backendAnchorId(hash);
    } catch (error) {}
    return "";
  }

  function focusRequestedBackend() {
    const id = requestedBackendAnchor();
    if (!id) return;
    const target = document.getElementById(id);
    if (!target) return;
    document.querySelectorAll(".backend-card.focused").forEach((el) => el.classList.remove("focused"));
    target.classList.add("focused");
    target.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  const backendGroups = [
    { id: "chat", label: "Chat & Reasoning", capabilities: ["chat", "transcription"] },
    { id: "embeddings", label: "Embeddings", capabilities: ["embeddings"] },
    { id: "images", label: "Images", capabilities: ["images"] },
    { id: "video", label: "Video", capabilities: ["video"] },
    { id: "speech", label: "Speech & Audio", capabilities: ["tts", "speech_to_speech"] },
    { id: "music", label: "Music", capabilities: ["music"] },
    { id: "ocr", label: "OCR", capabilities: ["ocr"] },
    { id: "integrations", label: "Integrations", capabilities: ["bridge"] },
    { id: "other", label: "Other", capabilities: [] },
  ];

  function backendGroupFor(backend) {
    const capabilities = new Set(capabilityList(backend));
    const provider = String(backend?.provider || "").trim().toLowerCase();
    const backendClass = String(backend?.backend_class || "").trim().toLowerCase();
    for (const group of backendGroups) {
      if (group.id === "other") continue;
      if (group.capabilities.some((capability) => capabilities.has(capability))) return group;
    }
    if (provider.includes("vllm") || provider.includes("mlx") || backendClass.includes("vllm") || backendClass.includes("mlx")) {
      return backendGroups[0];
    }
    return backendGroups[backendGroups.length - 1];
  }

  function mergeBackendStatusPayload(lifecyclePayload, registryPayload) {
    if (!registryPayload || typeof registryPayload !== "object") return lifecyclePayload || {};
    const base = lifecyclePayload && typeof lifecyclePayload === "object" ? { ...lifecyclePayload } : {};
    const lifecycleBackends = Array.isArray(lifecyclePayload?.backends) ? lifecyclePayload.backends : [];
    const registryBackends = Array.isArray(registryPayload?.backends) ? registryPayload.backends : [];
    const merged = new Map();
    lifecycleBackends.forEach((backend) => {
      const key = String(backend?.backend_class || "").trim();
      if (key) merged.set(key, { ...backend });
    });
    registryBackends.forEach((backend) => {
      const key = String(backend?.backend_class || "").trim();
      if (!key) return;
      const existing = merged.get(key) || {};
      merged.set(key, {
        ...existing,
        ...backend,
        capabilities: capabilityList(existing).length ? existing.capabilities : backend.capabilities,
        aliases: Array.isArray(backend.aliases) ? backend.aliases : existing.aliases,
        description: backend.description || existing.description,
        provider: backend.provider || existing.provider,
        base_url: backend.base_url || existing.base_url,
        health: backend.health || existing.health,
        hostname: existing.hostname || backend.hostname,
      });
    });
    if (merged.size > 0) base.backends = [...merged.values()];
    if (Array.isArray(lifecyclePayload?.core_services)) base.core_services = lifecyclePayload.core_services;
    if (Array.isArray(registryPayload?.control_plane)) base.control_plane = registryPayload.control_plane;
    if (registryPayload.alias_config) base.alias_config = registryPayload.alias_config;
    base.settings = {
      ...(registryPayload.settings && typeof registryPayload.settings === "object" ? registryPayload.settings : {}),
      ...(lifecyclePayload?.settings && typeof lifecyclePayload.settings === "object" ? lifecyclePayload.settings : {}),
    };
    base.generated_at = Number(lifecyclePayload?.generated_at || registryPayload?.generated_at || Date.now() / 1000);
    return base;
  }

  function renderHosts(hosts) {
    if (!hostsEl) return;
    hostsEl.innerHTML = "";
    if (!Array.isArray(hosts) || !hosts.length) {
      hostsEl.innerHTML = '<div class="hint">No hosts reported.</div>';
      return;
    }
    hosts.forEach((host) => {
      const card = document.createElement("div");
      card.className = "card";
      if (isStale(host.updated_at)) card.classList.add("stale");
      const name = document.createElement("div");
      name.className = "host-name";
      name.textContent = host.name || "unknown";
      card.appendChild(name);

      const meta = document.createElement("div");
      meta.className = "meta";
      const hostMeta = [host.resource_kind || host.platform || "host"];
      const hostFreshness = freshnessText(host.updated_at);
      if (hostFreshness) hostMeta.push(hostFreshness);
      const hostStale = staleText(host.updated_at);
      if (hostStale) hostMeta.push(hostStale);
      if (host.error) hostMeta.push(host.error);
      meta.textContent = hostMeta.join(" · ");
      card.appendChild(meta);

      const gpus = Array.isArray(host.gpus) ? host.gpus : [];
      if (gpus.length) {
        gpus.forEach((gpu) => {
          const row = document.createElement("div");
          row.style.marginTop = "10px";
          const used = Number(gpu.memory_used_mb || 0);
          const total = Number(gpu.memory_total_mb || 0);
          row.innerHTML = `<div class="meta">${gpu.name || `GPU ${gpu.index}`} · ${fmtMb(used)} / ${fmtMb(total)} · ${gpu.utilization_gpu_pct || 0}% util</div>`;
          row.appendChild(bar(used, total));
          card.appendChild(row);
        });
      }

      const hasMemory = appendMemoryRow(card, host.memory);
      if (!gpus.length && !hasMemory) {
        const empty = document.createElement("div");
        empty.className = "meta";
        empty.style.marginTop = "10px";
        empty.textContent = "No resource metrics yet.";
        card.appendChild(empty);
      }
      hostsEl.appendChild(card);
    });
  }

  function renderCoreServices(services) {
    if (!coreServicesEl) return;
    coreServicesEl.innerHTML = "";
    if (!Array.isArray(services) || !services.length) {
      coreServicesEl.innerHTML = '<div class="hint">No core services reported.</div>';
      return;
    }
    const sorted = [...services].sort((a, b) =>
      (Number(a.status_rank ?? 9) - Number(b.status_rank ?? 9))
      || String(a.host || "").localeCompare(String(b.host || ""))
      || String(a.display_name || a.service_id || "").localeCompare(String(b.display_name || b.service_id || ""))
    );
    sorted.forEach((service) => {
      const card = document.createElement("div");
      card.className = `core-service-card ${service.active ? "active" : "problem"}`;
      if (isStale(service.updated_at)) card.classList.add("stale");

      const left = document.createElement("div");
      const name = document.createElement("div");
      name.className = "backend-name";
      name.textContent = service.display_name || service.service_id || "Core service";
      left.appendChild(name);

      const meta = document.createElement("div");
      meta.className = "meta";
      const components = Array.isArray(service.components) ? service.components.join(", ") : "";
      meta.textContent = [service.service_id, service.host || "unknown host", components].filter(Boolean).join(" · ");
      left.appendChild(meta);

      const badges = document.createElement("div");
      badges.className = "badges";
      badges.appendChild(badge(service.status_label || service.status || "unknown", statusBadgeClass(service)));
      badges.appendChild(badge(service.tier || "core", service.tier === "edge" ? "blue" : "crucial"));
      const fresh = freshnessText(service.updated_at);
      if (fresh) badges.appendChild(badge(fresh.replace("refreshed", "checked"), "blue"));
      const stale = staleText(service.updated_at);
      if (stale) badges.appendChild(badge(stale, "yellow"));
      left.appendChild(badges);

      const detailParts = [];
      const containers = Array.isArray(service.containers) ? service.containers : [];
      if (containers.length) {
        detailParts.push(containers.map((item) => `${item.name}: ${item.status}`).join(" · "));
      }
      if (Array.isArray(service.missing_components) && service.missing_components.length) {
        detailParts.push(`missing ${service.missing_components.join(", ")}`);
      }
      if (service.host_error) detailParts.push(service.host_error);
      if (service.notes) detailParts.push(service.notes);
      if (detailParts.length) {
        const detail = document.createElement("div");
        detail.className = service.active ? "meta" : "meta error";
        detail.style.marginTop = "6px";
        detail.textContent = detailParts.join(" · ");
        left.appendChild(detail);
      }

      card.appendChild(left);
      coreServicesEl.appendChild(card);
    });
  }

  function renderControlPlane(services) {
    if (!controlPlaneEl) return;
    controlPlaneEl.innerHTML = "";
    if (!Array.isArray(services) || !services.length) {
      controlPlaneEl.innerHTML = '<div class="hint">No control-plane services reported.</div>';
      return;
    }
    const sorted = [...services].sort((a, b) =>
      (Number(a.status_rank ?? 9) - Number(b.status_rank ?? 9))
      || String(a.display_name || a.service_id || "").localeCompare(String(b.display_name || b.service_id || ""))
    );
    sorted.forEach((service) => {
      const card = document.createElement("div");
      card.className = `core-service-card ${service.active ? "active" : "problem"}`;
      if (isStale(service.updated_at)) card.classList.add("stale");

      const left = document.createElement("div");
      const name = document.createElement("div");
      name.className = "backend-name";
      name.textContent = service.display_name || service.service_id || "Control-plane service";
      left.appendChild(name);

      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = [service.service_id, service.host || "gateway", service.endpoint || ""].filter(Boolean).join(" · ");
      left.appendChild(meta);

      const badges = document.createElement("div");
      badges.className = "badges";
      badges.appendChild(badge(service.status_label || service.status || "unknown", statusBadgeClass(service)));
      badges.appendChild(badge("control plane", "crucial"));
      const fresh = freshnessText(service.updated_at);
      if (fresh) badges.appendChild(badge(fresh.replace("refreshed", "checked"), "blue"));
      const stale = staleText(service.updated_at);
      if (stale) badges.appendChild(badge(stale, "yellow"));
      left.appendChild(badges);

      if (service.notes) {
        const detail = document.createElement("div");
        detail.className = service.active ? "meta" : "meta error";
        detail.style.marginTop = "6px";
        detail.textContent = service.notes;
        left.appendChild(detail);
      }

      card.appendChild(left);
      controlPlaneEl.appendChild(card);
    });
  }

  function actionButton(label, backendClass, action, danger) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    if (danger) btn.dataset.danger = "true";
    btn.addEventListener("click", () => { handleEditTask(task.id); });

function handleEditTask(taskId) {
  // Open edit modal or call backend API
  console.log(`Edit task ${taskId}`);
  // Example: Open modal
  // showModal({ taskId });
  // Example: API call
  // fetch(`/api/tasks/edit/${taskId}`);
}

function handleEditTask(taskId) {
  // Implement edit logic here (e.g., open modal or API call)
  console.log(`Edit task ${taskId}`);
  // Example: Open edit modal
  // showEditModal(taskId);
  // Or make API call:
  // fetch(`/api/tasks/edit/${taskId}`);
}

  function handleEditTask(taskId) {
    console.log("Edit task:", taskId);
    // Implement modal or API call here
  }
      if (action === 'edit') {
        void handleEditTask(task.id);
      } else {
        void runAction(backendClass, 'edit', false);
      }
    });
    return btn;
  }

  function renderBackends(backends) {
    if (!backendsEl) return;
    backendsEl.innerHTML = "";
    if (!Array.isArray(backends) || !backends.length) {
      backendsEl.innerHTML = '<div class="hint">No backend lifecycle policy loaded.</div>';
      return;
    }
    const tierRank = { crucial: 0, high: 1, optional: 2 };
    const sortedBackends = [...backends].sort((a, b) => {
      const ta = tierRank[a.tier] ?? 9;
      const tb = tierRank[b.tier] ?? 9;
      if (ta !== tb) return ta - tb;
      return String(a.host || "").localeCompare(String(b.host || "")) || String(a.display_name || a.backend_class).localeCompare(String(b.display_name || b.backend_class));
    });

    const groups = new Map(backendGroups.map((group) => [group.id, { ...group, backends: [] }]));
    sortedBackends.forEach((backend) => {
      const group = backendGroupFor(backend);
      groups.get(group.id).backends.push(backend);
    });

    const renderBackendCard = (backend) => {
      const card = document.createElement("div");
      const lifecycleStatus = safeStatusClass(backend.status);
      card.className = `backend-card status-${lifecycleStatus} ${backend.active ? "active" : ""} ${backend.active && backend.ready === false ? "blocked" : ""}`;
      const anchorId = backendAnchorId(backend.backend_class);
      if (anchorId) card.id = anchorId;
      if (backend.backend_class) card.dataset.backendClass = backend.backend_class;
      const lastCheck = effectiveBackendLastCheck(backend);
      if (isStale(lastCheck)) card.classList.add("stale");

      const left = document.createElement("div");
      const name = document.createElement("div");
      name.className = "backend-name";
      name.textContent = backend.display_name || backend.backend_class;
      left.appendChild(name);
      const meta = document.createElement("div");
      meta.className = "meta";
      const metaParts = [
        backend.backend_class,
        backend.host || backend.hostname || "unknown host",
      ];
      if (backend.provider && backend.provider !== backend.backend_class) metaParts.push(backend.provider);
      metaParts.push(`est ${fmtMb(backend.estimated_vram_mb)}`);
      meta.textContent = metaParts.filter(Boolean).join(" · ");
      left.appendChild(meta);
      const badges = document.createElement("div");
      badges.className = "badges";
      badges.appendChild(badge(backend.status_label || "No healthy check yet", statusBadgeClass(backend)));
      badges.appendChild(badge(backend.tier || "optional", backend.tier || "optional"));
      badges.appendChild(badge(backend.active ? "active" : "stopped", backend.active ? "green" : "grey"));
      capabilityList(backend).forEach((capability) => {
        badges.appendChild(badge(capability, "blue"));
      });
      if (backend.active) {
        badges.appendChild(badge(backend.ready === true ? "ready" : backend.ready === false ? "not ready" : "unknown", backend.ready === true ? "green" : backend.ready === false ? "red" : "grey"));
      } else if (backend.last_ready_at) {
        badges.appendChild(badge(`last ready ${formatTimestamp(backend.last_ready_at) || "known"}`, "blue"));
      } else if (backend.last_unhealthy_at) {
        badges.appendChild(badge(`last unhealthy ${formatTimestamp(backend.last_unhealthy_at) || "known"}`, "purple"));
      } else {
        badges.appendChild(badge("never ready", "grey"));
      }
      const backendFreshness = freshnessText(lastCheck);
      if (backendFreshness) badges.appendChild(badge(backendFreshness.replace("refreshed", "checked"), "blue"));
      const backendStale = staleText(lastCheck);
      if (backendStale) badges.appendChild(badge(backendStale, "yellow"));
      if (backend.inflight) badges.appendChild(badge(`${backend.inflight} running`, "ok"));
      left.appendChild(badges);

      const detailParts = [];
      if (backend.description) detailParts.push(String(backend.description));
      if (backend.base_url) detailParts.push(`base ${backend.base_url}`);
      if (backend.health && typeof backend.health === "object") {
        const liveness = String(backend.health.liveness || "").trim();
        const readiness = String(backend.health.readiness || "").trim();
        if (liveness || readiness) detailParts.push(`health ${liveness || "--"} / ${readiness || "--"}`);
      }
      const aliases = Array.isArray(backend.aliases) ? backend.aliases : [];
      if (aliases.length) {
        detailParts.push(`aliases ${aliases.map((alias) => `${alias.name} -> ${alias.target}`).join(", ")}`);
      }
      if (Number(backend.idle_observed_vram_mb || 0) > 0) detailParts.push(`idle ${fmtMb(backend.idle_observed_vram_mb)}`);
      if (Number(backend.peak_observed_vram_mb || 0) > 0) detailParts.push(`peak ${fmtMb(backend.peak_observed_vram_mb)}`);
      if (lastCheck) detailParts.push(`last check ${formatTimestamp(lastCheck)}`);
      if (backend.last_action_error) detailParts.push(backend.last_action_error);
      if (backend.health_error) detailParts.push(backend.health_error);
      if (!backend.health_error && backend.status === "inactive_unhealthy" && backend.last_health_error) detailParts.push(backend.last_health_error);
      if (backend.last_stopped_at && !backend.active) detailParts.push(`stopped ${formatTimestamp(backend.last_stopped_at) || "recently"}`);
      if (backend.notes) detailParts.push(backend.notes);
      if (detailParts.length) {
        const detail = document.createElement("div");
        detail.className = backend.health_error || backend.last_action_error || (backend.status === "inactive_unhealthy" && backend.last_health_error) ? "meta error" : "meta";
        detail.style.marginTop = "6px";
        detail.textContent = detailParts.join(" · ");
        left.appendChild(detail);
      }

      card.appendChild(left);
      if (currentUserIsAdmin) {
        const controls = document.createElement("div");
        controls.className = "row";
        controls.style.justifyContent = "flex-end";
        if (backend.active) {
          controls.appendChild(actionButton("Deactivate", backend.backend_class, "deactivate", true));
        } else {
          controls.appendChild(actionButton("Activate", backend.backend_class, "activate", false));
        }
        card.appendChild(controls);
      }
      return card;
    };

    backendGroups.forEach((groupDef) => {
      const group = groups.get(groupDef.id);
      if (!group || !group.backends.length) return;

      const section = document.createElement("section");
      section.className = "backend-group";

      const header = document.createElement("div");
      header.className = "backend-group-header";
      const title = document.createElement("div");
      title.className = "backend-group-title";
      title.textContent = group.label;
      const count = document.createElement("div");
      count.className = "backend-group-count";
      count.textContent = `${group.backends.length}`;
      header.appendChild(title);
      header.appendChild(count);
      section.appendChild(header);

      const list = document.createElement("div");
      list.className = "backend-group-list";
      group.backends.forEach((backend) => list.appendChild(renderBackendCard(backend)));
      section.appendChild(list);
      backendsEl.appendChild(section);
    });
    focusRequestedBackend();
  }

  async function loadCurrentUser() {
    try {
      const resp = await fetch("/ui/api/auth/me", { method: "GET", credentials: "same-origin" });
      if (handle401(resp)) return;
      if (!resp.ok) return;
      const payload = await resp.json();
      currentUserIsAdmin = !!(payload?.authenticated && payload?.user?.admin);
    } catch (error) {
      currentUserIsAdmin = false;
    }
  }

  function renderPayload(payload, options) {
    const opts = options || {};
    updatePollInterval(payload);
    renderHosts(payload.hosts || []);
    renderControlPlane(payload.control_plane || []);
    renderCoreServices(payload.core_services || []);
    renderBackends(payload.backends || []);
    const statusParts = [`Mode: ${payload.mode || "unknown"}`];
    if (opts.cached) statusParts.push("showing cached data");
    if (opts.registryError) statusParts.push(`registry ${opts.registryError}`);
    statusParts.push(`Updated ${new Date(Number(payload.generated_at || 0) * 1000).toLocaleTimeString()}`);
    setStatus(statusParts.join(" · "), !!opts.registryError);
  }

  async function loadStatus(refresh) {
    const forceRefresh = refresh === true;
    if (refreshEl) refreshEl.disabled = true;
    setStatus(forceRefresh ? "Refreshing lifecycle state..." : "Loading lifecycle state...", false);
    try {
      const fetchJson = async (url) => {
        const resp = await fetch(url, { credentials: "same-origin" });
        if (handle401(resp)) return { redirected: true };
        if (!resp.ok) {
          const text = await resp.text().catch(() => "");
          return { error: text || `HTTP ${resp.status}` };
        }
        return { data: await resp.json() };
      };
      const lifecyclePath = `/ui/api/lifecycle/status${forceRefresh ? "?refresh=true" : ""}`;
      const lifecyclePromise = fetchJson(lifecyclePath);
      const registryPromise = fetchJson("/ui/api/backend_status");

      const [lifecycleResult, registryResult] = await Promise.all([lifecyclePromise, registryPromise]);
      if (lifecycleResult.redirected || registryResult.redirected) return;
      let payload = lifecycleResult.data || null;
      if (registryResult.redirected) return;
      if (!payload && !registryResult.data) {
        throw new Error(lifecycleResult.error || registryResult.error || "No status payload returned");
      }
      payload = mergeBackendStatusPayload(payload, registryResult.data);
      renderPayload(payload, { registryError: registryResult.error });
      saveCachedPayload(payload);
    } catch (error) {
      const cached = loadCachedPayload();
      if (cached) renderPayload(cached, { cached: true });
      setStatus(`Lifecycle status failed: ${String(error?.message || error)}`, true);
    } finally {
      if (refreshEl) refreshEl.disabled = false;
    }
  }

  async function runAction(backendClass, action, confirmed) {
    setStatus(`${action} ${backendClass}...`, false);
    try {
      const resp = await fetch("/ui/api/lifecycle/action", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ backend_class: backendClass, action, confirmed, allow_disruptive: confirmed }),
      });
      if (handle401(resp)) return;
      const payload = await resp.json().catch(() => ({}));
      if (resp.status === 403) {
        setStatus("Admin privileges are required for manual lifecycle actions.", true);
        return;
      }
      if (!resp.ok) throw new Error(payload?.detail ? JSON.stringify(payload.detail) : `HTTP ${resp.status}`);
      if (payload?.decision === "requires_confirmation" && !confirmed) {
        const ok = window.confirm(`${payload.message || "This action needs confirmation."}\n\nProceed?`);
        if (ok) return runAction(backendClass, action, true);
      }
      setStatus(`${backendClass}: ${String(payload?.decision || action).replace(/_/g, " ")}`, payload?.ok === false);
      await loadStatus(true);
    } catch (error) {
      setStatus(`Lifecycle action failed: ${String(error?.message || error)}`, true);
    }
  }

  if (refreshEl) refreshEl.addEventListener("click", () => void loadStatus(true));
  window.addEventListener("hashchange", () => focusRequestedBackend());
  void (async () => {
    await loadCurrentUser();
    const cached = loadCachedPayload();
    if (cached) renderPayload(cached, { cached: true });
    await loadStatus(false);
  })();
  window.setInterval(() => void loadStatus(false), POLL_INTERVAL_MS);
})();
