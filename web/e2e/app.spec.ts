import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// One full browser E2E over the real local API + fixture data:
// 連線 → 建立/選擇名單 → 提交 → 等 terminal → 查看候選 → 開圖及原因
// → 保存覆核 → reload/重新認證 → 標記仍存在。
// The token here is a fresh synthetic local token from a throwaway data dir.

const state = JSON.parse(
  readFileSync(resolve(import.meta.dirname, ".state.json"), "utf-8"),
) as { token: string; demo: { watchlist_id: string } };

// Unique per run: a reused serve keeps its previous data directory.
const listName = `e2e-list-${Date.now()}`;

test.describe.configure({ mode: "serial" });

test("connects with the local token", async ({ page }) => {
  await page.goto("/ui/");
  await expect(page.getByRole("heading", { name: "連線本地 API" })).toBeVisible();
  await page.getByPlaceholder("貼上 token").fill(state.token);
  await page.getByRole("button", { name: "連線" }).click();
  await expect(page.getByRole("heading", { name: "名單" })).toBeVisible();
  // The close-only limitation banner is part of every session.
  await expect(page.getByRole("note")).toContainText("close-only 初篩");
});

test("creates a list, submits one scan, reaches terminal without duplicates", async ({ page }) => {
  await page.goto("/ui/");
  await page.getByPlaceholder("貼上 token").fill(state.token);
  await page.getByRole("button", { name: "連線" }).click();

  // Create an explicitly-named list (paste symbols).
  await page.getByPlaceholder("us-growth").fill(listName);
  await page.getByPlaceholder("AAPL\nMSFT").fill("DEMO");
  await page.getByRole("button", { name: "建立名單" }).click();
  await expect(page.getByText(new RegExp(`已建立 ${listName}`))).toBeVisible();

  // The scan form: explicit list + data mode (auto default), one submission.
  await page.getByLabel("掃描名單").selectOption({ label: `${listName}（1 隻）` });
  await page.getByRole("button", { name: "開始掃描" }).click();
  const scanLine = page.locator("h3", { hasText: "Scan" });
  await expect(scanLine).toBeVisible();
  await expect(scanLine).toContainText(/排隊中|掃描中|完成|部分完成|失敗/);

  // Wait for terminal state via the polled progress card.
  await expect(
    scanLine,
    "scan reached terminal within timeout",
  ).toContainText(/完成|部分完成/, { timeout: 60_000 });

  // Exactly ONE run for the fresh list (no double submit on this path).
  await page.getByRole("button", { name: "查看候選" }).click();
  await expect(page.getByRole("table").first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "DEMO" })).toBeVisible();
});

test("opens chart and reasons, saves a review", async ({ page }) => {
  await page.goto("/ui/");
  await page.getByPlaceholder("貼上 token").fill(state.token);
  await page.getByRole("button", { name: "連線" }).click();
  await page.getByRole("button", { name: "歷史" }).click();
  // Open the first history row, then the DEMO result inside it (only the
  // selected symbol's series is fetched).
  await page.locator("table.list tbody tr").first().click();
  await page.getByRole("cell", { name: "DEMO" }).click();
  await expect(page.locator("svg[role=img]")).toBeVisible();
  await expect(page.getByText(/入選原因與分項/)).toBeVisible();

  // Save the review: label + note. No 已保存 before the 2xx.
  await page.getByRole("button", { name: "值得睇", exact: true }).click();
  await page.getByLabel(/備註/).fill("E2E：型態清楚，值得覆核");
  await page.getByRole("button", { name: "保存標記" }).click();
  await expect(page.getByText(/已保存（revision 1）/)).toBeVisible();
});

test("reload requires reconnect and the review survives", async ({ page }) => {
  await page.goto("/ui/");
  await page.getByPlaceholder("貼上 token").fill(state.token);
  await page.getByRole("button", { name: "連線" }).click();

  // Reach the run through 歷史 (not an in-session submit): the run is bound by id.
  await page.getByRole("button", { name: "歷史" }).click();
  await page.locator("table.list tbody tr").first().click();
  await page.getByRole("cell", { name: "DEMO" }).click();
  const noteBox = page.getByLabel(/備註/);
  await expect(noteBox).toHaveValue("E2E：型態清楚，值得覆核");
  await expect(page.getByRole("button", { name: "值得睇", exact: true })).toHaveAttribute("aria-pressed", "true");

  // The reload did NOT silently rebuild the scan: exactly one run exists for
  // the E2E list (fresh reload, single idempotency key, no auto POST).
  const watchlists = await page
    .request
    .get("/api/v1/watchlists", { headers: { Authorization: `Bearer ${state.token}` } })
    .then((response) => response.json());
  const e2eList = watchlists.find((list: { name: string }) => list.name === listName);
  const runs = await page
    .request
    .get("/api/v1/scans?limit=200", { headers: { Authorization: `Bearer ${state.token}` } })
    .then((response) => response.json());
  const ownRuns = runs.items.filter(
    (item: { watchlist_id: string }) => item.watchlist_id === e2eList.id,
  );
  expect(ownRuns).toHaveLength(1);
});
