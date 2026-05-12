(() => {
  const instances = new WeakMap();

  function selectedText(select) {
    if (!(select instanceof HTMLSelectElement)) return "";
    const option = select.options[select.selectedIndex] || null;
    return option ? String(option.textContent || "").trim() : "";
  }

  function measure(instance) {
    if (!instance) return;
    const available = Math.max(0, instance.overlay.clientWidth);
    const required = Math.max(0, instance.label.scrollWidth);
    const overflow = Math.max(0, required - available);
    instance.wrapper.classList.toggle("nexus-marquee-overflow", overflow > 4);
    instance.wrapper.style.setProperty("--nexus-marquee-distance", `${overflow}px`);
    instance.wrapper.style.setProperty("--nexus-marquee-duration", `${Math.min(24, Math.max(8, overflow / 14))}s`);
  }

  function refresh(select) {
    const instance = instances.get(select) || enhance(select);
    if (!instance) return;
    const text = selectedText(select);
    instance.label.textContent = text;
    select.title = text;
    measure(instance);
    window.requestAnimationFrame(() => measure(instance));
  }

  function enhance(select) {
    if (!(select instanceof HTMLSelectElement)) return null;
    if (instances.has(select)) return instances.get(select);
    if (!select.matches("select[data-marquee-select]")) return null;
    const parent = select.parentNode;
    if (!parent) return null;

    const wrapper = document.createElement("span");
    wrapper.className = "nexus-marquee-select";
    wrapper.style.setProperty("--nexus-marquee-select-width", select.dataset.marqueeWidth || "26rem");

    parent.insertBefore(wrapper, select);
    wrapper.appendChild(select);

    const overlay = document.createElement("span");
    overlay.className = "nexus-marquee-overlay";
    overlay.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.className = "nexus-marquee-label";
    overlay.appendChild(label);
    wrapper.appendChild(overlay);

    const instance = { select, wrapper, overlay, label };
    instances.set(select, instance);

    const mutationObserver = new MutationObserver(() => refresh(select));
    mutationObserver.observe(select, { childList: true, subtree: true, characterData: true });
    instance.mutationObserver = mutationObserver;

    if (typeof ResizeObserver !== "undefined") {
      const resizeObserver = new ResizeObserver(() => measure(instance));
      resizeObserver.observe(wrapper);
      instance.resizeObserver = resizeObserver;
    }

    select.addEventListener("change", () => refresh(select));
    select.addEventListener("input", () => refresh(select));

    refresh(select);
    return instance;
  }

  function refreshAll(root = document) {
    if (!root || typeof root.querySelectorAll !== "function") return;
    root.querySelectorAll("select[data-marquee-select]").forEach((select) => refresh(select));
  }

  function init() {
    document.querySelectorAll("select[data-marquee-select]").forEach((select) => enhance(select));
    refreshAll(document);
  }

  window.NexusSelectMarquee = {
    enhance,
    refresh,
    refreshAll,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
