// Страница объявления: вкладки, обратный отсчёт до окончания приёма.
(() => {
  const showTab = (name) => {
    for (const t of document.querySelectorAll("[data-tab]")) t.setAttribute("aria-selected", t.dataset.tab === name);
    for (const p of document.querySelectorAll("[data-panel]")) p.hidden = p.dataset.panel !== name;
  };
  for (const tab of document.querySelectorAll("[data-tab]")) {
    tab.addEventListener("click", () => showTab(tab.dataset.tab));
  }
  for (const link of document.querySelectorAll("[data-goto]")) {
    link.addEventListener("click", () => showTab(link.dataset.goto));
  }
  const cd = document.querySelector(".countdown");
  if (cd) {
    const end = new Date(cd.dataset.end);
    const parts = Object.fromEntries([...cd.querySelectorAll("[data-part]")].map(e => [e.dataset.part, e]));
    const tick = () => {
      let s = Math.max(0, Math.floor((end - Date.now()) / 1000));
      const v = { d: Math.floor(s / 86400), h: Math.floor(s % 86400 / 3600), m: Math.floor(s % 3600 / 60), s: s % 60 };
      for (const k in v) parts[k].textContent = v[k];
    };
    tick(); setInterval(tick, 1000);
  }
})();
