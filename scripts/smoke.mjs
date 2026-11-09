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
