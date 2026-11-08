// Страница лота: подтверждение поиска с ИИ, «Что спросить у поставщика», вид результатов,
// обновление результата, пока идёт поиск. Данные страницы — data-* у [data-lot-page].
const PAGE = document.querySelector("[data-lot-page]");
(() => {
  // Поиск с ИИ: клиенту — подтверждение списания (или предложение купить поиски).
  (() => {
    const STAFF = PAGE.dataset.staff === "1";
    const HAS = PAGE.dataset.hasSearches === "1";
    const dialog = document.getElementById("search-confirm");
    if (STAFF || !dialog) return;
    let pending = null;
    document.addEventListener("submit", (event) => {
      const form = event.target.closest("[data-search]");
      if (!form || form.dataset.confirmed) return;
      let skip = false;
      try { skip = HAS && localStorage.getItem("zakup-search-confirm") === "skip"; } catch (e) {}
      if (skip) return;
      event.preventDefault();
      pending = form;
      dialog.showModal();
    }, true);
    dialog.addEventListener("close", () => {
      if (dialog.returnValue === "ok" && pending) {
        if (dialog.querySelector("[data-sc-skip]")?.checked) {
          try { localStorage.setItem("zakup-search-confirm", "skip"); } catch (e) {}
        }
        // Флаг — только на эту отправку: следующая снова спросит подтверждение.
        pending.dataset.confirmed = "1";
        pending.requestSubmit();
        delete pending.dataset.confirmed;
      }
      pending = null;
    });
  })();
  // «Что спросить у поставщика»: чек-лист от ИИ по кнопке; готовый — общий для всех.
  const qDialog = document.getElementById("questions");
  const qBody = qDialog.querySelector("[data-q-body]");
  const qCopy = qDialog.querySelector("[data-q-copy]");
  let qUrl = null;
  const qSrc = (url) => `/lots/${PAGE.dataset.lot}/questions?url=` + encodeURIComponent(url);
  const qShow = async (url) => {
    if (url !== qUrl) return;  // окно закрыли или открыли другое предложение
    try {
      const response = await fetch(qSrc(url));
      if (response.redirected) { qBody.innerHTML = '<p class="muted"><a href="/login">' + CONFIG.t.login_again + '</a></p>'; return; }
      if (response.ok && url === qUrl) qBody.innerHTML = await response.text();
    } catch {}
    const status = qBody.querySelector("[data-q-status]")?.dataset.qStatus;
    qCopy.hidden = status !== "done";
    if (status === "queued" || status === "running") setTimeout(() => qShow(url), 3000);
  };
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-questions]");
    if (!button) return;
    qUrl = button.dataset.questions;
    qDialog.querySelector("[data-q-title]").textContent = button.dataset.title || "";
    qBody.innerHTML = '<p><span class="spinner"></span></p>';
    qCopy.hidden = true;
    qDialog.showModal();
    const body = new URLSearchParams({ url: qUrl });
    await fetch(`/lots/${PAGE.dataset.lot}/questions`, { method: "POST", body }).catch(() => {});
    qShow(qUrl);
  });
  qDialog.querySelector("[data-q-close]").addEventListener("click", () => { qUrl = null; qDialog.close(); });
  qDialog.addEventListener("close", () => { qUrl = null; });
  qCopy.addEventListener("click", async () => {
    const parts = [...qBody.querySelectorAll("h3, li")].map((el) => el.tagName === "H3" ? "\n" + el.textContent.trim() + ":" : "— " + el.textContent.trim());
    try { await navigator.clipboard.writeText(parts.join("\n").trim()); qCopy.textContent = CONFIG.t.copied; }
    catch { qCopy.textContent = CONFIG.t.copy_failed; }
    setTimeout(() => { qCopy.textContent = "📋 " + CONFIG.t.copy_questions; }, 2000);
  });
  // Вид результатов подбора: таблица или плитка (запоминается); фрагмент обновляется — применяем заново.
  const applyRView = () => {
    let view = matchMedia("(max-width: 760px)").matches ? "cards" : "table";
    try { view = localStorage.getItem("zakup-rview") || view; } catch (e) {}
    for (const box of document.querySelectorAll("[data-rview-box]")) box.dataset.rview = view;
    for (const b of document.querySelectorAll("[data-rview]:not([data-rview-box])")) b.setAttribute("aria-pressed", String(b.dataset.rview === view));
  };
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-rview]");
    if (!button) return;
    try { localStorage.setItem("zakup-rview", button.dataset.rview); } catch (e) {}
    applyRView();
  });
  applyRView();
  // Пока поиск идёт — обновляем результат раз в 10 секунд.
  const research = document.querySelector("[data-research]");
  const poll = async () => {
    const status = research.querySelector("[data-research-status]")?.dataset.researchStatus;
    if (status !== "queued" && status !== "running") return;
    try {
      const response = await fetch(research.dataset.src);
      // Сессия истекла — fetch молча пришёл на страницу входа: не вставляем её сюда.
      if (response.redirected) {
        research.innerHTML = '<p class="muted">' + CONFIG.t.session_expired + ' <a href="/login">' + CONFIG.t.login_again + '</a>.</p>';
        return;
      }
      if (response.ok) { research.innerHTML = await response.text(); applyRView(); }
    } catch {}
    const now = research.querySelector("[data-research-status]")?.dataset.researchStatus;
    if (now === "queued" || now === "running") setTimeout(poll, 10000);
    else document.querySelector(".research-form .primary")?.removeAttribute("disabled");
  };
  setTimeout(poll, 10000);
})();
