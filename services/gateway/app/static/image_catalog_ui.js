(() => {
  const $ = (id) => document.getElementById(id);

  const promptEl = $("prompt");
  const backendEl = $("backend");
  const backendHintEl = $("backendHint");
  const sizeEl = $("size");
  const nEl = $("n");
  const modelSelectEl = $("modelSelect");
  const modelHintEl = $("modelHint");
  const modelCustomWrapEl = $("modelCustomWrap");
  const modelCustomEl = $("modelCustom");
  const seedEl = $("seed");
  const stepsEl = $("steps");
  const guidanceEl = $("guidance");
  const schedulerEl = $("scheduler");
  const styleEl = $("style");
  const qualityEl = $("quality");
  const negativeEl = $("negative");
  const extraEl = $("extra");
  const generateEl = $("generate");
  const refreshCatalogEl = $("refreshCatalog");

  const backendSummaryEl = $("backendSummary");
  const modelManagementEl = $("modelManagement");
  const statusEl = $("status");
  const metaEl = $("meta");
  const galleryEl = $("gallery");
  const debugEl = $("debug");

  const fieldDefs = {
    seed: { wrap: $("seedWrap"), input: seedEl, label: $("seedLabel"), help: $("seedHelp") },
    steps: { wrap: $("stepsWrap"), input: stepsEl, label: $("stepsLabel"), help: $("stepsHelp") },
    guidance_scale: { wrap: $("guidanceWrap"), input: guidanceEl, label: $("guidanceLabel"), help: $("guidanceHelp") },
    scheduler: { wrap: $("schedulerWrap"), input: schedulerEl, label: $("schedulerLabel"), help: $("schedulerHelp") },
    style: { wrap: $("styleWrap"), input: styleEl, label: $("styleLabel"), help: $("styleHelp") },
    quality: { wrap: $("qualityWrap"), input: qualityEl, label: $("qualityLabel"), help: $("qualityHelp") },
    negative_prompt: { wrap: $("negativeWrap"), input: negativeEl, label: $("negativeLabel"), help: $("negativeHelp") },
    extra_json: { wrap: $("extraWrap"), input: extraEl, label: $("extraLabel"), help: $("extraHelp") },
  };

  const numericFields = new Set(["seed", "steps", "guidance_scale"]);
  const prefill = readQueryPrefill();
  const state = {
    catalog: null,
    backendMap: new Map(),
  };

  function refreshModelMarquee() {
    window.NexusSelectMarquee?.refresh(modelSelectEl);
  }

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

  function safeExternalUrl(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    try {
      const parsed = new URL(raw, window.location.origin);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.href;
    } catch (error) {
      return "";
    }
    return "";
  }

  function clearOutput() {
    metaEl.textContent = "";
    galleryEl.innerHTML = "";
  }

  function parseNum(value) {
    const s = String(value || "").trim();
    if (!s) return null;
    const n = Number(s);
    return Number.isFinite(n) ? n : null;
  }

  function parseIntNum(value) {
    const n = parseNum(value);
    if (n === null) return null;
    return Math.trunc(n);
  }

  function readQueryPrefill() {
    const qs = new URLSearchParams(location.search || "");
    return {
      prompt: qs.get("prompt") || "",
      size: qs.get("size") || "",
      n: qs.get("n") || "",
      model: qs.get("model") || "",
      backendClass: qs.get("backend_class") || qs.get("backend") || "",
      seed: qs.get("seed") || "",
      steps: qs.get("steps") || "",
      guidance: qs.get("guidance_scale") || qs.get("guidance") || "",
      negative: qs.get("negative_prompt") || qs.get("negative") || "",
      scheduler: qs.get("scheduler") || "",
      style: qs.get("style") || "",
      quality: qs.get("quality") || "",
      extra: qs.get("extra") || "",
    };
  }

  function applySimplePrefill() {
    if (prefill.prompt) promptEl.value = prefill.prompt;
    if (prefill.size) sizeEl.value = prefill.size;
    if (prefill.n) nEl.value = prefill.n;
    if (prefill.seed) seedEl.value = prefill.seed;
    if (prefill.steps) stepsEl.value = prefill.steps;
    if (prefill.guidance) guidanceEl.value = prefill.guidance;
    if (prefill.negative) negativeEl.value = prefill.negative;
    if (prefill.scheduler) schedulerEl.value = prefill.scheduler;
    if (prefill.style) styleEl.value = prefill.style;
    if (prefill.quality) qualityEl.value = prefill.quality;
    if (prefill.extra) extraEl.value = prefill.extra;
  }

  function setFieldVisible(key, config) {
    const field = fieldDefs[key];
    if (!field) return;
    const enabled = !!(config && config.enabled);
    field.wrap.classList.toggle("hidden", !enabled);
    if (field.label && config && config.label) {
      field.label.textContent = `${config.label} (optional)`;
    }
    if (field.help) {
      field.help.textContent = config && config.help ? config.help : "";
    }
    if (!enabled) {
      field.input.value = "";
      return;
    }
    if (config && typeof config.placeholder === "string") {
      field.input.placeholder = config.placeholder;
    }
    if (numericFields.has(key)) {
      if (config && config.min !== undefined) field.input.min = String(config.min);
      if (config && config.max !== undefined) field.input.max = String(config.max);
      if (config && config.step !== undefined) field.input.step = String(config.step);
    }
    const current = String(field.input.value || "").trim();
    if (!current && config && config.default !== undefined && config.default !== null) {
      field.input.value = String(config.default);
    }
  }

  function renderBackendSummary(entry) {
    if (!entry) {
      backendSummaryEl.textContent = "No image backend selected.";
      modelManagementEl.replaceChildren();
      return;
    }

    const bits = [];
    bits.push(`${entry.display_name || entry.backend_class} (${entry.backend_class})`);
    if (entry.description) bits.push(entry.description);
    bits.push(`Health: ${entry.healthy === true ? "healthy" : entry.healthy === false ? "unhealthy" : "unknown"}`);
    bits.push(`Readiness: ${entry.ready === true ? "ready" : entry.ready === false ? "not ready" : "unknown"}`);
    if (entry.base_url) bits.push(`Target: ${entry.base_url}`);
    if (entry.error) bits.push(`Error: ${entry.error}`);
    backendSummaryEl.textContent = bits.join(" • ");

    const management = entry.model_management || {};
    const managementBits = [];
    managementBits.push(
      management.supported
        ? "Model management is available for this backend."
        : (management.message || "Model management is not available through the gateway for this backend.")
    );
    if (management.source_url) managementBits.push(`Model source: ${management.source_url}`);
    if (management.upstream_error) managementBits.push(`Upstream note: ${management.upstream_error}`);
    if (entry.models_error) managementBits.push(`Model list error: ${entry.models_error}`);
    modelManagementEl.replaceChildren();
    const summary = document.createElement("span");
    summary.textContent = managementBits.join(" • ");
    modelManagementEl.appendChild(summary);
    const uiUrl = safeExternalUrl(management.ui_url);
    if (uiUrl) {
      const sep = document.createElement("span");
      sep.textContent = summary.textContent ? " • " : "";
      const link = document.createElement("a");
      link.href = uiUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = String(management.ui_label || "Open management UI");
      modelManagementEl.appendChild(sep);
      modelManagementEl.appendChild(link);
    }
  }

  function renderModelSelect(entry) {
    modelSelectEl.innerHTML = "";

    const defaultOpt = document.createElement("option");
    defaultOpt.value = "";
    defaultOpt.textContent = "Use backend default";
    modelSelectEl.appendChild(defaultOpt);

    const models = Array.isArray(entry && entry.models) ? entry.models : [];
    for (const item of models) {
      const value = String(item && item.id ? item.id : "").trim();
      if (!value) continue;
      const option = document.createElement("option");
      option.value = value;
      const label = String(item && item.name ? item.name : value).trim();
      option.textContent = label && label !== value ? `${label} (${value})` : value;
      modelSelectEl.appendChild(option);
    }

    const customOpt = document.createElement("option");
    customOpt.value = "__custom__";
    customOpt.textContent = "Custom model id...";
    modelSelectEl.appendChild(customOpt);

    const hintBits = [];
    hintBits.push(models.length ? `${models.length} installed model${models.length === 1 ? "" : "s"} discovered.` : "No installed models were discovered for this backend.");
    if (entry && entry.models_error) hintBits.push(`Lookup error: ${entry.models_error}`);
    modelHintEl.textContent = hintBits.join(" ");

    if (prefill.model) {
      const match = models.some((item) => String(item && item.id ? item.id : "").trim() === prefill.model);
      if (match) {
        modelSelectEl.value = prefill.model;
        modelCustomWrapEl.classList.add("hidden");
        modelCustomEl.value = "";
        refreshModelMarquee();
        return;
      }
      modelSelectEl.value = "__custom__";
      modelCustomWrapEl.classList.remove("hidden");
      modelCustomEl.value = prefill.model;
      refreshModelMarquee();
      return;
    }

    modelSelectEl.value = "";
    modelCustomWrapEl.classList.add("hidden");
    modelCustomEl.value = "";
    refreshModelMarquee();
  }

  function applyBackendSelection(backendClass) {
    const entry = state.backendMap.get(String(backendClass || ""));
    renderBackendSummary(entry || null);
    if (!entry) {
      backendHintEl.textContent = "";
      return;
    }

    backendHintEl.textContent = entry.description || entry.display_name || entry.backend_class;
    renderModelSelect(entry);

    const optionProfile = entry.options || {};
    const fields = optionProfile.fields || {};
    for (const key of Object.keys(fieldDefs)) {
      const fallback = key === "extra_json" ? { enabled: true } : { enabled: false };
      setFieldVisible(key, fields[key] || fallback);
    }

    const sizes = Array.isArray(optionProfile.size_options) ? optionProfile.size_options : [];
    if (sizes.length) {
      const current = String(sizeEl.value || "").trim();
      const preferred = current || prefill.size || String(optionProfile.defaults && optionProfile.defaults.size ? optionProfile.defaults.size : "").trim();
      sizeEl.innerHTML = "";
      for (const size of sizes) {
        const option = document.createElement("option");
        option.value = size;
        option.textContent = size;
        if (preferred && preferred === size) option.selected = true;
        sizeEl.appendChild(option);
      }
      if (!sizeEl.value && sizeEl.options.length) sizeEl.selectedIndex = 0;
    }
  }

  function renderBackendSelect(catalog) {
    backendEl.innerHTML = "";
    state.backendMap = new Map();

    const backends = Array.isArray(catalog && catalog.backends) ? catalog.backends : [];
    for (const entry of backends) {
      if (!entry || !entry.backend_class) continue;
      state.backendMap.set(entry.backend_class, entry);
      const option = document.createElement("option");
      option.value = entry.backend_class;
      const readiness = entry.ready === true ? "ready" : entry.ready === false ? "not ready" : "unknown";
      option.textContent = `${entry.display_name || entry.backend_class} (${readiness})`;
      backendEl.appendChild(option);
    }

    const preferred = prefill.backendClass || String(catalog && catalog.default_backend_class ? catalog.default_backend_class : "").trim();
    if (preferred && state.backendMap.has(preferred)) {
      backendEl.value = preferred;
    } else if (backendEl.options.length) {
      backendEl.selectedIndex = 0;
    }

    applyBackendSelection(backendEl.value);

    const panel = document.querySelector("[data-backend-status]");
    if (panel) {
      panel.setAttribute("data-backends", Array.from(state.backendMap.keys()).join(","));
    }
  }

  async function loadCatalog() {
    const resp = await fetch("/ui/api/image/catalog", {
      method: "GET",
      credentials: "same-origin",
    });
    if (handle401(resp)) return null;

    const text = await resp.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      throw new Error(`Invalid image catalog response: ${text}`);
    }

    if (!resp.ok) {
      throw new Error(typeof payload === "string" ? payload : JSON.stringify(payload));
    }

    state.catalog = payload;
    renderBackendSelect(payload);
    return payload;
  }

  function buildRequestBody() {
    const prompt = String(promptEl.value || "").trim();
    if (!prompt) throw new Error("prompt required");

    const backendClass = String(backendEl.value || "").trim();
    if (!backendClass) throw new Error("backend required");

    const body = {
      prompt,
      backend_class: backendClass,
      size: String(sizeEl.value || "1024x1024"),
      n: Math.max(1, Math.min(8, parseIntNum(nEl.value) ?? 1)),
    };

    const modelChoice = String(modelSelectEl.value || "").trim();
    if (modelChoice === "__custom__") {
      const custom = String(modelCustomEl.value || "").trim();
      if (custom) body.model = custom;
    } else if (modelChoice) {
      body.model = modelChoice;
    }

    const optionReaders = {
      seed: () => parseIntNum(seedEl.value),
      steps: () => parseIntNum(stepsEl.value),
      guidance_scale: () => parseNum(guidanceEl.value),
      scheduler: () => String(schedulerEl.value || "").trim() || null,
      style: () => String(styleEl.value || "").trim() || null,
      quality: () => String(qualityEl.value || "").trim() || null,
      negative_prompt: () => String(negativeEl.value || "").trim() || null,
    };

    for (const [key, reader] of Object.entries(optionReaders)) {
      const field = fieldDefs[key];
      if (!field || field.wrap.classList.contains("hidden")) continue;
      const value = reader();
      if (value !== null && value !== undefined && value !== "") {
        body[key] = value;
      }
    }

    const extraRaw = String(extraEl.value || "").trim();
    if (extraRaw) {
      let extra;
      try {
        extra = JSON.parse(extraRaw);
      } catch {
        throw new Error("extra JSON is invalid");
      }
      if (!extra || typeof extra !== "object" || Array.isArray(extra)) {
        throw new Error("extra JSON must be an object");
      }
      for (const [key, value] of Object.entries(extra)) {
        body[key] = value;
      }
    }

    return body;
  }

  function renderImages(payload) {
    const items = payload && payload.data;
    if (!Array.isArray(items) || !items.length) return;

    const urls = items
      .map((item) => item && typeof item.url === "string" ? item.url.trim() : "")
      .filter(Boolean);
    if (!urls.length) return;

    const viewer = document.createElement("div");
    viewer.className = "image-viewer";

    const stage = document.createElement("div");
    stage.className = "image-stage";
    const frame = document.createElement("div");
    frame.className = "image-stage-frame";
    const stageImage = document.createElement("img");
    frame.appendChild(stageImage);

    const toolbar = document.createElement("div");
    toolbar.className = "image-stage-toolbar";
    const counter = document.createElement("span");
    counter.className = "hint";
    counter.setAttribute("aria-live", "polite");
    const actions = document.createElement("div");
    actions.className = "image-stage-actions";
    const previousButton = document.createElement("button");
    previousButton.type = "button";
    previousButton.textContent = "Previous";
    previousButton.setAttribute("aria-label", "Previous image");
    const nextButton = document.createElement("button");
    nextButton.type = "button";
    nextButton.textContent = "Next";
    nextButton.setAttribute("aria-label", "Next image");
    const openLink = document.createElement("a");
    openLink.target = "_blank";
    openLink.rel = "noopener noreferrer";
    openLink.textContent = "Open full size";
    const copyButton = document.createElement("button");
    copyButton.type = "button";
    copyButton.textContent = "Copy URL";
    copyButton.setAttribute("data-ui-role", "secondary");
    actions.append(previousButton, nextButton, openLink, copyButton);
    toolbar.append(counter, actions);
    stage.append(frame, toolbar);

    const strip = document.createElement("div");
    strip.className = "thumbnail-strip";
    strip.setAttribute("role", "listbox");
    strip.setAttribute("aria-label", "Generated images");
    const thumbnailButtons = [];
    let selectedIndex = 0;

    function selectImage(index, { focus = false } = {}) {
      selectedIndex = Math.max(0, Math.min(index, urls.length - 1));
      const selectedUrl = urls[selectedIndex];
      stageImage.src = selectedUrl;
      stageImage.alt = `Generated image ${selectedIndex + 1} of ${urls.length}`;
      openLink.href = selectedUrl;
      counter.textContent = `Image ${selectedIndex + 1} of ${urls.length}`;
      thumbnailButtons.forEach((button, buttonIndex) => {
        const selected = buttonIndex === selectedIndex;
        button.setAttribute("aria-selected", selected ? "true" : "false");
        button.tabIndex = selected ? 0 : -1;
      });
      const selectedButton = thumbnailButtons[selectedIndex];
      selectedButton?.scrollIntoView({ block: "nearest", inline: "nearest" });
      if (focus) selectedButton?.focus();
    }

    urls.forEach((url, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "image-thumbnail";
      button.setAttribute("role", "option");
      button.setAttribute("aria-label", `Show generated image ${index + 1}`);
      const image = document.createElement("img");
      image.src = url;
      image.alt = "";
      // Fetch every batch member while its short-lived Gateway URL is valid.
      // Lazy loading can leave off-screen thumbnails unfetched until expiry.
      image.loading = "eager";
      button.appendChild(image);
      button.addEventListener("click", () => selectImage(index));
      button.addEventListener("keydown", (event) => {
        let nextIndex = null;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = selectedIndex + 1;
        if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = selectedIndex - 1;
        if (event.key === "Home") nextIndex = 0;
        if (event.key === "End") nextIndex = urls.length - 1;
        if (nextIndex === null) return;
        event.preventDefault();
        selectImage((nextIndex + urls.length) % urls.length, { focus: true });
      });
      thumbnailButtons.push(button);
      strip.appendChild(button);
    });

    copyButton.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(urls[selectedIndex]);
        setStatus(`Copied image ${selectedIndex + 1} URL to clipboard`, false);
      } catch (error) {
        setStatus("Could not copy the image URL", true);
      }
    });
    previousButton.disabled = urls.length < 2;
    nextButton.disabled = urls.length < 2;
    previousButton.addEventListener("click", () => {
      selectImage((selectedIndex - 1 + urls.length) % urls.length);
    });
    nextButton.addEventListener("click", () => {
      selectImage((selectedIndex + 1) % urls.length);
    });

    viewer.append(stage, strip);
    galleryEl.appendChild(viewer);
    selectImage(0);
  }

  async function generate() {
    setStatus("", false);
    clearOutput();

    let body;
    try {
      body = buildRequestBody();
    } catch (error) {
      setStatus(String(error && error.message ? error.message : error), true);
      return;
    }

    generateEl.disabled = true;
    setStatus("Generating...", false);

    const progressId = globalThis.crypto?.randomUUID
      ? globalThis.crypto.randomUUID()
      : `image-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    body.progress_id = progressId;

    let stop = null;
    let progressWrap = null;

    try {
      progressWrap = document.createElement("div");
      progressWrap.className = "progress-wrapper";
      const bar = document.createElement("div");
      bar.className = "progress";
      const inner = document.createElement("div");
      inner.className = "progress-inner";
      bar.appendChild(inner);
      const text = document.createElement("div");
      text.className = "progress-text";
      text.textContent = "0%";
      progressWrap.appendChild(bar);
      progressWrap.appendChild(text);
      statusEl.appendChild(progressWrap);

      let pollTimer = null;
      let polling = true;
      const renderProgress = (progress) => {
        const rawPercentage = Number(progress && progress.percentage);
        const hasPercentage = Number.isFinite(rawPercentage);
        const pct = hasPercentage
          ? Math.max(0, Math.min(100, Math.round(rawPercentage * 100)))
          : null;
        inner.style.width = pct === null ? "0%" : `${pct}%`;

        const parts = [];
        const currentImage = Number(progress && progress.current_image);
        const totalImages = Number(progress && progress.total_images);
        if (Number.isFinite(currentImage) && Number.isFinite(totalImages)) {
          parts.push(`Image ${currentImage} of ${totalImages}`);
        }
        const message = String((progress && progress.message) || "").trim();
        if (message) parts.push(message);
        if (pct !== null) parts.push(`${pct}%`);
        text.textContent = parts.join(" • ") || "Waiting for InvokeAI...";
      };
      const pollProgress = async () => {
        if (!polling) return;
        const backendClass = String(body.backend_class || body.backend || "gpu_heavy");
        try {
          const response = await fetch(
            `/ui/api/image/progress/${encodeURIComponent(progressId)}?backend_class=${encodeURIComponent(backendClass)}`,
            { credentials: "same-origin", cache: "no-store" },
          );
          if (response.ok) renderProgress(await response.json());
        } catch (error) {
          // Generation remains authoritative; a transient progress failure is non-fatal.
        } finally {
          if (polling) pollTimer = setTimeout(pollProgress, 500);
        }
      };
      void pollProgress();
      stop = (completed) => {
        polling = false;
        if (pollTimer !== null) clearTimeout(pollTimer);
        if (completed) {
          inner.style.width = "100%";
          text.textContent = "Generation complete • 100%";
        }
        setTimeout(() => {
          try {
            progressWrap.remove();
          } catch (error) {}
        }, 300);
      };

      const resp = await fetch("/ui/api/image", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (handle401(resp)) return;

      const textBody = await resp.text();
      let payload;
      try {
        payload = JSON.parse(textBody);
      } catch {
        payload = textBody;
      }

      debugEl.textContent = JSON.stringify({ request: body, response: payload }, null, 2);

      if (!resp.ok) {
        if (stop) stop(false);
        setStatus(`HTTP ${resp.status}: ${typeof payload === "string" ? payload : JSON.stringify(payload)}`, true);
        return;
      }

      if (stop) stop(true);

      const gateway = payload && payload._gateway;
      const bits = [];
      if (gateway && gateway.backend) bits.push(`backend=${gateway.backend}`);
      if (gateway && gateway.backend_class) bits.push(`class=${gateway.backend_class}`);
      if (gateway && gateway.model) bits.push(`model=${gateway.model}`);
      if (gateway && gateway.base_url) bits.push(`target=${gateway.base_url}`);
      if (gateway && gateway.ui_image_sha256) bits.push(`sha=${String(gateway.ui_image_sha256).slice(0, 12)}`);
      if (gateway && gateway.ttl_sec) bits.push(`ttl=${gateway.ttl_sec}s`);
      metaEl.textContent = bits.join(" • ");

      setStatus("Done", false);
      renderImages(payload);
    } catch (error) {
      if (stop) stop(false);
      setStatus(String(error), true);
    } finally {
      generateEl.disabled = false;
    }
  }

  backendEl.addEventListener("change", () => {
    prefill.model = "";
    applyBackendSelection(backendEl.value);
  });

  modelSelectEl.addEventListener("change", () => {
    const custom = String(modelSelectEl.value || "") === "__custom__";
    modelCustomWrapEl.classList.toggle("hidden", !custom);
    if (!custom) modelCustomEl.value = "";
  });

  refreshCatalogEl.addEventListener("click", async () => {
    refreshCatalogEl.disabled = true;
    setStatus("Refreshing backend catalog...", false);
    try {
      prefill.model = String(modelSelectEl.value || "") === "__custom__"
        ? String(modelCustomEl.value || "").trim()
        : String(modelSelectEl.value || "").trim();
      await loadCatalog();
      setStatus("Backend catalog refreshed.", false);
    } catch (error) {
      setStatus(String(error && error.message ? error.message : error), true);
    } finally {
      refreshCatalogEl.disabled = false;
    }
  });

  generateEl.addEventListener("click", () => void generate());

  promptEl.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      void generate();
    }
  });

  applySimplePrefill();
  void loadCatalog().catch((error) => {
    setStatus(`Failed to load image backends: ${String(error && error.message ? error.message : error)}`, true);
  });
})();
