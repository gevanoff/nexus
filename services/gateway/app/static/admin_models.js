(() => {
  const statusEl = document.getElementById("admin_models_status");
  const listEl = document.getElementById("admin_models_list");
  const refreshEl = document.getElementById("admin_models_refresh");
  const benchmarkEls = {
    models: document.getElementById("model_benchmark_models"),
    maxTokens: document.getElementById("model_benchmark_max_tokens"),
    runs: document.getElementById("model_benchmark_runs"),
    warmups: document.getElementById("model_benchmark_warmups"),
    temperature: document.getElementById("model_benchmark_temperature"),
    topP: document.getElementById("model_benchmark_top_p"),
    prompt: document.getElementById("model_benchmark_prompt"),
    start: document.getElementById("model_benchmark_start"),
    selectAliases: document.getElementById("model_benchmark_select_aliases"),
    clear: document.getElementById("model_benchmark_clear"),
    status: document.getElementById("model_benchmark_status"),
    results: document.getElementById("model_benchmark_results"),
  };
  const toolEls = {
    models: document.getElementById("model_tool_qualification_models"),
    maxTokens: document.getElementById("model_tool_qualification_max_tokens"),
    temperature: document.getElementById("model_tool_qualification_temperature"),
    start: document.getElementById("model_tool_qualification_start"),
    selectAliases: document.getElementById("model_tool_qualification_select_aliases"),
    clear: document.getElementById("model_tool_qualification_clear"),
    status: document.getElementById("model_tool_qualification_status"),
    results: document.getElementById("model_tool_qualification_results"),
  };
  let hugeLanePollTimer = null;
  let modelFetchPollTimer = null;
  let visibleBenchmarkAliases = [];
  let visibleToolAliases = [];
  const modelActionStatus = new Map();

  function handle401(resp) {
    if (resp && resp.status === 401) {
      const back = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/ui/login?next=${back}`;
      return true;
    }
    return false;
  }

  function formatTimestamp(ts) {
    if (!Number.isFinite(ts) || ts <= 0) return "";
    try {
      return new Date(ts * 1000).toLocaleString();
    } catch (error) {
      return "";
    }
  }

  function formatDuration(value) {
    const sec = Number(value || 0);
    if (!Number.isFinite(sec) || sec <= 0) return "0s";
    if (sec < 90) return `${Math.round(sec)}s`;
    return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  }

  function formatMetric(value, suffix) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "-";
    const rendered = num >= 100 ? num.toFixed(0) : num >= 10 ? num.toFixed(1) : num.toFixed(2);
    return suffix ? `${rendered} ${suffix}` : rendered;
  }

  function benchmarkText(benchmark) {
    if (!benchmark || typeof benchmark !== "object") return "";
    const parts = [];
    const tps = Number(benchmark.tokens_per_sec);
    if (Number.isFinite(tps)) parts.push(`${formatMetric(tps, "tok/s")}`);
    const decode = Number(benchmark.decode_tokens_per_sec);
    if (Number.isFinite(decode)) parts.push(`${formatMetric(decode, "decode tok/s")}`);
    const ttft = Number(benchmark.time_to_first_token_ms);
    if (Number.isFinite(ttft)) parts.push(`TTFT ${formatMetric(ttft, "ms")}`);
    const tokens = Number(benchmark.completion_tokens);
    if (Number.isFinite(tokens)) parts.push(`${formatMetric(tokens, "tokens")}`);
    const completed = formatTimestamp(Number(benchmark.completed_at || 0));
    if (completed) parts.push(completed);
    return parts.length ? `latest benchmark: ${parts.join(" · ")}` : "";
  }

  function categoryText(label, value) {
    if (!value || typeof value !== "object") return "";
    const passed = Number(value.passed);
    const total = Number(value.total);
    if (!Number.isFinite(total) || total <= 0) return "";
    const ok = Number.isFinite(passed) ? passed : 0;
    return `${label} ${ok}/${total}`;
  }

  function toolQualificationText(result) {
    if (!result || typeof result !== "object") return "";
    const passed = Number(result.passed);
    const total = Number(result.total);
    const parts = [];
    if (Number.isFinite(passed) && Number.isFinite(total)) {
      parts.push(`tool qualification: ${result.ok === true ? "pass" : "fail"} ${passed}/${total}`);
    } else {
      parts.push(`tool qualification: ${result.ok === true ? "pass" : "fail"}`);
    }
    const categories = result.by_category && typeof result.by_category === "object" ? result.by_category : {};
    ["auto", "required", "named", "stream", "roundtrip"].forEach((name) => {
      const text = categoryText(name, categories[name]);
      if (text) parts.push(text);
    });
    const completed = formatTimestamp(Number(result.completed_at || 0));
    if (completed) parts.push(completed);
    if (result.first_error) parts.push(`error: ${String(result.first_error).slice(0, 120)}`);
    return parts.join(" · ");
  }

  function toolQualificationBadge(result) {
    if (!result || typeof result !== "object") return null;
    const passed = Number(result.passed);
    const total = Number(result.total);
    const countText = Number.isFinite(passed) && Number.isFinite(total) ? ` ${passed}/${total}` : "";
    return { text: `tools ${result.ok === true ? "pass" : "fail"}${countText}`, tone: result.ok === true ? "green" : "red" };
  }

  function shortModel(value) {
    const text = String(value || "");
    if (text.length <= 72) return text;
    return `${text.slice(0, 34)}...${text.slice(-34)}`;
  }

  function badge(text, tone) {
    const el = document.createElement("span");
    el.className = `model-admin-badge${tone ? ` ${tone}` : ""}`;
    el.textContent = text;
    return el;
  }

  function row(left, middle, badges, actions) {
    const el = document.createElement("div");
    el.className = "model-admin-row";
    const name = document.createElement("div");
    name.className = "model-admin-name";
    name.textContent = left;
    const detail = document.createElement("div");
    detail.textContent = middle;
    detail.className = "model-admin-muted";
    const badgeWrap = document.createElement("div");
    badgeWrap.className = "model-admin-badges";
    (badges || []).forEach((item) => badgeWrap.appendChild(badge(item.text, item.tone)));
    (actions || []).forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = item.text;
      button.dataset.uiRole = item.role || "secondary";
      button.disabled = item.disabled === true;
      button.addEventListener("click", item.onClick);
      badgeWrap.appendChild(button);
    });
    el.appendChild(name);
    el.appendChild(detail);
    el.appendChild(badgeWrap);
    return el;
  }

  function modelActionKey(backend, model) {
    return `${String(backend || "").trim()}:${String(model || "").trim()}`;
  }

  function setModelActionStatus(backend, model, text, tone = "yellow") {
    const key = modelActionKey(backend, model);
    if (!String(text || "").trim()) {
      modelActionStatus.delete(key);
      return;
    }
    modelActionStatus.set(key, { text: String(text), tone });
  }

  function group(title) {
    const el = document.createElement("div");
    el.className = "model-admin-group";
    const heading = document.createElement("div");
    heading.className = "model-admin-title";
    heading.textContent = title;
    el.appendChild(heading);
    return el;
  }

  function stateValue(value) {
    const text = String(value || "").trim();
    return text;
  }

  function stateTone(text) {
    const value = String(text || "").trim().toLowerCase();
    if (!value) return "";
    if (/(^|\s)(fail|failed|error|not ready|not selectable|hidden|missing|stalled|stopped|unknown|unavailable)(\s|$)/.test(value)) return "red";
    if (/(^|\s)(fetch|purge|requested|cache only|not_advertised)(\s|$)/.test(value)) return "yellow";
    if (/(^|\s)(ok|pass|passed|ready|available|visible|advertised|selectable|cached|downloading|in progress)(\s|$)/.test(value)) return "green";
    return "neutral";
  }

  function stateCell(text, tone, href) {
    const label = stateValue(text);
    if (!label) return "";
    return { kind: "state", text: label, tone: tone || stateTone(label), href: href || "" };
  }

  function fetchProgressCell(activity, cacheState) {
    if (!activity || typeof activity !== "object") {
      return cacheState === "fetching" ? stateCell("fetch state unknown", "red") : "";
    }
    const downloaded = Number(activity.downloaded_shards);
    const expected = Number(activity.expected_shards);
    const attempt = Number(activity.attempt);
    const maxAttempts = Number(activity.max_attempts);
    const details = [];
    if (Number.isFinite(downloaded) && Number.isFinite(expected) && expected > 0) {
      details.push(`${downloaded}/${expected} shards`);
    }
    if (Number.isFinite(attempt) && Number.isFinite(maxAttempts) && maxAttempts > 0) {
      details.push(`attempt ${attempt}/${maxAttempts}`);
    }
    if (activity.job_state === "retry_wait" && Number(activity.next_retry_at) > 0) {
      details.push(`retry ${formatTimestamp(Number(activity.next_retry_at))}`);
    }
    if (activity.error) details.push(String(activity.error).slice(0, 180));
    const rawPercent = activity.progress_percent;
    const percent = rawPercent === null || rawPercent === undefined ? Number.NaN : Number(rawPercent);
    let tone = "neutral";
    if (activity.status === "failed" || activity.status === "stalled") tone = "red";
    else if (activity.status === "complete") tone = "green";
    else if (activity.status === "active") tone = "yellow";
    return {
      kind: "download-progress",
      text: activity.label || activity.status || "download",
      tone,
      details: details.join(" · "),
      percent,
    };
  }

  function sanitizeIdPart(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  function aliasAnchorId(aliasName) {
    const clean = sanitizeIdPart(aliasName);
    return clean ? `alias-row-${clean}` : "";
  }

  function aliasLinksCell(aliasValues) {
    const links = [];
    (Array.isArray(aliasValues) ? aliasValues : []).forEach((entry) => {
      const text = String(entry || "").trim();
      if (!text) return;
      const baseAlias = text.replace(/\s+\(fallback\)$/i, "").trim();
      const id = aliasAnchorId(baseAlias);
      links.push({ text, href: id ? `#${id}` : "" });
    });
    return links.length ? { kind: "links", items: links } : "";
  }

  function toolResultCell(result) {
    if (!result || typeof result !== "object") return "";
    const passed = Number(result.passed);
    const total = Number(result.total);
    if (!Number.isFinite(total) || total <= 0) return "";
    const ok = Number.isFinite(passed) ? passed : 0;
    return stateCell(`${ok}/${total}`, result.ok === true ? "green" : "red", "#model_tool_qualification_section");
  }

  function benchmarkResultCell(result) {
    if (!result || typeof result !== "object") return "";
    const text = stripPrefix(benchmarkText(result), "latest benchmark:");
    if (!text) return "";
    return stateCell(text, "neutral", "#model_benchmark_start");
  }

  function isEmbeddingModel(model) {
    const backend = String(model?.backend || "").toLowerCase();
    const name = String(model?.model || "").toLowerCase();
    const provider = String(model?.provider || "").toLowerCase();
    return backend.includes("embedding") || name.includes("embedding") || provider.includes("embedding");
  }

  function splitModels(models) {
    const llm = [];
    const nonLlm = [];
    (models || []).forEach((model) => {
      if (model?.chat_candidate === true && !isEmbeddingModel(model)) llm.push(model);
      else nonLlm.push(model);
    });
    return { llm, nonLlm };
  }

  function stripPrefix(text, prefix) {
    const source = String(text || "");
    return source.startsWith(prefix) ? source.slice(prefix.length).trim() : source;
  }

  function addClassTokens(el, className) {
    const tokens = String(className || "")
      .split(/\s+/)
      .map((token) => token.trim())
      .filter(Boolean);
    if (tokens.length) el.classList.add(...tokens);
  }

  function tableGroup(title, columns, rows) {
    const wrap = group(title);
    const tableWrap = document.createElement("div");
    tableWrap.className = "model-admin-table-wrap";
    const table = document.createElement("table");
    table.className = "model-admin-table";
    const normalizedColumns = (columns || []).map((column) => (typeof column === "string" ? { label: column } : column || { label: "" }));

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    normalizedColumns.forEach((column) => {
      const th = document.createElement("th");
      th.textContent = column.label || "";
      if (column.className) addClassTokens(th, column.className);
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    (rows || []).forEach((cells) => {
      const tr = document.createElement("tr");
      cells.forEach((cellValue, index) => {
        const td = document.createElement("td");
        const column = normalizedColumns[index] || {};
        if (column.className) addClassTokens(td, column.className);
        if (column.type === "actions" && Array.isArray(cellValue)) {
          if (!cellValue.length) {
            td.textContent = "";
          } else {
            cellValue.forEach((item) => {
              const button = document.createElement("button");
              button.type = "button";
              button.textContent = item.text;
              button.dataset.uiRole = item.role || "secondary";
              button.disabled = item.disabled === true;
              button.addEventListener("click", item.onClick);
              td.appendChild(button);
            });
          }
        } else if (cellValue && typeof cellValue === "object" && cellValue.kind === "state") {
          if (!cellValue.text) {
            td.textContent = "";
          } else if (cellValue.href) {
            const link = document.createElement("a");
            link.className = `model-admin-state ${cellValue.tone || ""}`.trim();
            link.href = cellValue.href;
            link.textContent = cellValue.text;
            td.appendChild(link);
          } else {
            const chip = document.createElement("span");
            chip.className = `model-admin-state ${cellValue.tone || ""}`.trim();
            chip.textContent = cellValue.text;
            td.appendChild(chip);
          }
        } else if (cellValue && typeof cellValue === "object" && cellValue.kind === "download-progress") {
          const wrap = document.createElement("div");
          wrap.className = "model-admin-fetch";
          const chip = document.createElement("span");
          chip.className = `model-admin-state ${cellValue.tone || ""}`.trim();
          chip.textContent = cellValue.text || "download";
          wrap.appendChild(chip);
          if (cellValue.details) {
            const details = document.createElement("div");
            details.className = "model-admin-fetch-detail";
            details.textContent = cellValue.details;
            wrap.appendChild(details);
          }
          if (Number.isFinite(cellValue.percent)) {
            const track = document.createElement("div");
            track.className = "model-admin-progress model-admin-fetch-progress";
            const fill = document.createElement("span");
            fill.style.width = `${Math.max(0, Math.min(100, cellValue.percent)).toFixed(1)}%`;
            track.appendChild(fill);
            wrap.appendChild(track);
          }
          td.appendChild(wrap);
        } else if (cellValue && typeof cellValue === "object" && cellValue.kind === "anchor-text") {
          const text = String(cellValue.text || "");
          td.textContent = text;
          if (cellValue.id) td.id = String(cellValue.id);
        } else if (cellValue && typeof cellValue === "object" && cellValue.kind === "links") {
          const items = Array.isArray(cellValue.items) ? cellValue.items : [];
          items.forEach((item, itemIndex) => {
            const label = String(item?.text || "").trim();
            if (!label) return;
            if (itemIndex > 0) td.appendChild(document.createTextNode(", "));
            if (item?.href) {
              const link = document.createElement("a");
              link.href = String(item.href);
              link.textContent = label;
              td.appendChild(link);
            } else {
              td.appendChild(document.createTextNode(label));
            }
          });
        } else {
          td.textContent = stateValue(cellValue);
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    wrap.appendChild(tableWrap);
    updateScrollHintState(tableWrap);
    tableWrap.addEventListener("scroll", () => updateScrollHintState(tableWrap), { passive: true });
    return wrap;
  }

  function updateScrollHintState(scroller) {
    if (!scroller) return;
    const max = Math.max(0, scroller.scrollWidth - scroller.clientWidth);
    const left = scroller.scrollLeft || 0;
    scroller.classList.toggle("has-overflow-left", left > 0);
    scroller.classList.toggle("has-overflow-right", max > 1 && left < max - 1);
  }

  function refreshAllScrollHints() {
    document.querySelectorAll(".model-admin-table-wrap").forEach((node) => updateScrollHintState(node));
  }

  function benchmarkSelectedModels() {
    if (!benchmarkEls.models) return [];
    return Array.from(benchmarkEls.models.selectedOptions || [])
      .map((option) => String(option.value || "").trim())
      .filter(Boolean);
  }

  function toolQualificationSelectedModels() {
    if (!toolEls.models) return [];
    return Array.from(toolEls.models.selectedOptions || [])
      .map((option) => String(option.value || "").trim())
      .filter(Boolean);
  }

  function numericInput(el, fallback, min, max) {
    const value = Number(el?.value);
    if (!Number.isFinite(value)) return fallback;
    return Math.max(min, Math.min(max, value));
  }

  function appendBenchmarkOptionGroup(select, label, items) {
    if (!items.length) return;
    const groupEl = document.createElement("optgroup");
    groupEl.label = label;
    items.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      groupEl.appendChild(option);
    });
    select.appendChild(groupEl);
  }

  function populateBenchmarkModels(payload) {
    const select = benchmarkEls.models;
    if (!select) return;
    const previous = new Set(benchmarkSelectedModels());
    const aliases = Array.isArray(payload?.aliases) ? payload.aliases : [];
    const models = Array.isArray(payload?.models) ? payload.models : [];

    const selectableAliasNames = new Set();
    models.forEach((model) => {
      if (model.selectable !== true || !Array.isArray(model.aliases)) return;
      model.aliases.forEach((aliasName) => {
        const clean = String(aliasName || "").replace(/\s+\(fallback\)$/, "").trim();
        if (clean) selectableAliasNames.add(clean);
      });
    });

    const aliasOptions = [];
    visibleBenchmarkAliases = [];
    aliases.forEach((alias) => {
      const value = String(alias.alias || "").trim();
      if (!value || alias.visible === false || alias.unavailable_reason || !selectableAliasNames.has(value)) return;
      visibleBenchmarkAliases.push(value);
      const backend = alias.effective_backend || alias.backend || "";
      const resolved = alias.effective_model || alias.configured_model || "";
      aliasOptions.push({ value, label: `${value} -> ${backend}:${shortModel(resolved)}` });
    });

    const seen = new Set(aliasOptions.map((item) => item.value));
    const modelOptions = [];
    models.forEach((model) => {
      if (model.selectable !== true) return;
      const backend = String(model.backend || "").trim();
      const modelName = String(model.model || "").trim();
      if (!backend || !modelName) return;
      const value = `${backend}:${modelName}`;
      if (seen.has(value)) return;
      seen.add(value);
      const aliasText = Array.isArray(model.aliases) && model.aliases.length ? ` aliases: ${model.aliases.join(", ")}` : "";
      modelOptions.push({ value, label: `${backend}:${shortModel(modelName)}${aliasText}` });
    });

    select.innerHTML = "";
    appendBenchmarkOptionGroup(select, "Aliases", aliasOptions);
    appendBenchmarkOptionGroup(select, "Concrete models", modelOptions);

    const wanted = previous.size ? previous : new Set(["fast", "default", "coder"].filter((item) => visibleBenchmarkAliases.includes(item)));
    Array.from(select.options || []).forEach((option) => {
      option.selected = wanted.has(option.value);
    });
  }

  function populateToolQualificationModels(payload) {
    const select = toolEls.models;
    if (!select) return;
    const previous = new Set(toolQualificationSelectedModels());
    const aliases = Array.isArray(payload?.aliases) ? payload.aliases : [];
    const models = Array.isArray(payload?.models) ? payload.models : [];

    const selectableAliasNames = new Set();
    models.forEach((model) => {
      if (model.selectable !== true || !Array.isArray(model.aliases)) return;
      model.aliases.forEach((aliasName) => {
        const clean = String(aliasName || "").replace(/\s+\(fallback\)$/, "").trim();
        if (clean) selectableAliasNames.add(clean);
      });
    });

    const aliasOptions = [];
    visibleToolAliases = [];
    aliases.forEach((alias) => {
      const value = String(alias.alias || "").trim();
      if (!value || alias.visible === false || alias.unavailable_reason || alias.tools === false || !selectableAliasNames.has(value)) return;
      visibleToolAliases.push(value);
      const backend = alias.effective_backend || alias.backend || "";
      const resolved = alias.effective_model || alias.configured_model || "";
      aliasOptions.push({ value, label: `${value} -> ${backend}:${shortModel(resolved)}` });
    });

    const seen = new Set(aliasOptions.map((item) => item.value));
    const modelOptions = [];
    models.forEach((model) => {
      if (model.selectable !== true) return;
      const backend = String(model.backend || "").trim();
      const modelName = String(model.model || "").trim();
      if (!backend || !modelName) return;
      const value = `${backend}:${modelName}`;
      if (seen.has(value)) return;
      seen.add(value);
      const aliasText = Array.isArray(model.aliases) && model.aliases.length ? ` aliases: ${model.aliases.join(", ")}` : "";
      modelOptions.push({ value, label: `${backend}:${shortModel(modelName)}${aliasText}` });
    });

    select.innerHTML = "";
    appendBenchmarkOptionGroup(select, "Tool aliases", aliasOptions);
    appendBenchmarkOptionGroup(select, "Concrete models", modelOptions);

    const wanted = previous.size ? previous : new Set(["fast-reasoning", "fast", "coder", "default"].filter((item) => visibleToolAliases.includes(item)));
    Array.from(select.options || []).forEach((option) => {
      option.selected = wanted.has(option.value);
    });
  }

  function renderBenchmarkSummary(payload) {
    const target = benchmarkEls.results;
    if (!target) return;
    const summary = Array.isArray(payload?.summary) ? payload.summary : [];
    if (!summary.length) {
      target.innerHTML = '<div class="hint">No benchmark results yet.</div>';
      return;
    }

    const table = document.createElement("table");
    table.className = "model-admin-table";
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["Model", "OK", "Tok/s avg", "Tok/s min", "Decode tok/s", "TTFT", "Tokens", "Backend", "Resolved model", "Error"].forEach((label) => {
      const th = document.createElement("th");
      th.textContent = label;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    summary.forEach((rowItem) => {
      const tr = document.createElement("tr");
      const values = [
        rowItem.model || "",
        `${rowItem.ok || 0}/${rowItem.runs || 0}`,
        formatMetric(rowItem.tokens_per_sec_avg, ""),
        formatMetric(rowItem.tokens_per_sec_min, ""),
        formatMetric(rowItem.decode_tokens_per_sec_avg, ""),
        formatMetric(rowItem.time_to_first_token_ms_avg, "ms"),
        formatMetric(rowItem.completion_tokens_avg, ""),
        rowItem.backend || "",
        rowItem.resolved_model || "",
        rowItem.error || "",
      ];
      values.forEach((value) => {
        const td = document.createElement("td");
        td.textContent = String(value || "-");
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    target.innerHTML = "";
    const caption = document.createElement("div");
    caption.className = "hint";
    const completed = formatTimestamp(Number(payload?.generated_at || payload?.completed_at || 0));
    caption.textContent = payload?.run_id ? `Run ${payload.run_id}${completed ? ` · ${completed}` : ""}` : completed;
    target.appendChild(caption);
    target.appendChild(table);
  }

  function renderBenchmarkHistory(payload) {
    const recent = Array.isArray(payload?.benchmarks) ? payload.benchmarks : [];
    if (recent.length) {
      renderBenchmarkSummary(recent[0]);
    } else {
      renderBenchmarkSummary(null);
    }
  }

  function renderToolQualificationSummary(payload) {
    const target = toolEls.results;
    if (!target) return;
    const summary = Array.isArray(payload?.summary) ? payload.summary : [];
    if (!summary.length) {
      target.innerHTML = '<div class="hint">No tool qualification results yet.</div>';
      return;
    }

    const table = document.createElement("table");
    table.className = "model-admin-table";
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["Model", "OK", "Auto", "Required", "Named", "Stream", "Roundtrip", "Backend", "Resolved model", "Failure"].forEach((label) => {
      const th = document.createElement("th");
      th.textContent = label;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    summary.forEach((rowItem) => {
      const tr = document.createElement("tr");
      const values = [
        rowItem.model || "",
        `${rowItem.passed || 0}/${rowItem.total || 0}`,
        categoryText("", rowItem.auto).trim() || "-",
        categoryText("", rowItem.required).trim() || "-",
        categoryText("", rowItem.named).trim() || "-",
        categoryText("", rowItem.stream).trim() || "-",
        categoryText("", rowItem.roundtrip).trim() || "-",
        rowItem.backend || "",
        rowItem.resolved_model || "",
        rowItem.error || "",
      ];
      values.forEach((value) => {
        const td = document.createElement("td");
        td.textContent = String(value || "-");
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    target.innerHTML = "";
    const caption = document.createElement("div");
    caption.className = "hint";
    const completed = formatTimestamp(Number(payload?.generated_at || payload?.completed_at || 0));
    caption.textContent = payload?.run_id ? `Run ${payload.run_id}${completed ? ` · ${completed}` : ""}` : completed;
    target.appendChild(caption);
    target.appendChild(table);
  }

  function renderToolQualificationHistory(payload) {
    const recent = Array.isArray(payload?.tool_qualifications) ? payload.tool_qualifications : [];
    if (recent.length) {
      renderToolQualificationSummary(recent[0]);
    } else {
      renderToolQualificationSummary(null);
    }
  }

  function scheduleHugeLanePoll(active) {
    if (!active) {
      if (hugeLanePollTimer) window.clearTimeout(hugeLanePollTimer);
      hugeLanePollTimer = null;
      return;
    }
    if (hugeLanePollTimer) return;
    hugeLanePollTimer = window.setTimeout(() => {
      hugeLanePollTimer = null;
      void load();
    }, 5000);
  }

  function scheduleModelFetchPoll(active) {
    if (!active) {
      if (modelFetchPollTimer) window.clearTimeout(modelFetchPollTimer);
      modelFetchPollTimer = null;
      return;
    }
    if (modelFetchPollTimer) return;
    modelFetchPollTimer = window.setTimeout(() => {
      modelFetchPollTimer = null;
      void load();
    }, 5000);
  }

  function renderHugeLane(lane) {
    if (!lane || lane.enabled === false) {
      scheduleHugeLanePoll(false);
      return;
    }
    const hugeGroup = group("MLX Huge Model");
    hugeGroup.classList.add("model-admin-huge");
    const statusBadges = [
      {
        text: lane.status_label || lane.status || "unknown",
        tone: lane.status === "ready" ? "green" : lane.status === "switching" ? "yellow" : "red",
      },
    ];
    if (lane.target_model) statusBadges.push({ text: `target ${lane.target_model}`, tone: "yellow" });
    if (lane.error) statusBadges.push({ text: String(lane.error).slice(0, 120), tone: "red" });
    hugeGroup.appendChild(row(lane.active_model || "No active huge model", lane.route_model ? `routing ${lane.route_model}` : "", statusBadges));

    if (lane.status === "switching") {
      const progress = document.createElement("div");
      const elapsed = Number(lane.elapsed_sec || 0);
      const estimate = Math.max(1, Number(lane.estimated_load_sec || 120));
      progress.className = "model-admin-muted";
      progress.textContent = `Loading ${formatDuration(elapsed)} / ${formatDuration(estimate)}`;
      const track = document.createElement("div");
      track.className = "model-admin-progress";
      const fill = document.createElement("span");
      fill.style.width = `${Math.max(0, Math.min(100, (elapsed / estimate) * 100)).toFixed(0)}%`;
      track.appendChild(fill);
      progress.appendChild(track);
      hugeGroup.appendChild(progress);
    }

    const candidates = Array.isArray(lane.candidates) ? lane.candidates : [];
    candidates.forEach((candidate) => {
      const badges = [];
      if (candidate.active) badges.push({ text: "active", tone: "green" });
      if (candidate.target) badges.push({ text: "loading", tone: "yellow" });
      if (candidate.cache_state) badges.push({ text: candidate.cache_state, tone: candidate.cache_state === "cached" ? "green" : "yellow" });
      if (candidate.switchable === false) badges.push({ text: "not resident-compatible", tone: "red" });
      const actions = [];
      if (!candidate.active && !candidate.target && candidate.switchable !== false) {
        actions.push({
          text: "Switch",
          role: "secondary",
          disabled: candidate.cache_state !== "cached" || lane.status === "switching",
          onClick: () => void switchHugeLane(candidate.model, false),
        });
      }
      const details = [candidate.model];
      if (candidate.estimated_load_sec) details.push(`est ${formatDuration(candidate.estimated_load_sec)}`);
      if (candidate.estimated_memory_gb) details.push(`~${candidate.estimated_memory_gb} GB`);
      hugeGroup.appendChild(row(candidate.label || candidate.model || "unknown", details.join(" · "), badges, actions));
    });
    listEl.appendChild(hugeGroup);
    scheduleHugeLanePoll(lane.status === "switching");
  }

  function render(payload) {
    if (!listEl) return;
    listEl.innerHTML = "";
    if (statusEl) {
      const updated = formatTimestamp(Number(payload?.generated_at));
      statusEl.textContent = updated ? `Updated ${updated}` : "";
    }

    const aliases = Array.isArray(payload?.aliases) ? payload.aliases : [];
    const models = Array.isArray(payload?.models) ? payload.models : [];
    const backends = Array.isArray(payload?.backends) ? payload.backends : [];

    populateBenchmarkModels(payload);
    populateToolQualificationModels(payload);
    renderBenchmarkHistory(payload);
    renderToolQualificationHistory(payload);

    const backendByName = new Map(backends.map((item) => [String(item.backend || ""), item]));

    const backendGroup = group("Backends");
    if (!backends.length) {
      backendGroup.appendChild(row("None", "", []));
    } else {
      backends.forEach((backend) => {
        const badges = [
          {
            text: backend.ready === true ? "ready" : backend.ready === false ? "not ready" : "unknown",
            tone: backend.ready === true ? "green" : backend.ready === false ? "red" : "yellow",
          },
        ];
        if (backend.error) badges.push({ text: String(backend.error).slice(0, 80), tone: "red" });
        backendGroup.appendChild(row(backend.backend || "", backend.hostname || backend.base_url || "", badges));
      });
    }
    listEl.appendChild(backendGroup);

    renderHugeLane(payload?.mlx_huge_lane || null);

    const aliasColumns = [
      { label: "Alias", className: "col-key" },
      { label: "Target" },
      { label: "Visible" },
      { label: "Availability" },
      { label: "Benchmark result" },
      { label: "Tool qualification result" },
      { label: "Actions", type: "actions" },
    ];
    const aliasRows = aliases.length
      ? aliases.map((alias) => {
          const effective = `${alias.effective_backend || alias.backend || ""}:${alias.effective_model || ""}`;
          return [
            { kind: "anchor-text", text: alias.alias || "", id: aliasAnchorId(alias.alias || "") },
            effective,
            stateCell(alias.visible ? "visible" : "hidden", alias.visible ? "green" : "red"),
            alias.unavailable_reason ? stateCell(alias.unavailable_reason, "red") : stateCell("available", "green"),
            benchmarkResultCell(alias.benchmark_latest),
            toolResultCell(alias.tool_qualification_latest),
            [],
          ];
        })
      : [["None", "", "", "", "", "", []]];
    listEl.appendChild(tableGroup("Aliases", aliasColumns, aliasRows));

    const modelColumns = [
      { label: "Model", className: "model-col-model col-key" },
      { label: "Aliases" },
      { label: "Selectable", className: "col-state" },
      { label: "Advertised", className: "col-state" },
      { label: "Cache", className: "col-state" },
      { label: "Fetch", className: "col-state" },
      { label: "Availability", className: "col-state" },
      { label: "Tool qualification result", className: "col-tool-result" },
      { label: "Benchmark result" },
      { label: "Action status", className: "col-state" },
      { label: "Actions", type: "actions" },
    ];

    function modelRow(model) {
          const activity = model.fetch_activity && typeof model.fetch_activity === "object" ? model.fetch_activity : null;
          const fetchState = fetchProgressCell(activity, model.cache_state);

          const availabilityParts = [];
          if (model.unavailable_reason) availabilityParts.push(String(model.unavailable_reason));
          if (model.cache_only) availabilityParts.push("cache only");
          const availability = availabilityParts.join(" · ");

          const aliasCell = aliasLinksCell(model.aliases);
          const actionStatus = modelActionStatus.get(modelActionKey(model.backend || "local_mlx", model.model || ""));
          const actions = [];
          if (model.provider === "mlx" && ((model.cache_state === "fetching" && activity?.status !== "active") || activity?.status === "failed")) {
            actions.push({
              text: "Restart fetch",
              role: "secondary",
              onClick: () => void restartFetch(model.backend || "local_mlx", model.model || ""),
            });
          }
          if (model.provider === "mlx") {
            actions.push({
              text: "Re-download",
              role: "secondary",
              disabled: activity?.status === "active",
              onClick: () => void redownloadModelCache(model.backend || "local_mlx", model.model || ""),
            });
            actions.push({
              text: "Purge cache",
              role: "secondary",
              onClick: () => void purgeModelCache(model.backend || "local_mlx", model.model || ""),
            });
          }
          return [
            model.model || "",
            aliasCell,
            model.selectable ? stateCell("selectable", "green") : stateCell("not selectable", "red"),
            model.advertised ? stateCell("advertised", "green") : "",
            model.cache_state ? stateCell(model.cache_state, stateTone(model.cache_state)) : "",
            fetchState,
            availability ? stateCell(availability, stateTone(availability)) : stateCell("available", "green"),
            toolResultCell(model.tool_qualification_latest),
            benchmarkResultCell(model.benchmark_latest),
            actionStatus?.text ? stateCell(actionStatus.text, actionStatus.tone || stateTone(actionStatus.text)) : "",
            actions,
          ];
    }

    const separated = splitModels(models);
    const llmByBackend = new Map();
    separated.llm.forEach((model) => {
      const key = String(model.backend || "");
      if (!llmByBackend.has(key)) llmByBackend.set(key, []);
      llmByBackend.get(key).push(model);
    });

    if (!llmByBackend.size) {
      listEl.appendChild(tableGroup("LLM Models", modelColumns, [["None", "", "", "", "", "", "", "", "", "", []]]));
    } else {
      Array.from(llmByBackend.keys())
        .sort((a, b) => a.localeCompare(b))
        .forEach((backendName) => {
          const backendInfo = backendByName.get(backendName) || {};
          const host = String(backendInfo.hostname || backendInfo.base_url || "").trim();
          const title = host ? `LLM Models - ${backendName} (${host})` : `LLM Models - ${backendName}`;
          const rows = (llmByBackend.get(backendName) || []).map((model) => modelRow(model));
          listEl.appendChild(tableGroup(title, modelColumns, rows));
        });
    }

    const nonLlmByBackend = new Map();
    separated.nonLlm.forEach((model) => {
      const key = String(model.backend || "");
      if (!nonLlmByBackend.has(key)) nonLlmByBackend.set(key, []);
      nonLlmByBackend.get(key).push(model);
    });
    if (nonLlmByBackend.size) {
      Array.from(nonLlmByBackend.keys())
        .sort((a, b) => a.localeCompare(b))
        .forEach((backendName) => {
          const backendInfo = backendByName.get(backendName) || {};
          const host = String(backendInfo.hostname || backendInfo.base_url || "").trim();
          const title = host ? `Non-LLM / Embeddings - ${backendName} (${host})` : `Non-LLM / Embeddings - ${backendName}`;
          const rows = (nonLlmByBackend.get(backendName) || []).map((model) => modelRow(model));
          listEl.appendChild(tableGroup(title, modelColumns, rows));
        });
    }
      const hasActiveFetch = models.some((model) => {
        const activity = model?.fetch_activity;
        return activity?.status === "active" || activity?.job_state === "retry_wait";
      });
      scheduleModelFetchPoll(hasActiveFetch);
      refreshAllScrollHints();
  }

  async function load() {
    if (statusEl) statusEl.textContent = "Loading...";
    if (refreshEl) refreshEl.disabled = true;
    try {
      const resp = await fetch("/ui/api/admin/models", { method: "GET", credentials: "same-origin" });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        if (statusEl) statusEl.textContent = resp.status === 403 ? "Admin required." : `HTTP ${resp.status}: ${text}`;
        if (listEl) listEl.innerHTML = "";
        return;
      }
      render(JSON.parse(text));
    } catch (error) {
      if (statusEl) statusEl.textContent = `Error: ${String(error)}`;
    } finally {
      if (refreshEl) refreshEl.disabled = false;
    }
  }

  async function restartFetch(backend, model) {
    if (!model) return;
    if (statusEl) statusEl.textContent = `Starting prefetch for ${model}...`;
    try {
      const resp = await fetch("/ui/api/admin/models/prefetch", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ backend, model }),
      });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        if (statusEl) statusEl.textContent = `Prefetch restart failed: HTTP ${resp.status}: ${text}`;
        return;
      }
      const payload = JSON.parse(text);
      if (statusEl) statusEl.textContent = `Prefetch started for ${model}${payload.pid ? ` (pid ${payload.pid})` : ""}.`;
      window.setTimeout(() => void load(), 1500);
    } catch (error) {
      if (statusEl) statusEl.textContent = `Prefetch restart failed: ${String(error)}`;
    }
  }

  async function redownloadModelCache(backend, model) {
    if (!model) return;
    const ok = window.confirm(`Erase all cached files for ${model} and download it again? This can transfer hundreds of gigabytes.`);
    if (!ok) return;
    if (statusEl) statusEl.textContent = `Starting re-download for ${model}...`;
    setModelActionStatus(backend, model, "re-download requested", "yellow");
    try {
      const resp = await fetch("/ui/api/admin/models/redownload", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ backend, model }),
      });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        if (statusEl) statusEl.textContent = `Re-download failed: HTTP ${resp.status}: ${text}`;
        return;
      }
      const payload = JSON.parse(text);
      if (statusEl) statusEl.textContent = `Re-download started for ${model}${payload.pid ? ` (pid ${payload.pid})` : ""}.`;
      setModelActionStatus(backend, model, "re-download in progress", "green");
      window.setTimeout(() => void load(), 1500);
    } catch (error) {
      if (statusEl) statusEl.textContent = `Re-download failed: ${String(error)}`;
      setModelActionStatus(backend, model, "re-download failed", "red");
    }
  }

  async function purgeModelCache(backend, model) {
    if (!model) return;
    const ok = window.confirm(`Purge cached files for ${model}? This removes the local Hugging Face cache and may force a full re-download.`);
    if (!ok) return;
    if (statusEl) statusEl.textContent = `Purging cache for ${model}...`;
    setModelActionStatus(backend, model, "purge requested", "yellow");
    try {
      const resp = await fetch("/ui/api/admin/models/purge", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ backend, model }),
      });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        if (statusEl) statusEl.textContent = `Cache purge failed: HTTP ${resp.status}: ${text}`;
        return;
      }
      const payload = JSON.parse(text);
      const removed = Array.isArray(payload.removed_paths) ? payload.removed_paths.length : 0;
      if (statusEl) statusEl.textContent = `Purged cache for ${model}${removed ? ` (${removed} path${removed === 1 ? "" : "s"})` : ""}.`;
      setModelActionStatus(backend, model, "cache purged", "green");
      window.setTimeout(() => void load(), 1000);
    } catch (error) {
      if (statusEl) statusEl.textContent = `Cache purge failed: ${String(error)}`;
      setModelActionStatus(backend, model, "purge failed", "red");
    }
  }

  function benchmarkRequestBody() {
    const models = benchmarkSelectedModels();
    if (!models.length) throw new Error("Select at least one model.");
    return {
      models,
      max_tokens: numericInput(benchmarkEls.maxTokens, 512, 1, 4096),
      runs: numericInput(benchmarkEls.runs, 3, 1, 10),
      warmup_runs: numericInput(benchmarkEls.warmups, 1, 0, 3),
      temperature: numericInput(benchmarkEls.temperature, 0.2, 0, 2),
      top_p: numericInput(benchmarkEls.topP, 0.95, 0.01, 1),
      prompt: String(benchmarkEls.prompt?.value || "").trim(),
      stream: true,
    };
  }

  async function runBenchmark() {
    if (!benchmarkEls.start) return;
    benchmarkEls.start.disabled = true;
    if (benchmarkEls.status) benchmarkEls.status.textContent = "Benchmark running...";
    try {
      const body = benchmarkRequestBody();
      const resp = await fetch("/ui/api/admin/models/benchmark", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        let detail = text;
        try {
          const parsed = JSON.parse(text);
          detail = parsed?.detail || text;
        } catch (error) {
          detail = text;
        }
        throw new Error(`HTTP ${resp.status}: ${detail}`);
      }
      const payload = JSON.parse(text);
      renderBenchmarkSummary(payload);
      if (benchmarkEls.status) benchmarkEls.status.textContent = `Benchmark complete: ${payload.run_id || ""}`;
    } catch (error) {
      if (benchmarkEls.status) benchmarkEls.status.textContent = `Benchmark failed: ${String(error?.message || error)}`;
    } finally {
      benchmarkEls.start.disabled = false;
    }
  }

  function toolQualificationRequestBody() {
    const models = toolQualificationSelectedModels();
    if (!models.length) throw new Error("Select at least one model.");
    return {
      models,
      max_tokens: numericInput(toolEls.maxTokens, 96, 1, 512),
      temperature: numericInput(toolEls.temperature, 0, 0, 2),
      include_stream: true,
      include_roundtrip: true,
    };
  }

  async function runToolQualification() {
    if (!toolEls.start) return;
    toolEls.start.disabled = true;
    if (toolEls.status) toolEls.status.textContent = "Tool qualification running...";
    try {
      const body = toolQualificationRequestBody();
      const resp = await fetch("/ui/api/admin/models/tool-qualification", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        let detail = text;
        try {
          const parsed = JSON.parse(text);
          detail = parsed?.detail || text;
        } catch (error) {
          detail = text;
        }
        throw new Error(`HTTP ${resp.status}: ${detail}`);
      }
      const payload = JSON.parse(text);
      renderToolQualificationSummary(payload);
      if (toolEls.status) toolEls.status.textContent = `Tool qualification complete: ${payload.run_id || ""}`;
      window.setTimeout(() => void load(), 1000);
    } catch (error) {
      if (toolEls.status) toolEls.status.textContent = `Tool qualification failed: ${String(error?.message || error)}`;
    } finally {
      toolEls.start.disabled = false;
    }
  }

  function selectVisibleAliases() {
    const wanted = new Set(visibleBenchmarkAliases);
    Array.from(benchmarkEls.models?.options || []).forEach((option) => {
      option.selected = wanted.has(option.value);
    });
  }

  function selectToolAliases() {
    const wanted = new Set(visibleToolAliases);
    Array.from(toolEls.models?.options || []).forEach((option) => {
      option.selected = wanted.has(option.value);
    });
  }

  async function switchHugeLane(model, confirmed) {
    const modelId = String(model || "").trim();
    if (!modelId) return;
    if (statusEl) statusEl.textContent = `Switching MLX huge model to ${modelId}...`;
    try {
      const resp = await fetch("/ui/api/mlx/huge-lane/switch", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelId, confirmed }),
      });
      if (handle401(resp)) return;
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const detail = payload?.detail;
        const message = typeof detail === "string" ? detail : detail?.error ? `${detail.error}: ${detail.cache_state || ""}` : `HTTP ${resp.status}`;
        throw new Error(message);
      }
      if (payload?.decision === "requires_confirmation" && !confirmed) {
        const ok = window.confirm(payload.message || `Switch MLX huge model to ${modelId}?`);
        if (ok) return switchHugeLane(modelId, true);
        if (statusEl) statusEl.textContent = "MLX huge model switch cancelled.";
        return;
      }
      await load();
    } catch (error) {
      if (statusEl) statusEl.textContent = `MLX huge model switch failed: ${String(error?.message || error)}`;
    }
  }

  if (refreshEl) refreshEl.addEventListener("click", () => void load());
  if (benchmarkEls.start) benchmarkEls.start.addEventListener("click", () => void runBenchmark());
  if (benchmarkEls.selectAliases) benchmarkEls.selectAliases.addEventListener("click", selectVisibleAliases);
  if (benchmarkEls.clear) benchmarkEls.clear.addEventListener("click", () => {
    if (benchmarkEls.results) benchmarkEls.results.innerHTML = '<div class="hint">No benchmark results yet.</div>';
    if (benchmarkEls.status) benchmarkEls.status.textContent = "";
  });
  if (toolEls.start) toolEls.start.addEventListener("click", () => void runToolQualification());
  if (toolEls.selectAliases) toolEls.selectAliases.addEventListener("click", selectToolAliases);
  if (toolEls.clear) toolEls.clear.addEventListener("click", () => {
    if (toolEls.results) toolEls.results.innerHTML = '<div class="hint">No tool qualification results yet.</div>';
    if (toolEls.status) toolEls.status.textContent = "";
  });
  void load();
})();
