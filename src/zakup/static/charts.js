// Простые графики без библиотек: столбики (SVG), полоски-строки и тепловая карта.
// Цвета — из темы сайта (var(--accent), var(--muted)); стили — .chart в base.html.
(() => {
  const num = (v, d = 0) => Number(v || 0).toLocaleString("ru-RU", {maximumFractionDigits: d});
  const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const short = (iso) => { const [, m, d] = String(iso).split("-"); return `${d}.${m}`; };

  // series: [{values, cls: "a"|"b", name}] — рядом друг с другом; every — подпись через N столбиков.
  const bars = (series, labels, every = 1, fmt = num) => {
    const n = Math.max(labels.length, 1), groups = series.length, W = 600, H = 160, top = 14, bottom = 18;
    const max = Math.max(1, ...series.flatMap((s) => s.values));
    const slot = W / n, bw = Math.max(1, (slot * 0.78) / groups);
    let out = `<svg class="chart" viewBox="0 0 ${W} ${H + bottom}" role="img"><text x="0" y="9">${esc(fmt(max))}</text>`;
    series.forEach((s, g) => s.values.forEach((v, i) => {
      const h = Math.max(v ? 2 : 0, (v / max) * (H - top));
      const x = i * slot + slot * 0.11 + g * bw;
      out += `<rect class="${s.cls || "a"}" x="${x.toFixed(1)}" y="${(H - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="1.5"><title>${esc(labels[i])}: ${esc(fmt(v))}${s.name ? " — " + esc(s.name) : ""}</title></rect>`;
    }));
    labels.forEach((l, i) => {
      if (i % every === 0 || i === n - 1) out += `<text x="${(i * slot + slot / 2).toFixed(1)}" y="${H + 13}" text-anchor="middle">${esc(l)}</text>`;
    });
    return out + "</svg>";
  };

  const legend = (...names) => `<div class="chart-legend">${names.map((n, i) =>
    `<span><i class="${i ? "b" : "a"}"></i>${esc(n)}</span>`).join("")}</div>`;

  // items: [[подпись, значение, доля 0..1 или null, ссылка?]]
  const rows = (items, empty = "—") => items.length ? `<div class="chart-rows">${items.map(([name, value, share, href]) =>
    `<div class="chart-row"><span>${href ? `<a href="${esc(href)}">${esc(name)}</a>` : esc(name)}</span><b>${esc(value)}</b>${share != null
      ? `<div class="chart-bar"><i style="width:${Math.max(1, Math.min(100, share * 100)).toFixed(0)}%"></i></div>` : ""}</div>`).join("")}</div>`
    : `<p class="muted">${esc(empty)}</p>`;

  // grid 7×24 (пн…вс × час); wd — подписи дней.
  const heat = (grid, wd) => {
    const max = Math.max(1, ...grid.flat());
    let out = '<div class="chart-heat"><span></span>';
    for (let h = 0; h < 24; h++) out += `<span class="h">${h % 3 === 0 ? h : ""}</span>`;
    grid.forEach((row, d) => {
      out += `<span class="d">${esc(wd[d])}</span>`;
      row.forEach((v, h) => { out += `<i style="opacity:${v ? (0.12 + 0.88 * v / max).toFixed(2) : 0.04}" title="${esc(wd[d])} ${h}:00 — ${esc(num(v))}"></i>`; });
    });
    return out + "</div>";
  };

  // KPI-карточки: [{label, value, change}] — change в процентах к прошлому периоду.
  const kpis = (items, vs) => items.map(({label, value, change, hint}) => {
    const tag = change == null ? "" : `<span class="${change >= 0 ? "up" : "down"}">${change >= 0 ? "+" : ""}${change}% ${esc(vs)}</span>`;
    return `<div class="chart-kpi"${hint ? ` title="${esc(hint)}"` : ""}><span>${esc(label)}</span><b>${esc(value)}</b>${tag}</div>`;
  }).join("");

  window.Charts = {num, esc, short, bars, legend, rows, heat, kpis};
})();
