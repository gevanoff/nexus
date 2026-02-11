(() => {
  const $ = (id) => document.getElementById(id);

  const textEl = $("text");
  const backendEl = $("backend");
  const backendHealthEl = $("backendHealth");
  const voiceEl = $("voice");
  const speedEl = $("speed");
  const generateEl = $("generate");
  const statusEl = $("status");
  const metaEl = $("meta");
  const playerEl = $("player");

  let activeObjectUrl = null;

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  function setMeta(text) {
    metaEl.textContent = text || "";
  }

  function clearPlayer() {
    if (activeObjectUrl) {
      try {
        URL.revokeObjectURL(activeObjectUrl);
      } catch (e) {
        // ignore
      }
      activeObjectUrl = null;
    }
    if (playerEl) playerEl.innerHTML = "";
  }

  function buildRequest() {
    const text = String(textEl.value || "").trim();
    if (!text) throw new Error("text is required");

    const backendClass = String(backendEl?.value || "").trim();
    const voice = String(voiceEl.value || "").trim();
    const speedRaw = parseFloat(String(speedEl.value || "1"));
    const speed = Number.isFinite(speedRaw) ? Math.min(2, Math.max(0.5, speedRaw)) : 1;

    const body = { text, speed };
    if (backendClass) body.backend_class = backendClass;
    if (voice) body.voice = voice;

    return body;
  }

  async function loadVoices() {
    if (!voiceEl) return;
    try {
      const backendClass = String(backendEl?.value || "").trim();
      const qs = backendClass ? `?backend_class=${encodeURIComponent(backendClass)}` : "";
      const resp = await fetch(`/ui/api/tts/voices${qs}`, { method: 'GET', credentials: 'same-origin' });
      if (!resp.ok) return;
      let payload;
      try {
        payload = await resp.json();
      } catch {
        return;
      }

      let list = [];
      if (Array.isArray(payload)) {
        list = payload;
      } else if (Array.isArray(payload.voices)) {
        list = payload.voices;
      } else if (Array.isArray(payload.data)) {
        list = payload.data;
      } else if (Array.isArray(payload.items)) {
        list = payload.items;
      }

      for (const v of list) {
        let val = '';
        let label = '';
        if (typeof v === 'string') {
          val = v; label = v;
        } else if (v && typeof v === 'object') {
          val = v.id || v.name || v.voice || JSON.stringify(v);
          label = v.name || v.id || v.voice || val;
        }
        if (!val) continue;
        const opt = document.createElement('option');
        opt.value = val;
        opt.textContent = label;
        voiceEl.appendChild(opt);
      }
    } catch (e) {
      // ignore failures; voices are optional
    }
  }

  async function loadBackends() {
    if (!backendEl) return;
    try {
      const resp = await fetch('/ui/api/tts/backends', { method: 'GET', credentials: 'same-origin' });
      if (!resp.ok) return;
      const payload = await resp.json();
      const list = Array.isArray(payload?.available_backends) ? payload.available_backends : [];
      backendEl.innerHTML = '<option value="">(default)</option>';
      for (const item of list) {
        const val = item?.backend_class;
        if (!val) continue;
        const opt = document.createElement('option');
        opt.value = val;
        const health = item?.ready === false ? 'not ready' : (item?.healthy === false ? 'unhealthy' : 'ready');
        opt.textContent = item?.description ? `${val} — ${item.description} (${health})` : `${val} (${health})`;
        backendEl.appendChild(opt);
      }
      if (backendHealthEl) {
        const selected = list.find((b) => b.backend_class === backendEl.value) || list[0];
        if (selected) {
          const health = selected?.ready === false ? 'not ready' : (selected?.healthy === false ? 'unhealthy' : 'ready');
          backendHealthEl.textContent = `${selected.backend_class}: ${health}`;
        }
      }
    } catch (e) {
      // ignore
    }
  }

  function formatTime(seconds) {
    const total = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
    const mins = Math.floor(total / 60);
    const secs = Math.floor(total % 60);
    return `${mins}:${secs.toString().padStart(2, "0")}`;
  }

  function renderAudio(url) {
    if (!url) return;
    if (!playerEl) return;

    const wrapper = document.createElement("div");
    wrapper.className = "audio-card";

    const audio = document.createElement("audio");
    audio.src = url;
    audio.preload = "metadata";
    audio.controls = true;

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
    // Only expose a volume control for TTS playback; remove seek slider.
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

    const makeSafe = (s) => {
      try {
        return String(s || "").replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 48) || "tts";
      } catch (e) {
        return "tts";
      }
    };

    const openA = document.createElement("a");
    openA.href = url;
    openA.target = "_blank";
    openA.rel = "noreferrer";
    openA.textContent = "Open";

    const downloadA = document.createElement("a");
    downloadA.href = url;
    downloadA.rel = "noopener noreferrer";
    downloadA.textContent = "Download";

    // Build a dynamic filename: <voice>-<uniq>.<ext>
    try {
      const voiceName = makeSafe((voiceEl && voiceEl.value) ? voiceEl.value : "tts");
      const uniq = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
      let ext = "wav";
      try {
        const m = String(url || "").split("?")[0].split(".");
        if (m.length > 1) {
          const maybe = m[m.length - 1].toLowerCase();
          if (/^[a-z0-9]{1,6}$/.test(maybe)) ext = maybe;
        }
      } catch (e) {}
      downloadA.download = `${voiceName}-${uniq}.${ext}`;
    } catch (e) {
      // ignore and let browser default to the href name
    }

    links.appendChild(openA);
    links.appendChild(downloadA);

    wrapper.appendChild(audio);
    wrapper.appendChild(controls);
    wrapper.appendChild(links);

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

    // links provide Open and Download actions; no copy handler required.

    playerEl.appendChild(wrapper);
  }

  function decodeBase64Audio(payload) {
    const raw = payload?.audio_base64 || payload?.audio || payload?.audio_data;
    if (!raw) return null;
    let b64 = String(raw);
    let contentType = payload?.content_type || payload?.mime_type || payload?.format || "audio/wav";
    if (b64.startsWith("data:")) {
      const parts = b64.split(",", 2);
      if (parts.length === 2) {
        const header = parts[0];
        b64 = parts[1];
        const mime = header.split(";")[0].replace("data:", "");
        if (mime) contentType = mime;
      }
    }
    try {
      const binary = atob(String(b64));
      const len = binary.length;
      const bytes = new Uint8Array(len);
      for (let i = 0; i < len; i += 1) {
        bytes[i] = binary.charCodeAt(i);
      }
      return new Blob([bytes], { type: contentType });
    } catch {
      return null;
    }
  }

  async function handleGenerate() {
    setStatus("", false);
    setMeta("");
    clearPlayer();

    let body;
    try {
      body = buildRequest();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    generateEl.disabled = true;
    setStatus("Generating...", false);

    try {
      const resp = await fetch("/ui/api/tts", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const contentType = resp.headers.get("content-type") || "";

      if (!resp.ok) {
        const text = await resp.text();
        setStatus(`HTTP ${resp.status}: ${text}`, true);
        return;
      }

      let url = "";
      if (contentType.includes("application/json")) {
        const payload = await resp.json();
        if (payload?.audio_url) {
          url = String(payload.audio_url || "").trim();
        } else {
          const blob = decodeBase64Audio(payload);
          if (blob) {
            url = URL.createObjectURL(blob);
            activeObjectUrl = url;
          }
        }

        if (payload?._gateway) {
          setMeta(`Backend: ${payload._gateway.backend_class || payload._gateway.backend}`);
        }

        if (!url) {
          setStatus("No audio returned in JSON response.", true);
          setMeta(JSON.stringify(payload));
          return;
        }
      } else {
        const blob = await resp.blob();
        url = URL.createObjectURL(blob);
        activeObjectUrl = url;
      }

      setStatus("Audio ready.", false);
      renderAudio(url);
    } catch (e) {
      setStatus(String(e?.message || e), true);
    } finally {
      generateEl.disabled = false;
    }
  }

  async function loadUserSettings() {
    try {
      const resp = await fetch('/ui/api/user/settings', { method: 'GET', credentials: 'same-origin' });
      if (!resp.ok) return null;
      const payload = await resp.json();
      return payload && payload.settings ? payload.settings : null;
    } catch (e) {
      return null;
    }
  }

  async function saveUserSettings(settings) {
    try {
      const resp = await fetch('/ui/api/user/settings', {
        method: 'PUT',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings }),
      });
      return resp.ok;
    } catch (e) {
      return false;
    }
  }

  // Load available backends/voices, apply saved setting (server or localStorage), and bind UI handlers.
  (async () => {
    await loadBackends();
    await loadVoices();

    // Try server-side settings first; fall back to localStorage for unauthenticated users.
    const serverSettings = await loadUserSettings();
    if (serverSettings && serverSettings.tts && serverSettings.tts.backend_class && backendEl) {
      try { backendEl.value = serverSettings.tts.backend_class; } catch (e) {}
      await loadVoices();
    } else if (backendEl) {
      try {
        const savedBackend = localStorage.getItem('gw_ui_tts_backend');
        if (savedBackend) {
          backendEl.value = savedBackend;
          await loadVoices();
        }
      } catch (e) {}
    }
    if (serverSettings && serverSettings.tts && serverSettings.tts.voice && voiceEl) {
      try { voiceEl.value = serverSettings.tts.voice; } catch (e) {}
    } else {
      try {
        const saved = localStorage.getItem('gw_ui_tts_voice');
        if (saved && voiceEl) voiceEl.value = saved;
      } catch (e) {}
    }

    if (backendEl) {
      backendEl.addEventListener('change', async () => {
        const backendClass = String(backendEl.value || '').trim();
        await loadVoices();
        const ok = await saveUserSettings({ tts: { backend_class: backendClass } });
        if (!ok) {
          try { localStorage.setItem('gw_ui_tts_backend', backendClass); } catch (e) {}
        }
      });
    }

    if (voiceEl) {
      voiceEl.addEventListener('change', async () => {
        const val = String(voiceEl.value || '').trim();
        // Try to persist server-side; if that fails (401 or network), save to localStorage.
        const ok = await saveUserSettings({ tts: { voice: val } });
        if (!ok) {
          try { localStorage.setItem('gw_ui_tts_voice', val); } catch (e) {}
        }
      });
    }

    generateEl.addEventListener('click', handleGenerate);
  })();
})();
