(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    providers: $("providers"), configStatus: $("configStatus"), accounts: $("accounts"), media: $("media"),
    accountSelect: $("accountSelect"), mediaSelect: $("mediaSelect"), accountCapabilities: $("accountCapabilities"),
    uploadForm: $("uploadForm"), videoFile: $("videoFile"), publish: $("publish"), publishStatus: $("publishStatus"),
    publications: $("publications"), privacy: $("privacy"), publishConsent: $("publishConsent"), musicConsent: $("musicConsent"),
  };
  const state = { config: null, accounts: [], media: [], publications: [], capabilities: null };

  function status(el, text, error = false) {
    el.textContent = text || "";
    el.className = `hint status${error ? " error" : ""}`;
  }
  async function fetchJson(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
    const text = await response.text();
    let payload = {};
    if (text) { try { payload = JSON.parse(text); } catch (error) { payload = { detail: text }; } }
    if (!response.ok) {
      if (response.status === 401) {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.replace(`/ui/login?next=${next}`);
      }
      const detail = payload?.detail || payload?.error || `HTTP ${response.status}`;
      const message = typeof detail === "string" ? detail : detail?.message || detail?.provider_error?.message || JSON.stringify(detail);
      const error = new Error(message); error.payload = payload; throw error;
    }
    return payload;
  }
  function fmtBytes(value) {
    const n = Number(value || 0); if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB"]; const index = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / Math.pow(1024, index)).toFixed(index ? 1 : 0)} ${units[index]}`;
  }
  function fmtTs(value) { return value ? new Date(Number(value) * 1000).toLocaleString() : ""; }
  function currentAccount() { return state.accounts.find((item) => item.id === els.accountSelect.value) || null; }
  function currentMedia() { return state.media.find((item) => item.id === els.mediaSelect.value) || null; }
  function lines(value) { return String(value || "").split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean); }

  async function loadConfig() {
    state.config = await fetchJson("/ui/api/social/publishing/config");
    renderProviders();
  }
  function renderProviders() {
    els.providers.innerHTML = "";
    const entries = [
      ["youtube", "YouTube", "Google OAuth and resumable video upload"],
      ["meta", "Facebook + Instagram", "Meta Login, Facebook Pages, and linked Instagram professional accounts"],
      ["tiktok", "TikTok", "Login Kit and Content Posting API"],
    ];
    for (const [key, label, description] of entries) {
      const readiness = state.config?.readiness?.[key] || { ready: false, missing: [] };
      const card = document.createElement("div"); card.className = "provider stack";
      const head = document.createElement("div"); head.className = "provider-head";
      const title = document.createElement("h3"); title.textContent = label;
      const badge = document.createElement("span"); badge.className = `badge ${readiness.ready ? "good" : "error"}`; badge.textContent = readiness.ready ? "Configured" : "Needs setup";
      head.append(title, badge); card.appendChild(head);
      const note = document.createElement("div"); note.className = "hint"; note.textContent = description; card.appendChild(note);
      if (readiness.missing?.length) { const missing = document.createElement("div"); missing.className = "hint error"; missing.textContent = `Missing: ${readiness.missing.join(", ")}`; card.appendChild(missing); }
      const button = document.createElement("button"); button.type = "button"; button.textContent = `Connect ${label}`; button.disabled = !readiness.ready;
      button.addEventListener("click", () => connectProvider(key)); card.appendChild(button); els.providers.appendChild(card);
    }
    status(els.configStatus, state.config.enabled ? "Publishing is enabled. Provider actions still require their own app approval and scopes." : "Publishing is disabled until SOCIAL_PUBLISHING_ENABLED=true.", !state.config.enabled);
  }
  async function connectProvider(provider) {
    status(els.configStatus, `Opening ${provider} authorization…`);
    try {
      const payload = await fetchJson("/ui/api/social/oauth/start", { method: "POST", body: JSON.stringify({ provider, redirect_after: "/ui/social/publish" }) });
      window.location.assign(payload.authorization_url);
    } catch (error) { status(els.configStatus, error.message, true); }
  }

  async function loadAccounts() {
    const payload = await fetchJson("/ui/api/social/accounts"); state.accounts = payload.accounts || []; renderAccounts();
  }
  function renderAccounts() {
    els.accounts.innerHTML = ""; els.accountSelect.innerHTML = '<option value="">Select account</option>';
    if (!state.accounts.length) els.accounts.innerHTML = '<div class="hint">No connected accounts.</div>';
    for (const account of state.accounts) {
      const item = document.createElement("div"); item.className = "item";
      const head = document.createElement("div"); head.className = "item-head";
      const title = document.createElement("div"); title.innerHTML = `<strong>${escapeHtml(account.display_name)}</strong><div class="hint">${escapeHtml(account.provider)} · ${escapeHtml(account.account_type)}</div>`;
      const disconnect = document.createElement("button"); disconnect.type = "button"; disconnect.className = "danger"; disconnect.textContent = "Disconnect";
      disconnect.addEventListener("click", async () => { if (!window.confirm(`Disconnect ${account.display_name}?`)) return; await fetchJson(`/ui/api/social/accounts/${encodeURIComponent(account.id)}`, { method: "DELETE" }); await loadAccounts(); });
      head.append(title, disconnect); item.appendChild(head); els.accounts.appendChild(item);
      const option = document.createElement("option"); option.value = account.id; option.textContent = `${account.display_name} (${account.provider})`; els.accountSelect.appendChild(option);
    }
  }

  async function loadMedia() {
    const payload = await fetchJson("/ui/api/social/media"); state.media = payload.media || []; renderMedia();
  }
  function renderMedia() {
    els.media.innerHTML = ""; els.mediaSelect.innerHTML = '<option value="">Select video</option>';
    if (!state.media.length) els.media.innerHTML = '<div class="hint">No uploaded videos.</div>';
    for (const media of state.media) {
      const item = document.createElement("div"); item.className = "item";
      const duration = media.metadata?.duration_sec ? ` · ${media.metadata.duration_sec}s` : "";
      item.innerHTML = `<strong>${escapeHtml(media.filename)}</strong><div class="hint">${fmtBytes(media.size_bytes)}${duration} · expires ${fmtTs(media.expires_ts)}</div>`;
      els.media.appendChild(item);
      const option = document.createElement("option"); option.value = media.id; option.textContent = `${media.filename} (${fmtBytes(media.size_bytes)})`; els.mediaSelect.appendChild(option);
    }
  }
  async function uploadMedia(event) {
    event.preventDefault(); const file = els.videoFile.files?.[0]; if (!file) return;
    const form = new FormData(); form.append("file", file);
    status(els.publishStatus, `Uploading ${file.name}…`);
    try {
      const payload = await fetchJson("/ui/api/social/media", { method: "POST", body: form });
      await loadMedia(); els.mediaSelect.value = payload.media.id; els.videoFile.value = ""; status(els.publishStatus, "Video uploaded.");
    } catch (error) { status(els.publishStatus, error.message, true); }
  }

  function setVisible(id, visible) { $(id).classList.toggle("hidden", !visible); }
  function setPrivacyOptions(options) {
    els.privacy.innerHTML = '<option value="">Select explicitly</option>';
    for (const value of options) { const option = document.createElement("option"); option.value = value; option.textContent = value.replaceAll("_", " "); els.privacy.appendChild(option); }
  }
  async function accountChanged() {
    const account = currentAccount(); state.capabilities = null; els.accountCapabilities.textContent = "";
    setVisible("youtubeOptions", account?.provider === "youtube"); setVisible("instagramOptions", account?.provider === "instagram"); setVisible("tiktokOptions", account?.provider === "tiktok");
    setVisible("descriptionField", account?.provider !== "instagram" && account?.provider !== "tiktok");
    setVisible("tagsField", account?.provider === "youtube"); setVisible("categoryField", account?.provider === "youtube"); setVisible("publishAtField", account?.provider === "youtube");
    setVisible("privacyField", account?.provider === "youtube" || account?.provider === "tiktok"); setVisible("coverField", account?.provider === "instagram" || account?.provider === "tiktok");
    if (!account) { setPrivacyOptions([]); return; }
    if (account.provider === "youtube") setPrivacyOptions(["private", "unlisted", "public"]); else setPrivacyOptions([]);
    prefillDraft(account.provider);
    try {
      const payload = await fetchJson(`/ui/api/social/accounts/${encodeURIComponent(account.id)}/capabilities`); state.capabilities = payload;
      if (account.provider === "tiktok") {
        const data = payload.data || {}; setPrivacyOptions(data.privacy_level_options || []);
        $("allowComment").disabled = !!data.comment_disabled; $("allowDuet").disabled = !!data.duet_disabled; $("allowStitch").disabled = !!data.stitch_disabled;
        els.accountCapabilities.textContent = `${data.creator_nickname || account.display_name} · maximum video duration ${data.max_video_post_duration_sec || "unknown"} seconds`;
      } else els.accountCapabilities.textContent = `${account.display_name} is ready for ${account.provider} publishing.`;
    } catch (error) { els.accountCapabilities.textContent = error.message; els.accountCapabilities.className = "hint error"; }
  }
  function prefillDraft(provider) {
    try {
      const saved = JSON.parse(localStorage.getItem("nexus.socialStudio.v1") || "null");
      const source = saved?.result?.drafts?.platforms?.[provider]?.variants?.[0]; if (!source) return;
      $("title").value = source.title || source.caption || ""; $("description").value = source.description || "";
      $("tags").value = Array.isArray(source.tags) ? source.tags.join("\n") : Array.isArray(source.hashtags) ? source.hashtags.join("\n") : "";
    } catch (error) {}
  }
  function metadataFor(account) {
    const title = $("title").value.trim(); const description = $("description").value.trim();
    if (account.provider === "youtube") return { title, description, tags: lines($("tags").value), category_id: $("categoryId").value.trim() || "22", privacy_status: els.privacy.value, publish_at: $("publishAt").value.trim() || null, made_for_kids: $("madeForKids").checked };
    if (account.provider === "facebook") return { title, description, video_state: "PUBLISHED" };
    if (account.provider === "instagram") return { caption: title, share_to_feed: $("shareToFeed").checked, thumb_offset: Number($("coverTimestamp").value || 0) };
    if (account.provider === "tiktok") return { title, privacy_level: els.privacy.value, allow_comment: $("allowComment").checked, allow_duet: $("allowDuet").checked, allow_stitch: $("allowStitch").checked, brand_organic_toggle: $("brandOrganic").checked, brand_content_toggle: $("brandContent").checked, is_aigc: $("isAigc").checked, video_cover_timestamp_ms: Number($("coverTimestamp").value || 0) };
    return {};
  }
  async function publish() {
    const account = currentAccount(); const media = currentMedia();
    if (!account || !media) { status(els.publishStatus, "Select an account and video.", true); return; }
    if (!els.publishConsent.checked) { status(els.publishStatus, "Review and confirm the publication first.", true); return; }
    if (account.provider === "tiktok" && !els.musicConsent.checked) { status(els.publishStatus, "TikTok Music Usage Confirmation is required.", true); return; }
    if ((account.provider === "youtube" || account.provider === "tiktok") && !els.privacy.value) { status(els.publishStatus, "Select privacy explicitly.", true); return; }
    els.publish.disabled = true; status(els.publishStatus, `Starting ${account.provider} publication…`);
    try {
      const payload = await fetchJson("/ui/api/social/publications", { method: "POST", body: JSON.stringify({ account_id: account.id, media_id: media.id, metadata: metadataFor(account), confirmed: true, music_usage_confirmed: account.provider === "tiktok" ? els.musicConsent.checked : false }) });
      status(els.publishStatus, `Publication ${payload.publication.status}. Use Refresh status while the provider processes it.`); await loadPublications();
    } catch (error) { status(els.publishStatus, error.message, true); await loadPublications(); } finally { els.publish.disabled = false; }
  }

  async function loadPublications() { const payload = await fetchJson("/ui/api/social/publications"); state.publications = payload.publications || []; renderPublications(); }
  function renderPublications() {
    els.publications.innerHTML = ""; if (!state.publications.length) els.publications.innerHTML = '<div class="hint">No publication attempts.</div>';
    for (const publication of state.publications) {
      const item = document.createElement("div"); item.className = "item";
      const head = document.createElement("div"); head.className = "item-head";
      const title = document.createElement("div"); title.innerHTML = `<strong>${escapeHtml(publication.provider)} · ${escapeHtml(publication.status)}</strong><div class="hint">${fmtTs(publication.updated_ts)} · remote ${escapeHtml(publication.remote_id || "pending")}</div>`;
      const advance = document.createElement("button"); advance.type = "button"; advance.textContent = "Refresh status"; advance.disabled = ["PUBLISHED", "FAILED_PERMANENT", "REVOKED"].includes(publication.status);
      advance.addEventListener("click", async () => { advance.disabled = true; try { await fetchJson("/ui/api/social/publications/advance", { method: "POST", body: JSON.stringify({ publication_id: publication.id }) }); await loadPublications(); } catch (error) { status(els.publishStatus, error.message, true); await loadPublications(); } });
      head.append(title, advance); item.appendChild(head);
      if (publication.error && Object.keys(publication.error).length) { const pre = document.createElement("pre"); pre.textContent = JSON.stringify(publication.error, null, 2); item.appendChild(pre); }
      els.publications.appendChild(item);
    }
  }
  function escapeHtml(value) { const div = document.createElement("div"); div.textContent = String(value || ""); return div.innerHTML; }

  $("refreshAccounts").addEventListener("click", loadAccounts); $("refreshMedia").addEventListener("click", loadMedia); $("refreshPublications").addEventListener("click", loadPublications);
  els.uploadForm.addEventListener("submit", uploadMedia); els.accountSelect.addEventListener("change", accountChanged); els.publish.addEventListener("click", publish);

  Promise.all([loadConfig(), loadAccounts(), loadMedia(), loadPublications()]).then(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("oauth_error")) status(els.configStatus, params.get("oauth_error"), true);
    else if (params.get("oauth_connected")) status(els.configStatus, `Connected ${params.get("oauth_connected")} ${params.get("oauth_provider")} account record(s).`);
  }).catch((error) => status(els.configStatus, error.message, true));
})();
