import { defineConfig } from "@playwright/test";

// Browser E2E against the REAL local stack: a qscan serve subprocess (fixture
// provider, loopback, seeded SYNTHETIC demo data) plus the built UI from
// src/qscan/interfaces/web/dist. No third-party network: Chrome runs with the
// --host-resolver-rules lockdown so any stray external request fails loudly.
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  forbidOnly: true,
  reporter: "list",
  use: {
    baseURL: process.env.QSCAN_E2E_URL ?? "http://127.0.0.1:8944",
    launchOptions: {
      channel: "chrome",
      args: [
        // No third-party network in browser tests: every non-local DNS lookup
        // is poisoned so an accidental CDN/analytics fetch fails immediately.
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
      ],
    },
  },
  webServer: {
    command: "node e2e/serve.mjs",
    url: process.env.QSCAN_E2E_URL ?? "http://127.0.0.1:8944/health/live",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
