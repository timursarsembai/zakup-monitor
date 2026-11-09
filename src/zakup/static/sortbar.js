// Над результатами: вид (таблица/плитка, запоминается), сортировка и число строк.
// Текущий запрос — из data-query у .sortbar. Блок результатов бывает подменён без
// перезагрузки (filters.js) — тогда initSortbar() вызывается заново.
(() => {
  window.initSortbar = () => {
    // Вид результатов: таблица (по умолчанию) или плитка-карточки; запоминается в браузере.
    const box = document.querySelector("[data-view-box]");
    const buttons = document.querySelectorAll(".view-switch [data-view]");
    if (box) {
      const apply = (view) => {
        box.dataset.view = view;
        for (const b of buttons) b.setAttribute("aria-pressed", String(b.dataset.view === view));
      };
      // Без сохранённого выбора: на телефоне — плитка, на компьютере — таблица.
      let saved = matchMedia("(max-width: 760px)").matches ? "cards" : "table";
      try { saved = localStorage.getItem("zakup-view") || saved; } catch (e) {}
      apply(saved);
      for (const b of buttons) b.addEventListener("click", () => {
        apply(b.dataset.view);
        try { localStorage.setItem("zakup-view", b.dataset.view); } catch (e) {}
      });
    }
    for (const select of document.querySelectorAll(".sortbar [data-param]")) {
      select.addEventListener("change", () => {
        // На странице с фильтром — без перезагрузки (filters.js); иначе обычный переход.
        if (window.applyFilterParam) return window.applyFilterParam(select.dataset.param, select.value);
        const bar = select.closest(".sortbar");
        const url = new URL(location.pathname + "?" + bar.dataset.query, location.origin);
        url.searchParams.set(select.dataset.param, select.value);
        if (bar.dataset.hidden) url.searchParams.set("hidden", "1");
        location.href = url;
      });
    }
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", window.initSortbar);
  else window.initSortbar();
})();
