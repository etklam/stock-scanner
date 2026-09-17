import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { claimRecovery, clearPending, loadPending, releaseRecovery, savePending } from "../state/pending";
import { ListsScan } from "./ListsScan";

const watchlist = {
  id: "watchlist-1",
  name: "default",
  revision: 1,
  symbols: ["DEMO"],
};

function renderScreen(overrides: Partial<ApiClient> = {}) {
  const api = {
    currentSession: vi.fn().mockResolvedValue({ as_of_session: "2026-09-04", reference_session: "2026-09-03" }),
    watchlists: vi.fn().mockResolvedValue([watchlist]),
    submitScan: vi.fn().mockResolvedValue({ id: "scan-1", state: "QUEUED" }),
    scanStatus: vi.fn().mockResolvedValue({ id: "scan-1", state: "SUCCEEDED" }),
    createWatchlist: vi.fn(),
    replaceSymbols: vi.fn(),
    ...overrides,
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ListsScan client={api} onOpenRun={vi.fn()} />
    </QueryClientProvider>,
  );
  return api;
}

afterEach(() => {
  clearPending();
  vi.restoreAllMocks();
});

describe("ListsScan", () => {
  it("submits the first watchlist without requiring a select interaction", async () => {
    const api = renderScreen();
    const submit = await screen.findByRole("button", { name: "開始掃描" });

    await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(submit);

    await waitFor(() => expect(api.submitScan).toHaveBeenCalledTimes(1));
    expect(api.submitScan).toHaveBeenCalledWith(
      { watchlist_id: "watchlist-1", as_of_session: null, data_mode: "auto" },
      expect.any(String),
    );
  });

  it("keeps a recovered scan intent until polling reaches a terminal state", async () => {
    const body = { watchlist_id: "watchlist-1", as_of_session: null, data_mode: "auto" } as const;
    savePending({ key: "recovery-key", body, scanId: null });
    const submitScan = vi.fn().mockResolvedValue({ id: "scan-2", state: "QUEUED" });
    const scanStatus = vi.fn(() => new Promise<never>(() => undefined));
    const api = renderScreen({ submitScan, scanStatus });

    await waitFor(() => expect(submitScan).toHaveBeenCalledWith(body, "recovery-key"));
    await waitFor(() => expect(loadPending()).toMatchObject({ key: "recovery-key", scanId: "scan-2" }));
    expect(api.scanStatus).toHaveBeenCalledWith("scan-2", expect.anything());
  });

  it("clears only the matching intent after a terminal poll", async () => {
    savePending({
      key: "known-key",
      body: { watchlist_id: "watchlist-1", as_of_session: null, data_mode: "auto" },
      scanId: "scan-known",
    });
    const scanStatus = vi.fn().mockResolvedValue({ id: "scan-known", state: "SUCCEEDED" });
    const api = renderScreen({ scanStatus });

    await waitFor(() => expect(loadPending()).toBeNull());
    expect(api.submitScan).not.toHaveBeenCalled();
    expect(scanStatus).toHaveBeenCalledWith("scan-known", expect.anything());
  });

  it("releases a failed recovery claim so the same intent can be retried", async () => {
    savePending({
      key: "retryable-key",
      body: { watchlist_id: "watchlist-1", as_of_session: null, data_mode: "auto" },
      scanId: null,
    });
    const submitScan = vi.fn().mockRejectedValue(new TypeError("offline"));
    renderScreen({ submitScan });

    await screen.findByText(/offline/);
    expect(claimRecovery("retryable-key")).toBe(true);
    releaseRecovery("retryable-key");
  });

  it("retries an unknown manual outcome with the original key and body", async () => {
    const submitScan = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network lost"))
      .mockResolvedValueOnce({ id: "scan-3", state: "QUEUED" });
    const api = renderScreen({ submitScan });
    const submit = await screen.findByRole("button", { name: "開始掃描" });
    await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(submit);
    await screen.findByText(/network lost/);
    await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(submit);

    await waitFor(() => expect(submitScan).toHaveBeenCalledTimes(2));
    expect(submitScan.mock.calls[1]).toEqual(submitScan.mock.calls[0]);
    const firstCall = submitScan.mock.calls[0];
    expect(firstCall).toBeDefined();
    expect(loadPending()).toMatchObject({ key: firstCall?.[1], scanId: "scan-3" });
    await waitFor(() => expect(api.scanStatus).toHaveBeenCalledWith("scan-3", expect.anything()));
  });
});
