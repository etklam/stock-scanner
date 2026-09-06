import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import { ReviewPanel } from "./Detail";

function harness(
  putReview: unknown,
  reviews: { items: unknown[]; next_cursor: null },
): ReturnType<typeof render> {
  const api = {
    reviews: vi.fn().mockResolvedValue(reviews),
    putReview,
  } as unknown as ApiClient;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ReviewPanel client={api} runId="run" instrumentId="instr" />
    </QueryClientProvider>,
  );
}

async function filledPanel(putReview: unknown) {
  harness(putReview, { items: [], next_cursor: null });
  await waitFor(() => expect(screen.getByRole("radiogroup")).toBeTruthy());
  fireEvent.change(screen.getByLabelText(/備註/), { target: { value: "保留輸入" } });
  fireEvent.click(screen.getByText("值得睇"));
}

describe("ReviewPanel", () => {
  it("shows 已保存 only after a 2xx response, never before", async () => {
    let resolvePut: ((value: unknown) => void) | undefined;
    const putReview = vi.fn(
      () =>
        new Promise((resolve) => {
          resolvePut = resolve;
        }),
    );
    await filledPanel(putReview);
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    expect(screen.queryByText(/已保存/)).toBeNull(); // pending: no fake success
    await vi.waitFor(() => expect(putReview).toHaveBeenCalled());
    resolvePut?.({
      run_id: "run",
      instrument_id: "instr",
      label: "worth_reviewing",
      note: "保留輸入",
      revision: 1,
      updated_at: "2026-09-06T00:00:00Z",
    });
    await screen.findByText(/已保存（revision 1）/);
  });

  it("keeps the user's input and names the conflict on a 409", async () => {
    const putReview = vi.fn().mockRejectedValue(
      new ApiError(409, {
        code: "REVIEW_REVISION_CONFLICT",
        message: "conflict",
        details: {},
        request_id: "r",
      }),
    );
    await filledPanel(putReview);
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    await screen.findByText(/revision 衝突/);
    expect((screen.getByLabelText(/備註/) as HTMLTextAreaElement).value).toBe("保留輸入");
    expect(screen.queryByText(/已保存/)).toBeNull(); // failure is never faked
    expect(screen.getByRole("button", { name: "重新載入最新標記" })).toBeTruthy();
  });

  it("keeps the input and shows the error on a network failure", async () => {
    const putReview = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    await filledPanel(putReview);
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    await screen.findByText(/fetch failed/);
    expect((screen.getByLabelText(/備註/) as HTMLTextAreaElement).value).toBe("保留輸入");
  });

  it("未標記 is not 唔值得睇: three explicit options only", async () => {
    harness({} as ApiClient["putReview"], { items: [], next_cursor: null });
    await screen.findByRole("radiogroup");
    for (const name of ["值得睇", "一般", "唔值得睇"]) {
      expect(screen.getByText(name)).toBeTruthy();
    }
    expect(screen.getByText("未標記 ≠ 唔值得睇")).toBeTruthy();
  });
});
