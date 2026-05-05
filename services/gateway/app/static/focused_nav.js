(() => {
  const styleId = "nexus-focused-nav-styles";
  const appLinks = [
    ["Image UI", "/ui/image"],
    ["Music UI", "/ui/music"],
    ["Video UI", "/ui/video"],
    ["PersonaPlex UI", "/ui/personaplex"],
    ["Text-to-speech", "/ui/tts"],
    ["Voice Clone", "/ui/voice-clone"],
    ["OCR / Scan", "/ui/ocr"],
    ["Coding Workspaces", "/ui/coding"],
    ["Resources", "/ui/resources"],
  ];

  function ensureStyles() {
    if (document.getElementById(styleId)) return;
    const style = document.createElement("style");
    style.id = styleId;
    style.textContent = `
      .focused-nav-wrap { position: relative; display: inline-flex; }
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
      .focused-nav-caret { color: #93a4ba; font-size: 12px; margin-left: 2px; }
    `;
    document.head.appendChild(style);
  }

  function button(text) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "pill";
    el.dataset.uiRole = "menu";
    el.textContent = text;
    return el;
  }

  function linkButton(text, href) {
    const el = document.createElement("a");
    el.className = "pill";
    el.dataset.uiRole = "nav";
    el.href = href;
    el.textContent = text;
    return el;
  }

  function currentPath() {
    return window.location.pathname.replace(/\/+$/, "");
  }

  async function exposeAdminLink(adminLink) {
    try {
      const resp = await fetch("/ui/api/auth/me", { method: "GET", credentials: "same-origin" });
      if (!resp.ok) return;
      const payload = await resp.json();
      if (payload?.authenticated && payload?.user?.admin) {
        adminLink.hidden = false;
      }
    } catch (error) {}
  }

  function init() {
    ensureStyles();
    const row = document.querySelector("main > header .row");
    if (!row || row.dataset.focusedNavInit === "1") return;
    row.dataset.focusedNavInit = "1";

    const settings = linkButton("Settings", "/ui?settings=1");
    settings.title = "Open user settings and preferences";

    const appsWrap = document.createElement("div");
    appsWrap.className = "focused-nav-wrap";
    const appsBtn = button("Apps");
    appsBtn.setAttribute("aria-expanded", "false");
    const caret = document.createElement("span");
    caret.className = "focused-nav-caret";
    caret.setAttribute("aria-hidden", "true");
    caret.textContent = "v";
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
    const admin = document.createElement("a");
    admin.href = "/ui/admin/users";
    admin.textContent = "Admin UI";
    admin.hidden = true;
    menu.appendChild(admin);

    appsWrap.appendChild(appsBtn);
    appsWrap.appendChild(menu);
    row.appendChild(settings);
    row.appendChild(appsWrap);

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
    void exposeAdminLink(admin);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
