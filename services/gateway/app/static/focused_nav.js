(() => {
  const styleId = "nexus-focused-nav-styles";
  const apiStatusId = "focusedApiKeyStatus";
  const appLinks = [
    ["Image UI", "/ui/image"],
    ["Music UI", "/ui/music"],
    ["Video UI", "/ui/video"],
    ["PersonaPlex UI", "/ui/personaplex"],
    ["Text-to-speech", "/ui/tts"],
    ["Voice Clone", "/ui/voice-clone"],
    ["OCR / Scan", "/ui/ocr"],
    ["Coding Workspaces", "/ui/coding"],
    ["Scheduled Tasks", "/ui/tasks"],
    ["Sentinel", "/ui/sentinel"],
    ["Resources", "/ui/resources"],
  ];

  function ensureStyles() {
    if (document.getElementById(styleId)) return;
    const style = document.createElement("style");
    style.id = styleId;
    style.textContent = `
      .focused-nav-wrap { position: relative; display: inline-flex; }
      .pill {
        display: inline-flex;
        gap: 8px;
        align-items: center;
        justify-content: center;
        padding: 6px 10px;
        border: 1px solid rgba(231,237,246,0.12);
        border-radius: 999px;
        background: linear-gradient(180deg, rgba(20,26,36,0.8), rgba(14,19,28,0.8));
        color: #e7edf6;
        text-decoration: none;
      }
      .pill:hover { border-color: rgba(111,184,255,0.44); text-decoration: none; }
      main > header > :first-child { flex: 1 1 420px; min-width: 220px; }
      main > header > .row { flex: 0 0 auto; justify-content: flex-end; max-width: 62%; }
      .focused-nav-secondary {
        display: flex;
        flex-wrap: wrap;
        justify-content: flex-end;
        gap: 8px;
        margin: -4px 0 12px;
      }
      .focused-nav-icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 1.1em;
        min-width: 1.1em;
        color: #cfe7ff;
      }
      .focused-nav-menu {
        position: absolute;
        right: 0;
        top: calc(100% + 6px);
        min-width: 210px;
        z-index: 1300;
        display: none;
        padding: 6px;
        border: 1px solid rgba(231,237,246,0.14);
        border-radius: 8px;
        background: linear-gradient(180deg, rgba(13,20,30,0.98), rgba(8,12,20,0.98));
        box-shadow: 0 12px 28px rgba(0,0,0,0.58);
      }
      .focused-nav-menu[aria-hidden="false"] { display: block; }
      .focused-nav-menu a {
        display: block;
        padding: 8px 10px;
        border-radius: 6px;
        color: #e7edf6;
        text-decoration: none;
      }
      .focused-nav-menu a:hover { background: rgba(231,237,246,0.06); text-decoration: none; }
      .focused-nav-caret {
        display: inline-flex;
        width: 10px;
        height: 10px;
        margin-left: 1px;
        align-items: center;
        justify-content: center;
      }
      .focused-nav-caret::before {
        content: "";
        width: 6px;
        height: 6px;
        border-right: 2px solid #93a4ba;
        border-bottom: 2px solid #93a4ba;
        transform: rotate(45deg);
        transition: transform 0.16s ease, border-color 0.16s ease;
      }
      .pill:hover .focused-nav-caret::before { border-color: #6fb8ff; }
      .pill[aria-expanded="true"] .focused-nav-caret::before { transform: rotate(225deg); }
      .focused-nav-current { opacity: 0.72; pointer-events: none; }
      .focused-api-status {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 9px;
        border: 1px solid rgba(231,237,246,0.12);
        border-radius: 999px;
        background: rgba(231,237,246,0.04);
        color: #9fb3d6;
        font: inherit;
        font-size: 11px;
        letter-spacing: 0.02em;
        cursor: pointer;
        box-shadow: none;
      }
      .focused-api-status::before {
        content: "";
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: #8a96a8;
      }
      .focused-api-status.active {
        color: #cfe7ff;
        border-color: rgba(111,184,255,0.28);
        background: rgba(111,184,255,0.08);
      }
      .focused-api-status.active::before { background: #6fb8ff; }
      @media (max-width: 760px) {
        main > header > :first-child { flex: 0 1 auto; min-width: 0; }
        main > header > .row { width: 100%; max-width: 100%; justify-content: flex-start; }
      }
    `;
    document.head.appendChild(style);
  }

  function icon(name) {
    const span = document.createElement("span");
    span.className = "focused-nav-icon";
    span.setAttribute("aria-hidden", "true");
    const paths = {
      Chat: '<path d="M4 5.5A3.5 3.5 0 0 1 7.5 2h9A3.5 3.5 0 0 1 20 5.5v5A3.5 3.5 0 0 1 16.5 14H10l-4.5 4v-4A3.5 3.5 0 0 1 4 10.5v-5Z"/>',
      Gear: '<path d="M12 8.3A3.7 3.7 0 1 0 12 15.7A3.7 3.7 0 0 0 12 8.3Z"/><path d="M19.4 13.5a7.8 7.8 0 0 0 0-3l2-1.5-2-3.4-2.4 1a8 8 0 0 0-2.6-1.5L14 2.5h-4l-.4 2.6A8 8 0 0 0 7 6.6l-2.4-1-2 3.4 2 1.5a7.8 7.8 0 0 0 0 3l-2 1.5 2 3.4 2.4-1a8 8 0 0 0 2.6 1.5l.4 2.6h4l.4-2.6a8 8 0 0 0 2.6-1.5l2.4 1 2-3.4-2-1.5Z"/>',
      Grid: '<path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z"/>',
      Menu: '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
      Refresh: '<path d="M20 6v5h-5"/><path d="M4 18v-5h5"/><path d="M18.2 9A7 7 0 0 0 6.4 6.4L4 8.7"/><path d="M5.8 15A7 7 0 0 0 17.6 17.6L20 15.3"/>',
      Resources: '<path d="M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Z"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    };
    span.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round">${paths[name] || ""}</svg>`;
    return span;
  }

  function appendLabel(el, text) {
    const label = document.createElement("span");
    label.textContent = text;
    el.appendChild(label);
  }

  function button(text, iconText) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "pill";
    el.dataset.uiRole = "menu";
    if (iconText) el.appendChild(icon(iconText));
    appendLabel(el, text);
    return el;
  }

  function linkButton(text, href, iconText) {
    const el = document.createElement("a");
    el.className = "pill";
    el.dataset.uiRole = "nav";
    el.href = href;
    if (iconText) el.appendChild(icon(iconText));
    appendLabel(el, text);
    return el;
  }

  function currentPath() {
    return window.location.pathname.replace(/\/+$/, "");
  }

  async function exposeAdminLink(...adminLinks) {
    try {
      const resp = await fetch("/ui/api/auth/me", { method: "GET", credentials: "same-origin" });
      if (!resp.ok) return;
      const payload = await resp.json();
      if (payload?.authenticated && payload?.user?.admin) {
        adminLinks.forEach((adminLink) => {
          if (adminLink) adminLink.hidden = false;
        });
      }
    } catch (error) {}
  }

  function hasApiKey() {
    return !!(window.GatewayAuth && window.GatewayAuth.getApiKey && window.GatewayAuth.getApiKey());
  }

  function updateApiStatus(el) {
    if (!el) return;
    const active = hasApiKey();
    el.textContent = active ? "API key active" : "API key inactive";
    el.classList.toggle("active", active);
    el.title = active
      ? "A saved personal API key is active in this browser."
      : "No saved browser API key. Open Settings to create or paste one.";
  }

  function makeApiStatus() {
    const el = document.createElement("button");
    el.id = apiStatusId;
    el.type = "button";
    el.className = "focused-api-status";
    el.dataset.uiRole = "status";
    el.addEventListener("click", () => {
      window.location.href = "/ui?settings=security";
    });
    updateApiStatus(el);
    window.addEventListener("gateway-auth-changed", () => updateApiStatus(el));
    return el;
  }

  function normalizeButton(el, text, iconText) {
    if (!el) return null;
    el.classList.add("pill");
    el.textContent = "";
    if (iconText) el.appendChild(icon(iconText));
    appendLabel(el, text);
    return el;
  }

  function normalizeLink(el, text, iconText) {
    if (!el) return null;
    el.classList.add("pill");
    el.dataset.uiRole = el.dataset.uiRole || "nav";
    el.textContent = "";
    if (iconText) el.appendChild(icon(iconText));
    appendLabel(el, text);
    return el;
  }

  function isBackToChat(el) {
    if (!el || el.tagName !== "A") return false;
    const href = String(el.getAttribute("href") || "").replace(/\/+$/, "");
    return href === "/ui" && String(el.textContent || "").toLowerCase().includes("chat");
  }

  function isResourcesLink(el) {
    if (!el || el.tagName !== "A") return false;
    return String(el.getAttribute("href") || "").replace(/\/+$/, "") === "/ui/resources";
  }

  function isRefreshButton(el) {
    if (!el || el.tagName !== "BUTTON") return false;
    return String(el.textContent || "").trim().toLowerCase() === "refresh";
  }

  function ensureSecondaryRow(header) {
    let secondary = header.nextElementSibling;
    if (secondary && secondary.classList && secondary.classList.contains("focused-nav-secondary")) return secondary;
    secondary = document.createElement("div");
    secondary.className = "focused-nav-secondary";
    header.insertAdjacentElement("afterend", secondary);
    return secondary;
  }

  function init() {
    ensureStyles();
    const header = document.querySelector("main > header");
    const row = document.querySelector("main > header .row");
    if (!row || row.dataset.focusedNavInit === "1") return;
    row.dataset.focusedNavInit = "1";

    const originalItems = Array.from(row.children);
    const backToChat = originalItems.find(isBackToChat) || linkButton("Back to Chat", "/ui", "Chat");
    const originalRefresh = originalItems.find(isRefreshButton);
    const resources = originalItems.find(isResourcesLink) || linkButton("Resources", "/ui/resources", "Resources");
    const used = new Set([backToChat, originalRefresh, resources].filter(Boolean));

    const refresh = originalRefresh || button("Refresh", "Refresh");
    normalizeLink(backToChat, "Back to Chat", "Chat");
    normalizeButton(refresh, "Refresh", "Refresh");
    normalizeLink(resources, "Resources", "Resources");
    if (!originalRefresh) refresh.addEventListener("click", () => window.location.reload());
    if (currentPath() === "/ui/resources") resources.classList.add("focused-nav-current");

    const settings = linkButton("Settings", "/ui?settings=1", "Gear");
    settings.title = "Open user settings and preferences";

    const appsWrap = document.createElement("div");
    appsWrap.className = "focused-nav-wrap";
    const appsBtn = button("Apps", "Menu");
    appsBtn.setAttribute("aria-expanded", "false");
    const caret = document.createElement("span");
    caret.className = "focused-nav-caret";
    caret.setAttribute("aria-hidden", "true");
    appsBtn.appendChild(caret);

    const menu = document.createElement("div");
    menu.className = "focused-nav-menu";
    menu.setAttribute("aria-hidden", "true");
    const here = currentPath();
    appLinks.forEach(([label, href]) => {
      if (href.replace(/\/+$/, "") === here) return;
      const item = document.createElement("a");
      item.href = href;
      item.textContent = label;
      menu.appendChild(item);
    });
    const modelAdmin = document.createElement("a");
    modelAdmin.href = "/ui/admin/models";
    modelAdmin.textContent = "Model Admin";
    modelAdmin.hidden = true;
    menu.appendChild(modelAdmin);

    const admin = document.createElement("a");
    admin.href = "/ui/admin/users";
    admin.textContent = "Admin UI";
    admin.hidden = true;
    menu.appendChild(admin);

    appsWrap.appendChild(appsBtn);
    appsWrap.appendChild(menu);

    const apiStatus = document.getElementById(apiStatusId) || makeApiStatus();
    row.textContent = "";
    row.appendChild(backToChat);
    row.appendChild(refresh);
    row.appendChild(resources);
    row.appendChild(appsWrap);
    row.appendChild(settings);
    row.appendChild(apiStatus);

    const extras = originalItems.filter((item) => !used.has(item));
    if (extras.length && header) {
      const secondary = ensureSecondaryRow(header);
      extras.forEach((item) => secondary.appendChild(item));
    }

    appsBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      const expanded = appsBtn.getAttribute("aria-expanded") === "true";
      const next = !expanded;
      appsBtn.setAttribute("aria-expanded", next ? "true" : "false");
      menu.setAttribute("aria-hidden", next ? "false" : "true");
    });
    menu.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", () => {
      appsBtn.setAttribute("aria-expanded", "false");
      menu.setAttribute("aria-hidden", "true");
    });
    void exposeAdminLink(modelAdmin, admin);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
