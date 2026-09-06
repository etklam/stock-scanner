import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Series } from "../api/client";
import { Chart } from "./Chart";
import { reasonLabel } from "../state/labels";

function series(overrides: Partial<Series> = {}): Series {
  const sessions = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"];
  return {
    run_id: "run",
    instrument_id: "instr",
    symbol: "GOOD",
    sessions,
    closes: [10, 10.5, 11, 12],
    sma: { sma10: [null, 10.2, 10.6, 11.2] },
    window_start: "2026-08-04",
    window_end: "2026-08-06",
    close_resistance: 11,
    displayed_sessions: 4,
    price_basis: "split_adjusted_close",
    as_of_session: "2026-08-06",
    ...overrides,
  };
}

describe("Chart", () => {
  it("renders close, resistance and window band", () => {
    const { container } = render(<Chart series={series()} />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(screen.getByText(/收市阻力 11\.00/)).toBeTruthy();
    expect(container.innerHTML).toContain("收市價與均線圖");
  });

  it("gaps missing SMA values instead of drawing zeros", () => {
    const { container } = render(<Chart series={series()} />);
    const paths = [...container.querySelectorAll("path")].map((path) => path.getAttribute("d"));
    const smaPath = paths.find((path) => path?.startsWith("M")); // first M starts at first non-null
    expect(smaPath).toBeTruthy();
    // The leading null must not appear as a point at y for value 0.
    expect(smaPath).not.toContain(",NaN");
    expect(screen.getByText(/SMA10/)).toBeTruthy();
  });

  it("survives a constant series (flat domain padded, no NaN)", () => {
    const { container } = render(
      <Chart series={series({ closes: [7, 7, 7, 7], sma: {}, close_resistance: null })} />,
    );
    const paths = [...container.querySelectorAll("path")].map((path) => path.getAttribute("d"));
    expect(paths.every((path) => path !== null && !path.includes("NaN"))).toBe(true);
  });

  it("renders nothing noisy when the series is empty", () => {
    render(<Chart series={series({ closes: [], sma: {} })} />);
    expect(screen.getByText("無可用價格資料。")).toBeTruthy();
  });

  it("maps unknown reason codes to a readable fallback", () => {
    expect(reasonLabel("SOME_FUTURE_CODE")).toBe("未知原因（SOME_FUTURE_CODE）");
    expect(reasonLabel("MOMENTUM_GATE_FAILED")).toBe("動量條件未達標");
  });
});
