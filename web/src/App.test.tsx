import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ApiClient, AutomationStatus, LatestReport } from "./api/client";
import { useAuth } from "./auth";
import { App, reportPath } from "./App";

vi.mock("./auth", () => ({ useAuth: vi.fn() }));

it("defaults to Today and keeps manual scanning under Advanced", async () => {
  const client = {
    automationStatus: vi.fn().mockResolvedValue({ enabled: false, latest_completed_session: null, job: null, run: null, next_due_session: null, next_due_time: null, universe: null, report: null, notification: null } satisfies AutomationStatus),
    latestReport: vi.fn().mockResolvedValue({ run_id: null, state: "MISSING", attempts: 0, url: null, error: null } satisfies LatestReport),
    currentSession: vi.fn().mockResolvedValue({ as_of_session: "2026-09-17", reference_session: "2026-09-17" }),
    watchlists: vi.fn().mockResolvedValue([]),
  } as unknown as ApiClient;
  vi.mocked(useAuth).mockReturnValue({
    client, connected: true, checking: false, retrySession: vi.fn(), connect: vi.fn(), disconnect: vi.fn(),
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><App queryClient={queryClient} /></QueryClientProvider>);

  expect(await screen.findByRole("heading", { name: "今日報告" })).toBeTruthy();
  for (const name of ["今日", "歷史", "設定", "進階"]) expect(screen.getByRole("button", { name })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "名單" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "進階" }));
  expect(await screen.findByRole("heading", { name: "名單" })).toBeTruthy();
});

it("turns a notification report query into the authenticated report path", () => {
  expect(reportPath("?report=00000000-0000-4000-8000-000000000001"))
    .toBe("/api/v1/reports/00000000-0000-4000-8000-000000000001");
  expect(reportPath("?report=https://evil.example/")).toBeNull();
});
