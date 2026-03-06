(() => {
  const $ = (id) => document.getElementById(id);

  const textEl = $("text");
  const backendEl = $("backend");
  const promptAudioEl = $("promptAudio");
  const voiceNameEl = $("voiceName");
  const sampleTextEl = $("sampleText");
  const recordAudioEl = $("recordAudio");
  const stopRecordingEl = $("stopRecording");
  const clearRecordingEl = $("clearRecording");
  const recordStatusEl = $("recordStatus");
  const recordPlayerEl = $("recordPlayer");
  const generateEl = $("generate");
  const statusEl = $("status");
  const metaEl = $("meta");
  const playerEl = $("player");

  let activeObjectUrl = null;
  let recordObjectUrl = null;
  let recordedBlob = null;
  let recorder = null;
  let recordingStream = null;

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  function setMeta(text) {
    metaEl.textContent = text || "";
  }

  function clearPlayer() {
    if (activeObjectUrl) {
      try { URL.revokeObjectURL(activeObjectUrl); } catch (e) {}
      activeObjectUrl = null;
    }
    if (playerEl) playerEl.innerHTML = "";
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

  function renderAudio(url) {
    if (!url || !playerEl) return;
    const wrapper = document.createElement("div");
    wrapper.className = "audio-card";
    const audio = document.createElement("audio");
    audio.src = url;
    audio.preload = "metadata";
    audio.controls = true;
    wrapper.appendChild(audio);
    playerEl.appendChild(wrapper);
  }

  function buildFormData() {
    const text = String(textEl.value || "").trim();
    if (!text) throw new Error("text is required");

    const promptAudio = getPromptAudio();

    const fd = new FormData();
    fd.append("text", text);
    if (promptAudio) fd.append("prompt_audio", promptAudio.file, promptAudio.name);
    if (!promptAudio) {
      throw new Error("upload sample audio or record a sample first");
    }

    const backendClass = String(backendEl?.value || "").trim();
    if (backendClass) fd.append("backend_class", backendClass);

    const voiceName = String(voiceNameEl?.value || "").trim();
    if (!voiceName) {
      throw new Error("voice name is required");
    }
    fd.append("voice_name", voiceName);

    return fd;
  }

  async function handleGenerate() {
    setStatus("", false);
    setMeta("");
    clearPlayer();

    let formData;
    try {
      formData = buildFormData();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    generateEl.disabled = true;
    setStatus("Generating...", false);

    try {
      const resp = await fetch('/ui/api/tts/clone', {
        method: 'POST',
        credentials: 'same-origin',
        body: formData,
      });

      const contentType = resp.headers.get('content-type') || '';
      const savedVoiceId = String(resp.headers.get('X-Gateway-Voice-Id') || '').trim();
      if (!resp.ok) {
        const err = await resp.text();
        setStatus(err || `HTTP ${resp.status}`, true);
        return;
      }

      if (contentType.includes('application/json')) {
        const payload = await resp.json();
        const raw = payload?.audio_base64 || payload?.audio || payload?.audio_data;
        if (raw) {
          let b64 = String(raw || "");
          if (b64.startsWith('data:')) {
            renderAudio(b64);
          } else {
            const binary = atob(b64);
            const len = binary.length;
            const bytes = new Uint8Array(len);
            for (let i = 0; i < len; i += 1) bytes[i] = binary.charCodeAt(i);
            const blob = new Blob([bytes], { type: payload?.content_type || 'audio/wav' });
            const url = URL.createObjectURL(blob);
            activeObjectUrl = url;
            renderAudio(url);
          }
        } else if (payload?.audio_url) {
          renderAudio(String(payload.audio_url));
        } else {
          setMeta(JSON.stringify(payload, null, 2));
        }
      } else {
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        activeObjectUrl = url;
        renderAudio(url);
      }

      if (savedVoiceId) {
        setStatus(`Audio ready. Saved voice: ${savedVoiceId}`, false);
      } else {
        setStatus("Audio ready.", false);
      }
    } catch (e) {
      setStatus(String(e), true);
    } finally {
      generateEl.disabled = false;
    }
  }

  if (generateEl) generateEl.addEventListener('click', handleGenerate);
  if (recordAudioEl) recordAudioEl.addEventListener('click', startRecording);
  if (stopRecordingEl) stopRecordingEl.addEventListener('click', stopRecording);
  if (clearRecordingEl) clearRecordingEl.addEventListener('click', clearRecording);

  if (backendEl) backendEl.value = "luxtts";
  clearRecording();
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
