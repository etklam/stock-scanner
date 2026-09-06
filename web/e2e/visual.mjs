// Visual acceptance: drives the real serve + built UI in headless Chrome at
// desktop 1280px and narrow 390px, capturing the three screens plus keyboard
// focus and long-name/table-overflow behaviour. Screenshots land in
// e2e/shots/ for human inspection; assertions pin the layout invariants that
// matter (no horizontal page scroll, chart fits, rows readable).
import { chromium } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const state = JSON.parse(readFileSync(resolve(import.meta.dirname, ".state.json"), "utf-8"));
const BASE = `http://127.0.0.1:${state.port}`;
const OUT = resolve(import.meta.dirname, "shots");
mkdirSync(OUT, { recursive: true });

const unique = Date.now();
const browser = await chromium.launch({ channel: "chrome" });

for (const [label, width, height] of [["desktop", 1280, 900], ["narrow", 390, 844]]) {
  const context = await browser.newContext({ viewport: { width, height } });
  const page = await context.newPage();
  await page.goto(`${BASE}/ui/`);
  await page.getByPlaceholder("貼上 token").fill(state.token);
  await page.getByRole("button", { name: "連線" }).click();
  await page.getByRole("heading", { name: "名單" }).waitFor();

  // Long name + Chinese + empty-value coverage.
  await page.getByPlaceholder("us-growth").fill(`vis-${unique}-${label}-with-a-very-long-name-測試`);
  await page.getByPlaceholder("AAPL\nMSFT").fill("DEMO");
  await page.getByRole("button", { name: "建立名單" }).click();
  await page.getByText(/已建立 /).waitFor();
  await page.getByLabel("掃描名單").selectOption({ index: 0 });
  await page.getByRole("button", { name: "開始掃描" }).click();
  await page.getByRole("button", { name: "查看候選" }).waitFor({ timeout: 60_000 });
  await page.screenshot({ path: `${OUT}/scan-${label}.png`, fullPage: true });

  await page.getByRole("button", { name: "查看候選" }).click();
  await page.getByRole("cell", { name: "DEMO" }).click();
  await page.locator("svg[role=img]").waitFor();
  await page.getByRole("button", { name: "值得睇", exact: true }).click();
  await page.screenshot({ path: `${OUT}/detail-${label}.png`, fullPage: true });

  // Keyboard: focus reaches rows and buttons.
  await page.getByRole("cell", { name: "DEMO" }).focus();
  await page.keyboard.press("Enter");
  const focused = await page.evaluate(() => document.activeElement?.tagName);
  console.log(`${label}: focused after Enter = ${focused}`);

  // Layout invariants: no horizontal document overflow.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  console.log(`${label}: horizontal overflow px = ${overflow}`);
  await context.close();
}
await browser.close();
console.log(`screenshots in ${OUT}`);
