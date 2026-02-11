(() => {
  const $ = (id) => document.getElementById(id);

  const modelEl = $("model");
  const loadModelsEl = $("loadModels");
  const inputEl = $("input");
  const sendEl = $("send");
  const clearEl = $("clear");
  const outEl = $("out");
  const metaEl = $("meta");

  function setOutput(text) {
    try {
      if (outEl) outEl.textContent = String(text || "");
    } catch (e) {}
  }

  function setMeta(text) {
    try {
      if (metaEl) metaEl.textContent = String(text || "");
    } catch (e) {}
  }

  (() => {
    const $ = (id) => document.getElementById(id);

    const chatEl = $("chat");
    const modelEl = $("model");
    const loadModelsEl = $("loadModels");
    const inputEl = $("input");
    const sendEl = $("send");
    const clearEl = $("clear");
    const clearChatEl = $("clearChat");
    const resetSessionEl = $("resetSession");
    const settingsBtn = $("settingsBtn");
    const attachBtn = $("attachBtn");
    const fileInput = $("fileInput");
    const attachmentsList = $("attachmentsList");
    const backendStatusPanel = $("backendStatusPanel");
    const backendStatusList = $("backendStatusList");
    const backendStatusUpdated = $("backendStatusUpdated");
    const backendStatusError = $("backendStatusError");
    const backendStatusRefresh = $("backendStatusRefresh");
    const backendStatusSpinner = $("backendStatusSpinner");

    /** @type {{role:'user'|'assistant'|'system', content:string}[]} */
    let history = [];

    const CONVO_KEY = "gw_ui2_conversation_id";

    let conversationId = "";
    let conversationResetting = false;
    let pendingAttachments = [];
    let modelOptionsCache = [];

    function handle401(resp) {
      if (resp && resp.status === 401) {
        const back = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/ui/login?next=${back}`;
        return true;
      }
      return false;
    }

    

    function escapeHtml(s) {
      return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function buildImageUiUrl({ prompt, image_request }) {
      const qs = new URLSearchParams();
      const p = typeof prompt === "string" ? prompt.trim() : "";
      if (p) qs.set("prompt", p);

      const req = image_request && typeof image_request === "object" ? image_request : {};
      const add = (k, v) => {
        if (v === undefined || v === null) return;
        const s = String(v).trim();
        if (!s) return;
        qs.set(k, s);
      };

      add("size", req.size);
      add("n", req.n);
      add("model", req.model);
      add("seed", req.seed);
      add("steps", req.steps);
      add("guidance_scale", req.guidance_scale);
      add("negative_prompt", req.negative_prompt);

      const q = qs.toString();
      return q ? `/ui/image?${q}` : "/ui/image";
    }

    function scrollToBottom() {
      if (!chatEl) return;
      chatEl.scrollTop = chatEl.scrollHeight;
    }

    function formatTimestamp(tsSeconds) {
      if (!Number.isFinite(tsSeconds)) return "--";
      try {
        return new Date(tsSeconds * 1000).toLocaleTimeString();
      } catch (e) {
        return "--";
      }
    }

    function renderBackendStatus(data) {
      if (!backendStatusList) return;
      backendStatusList.innerHTML = "";
      if (!data || !Array.isArray(data.backends)) {
        const empty = document.createElement("div");
        empty.className = "status-empty";
        empty.textContent = "No backend status available.";
        backendStatusList.appendChild(empty);
        return;
      }

      const backendGroups = [
        { title: "Core", backends: ["ollama", "local_mlx"] },
        { title: "Images", backends: ["gpu_fast", "gpu_heavy"] },
        { title: "TTS", backends: ["pocket_tts", "luxtts", "qwen3_tts"] },
        { title: "Music", backends: ["heartmula_music"] },
        { title: "OCR", backends: ["lighton_ocr"] },
        { title: "Video", backends: ["followyourcanvas", "skyreels_v2"] },
      ];

      const backendLabels = {
        gpu_fast: "SDXL-Turbo",
        gpu_heavy: "InvokeAI",
        lighton_ocr: "LightOnOCR",
        personaplex: "PersonaPlex",
        skyreels_v2: "SkyReels-V2",
      };

      const backendMap = new Map();
      data.backends.forEach((backend) => {
        backendMap.set(backend.backend_class, backend);
      });

      const used = new Set();

      const renderBackendRow = (backend, { displayName, missing } = {}) => {
        const row = document.createElement("div");
        row.className = "status-row";

        const header = document.createElement("div");
        header.className = "status-row-header";

        const name = document.createElement("div");
        name.className = "status-name";
        const resolvedName = displayName || backend.backend_class || "unknown";
        name.textContent = resolvedName;
        if (backend.backend_class && displayName && displayName !== backend.backend_class) {
          const alias = document.createElement("span");
          alias.className = "status-name-alias";
          alias.textContent = ` (${backend.backend_class})`;
          name.appendChild(alias);
        }
        header.appendChild(name);

        const badges = document.createElement("div");
        badges.className = "status-badges";

        if (missing) {
          row.classList.add("warn");
          const missingBadge = document.createElement("span");
          missingBadge.className = "status-badge warn";
          missingBadge.textContent = "Not configured";
          badges.appendChild(missingBadge);
        } else {
          const healthy = document.createElement("span");
          const isHealthy = backend.healthy === true;
          healthy.className = `status-badge ${isHealthy ? "ok" : backend.healthy === false ? "bad" : "warn"}`;
          healthy.textContent =
            backend.healthy === undefined ? "Health unknown" : isHealthy ? "Healthy" : "Unhealthy";
          badges.appendChild(healthy);

          const ready = document.createElement("span");
          const isReady = backend.ready === true;
          ready.className = `status-badge ${isReady ? "ok" : backend.ready === false ? "bad" : "warn"}`;
          ready.textContent = backend.ready === undefined ? "Readiness unknown" : isReady ? "Ready" : "Not ready";
          badges.appendChild(ready);
          if (backend.healthy === false && backend.ready === false) {
            row.classList.add("bad");
          } else if (isHealthy && isReady) {
            row.classList.add("ok");
          } else {
            row.classList.add("warn");
          }
        }

        header.appendChild(badges);
        row.appendChild(header);

        const detail = document.createElement("div");
        detail.className = "status-detail";
        if (missing) {
          detail.textContent = "Not configured in the backend registry.";
        } else {
          const capabilities = Array.isArray(backend.capabilities) ? backend.capabilities.join(", ") : "unknown";
          const lastCheck = backend.last_check ? formatTimestamp(backend.last_check) : "--";
          detail.textContent = `Capabilities: ${capabilities} • Last check: ${lastCheck}`;
        }
        row.appendChild(detail);

        const aliasEntries = Array.isArray(backend.aliases) ? backend.aliases : [];
        if (aliasEntries.length > 0) {
          const aliasDetail = document.createElement("div");
          aliasDetail.className = "status-aliases";
          const aliasText = aliasEntries
            .map((alias) => `${alias.name} → ${alias.target}`)
            .filter(Boolean)
            .join(", ");
          aliasDetail.textContent = `Aliases: ${aliasText}`;
          row.appendChild(aliasDetail);
        }

        if (backend.error) {
          const err = document.createElement("div");
          err.className = "status-error";
          err.textContent = backend.error;
          row.appendChild(err);
        }

        return row;
      };

      const renderGroup = (title, backendKeys) => {
        const group = document.createElement("div");
        group.className = "status-group";
        const heading = document.createElement("div");
        heading.className = "status-group-title";
        heading.textContent = title;
        group.appendChild(heading);

        const list = document.createElement("div");
        list.className = "status-group-list";
        backendKeys.forEach((backendKey) => {
          const backend = backendMap.get(backendKey);
          const displayName = backendLabels[backendKey] || backendKey;
          list.appendChild(renderBackendRow(backend || { backend_class: backendKey }, { displayName, missing: !backend }));
          if (backend) used.add(backendKey);
        });
        group.appendChild(list);
        backendStatusList.appendChild(group);
      };

      backendGroups.forEach((group) => {
        renderGroup(group.title, group.backends);
      });

      const extraBackends = data.backends
        .map((backend) => backend.backend_class)
        .filter((backendClass) => backendClass && !used.has(backendClass));
      if (extraBackends.length > 0) {
        renderGroup("Other", extraBackends);
      }
    }

    async function loadBackendStatus() {
      if (!backendStatusList) return;
      if (backendStatusRefresh) backendStatusRefresh.disabled = true;
      if (backendStatusSpinner) backendStatusSpinner.hidden = false;
      if (backendStatusError) backendStatusError.hidden = true;
      try {
        const resp = await fetch("/ui/api/backend_status", { credentials: "same-origin" });
        if (handle401(resp)) return;
        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(text || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        renderBackendStatus(data);
        if (backendStatusUpdated) {
          backendStatusUpdated.textContent = `Last updated: ${formatTimestamp(data.generated_at)}`;
        }
      } catch (e) {
        if (backendStatusError) {
          backendStatusError.textContent = `Failed to load status: ${e}`;
          backendStatusError.hidden = false;
        }
        if (backendStatusList) {
          backendStatusList.innerHTML = "";
          const empty = document.createElement("div");
          empty.className = "status-empty";
          empty.textContent = "Unable to load backend status.";
          backendStatusList.appendChild(empty);
        }
      } finally {
        if (backendStatusRefresh) backendStatusRefresh.disabled = false;
        if (backendStatusSpinner) backendStatusSpinner.hidden = true;
      }
    }

    let backendStatusInterval = null;
    function startBackendStatusPolling() {
      if (backendStatusInterval) return;
      loadBackendStatus();
      backendStatusInterval = window.setInterval(loadBackendStatus, 30000);
    }

    function stopBackendStatusPolling() {
      if (backendStatusInterval) {
        window.clearInterval(backendStatusInterval);
        backendStatusInterval = null;
      }
    }

    function addMessage({ role, content, meta, html, attachments }) {
      if (!chatEl) return { wrap: null, metaEl: null, contentEl: null };
      const wrap = document.createElement("div");
      wrap.className = `msg ${role}`;

      const metaEl = document.createElement("div");
      metaEl.className = "meta";
      metaEl.textContent = meta || (role === "user" ? "You" : role === "assistant" ? "Assistant" : "System");
      // Optionally show timestamp
      try {
        if (userSettings && userSettings.showTimestamps) {
          const ts = document.createElement('span');
          ts.className = 'ts';
          ts.style.marginLeft = '8px';
          ts.style.color = '#93a4ba';
          ts.style.fontSize = '11px';
          ts.textContent = new Date().toLocaleTimeString();
          metaEl.appendChild(ts);
        }
      } catch (e) {}

      const contentEl = document.createElement("div");
      contentEl.className = "content";
      if (html) {
        contentEl.innerHTML = html;
      } else {
        contentEl.textContent = content || "";
      }

      if (Array.isArray(attachments) && attachments.length > 0) {
        const list = document.createElement("div");
        list.className = "message-attachments";
        const label = document.createElement("div");
        label.className = "meta";
        label.textContent = "Attachments";
        list.appendChild(label);
        attachments.forEach((item) => {
          if (!item) return;
          const row = document.createElement("div");
          row.className = "attachment-row";
          const name = document.createElement("div");
          name.className = "attachment-name";
          const link = document.createElement("a");
          link.href = item.url || "#";
          link.textContent = item.filename || "attachment";
          link.target = "_blank";
          link.rel = "noopener";
          name.appendChild(link);
          const meta = document.createElement("div");
          meta.className = "attachment-meta";
          const bits = [];
          if (item.mime) bits.push(item.mime);
          if (Number.isFinite(item.bytes)) bits.push(`${item.bytes} bytes`);
          meta.textContent = bits.join(" • ");
          row.appendChild(name);
          row.appendChild(meta);
          list.appendChild(row);
        });
        contentEl.appendChild(list);
      }

      wrap.appendChild(metaEl);
      wrap.appendChild(contentEl);
      chatEl.appendChild(wrap);
      scrollToBottom();

      return { wrap, metaEl, contentEl };
    }

    function formatTime(seconds) {
      const total = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
      const mins = Math.floor(total / 60);
      const secs = Math.floor(total % 60);
      return `${mins}:${secs.toString().padStart(2, "0")}`;
    }

    function createAudioPlayer(url) {
      const wrap = document.createElement("div");
      wrap.className = "audio-card";

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

      // Remove seek slider for chat playback; only expose labeled volume.
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

      wrap.appendChild(audio);
      wrap.appendChild(controls);

      // Apply user-configured audio volume and autoplay
      try {
        audio.volume = typeof userSettings.audioVolume === 'number' ? Number(userSettings.audioVolume) : audio.volume;
        audio.autoplay = !!userSettings.autoPlayTTS;
      } catch (e) {}

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

      // initialize volume control from user settings if present
      try {
        if (typeof userSettings.audioVolume === 'number') {
          volume.value = String(Number(userSettings.audioVolume));
          audio.volume = Number(userSettings.audioVolume);
        }
      } catch (e) {}

      return wrap;
    }

    function renderStoredMessage(m) {
      if (!m || typeof m !== "object") return;
      const role = typeof m.role === "string" ? m.role : "assistant";
      const type = typeof m.type === "string" ? m.type : "";
      const content = typeof m.content === "string" ? m.content : "";
      const attachments = Array.isArray(m.attachments) ? m.attachments : [];

      if (type === "image" && typeof m.url === "string" && m.url.trim()) {
        const metaBits = [];
        if (m.backend) metaBits.push(`backend=${m.backend}`);
        if (m.model) metaBits.push(`model=${m.model}`);
        if (m.sha256) metaBits.push(`sha=${String(m.sha256).slice(0, 12)}`);

        const link = buildImageUiUrl({ prompt: m.prompt, image_request: m.image_request });

        addMessage({
          role: role === "user" ? "user" : "assistant",
          meta: metaBits.length ? `Image • ${metaBits.join(" • ")}` : "Image",
          html: `<img class="gen" src="${escapeHtml(m.url.trim())}" alt="generated" />\n<div style="margin-top:8px"><a href="${escapeHtml(link)}">Open in Image UI</a></div>`,
        });
        return;
      }

      const metaBits = [];
      if (m.backend) metaBits.push(`backend=${m.backend}`);
      if (m.model) metaBits.push(`model=${m.model}`);
      if (m.reason) metaBits.push(`reason=${m.reason}`);
      addMessage({ role, content, meta: metaBits.length ? metaBits.join(" • ") : undefined, attachments });
    }

    function clearChatUI() {
      if (!chatEl) return;
      chatEl.innerHTML = "";
    }

    async function resetSession() {
      // remove local conversation id, clear UI, and create a new conversation
      try {
        localStorage.removeItem(CONVO_KEY);
      } catch (e) {}
      clearChatUI();
      try {
        const resp = await fetch("/ui/api/conversations/new", { method: "POST", credentials: "same-origin" });
        if (resp.ok) {
          const payload = await resp.json();
          if (payload && typeof payload.conversation_id === 'string') {
            conversationId = payload.conversation_id;
            try { localStorage.setItem(CONVO_KEY, conversationId); } catch (e) {}
          }
        }
      } catch (e) {}
    }

    // User settings management
    let userSettings = { ttsVoice: "", ttsBackend: "", showTimestamps: false, audioVolume: 1.0, autoPlayTTS: false, systemPrompt: "", profileTone: "", preferredModel: "default" };

    async function loadUserSettings() {
      try {
        const resp = await fetch('/ui/api/user/settings', { method: 'GET', credentials: 'same-origin' });
        if (!resp.ok) return;
        const payload = await resp.json();
        const s = payload?.settings || {};
        userSettings = {
          ttsVoice: s.tts?.voice || s.tts_voice || s.ttsVoice || "",
          ttsBackend: s.tts?.backend_class || s.tts?.backend || s.tts_backend || s.ttsBackend || "",
          showTimestamps: !!(s.ui && s.ui.showTimestamps || s.showTimestamps),
          audioVolume: typeof s.audioVolume === 'number' ? s.audioVolume : (s.audio && typeof s.audio.volume === 'number' ? s.audio.volume : (s.audioVolume || 1.0)),
          autoPlayTTS: !!(s.audio && s.audio.autoPlayTTS || s.autoPlayTTS),
          systemPrompt: (s.profile && s.profile.system_prompt) || s.profile?.system_prompt || s.profile?.systemPrompt || "",
          profileTone: (s.profile && s.profile.tone) || s.profile?.tone || "",
          preferredModel: (s.chat && s.chat.model_preference) || s.model_preference || s.modelPreference || "default",
        };
        applyUserSettingsToUi();
      } catch (e) {
        console.error('loadUserSettings', e);
      }
    }

    function applyUserSettingsToUi() {
      // Nothing heavy for now; audio players read userSettings when created.
      // If timestamps are enabled, existing messages won't be backfilled.
    }

    function openSettings() {
      const modal = document.getElementById('settingsModal');
      if (!modal) return;
      // populate form
      (async () => {
        await loadUserSettings();
        if (!modelOptionsCache.length) {
          await loadModels();
        }
        const backendSelect = document.getElementById('settings_tts_backend');
        const select = document.getElementById('settings_tts_voice');
        const showTs = document.getElementById('settings_show_timestamps');
        const vol = document.getElementById('settings_audio_volume');
        const autoplay = document.getElementById('settings_autoplay_tts');
        const preferredModel = document.getElementById('settings_model_preference');
        // populate voice list if available from TTS voices endpoint
        try {
          if (backendSelect) {
            try {
              const b = await fetch('/ui/api/tts/backends', { method: 'GET', credentials: 'same-origin' });
              if (b.ok) {
                const payload = await b.json();
                const list = Array.isArray(payload?.available_backends) ? payload.available_backends : [];
                backendSelect.innerHTML = '<option value="">(default)</option>';
                for (const item of list) {
                  if (!item?.backend_class) continue;
                  const opt = document.createElement('option');
                  opt.value = item.backend_class;
                  opt.textContent = item.description ? `${item.backend_class} — ${item.description}` : String(item.backend_class);
                  backendSelect.appendChild(opt);
                }
              }
            } catch (e) {}
          }
          const backendClass = backendSelect ? String(backendSelect.value || userSettings.ttsBackend || '').trim() : String(userSettings.ttsBackend || '').trim();
          const qs = backendClass ? `?backend_class=${encodeURIComponent(backendClass)}` : '';
          const r = await fetch(`/ui/api/tts/voices${qs}`, { method: 'GET', credentials: 'same-origin' });
          if (r.ok) {
            const voices = await r.json();
            if (Array.isArray(voices) && select) {
              select.innerHTML = '<option value="">(default)</option>';
              for (const v of voices) {
                try {
                  const opt = document.createElement('option');
                  opt.value = String(v);
                  opt.textContent = String(v);
                  select.appendChild(opt);
                } catch (e) {}
              }
            }
          }
        } catch (e) {}

        if (backendSelect) {
          backendSelect.value = userSettings.ttsBackend || "";
          backendSelect.onchange = async () => {
            try {
              const backendClass = String(backendSelect.value || '').trim();
              const qs = backendClass ? `?backend_class=${encodeURIComponent(backendClass)}` : '';
              const r = await fetch(`/ui/api/tts/voices${qs}`, { method: 'GET', credentials: 'same-origin' });
              if (r.ok) {
                const voices = await r.json();
                if (Array.isArray(voices) && select) {
                  select.innerHTML = '<option value="">(default)</option>';
                  for (const v of voices) {
                    try {
                      const opt = document.createElement('option');
                      opt.value = String(v);
                      opt.textContent = String(v);
                      select.appendChild(opt);
                    } catch (e) {}
                  }
                }
              }
            } catch (e) {}
          };
        }
        if (select) select.value = userSettings.ttsVoice || "";
        if (showTs) showTs.checked = !!userSettings.showTimestamps;
        if (vol) vol.value = String(Number(userSettings.audioVolume || 1));
        if (autoplay) autoplay.checked = !!userSettings.autoPlayTTS;
        if (preferredModel) {
          syncSettingsModelSelect(userSettings.preferredModel || "default");
        }
        // populate profile fields
        try {
          const sys = document.getElementById('settings_system_prompt');
          const tone = document.getElementById('settings_profile_tone');
          if (sys) sys.value = userSettings.systemPrompt || "";
          if (tone) tone.value = userSettings.profileTone || "";
        } catch (e) {}

        // Show password controls only when user auth is enabled and the user
        // is authenticated. Call /ui/api/auth/me and hide the password fieldset
        // when unauthenticated or the endpoint returns 401/403.
        try {
          const pwField = document.getElementById('settings_password_fieldset');
          let showPw = false;
          try {
            const r = await fetch('/ui/api/auth/me', { method: 'GET', credentials: 'same-origin' });
            if (r.ok) {
              const j = await r.json();
              showPw = !!j && !!j.authenticated;
            } else {
              // If the route returns 401/403, treat as not authenticated
              showPw = false;
            }
          } catch (e) {
            showPw = false;
          }
          if (pwField) pwField.style.display = showPw ? 'block' : 'none';
        } catch (e) {}

        modal.setAttribute('aria-hidden', 'false');
        const close = document.getElementById('settingsClose');
        if (close) close.focus();
      })();
    }

    function closeSettings() {
      const modal = document.getElementById('settingsModal');
      if (!modal) return;
      modal.setAttribute('aria-hidden', 'true');
    }

    async function saveSettingsFromModal() {
      const backendSelect = document.getElementById('settings_tts_backend');
      const select = document.getElementById('settings_tts_voice');
      const showTs = document.getElementById('settings_show_timestamps');
      const vol = document.getElementById('settings_audio_volume');
      const autoplay = document.getElementById('settings_autoplay_tts');
      const preferredModel = document.getElementById('settings_model_preference');
      const sys = document.getElementById('settings_system_prompt');
      const tone = document.getElementById('settings_profile_tone');
      const curPwd = document.getElementById('settings_current_password');
      const newPwd = document.getElementById('settings_new_password');
      const confirmPwd = document.getElementById('settings_confirm_password');
      const chosenModel = normalizePreferredModel(preferredModel ? String(preferredModel.value || "").trim() : "default");
      const newSettings = {
        tts: { voice: select ? select.value : "", backend_class: backendSelect ? backendSelect.value : "" },
        ui: { showTimestamps: !!(showTs && showTs.checked) },
        audioVolume: vol ? Number(vol.value) : 1.0,
        autoPlayTTS: !!(autoplay && autoplay.checked),
        profile: { system_prompt: sys ? String(sys.value || '') : '', tone: tone ? String(tone.value || '') : '' },
        chat: { model_preference: chosenModel || "default" },
      };
      try {
        // If user provided password fields, attempt password change first.
        try {
          const cur = curPwd ? String(curPwd.value || '') : '';
          const nw = newPwd ? String(newPwd.value || '') : '';
          const conf = confirmPwd ? String(confirmPwd.value || '') : '';
          if (cur || nw || conf) {
            if (!cur) { alert('Current password is required to change password'); return; }
            if (!nw) { alert('New password is required'); return; }
            if (nw !== conf) { alert('New password and confirmation do not match'); return; }

            const pwResp = await fetch('/ui/api/user/password', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ current: cur, new: nw }) });
            if (!pwResp.ok) {
              const txt = await pwResp.text();
              alert('Failed to change password: ' + (txt || pwResp.status));
              return;
            }
            alert('Password changed');
          }
        } catch (e) {
          console.error('change password', e);
          alert('Failed to change password');
          return;
        }
        const put = await fetch('/ui/api/user/settings', { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ settings: newSettings }) });
        if (!put.ok) {
          alert('Failed to save settings');
          return;
        }
        // update local copy
        userSettings.ttsVoice = newSettings.tts.voice || "";
        userSettings.ttsBackend = newSettings.tts.backend_class || "";
        userSettings.showTimestamps = !!newSettings.ui.showTimestamps;
        userSettings.audioVolume = Number(newSettings.audioVolume || 1.0);
        userSettings.autoPlayTTS = !!newSettings.autoPlayTTS;
        userSettings.systemPrompt = newSettings.profile?.system_prompt || "";
        userSettings.profileTone = newSettings.profile?.tone || "";
        userSettings.preferredModel = newSettings.chat?.model_preference || "default";
        syncSettingsModelSelect(userSettings.preferredModel);
        applyUserSettingsToUi();
        closeSettings();
      } catch (e) {
        console.error('saveSettings', e);
        alert('Failed to save settings');
      }
    }

    function setBusy(busy) {
      if (sendEl) sendEl.disabled = busy;
      if (inputEl) inputEl.disabled = busy;
      if (modelEl) modelEl.disabled = busy;
      if (loadModelsEl) loadModelsEl.disabled = busy;
    }

    // Progress utilities for inline generation (simulated incremental progress)
    function _createProgressEl() {
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

    function _startProgress(inner, txt) {
      inner.classList.add('indeterminate');
      txt.textContent = 'Processing...';
      return () => {
        inner.classList.remove('indeterminate');
        txt.textContent = '';
      };
    }

    function normalizeModelIds(modelIds) {
      const ids = Array.isArray(modelIds) ? modelIds.filter((x) => typeof x === "string" && x.trim()) : [];
      const unique = Array.from(new Set(ids.map((x) => x.trim())));
      if (!unique.includes("default")) unique.push("default");
      unique.sort((a, b) => {
        if (a === "default") return -1;
        if (b === "default") return 1;
        return a.localeCompare(b);
      });
      return unique;
    }

    function populateModelSelect(selectEl, options) {
      if (!selectEl) return;
      selectEl.innerHTML = "";
      for (const id of options) {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = id;
        selectEl.appendChild(opt);
      }
    }

    function pickModelValue({ options, preferred, fallback }) {
      const want = (preferred || "").trim();
      if (want && options.includes(want)) return want;
      if (fallback && options.includes(fallback)) return fallback;
      if (options.includes("default")) return "default";
      return options[0] || "default";
    }

    function syncSettingsModelSelect(preferred) {
      const select = document.getElementById("settings_model_preference");
      if (!select) return;
      populateModelSelect(select, modelOptionsCache);
      const desired = pickModelValue({ options: modelOptionsCache, preferred });
      select.value = desired;
    }

    function normalizePreferredModel(preferred) {
      const desired = (preferred || "").trim();
      if (!modelOptionsCache.length) return desired || "default";
      return modelOptionsCache.includes(desired) ? desired : "default";
    }

    function _setModelOptions(modelIds, preferred) {
      const prev = modelEl ? modelEl.value : "";
      const options = normalizeModelIds(modelIds);
      modelOptionsCache = options;
      populateModelSelect(modelEl, options);
      const desired = pickModelValue({ options, preferred, fallback: prev });
      if (modelEl) modelEl.value = desired;
      syncSettingsModelSelect(preferred);
    }

    async function loadModels() {
      try {
        const resp = await fetch("/ui/api/models", { method: "GET", credentials: "same-origin" });
        const text = await resp.text();
        if (handle401(resp)) return;
        if (!resp.ok) {
          _setModelOptions(["default"], userSettings.preferredModel || "default");
          addMessage({ role: "system", content: text, meta: `Models HTTP ${resp.status}` });
          return;
        }

        let payload;
        try {
          payload = JSON.parse(text);
        } catch {
          _setModelOptions(["default"], userSettings.preferredModel || "default");
          addMessage({ role: "system", content: "Models: invalid JSON" });
          return;
        }

        const data = payload && Array.isArray(payload.data) ? payload.data : [];
        const ids = data.map((m) => m && m.id).filter((x) => typeof x === "string");
        _setModelOptions(ids, userSettings.preferredModel || "default");
      } catch (e) {
        _setModelOptions(["default"], userSettings.preferredModel || "default");
        addMessage({ role: "system", content: `Models error: ${String(e)}` });
      }
    }

    async function ensureConversation() {
      const fromStorage = (localStorage.getItem(CONVO_KEY) || "").trim();
      if (fromStorage) {
        conversationId = fromStorage;
        return;
      }

      const resp = await fetch("/ui/api/conversations/new", { method: "POST", credentials: "same-origin" });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        addMessage({ role: "system", content: text, meta: `Conversation HTTP ${resp.status}` });
        return;
      }
      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        addMessage({ role: "system", content: "Conversation: invalid JSON" });
        return;
      }
      const cid = payload && typeof payload.conversation_id === "string" ? payload.conversation_id.trim() : "";
      if (!cid) {
        addMessage({ role: "system", content: "Conversation: missing id" });
        return;
      }
      conversationId = cid;
      localStorage.setItem(CONVO_KEY, cid);
    }

    async function resetConversationId(reason) {
      if (conversationResetting) return;
      conversationResetting = true;
      try {
        localStorage.removeItem(CONVO_KEY);
        conversationId = "";
        if (reason) {
          addMessage({ role: "system", content: reason });
        }
        await ensureConversation();
      } finally {
        conversationResetting = false;
      }
    }

    async function loadConversation() {
      if (!conversationId) return;
      const resp = await fetch(`/ui/api/conversations/${encodeURIComponent(conversationId)}`, { method: "GET", credentials: "same-origin" });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        if (resp.status === 404) {
          await resetConversationId("Conversation expired or missing. Starting a new one.");
          return;
        }
        addMessage({ role: "system", content: text, meta: `Load convo HTTP ${resp.status}` });
        return;
      }
      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        addMessage({ role: "system", content: text, meta: "Load convo OK (non-JSON)" });
        return;
      }

      const msgs = payload && Array.isArray(payload.messages) ? payload.messages : [];
      for (const m of msgs) {
        renderStoredMessage(m);
      }
    }

    async function appendToConversation(message) {
      if (!conversationId) return;
      await fetch(`/ui/api/conversations/${encodeURIComponent(conversationId)}/append`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
    }

    

    async function sendChatMessage(userText) {
      const model = (modelEl.value || "").trim() || "default";

      // Intercept explicit slash-commands here as a safety net so any caller
      // of sendChatMessage will route commands to the correct backend.
      const lower = String(userText || "").trim().toLowerCase();
      if (lower === '/image' || lower.startsWith('/image ')) {
        const prompt = String(userText || "").replace(/^\/image\s*/i, '').trim();
        await generateImage(prompt || '', {});
        return;
      }
      if (lower === '/clone' || lower.startsWith('/clone ')) {
        try { window.open('/ui/voice-clone', '_blank', 'noopener'); } catch (e) {}
        addMessage({ role: 'system', content: 'Opened Voice Clone UI in a new tab.' });
        return;
      }
      if (lower === '/scan' || lower.startsWith('/scan ')) {
        const image_url = String(userText || "").replace(/^\/scan\s*/i, '').trim();
        await generateScan(image_url || '');
        return;
      }
      if (lower === '/speech' || lower.startsWith('/speech ') || lower.startsWith('/tts ')) {
        const prompt = String(userText || "").replace(/^\/(speech|tts)\s*/i, '').trim();
        await generateSpeech(prompt || '');
        return;
      }

      history.push({ role: "user", content: userText, attachments: pendingAttachments });
      addMessage({ role: "user", content: userText, attachments: pendingAttachments });

      const assistant = addMessage({ role: "assistant", content: "", meta: "Assistant" });
      assistant.contentEl.textContent = "";
      const thinkingLine = document.createElement("div");
      thinkingLine.className = "thinking-line";
      thinkingLine.style.display = "none";
      const contentText = document.createElement("div");
      contentText.className = "content-text";
      assistant.contentEl.appendChild(thinkingLine);
      assistant.contentEl.appendChild(contentText);

      setBusy(true);

      try {
        const sendRequest = () =>
          fetch("/ui/api/chat_stream", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model, conversation_id: conversationId, message: userText, attachments: pendingAttachments }),
          });

        let resp = await sendRequest();
        if (resp.status === 404 && conversationId) {
          await resetConversationId("Conversation expired or missing. Retrying with a new one.");
          resp = await sendRequest();
        }

        if (handle401(resp)) return;

        const backend = resp.headers.get("x-backend-used") || "";
        const usedModel = resp.headers.get("x-model-used") || "";
        const reason = resp.headers.get("x-router-reason") || "";
        let hasContent = false;
        let thinkingShown = false;
        let thinkingBuffer = "";
        let isOllama = backend === "ollama";

        if (!resp.ok) {
          const text = await resp.text();
          contentText.textContent = text;
          assistant.metaEl.textContent = `HTTP ${resp.status}`;
          return;
        }

        const setThinking = (text) => {
          if (!text) {
            thinkingLine.textContent = "";
            thinkingLine.style.display = "none";
            return;
          }
          thinkingLine.textContent = text;
          thinkingLine.style.display = "block";
          scrollToBottom();
        };

        const showThinking = () => {
          if (hasContent || thinkingShown || !isOllama) return;
          setThinking("Thinking…");
          thinkingShown = true;
        };

        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buf = "";
        let full = "";

        function updateMeta(extra) {
          const bits = [];
          if (backend) bits.push(`backend=${backend}`);
          if (usedModel) bits.push(`model=${usedModel}`);
          if (reason) bits.push(`reason=${reason}`);
          if (extra) bits.push(extra);
          assistant.metaEl.textContent = bits.length ? bits.join(" • ") : "Assistant";
        }

        updateMeta("streaming");
        showThinking();

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });

          while (true) {
            const idx = buf.indexOf("\n\n");
            if (idx < 0) break;
            const rawEvent = buf.slice(0, idx);
            buf = buf.slice(idx + 2);

            const lines = rawEvent.split("\n");
            for (const line of lines) {
              const trimmed = line.trim();
              if (!trimmed.startsWith("data:")) continue;
              const data = trimmed.slice(5).trim();
              if (data === "[DONE]") {
                updateMeta("done");
                continue;
              }

              let evt;
              try {
                evt = JSON.parse(data);
              } catch {
                continue;
              }

              if (!evt || typeof evt !== "object") continue;

              if (evt.type === "route") {
                const bits = [];
                if (evt.backend) bits.push(`backend=${evt.backend}`);
                if (evt.model) bits.push(`model=${evt.model}`);
                if (evt.reason) bits.push(`reason=${evt.reason}`);
                assistant.metaEl.textContent = bits.join(" • ") || assistant.metaEl.textContent;
                if (evt.backend) {
                  isOllama = evt.backend === "ollama";
                  showThinking();
                }
                continue;
              }

              if (evt.type === "audio") {
                const url = String(evt.url || "").trim();
                if (url) {
                  const player = createAudioPlayer(url);
                  assistant.contentEl.innerHTML = "";
                  assistant.contentEl.appendChild(player);
                  hasContent = true;
                  if (thinkingShown) setThinking("");
                }
                continue;
              }

              if (evt.type === "thinking" && typeof evt.thinking === "string") {
                thinkingBuffer += evt.thinking;
                setThinking(`Thinking: ${thinkingBuffer}`);
                thinkingShown = true;
                continue;
              }

              if (evt.type === "delta" && typeof evt.delta === "string") {
                if (!hasContent) {
                  hasContent = true;
                  if (thinkingShown) setThinking("");
                }
                full += evt.delta;
                contentText.textContent = full;
                scrollToBottom();
                continue;
              }

              if (evt.type === "error") {
                contentText.textContent = `${full}\n\n[error]\n${JSON.stringify(evt.error || evt, null, 2)}`;
                updateMeta("error");
                continue;
              }

              if (evt.type === "done") {
                if (!hasContent && thinkingShown) {
                  setThinking("");
                }
                updateMeta("done");
                continue;
              }
            }
          }
        }

        history.push({ role: "assistant", content: full });
      } catch (e) {
        addMessage({ role: "system", content: String(e) });
      } finally {
        setBusy(false);
        pendingAttachments = [];
        renderPendingAttachments();
      }
    }

    async function generateMusic(body) {
      try {
        // Show assistant placeholder with thinking while music is generated
        const assistant = addMessage({ role: "assistant", content: "", meta: "Assistant" });
        assistant.contentEl.textContent = "";
        const thinkingLine = document.createElement("div");
        thinkingLine.className = "thinking-line";
        thinkingLine.textContent = "Generating music…";
        assistant.contentEl.appendChild(thinkingLine);

        const resp = await fetch("/ui/api/music", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const text = await resp.text();
        if (handle401(resp)) return;
        if (!resp.ok) {
          assistant.contentEl.textContent = text;
          assistant.metaEl.textContent = `Music HTTP ${resp.status}`;
          return;
        }
        let payload;
        try {
          payload = JSON.parse(text);
        } catch {
          assistant.contentEl.textContent = text;
          return;
        }

        // If the backend provided an audio URL, show inline audio player.
        const url = payload?.audio_url || (payload?.data && payload.data[0] && payload.data[0].url);
        if (typeof url === "string" && url.trim()) {
          const player = createAudioPlayer(url.trim());
          assistant.contentEl.innerHTML = "";
          assistant.contentEl.appendChild(player);
          return;
        }

        assistant.contentEl.textContent = `Music response: ${JSON.stringify(payload)}`;
      } catch (e) {
        addMessage({ role: "system", content: String(e) });
      }
    }

    async function generateScan(image_url) {
      if (!image_url) return;
      try {
        const assistant = addMessage({ role: "assistant", content: "", meta: "Assistant" });
        assistant.contentEl.textContent = "";
        const thinkingLine = document.createElement("div");
        thinkingLine.className = "thinking-line";
        thinkingLine.textContent = "Scanning image…";
        assistant.contentEl.appendChild(thinkingLine);

        const resp = await fetch("/ui/api/scan", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ image_url }),
        });
        const text = await resp.text();
        if (handle401(resp)) return;
        if (!resp.ok) {
          assistant.contentEl.textContent = text;
          assistant.metaEl.textContent = `Scan HTTP ${resp.status}`;
          return;
        }
        let payload;
        try {
          payload = JSON.parse(text);
        } catch {
          assistant.contentEl.textContent = text;
          return;
        }

        let ocrText = "";
        if (typeof payload === 'object' && payload !== null) {
          if (typeof payload.text === 'string' && payload.text.trim()) {
            ocrText = payload.text.trim();
          } else if (Array.isArray(payload.data) && payload.data.length) {
            const parts = [];
            for (const item of payload.data) {
              if (item && typeof item === 'object') {
                if (typeof item.text === 'string' && item.text.trim()) parts.push(item.text.trim());
                else if (Array.isArray(item.lines)) {
                  for (const ln of item.lines) if (ln && typeof ln.text === 'string') parts.push(ln.text);
                } else {
                  parts.push(JSON.stringify(item));
                }
              }
            }
            ocrText = parts.join('\n');
          } else {
            ocrText = JSON.stringify(payload, null, 2);
          }
        } else {
          ocrText = String(payload || '');
        }

        assistant.contentEl.textContent = ocrText;
      } catch (e) {
        addMessage({ role: "system", content: String(e) });
      }
    }

    async function generateImage(prompt, image_request) {
      const body = { prompt, ...image_request };
      try {
        // Show assistant placeholder with thinking while image is generated
        const assistant = addMessage({ role: "assistant", content: "", meta: "Assistant" });
        assistant.contentEl.textContent = "";
        const thinkingLine = document.createElement("div");
        thinkingLine.className = "thinking-line";
        thinkingLine.textContent = "Generating image…";
        assistant.contentEl.appendChild(thinkingLine);

        const resp = await fetch("/ui/api/image", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const text = await resp.text();
        if (handle401(resp)) return;
        if (!resp.ok) {
          assistant.contentEl.textContent = text;
          assistant.metaEl.textContent = `Image HTTP ${resp.status}`;
          return;
        }
        let payload;
        try {
          payload = JSON.parse(text);
        } catch {
          assistant.contentEl.textContent = text;
          assistant.metaEl.textContent = "Image OK (non-JSON)";
          return;
        }
        const b64 = payload?.data?.[0]?.b64_json;
        const url = payload?.data?.[0]?.url;
        if (typeof url === "string" && url.trim()) {
          assistant.contentEl.innerHTML = `<img class="gen" src="${escapeHtml(url.trim())}" alt="generated" />`;
          return;
        }
        if (typeof b64 === "string" && b64.trim()) {
          const src = b64.trim().startsWith("data:") ? b64.trim() : `data:${payload?._gateway?.mime||'image/png'};base64,${b64.trim()}`;
          assistant.contentEl.innerHTML = `<img class="gen" src="${escapeHtml(src)}" alt="generated" />`;
          return;
        }
        assistant.contentEl.textContent = JSON.stringify(payload, null, 2);
      } catch (e) {
        addMessage({ role: "system", content: String(e) });
      }
    }

    async function generateSpeech(prompt) {
      if (!prompt) return;
      try {
        const assistant = addMessage({ role: "assistant", content: "", meta: "Assistant" });
        assistant.contentEl.textContent = "";
        const thinkingLine = document.createElement("div");
        thinkingLine.className = "thinking-line";
        thinkingLine.textContent = "Synthesizing speech…";
        assistant.contentEl.appendChild(thinkingLine);

        const ttsBody = { text: prompt };
        try { if (userSettings && userSettings.ttsVoice) ttsBody.voice = String(userSettings.ttsVoice); } catch (e) {}
        try { if (userSettings && userSettings.ttsBackend) ttsBody.backend_class = String(userSettings.ttsBackend); } catch (e) {}
        const resp = await fetch("/ui/api/tts", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(ttsBody),
        });

        if (handle401(resp)) return;
        if (!resp.ok) {
          const txt = await resp.text();
          assistant.contentEl.textContent = `TTS HTTP ${resp.status}: ${txt}`;
          return;
        }

        // Prefer server-provided cached UI URL when available.
        const gatewayUrl = resp.headers.get("x-gateway-tts-url") || resp.headers.get("X-Gateway-TTS-URL");
        if (gatewayUrl) {
          const player = createAudioPlayer(String(gatewayUrl).trim());
          assistant.contentEl.innerHTML = "";
          assistant.contentEl.appendChild(player);
          return;
        }

        const contentType = resp.headers.get("content-type") || "";
        let url = "";
        if (contentType.includes("application/json")) {
          const payload = await resp.json();
          if (payload?.audio_url) {
            url = String(payload.audio_url || "").trim();
          } else {
            const raw = payload?.audio_base64 || payload?.audio || payload?.audio_data;
            if (raw) {
              let b64 = String(raw || "");
              if (b64.startsWith("data:")) {
                url = b64;
              } else {
                const binary = atob(b64);
                const len = binary.length;
                const bytes = new Uint8Array(len);
                for (let i = 0; i < len; i += 1) bytes[i] = binary.charCodeAt(i);
                const blob = new Blob([bytes], { type: payload?.content_type || "audio/wav" });
                url = URL.createObjectURL(blob);
              }
            }
          }
        } else {
          const blob = await resp.blob();
          url = URL.createObjectURL(blob);
        }

        if (url) {
          const player = createAudioPlayer(url);
          assistant.contentEl.innerHTML = "";
          assistant.contentEl.appendChild(player);
          return;
        }

        assistant.contentEl.textContent = "No audio returned from TTS.";
      } catch (e) {
        addMessage({ role: "system", content: String(e) });
      }
    }

    document.addEventListener('DOMContentLoaded', () => {
      if (!chatEl) return;
      void loadModels();
      (async () => { await loadUserSettings(); await ensureConversation(); await loadConversation(); })();
      async function handleSendClick() {
        const text = (inputEl.value || '').trim();
        if (!text && pendingAttachments.length === 0) return;
        inputEl.value = '';

        // Explicit command routing takes precedence.
        const lower = text.trim().toLowerCase();
        if (lower === '/image' || lower.startsWith('/image ')) {
          const prompt = text.replace(/^\/image\s*/i, '').trim();
          await generateImage(prompt || '', {});
          return;
        }
        if (lower === '/music' || lower.startsWith('/music ')) {
          const body = { style: text.replace(/^\/music\s*/i, '').trim() };
          await generateMusic(body);
          return;
        }
        if (lower === '/clone' || lower.startsWith('/clone ')) {
          try { window.open('/ui/voice-clone', '_blank', 'noopener'); } catch (e) {}
          addMessage({ role: 'system', content: 'Opened Voice Clone UI in a new tab.' });
          return;
        }
        if (lower === '/speech' || lower.startsWith('/speech ') || lower.startsWith('/tts ')) {
          const prompt = text.replace(/^\/(speech|tts)\s*/i, '').trim();
          await generateSpeech(prompt || '');
          return;
        }

        await sendChatMessage(text);
      }

      if (sendEl) sendEl.addEventListener('click', () => void handleSendClick());
      if (clearChatEl) clearChatEl.addEventListener('click', () => clearChatUI());
      if (resetSessionEl) resetSessionEl.addEventListener('click', () => resetSession());
      if (settingsBtn) settingsBtn.addEventListener('click', () => openSettings());
      // modal controls
      const settingsCancel = document.getElementById('settings_cancel');
      const settingsSave = document.getElementById('settings_save');
      const settingsClose = document.getElementById('settingsClose');
      if (settingsCancel) settingsCancel.addEventListener('click', () => closeSettings());
      if (settingsClose) settingsClose.addEventListener('click', () => closeSettings());
      if (settingsSave) settingsSave.addEventListener('click', () => saveSettingsFromModal());
      if (backendStatusPanel) {
        backendStatusPanel.addEventListener('toggle', () => {
          if (backendStatusPanel.open) {
            startBackendStatusPolling();
          } else {
            stopBackendStatusPolling();
          }
        });
        if (backendStatusPanel.open) {
          startBackendStatusPolling();
        }
      }
      if (backendStatusRefresh) {
        backendStatusRefresh.addEventListener('click', () => {
          void loadBackendStatus();
        });
      }
      function setActiveSettingsSection(sectionId) {
        const menuButtons = document.querySelectorAll('.settings-menu button');
        const sections = document.querySelectorAll('.settings-section');
        menuButtons.forEach((btn) => {
          btn.classList.toggle('active', btn.dataset.section === sectionId);
        });
        sections.forEach((section) => {
          section.classList.toggle('active', section.id === sectionId);
        });
      }
      document.querySelectorAll('.settings-menu button').forEach((btn) => {
        btn.addEventListener('click', () => {
          const target = btn.dataset.section;
          if (target) setActiveSettingsSection(target);
        });
      });
      // Apps menu: toggle and admin-only link visibility
      const appsBtnEl = document.getElementById('appsBtn');
      const appsMenuEl = document.getElementById('appsMenu');
      const adminUiLinkEl = document.getElementById('adminUiLink');
      if (appsBtnEl && appsMenuEl) {
        appsBtnEl.addEventListener('click', (e) => {
          e.stopPropagation();
          const expanded = appsBtnEl.getAttribute('aria-expanded') === 'true';
          const willExpand = !expanded;
          appsBtnEl.setAttribute('aria-expanded', willExpand ? 'true' : 'false');
          appsMenuEl.setAttribute('aria-hidden', willExpand ? 'false' : 'true');
          try { appsMenuEl.hidden = !willExpand; } catch (e) {}
        });
        // close when clicking elsewhere
        document.addEventListener('click', (ev) => {
          try {
            if (!appsMenuEl.contains(ev.target) && ev.target !== appsBtnEl) {
              appsBtnEl.setAttribute('aria-expanded', 'false');
              appsMenuEl.setAttribute('aria-hidden', 'true');
              try { appsMenuEl.hidden = true; } catch (e) {}
            }
          } catch (e) {}
        });
        // allow clicks inside menu without closing
        appsMenuEl.addEventListener('click', (ev) => ev.stopPropagation());
      }
      // Show admin link when the current user is admin (call /ui/api/auth/me)
      (async () => {
        try {
          const r = await fetch('/ui/api/auth/me', { method: 'GET', credentials: 'same-origin' });
          if (!r.ok) return;
          const j = await r.json();
          if (j && j.authenticated && j.user && j.user.admin) {
            try { if (adminUiLinkEl) adminUiLinkEl.style.display = 'block'; } catch (e) {}
          }
        } catch (e) {}
      })();
      if (inputEl) {
        inputEl.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            void handleSendClick();
          }
        });
      }
      if (clearEl) clearEl.addEventListener('click', () => { if (inputEl) inputEl.value = ''; });
      function formatBytes(bytes) {
        if (!Number.isFinite(bytes)) return "";
        if (bytes < 1024) return `${bytes} B`;
        const kb = bytes / 1024;
        if (kb < 1024) return `${kb.toFixed(1)} KB`;
        const mb = kb / 1024;
        return `${mb.toFixed(1)} MB`;
      }
      function renderPendingAttachments() {
        if (!attachmentsList) return;
        attachmentsList.innerHTML = "";
        if (!pendingAttachments.length) {
          attachmentsList.hidden = true;
          return;
        }
        attachmentsList.hidden = false;
        pendingAttachments.forEach((item, idx) => {
          const row = document.createElement("div");
          row.className = "attachment-row";
          const name = document.createElement("div");
          name.className = "attachment-name";
          name.textContent = item.filename || "attachment";
          const meta = document.createElement("div");
          meta.className = "attachment-meta";
          const parts = [];
          if (item.mime) parts.push(item.mime);
          if (Number.isFinite(item.bytes)) parts.push(formatBytes(item.bytes));
          meta.textContent = parts.join(" • ");
          const removeBtn = document.createElement("button");
          removeBtn.type = "button";
          removeBtn.textContent = "Remove";
          removeBtn.addEventListener("click", () => {
            pendingAttachments.splice(idx, 1);
            renderPendingAttachments();
          });
          row.appendChild(name);
          row.appendChild(meta);
          row.appendChild(removeBtn);
          attachmentsList.appendChild(row);
        });
      }
      async function uploadAttachments(files) {
        if (!files || !files.length) return;
        try {
          if (!conversationId) {
            await ensureConversation();
          }
          const form = new FormData();
          for (const file of files) {
            form.append("files", file, file.name);
          }
          const resp = await fetch(`/ui/api/conversations/${encodeURIComponent(conversationId)}/files`, {
            method: "POST",
            credentials: "same-origin",
            body: form,
          });
          if (handle401(resp)) return;
          if (!resp.ok) {
            const text = await resp.text();
            addMessage({ role: "system", content: `Upload failed: ${text || resp.status}` });
            return;
          }
          const payload = await resp.json();
          const uploaded = Array.isArray(payload?.files) ? payload.files : [];
          uploaded.forEach((item) => {
            if (item && item.url && item.filename) {
              pendingAttachments.push(item);
            }
          });
          renderPendingAttachments();
        } catch (e) {
          addMessage({ role: "system", content: `Upload failed: ${String(e)}` });
        }
      }
      if (attachBtn && fileInput) {
        attachBtn.addEventListener("click", () => fileInput.click());
        fileInput.addEventListener("change", () => {
          const files = Array.from(fileInput.files || []);
          fileInput.value = "";
          void uploadAttachments(files);
        });
      }
    });
  })();
  setOutput("Ready.");
  setMeta("Ctrl+Enter to send");
  void loadModels();
})();
