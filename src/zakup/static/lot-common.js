// Страницы объявления и лота: выжимка техспецификации, текст спецификации,
// кнопка «Поделиться». Подписи — из CONFIG.t (app.js).
(() => {
  // Выжимка техспецификации: запрос и опрос, пока ИИ готовит.
  const pollSummary = async (box) => {
    try {
      const response = await fetch(box.dataset.src);
      if (response.ok && !response.redirected) box.innerHTML = await response.text();
    } catch {}
    const status = box.querySelector("[data-summary-status]")?.dataset.summaryStatus;
    if (status === "queued" || status === "running") setTimeout(() => pollSummary(box), 3000);
  };
  document.addEventListener("click", async (event) => {
    const start = event.target.closest("[data-summary-start]");
    if (!start) return;
    const box = start.closest("[data-summary]");
    start.disabled = true;
    await fetch(box.dataset.src, { method: "POST" });
    pollSummary(box);
  });
  for (const box of document.querySelectorAll("[data-summary]")) {
    const status = box.querySelector("[data-summary-status]")?.dataset.summaryStatus;
    if (status === "queued" || status === "running") setTimeout(() => pollSummary(box), 3000);
  }
  // При ошибке — кнопка «Обновить»: она просит сервер не брать ошибку из кэша.
  const loadSpec = async (spec, refresh) => {
    const body = spec.querySelector(".spec-body");
    body.innerHTML = '<p class="muted">' + CONFIG.t.loading + '</p>';
    try {
      const response = await fetch(spec.dataset.src + (refresh ? "?refresh=1" : ""));
      body.innerHTML = response.ok ? await response.text()
        : '<p class="muted">' + CONFIG.t.load_failed + ' <button type="button" data-spec-retry>' + CONFIG.t.refresh + '</button></p>';
    } catch {
      body.innerHTML = '<p class="muted">' + CONFIG.t.offline + ' <button type="button" data-spec-retry>' + CONFIG.t.refresh + '</button></p>';
    }
  };
  document.addEventListener("click", (event) => {
    const retry = event.target.closest("[data-spec-retry]");
    if (retry) loadSpec(retry.closest("details.spec"), true);
  });
  // Спецификация свёрнута; текст из базы уже в разметке, остальное — при раскрытии.
  for (const spec of document.querySelectorAll("details.spec")) {
    spec.addEventListener("toggle", () => {
      if (spec.open && !spec.dataset.loaded) { spec.dataset.loaded = "1"; loadSpec(spec, false); }
    });
  }
  // На телефоне — системное меню «Поделиться», на компьютере — копирование ссылки.
  for (const button of document.querySelectorAll("[data-share]")) {
    button.addEventListener("click", async () => {
      const url = button.dataset.share, title = button.dataset.shareTitle;
      if (navigator.share) {
        try { await navigator.share({ title, url }); return; } catch (e) { if (e.name === "AbortError") return; }
      }
      try { await navigator.clipboard.writeText(url); } catch (e) { prompt(CONFIG.t.copy_link, url); return; }
      const done = button.parentElement.querySelector("[data-share-done]");
      done.hidden = false; setTimeout(() => { done.hidden = true; }, 2500);
    });
  }
})();
