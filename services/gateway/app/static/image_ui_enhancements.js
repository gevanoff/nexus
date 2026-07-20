(() => {
  const statusEl = document.getElementById("status");
  const galleryEl = document.getElementById("gallery");
  const nameSchemeEl = document.getElementById("nameScheme");

  function tryJson(value) {
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  }

  function decodePythonQuoted(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    if (raw.startsWith('"')) {
      const parsed = tryJson(raw);
      return typeof parsed === "string" ? parsed : raw;
    }
    if (raw.startsWith("'") && raw.endsWith("'")) {
      return raw
        .slice(1, -1)
        .replace(/\\'/g, "'")
        .replace(/\\n/g, "\n")
        .replace(/\\"/g, '"')
        .replace(/\\\\/g, "\\");
    }
    return raw;
  }

  function unwrapBackendDetail(rawText) {
    const match = String(rawText || "").match(/^HTTP\s+(\d+):\s*([\s\S]*)$/);
    if (!match) return null;

    const gatewayStatus = Number(match[1]);
    const outer = tryJson(match[2]);
    let detail = outer && typeof outer === "object" && "detail" in outer ? outer.detail : match[2];
    if (detail && typeof detail === "object") {
      detail = detail.message || detail.detail || JSON.stringify(detail);
    }
    detail = String(detail || "").trim();

    const backendMatch = detail.match(/image(?: edit)? backend HTTP\s+(\d+):\s*([\s\S]*)$/i);
    let backendStatus = null;
    if (backendMatch) {
      backendStatus = Number(backendMatch[1]);
      detail = backendMatch[2].trim();
    }

    const pythonDetailPrefix = "{'detail': ";
    if (detail.startsWith(pythonDetailPrefix) && detail.endsWith("}")) {
      detail = decodePythonQuoted(detail.slice(pythonDetailPrefix.length, -1).trim());
    } else {
      const nested = tryJson(detail);
      if (nested && typeof nested === "object" && "detail" in nested) {
        detail = typeof nested.detail === "string" ? nested.detail : JSON.stringify(nested.detail);
      }
    }

    return { gatewayStatus, backendStatus, detail };
  }

  function humanizeImageError(parsed) {
    if (!parsed || !parsed.detail) return "";
    const detail = parsed.detail;

    const mismatch = detail.match(
      /InvokeAI workflow requires a\s+([A-Za-z0-9_-]+)\s+model, but model\s+['"]?([^'"]+)['"]?\s+is unavailable or incompatible\.\s+Compatible installed models:\s*([\s\S]+)/i,
    );
    if (mismatch) {
      return [
        "Image model/workflow mismatch.",
        `Selected model identifier: ${mismatch[2]}`,
        `Required workflow family: ${mismatch[1].toUpperCase()}`,
        `Compatible installed models: ${mismatch[3].trim()}`,
        "Resolution: refresh the backend catalog and choose one of the compatible models, or configure a workflow exported for the selected model family.",
      ].join("\n");
    }

    const status = parsed.backendStatus || parsed.gatewayStatus;
    const prefix = parsed.backendStatus
      ? `Image backend rejected the request (HTTP ${parsed.backendStatus}).`
      : `Image request failed (HTTP ${status}).`;
    return `${prefix}\n${detail}`;
  }

  let rewritingStatus = false;
  function formatStatusError() {
    if (!statusEl || rewritingStatus) return;
    const raw = String(statusEl.textContent || "").trim();
    if (!raw.startsWith("HTTP ")) return;
    const parsed = unwrapBackendDetail(raw);
    const formatted = humanizeImageError(parsed);
    if (!formatted || formatted === raw) return;
    rewritingStatus = true;
    statusEl.dataset.rawBackendError = raw;
    statusEl.textContent = formatted;
    statusEl.style.whiteSpace = "pre-wrap";
    statusEl.title = "The complete raw response remains available under Last request/response.";
    rewritingStatus = false;
  }

  function parseNamingScheme(rawValue, index) {
    const raw = String(rawValue || "").trim();
    const fallback = raw || "example01";
    const match = fallback.match(/^(.*?)(\d+)$/);
    let stem;
    if (match) {
      const prefix = match[1] || "image";
      const width = Math.max(1, match[2].length);
      const start = Number(match[2]);
      stem = `${prefix}${String(start + index).padStart(width, "0")}`;
    } else {
      stem = `${fallback}${String(index + 1).padStart(2, "0")}`;
    }
    stem = stem
      .replace(/[\\/:*?"<>|\x00-\x1f]/g, "-")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/[. ]+$/g, "");
    return stem || `image${String(index + 1).padStart(2, "0")}`;
  }

  function selectedIndex(viewer) {
    const counter = viewer.querySelector(".image-stage-toolbar .hint");
    const match = String(counter && counter.textContent ? counter.textContent : "").match(/Image\s+(\d+)\s+of/i);
    return match ? Math.max(0, Number(match[1]) - 1) : 0;
  }

  async function downloadSelected(viewer, button) {
    const image = viewer.querySelector(".image-stage-frame img");
    const src = String(image && image.src ? image.src : "").trim();
    if (!src) return;
    const index = selectedIndex(viewer);
    const filename = `${parseNamingScheme(nameSchemeEl && nameSchemeEl.value, index)}.png`;
    button.disabled = true;
    try {
      const response = await fetch(src, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (error) {
      if (statusEl) {
        statusEl.textContent = `Could not download ${filename}: ${String(error && error.message ? error.message : error)}`;
        statusEl.className = "hint error";
      }
    } finally {
      button.disabled = false;
    }
  }

  function updateDownloadButton(viewer) {
    const button = viewer.querySelector("[data-named-image-download]");
    if (!button) return;
    const index = selectedIndex(viewer);
    button.textContent = `Download ${parseNamingScheme(nameSchemeEl && nameSchemeEl.value, index)}.png`;
  }

  function enhanceViewer(viewer) {
    if (!(viewer instanceof HTMLElement) || viewer.dataset.namedDownloadReady === "true") return;
    const toolbar = viewer.querySelector(".image-stage-toolbar");
    if (!toolbar) return;
    let actions = viewer.querySelector(".image-stage-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "image-stage-actions";
      const openLink = toolbar.querySelector("a");
      if (openLink) actions.appendChild(openLink);
      toolbar.appendChild(actions);
    }
    viewer.dataset.namedDownloadReady = "true";
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("data-ui-role", "secondary");
    button.setAttribute("data-named-image-download", "true");
    button.addEventListener("click", () => void downloadSelected(viewer, button));
    actions.appendChild(button);

    const counter = viewer.querySelector(".image-stage-toolbar .hint");
    if (counter) {
      new MutationObserver(() => updateDownloadButton(viewer)).observe(counter, {
        childList: true,
        characterData: true,
        subtree: true,
      });
    }
    updateDownloadButton(viewer);
  }

  if (statusEl) {
    new MutationObserver(formatStatusError).observe(statusEl, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  if (galleryEl) {
    const enhanceGallery = () => galleryEl.querySelectorAll(".image-viewer").forEach(enhanceViewer);
    new MutationObserver(enhanceGallery).observe(galleryEl, { childList: true, subtree: true });
    enhanceGallery();
  }

  if (nameSchemeEl) {
    const query = new URLSearchParams(window.location.search || "");
    if (!nameSchemeEl.value && query.get("name_scheme")) nameSchemeEl.value = query.get("name_scheme");
    nameSchemeEl.addEventListener("input", () => {
      galleryEl?.querySelectorAll(".image-viewer").forEach(updateDownloadButton);
    });
  }
})();
