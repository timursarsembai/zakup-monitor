// «Анализ рынка»: вкладки и «Динамика» (период, показатель; данные — /analytics/trends).
// Подписи — data-labels у [data-trends].
  // Динамика рынка: период и показатель выбираются кнопками, данные — /analytics/trends.
  (() => {
    const root = document.querySelector("[data-trends]");
    const C = window.Charts, $ = (s) => root.querySelector(s);
    const T = JSON.parse(root.dataset.labels);
    const KEYS = ["lots", "announcements", "amount"];
    let data = null, metric = "lots", days = 30, started = false;
    try { days = Number(localStorage.getItem("zakup-trend-days")) || 30; } catch (e) {}
    const chart = () => {
      const labels = data.labels.map(C.short);
      $("[data-t-chart]").innerHTML = C.bars([{values: data.series[metric]}], labels, Math.ceil(labels.length / 8),
        (v) => C.num(v, metric === "amount" ? 1 : 0)) + `<p class="muted">${C.esc(T[data.unit])}</p>`;
      for (const b of $("[data-t-metrics]").children) b.setAttribute("aria-pressed", String(b.dataset.metric === metric));
    };
    const render = () => {
      $("[data-t-range]").textContent = `${C.short(data.start)} — ${C.short(data.end)}`;
      $("[data-t-kpis]").innerHTML = C.kpis(KEYS.map((k) => ({label: T[k], value: C.num(data.totals[k]), change: data.change[k]})), T.vs);
      $("[data-t-metrics]").innerHTML = KEYS.map((k) => `<button type="button" data-metric="${k}">${C.esc(T[k])}</button>`).join("");
      chart();
      $("[data-t-week]").innerHTML = C.bars([{values: data.weekdays.lots, name: T.lots}, {values: data.weekdays.announcements, cls: "b", name: T.announcements}], T.wd)
        + C.legend(T.lots, T.announcements);
      $("[data-t-hours]").innerHTML = C.bars([{values: data.hours}], data.hours.map((_, h) => String(h)), 3);
      const top = Math.max(1, ...data.customers.map((c) => c[2]));
      $("[data-t-customers]").innerHTML = C.rows(data.customers.map(([n, c, t]) => [`${n} · ${C.num(c)}`, `${C.num(t / 1e6, 1)} ${T.mln}`, t / top]));
      $("[data-t-note]").textContent = `${T.since} ${C.short(data.since)}.${data.since.slice(0, 4)}`;
    };
    const load = () => {
      for (const b of root.querySelectorAll("[data-days]")) b.setAttribute("aria-pressed", String(Number(b.dataset.days) === days));
      $("[data-t-loading]").hidden = false;
      fetch(`/analytics/trends?days=${days}`).then((r) => r.ok ? r.json() : null).then((json) => {
        $("[data-t-loading]").hidden = true;
        if (!json) return;
        data = json; $("[data-t-body]").hidden = false; render();
      });
    };
    root.addEventListener("click", (event) => {
      const p = event.target.closest("[data-days]"), m = event.target.closest("[data-metric]");
      if (p) { days = Number(p.dataset.days); try { localStorage.setItem("zakup-trend-days", days); } catch (e) {} load(); }
      if (m && data) { metric = m.dataset.metric; chart(); }
    });
    // Грузим, когда вкладку «Динамика» открыли.
    const panel = root.closest("[data-tab-panel]");
    new MutationObserver(() => { if (!panel.hidden && !started) { started = true; load(); } })
      .observe(panel, {attributes: true, attributeFilter: ["hidden"]});
    window.addEventListener("load", () => { if (!panel.hidden && !started) { started = true; load(); } });
  })();
  const tabs = [...document.querySelectorAll("[data-tab]")];
  const showTab = (name) => {
    if (!tabs.some((t) => t.dataset.tab === name)) name = tabs[0].dataset.tab;
    for (const t of tabs) t.setAttribute("aria-selected", String(t.dataset.tab === name));
    for (const p of document.querySelectorAll("[data-tab-panel]")) p.hidden = p.dataset.tabPanel !== name;
  };
  for (const t of tabs) t.addEventListener("click", (event) => {
    event.preventDefault(); history.replaceState(null, "", "#" + t.dataset.tab); showTab(t.dataset.tab);
  });
  showTab(location.hash.slice(1));
