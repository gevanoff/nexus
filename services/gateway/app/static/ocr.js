const imageUrlInput = document.getElementById("imageUrl");
const backendEl = document.getElementById("backend");
const backendHintEl = document.getElementById("backendHint");
const runButton = document.getElementById("run");
const statusEl = document.getElementById("status");
const outputEl = document.getElementById("output");
const debugEl = document.getElementById("debug");

function handle401(resp) {
  if (resp && resp.status === 401) {
    const back = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.href = `/ui/login?next=${back}`;
    return true;
  }
  return false;
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", isError);
}

function setOutput(text) {
  outputEl.textContent = text;
}

async function runScan() {
  const imageUrl = (imageUrlInput.value || "").trim();
  if (!imageUrl) {
    setStatus("Please provide an image URL.", true);
    return;
  }

  runButton.disabled = true;
  setStatus("Running OCR…");
  setOutput("Working…");

  const payload = { image_url: imageUrl };
  const backendClass = String(backendEl?.value || "").trim();
  if (backendClass) payload.backend_class = backendClass;
  debugEl.textContent = JSON.stringify({ request: payload }, null, 2);

  try {
    const resp = await fetch("/ui/api/scan", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (handle401(resp)) return;

    const text = await resp.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      data = { raw: text };
    }

    debugEl.textContent = JSON.stringify({ request: payload, response: data }, null, 2);

    if (!resp.ok) {
      setStatus(`OCR failed: ${resp.status}`, true);
      setOutput(JSON.stringify(data, null, 2));
      return;
    }

    const outText =
      (data && typeof data.text === "string" && data.text.trim()) ||
      (data && typeof data.output_text === "string" && data.output_text.trim()) ||
      (data && typeof data.result === "string" && data.result.trim()) ||
      "";

    if (outText) {
      setOutput(outText);
      setStatus("OCR complete.");
    } else {
      setOutput(JSON.stringify(data, null, 2));
      setStatus("OCR complete (no text field found).");
    }
  } catch (err) {
    setStatus(`OCR error: ${err}`, true);
    setOutput("Request failed.");
  } finally {
    runButton.disabled = false;
  }
}

async function loadBackends() {
  if (!backendEl) return;
  try {
    const resp = await fetch("/ui/api/ocr/backends", { method: "GET", credentials: "same-origin" });
    if (handle401(resp)) return;
    if (!resp.ok) {
      setStatus(`Failed to load OCR backends (HTTP ${resp.status}).`, true);
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
    if (backendHintEl) {
      backendHintEl.textContent = list.length
        ? `${list.length} OCR backend${list.length === 1 ? "" : "s"} available.`
        : "No OCR backends are currently available.";
    }
    const details = document.querySelector("[data-backend-status]");
    if (details instanceof HTMLElement) {
      const values = list
        .map((item) => String(item?.backend_class || "").trim())
        .filter(Boolean)
        .join(",");
      if (values) details.setAttribute("data-backends", values);
    }
  } catch (err) {
    setStatus(`Failed to load OCR backends: ${err}`, true);
  }
}

runButton.addEventListener("click", runScan);
imageUrlInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    runScan();
  }
});
void loadBackends();
