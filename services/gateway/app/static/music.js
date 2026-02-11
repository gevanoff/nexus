(() => {
  const $ = (id) => document.getElementById(id);

  const styleEl = $("style");
  const lyricsEl = $("lyrics");
  const durationEl = $("duration");
  const modelEl = $("model");
  const tempEl = $("temperature");
  const topPEl = $("top_p");
  const topKEl = $("top_k");
  const tagsEl = $("tags");
  const extraEl = $("extra");
  const generateEl = $("generate");

  const statusEl = $("status");
  const metaEl = $("meta");
  const galleryEl = $("gallery");
  const debugEl = $("debug");

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  function clearOutput() {
    metaEl.textContent = "";
    galleryEl.innerHTML = "";
  }

  // UI progress helpers
  function _createUiProgress() {
    const wrap = document.createElement('div');
    wrap.className = 'progress-wrapper';
    const bar = document.createElement('div');
    bar.className = 'progress';
    const inner = document.createElement('div');
    inner.className = 'progress-inner';
    bar.appendChild(inner);
    const txt = document.createElement('div');
    txt.className = 'progress-text';
    txt.textContent = 'Processing...';
    wrap.appendChild(bar);
    wrap.appendChild(txt);
    return {wrap, inner, txt};
  }

  function _startUiProgress(inner, txt) {
    inner.classList.add('indeterminate');
    txt.textContent = 'Processing...';
    return () => {
      inner.classList.remove('indeterminate');
      txt.textContent = '';
    };
  }

  function parseNum(value) {
    const s = String(value || "").trim();
    if (!s) return null;
    const n = Number(s);
    return Number.isFinite(n) ? n : null;
  }

  function buildRequestBody() {
    const style = String(styleEl.value || "").trim();
    const lyrics = String(lyricsEl.value || "").trim();
    if (!style && !lyrics) throw new Error("style or lyrics required");

    const body = {
      duration: Math.max(1, Math.min(300, parseInt(String(durationEl.value || "15"), 10) || 15)),
    };

    if (style) body.style = style;
    if (lyrics) body.lyrics = lyrics;

    const model = String(modelEl.value || "").trim();
    if (model) body.model = model;

    const temperature = parseNum(tempEl.value);
    if (temperature !== null) body.temperature = temperature;

    const top_p = parseNum(topPEl.value);
    if (top_p !== null) body.top_p = top_p;

    const top_k = parseInt(String(topKEl.value || "0"), 10);
    if (!Number.isNaN(top_k) && top_k > 0) body.top_k = top_k;

    const tags = String(tagsEl.value || "").trim();
    if (tags) body.tags = tags.split(/\s*,\s*/).filter(Boolean);

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

  function renderAudio(payload) {
    const url = payload?.audio_url;
    if (!url) return;

    const div = document.createElement("div");
    div.className = "thumb";
    const wrapper = document.createElement("div");
    wrapper.className = "audio-card";

    const audio = document.createElement("audio");
    audio.src = url;
    audio.preload = "metadata";

    const controls = document.createElement("div");
    controls.className = "audio-controls";

    const meta = document.createElement("div");
    meta.className = "audio-meta";
    const currentEl = document.createElement("span");
    currentEl.textContent = "0:00";
    const totalEl = document.createElement("span");
    totalEl.textContent = "0:00";
    meta.appendChild(currentEl);
    meta.appendChild(totalEl);

    const sliders = document.createElement("div");
    sliders.className = "audio-sliders";
    // Remove seek slider for music thumbnails; only expose labeled volume control.
    const volumeLabel = document.createElement("span");
    volumeLabel.textContent = "Volume";
    volumeLabel.className = "volume-label";
    const volume = document.createElement("input");
    volume.type = "range";
    volume.min = "0";
    volume.max = "1";
    volume.step = "0.01";
    volume.value = String(audio.volume);
    volume.title = "Volume";
    sliders.appendChild(volumeLabel);
    sliders.appendChild(volume);

    controls.appendChild(meta);
    controls.appendChild(sliders);

    const links = document.createElement("div");
    links.style.display = "flex";
    links.style.gap = "12px";
    links.style.justifyContent = "flex-end";
    links.innerHTML = `
      <a href="${url}" target="_blank" rel="noreferrer">Open</a>
      <a href="#" data-copy="${url}">Copy URL</a>
    div.innerHTML = `
      <div style="display:flex; flex-direction:column; gap:10px;">
        <audio controls src="${url}"></audio>
        <div style="display:flex; gap:12px; justify-content:flex-end;">
          <a href="${url}" target="_blank" rel="noreferrer">Open</a>
          <a href="#" data-copy="${url}">Copy URL</a>
        </div>
      </div>
    `;

    wrapper.appendChild(audio);
    wrapper.appendChild(controls);
    wrapper.appendChild(links);
    div.appendChild(wrapper);

    audio.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(audio.duration)) {
        totalEl.textContent = formatTime(audio.duration);
      }
    });
    audio.addEventListener("timeupdate", () => {
      currentEl.textContent = formatTime(audio.currentTime);
    });
    volume.addEventListener("input", () => {
      audio.volume = Number(volume.value);
    });

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

  function formatTime(seconds) {
    const total = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
    const mins = Math.floor(total / 60);
    const secs = Math.floor(total % 60);
    return `${mins}:${secs.toString().padStart(2, "0")}`;
  }

  function readQueryPrefill() {
    const qs = new URLSearchParams(location.search || "");

    const style = qs.get("style");
    const lyrics = qs.get("lyrics");
    const duration = qs.get("duration");
    const model = qs.get("model");
    const temperature = qs.get("temperature");
    const top_p = qs.get("top_p");
    const top_k = qs.get("top_k");

    if (style && styleEl) styleEl.value = style;
    if (lyrics && lyricsEl) lyricsEl.value = lyrics;
    if (duration && durationEl) durationEl.value = duration;
    if (model && modelEl) modelEl.value = model;
    if (temperature && tempEl) tempEl.value = temperature;
    if (top_p && topPEl) topPEl.value = top_p;
    if (top_k && topKEl) topKEl.value = top_k;

    // Optional: also accept a JSON blob for extra fields.
    const extraJson = qs.get("extra");
    if (extraJson && extraEl) extraEl.value = extraJson;
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

    // show progress bar
    const progressContainer = $("music_progress");
    let stop = null;
    let progWrap = null;
    try {
      const {wrap, inner, txt} = _createUiProgress();
      progWrap = wrap;
      if (progressContainer) progressContainer.appendChild(wrap);
      stop = _startUiProgress(inner, txt);

      const resp = await fetch("/ui/api/music", {
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
        try { if (progWrap) progWrap.remove(); } catch (e) {}
        setStatus(`HTTP ${resp.status}: ${typeof payload === "string" ? payload : JSON.stringify(payload)}`, true);
        return;
      }

      // finish progress
      try { if (stop) stop(); } catch (e) {}
      try { if (progWrap) progWrap.remove(); } catch (e) {}

      const gw = payload?._gateway;
      const bits = [];
      if (gw?.backend) bits.push(`backend=${gw.backend}`);
      if (gw?.backend_class) bits.push(`class=${gw.backend_class}`);
      if (gw?.upstream_latency_ms) bits.push(`latency=${Math.round(gw.upstream_latency_ms)}ms`);
      metaEl.textContent = bits.join(" • ");

      setStatus("Done", false);
      renderAudio(payload);
    } catch (e) {
      setStatus(String(e), true);
    } finally {
      generateEl.disabled = false;
    }
  }

  generateEl.addEventListener("click", () => void generate());

  styleEl.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      void generate();
    }
  });

  // Prefill from query string when present (so /ui/music?prompt=... works)
  (function () {
    try {
      readQueryPrefill();
    } catch (e) {
      // ignore
    }
  })();
})();
