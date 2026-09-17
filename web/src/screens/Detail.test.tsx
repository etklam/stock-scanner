import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import { RunDetail, SymbolDetail } from "./Detail";

const series = {
  run_id: "run", instrument_id: "instr", symbol: "DEMO",
  sessions: ["2026-08-01"], closes: [10], sma: {}, displayed_sessions: 1,
  price_basis: "split_adjusted_close", as_of_session: "2026-08-01",
  window_start: null, window_end: null, close_resistance: null,
};

const row = {
  instrument: { id: "instr", display_symbol: "DEMO", exchange: "NYSE", currency: "USD", instrument_type: "EQUITY" },
  category: "evaluated", rank: 1, warnings: ["UNDEFINED_CONTRACTION"], reasons: [],
  analysis: {
    instrument_id: "instr", context: {}, config_hash: "hash", evaluation_status: "EVALUATED",
    is_candidate: true, stage: "NEAR_CLOSE_RESISTANCE", score: 10, features: { return_126: null }, reasons: [],
    selected_window: {
      window_sessions: 20, available: true, eligible: true, features: {}, score_breakdown: {}, score: 10,
      stage: "NEAR_CLOSE_RESISTANCE", is_close_break: false,
      reasons: [{ code: "NEAR_CLOSE_RESISTANCE", parameters: {} }],
    }, alternative_windows: [], liquidity: "NOT_EVALUATED",
  },
  alternative_windows: [{
    window_sessions: 40, available: true, eligible: false, features: {}, score_breakdown: {}, score: null,
    stage: null, is_close_break: false, reasons: [{ code: "BASE_RANGE_GATE_FAILED", parameters: {} }],
  }],
};

function renderDetail(seriesResult: unknown) {
  const api = {
    resultDetail: vi.fn().mockResolvedValue(row),
    series: vi.fn().mockImplementation(() =>
      seriesResult instanceof Error ? Promise.reject(seriesResult) : Promise.resolve(seriesResult),
    ),
  } as unknown as ApiClient;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><SymbolDetail client={api} runId="run" instrumentId="instr" /></QueryClientProvider>);
}

describe("SymbolDetail", () => {
  it("keeps reasons and diagnostics visible when the chart series fails", async () => {
    renderDetail(new ApiError(404, { code: "NO_DATA", message: "missing", details: {}, request_id: "r" }));
    expect(await screen.findByText("入選原因與分項（候選／評估）")).toBeTruthy();
    expect(screen.getByText("接近收市阻力")).toBeTruthy();
    expect(await screen.findByText(/圖表資料缺失/)).toBeTruthy();
  });

  it("classifies alternative gates, warnings, and unavailable features", async () => {
    renderDetail(Promise.resolve(series));
    expect(await screen.findByText("其他窗口 gate")).toBeTruthy();
    expect(screen.getByText(/40 日/)).toBeTruthy();
    expect(screen.getByText(/整理區間過寬/)).toBeTruthy();
    expect(screen.getByText(/資料警告/)).toBeTruthy();
    expect(screen.getByText(/收窄指標無法計算（分母為零）/)).toBeTruthy();
    expect(screen.getByText(/不可用指標/)).toBeTruthy();
    expect(screen.getByText(/return_126/)).toBeTruthy();
  });
});

describe("RunDetail", () => {
  it("shows the selected-window reason in the results table", async () => {
    const api = {
      scanStatus: vi.fn().mockResolvedValue({
        id: "run", state: "SUCCEEDED", as_of_session: "2026-08-01", reference_session: "2026-07-31",
        watchlist_revision: 1, data_mode: "auto", counts: { requested: 1, evaluated: 1, excluded: 0, data_error: 0, candidate: 1 },
        warnings: [], links: {},
      }),
      resultsPage: vi.fn().mockResolvedValue({ items: [row], next_cursor: null }),
    } as unknown as ApiClient;
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <RunDetail client={api} runId="run" />
      </QueryClientProvider>,
    );

    const resultRow = await screen.findByRole("row", { name: /DEMO/ });
    const cells = within(resultRow).getAllByRole("cell");
    expect(cells[6]?.textContent).toContain("接近收市阻力");
  });
});
