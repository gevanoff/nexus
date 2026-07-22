(() => {
  const PLATFORM_KEYS = ["youtube", "facebook", "instagram", "tiktok"];
  const LOCAL_KEY = "nexus.socialStudio.v1";
  const $ = (id) => document.getElementById(id);

  const els = {
    persistence: $("persistence"), brandSelect: $("brandSelect"), newBrand: $("newBrand"),
    duplicateBrand: $("duplicateBrand"), deleteBrand: $("deleteBrand"), saveBrand: $("saveBrand"),
    brandStatus: $("brandStatus"), newBrief: $("newBrief"), saveBrief: $("saveBrief"), briefStatus: $("briefStatus"),
    saveWorkspace: $("saveWorkspace"),
    previewPrompt: $("previewPrompt"), generate: $("generate"), exportJson: $("exportJson"), status: $("status"),
    model: $("model"), variants: $("variants"), customInstruction: $("customInstruction"),
    resultsSection: $("resultsSection"), resultTabs: $("resultTabs"), results: $("results"), routing: $("routing"),
    promptDialog: $("promptDialog"), systemPrompt: $("systemPrompt"), userPrompt: $("userPrompt"),
    copyPrompt: $("copyPrompt"), closePrompt: $("closePrompt"),
  };

  const brandFieldIds = {
    name: "brandName", description: "brandDescription", audience: "brandAudience", voice: "brandVoice",
    terminology: "brandTerminology", required_facts: "brandFacts", prohibited_claims: "brandAvoid",
    calls_to_action: "brandCtas", default_links: "brandLinks", default_hashtags: "brandHashtags",
    prompt_addendum: "brandPrompt",
  };
  const briefFieldIds = {
    asset_name: "assetName", subject: "subject", content_summary: "contentSummary", transcript_notes: "transcriptNotes",
    key_points: "keyPoints", people_organizations: "peopleOrganizations", dates_locations: "datesLocations",
    content_goal: "contentGoal", audience_override: "audienceOverride", call_to_action: "callToAction",
    destination_url: "destinationUrl", language: "language", factual_constraints: "factualConstraints", extra_notes: "extraNotes",
  };
  const lmFieldTargets = [
    { section: "brand", field: "audience", id: "brandAudience", label: "Primary audience", output: "string" },
    { section: "brand", field: "voice", id: "brandVoice", label: "Voice and tone", output: "string" },
    { section: "brand", field: "terminology", id: "brandTerminology", label: "Required terminology", output: "list" },
    { section: "brand", field: "required_facts", id: "brandFacts", label: "Required facts or standing context", output: "list" },
    { section: "brand", field: "prohibited_claims", id: "brandAvoid", label: "Claims or formulations to avoid", output: "list" },
    { section: "brand", field: "calls_to_action", id: "brandCtas", label: "Default calls to action", output: "list" },
    { section: "brand", field: "default_links", id: "brandLinks", label: "Default links", output: "list" },
    { section: "brand", field: "default_hashtags", id: "brandHashtags", label: "Default hashtags", output: "list" },
    { section: "brand", field: "platform_guidance.youtube", id: "guideYoutube", label: "YouTube brand guidance", output: "string" },
    { section: "brand", field: "platform_guidance.facebook", id: "guideFacebook", label: "Facebook brand guidance", output: "string" },
    { section: "brand", field: "platform_guidance.instagram", id: "guideInstagram", label: "Instagram brand guidance", output: "string" },
    { section: "brand", field: "platform_guidance.tiktok", id: "guideTiktok", label: "TikTok brand guidance", output: "string" },
    { section: "brand", field: "prompt_addendum", id: "brandPrompt", label: "Additional brand prompt guidance", output: "string" },
    { section: "brief", field: "key_points", id: "keyPoints", label: "Key points", output: "list" },
    { section: "brief", field: "people_organizations", id: "peopleOrganizations", label: "People and organizations", output: "list" },
    { section: "brief", field: "dates_locations", id: "datesLocations", label: "Dates and locations", output: "list" },
    { section: "brief", field: "content_goal", id: "contentGoal", label: "Content goal", output: "string" },
    { section: "brief", field: "audience_override", id: "audienceOverride", label: "Audience override", output: "string" },
    { section: "brief", field: "call_to_action", id: "callToAction", label: "Call to action", output: "string" },
    { section: "brief", field: "destination_url", id: "destinationUrl", label: "Destination URL", output: "string" },
    { section: "brief", field: "factual_constraints", id: "factualConstraints", label: "Factual constraints", output: "list" },
    { section: "brief", field: "extra_notes", id: "extraNotes", label: "Extra notes", output: "string" },
  ];
  const listBrandFields = new Set(["terminology", "required_facts", "prohibited_claims", "calls_to_action", "default_links", "default_hashtags"]);
  const listBriefFields = new Set(["key_points", "people_organizations", "dates_locations", "factual_constraints"]);
  const state = { workspace: null, persistent: false, contracts: {}, result: null, activeResultPlatform: "" };

  function setStatus(text, isError = false) {
    els.status.textContent = text || "";
    els.status.className = `hint status${isError ? " error" : ""}`;
  }
  function setSectionStatus(element, text, isError = false) {
    if (!element) return;
    element.textContent = text || "";
    element.className = `hint${isError ? " error" : ""}`;
  }
  function sectionStatus(section) { return section === "brand" ? els.brandStatus : els.briefStatus; }
  function splitLines(value) {
    return String(value || "").split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean);
  }
  function joinLines(value) { return Array.isArray(value) ? value.join("\n") : String(value || ""); }
  function slug(value) {
    return String(value || "brand").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "brand";
  }

  async function fetchJson(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
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
      const message = typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail);
      const error = new Error(message); error.payload = payload; throw error;
    }
    return payload;
  }

  function localWorkspace() {
    try {
      const parsed = JSON.parse(localStorage.getItem(LOCAL_KEY) || "null");
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch (error) { return null; }
  }
  function saveLocal() {
    try { localStorage.setItem(LOCAL_KEY, JSON.stringify({ workspace: state.workspace, result: state.result })); } catch (error) {}
  }
  function activeBrand() {
    const brands = state.workspace?.brands || [];
    return brands.find((brand) => brand.id === state.workspace.active_brand_id) || brands[0] || null;
  }

  function writeBrandToForm(brand) {
    if (!brand) return;
    for (const [field, id] of Object.entries(brandFieldIds)) {
      const el = $(id); if (!el) continue;
      el.value = listBrandFields.has(field) ? joinLines(brand[field]) : String(brand[field] || "");
    }
    $("guideYoutube").value = brand.platform_guidance?.youtube || "";
    $("guideFacebook").value = brand.platform_guidance?.facebook || "";
    $("guideInstagram").value = brand.platform_guidance?.instagram || "";
    $("guideTiktok").value = brand.platform_guidance?.tiktok || "";
  }
  function readBrandFromForm() {
    const current = activeBrand() || {};
    const brand = { ...current };
    for (const [field, id] of Object.entries(brandFieldIds)) {
      const value = $(id)?.value || "";
      brand[field] = listBrandFields.has(field) ? splitLines(value) : value.trim();
    }
    brand.id = current.id || slug(brand.name);
    brand.platform_guidance = {
      youtube: $("guideYoutube").value.trim(), facebook: $("guideFacebook").value.trim(),
      instagram: $("guideInstagram").value.trim(), tiktok: $("guideTiktok").value.trim(),
    };
    return brand;
  }
  function writeBriefToForm(brief) {
    const source = brief || {};
    for (const [field, id] of Object.entries(briefFieldIds)) {
      const el = $(id); if (!el) continue;
      el.value = listBriefFields.has(field) ? joinLines(source[field]) : String(source[field] || "");
    }
    if (!$("language").value) $("language").value = "English";
  }
  function readBriefFromForm() {
    const brief = {};
    for (const [field, id] of Object.entries(briefFieldIds)) {
      const value = $(id)?.value || "";
      brief[field] = listBriefFields.has(field) ? splitLines(value) : value.trim();
    }
    return brief;
  }
  function syncFormIntoWorkspace() {
    if (!state.workspace) return;
    const brand = readBrandFromForm();
    state.workspace.brands = (state.workspace.brands || []).map((item) => item.id === brand.id ? brand : item);
    state.workspace.working_brief = readBriefFromForm();
    state.workspace.active_brand_id = brand.id;
    saveLocal();
  }
  function renderBrandSelect() {
    const selected = state.workspace?.active_brand_id || "";
    els.brandSelect.innerHTML = "";
    for (const brand of state.workspace?.brands || []) {
      const option = document.createElement("option"); option.value = brand.id; option.textContent = brand.name || brand.id;
      option.selected = brand.id === selected; els.brandSelect.appendChild(option);
    }
    writeBrandToForm(activeBrand());
  }
  function uniqueBrandId(base) {
    const used = new Set((state.workspace?.brands || []).map((brand) => brand.id));
    let candidate = slug(base); let suffix = 2;
    while (used.has(candidate)) candidate = `${slug(base)}-${suffix++}`;
    return candidate;
  }
  function addBrand(copyCurrent = false) {
    syncFormIntoWorkspace();
    const source = copyCurrent && activeBrand() ? JSON.parse(JSON.stringify(activeBrand())) : {
      description: "", audience: "", voice: "Clear, accurate, and concise.", terminology: [], required_facts: [],
      prohibited_claims: [], calls_to_action: [], default_links: [], default_hashtags: [],
      platform_guidance: { youtube: "", facebook: "", instagram: "", tiktok: "" }, prompt_addendum: "",
    };
    const name = copyCurrent ? `${source.name || "Brand"} copy` : "New brand";
    const brand = { ...source, id: uniqueBrandId(name), name };
    state.workspace.brands.push(brand); state.workspace.active_brand_id = brand.id; renderBrandSelect(); saveLocal();
    setSectionStatus(els.brandStatus, copyCurrent ? "Duplicated profile — edit it, then save." : "New profile — edit it, then save.");
  }
  function deleteBrand() {
    if ((state.workspace?.brands || []).length <= 1) { setStatus("At least one brand profile is required.", true); return; }
    const id = state.workspace.active_brand_id;
    state.workspace.brands = state.workspace.brands.filter((brand) => brand.id !== id);
    state.workspace.active_brand_id = state.workspace.brands[0].id; renderBrandSelect(); saveLocal();
    setSectionStatus(els.brandStatus, "Profile deleted — save to confirm the change.");
  }
  function briefHasContent(brief) {
    return Object.entries(brief || {}).some(([field, value]) => {
      if (field === "language" && (!value || value === "English")) return false;
      return Array.isArray(value) ? value.length > 0 : !!String(value || "").trim();
    });
  }
  function newBrief() {
    const current = readBriefFromForm();
    if (briefHasContent(current) && !window.confirm("Start a new video brief? Unsaved brief changes will be cleared.")) return;
    syncFormIntoWorkspace();
    state.workspace.working_brief = { language: "English" };
    state.result = null;
    writeBriefToForm(state.workspace.working_brief);
    renderResults();
    saveLocal();
    setSectionStatus(els.briefStatus, "New brief — add its details, then save.");
    setStatus("New video brief started.");
  }

  function selectedPlatforms() {
    return PLATFORM_KEYS.filter((key) => $(`platform${key[0].toUpperCase()}${key.slice(1)}`)?.checked);
  }
  function requestBody() {
    syncFormIntoWorkspace();
    return {
      model: els.model.value || "default", brand: activeBrand() || {}, brief: state.workspace.working_brief || {},
      platforms: selectedPlatforms(), variants: Number(els.variants.value || 1),
      custom_instruction: els.customInstruction.value.trim(),
    };
  }
  async function fillFieldWithLm(spec, button) {
    if (!state.workspace) {
      setStatus("The Social Studio workspace is still loading.", true);
      return;
    }
    syncFormIntoWorkspace();
    const control = $(spec.id);
    const targetStatus = sectionStatus(spec.section);
    const originalText = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "…";
    setSectionStatus(targetStatus, `Generating ${spec.label}…`);
    setStatus(`Generating ${spec.label} with ${els.model.value || "default"}…`);
    try {
      const payload = await fetchJson("/ui/api/social/field/generate", {
        method: "POST",
        body: JSON.stringify({
          model: els.model.value || "default",
          section: spec.section,
          field: spec.field,
          brand: activeBrand() || {},
          brief: state.workspace.working_brief || {},
        }),
      });
      control.value = spec.output === "list" ? joinLines(payload.value) : String(payload.value || "");
      control.dispatchEvent(new Event("input", { bubbles: true }));
      setSectionStatus(targetStatus, `${spec.label} generated — review, then save.`);
      setStatus(`${spec.label} generated with ${payload.routing?.model || els.model.value || "the selected model"}.`);
      control.focus();
    } catch (error) {
      setSectionStatus(targetStatus, error.message, true);
      setStatus(error.message, true);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = originalText;
    }
  }
  function installLmFillButtons() {
    for (const spec of lmFieldTargets) {
      const control = $(spec.id);
      const label = control?.closest("label");
      const caption = label ? Array.from(label.children).find((child) => child.tagName === "SPAN") : null;
      if (!control || !label || !caption) continue;
      const heading = document.createElement("div");
      heading.className = "field-heading";
      caption.replaceWith(heading);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "lm-fill-button";
      button.textContent = "✦";
      button.title = `Fill ${spec.label} with the selected Nexus model using current form context`;
      button.setAttribute("aria-label", button.title);
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        fillFieldWithLm(spec, button);
      });
      heading.append(caption, button);
    }
  }
  async function saveWorkspace(showStatus = true, successMessage = "All changes saved.") {
    syncFormIntoWorkspace();
    const payload = await fetchJson("/ui/api/social/state", { method: "PUT", body: JSON.stringify({ state: state.workspace }) });
    state.workspace = payload.state; state.persistent = !!payload.persistent;
    els.persistence.textContent = state.persistent ? "Saved to your Nexus user profile" : "Saved in this browser";
    renderBrandSelect(); saveLocal();
    setSectionStatus(els.brandStatus, "Saved.");
    setSectionStatus(els.briefStatus, "Saved.");
    if (showStatus) setStatus(successMessage);
  }

  function modelLabel(item) {
    if (!item || typeof item !== "object") return "";
    if (item.label) return item.label;
    const id = String(item.id || ""); const target = String(item.resolved_model || item.upstream_model || "");
    return target && target !== id ? `${id} → ${target}` : id;
  }
  async function loadModels() {
    try {
      const payload = await fetchJson("/ui/api/models");
      const models = Array.isArray(payload?.data) ? payload.data : [];
      const preferred = ["default", "fast", "reasoning", "long", "coder"];
      const byId = new Map(models.map((item) => [String(item?.id || ""), item]).filter(([id]) => id));
      const ordered = [];
      for (const id of preferred) { if (byId.has(id)) { ordered.push(byId.get(id)); byId.delete(id); } }
      ordered.push(...Array.from(byId.values()).sort((a, b) => String(a.id).localeCompare(String(b.id))));
      els.model.innerHTML = "";
      for (const item of ordered) {
        const option = document.createElement("option"); option.value = item.id; option.textContent = modelLabel(item);
        if (item.id === "default") option.selected = true; els.model.appendChild(option);
      }
      if (!ordered.length) els.model.innerHTML = '<option value="default">default</option>';
    } catch (error) { els.model.innerHTML = '<option value="default">default</option>'; }
  }

  async function previewPrompt() {
    const body = requestBody();
    if (!body.platforms.length) { setStatus("Select at least one platform.", true); return; }
    setStatus("Building prompt…");
    try {
      const payload = await fetchJson("/ui/api/social/prompt", { method: "POST", body: JSON.stringify(body) });
      els.systemPrompt.textContent = payload.system_prompt || ""; els.userPrompt.textContent = payload.user_prompt || "";
      els.promptDialog.showModal(); setStatus("Prompt ready.");
    } catch (error) { setStatus(error.message, true); }
  }
  function fieldValue(value) { return Array.isArray(value) ? value.join("\n") : String(value || ""); }
  function copyText(text) {
    const value = String(text || "");
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(value);
    const temp = document.createElement("textarea"); temp.value = value; temp.style.position = "fixed"; temp.style.left = "-9999px";
    document.body.appendChild(temp); temp.select(); document.execCommand("copy"); temp.remove(); return Promise.resolve();
  }
  function collectVariantText(platform, variant) {
    const fields = state.contracts?.[platform]?.fields || Object.keys(variant).filter((key) => key !== "rationale");
    return fields.map((field) => `${field}: ${fieldValue(variant[field])}`).join("\n");
  }

  function renderPlatform(platform) {
    const source = state.result?.drafts?.platforms?.[platform]; els.results.innerHTML = ""; if (!source) return;
    const card = document.createElement("div"); card.className = "result-card";
    const grid = document.createElement("div"); grid.className = "result-grid";
    const fields = state.contracts?.[platform]?.fields || [];
    source.variants.forEach((variant, index) => {
      const box = document.createElement("div"); box.className = "variant";
      const head = document.createElement("div"); head.className = "variant-head";
      const title = document.createElement("h3"); title.textContent = `Variant ${index + 1}`;
      const copy = document.createElement("button"); copy.type = "button"; copy.textContent = "Copy all";
      copy.addEventListener("click", async () => { await copyText(collectVariantText(platform, variant)); copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy all"; }, 1000); });
      head.append(title, copy); box.appendChild(head);
      for (const field of fields) {
        const label = document.createElement("label"); const caption = document.createElement("span"); caption.textContent = field.replaceAll("_", " ");
        const textarea = document.createElement("textarea"); textarea.value = fieldValue(variant[field]);
        textarea.addEventListener("input", () => { if (Array.isArray(variant[field])) variant[field] = splitLines(textarea.value); else variant[field] = textarea.value; saveLocal(); });
        label.append(caption, textarea); box.appendChild(label);
      }
      if (variant.rationale) { const rationale = document.createElement("div"); rationale.className = "hint"; rationale.textContent = `Rationale: ${variant.rationale}`; box.appendChild(rationale); }
      grid.appendChild(box);
    });
    card.appendChild(grid);
    if (source.warnings?.length) {
      const warnings = document.createElement("ul"); warnings.className = "warnings";
      for (const warning of source.warnings) { const li = document.createElement("li"); li.textContent = warning; warnings.appendChild(li); }
      card.appendChild(warnings);
    }
    els.results.appendChild(card);
  }
  function renderResults() {
    const platforms = Object.keys(state.result?.drafts?.platforms || {});
    if (!platforms.length) { els.resultsSection.hidden = true; els.exportJson.disabled = true; return; }
    els.resultsSection.hidden = false; els.exportJson.disabled = false;
    if (!platforms.includes(state.activeResultPlatform)) state.activeResultPlatform = platforms[0];
    els.resultTabs.innerHTML = "";
    for (const platform of platforms) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = state.contracts?.[platform]?.label || platform;
      button.classList.toggle("active", platform === state.activeResultPlatform);
      button.addEventListener("click", () => { state.activeResultPlatform = platform; renderResults(); }); els.resultTabs.appendChild(button);
    }
    const routing = state.result.routing || {}; els.routing.textContent = routing.model ? `${routing.backend} · ${routing.model}` : "";
    renderPlatform(state.activeResultPlatform);
  }

  async function generate() {
    const body = requestBody();
    if (!body.platforms.length) { setStatus("Select at least one platform.", true); return; }
    if (!body.brief.subject && !body.brief.content_summary && !body.brief.transcript_notes) { setStatus("Add a subject, summary, or transcript before generating.", true); return; }
    els.generate.disabled = true; setStatus("Generating platform drafts…");
    try {
      await saveWorkspace(false);
      const payload = await fetchJson("/ui/api/social/generate", { method: "POST", body: JSON.stringify(body) });
      state.result = payload; state.activeResultPlatform = body.platforms[0];
      els.systemPrompt.textContent = payload.prompt?.system_prompt || ""; els.userPrompt.textContent = payload.prompt?.user_prompt || "";
      saveLocal(); renderResults(); setStatus("Drafts generated. Review every field before publishing.");
    } catch (error) {
      const raw = error.payload?.detail?.raw_text;
      if (raw) { els.systemPrompt.textContent = "The model returned text that could not be parsed as the required JSON."; els.userPrompt.textContent = raw; els.promptDialog.showModal(); }
      setStatus(error.message, true);
    } finally { els.generate.disabled = false; }
  }
  function exportJson() {
    if (!state.result) return;
    const blob = new Blob([JSON.stringify(state.result, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = `nexus-social-drafts-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
  }
  async function loadWorkspace() {
    const local = localWorkspace();
    try {
      const payload = await fetchJson("/ui/api/social/state");
      state.workspace = payload.state; state.persistent = !!payload.persistent; state.contracts = payload.platform_contracts || {};
      if (!state.persistent && local?.workspace) state.workspace = local.workspace;
      if (local?.result) state.result = local.result;
      els.persistence.textContent = state.persistent ? "Saved to your Nexus user profile" : "Saved in this browser";
    } catch (error) {
      if (!local?.workspace) throw error;
      state.workspace = local.workspace; state.result = local.result || null; els.persistence.textContent = "Offline browser copy";
    }
    renderBrandSelect(); writeBriefToForm(state.workspace.working_brief || {}); renderResults();
    setSectionStatus(els.brandStatus, "Ready to edit.");
    setSectionStatus(els.briefStatus, "Ready to edit.");
  }

  els.brandSelect.addEventListener("change", () => { syncFormIntoWorkspace(); state.workspace.active_brand_id = els.brandSelect.value; writeBrandToForm(activeBrand()); saveLocal(); });
  els.newBrand.addEventListener("click", () => addBrand(false)); els.duplicateBrand.addEventListener("click", () => addBrand(true)); els.deleteBrand.addEventListener("click", deleteBrand);
  els.saveBrand.addEventListener("click", () => saveWorkspace(true, "Brand profile saved.").catch((error) => { setSectionStatus(els.brandStatus, error.message, true); setStatus(error.message, true); }));
  els.newBrief.addEventListener("click", newBrief);
  els.saveBrief.addEventListener("click", () => saveWorkspace(true, "Video brief saved.").catch((error) => { setSectionStatus(els.briefStatus, error.message, true); setStatus(error.message, true); }));
  els.saveWorkspace.addEventListener("click", () => saveWorkspace(true).catch((error) => setStatus(error.message, true)));
  els.previewPrompt.addEventListener("click", previewPrompt); els.generate.addEventListener("click", generate); els.exportJson.addEventListener("click", exportJson);
  els.closePrompt.addEventListener("click", () => els.promptDialog.close());
  els.copyPrompt.addEventListener("click", async () => { await copyText(`SYSTEM\n${els.systemPrompt.textContent}\n\nUSER\n${els.userPrompt.textContent}`); els.copyPrompt.textContent = "Copied"; setTimeout(() => { els.copyPrompt.textContent = "Copy"; }, 1000); });

  for (const id of [...Object.values(brandFieldIds), "guideYoutube", "guideFacebook", "guideInstagram", "guideTiktok"]) {
    $(id)?.addEventListener("input", () => setSectionStatus(els.brandStatus, "Unsaved changes."));
  }
  for (const id of Object.values(briefFieldIds)) {
    $(id)?.addEventListener("input", () => setSectionStatus(els.briefStatus, "Unsaved changes."));
  }

  installLmFillButtons();
  Promise.all([loadWorkspace(), loadModels()]).catch((error) => setStatus(error.message, true));
})();
