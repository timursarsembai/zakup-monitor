// Настройки: «Интерфейс» (тема, масштаб, контраст), вкладки, выбор провайдера ИИ и
// список моделей. Открытая вкладка — data-tab у [data-settings].
(() => {
  // «Интерфейс»: тема, масштаб и контраст — в этом браузере (localStorage).
  (() => {
    const root = document.documentElement;
    const store = (key, value) => { try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch (e) {} };
    for (const radio of document.querySelectorAll("[data-theme-choice]")) {
      radio.checked = radio.value === root.dataset.theme;
      radio.addEventListener("change", () => { root.dataset.theme = radio.value; store("zakup-theme", radio.value); });
    }
    const out = document.querySelector("[data-ui-zoom-value]");
    const contrast = document.querySelector("[data-ui-contrast]");
    if (!out) return;
    const zoomNow = () => parseFloat(root.style.zoom) || 1;
    const show = () => { out.textContent = Math.round(zoomNow() * 100) + "%"; contrast.checked = root.dataset.contrast === "high"; };
    const setZoom = (value) => {
      const zoom = Math.min(2, Math.max(1, Math.round(value * 10) / 10));
      root.style.zoom = zoom === 1 ? "" : zoom; store("zakup-zoom", zoom === 1 ? "" : String(zoom)); show();
    };
    for (const button of document.querySelectorAll("[data-ui-zoom]")) {
      button.addEventListener("click", () => setZoom(zoomNow() + parseFloat(button.dataset.uiZoom)));
    }
    contrast.addEventListener("change", () => {
      if (contrast.checked) root.dataset.contrast = "high"; else delete root.dataset.contrast;
      store("zakup-contrast", contrast.checked ? "high" : "");
    });
    document.querySelector("[data-ui-reset]").addEventListener("click", () => {
      delete root.dataset.contrast; store("zakup-contrast", ""); setZoom(1);
    });
    show();
  })();
  // Вкладки: выбор в адресе (#ai), чтобы после сохранения вернуться туда же.
  const tabs = [...document.querySelectorAll("[data-tab]")];  // ссылка «Журнал» — не вкладка
  const showTab = (name) => {
    if (!tabs.some((t) => t.dataset.tab === name)) name = tabs[0].dataset.tab;
    for (const t of tabs) t.setAttribute("aria-selected", String(t.dataset.tab === name));
    for (const p of document.querySelectorAll("[data-tab-panel]")) p.hidden = p.dataset.tabPanel !== name;
  };
  for (const t of tabs) t.addEventListener("click", (event) => {
    event.preventDefault(); history.replaceState(null, "", "#" + t.dataset.tab); showTab(t.dataset.tab);
  });
  showTab(document.querySelector("[data-settings]").dataset.initialTab || location.hash.slice(1));

  // ИИ: поля ключа и модели — только у выбранного API; у Claude Code полей нет.
  const aiForm = document.querySelector("[data-ai-form]");
  if (aiForm) {
    const sync = () => {
      const chosen = aiForm.querySelector("input[name=provider]:checked")?.value;
      for (const box of aiForm.querySelectorAll("[data-provider-fields]")) {
        const on = box.dataset.providerFields === chosen;
        box.hidden = !on;
        for (const field of box.querySelectorAll("input, select")) field.disabled = !on;
      }
    };
    aiForm.addEventListener("change", sync);
    sync();
    // Если ключ сохранён — подтягиваем актуальный список моделей у провайдера.
    for (const status of aiForm.querySelectorAll("[data-models-status]")) {
      const provider = status.dataset.modelsStatus;
      const selects = aiForm.querySelectorAll(`[data-models="${provider}"]`);
      fetch(`/settings/ai/models?provider=${provider}`).then((r) => r.ok ? r.json() : null).then((models) => {
        if (!models || !models.length) return;
        for (const select of selects) {
          const current = select.value;
          const options = models.map(([id, label]) => new Option(label, id, false, id === current));
          // Сохранённой модели нет в списке провайдера — оставляем её, чтобы не сбросить молча.
          if (current && !models.some(([id]) => id === current)) options.unshift(new Option(current, current, false, true));
          select.replaceChildren(...options);
        }
        status.textContent = `${status.dataset.loadedText} ${models.length}.`;
      }).catch(() => {});
    }
  }

})();
