// Общие скрипты всех страниц: окно «платная функция», режим для слабовидящих, кнопка
// «наверх», меню ☰, подтверждения, смена темы, маска телефона.
// Значения со страницы (оплачен ли тариф, подписи) — CONFIG, объявлен в <head> (base.html).
  // Без оплаченного тарифа элементы с data-paid не срабатывают, а открывают окно.
  (() => {
    const PAID = CONFIG.paid;
    const dialog = document.getElementById("paywall");
    if (PAID || !dialog) return;
    const stop = (event) => {
      if (!event.target.closest || !event.target.closest("[data-paid]")) return;
      event.preventDefault(); event.stopImmediatePropagation(); dialog.showModal();
      // Какая функция привела к окну оплаты — для статистики продаж.
      const el = event.target.closest("[data-paid]");
      const feature = el.dataset.feature || el.getAttribute("href") || el.getAttribute("action")
        || (el.textContent || "").trim().slice(0, 60);
      fetch("/t/event", {method: "POST", headers: {"Content-Type": "application/json"}, keepalive: true,
        body: JSON.stringify({name: "paywall", props: {feature, page: location.pathname}})}).catch(() => {});
    };
    document.addEventListener("click", stop, true);
    document.addEventListener("submit", stop, true);
    dialog.querySelector("[data-pw-close]").addEventListener("click", () => dialog.close());
  })();

  // Всплывающее окно (base.html, #popup): через 2 с после загрузки, если не открыто другое
  // окно. Закрытое помним: data-days дней, 0 — навсегда. Без localStorage не показываем —
  // иначе окно выскакивало бы на каждой странице.
  (() => {
    const dialog = document.getElementById("popup");
    if (!dialog || typeof dialog.showModal !== "function") return;
    const key = "zakup-popup-" + dialog.dataset.popup;
    const days = Number(dialog.dataset.days || 0);
    try {
      const closedAt = Number(localStorage.getItem(key));
      if (closedAt && (!days || Date.now() - closedAt < days * 86400000)) return;
    } catch (e) { return; }
    const remember = () => { try { localStorage.setItem(key, String(Date.now())); } catch (e) {} };
    dialog.addEventListener("close", remember);
    // Переход по ссылке из окна («Добавить товары») — тоже «показали».
    for (const link of dialog.querySelectorAll("a")) link.addEventListener("click", remember);
    for (const button of dialog.querySelectorAll("[data-pw-close]")) button.addEventListener("click", () => dialog.close());
    // Клик мимо окна (по затемнению) — закрыть; но не когда выделяли текст в поле и отпустили снаружи.
    let downOnBackdrop = false;
    dialog.addEventListener("pointerdown", (event) => { downOnBackdrop = event.target === dialog; });
    dialog.addEventListener("click", (event) => { if (downOnBackdrop && event.target === dialog) dialog.close(); });
    setTimeout(() => {
      // Не мешаем: открыто меню или другое окно, или человек уже печатает.
      if (document.querySelector('dialog[open], [role="dialog"]:not([hidden]):not(dialog)')) return;
      if (document.activeElement && document.activeElement.matches("input, textarea, select, [contenteditable]")) return;
      dialog.showModal();
    }, 2000);
  })();

  (() => {
    const root = document.documentElement;
    const panel = document.querySelector("[data-a11y-panel]");
    const toggle = document.querySelector("[data-a11y-toggle]");
    if (!panel) return;
    const store = (key, value) => { try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch (e) {} };
    const zoomNow = () => parseFloat(root.style.zoom) || 1;
    const show = () => {
      panel.querySelector("[data-zoom-value]").textContent = Math.round(zoomNow() * 100) + "%";
      panel.querySelector("[data-contrast]").checked = root.dataset.contrast === "high";
    };
    const setZoom = (value) => {
      const zoom = Math.min(2, Math.max(1, Math.round(value * 10) / 10));
      root.style.zoom = zoom === 1 ? "" : zoom;
      store("zakup-zoom", zoom === 1 ? "" : String(zoom));
      show();
    };
    toggle.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      toggle.setAttribute("aria-expanded", String(!panel.hidden));
      show();
    });
    document.addEventListener("click", (event) => {
      if (!panel.hidden && !event.target.closest(".a11y")) { panel.hidden = true; toggle.setAttribute("aria-expanded", "false"); }
    });
    for (const button of panel.querySelectorAll("[data-zoom]")) {
      button.addEventListener("click", () => setZoom(zoomNow() + parseFloat(button.dataset.zoom)));
    }
    panel.querySelector("[data-contrast]").addEventListener("change", (event) => {
      if (event.target.checked) root.dataset.contrast = "high"; else delete root.dataset.contrast;
      store("zakup-contrast", event.target.checked ? "high" : "");
    });
    panel.querySelector("[data-a11y-reset]").addEventListener("click", () => {
      delete root.dataset.contrast; store("zakup-contrast", "");
      setZoom(1);
    });
  })();
  // Кнопка «наверх»: появляется, когда страницу прокрутили.
  (() => {
    const button = document.querySelector("[data-to-top]");
    if (!button) return;
    const sync = () => button.classList.toggle("show", window.scrollY > 500);
    window.addEventListener("scroll", sync, { passive: true });
    button.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
    sync();
  })();
  // Меню ☰ на телефоне: открыть/закрыть, закрыть по нажатию вне его.
  (() => {
    const burger = document.querySelector("[data-burger]");
    const tools = document.querySelector("[data-tools]");
    if (!burger || !tools) return;
    const set = (open) => { tools.classList.toggle("open", open); burger.setAttribute("aria-expanded", String(open)); };
    burger.addEventListener("click", (e) => { e.stopPropagation(); set(!tools.classList.contains("open")); });
    document.addEventListener("click", (e) => { if (!tools.contains(e.target) && e.target !== burger) set(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") set(false); });
  })();
  // Вид таблиц с переключателем «Таблица / Плитка» (data-rview-box): выбор запоминается — общий
  // на весь сайт, а у своей группы (data-rview-box="catalog", кнопки в data-rview-group="catalog") —
  // отдельный. По умолчанию: data-rview-default у таблицы, иначе на телефоне плитка.
  const rviewKey = (group) => "zakup-rview" + (group ? "-" + group : "");
  const rviewGroup = (button) => button.closest("[data-rview-group]")?.dataset.rviewGroup || "";
  window.applyRView = () => {
    const views = {};
    for (const box of document.querySelectorAll("[data-rview-box]")) {
      const group = box.dataset.rviewBox || "";
      let view = box.dataset.rviewDefault || (matchMedia("(max-width: 760px)").matches ? "cards" : "table");
      try { view = localStorage.getItem(rviewKey(group)) || view; } catch (e) {}
      box.dataset.rview = views[group] = view;
    }
    for (const b of document.querySelectorAll("button[data-rview]")) {
      b.setAttribute("aria-pressed", String(b.dataset.rview === views[rviewGroup(b)]));
    }
  };
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-rview]");
    if (!button) return;
    try { localStorage.setItem(rviewKey(rviewGroup(button)), button.dataset.rview); } catch (e) {}
    window.applyRView();
  });
  window.applyRView();
  // Компьютер: свернуть / развернуть левую панель (запоминается).
  for (const button of document.querySelectorAll("[data-side-collapse]")) {
    button.addEventListener("click", () => {
      const root = document.documentElement;
      const mini = root.dataset.side !== "mini";
      if (mini) root.dataset.side = "mini"; else delete root.dataset.side;
      try { localStorage.setItem("zakup-side", mini ? "mini" : "full"); } catch (e) {}
    });
  }
  // Телефон: шторки снизу над нижней панелью — «Ещё» и «Закупки» (объявления / лоты).
  // Закрываются повторным нажатием, по фону, Esc и свайпом вниз; открыта одна за раз.
  (() => {
    const backdrop = document.querySelector("[data-more-backdrop]");
    if (!backdrop) return;
    const phone = matchMedia("(max-width: 760px)");
    const sheets = [];
    const bind = (trigger, sheet) => {
      if (!trigger || !sheet) return;
      let timer;
      const set = (open) => {
        clearTimeout(timer);
        trigger.setAttribute("aria-expanded", String(open));
        if (open) {
          for (const other of sheets) if (other.sheet !== sheet) other.set(false);
          sheet.hidden = backdrop.hidden = false;
          requestAnimationFrame(() => requestAnimationFrame(() => {
            sheet.classList.add("open");
            backdrop.classList.add("open");
            sheet.querySelector(".more-tile")?.focus({ preventScroll: true });
          }));
        } else if (!sheet.hidden) {
          sheet.classList.remove("open");
          if (!sheets.some((o) => o.sheet !== sheet && !o.sheet.hidden)) backdrop.classList.remove("open");
          timer = setTimeout(() => {
            sheet.hidden = true;
            if (sheets.every((o) => o.sheet.hidden)) backdrop.hidden = true;
          }, 260);
          if (sheet.contains(document.activeElement)) trigger.focus({ preventScroll: true });
        }
      };
      sheets.push({ sheet, set });
      // Нажали плитку — шторку закрыть (в том числе перед окном «платная функция»).
      sheet.addEventListener("click", (e) => { if (e.target.closest("a.more-tile")) set(false); }, true);
      // Tab не уходит за шторку, пока она открыта.
      sheet.addEventListener("keydown", (e) => {
        if (e.key !== "Tab") return;
        const items = [...sheet.querySelectorAll("a, button")];
        const first = items[0], last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      });
      trigger.addEventListener("click", (e) => {
        if (trigger.tagName === "A" && !phone.matches) return;  // на компьютере — обычная ссылка
        e.preventDefault();
        // Открыта шторка фильтра — нажатие в нижней панели её закрывает, и только.
        if (window.closeFilterSheet?.()) return;
        set(trigger.getAttribute("aria-expanded") !== "true");
      });
      let startY = null;
      sheet.addEventListener("touchstart", (e) => { startY = sheet.scrollTop ? null : e.touches[0].clientY; }, { passive: true });
      sheet.addEventListener("touchend", (e) => {
        if (startY !== null && e.changedTouches[0].clientY - startY > 60) set(false);
        startY = null;
      }, { passive: true });
    };
    window.closeNavSheets = () => sheets.forEach((o) => o.set(false));
    bind(document.querySelector("[data-more]"), document.querySelector("[data-more-sheet]"));
    bind(document.querySelector("[data-search-open]"), document.querySelector("[data-search-sheet]"));
    backdrop.addEventListener("click", () => sheets.forEach((o) => o.set(false)));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") sheets.forEach((o) => o.set(false)); });
  })();
  // Подтверждение для форм с data-confirm (выход, удаление и т.п.): защита от
  // случайного нажатия. Текст — из атрибута, не из кода.
  document.addEventListener("submit", (event) => {
    // У формы целиком или у одной кнопки (например, «Удалить» рядом с «Сохранить»).
    const text = event.submitter?.dataset.confirm || event.target.closest("form[data-confirm]")?.dataset.confirm;
    if (text && !confirm(text)) event.preventDefault();
  });
  // Темы по кругу: светлая → бежевая → синяя → тёмная → светлая.
  // Значок кнопки — кружок цвета текущей темы.
  const THEMES = [["light", "#ffffff", CONFIG.t.light], ["beige", "#f2ecbc", CONFIG.t.beige], ["navy", "#0e1b2e", CONFIG.t.navy], ["dark", "#151517", CONFIG.t.dark]];
  for (const button of document.querySelectorAll("[data-theme-toggle]")) {
    const index = () => Math.max(0, THEMES.findIndex(([name]) => name === document.documentElement.dataset.theme));
    const show = () => {
      const [, color, label] = THEMES[index()];
      const [, , nextLabel] = THEMES[(index() + 1) % THEMES.length];
      // Плитка в шторке «Ещё» — кружок и подпись; обычная кнопка — только кружок.
      if (button.dataset.themeToggle === "tile") button.querySelector(".swatch").style.background = color;
      else {
        button.innerHTML = '<span class="swatch"></span>';
        button.firstChild.style.background = color;
      }
      button.title = `${CONFIG.t.theme}: ${label}. ${CONFIG.t.press} — ${nextLabel.toLowerCase()}`;
    };
    button.addEventListener("click", () => {
      const next = THEMES[(index() + 1) % THEMES.length][0];
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("zakup-theme", next); } catch (e) {}
      show();
    });
    show();
  }

  // Маска телефона во всех полях type=tel: «+7 7XX XXX XX XX». Сервер принимает и без неё.
  (() => {
    const format = (value) => {
      let d = value.replace(/\D/g, "");
      // Вставили полный номер поверх «+7 » (+7 8701…, +7 +7701…): местный номер — последние 10 цифр.
      if (d.length > 11) d = "7" + d.slice(-10);
      // Автозаполнение без кода страны: «7011112233» — это местный номер.
      if (d.length === 10 && !value.trim().startsWith("+")) d = "7" + d;
      if (d[0] === "8") d = "7" + d.slice(1);
      if (d && d[0] !== "7") d = "7" + d;
      const n = d.slice(1, 11);
      if (!d) return "";
      return ("+7 " + [n.slice(0, 3), n.slice(3, 6), n.slice(6, 8), n.slice(8, 10)].filter(Boolean).join(" ")).trimEnd();
    };
    for (const input of document.querySelectorAll('input[type="tel"]')) {
      if (input.value) input.value = format(input.value);
      let previous = input.value;
      input.addEventListener("focus", () => { if (!input.value) previous = input.value = "+7 "; });
      // Поле с autofocus получило фокус раньше, чем подключилась маска.
      if (document.activeElement === input && !input.value) previous = input.value = "+7 ";
      input.addEventListener("blur", () => { if (input.value.replace(/\D/g, "") === "7") input.value = ""; });
      input.addEventListener("input", (event) => {
        // Номер уже полный — лишняя набранная цифра не сдвигает его.
        if (event.inputType === "insertText" && previous.replace(/\D/g, "").length >= 11) {
          input.value = previous; return;
        }
        // Стирание пробела не должно «застревать»: при удалении форматируем без добавления.
        const formatted = format(input.value);
        if (event.inputType && event.inputType.startsWith("delete") && formatted.length > input.value.length) { previous = input.value; return; }
        input.value = formatted || (document.activeElement === input ? "+7 " : "");
        previous = input.value;
      });
    }
  })();

  // У каждого поля пароля — кнопка «глаз»: показать введённое и снова скрыть.
  (() => {
    const EYE = '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>';
    const EYE_OFF = '<path d="M3 3l18 18M10.6 5.1A10.8 10.8 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.1M6.6 6.6A17.4 17.4 0 0 0 2 12s3.6 7 10 7a10 10 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>';
    for (const input of document.querySelectorAll('input[type="password"]')) {
      const wrap = document.createElement("span");
      wrap.className = "pass-wrap";
      input.replaceWith(wrap);
      wrap.append(input);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "pw-eye";
      const draw = () => {
        const shown = input.type === "text";
        const label = shown ? CONFIG.t.hide_password : CONFIG.t.show_password;
        button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${shown ? EYE_OFF : EYE}</svg>`;
        button.setAttribute("aria-label", label);
        button.title = label;
        button.setAttribute("aria-pressed", String(shown));
      };
      button.addEventListener("click", () => {
        input.type = input.type === "password" ? "text" : "password";
        draw();
        input.focus({ preventScroll: true });
      });
      // Перед отправкой — снова скрыть: браузер не запомнит пароль как обычный текст.
      input.form?.addEventListener("submit", () => { input.type = "password"; });
      draw();
      wrap.append(button);
    }
  })();
