import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import { ReviewPanel } from "./Detail";

function harness(
  putReview: unknown,
  reviews: { items: unknown[]; next_cursor: null },
): ReturnType<typeof render> & { api: ApiClient; queryClient: QueryClient } {
  const api = {
    reviews: vi.fn().mockResolvedValue(reviews),
    putReview,
  } as unknown as ApiClient;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <ReviewPanel client={api} runId="run" instrumentId="instr" />
    </QueryClientProvider>,
  );
  return Object.assign(view, { api, queryClient });
}

async function filledPanel(putReview: unknown, reviews = { items: [], next_cursor: null as null }) {
  const view = harness(putReview, reviews);
  await waitFor(() => expect(screen.getByRole("radiogroup")).toBeTruthy());
  fireEvent.change(screen.getByLabelText(/備註/), { target: { value: "保留輸入" } });
  fireEvent.click(screen.getByText("值得睇"));
  return view;
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

  it("writes the successful response into the reviews query cache", async () => {
    const saved = {
      run_id: "run", instrument_id: "instr", label: "worth_reviewing", note: "保留輸入",
      revision: 2, updated_at: "2026-09-06T00:00:00Z",
    };
    const view = await filledPanel(vi.fn().mockResolvedValue(saved));
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    await screen.findByText(/已保存（revision 2）/);
    expect(view.queryClient.getQueryData(["reviews", "run"])).toEqual({
      items: [saved], next_cursor: null,
    });
  });

  it("adopts a newer server revision even when label and note are unchanged", async () => {
    const review = {
      run_id: "run", instrument_id: "instr", label: "borderline", note: "same",
      revision: 1, updated_at: "2026-09-06T00:00:00Z",
    };
    const view = harness(vi.fn(), { items: [review], next_cursor: null });
    await screen.findByText(/目前 revision 1/);
    view.queryClient.setQueryData(["reviews", "run"], {
      items: [{ ...review, revision: 2, updated_at: "2026-09-06T00:01:00Z" }], next_cursor: null,
    });
    await screen.findByText(/目前 revision 2/);
  });

  it("does not let an old save response mark a newer draft clean", async () => {
    let resolvePut: ((value: unknown) => void) | undefined;
    const view = await filledPanel(vi.fn(() => new Promise((resolve) => { resolvePut = resolve; })));
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    fireEvent.change(screen.getByLabelText(/備註/), { target: { value: "新草稿" } });
    resolvePut?.({
      run_id: "run", instrument_id: "instr", label: "worth_reviewing", note: "保留輸入",
      revision: 1, updated_at: "2026-09-06T00:00:00Z",
    });
    await waitFor(() => expect((screen.getByLabelText(/備註/) as HTMLTextAreaElement).value).toBe("新草稿"));
    view.queryClient.setQueryData(["reviews", "run"], {
      items: [{ run_id: "run", instrument_id: "instr", label: "worth_reviewing", note: "保留輸入", revision: 1, updated_at: "2026-09-06T00:00:00Z" }],
      next_cursor: null,
    });
    await waitFor(() => expect((screen.getByLabelText(/備註/) as HTMLTextAreaElement).value).toBe("新草稿"));
  });

  it("reloads a 409 baseline, then requires explicitly reapplying the draft", async () => {
    const latest = { run_id: "run", instrument_id: "instr", label: "not_useful", note: "server", revision: 2, updated_at: "2026-09-06T00:02:00Z" };
    const putReview = vi.fn()
      .mockRejectedValueOnce(new ApiError(409, { code: "REVIEW_REVISION_CONFLICT", message: "conflict", details: {}, request_id: "r" }))
      .mockResolvedValueOnce({ ...latest, label: "worth_reviewing", note: "保留輸入", revision: 3 });
    const view = harness(putReview, { items: [{ ...latest, revision: 1, label: "borderline", note: "old" }], next_cursor: null });
    await screen.findByText(/目前 revision 1/);
    fireEvent.change(screen.getByLabelText(/備註/), { target: { value: "保留輸入" } });
    fireEvent.click(screen.getByText("值得睇"));
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    await screen.findByText(/revision 衝突/);
    view.api.reviews = vi.fn().mockResolvedValue({ items: [latest], next_cursor: null });
    fireEvent.click(screen.getByRole("button", { name: "重新載入最新標記" }));
    await screen.findByDisplayValue("server");
    fireEvent.click(screen.getByRole("button", { name: "套用未保存草稿" }));
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    await screen.findByText(/已保存（revision 3）/);
    expect(putReview).toHaveBeenLastCalledWith("run", "instr", { label: "worth_reviewing", note: "保留輸入", expected_revision: 2 });
  });

  it("isolates draft state when the run and instrument identity changes", async () => {
    const api = { reviews: vi.fn().mockResolvedValue({ items: [], next_cursor: null }), putReview: vi.fn() } as unknown as ApiClient;
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(
      <QueryClientProvider client={queryClient}>
        <ReviewPanel client={api} runId="run-a" instrumentId="instr-a" />
      </QueryClientProvider>,
    );
    await screen.findByRole("radiogroup");
    fireEvent.change(screen.getByLabelText(/備註/), { target: { value: "A draft" } });
    view.rerender(
      <QueryClientProvider client={queryClient}>
        <ReviewPanel client={api} runId="run-b" instrumentId="instr-b" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect((screen.getByLabelText(/備註/) as HTMLTextAreaElement).value).toBe(""));
  });

  it("does not apply a delayed save response to a newly selected identity", async () => {
    let resolvePut: ((value: unknown) => void) | undefined;
    const putReview = vi.fn(() => new Promise((resolve) => { resolvePut = resolve; }));
    const view = await filledPanel(putReview);
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    view.rerender(
      <QueryClientProvider client={view.queryClient}>
        <ReviewPanel client={view.api} runId="run-b" instrumentId="instr-b" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect((screen.getByLabelText(/備註/) as HTMLTextAreaElement).value).toBe(""));
    resolvePut?.({ run_id: "run-a", instrument_id: "instr-a", label: "worth_reviewing", note: "A", revision: 1, updated_at: "2026-09-06T00:00:00Z" });
    await waitFor(() => expect(screen.queryByText(/已保存/)).toBeNull());
    expect(screen.queryByText(/舊草稿/)).toBeNull();
  });

  it("preserves edits made while a conflicting PUT is still in flight", async () => {
    let rejectPut: ((reason: unknown) => void) | undefined;
    const putReview = vi.fn()
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPut = reject; }))
      .mockResolvedValueOnce({ run_id: "run", instrument_id: "instr", label: "worth_reviewing", note: "newer draft", revision: 3, updated_at: "2026-09-06T00:03:00Z" });
    const view = await filledPanel(putReview);
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    fireEvent.change(screen.getByLabelText(/備註/), { target: { value: "newer draft" } });
    rejectPut?.(new ApiError(409, { code: "REVIEW_REVISION_CONFLICT", message: "conflict", details: {}, request_id: "r" }));
    await screen.findByText(/revision 衝突/);
    view.api.reviews = vi.fn().mockResolvedValue({ items: [{ run_id: "run", instrument_id: "instr", label: "not_useful", note: "server", revision: 2, updated_at: "2026-09-06T00:02:00Z" }], next_cursor: null });
    fireEvent.click(screen.getByRole("button", { name: "重新載入最新標記" }));
    await screen.findByDisplayValue("server");
    fireEvent.click(screen.getByRole("button", { name: "套用未保存草稿" }));
    fireEvent.click(screen.getByRole("button", { name: "保存標記" }));
    await screen.findByText(/已保存（revision 3）/);
    expect(putReview).toHaveBeenLastCalledWith("run", "instr", { label: "worth_reviewing", note: "newer draft", expected_revision: 2 });
  });
});
