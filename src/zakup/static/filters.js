// Панель фильтров поиска: на телефоне — на весь экран, подписка, аванс, срок.
// Подписи — из data-labels у блока аванса.
(() => {
  // На телефоне фильтр открывается на весь экран.
  const sheet = document.getElementById("filters");
  const backdrop = document.querySelector("[data-filter-backdrop]");
  const title = sheet.querySelector(".sheet-head b");
  const fullTitle = title.textContent;
  // Панель условий (телефон): условие открывает этот же фильтр снизу, но только своё поле;
  // «Фильтры» — весь фильтр; «Сохранить» — только подписку; «Сортировка» — список.
  const closeSheet = () => {
    sheet.classList.remove("open", "single");
    delete sheet.dataset.mode;
    for (const group of sheet.querySelectorAll(".shown")) group.classList.remove("shown");
    if (backdrop) backdrop.hidden = true;
    title.textContent = fullTitle;
    document.body.classList.remove("sheet-open");
  };
  // Для нижней панели (app.js): закрыть шторку фильтра; true — если она была открыта.
  window.closeFilterSheet = () => {
    if (!sheet.classList.contains("open")) return false;
    closeSheet();
    return true;
  };
  const openSheet = (mode, label) => {
    closeSheet();
    window.closeNavSheets?.();  // шторки «Ещё» / «Закупки» — закрыть: одна шторка за раз
    sheet.classList.add("open");
    document.body.classList.add("sheet-open");
    if (mode === "all") return;
    sheet.classList.add("single");
    sheet.dataset.mode = mode;
    title.textContent = label;
    for (const group of sheet.querySelectorAll(`[data-chip="${mode}"]`)) group.classList.add("shown");
    // Списки (регионы, категории…) — сразу раскрыты; после этого же клика, иначе его
    // закроет обработчик «клик мимо списка».
    setTimeout(() => { for (const list of sheet.querySelectorAll(".shown details.multi")) list.open = true; });
    if (backdrop) backdrop.hidden = false;
  };
  // Без перезагрузки: форма уходит в фоне, подменяются результаты и панель условий,
  // адрес страницы обновляется («Назад» работает — перезагрузкой).
  const chipbar = document.querySelector("[data-chipbar]");
  const query = () => {
    const params = new URLSearchParams();
    for (const [key, value] of new FormData(sheet)) {
      const field = sheet.querySelector(`[name="${key}"]`);
      if (field && field.closest(".subscribe") && key !== "edit") continue;
      if (value === "") continue;  // пустые поля — короткий адрес; сервер понимает и так
      params.append(key, value);
    }
    return params.toString();
  };
  // Поля формы — как в ответе сервера (после «Сбросить» и др.): значения, галочки, ползунки.
  const syncForm = (fresh) => {
    for (const field of sheet.querySelectorAll("input, select")) {
      if (field.closest(".subscribe")) continue;
      let twin = null;
      if (field.hasAttribute("data-all")) {
        const index = [...sheet.querySelectorAll("[data-all]")].indexOf(field);
        twin = fresh.querySelectorAll("[data-all]")[index];
      } else if (field.name) {
        twin = field.type === "checkbox"
          ? fresh.querySelector(`[name="${field.name}"][value="${CSS.escape(field.value)}"]`)
          : fresh.querySelector(`[name="${field.name}"]`);
      }
      if (!twin) continue;
      if (field.type === "checkbox") field.checked = twin.checked;
      else if (field.type !== "hidden" || field.name === "sort") field.value = twin.value;
      if (field.type === "range") field.dispatchEvent(new Event("input"));
    }
    for (const option of sheet.querySelectorAll("[data-sort-value]")) {
      option.classList.toggle("on", option.dataset.sortValue === sheet.querySelector("[data-sort-field]").value);
    }
  };
  const softApply = async (target) => {
    const url = target || `${location.pathname}?${query()}`;
    // Шторку закрываем, когда результаты уже на месте, — без «схлопывания» на полпути.
    document.querySelector("[data-results]")?.classList.add("loading");
    try {
      const response = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
      if (!response.ok || response.redirected) throw new Error(String(response.status));
      const doc = new DOMParser().parseFromString(await response.text(), "text/html");
      const fresh = doc.querySelector("[data-results]");
      const freshBar = doc.querySelector("[data-chipbar]");
      if (!fresh) throw new Error("no results");
      document.querySelector("[data-results]").replaceWith(fresh);
      if (chipbar && freshBar) {
        // Панель перерисована — прокрутка условий остаётся там, где была.
        const scrolled = chipbar.querySelector(".chip-scroll")?.scrollLeft || 0;
        chipbar.innerHTML = freshBar.innerHTML;
        const strip = chipbar.querySelector(".chip-scroll");
        if (strip) strip.scrollLeft = scrolled;
      }
      const freshForm = doc.getElementById("filters");
      if (freshForm) {
        syncForm(freshForm);
        // Блоки «Мои фильтры» и подписки — как в ответе (загружен другой фильтр, режим правки).
        for (const selector of [".my-filters", ".subscribe"]) {
          const mine = sheet.querySelector(selector), theirs = freshForm.querySelector(selector);
          if (mine && theirs) mine.innerHTML = theirs.innerHTML;
        }
      }
      history.pushState({ soft: true }, "", url);
      closeSheet();
      window.initSortbar?.();
      window.applyRView?.();
    } catch (e) {
      location.href = url;  // не вышло — обычный переход
    }
  };
  addEventListener("popstate", () => location.reload());
  // Для панели над результатами (sortbar.js): сортировка и «Показывать по» — без перезагрузки.
  window.applyFilterParam = (name, value) => {
    let field = sheet.querySelector(`input[type=hidden][name="${name}"]`);
    if (!field) {
      field = Object.assign(document.createElement("input"), { type: "hidden", name });
      sheet.append(field);
    }
    field.value = value;
    if (name === "sort") {
      for (const option of sheet.querySelectorAll("[data-sort-value]")) option.classList.toggle("on", option.dataset.sortValue === value);
    }
    sheet.requestSubmit();
  };
  sheet.addEventListener("submit", (event) => {
    // Подписка и выгрузка в Excel — обычной отправкой; поиск («Найти», «Применить», условия) — без перезагрузки.
    const subscribe = subscribeButtonRef();
    if ((subscribe && event.submitter === subscribe) || event.submitter?.hasAttribute("formaction")) return;
    event.preventDefault();
    // Обработчик ниже отключил бы поля подписки — здесь они и так не уходят в запрос.
    event.stopImmediatePropagation();
    softApply();
  });
  const subscribeButtonRef = () => sheet.querySelector(".subscribe button.primary");
  chipbar?.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-chip-open]");
    if (opener) {
      openSheet(opener.dataset.chipOpen, opener.dataset.title || opener.getAttribute("aria-label") || opener.textContent.trim());
      return;
    }
    // «Только открытые», «Опубликованы сегодня» — переключаются сразу.
    const toggle = event.target.closest("[data-chip-toggle]");
    if (toggle) {
      const box = sheet.querySelector(`[name="${toggle.dataset.chipToggle}"]`);
      box.checked = !box.checked;
      sheet.requestSubmit();
    }
  });
  backdrop?.addEventListener("click", closeSheet);
  // «Сбросить» и «Мои фильтры» (тот же вид поиска) — без перезагрузки; блоки внутри
  // формы перерисовываются, поэтому — делегирование.
  sheet.addEventListener("click", (event) => {
    const link = event.target.closest("[data-reset-all], [data-saved-filter]:not([data-other-scope])");
    if (!link) return;
    event.preventDefault();
    softApply(link.getAttribute("href"));
  });
  for (const option of sheet.querySelectorAll("[data-sort-value]")) {
    option.addEventListener("click", () => {
      sheet.querySelector("[data-sort-field]").value = option.dataset.sortValue;
      for (const other of sheet.querySelectorAll("[data-sort-value]")) other.classList.toggle("on", other === option);
      sheet.requestSubmit();
    });
  }
  // «Сбросить» в шторке условия — очистить только его поля и применить.
  sheet.querySelector("[data-chip-reset]")?.addEventListener("click", () => {
    for (const group of sheet.querySelectorAll(".shown")) {
      for (const field of group.querySelectorAll("input, select")) {
        if (field.type === "checkbox") field.checked = false;
        else if (field.type === "range") field.value = field === group.querySelector("input[type=range]") ? 0 : 100;
        else if (field.tagName === "SELECT") field.value = [...field.options].some((o) => o.value === "") ? "" : field.options[0].value;
        else field.value = "";
      }
    }
    sheet.requestSubmit();
  });
  // Высота шапки — панель условий закрепляется сразу под ней.
  const header = document.querySelector("header");
  const syncHeader = () => header && document.documentElement.style.setProperty("--hdr", header.offsetHeight + "px");
  syncHeader();
  addEventListener("resize", syncHeader);
  for (const opener of document.querySelectorAll("[data-filter-open]")) opener.addEventListener("click", () => {
    sheet.classList.add("open"); document.body.classList.add("sheet-open");
    // «Подписаться» — сразу к блоку подписки внизу панели.
    if (opener.hasAttribute("data-filter-subscribe")) sheet.querySelector(".subscribe")?.scrollIntoView({ block: "start" });
  });
  document.querySelector("[data-filter-close]").addEventListener("click", closeSheet);
  // Поля подписки живут в той же форме, что и поиск: в обычный поиск их не отправляем,
  // а Enter в названии подписки — подписывает, а не ищет.
  // При редактировании подписки обычный поиск сохраняет режим правки (поле edit).
  // Блок подписки перерисовывается (режим правки) — кнопку ищем каждый раз.
  sheet.addEventListener("submit", (event) => {
    if (event.submitter !== sheet.querySelector(".subscribe button.primary")) {
      for (const field of sheet.querySelectorAll(".subscribe [name]")) field.disabled = field.name !== "edit";
    }
  });
  sheet.querySelector('.subscribe [name="name"]')?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); sheet.querySelector(".subscribe button.primary")?.click(); }
  });
  // Escape закрывает полноэкранный фильтр на телефоне.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sheet.classList.contains("open")) closeSheet();
  });
  // «Все …» в списке: отмечено — снять остальные; отметили что-то — снять «Все».
  for (const list of sheet.querySelectorAll(".multi-list")) {
    const all = list.querySelector("[data-all]");
    const items = [...list.querySelectorAll("input[type=checkbox]:not([data-all])")];
    all?.addEventListener("change", () => {
      if (all.checked) for (const item of items) item.checked = false;
      else if (!items.some((i) => i.checked)) all.checked = true;
    });
    for (const item of items) item.addEventListener("change", () => { if (all) all.checked = !items.some((i) => i.checked); });
  }
  // Выпадающие списки: клик мимо — закрыть.
  document.addEventListener("click", (event) => {
    for (const d of document.querySelectorAll("details.multi[open]")) if (!d.contains(event.target)) d.open = false;
  });
  // Аванс: два ползунка «от» и «до», между ними — закрашенная полоса.
  for (const box of document.querySelectorAll("[data-advance]")) {
    const [low, high] = box.querySelectorAll("input[type=range]");
    const label = box.querySelector("[data-advance-label]");
    const fill = document.createElement("div");
    fill.className = "fill";
    box.querySelector(".range2").prepend(fill);
    const draw = (moved) => {
      if (+low.value > +high.value) (moved === low ? high : low).value = moved.value;
      const a = +low.value, b = +high.value;
      const T = JSON.parse(box.dataset.labels);
      label.textContent = a === 0 && b === 100 ? T.any : b === 0 ? T.none : b === 100 ? `${T.from} ${a}%` : a === 0 ? `${T.to} ${b}%` : `${a}–${b}%`;
      // Центр бегунка: a% ширины + полбегунка − a% от ширины бегунка.
      const thumb = parseFloat(getComputedStyle(box.querySelector(".range2")).getPropertyValue("--thumb")) || 18;
      fill.style.left = `calc(${a}% + ${thumb / 2 - a * thumb / 100}px)`;
      fill.style.width = `calc(${b - a}% - ${(b - a) * thumb / 100}px)`;
    };
    low.addEventListener("input", () => draw(low));
    high.addEventListener("input", () => draw(high));
    for (const preset of box.querySelectorAll("[data-preset]")) {
      preset.addEventListener("click", () => {
        [low.value, high.value] = preset.dataset.preset.split(",");
        draw(low);
      });
    }
    draw(low);
  }
  // Срок (только в поиске закупок; в справочнике организаций этих полей нет).
  const mode = document.querySelector("[name=deadline_mode]");
  const until = document.querySelector("[name=deadline_date]");
  if (!mode || !until) return;
  mode.addEventListener("change", () => { if (mode.value !== "date") until.value = ""; else until.focus(); });
  until.addEventListener("change", () => { if (until.value) mode.value = "date"; });
})();
