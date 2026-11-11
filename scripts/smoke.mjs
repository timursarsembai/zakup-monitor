// Проверка после выкладки в настоящем браузере (scripts/smoke.sh): на главных страницах
// нет ошибок скриптов, на телефоне шторка фильтра открывается. Тесты на Python этого не ловят:
// 2026-11-09 filters.js падал при загрузке, и фильтр на телефоне не работал.
import { chromium } from "playwright";
const SITE = process.env.SITE || "https://zakup.sarsembai.com";
const pages = ["/", "/lots", "/announcements", "/orgs/customers", "/orgs/suppliers", "/categories", "/analytics", "/pricing"];
const browser = await chromium.launch();
const problems = [];
for (const [width, mobile] of [[1280, false], [390, true]]) {
  const page = await browser.newPage({ viewport: { width, height: 800 }, isMobile: mobile, hasTouch: mobile });
  page.on("pageerror", (error) => problems.push(`${width}px ${page.url()}: ${error.message}`));
  for (const path of pages) await page.goto(SITE + path, { waitUntil: "networkidle" });
  if (!mobile && await page.evaluate(() => document.documentElement.dataset.chips === "desktop")) {
    await page.goto(SITE + "/lots", { waitUntil: "networkidle" });
    await page.click('[data-chip-open="region"]');
    await page.waitForTimeout(300);
    const placed = await page.evaluate(() => {
      const sheet = document.getElementById("filters");
      const chip = document.querySelector('[data-chip-open="region"]').getBoundingClientRect();
      return sheet.classList.contains("open") && Math.abs(sheet.getBoundingClientRect().top - chip.bottom) < 40;
    });
    if (!placed) problems.push("1280px /lots: шторка «Регион» не раскрылась под кнопкой");
    // Подпись списка с галочками меняется сразу при отметке (2026-11-11 оставалась «Весь Казахстан»).
    const label = await page.evaluate(() => {
      const box = document.querySelector('details.multi:has(input[name="region"])');
      const input = box?.querySelector('input[name="region"]:not(:checked)');
      if (!input) return "нет списка";
      input.click();
      const text = box.querySelector("summary").textContent.trim();
      input.click();
      return text === input.closest("label").textContent.trim() ? "" : text;
    });
    if (label) problems.push(`1280px /lots: подпись списка «Регион» не обновилась: ${label}`);
  }
  if (mobile) {
    await page.goto(SITE + "/lots", { waitUntil: "networkidle" });
    await page.tap('[data-chip-open="region"]');
    await page.waitForTimeout(400);
    if (!(await page.evaluate(() => document.getElementById("filters")?.classList.contains("open")))) {
      problems.push("390px /lots: шторка «Регион» не открылась");
    }
  }
}
await browser.close();
if (problems.length) {
  console.log("ПРОВЕРКА В БРАУЗЕРЕ НЕ ПРОШЛА:\n" + problems.join("\n"));
  process.exit(1);
}
console.log("Проверка в браузере: ошибок нет");
