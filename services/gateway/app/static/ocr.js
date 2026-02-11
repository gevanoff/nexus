const imageUrlInput = document.getElementById("imageUrl");
const runButton = document.getElementById("run");
const statusEl = document.getElementById("status");
const outputEl = document.getElementById("output");
const debugEl = document.getElementById("debug");

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
  debugEl.textContent = JSON.stringify({ request: payload }, null, 2);

  try {
    const resp = await fetch("/ui/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

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

runButton.addEventListener("click", runScan);
imageUrlInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    runScan();
  }
});
