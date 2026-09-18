import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ApiClient, AutomationStatus, LatestReport } from "../api/client";
import { Today } from "./Today";

it("shows the honest first-run state by default", async () => {
  const client = {
    automationStatus: vi.fn().mockResolvedValue({
      enabled: false,
      latest_completed_session: null,
      job: null,
      run: null,
      next_due_session: null,
      next_due_time: null,
      universe: null,
      report: null,
      notification: null,
    } satisfies AutomationStatus),
    latestReport: vi.fn().mockResolvedValue({ run_id: null, state: "MISSING", attempts: 0, url: null, error: null } satisfies LatestReport),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={queryClient}>
      <Today client={client} />
    </QueryClientProvider>,
  );

  expect(await screen.findByRole("heading", { name: "今日報告" })).toBeTruthy();
  expect(screen.getByText(/尚未建立每日報告/)).toBeTruthy();
  expect(screen.getByText(/自動掃描未啟用/)).toBeTruthy();
});

it("shows an in-progress run with an explicit session and coverage", async () => {
  const client = {
    automationStatus: vi.fn().mockResolvedValue({
      enabled: true,
      latest_completed_session: "2026-09-16",
      job: { id: "job-current", session: "2026-09-17", attempt: 1, run_id: "run-current" },
      run: {
        id: "run-current",
        state: "RUNNING",
        progress: { phase: "market_data", processed_symbols: 200, total_symbols: 503, updated_at: "2026-09-17T22:00:00Z" },
        counts: null,
      },
      next_due_session: "2026-09-18",
      next_due_time: "2026-09-18T22:00:00Z",
      universe: {
        snapshot_id: "snapshot-1",
        source_url: "https://example.test/sp500",
        source_license: "CC BY-SA 4.0",
        source_revision: "12345",
        retrieved_at: "2026-09-17T20:00:00Z",
        member_count: 503,
        freshness: "CURRENT",
      },
      report: null,
      notification: null,
    } satisfies AutomationStatus),
    latestReport: vi.fn().mockResolvedValue({ run_id: null, state: "MISSING", attempts: 0, url: null, error: null } satisfies LatestReport),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={queryClient}>
      <Today client={client} />
    </QueryClientProvider>,
  );

  expect(await screen.findByRole("heading", { name: /2026-09-17/ })).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("40");
  expect(screen.getByText(/200 \/ 503/)).toBeTruthy();
  expect(screen.getByText(/market_data/)).toBeTruthy();
  expect(screen.getByRole("link", { name: /資料來源/ }).getAttribute("href")).toBe("https://example.test/sp500");
});

it("does not claim that a stale universe can start a fresh scan", async () => {
  const client = {
    automationStatus: vi.fn().mockResolvedValue({
      enabled: true,
      latest_completed_session: "2026-09-15",
      job: null,
      run: null,
      next_due_session: "2026-09-17",
      next_due_time: null,
      universe: {
        snapshot_id: "snapshot-old",
        source_url: "https://example.test/sp500",
        source_license: "CC BY-SA 4.0",
        source_revision: "12000",
        retrieved_at: "2026-09-08T20:00:00Z",
        member_count: 503,
        freshness: "STALE",
      },
      report: null,
      notification: null,
    } satisfies AutomationStatus),
    latestReport: vi.fn().mockResolvedValue({ run_id: null, state: "MISSING", attempts: 0, url: null, error: null } satisfies LatestReport),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={queryClient}>
      <Today client={client} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText(/快照已過期，唔會開始新掃描/)).toBeTruthy();
});

it("shows a failed run from the exact terminal run shape", async () => {
  const client = {
    automationStatus: vi.fn().mockResolvedValue({
      enabled: true,
      latest_completed_session: "2026-09-16",
      job: { id: "job-failed", session: "2026-09-17", attempt: 1, run_id: "run-failed" },
      run: {
        id: "run-failed", state: "FAILED", progress: null,
        counts: { requested: 503, evaluated: 400, excluded: 80, data_error: 23, candidate: 8 },
      },
      next_due_session: "2026-09-18",
      next_due_time: null,
      universe: null,
      report: null,
      notification: null,
    } satisfies AutomationStatus),
    latestReport: vi.fn().mockResolvedValue({ run_id: null, state: "MISSING", attempts: 0, url: null, error: null } satisfies LatestReport),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><Today client={client} /></QueryClientProvider>);

  expect(await screen.findByRole("heading", { name: "2026-09-17 掃描失敗" })).toBeTruthy();
  expect(screen.getByText(/覆蓋 503 \/ 503/)).toBeTruthy();
});

it("links to the immutable file selected by the latest report endpoint", async () => {
  const client = {
    automationStatus: vi.fn().mockResolvedValue({
      enabled: true, latest_completed_session: "2026-09-17", job: null, run: null,
      next_due_session: null, next_due_time: null, universe: null,
      report: { run_id: "older-run", state: "PUBLISHED", attempts: 1, url: "/api/v1/reports/older-run", error: null }, notification: null,
    } satisfies AutomationStatus),
    latestReport: vi.fn().mockResolvedValue({ run_id: "latest-run", state: "PUBLISHED", attempts: 1, url: "/api/v1/reports/latest-run", error: null } satisfies LatestReport),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><Today client={client} /></QueryClientProvider>);

  expect((await screen.findByRole("link", { name: "開啟最新報告" })).getAttribute("href"))
    .toBe("/api/v1/reports/latest-run");
});
