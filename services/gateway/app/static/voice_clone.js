(() => {
  const $ = (id) => document.getElementById(id);

  const promptAudioEl = $("promptAudio");
  const voiceNameEl = $("voiceName");
  const sampleTextEl = $("sampleText");
  const recordAudioEl = $("recordAudio");
  const stopRecordingEl = $("stopRecording");
  const clearRecordingEl = $("clearRecording");
  const uploadHintEl = $("uploadHint");
  const saveVoiceEl = $("saveVoice");
  const refreshVoicesEl = $("refreshVoices");
  const voiceListEl = $("voiceList");
  const recordStatusEl = $("recordStatus");
  const recordPlayerEl = $("recordPlayer");
  const statusEl = $("status");
  const metaEl = $("meta");

  let recordObjectUrl = null;
  let recordedBlob = null;
  let recorder = null;
  let recordingStream = null;
  let libraryMaxBytes = 0;

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  function setMeta(text) {
    metaEl.textContent = text || "";
  }

  function setRecordStatus(text) {
    if (recordStatusEl) recordStatusEl.textContent = text || "";
  }

  async function getMicPermissionState() {
    try {
      if (!navigator?.permissions?.query) return "unknown";
      const status = await navigator.permissions.query({ name: "microphone" });
      return String(status?.state || "unknown");
    } catch (e) {
      return "unknown";
    }
  }

  async function diagnoseMicReadiness() {
    const reasons = [];

    if (!window.isSecureContext) {
      reasons.push("This page is not in a secure context (HTTPS or localhost is required).");
    }

    if (!navigator?.mediaDevices) {
      reasons.push("Browser API missing: navigator.mediaDevices is unavailable.");
    }
    if (!navigator?.mediaDevices?.getUserMedia) {
      reasons.push("Browser API missing: mediaDevices.getUserMedia is unavailable.");
    }
    if (typeof MediaRecorder === "undefined") {
      reasons.push("Browser API missing: MediaRecorder is unavailable.");
    }

    const permissionState = await getMicPermissionState();
    if (permissionState === "denied") {
      reasons.push("Microphone permission is denied for this site in browser settings.");
    }

    return { reasons, permissionState };
  }

  function browserMicPermissionHelp() {
    const host = window.location.host;
    return `Enable microphone permission for ${host} in browser site settings (lock icon near the address bar), then reload this page.`;
  }

  function browserMicRecoverySteps() {
    return "If blocked: click the lock icon in the address bar → Site settings → Microphone → Allow, then reload and try again.";
  }

  function clearRecording() {
    if (recordObjectUrl) {
      try { URL.revokeObjectURL(recordObjectUrl); } catch (e) {}
    }
    recordObjectUrl = null;
    recordedBlob = null;
    if (recordPlayerEl) recordPlayerEl.innerHTML = "";
    setRecordStatus("No recording yet.");
    if (clearRecordingEl) clearRecordingEl.disabled = true;
    if (promptAudioEl) promptAudioEl.value = "";
    updateUploadHint();
  }

  function renderRecording(blob) {
    if (!recordPlayerEl) return;
    if (recordObjectUrl) {
      try { URL.revokeObjectURL(recordObjectUrl); } catch (e) {}
    }
    recordPlayerEl.innerHTML = "";
    recordObjectUrl = URL.createObjectURL(blob);
    const audio = document.createElement("audio");
    audio.src = recordObjectUrl;
    audio.controls = true;
    recordPlayerEl.appendChild(audio);
    if (clearRecordingEl) clearRecordingEl.disabled = false;
  }

  async function startRecording() {
    const diagnostics = await diagnoseMicReadiness();
    if (diagnostics.reasons.length > 0) {
      const details = diagnostics.reasons.join(" ");
      if (!window.isSecureContext) {
        const secureUrl = `https://${window.location.host}${window.location.pathname}${window.location.search}`;
        setRecordStatus(`${details} Open: ${secureUrl} ${browserMicRecoverySteps()}`);
      } else if (diagnostics.permissionState === "denied") {
        setRecordStatus(`${details} ${browserMicPermissionHelp()} ${browserMicRecoverySteps()}`);
      } else {
        setRecordStatus(`${details} ${browserMicRecoverySteps()}`);
      }
      return;
    }

    // Calling getUserMedia here initiates the browser permission prompt when state is "prompt".
    try {
      recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recorder = new MediaRecorder(recordingStream);
      const chunks = [];
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data && event.data.size > 0) chunks.push(event.data);
      });
      recorder.addEventListener("stop", () => {
        const blob = new Blob(chunks, { type: recorder?.mimeType || "audio/webm" });
        recordedBlob = blob;
        renderRecording(blob);
        setRecordStatus("Recording ready. It will be used unless a file is uploaded.");
        if (recordAudioEl) recordAudioEl.disabled = false;
        if (stopRecordingEl) stopRecordingEl.disabled = true;
        if (recordingStream) {
          recordingStream.getTracks().forEach((track) => track.stop());
          recordingStream = null;
        }
      });
      recorder.start();
      setRecordStatus("Recording... click Stop when done.");
      if (recordAudioEl) recordAudioEl.disabled = true;
      if (stopRecordingEl) stopRecordingEl.disabled = false;
    } catch (e) {
      const code = String(e?.name || "");
      if (code === "NotAllowedError" || code === "SecurityError") {
        setRecordStatus(`Microphone access denied. ${browserMicPermissionHelp()} ${browserMicRecoverySteps()}`);
      } else if (code === "NotFoundError" || code === "DevicesNotFoundError") {
        setRecordStatus("No microphone device was found. Connect a microphone, confirm OS input device permissions, then reload and try again.");
      } else if (code === "NotReadableError") {
        setRecordStatus("Microphone is busy or unavailable (possibly in use by another app). Close other apps using the mic, then retry.");
      } else {
        setRecordStatus(`Unable to start recording: ${String(e?.message || e)} ${browserMicRecoverySteps()}`);
      }
      if (recordAudioEl) recordAudioEl.disabled = false;
      if (stopRecordingEl) stopRecordingEl.disabled = true;
    }
  }

  function stopRecording() {
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    }
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let size = value;
    let idx = 0;
    while (size >= 1024 && idx < units.length - 1) {
      size /= 1024;
      idx += 1;
    }
    return `${size >= 10 || idx === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[idx]}`;
  }

  function updateUploadHint() {
    if (!uploadHintEl) return;
    const selected = promptAudioEl?.files && promptAudioEl.files[0];
    if (selected) {
      const limit = libraryMaxBytes ? ` Limit: ${formatBytes(libraryMaxBytes)}.` : "";
      uploadHintEl.textContent = `Selected ${selected.name} (${formatBytes(selected.size)}). Recommended sample length is about 5-15 seconds.${limit}`;
      return;
    }
    const limit = libraryMaxBytes ? ` Keep it under ${formatBytes(libraryMaxBytes)} when possible.` : "";
    uploadHintEl.textContent = `Choose a clean reference clip around 5-15 seconds long.${limit}`;
  }

  function getPromptAudio() {
    const file = promptAudioEl?.files && promptAudioEl.files[0];
    if (file) {
      return { file, name: file.name };
    }
    if (recordedBlob) {
      const ext = recordedBlob.type.includes("wav") ? "wav" : "webm";
      return { file: recordedBlob, name: `recording.${ext}` };
    }
    return null;
  }

  function buildFormData() {
    const voiceName = String(voiceNameEl?.value || "").trim();
    if (!voiceName) throw new Error("voice name is required");

    const promptAudio = getPromptAudio();
    if (!promptAudio) throw new Error("upload sample audio or record a sample first");

    if (libraryMaxBytes && Number(promptAudio.file?.size || 0) > libraryMaxBytes) {
      throw new Error(`audio exceeds library limit (${formatBytes(libraryMaxBytes)})`);
    }

    const fd = new FormData();
    fd.append("voice_name", voiceName);
    fd.append("prompt_audio", promptAudio.file, promptAudio.name);

    return fd;
  }

  function describeError(statusCode, text) {
    const raw = String(text || "").trim();
    if (statusCode === 413 || /request entity too large/i.test(raw)) {
      return "Upload rejected by nginx before Gateway received it. The active reverse-proxy body limit is still lower than this file size.";
    }
    return raw || `HTTP ${statusCode}`;
  }

  async function loadVoiceLibrary() {
    if (!voiceListEl) return;
    voiceListEl.innerHTML = '<div class="hint">Loading saved voices...</div>';
    try {
      const resp = await fetch('/ui/api/tts/voice-library', {
        method: 'GET',
        credentials: 'same-origin',
      });
      if (!resp.ok) {
        const err = await resp.text();
        voiceListEl.innerHTML = `<div class="hint error">${describeError(resp.status, err)}</div>`;
        return;
      }
      const payload = await resp.json();
      libraryMaxBytes = Number(payload?.max_bytes || 0);
      updateUploadHint();
      renderVoiceLibrary(Array.isArray(payload?.voices) ? payload.voices : []);
    } catch (e) {
      voiceListEl.innerHTML = `<div class="hint error">${String(e?.message || e)}</div>`;
    }
  }

  function renderVoiceLibrary(items) {
    if (!voiceListEl) return;
    voiceListEl.innerHTML = '';
    const voices = Array.isArray(items) ? items : [];
    if (!voices.length) {
      voiceListEl.innerHTML = '<div class="hint">No saved voices yet.</div>';
      return;
    }

    for (const item of voices) {
      const row = document.createElement('div');
      row.className = 'voice-row';

      const main = document.createElement('div');
      main.className = 'voice-main';

      const name = document.createElement('div');
      name.className = 'voice-name';
      name.textContent = String(item?.name || item?.id || 'voice');
      main.appendChild(name);

      const meta = document.createElement('div');
      meta.className = 'voice-meta';
      const parts = [];
      if (item?.filename) parts.push(String(item.filename));
      if (Number.isFinite(item?.bytes)) parts.push(formatBytes(Number(item.bytes)));
      if (item?.id) parts.push(`id: ${String(item.id)}`);
      meta.textContent = parts.join(' • ');
      main.appendChild(meta);

      const actions = document.createElement('div');
      actions.className = 'voice-actions';

      const renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.dataset.uiRole = 'secondary';
      renameBtn.textContent = 'Rename';
      renameBtn.addEventListener('click', async () => {
        const currentName = String(item?.name || item?.id || '').trim();
        const nextName = window.prompt('Rename voice', currentName);
        if (nextName == null) return;
        const trimmed = String(nextName || '').trim();
        if (!trimmed || trimmed === currentName) return;
        setStatus('Renaming voice...', false);
        try {
          const resp = await fetch(`/ui/api/tts/voice-library/${encodeURIComponent(String(item?.id || ''))}`, {
            method: 'PATCH',
            credentials: 'same-origin',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ voice_name: trimmed }),
          });
          if (!resp.ok) {
            const err = await resp.text();
            setStatus(describeError(resp.status, err), true);
            return;
          }
          setStatus(`Renamed voice to ${trimmed}.`, false);
          await loadVoiceLibrary();
        } catch (e) {
          setStatus(String(e?.message || e), true);
        }
      });

      const deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.dataset.uiRole = 'danger';
      deleteBtn.textContent = 'Remove';
      deleteBtn.addEventListener('click', async () => {
        const voiceId = String(item?.id || '').trim();
        const voiceName = String(item?.name || voiceId).trim();
        if (!voiceId) return;
        if (!window.confirm(`Remove voice '${voiceName}'?`)) return;
        setStatus('Removing voice...', false);
        try {
          const resp = await fetch(`/ui/api/tts/voice-library/${encodeURIComponent(voiceId)}`, {
            method: 'DELETE',
            credentials: 'same-origin',
          });
          if (!resp.ok) {
            const err = await resp.text();
            setStatus(describeError(resp.status, err), true);
            return;
          }
          setStatus(`Removed voice ${voiceName}.`, false);
          await loadVoiceLibrary();
        } catch (e) {
          setStatus(String(e?.message || e), true);
        }
      });

      actions.appendChild(renameBtn);
      actions.appendChild(deleteBtn);
      row.appendChild(main);
      row.appendChild(actions);
      voiceListEl.appendChild(row);
    }
  }

  async function handleSaveVoice() {
    setStatus("", false);
    setMeta("");

    let formData;
    try {
      formData = buildFormData();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    saveVoiceEl.disabled = true;
    setStatus("Saving voice...", false);

    try {
      const resp = await fetch('/ui/api/tts/voice-library', {
        method: 'POST',
        credentials: 'same-origin',
        body: formData,
      });

      if (!resp.ok) {
        const err = await resp.text();
        setStatus(describeError(resp.status, err), true);
        return;
      }

      const payload = await resp.json();
      const saved = payload?.voice || {};
      const savedName = String(saved?.name || voiceNameEl?.value || '').trim();
      const savedId = String(saved?.id || '').trim();
      setStatus(savedId ? `Saved voice ${savedName} (${savedId}).` : `Saved voice ${savedName}.`, false);
      setMeta('Use this voice from the Text-to-Speech UI with the LuxTTS backend.');
      clearRecording();
      if (voiceNameEl) voiceNameEl.value = '';
      await loadVoiceLibrary();
    } catch (e) {
      setStatus(String(e), true);
    } finally {
      saveVoiceEl.disabled = false;
    }
  }

  if (saveVoiceEl) saveVoiceEl.addEventListener('click', handleSaveVoice);
  if (refreshVoicesEl) refreshVoicesEl.addEventListener('click', loadVoiceLibrary);
  if (recordAudioEl) recordAudioEl.addEventListener('click', startRecording);
  if (stopRecordingEl) stopRecordingEl.addEventListener('click', stopRecording);
  if (clearRecordingEl) clearRecordingEl.addEventListener('click', clearRecording);
  if (promptAudioEl) promptAudioEl.addEventListener('change', updateUploadHint);

  clearRecording();
  updateUploadHint();
  loadVoiceLibrary().catch(() => {});
  diagnoseMicReadiness().then((diagnostics) => {
    if (!diagnostics.reasons.length) {
      setRecordStatus("Microphone appears available. Click Record to grant access/start.");
    } else if (diagnostics.permissionState === "denied") {
      setRecordStatus(`${diagnostics.reasons.join(" ")} ${browserMicPermissionHelp()} ${browserMicRecoverySteps()}`);
    } else {
      setRecordStatus(`${diagnostics.reasons.join(" ")} ${browserMicRecoverySteps()}`);
    }
  }).catch(() => {});
})();
