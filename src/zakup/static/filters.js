// Панель фильтров поиска: на телефоне — на весь экран, подписка, аванс, срок.
// Подписи — из data-labels у блока аванса.
(() => {
  // На телефоне фильтр открывается на весь экран.
  const sheet = document.getElementById("filters");
  for (const opener of document.querySelectorAll("[data-filter-open]")) opener.addEventListener("click", () => {
    sheet.classList.add("open"); document.body.classList.add("sheet-open");
    // «Подписаться» — сразу к блоку подписки внизу панели.
    if (opener.hasAttribute("data-filter-subscribe")) sheet.querySelector(".subscribe")?.scrollIntoView({ block: "start" });
  });
  document.querySelector("[data-filter-close]").addEventListener("click", () => {
    sheet.classList.remove("open"); document.body.classList.remove("sheet-open");
  });
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
    if (event.key === "Escape" && sheet.classList.contains("open")) {
      sheet.classList.remove("open"); document.body.classList.remove("sheet-open");
    }
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
