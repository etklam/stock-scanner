import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./client";

// Minimal fetch stub returning one queued response per call.
function stubFetch(responses: Array<{ status: number; body?: unknown }>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchMock = vi.fn(async (url: string | URL, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    const next = responses.shift();
    if (next === undefined) throw new Error("no scripted response");
    return new Response(next.body === undefined ? null : JSON.stringify(next.body), {
      status: next.status,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function client(): { api: ApiClient; unauthorized: ReturnType<typeof vi.fn> } {
  const unauthorized = vi.fn();
  const api = new ApiClient("", () => "token-abc", unauthorized);
  return { api, unauthorized };
}

describe("ApiClient", () => {
  it("unwraps ErrorEnvelope errors with code and status", async () => {
    stubFetch([
      {
        status: 409,
        body: {
          error: { code: "REVIEW_REVISION_CONFLICT", message: "conflict", request_id: "r1" },
        },
      },
    ]);
    const { api } = client();
    const error = await api
      .putReview("run", "instr", { label: "borderline", note: "" })
      .then(
        () => null,
        (cause: unknown) => cause,
      );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).code).toBe("REVIEW_REVISION_CONFLICT");
  });

  it("fires onUnauthorized and never retries on 401", async () => {
    const calls = stubFetch([{ status: 401, body: {} }, { status: 401, body: {} }]);
    const { api, unauthorized } = client();
    await expect(api.scanStatus("run-1")).rejects.toBeInstanceOf(ApiError);
    expect(unauthorized).toHaveBeenCalledTimes(1);
    expect(calls).toHaveLength(1);
  });

  it("retries 429 with the SAME idempotency key, bounded at 3 retries", async () => {
    const accepted = {
      id: "00000000-0000-0000-0000-000000000001",
      state: "QUEUED",
      as_of_session: "2026-09-04",
      watchlist_revision: 1,
      ruleset_version: "1.0.0",
    };
    const calls = stubFetch([
      { status: 429, body: { error: { code: "QUEUE_LIMIT_REACHED", message: "x", request_id: "r" } } },
      { status: 429, body: { error: { code: "QUEUE_LIMIT_REACHED", message: "x", request_id: "r" } } },
      { status: 202, body: accepted },
    ]);
    const { api } = client();
    const body = { watchlist_id: "wl", as_of_session: null, data_mode: "auto" } as const;
    const result = await api.submitScan(body, "my-key-00000001");
    expect(ApiClient.isAccepted(result)).toBe(true);
    expect(calls).toHaveLength(3);
    for (const call of calls) {
      expect((call.init.headers as Record<string, string>)["Idempotency-Key"]).toBe("my-key-00000001");
    }
  });

  it("gives up after bounded 429 retries instead of looping forever", async () => {
    stubFetch(
      Array.from({ length: 5 }, () => ({
        status: 429,
        body: { error: { code: "QUEUE_LIMIT_REACHED", message: "x", request_id: "r" } },
      })),
    );
    const { api } = client();
    const body = { watchlist_id: "wl", as_of_session: null, data_mode: "auto" } as const;
    await expect(api.submitScan(body, "my-key-00000002")).rejects.toMatchObject({ status: 429 });
  });

  it("refuses business requests without a token before any fetch", async () => {
    const calls = stubFetch([{ status: 200, body: [] }]);
    const unauthorized = vi.fn();
    const api = new ApiClient("", () => null, unauthorized);
    await expect(api.watchlists()).rejects.toMatchObject({ status: 401 });
    expect(calls).toHaveLength(0);
  });
});
