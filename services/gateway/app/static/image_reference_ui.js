(() => {
  const $ = (id) => document.getElementById(id);
  const generateButton = $("generate");
  const promptInput = $("prompt");
  const backendSelect = $("backend");
  const backendSummary = $("backendSummary");
  const gallery = $("gallery");
  const status = $("status");
  const meta = $("meta");
  const debug = $("debug");

  if (!generateButton || !promptInput || !backendSelect || !backendSummary) return;

  const state = {
    referenceFile: null,
    maskFile: null,
    referenceUrl: "",
    maskUrl: "",
    running: false,
  };

  const style = document.createElement("style");
  style.textContent = `
    .reference-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-top: 10px;
    }
    .reference-drop {
      min-height: 150px;
      border: 1px dashed rgba(231,237,246,0.22);
      border-radius: 12px;
      padding: 10px;
      display: grid;
      align-content: start;
      gap: 9px;
      background: rgba(8,12,18,0.48);
    }
    .reference-drop.has-file { border-style: solid; border-color: rgba(111,184,255,0.42); }
    .reference-preview {
      min-height: 96px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 9px;
      overflow: hidden;
      background: #080c12;
    }
    .reference-preview img {
      max-width: 100%;
      max-height: 220px;
      object-fit: contain;
      border: 0;
      border-radius: 8px;
    }
    .reference-placeholder { color: var(--muted); font-size: 12px; text-align: center; padding: 20px; }
    .reference-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .reference-actions input[type="file"] { max-width: 100%; padding: 8px; }
    .reference-slider { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; }
    .reference-slider input { width: 100%; padding: 0; }
    .reference-value { min-width: 3.5em; text-align: right; font-variant-numeric: tabular-nums; }
    .reference-disabled { opacity: 0.55; }
  `;
  document.head.appendChild(style);

  const panel = document.createElement("section");
  panel.id = "referenceImagePanel";
  panel.className = "panel-subtle";
  panel.style.marginTop = "12px";
  panel.innerHTML = `
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div>
        <strong style="font-size:13px">Reference image</strong>
        <div class="field-help">Adding a reference switches Generate to the OpenAI-compatible image-edit route.</div>
      </div>
      <span id="referenceBackendSupport" class="field-help"></span>
    </div>
    <div class="reference-grid">
      <div id="referenceDrop" class="reference-drop">
        <label>
          <span>Reference image</span>
          <input id="referenceImage" type="file" accept="image/*" />
        </label>
        <div id="referencePreview" class="reference-preview"><div class="reference-placeholder">No reference image selected.</div></div>
        <div class="reference-actions">
          <button id="removeReferenceImage" type="button" data-ui-role="secondary" disabled>Remove reference</button>
          <span id="referenceFileMeta" class="field-help"></span>
        </div>
      </div>
      <div>
        <label>
          <span>Purpose</span>
          <select id="referencePurpose">
            <option value="image_to_image">Image-to-image</option>
            <option value="composition">Composition reference</option>
            <option value="style">Style reference</option>
            <option value="controlnet">ControlNet</option>
          </select>
        </label>
        <div id="referencePurposeHelp" class="field-help" style="margin-top:6px"></div>
        <label style="margin-top:12px">
          <span>Strength / influence</span>
          <div class="reference-slider">
            <input id="referenceStrength" type="range" min="0" max="1" step="0.05" value="0.65" />
            <output id="referenceStrengthValue" class="reference-value">0.65</output>
          </div>
        </label>
        <div class="field-help" style="margin-top:5px">Higher values give the reference more influence. The selected InvokeAI workflow must expose a compatible strength or weight input.</div>
      </div>
      <div id="maskDrop" class="reference-drop">
        <label>
          <span>Inpainting mask (optional)</span>
          <input id="referenceMask" type="file" accept="image/*" />
        </label>
        <div id="maskPreview" class="reference-preview"><div class="reference-placeholder">No mask selected.</div></div>
        <div class="reference-actions">
          <button id="removeReferenceMask" type="button" data-ui-role="secondary" disabled>Remove mask</button>
          <span id="maskFileMeta" class="field-help"></span>
        </div>
        <div class="field-help">Masks are valid only for image-to-image. The mask dimensions must match the reference image.</div>
      </div>
    </div>
  `;
  backendSummary.parentNode.insertBefore(panel, backendSummary);

  const referenceInput = $("referenceImage");
  const referencePreview = $("referencePreview");
  const referenceDrop = $("referenceDrop");
  const referenceMeta = $("referenceFileMeta");
  const removeReference = $("removeReferenceImage");
  const purposeInput = $("referencePurpose");
  const purposeHelp = $("referencePurposeHelp");
  const strengthInput = $("referenceStrength");
  const strengthValue = $("referenceStrengthValue");
  const maskInput = $("referenceMask");
  const maskPreview = $("maskPreview");
  const maskDrop = $("maskDrop");
  const maskMeta = $("maskFileMeta");
  const removeMask = $("removeReferenceMask");
  const backendSupport = $("referenceBackendSupport");

  const purposeHelpText = {
    image_to_image: "Uses an img2img/image-to-latents workflow. Add a mask to use an inpainting workflow.",
    composition: "Uses an IP-Adapter/reference workflow configured to preserve composition.",
    style: "Uses an IP-Adapter/reference workflow configured for style transfer.",
    controlnet: "Uses a ControlNet/T2I-Adapter workflow. The server-side graph determines the preprocessor and control model.",
  };

  function setStatus(text, isError = false) {
    status.textContent = text || "";
    status.className = isError ? "hint error" : "hint";
  }

  function selectedBackendSupportsEdits() {
    const value = String(backendSelect.value || "").trim().toLowerCase();
    const optionText = String(backendSelect.selectedOptions?.[0]?.textContent || "").toLowerCase();
    return value === "gpu_heavy" || value.includes("invoke") || optionText.includes("invoke");
  }

  function updateBackendSupport() {
    const supported = selectedBackendSupportsEdits();
    backendSupport.textContent = supported
      ? "Selected backend advertises the InvokeAI edit path."
      : "Reference workflows currently require the InvokeAI backend.";
    backendSupport.className = supported ? "field-help" : "field-help error";
    panel.classList.toggle("reference-disabled", !supported && !state.referenceFile);
  }

  function formatFile(file) {
    if (!file) return "";
    const kib = Math.max(1, Math.round(file.size / 1024));
    return `${file.name} • ${kib} KiB`;
  }

  function revokeUrl(key) {
    const url = state[key];
    if (url) URL.revokeObjectURL(url);
    state[key] = "";
  }

  function renderFile(kind) {
    const isReference = kind === "reference";
    const file = isReference ? state.referenceFile : state.maskFile;
    const preview = isReference ? referencePreview : maskPreview;
    const drop = isReference ? referenceDrop : maskDrop;
    const fileMeta = isReference ? referenceMeta : maskMeta;
    const removeButton = isReference ? removeReference : removeMask;
    const urlKey = isReference ? "referenceUrl" : "maskUrl";

    revokeUrl(urlKey);
    preview.replaceChildren();
    drop.classList.toggle("has-file", !!file);
    removeButton.disabled = !file;
    fileMeta.textContent = formatFile(file);

    if (!file) {
      const placeholder = document.createElement("div");
      placeholder.className = "reference-placeholder";
      placeholder.textContent = isReference ? "No reference image selected." : "No mask selected.";
      preview.appendChild(placeholder);
      return;
    }

    state[urlKey] = URL.createObjectURL(file);
    const img = document.createElement("img");
    img.src = state[urlKey];
    img.alt = isReference ? "Reference image preview" : "Mask preview";
    preview.appendChild(img);
  }

  function clearReference() {
    state.referenceFile = null;
    referenceInput.value = "";
    renderFile("reference");
    clearMask();
  }

  function clearMask() {
    state.maskFile = null;
    maskInput.value = "";
    renderFile("mask");
  }

  function updatePurpose() {
    const purpose = String(purposeInput.value || "image_to_image");
    purposeHelp.textContent = purposeHelpText[purpose] || "";
    const maskAllowed = purpose === "image_to_image";
    maskDrop.classList.toggle("hidden", !maskAllowed);
    if (!maskAllowed && state.maskFile) clearMask();
  }

  async function imageDimensions(file) {
    if (!file) return null;
    if (typeof createImageBitmap === "function") {
      const bitmap = await createImageBitmap(file);
      try {
        return { width: bitmap.width, height: bitmap.height };
      } finally {
        bitmap.close();
      }
    }
    return await new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const image = new Image();
      image.onload = () => {
        resolve({ width: image.naturalWidth, height: image.naturalHeight });
        URL.revokeObjectURL(url);
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error(`Could not decode ${file.name}`));
      };
      image.src = url;
    });
  }

  async function validateFiles() {
    if (!state.referenceFile) throw new Error("Select a reference image first.");
    if (!String(state.referenceFile.type || "").startsWith("image/")) {
      throw new Error("Reference file must be an image.");
    }
    if (!state.maskFile) return;
    if (purposeInput.value !== "image_to_image") {
      throw new Error("Masks are supported only for image-to-image/inpainting.");
    }
    if (!String(state.maskFile.type || "").startsWith("image/")) {
      throw new Error("Mask file must be an image.");
    }
    const [referenceSize, maskSize] = await Promise.all([
      imageDimensions(state.referenceFile),
      imageDimensions(state.maskFile),
    ]);
    if (
      referenceSize &&
      maskSize &&
      (referenceSize.width !== maskSize.width || referenceSize.height !== maskSize.height)
    ) {
      throw new Error(
        `Mask dimensions ${maskSize.width}x${maskSize.height} must match reference ${referenceSize.width}x${referenceSize.height}.`
      );
    }
  }

  function appendIfPresent(form, key, element, transform = (value) => value) {
    if (!element || element.closest(".hidden")) return;
    const raw = String(element.value || "").trim();
    if (!raw) return;
    form.append(key, transform(raw));
  }

  function selectedModel() {
    const select = $("modelSelect");
    const custom = $("modelCustom");
    const value = String(select?.value || "").trim();
    if (value === "__custom__") return String(custom?.value || "").trim();
    return value;
  }

  function buildFormData() {
    const prompt = String(promptInput.value || "").trim();
    if (!prompt) throw new Error("prompt required");
    const backendClass = String(backendSelect.value || "").trim();
    if (!backendClass) throw new Error("backend required");
    if (!selectedBackendSupportsEdits()) {
      throw new Error("Reference-image workflows currently require the InvokeAI backend.");
    }

    const form = new FormData();
    form.append("prompt", prompt);
    form.append("backend_class", backendClass);
    form.append("purpose", String(purposeInput.value || "image_to_image"));
    form.append("strength", String(Number(strengthInput.value || 0.65)));
    form.append("size", String($("size")?.value || "1024x1024"));
    form.append("n", String(Math.max(1, Math.min(8, Number.parseInt($("n")?.value || "1", 10) || 1))));
    form.append("image", state.referenceFile, state.referenceFile.name || "reference.png");
    if (state.maskFile) form.append("mask", state.maskFile, state.maskFile.name || "mask.png");

    const model = selectedModel();
    if (model) form.append("model", model);
    appendIfPresent(form, "seed", $("seed"));
    appendIfPresent(form, "steps", $("steps"));
    appendIfPresent(form, "guidance_scale", $("guidance"));
    appendIfPresent(form, "scheduler", $("scheduler"));
    appendIfPresent(form, "negative_prompt", $("negative"));

    const extraRaw = String($("extra")?.value || "").trim();
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
        if (value === null || value === undefined || typeof value === "object") continue;
        form.set(key, String(value));
      }
    }
    return form;
  }

  function responseUrls(payload) {
    const items = Array.isArray(payload?.data) ? payload.data : [];
    return items.map((item) => {
      const url = String(item?.url || "").trim();
      if (url) return url;
      const b64 = String(item?.b64_json || "").trim();
      return b64 ? `data:image/png;base64,${b64}` : "";
    }).filter(Boolean);
  }

  function renderImages(payload) {
    gallery.replaceChildren();
    const urls = responseUrls(payload);
    if (!urls.length) return;

    const viewer = document.createElement("div");
    viewer.className = "image-viewer";
    const stage = document.createElement("div");
    stage.className = "image-stage";
    const frame = document.createElement("div");
    frame.className = "image-stage-frame";
    const image = document.createElement("img");
    frame.appendChild(image);
    const toolbar = document.createElement("div");
    toolbar.className = "image-stage-toolbar";
    const counter = document.createElement("span");
    counter.className = "hint";
    const open = document.createElement("a");
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    open.textContent = "Open full size";
    toolbar.append(counter, open);
    stage.append(frame, toolbar);

    const strip = document.createElement("div");
    strip.className = "thumbnail-strip";
    const buttons = [];
    const select = (index) => {
      const url = urls[index];
      image.src = url;
      image.alt = `Edited image ${index + 1} of ${urls.length}`;
      open.href = url;
      counter.textContent = `Image ${index + 1} of ${urls.length}`;
      buttons.forEach((button, idx) => button.setAttribute("aria-selected", idx === index ? "true" : "false"));
    };
    urls.forEach((url, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "image-thumbnail";
      button.setAttribute("aria-selected", "false");
      const thumb = document.createElement("img");
      thumb.src = url;
      thumb.alt = "";
      button.appendChild(thumb);
      button.addEventListener("click", () => select(index));
      buttons.push(button);
      strip.appendChild(button);
    });
    viewer.append(stage, strip);
    gallery.appendChild(viewer);
    select(0);
  }

  async function runEdit() {
    if (state.running) return;
    state.running = true;
    generateButton.disabled = true;
    meta.textContent = "";
    gallery.replaceChildren();
    setStatus("Validating reference workflow…");

    let form;
    try {
      await validateFiles();
      form = buildFormData();
      setStatus("Generating from reference image…");
      const response = await fetch("/ui/api/image/edit", {
        method: "POST",
        credentials: "same-origin",
        body: form,
      });
      if (response.status === 401) {
        const back = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/ui/login?next=${back}`;
        return;
      }
      const raw = await response.text();
      let payload;
      try {
        payload = JSON.parse(raw);
      } catch {
        payload = raw;
      }
      const requestSummary = {
        prompt: String(promptInput.value || "").trim(),
        backend_class: String(backendSelect.value || "").trim(),
        purpose: String(purposeInput.value || ""),
        strength: Number(strengthInput.value || 0.65),
        image: state.referenceFile?.name || "",
        mask: state.maskFile?.name || null,
        size: String($("size")?.value || ""),
        n: Number.parseInt($("n")?.value || "1", 10) || 1,
        model: selectedModel() || null,
      };
      debug.textContent = JSON.stringify({ request: requestSummary, response: payload }, null, 2);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${typeof payload === "string" ? payload : JSON.stringify(payload)}`);
      }

      const gateway = payload?._gateway || {};
      const bits = [
        gateway.backend_class ? `class=${gateway.backend_class}` : "",
        gateway.model ? `model=${gateway.model}` : "",
        `purpose=${purposeInput.value}`,
        `strength=${Number(strengthInput.value || 0.65).toFixed(2)}`,
      ].filter(Boolean);
      meta.textContent = bits.join(" • ");
      renderImages(payload);
      setStatus("Done");
    } catch (error) {
      setStatus(String(error?.message || error), true);
    } finally {
      state.running = false;
      generateButton.disabled = false;
    }
  }

  referenceInput.addEventListener("change", () => {
    state.referenceFile = referenceInput.files?.[0] || null;
    renderFile("reference");
  });
  maskInput.addEventListener("change", () => {
    state.maskFile = maskInput.files?.[0] || null;
    renderFile("mask");
  });
  removeReference.addEventListener("click", clearReference);
  removeMask.addEventListener("click", clearMask);
  purposeInput.addEventListener("change", updatePurpose);
  strengthInput.addEventListener("input", () => {
    strengthValue.textContent = Number(strengthInput.value || 0).toFixed(2);
  });
  backendSelect.addEventListener("change", updateBackendSupport);

  generateButton.addEventListener("click", (event) => {
    if (!state.referenceFile) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    void runEdit();
  }, true);

  promptInput.addEventListener("keydown", (event) => {
    if (!state.referenceFile || !(event.ctrlKey || event.metaKey) || event.key !== "Enter") return;
    event.preventDefault();
    event.stopImmediatePropagation();
    void runEdit();
  }, true);

  window.addEventListener("beforeunload", () => {
    revokeUrl("referenceUrl");
    revokeUrl("maskUrl");
  });

  updatePurpose();
  updateBackendSupport();
  renderFile("reference");
  renderFile("mask");
})();
