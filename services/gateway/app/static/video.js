(() => {
  const $ = (id) => document.getElementById(id);

  const promptEl = $("prompt");
  const backendEl = $("backend");
  const backendHintEl = $("backendHint");
  const durationEl = $("duration");
  const durationHintEl = $("durationHint");
  const resolutionEl = $("resolution");
  const generateEl = $("generate");
  const statusEl = $("status");
  const metaEl = $("meta");
  const previewEl = $("preview");

  // Keep this table synchronized with media-generation/app/video_options.py.
  // A focused contract test compares the identifiers and exact dimensions.
  const RESOLUTION_PROFILES = {
    ltx_video: {
      default: "540p",
      maxDurationSeconds: 10,
      options: [
        { value: "480p", label: "Low (704×384)", width: 704, height: 384 },
        { value: "540p", label: "Standard (768×512)", width: 768, height: 512 },
        { value: "720p", label: "720p-class (1280×704, high memory)", width: 1280, height: 704 },
      ],
    },
    hunyuan_video: {
      default: "720p",
      maxDurationSeconds: 10,
      options: [
        { value: "480p", label: "480p" },
        { value: "720p", label: "720p" },
      ],
    },
  };

  function handle401(resp) {
    if (resp && resp.status === 401) {
      const back = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/ui/login?next=${back}`;
      return true;
    }
    return false;
  }

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  function setMeta(text) {
    metaEl.textContent = text || "";
  }

  function setPreview(html) {
    if (!previewEl) return;
    previewEl.innerHTML = html || "";
  }

  function resolutionProfile(backendClass) {
    return RESOLUTION_PROFILES[String(backendClass || "").trim()] || {
      default: "720p",
      maxDurationSeconds: 10,
      options: [
        { value: "480p", label: "480p" },
        { value: "720p", label: "720p" },
      ],
    };
  }

  function renderResolutionOptions() {
    if (!resolutionEl) return;
    const previous = String(resolutionEl.value || "").trim();
    const profile = resolutionProfile(backendEl?.value);
    resolutionEl.innerHTML = "";
    for (const item of profile.options) {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      resolutionEl.appendChild(option);
    }
    const supported = profile.options.some((item) => item.value === previous);
    resolutionEl.value = supported ? previous : profile.default;

    const maxDuration = Number(profile.maxDurationSeconds || 10);
    if (durationEl) {
      durationEl.max = String(maxDuration);
      const current = parseInt(String(durationEl.value || "6"), 10) || 6;
      if (current > maxDuration) durationEl.value = String(maxDuration);
    }
    if (durationHintEl) durationHintEl.textContent = `Range: 1-${maxDuration}`;
  }

  function buildRequestPreview() {
    const prompt = String(promptEl.value || "").trim();
    if (!prompt) throw new Error("prompt is required");

    const profile = resolutionProfile(backendEl?.value);
    const maximum = Number(profile.maxDurationSeconds || 10);
    const rawDuration = parseInt(String(durationEl.value || "6"), 10) || 6;
    if (rawDuration < 1 || rawDuration > maximum) {
      throw new Error(`duration must be between 1 and ${maximum} seconds for this backend`);
    }
    const resolution = String(resolutionEl.value || profile.default).trim();
    const body = { prompt, duration: rawDuration, resolution };
    const backendClass = String(backendEl?.value || "").trim();
    if (backendClass) body.backend_class = backendClass;
    return body;
  }

  async function loadBackends() {
    if (!backendEl) return;
    try {
      const resp = await fetch("/ui/api/video/backends", { method: "GET", credentials: "same-origin" });
      if (handle401(resp)) return;
      if (!resp.ok) {
        setStatus(`Failed to load video backends (HTTP ${resp.status}).`, true);
        return;
      }
      const payload = await resp.json();
      const list = Array.isArray(payload?.available_backends)
        ? payload.available_backends
        : Array.isArray(payload?.backends)
          ? payload.backends
          : [];
      const defaultBackend = String(payload?.default_backend_class || "").trim();
      backendEl.innerHTML = "";
      for (const item of list) {
        const backendClass = String(item?.backend_class || "").trim();
        if (!backendClass) continue;
        const opt = document.createElement("option");
        opt.value = backendClass;
        const health = item?.ready === false ? "not ready" : (item?.healthy === false ? "unhealthy" : "ready");
        opt.textContent = item?.description ? `${backendClass} - ${item.description} (${health})` : `${backendClass} (${health})`;
        backendEl.appendChild(opt);
      }
      if (defaultBackend) {
        backendEl.value = defaultBackend;
      } else if (backendEl.options.length > 0) {
        backendEl.selectedIndex = 0;
      }
      renderResolutionOptions();
      if (backendHintEl) {
        backendHintEl.textContent = list.length
          ? `${list.length} compatible video backend${list.length === 1 ? "" : "s"} available.`
          : "No compatible video backends are currently available.";
      }
      const details = document.querySelector("[data-backend-status]");
      if (details instanceof HTMLElement) {
        const values = list
          .map((item) => String(item?.backend_class || "").trim())
          .filter(Boolean)
          .join(",");
        if (values) details.setAttribute("data-backends", values);
      }
    } catch (e) {
      setStatus(`Failed to load video backends: ${String(e?.message || e)}`, true);
    }
  }

  async function handleGenerate() {
    setStatus("", false);
    setMeta("");
    setPreview("");

    let preview;
    try {
      preview = buildRequestPreview();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    generateEl.disabled = true;
    setStatus("Generating...", false);

    try {
      const resp = await fetch("/ui/api/video", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(preview),
      });
      const requestId = resp.headers.get("X-Request-Id") || "";
      const text = await resp.text();
      if (!resp.ok) {
        let detail = text;
        try {
          detail = JSON.stringify(JSON.parse(text), null, 2);
        } catch {
          detail = text;
        }
        setStatus(`Request failed${requestId ? ` (request id: ${requestId})` : ""}`, true);
        setPreview(`<pre>${detail}</pre>`);
        return;
      }
      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        setStatus("Video generation returned non-JSON response.", false);
        setPreview(`<pre>${text}</pre>`);
        return;
      }

      const url = payload?.url || payload?.video_url || payload?.data?.[0]?.url;
      const gatewayMeta = payload?._gateway || {};
      if (url && typeof url === "string") {
        setPreview(`<video controls style="max-width:100%" src="${url}"></video>`);
      } else {
        setPreview(`<pre>${JSON.stringify(payload, null, 2)}</pre>`);
      }
      const metaBits = [`Status: ${payload?.status || "ok"}`];
      if (gatewayMeta.backend_class) metaBits.push(`Backend: ${gatewayMeta.backend_class}`);
      if (requestId) metaBits.push(`Request ID: ${requestId}`);
      setMeta(metaBits.join(" · "));
      setStatus("Done.", false);
    } catch (e) {
      setStatus(`Video request lost its connection before a response was returned: ${String(e?.message || e)}`, true);
    } finally {
      generateEl.disabled = false;
    }
  }

  backendEl?.addEventListener("change", renderResolutionOptions);
  generateEl.addEventListener("click", handleGenerate);
  renderResolutionOptions();
  void loadBackends();
})();
