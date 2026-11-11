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
  // Вид «таблица / плитка» — в app.js (общий для всех страниц); фрагмент обновился — применить заново.
  const applyRView = () => window.applyRView && window.applyRView();
  applyRView();
  // Пока поиск идёт — обновляем раз в 4 секунды: найденное по ходу появляется сразу.
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
    if (now === "queued" || now === "running") setTimeout(poll, 4000);
    else document.querySelector(".research-form .primary")?.removeAttribute("disabled");
  };
  setTimeout(poll, 4000);
})();

// Разделы лота — вкладки. Ссылки с #research, #suppliers, #results (и на любой элемент
// внутри вкладки) открывают нужную вкладку; выбранная — в адресе, чтобы «Назад» и ссылка
// вели туда же.
(() => {
  const tabs = [...document.querySelectorAll(".lot-tabs [data-tab]")];
  if (!tabs.length) return;
  const panels = [...document.querySelectorAll("[data-panel]")];
  const show = (name) => {
    for (const tab of tabs) tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
    for (const panel of panels) panel.hidden = panel.dataset.panel !== name;
    tabs.find((tab) => tab.dataset.tab === name)?.scrollIntoView({ block: "nearest", inline: "nearest" });
  };
  const fromHash = () => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (!id) return null;
    if (tabs.some((tab) => tab.dataset.tab === id)) return id;
    return document.getElementById(id)?.closest("[data-panel]")?.dataset.panel || null;
  };
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      show(tab.dataset.tab);
      history.replaceState(null, "", "#" + tab.dataset.tab);
    });
  }
  const initial = fromHash();
  show(initial || tabs[0].dataset.tab);
  // Ссылка вела на элемент внутри вкладки (например, #lot-123): после показа — к нему.
  if (initial && location.hash.length > 1 && !tabs.some((tab) => "#" + tab.dataset.tab === location.hash)) {
    document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
  }
  addEventListener("hashchange", () => { const name = fromHash(); if (name) show(name); });
})();

// Черновик технического предложения по найденному товару — окно, как у вопросов поставщику.
(() => {
  const dialog = document.getElementById("proposal");
  if (!dialog) return;
  const body = dialog.querySelector("[data-p-body]");
  const lot = document.querySelector("[data-lot-page]").dataset.lot;
  let current = null;
  const src = (url) => `/lots/${lot}/proposal?url=` + encodeURIComponent(url);
  const show = async (url) => {
    if (url !== current) return;
    try {
      const response = await fetch(src(url));
      if (response.redirected) { body.innerHTML = '<p class="muted"><a href="/login">' + CONFIG.t.login_again + "</a></p>"; return; }
      if (response.ok && url === current) body.innerHTML = await response.text();
    } catch {}
    const status = body.querySelector("[data-p-status]")?.dataset.pStatus;
    if (status === "queued" || status === "running") setTimeout(() => show(url), 3000);
  };
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-proposal]");
    if (!button || button.hasAttribute("data-paid") && !CONFIG.paid) return;
    current = button.dataset.proposal;
    dialog.querySelector("[data-p-title]").textContent = button.dataset.title || "";
    body.innerHTML = '<p><span class="spinner"></span></p>';
    dialog.showModal();
    await fetch(`/lots/${lot}/proposal`, { method: "POST", body: new URLSearchParams({ url: current }) }).catch(() => {});
    show(current);
  });
  dialog.querySelector("[data-p-close]").addEventListener("click", () => { current = null; dialog.close(); });
  dialog.addEventListener("close", () => { current = null; });
})();

// Цена заявки: объяснение от ИИ по кнопке — запуск и опрос фрагмента, пока готовится.
(() => {
  const box = document.querySelector("[data-bid-advice]");
  if (!box) return;
  const poll = async () => {
    try {
      const response = await fetch(box.dataset.src);
      if (response.ok && !response.redirected) box.innerHTML = await response.text();
    } catch {}
    const status = box.querySelector("[data-advice-status]")?.dataset.adviceStatus;
    if (status === "queued" || status === "running") setTimeout(poll, 3000);
  };
  box.addEventListener("click", async (event) => {
    if (!event.target.closest("[data-advice-start]")) return;
    box.innerHTML = '<p><span class="spinner"></span></p>';
    const response = await fetch(box.dataset.src, { method: "POST" }).catch(() => null);
    if (response && !response.ok) {
      // Например, дневной лимит (429): показать причину, а не молча вернуть кнопку.
      const detail = await response.json().then((data) => data.detail).catch(() => null);
      box.innerHTML = '<p class="muted"></p>';
      box.firstChild.textContent = detail || CONFIG.t.load_failed;
      return;
    }
    poll();
  });
  const status = box.querySelector("[data-advice-status]")?.dataset.adviceStatus;
  if (status === "queued" || status === "running") setTimeout(poll, 3000);
})();

// Конкуренты: цены участника в таких же лотах — раскрыть / свернуть строку под ним.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-sup-history]");
  if (!button) return;
  const row = button.closest("tr")?.nextElementSibling;
  if (!row || !row.classList.contains("sup-hist")) return;
  row.hidden = !row.hidden;
  button.closest("tr").classList.toggle("hist-open", !row.hidden);  // плитка: список внутри карточки
  button.setAttribute("aria-expanded", String(!row.hidden));
});
