import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ApiClient, AutomationStatus } from "../api/client";
import { Settings } from "./Settings";

const paused: AutomationStatus = {
  enabled: false,
  latest_completed_session: null,
  job: null,
  run: null,
  next_due_session: "2026-09-18",
  next_due_time: null,
  universe: null,
  report: null,
  notification: null,
};

it("enables automation only after the user asks", async () => {
  const client = {
    automationStatus: vi.fn().mockResolvedValue(paused),
    enableAutomation: vi.fn().mockResolvedValue({ ...paused, enabled: true }),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={queryClient}>
      <Settings client={client} />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "啟用自動掃描" }));
  expect(await screen.findByText("自動掃描已啟用")).toBeTruthy();
  expect(client.enableAutomation).toHaveBeenCalledTimes(1);
});

it("pauses enabled automation", async () => {
  const client = {
    automationStatus: vi.fn().mockResolvedValue({ ...paused, enabled: true }),
    pauseAutomation: vi.fn().mockResolvedValue(paused),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><Settings client={client} /></QueryClientProvider>);

  fireEvent.click(await screen.findByRole("button", { name: "暫停自動掃描" }));
  expect(await screen.findByText("自動掃描已暫停")).toBeTruthy();
  expect(client.pauseAutomation).toHaveBeenCalledTimes(1);
});

it("shows the truthful pending and error states for Run now", async () => {
  let rejectRun!: (cause: Error) => void;
  const client = {
    automationStatus: vi.fn().mockResolvedValue({ ...paused, enabled: true }),
    runAutomationNow: vi.fn().mockImplementation(() => new Promise((_resolve, reject) => { rejectRun = reject; })),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><Settings client={client} /></QueryClientProvider>);

  fireEvent.click(await screen.findByRole("button", { name: "立即執行" }));
  expect((await screen.findByRole("button", { name: "正在排入…" })).hasAttribute("disabled")).toBe(true);
  rejectRun(new Error("queue unavailable"));
  expect((await screen.findByRole("alert")).textContent).toContain("立即執行失敗");
});
