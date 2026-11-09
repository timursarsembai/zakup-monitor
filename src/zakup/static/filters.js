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
  const openSheet = (mode, label) => {
    closeSheet();
    sheet.classList.add("open");
    document.body.classList.add("sheet-open");
    if (mode === "all") return;
    sheet.classList.add("single");
    sheet.dataset.mode = mode;
    title.textContent = label;
    for (const group of sheet.querySelectorAll(`[data-chip="${mode}"]`)) {
      group.classList.add("shown");
      for (const list of group.querySelectorAll("details.multi")) list.open = true;
    }
    if (backdrop) backdrop.hidden = false;
  };
  for (const chip of document.querySelectorAll("[data-chip-open]")) {
    chip.addEventListener("click", () => openSheet(chip.dataset.chipOpen, chip.getAttribute("aria-label") || chip.textContent.trim()));
  }
  backdrop?.addEventListener("click", closeSheet);
  // «Только открытые», «Опубликованы сегодня» — переключаются сразу.
  for (const chip of document.querySelectorAll("[data-chip-toggle]")) {
    chip.addEventListener("click", () => {
      const box = sheet.querySelector(`[name="${chip.dataset.chipToggle}"]`);
      box.checked = !box.checked;
      sheet.requestSubmit();
    });
  }
  for (const option of sheet.querySelectorAll("[data-sort-value]")) {
    option.addEventListener("click", () => {
      sheet.querySelector("[data-sort-field]").value = option.dataset.sortValue;
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
  const subscribeButton = sheet.querySelector(".subscribe button.primary");
  sheet.addEventListener("submit", (event) => {
    if (event.submitter !== subscribeButton) {
      for (const field of sheet.querySelectorAll(".subscribe [name]")) field.disabled = field.name !== "edit";
    }
  });
  sheet.querySelector('.subscribe [name="name"]')?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); subscribeButton?.click(); }
  });
  // Escape закрывает полноэкранный фильтр на телефоне.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sheet.classList.contains("open")) closeSheet();
  });
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
  const mode = document.querySelector("[name=deadline_mode]");
  const until = document.querySelector("[name=deadline_date]");
  mode.addEventListener("change", () => { if (mode.value !== "date") until.value = ""; else until.focus(); });
  until.addEventListener("change", () => { if (until.value) mode.value = "date"; });
})();
