(() => {
  const $ = (id) => document.getElementById(id);

  const promptEl = $("prompt");
  const sizeEl = $("size");
  const nEl = $("n");
  const modelEl = $("model");
  const seedEl = $("seed");
  const stepsEl = $("steps");
  const guidanceEl = $("guidance");
  const negativeEl = $("negative");
  const extraEl = $("extra");
  const generateEl = $("generate");

  const statusEl = $("status");
  const metaEl = $("meta");
  const galleryEl = $("gallery");
  const debugEl = $("debug");

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
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

    const prompt = qs.get("prompt");
    const size = qs.get("size");
    const n = qs.get("n");
    const model = qs.get("model");
    const seed = qs.get("seed");
    const steps = qs.get("steps");
    const guidance = qs.get("guidance_scale") || qs.get("guidance");
    const negative = qs.get("negative_prompt") || qs.get("negative");

    if (prompt && promptEl) promptEl.value = prompt;
    if (size && sizeEl) sizeEl.value = size;
    if (n && nEl) nEl.value = n;
    if (model && modelEl) modelEl.value = model;
    if (seed && seedEl) seedEl.value = seed;
    if (steps && stepsEl) stepsEl.value = steps;
    if (guidance && guidanceEl) guidanceEl.value = guidance;
    if (negative && negativeEl) negativeEl.value = negative;

    // Optional: also accept a JSON blob for extra fields.
    const extraJson = qs.get("extra");
    if (extraJson && extraEl) extraEl.value = extraJson;
  }

  function buildRequestBody() {
    const prompt = String(promptEl.value || "").trim();
    if (!prompt) throw new Error("prompt required");

    const body = {
      prompt,
      size: String(sizeEl.value || "1024x1024"),
      n: Math.max(1, Math.min(8, parseIntNum(nEl.value) ?? 1)),
    };

    const model = String(modelEl.value || "").trim();
    if (model) body.model = model;

    const seed = parseIntNum(seedEl.value);
    if (seed !== null) body.seed = seed;

    const steps = parseIntNum(stepsEl.value);
    if (steps !== null) body.steps = steps;

    const guidance = parseNum(guidanceEl.value);
    if (guidance !== null) body.guidance_scale = guidance;

    const negative = String(negativeEl.value || "").trim();
    if (negative) body.negative_prompt = negative;

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
      for (const [k, v] of Object.entries(extra)) {
        body[k] = v;
      }
    }

    return body;
  }

  function renderImages(payload) {
    const items = payload?.data;
    if (!Array.isArray(items) || !items.length) return;

    for (const it of items) {
      const url = typeof it?.url === "string" ? it.url.trim() : "";
      if (!url) continue;

      const div = document.createElement("div");
      div.className = "thumb";
      div.innerHTML = `
        <img src="${escapeHtml(url)}" alt="generated" />
        <div style="margin-top:8px; display:flex; gap:10px; flex-wrap:wrap;">
          <a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">Open</a>
          <a href="#" data-copy="${escapeHtml(url)}">Copy URL</a>
        </div>
      `;

      div.addEventListener("click", (e) => {
        const a = e.target;
        if (!(a instanceof HTMLAnchorElement)) return;
        const copy = a.getAttribute("data-copy");
        if (!copy) return;
        e.preventDefault();
        void navigator.clipboard?.writeText(copy);
        setStatus("Copied URL to clipboard", false);
      });

      galleryEl.appendChild(div);
    }
  }

  async function generate() {
    setStatus("", false);
    metaEl.textContent = "";
    clearOutput();

    let body;
    try {
      body = buildRequestBody();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    generateEl.disabled = true;
    setStatus("Generating...", false);

    // show progress
    const progressContainer = document.getElementById('status');
    let stop = null;
    let progWrap = null;
    try {
      const wrapEl = document.createElement('div');
      wrapEl.className = 'progress-wrapper';
      const bar = document.createElement('div');
      bar.className = 'progress';
      const inner = document.createElement('div');
      inner.className = 'progress-inner';
      bar.appendChild(inner);
      const txt = document.createElement('div');
      txt.className = 'progress-text';
      txt.textContent = '0%';
      wrapEl.appendChild(bar);
      wrapEl.appendChild(txt);
      progWrap = wrapEl;
      if (progressContainer) progressContainer.appendChild(wrapEl);

      let pct = 0;
      const id = setInterval(() => {
        const step = Math.max(1, Math.floor((100 - pct) / 20));
        pct = Math.min(95, pct + step);
        inner.style.width = pct + '%';
        txt.textContent = pct + '%';
      }, 300);
      stop = () => { clearInterval(id); inner.style.width = '100%'; txt.textContent = '100%'; setTimeout(()=>{ try{ inner.style.width='0%'; txt.textContent=''; progWrap.remove(); }catch(e){} }, 300); };

      const resp = await fetch("/ui/api/image", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const text = await resp.text();
      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }

      debugEl.textContent = JSON.stringify({ request: body, response: payload }, null, 2);

      if (!resp.ok) {
        try { if (stop) stop(); } catch (e) {}
        setStatus(`HTTP ${resp.status}: ${typeof payload === "string" ? payload : JSON.stringify(payload)}`, true);
        return;
      }

      try { if (stop) stop(); } catch (e) {}

      const gw = payload?._gateway;
      const bits = [];
      if (gw?.backend) bits.push(`backend=${gw.backend}`);
      if (gw?.model) bits.push(`model=${gw.model}`);
      if (gw?.ui_image_sha256) bits.push(`sha=${String(gw.ui_image_sha256).slice(0, 12)}`);
      if (gw?.ttl_sec) bits.push(`ttl=${gw.ttl_sec}s`);
      metaEl.textContent = bits.join(" • ");

      setStatus("Done", false);
      renderImages(payload);
    } catch (e) {
      setStatus(String(e), true);
    } finally {
      generateEl.disabled = false;
    }
  }

  generateEl.addEventListener("click", () => void generate());

  promptEl.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      void generate();
    }
  });

  readQueryPrefill();
})();
