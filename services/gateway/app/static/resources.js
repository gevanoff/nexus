(() => {
  const hostsEl = document.getElementById("hosts");
  const backendsEl = document.getElementById("backends");
  const statusEl = document.getElementById("status");
  const refreshEl = document.getElementById("refresh");

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
      const name = document.createElement("div");
      name.className = "host-name";
      name.textContent = host.name || "unknown";
      card.appendChild(name);

      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${host.resource_kind || host.platform || "host"}${host.error ? ` · ${host.error}` : ""}`;
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
      } else if (host.memory && host.memory.total_mb) {
        const used = Number(host.memory.used_mb || 0);
        const total = Number(host.memory.total_mb || 0);
        const row = document.createElement("div");
        row.style.marginTop = "10px";
        row.innerHTML = `<div class="meta">Memory · ${fmtMb(used)} / ${fmtMb(total)}</div>`;
        row.appendChild(bar(used, total));
        card.appendChild(row);
      } else {
        const empty = document.createElement("div");
        empty.className = "meta";
        empty.style.marginTop = "10px";
        empty.textContent = "No resource metrics yet.";
        card.appendChild(empty);
      }
      hostsEl.appendChild(card);
    });
  }

  function actionButton(label, backendClass, action, danger) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    if (danger) btn.dataset.danger = "true";
    btn.addEventListener("click", () => {
      void runAction(backendClass, action, false);
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
    [...backends].sort((a, b) => {
      const ta = tierRank[a.tier] ?? 9;
      const tb = tierRank[b.tier] ?? 9;
      if (ta !== tb) return ta - tb;
      return String(a.host || "").localeCompare(String(b.host || "")) || String(a.display_name || a.backend_class).localeCompare(String(b.display_name || b.backend_class));
    }).forEach((backend) => {
      const card = document.createElement("div");
      const lifecycleStatus = safeStatusClass(backend.status);
      card.className = `backend-card status-${lifecycleStatus} ${backend.active ? "active" : ""} ${backend.active && backend.ready === false ? "blocked" : ""}`;

      const left = document.createElement("div");
      const name = document.createElement("div");
      name.className = "backend-name";
      name.textContent = backend.display_name || backend.backend_class;
      left.appendChild(name);
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${backend.backend_class} · ${backend.host || "unknown host"} · est ${fmtMb(backend.estimated_vram_mb)}`;
      left.appendChild(meta);
      const badges = document.createElement("div");
      badges.className = "badges";
      badges.appendChild(badge(backend.status_label || "No healthy check yet", statusBadgeClass(backend)));
      badges.appendChild(badge(backend.tier || "optional", backend.tier || "optional"));
      badges.appendChild(badge(backend.active ? "active" : "stopped", backend.active ? "green" : "grey"));
      if (backend.active) {
        badges.appendChild(badge(backend.ready === true ? "ready" : backend.ready === false ? "not ready" : "unknown", backend.ready === true ? "green" : backend.ready === false ? "red" : "grey"));
      } else if (backend.last_ready_at) {
        badges.appendChild(badge(`last ready ${formatTimestamp(backend.last_ready_at) || "known"}`, "blue"));
      } else if (backend.last_unhealthy_at) {
        badges.appendChild(badge(`last unhealthy ${formatTimestamp(backend.last_unhealthy_at) || "known"}`, "purple"));
      } else {
        badges.appendChild(badge("never ready", "grey"));
      }
      if (backend.inflight) badges.appendChild(badge(`${backend.inflight} running`, "ok"));
      left.appendChild(badges);

      const detailParts = [];
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

      const controls = document.createElement("div");
      controls.className = "row";
      controls.style.justifyContent = "flex-end";
      if (backend.active) {
        controls.appendChild(actionButton("Deactivate", backend.backend_class, "deactivate", true));
      } else {
        controls.appendChild(actionButton("Activate", backend.backend_class, "activate", false));
      }

      card.appendChild(left);
      card.appendChild(controls);
      backendsEl.appendChild(card);
    });
  }

  async function loadStatus() {
    if (refreshEl) refreshEl.disabled = true;
    setStatus("Refreshing lifecycle state...", false);
    try {
      const resp = await fetch("/ui/api/lifecycle/status?refresh=true", { credentials: "same-origin" });
      if (handle401(resp)) return;
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload = await resp.json();
      renderHosts(payload.hosts || []);
      renderBackends(payload.backends || []);
      setStatus(`Mode: ${payload.mode || "unknown"} · Updated ${new Date(Number(payload.generated_at || 0) * 1000).toLocaleTimeString()}`, false);
    } catch (error) {
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
      await loadStatus();
    } catch (error) {
      setStatus(`Lifecycle action failed: ${String(error?.message || error)}`, true);
    }
  }

  if (refreshEl) refreshEl.addEventListener("click", () => void loadStatus());
  void loadStatus();
  window.setInterval(() => void loadStatus(), 30000);
})();
