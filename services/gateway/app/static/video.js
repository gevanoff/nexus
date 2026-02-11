(() => {
  const $ = (id) => document.getElementById(id);

  const promptEl = $("prompt");
  const durationEl = $("duration");
  const resolutionEl = $("resolution");
  const generateEl = $("generate");
  const statusEl = $("status");
  const metaEl = $("meta");
  const previewEl = $("preview");

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

  function buildRequestPreview() {
    const prompt = String(promptEl.value || "").trim();
    if (!prompt) throw new Error("prompt is required");

    const duration = Math.max(1, Math.min(30, parseInt(String(durationEl.value || "6"), 10) || 6));
    const resolution = String(resolutionEl.value || "720p").trim();
    return { prompt, duration, resolution };
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
      if (url && typeof url === "string") {
        setPreview(`<video controls style="max-width:100%" src="${url}"></video>`);
      } else {
        setPreview(`<pre>${JSON.stringify(payload, null, 2)}</pre>`);
      }
      const metaBits = [`Status: ${payload?.status || "ok"}`];
      if (requestId) metaBits.push(`Request ID: ${requestId}`);
      setMeta(metaBits.join(" · "));
      setStatus("Done.", false);
    } catch (e) {
      setStatus(String(e), true);
    } finally {
      generateEl.disabled = false;
    }
  }

  generateEl.addEventListener("click", handleGenerate);
})();
