// Калькулятор маржи (/calculator): всё считается в браузере при каждом изменении поля.
// Подписи предупреждений — data-labels у формы. Налоговый режим и личные настройки
// (ставки, НДС, гарантия) запоминаются в браузере.
(() => {
  const form = document.querySelector("[data-calc-form]");
  if (!form) return;
  const out = document.querySelector("[data-calc-out]");
  const T = JSON.parse(form.dataset.labels || "{}");
  const VAT = 0.16;
  const KEEP = ["regime", "tax_rate", "vat", "cost_vat", "secure", "guarantee_fee", "money_rate", "kz_producer"];
  const STORE = "zakup-calc";
  const f = form.elements;
  const num = (name) => { const v = parseFloat(String(f[name].value).replace(",", ".")); return Number.isFinite(v) && v > 0 ? v : 0; };
  const fmt = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 0});
  const money = (v) => `${fmt.format(Math.round(v))} ₸`;
  const minus = (v) => (Math.round(v) ? `−${money(v)}` : money(0));
  const mini = document.querySelector("[data-calc-mini]");
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const set = (key, text) => { const el = out.querySelector(`[data-out="${key}"]`); if (el) el.textContent = text; };

  // Поля, которые не зависят от цены заявки.
  const terms = () => {
    const regime = f.regime.value;
    const vat = regime !== "simple" && f.vat.checked;
    return {
      amount: num("amount"), cost: num("cost"), extra: num("extra"), regime, rate: num("tax_rate") / 100,
      vat, costVat: vat && f.cost_vat.checked,
      bidPct: num("bid_pct") / 100, contractPct: f.kz_producer.checked ? 0 : num("contract_pct") / 100,
      advancePct: num("advance_pct") / 100, guarantee: f.secure.value === "guarantee",
      fee: num("guarantee_fee") / 100, dumpPct: num("dump_pct") / 100, dumpAllowed: f.dump_allowed.checked,
      bidDays: num("bid_days"), days: num("supply_days") + num("pay_days"), perDay: num("money_rate") / 100 / 365,
      kz: f.kz_producer.checked,
    };
  };

  // Всё о сделке при цене заявки price (с НДС, если плательщик).
  const deal = (t, price) => {
    const revenue = t.vat ? price / (1 + VAT) : price;
    const cost = t.costVat ? t.cost / (1 + VAT) : t.cost;
    const vatPay = t.vat ? (price - revenue) - (t.costVat ? t.cost - cost : 0) : 0;
    const bidSec = t.amount * t.bidPct;
    const contractSec = price * t.contractPct;
    const advance = price * t.advancePct;
    const advanceSec = t.kz ? 0 : advance;
    const floor = t.dumpPct ? t.amount * (1 - t.dumpPct) : 0;
    const below = floor > 0 && price < floor;
    const anti = below && t.dumpAllowed ? floor - price : 0;
    const goods = Math.max(0, t.cost + t.extra - advance);
    const secured = contractSec + advanceSec;
    const fees = t.guarantee ? (bidSec + secured) * t.fee : 0;
    const frozen = (t.guarantee ? 0 : bidSec * t.bidDays + secured * t.days) + (anti + goods) * t.days;
    const moneyCost = frozen * t.perDay;
    const base = t.regime === "simple" ? revenue : Math.max(0, revenue - cost - t.extra - fees);
    const tax = base * t.rate;
    const profit = revenue - cost - t.extra - fees - moneyCost - tax;
    const peak = Math.max(t.guarantee ? 0 : bidSec, (t.guarantee ? 0 : secured) + anti + goods);
    return {price, revenue, cost, vatPay, bidSec, contractSec, advanceSec, anti, goods, fees, moneyCost, tax,
            profit, peak, floor, rejected: below && !t.dumpAllowed, margin: revenue ? profit / revenue : 0};
  };

  // Самая низкая цена, при которой ok(deal) — прибыль растёт с ценой, поэтому делением пополам.
  const lowest = (t, ok) => {
    let lo = 0, hi = Math.max(t.amount * 3, (t.cost + t.extra) * 5, 1000);
    if (!ok(deal(t, hi))) return null;
    for (let i = 0; i < 60; i++) {
      const mid = (lo + hi) / 2;
      if (ok(deal(t, mid))) hi = mid; else lo = mid;
    }
    return Math.ceil(hi);
  };

  const render = () => {
    const t = terms();
    const price = num("price");
    out.querySelector('[data-row="vat"]').hidden = !t.vat;
    f.vat.disabled = t.regime === "simple";
    f.cost_vat.disabled = !t.vat;
    if (mini) mini.dataset.empty = price ? "" : "1";
    if (!price) {
      for (const el of out.querySelectorAll("[data-out]")) if (el.dataset.out !== "warnings") el.textContent = "—";
      out.querySelector('[data-out="warnings"]').innerHTML = "";
      return;
    }
    const d = deal(t, price);
    const profitEl = out.querySelector('[data-out="profit"]');
    set("profit", money(d.profit));
    profitEl.className = d.profit < 0 ? "neg" : "pos";
    set("margin", `${(d.margin * 100).toFixed(1).replace(".", ",")}%`);
    if (mini) {
      const mp = mini.querySelector('[data-mini="profit"]');
      mp.textContent = money(d.profit);
      mp.className = d.profit < 0 ? "neg" : "pos";
      mini.querySelector('[data-mini="margin"]').textContent = `${(d.margin * 100).toFixed(1).replace(".", ",")}%`;
    }
    const even = lowest(t, (x) => x.profit >= 0);
    set("breakeven", even === null ? "—" : money(even));
    set("peak", money(d.peak));
    set("price", money(d.price));
    set("vat_pay", money(d.vatPay));
    set("revenue", money(d.revenue));
    set("cost", minus(d.cost));
    set("extra", minus(t.extra));
    set("fees", minus(d.fees));
    set("money", minus(d.moneyCost));
    set("tax", minus(d.tax));
    set("profit2", money(d.profit));
    set("bid_sec", t.guarantee && d.bidSec ? T.guarantee : money(d.bidSec));
    set("contract_sec", t.guarantee && d.contractSec ? T.guarantee : money(d.contractSec));
    set("advance_sec", t.guarantee && d.advanceSec ? T.guarantee : money(d.advanceSec));
    set("anti", money(d.anti));
    set("goods", money(d.goods));
    const target = parseFloat(String(out.querySelector("[data-target]").value).replace(",", ".")) / 100;
    const want = Number.isFinite(target) ? lowest(t, (x) => x.margin >= target) : null;
    set("target_price", want === null ? "—" : money(want) + (t.amount && want > t.amount ? ` · ${T.above}` : ""));
    const warn = [];
    if (t.amount && price > t.amount) warn.push(["bad", T.above]);
    if (d.rejected) warn.push(["bad", T.rejected.replace("{floor}", money(d.floor))]);
    else if (d.anti) warn.push(["", T.anti.replace("{sum}", money(d.anti)).replace("{floor}", money(d.floor))]);
    if (d.profit < 0) warn.push(["bad", T.loss]);
    out.querySelector('[data-out="warnings"]').innerHTML =
      warn.map(([cls, text]) => `<p class="calc-warn ${cls}">${esc(text)}</p>`).join("");
  };

  // Режим налога меняет ставку на обычную для него (своя ставка — после выбора режима).
  f.regime.addEventListener("change", () => {
    f.tax_rate.value = f.regime.selectedOptions[0].dataset.rate;
  });
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
    for (const key of KEEP) {
      if (!(key in saved)) continue;
      if (f[key].type === "checkbox") f[key].checked = Boolean(saved[key]); else f[key].value = saved[key];
    }
  } catch (e) { /* без сохранённых настроек */ }
  const remember = () => {
    try {
      localStorage.setItem(STORE, JSON.stringify(Object.fromEntries(
        KEEP.map((key) => [key, f[key].type === "checkbox" ? f[key].checked : f[key].value]))));
    } catch (e) { /* приватный режим */ }
  };
  form.addEventListener("input", () => { render(); remember(); });
  form.addEventListener("change", () => { render(); remember(); });
  out.querySelector("[data-target]").addEventListener("input", render);
  render();
  // Строка-итог на телефоне видна, пока сам результат за экраном; нажатие — к результату.
  if (mini) {
    let resultVisible = false;
    const toggle = () => { mini.hidden = resultVisible || mini.dataset.empty === "1"; };
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(([entry]) => { resultVisible = entry.isIntersecting; toggle(); }).observe(out);
    }
    form.addEventListener("input", toggle);
    mini.addEventListener("click", () => out.scrollIntoView({behavior: "smooth", block: "start"}));
    toggle();
  }
})();
