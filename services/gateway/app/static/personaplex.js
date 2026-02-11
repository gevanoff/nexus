(() => {
  const $ = (id) => document.getElementById(id);

  const openEl = $("openUi");
  const linkEl = $("uiLink");
  const statusEl = $("status");

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  async function loadInfo() {
    try {
      const resp = await fetch('/ui/api/personaplex/info', { method: 'GET', credentials: 'same-origin' });
      if (!resp.ok) return;
      const payload = await resp.json();
      const uiUrl = String(payload?.ui_url || 'https://localhost:8998');
      if (linkEl) linkEl.href = uiUrl;
      if (openEl) openEl.onclick = () => {
        try { window.open(uiUrl, '_blank', 'noopener'); } catch (e) {}
      };
      setStatus(`PersonaPlex UI: ${uiUrl}`, false);
    } catch (e) {
      setStatus(String(e), true);
    }
  }

  loadInfo();
})();
