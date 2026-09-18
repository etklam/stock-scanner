import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./client";
import type { AutomationStatus } from "./client";

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
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function client(): { api: ApiClient; unauthorized: ReturnType<typeof vi.fn> } {
  const unauthorized = vi.fn();
  const api = new ApiClient("", () => "token-abc", unauthorized);
  return { api, unauthorized };
}

describe("ApiClient", () => {
  it("bootstraps a browser session and uses its in-memory CSRF token", async () => {
    const calls = stubFetch([
      { status: 200, body: { csrf_token: "csrf-memory-only" } },
      { status: 201, body: { id: "wl", name: "browser", revision: 1, symbols: ["AAPL"] } },
    ]);
    const unauthorized = vi.fn();
    const api = new ApiClient("", () => null, unauthorized);

    await api.establishBrowserSession();
    await api.createWatchlist("browser", ["AAPL"]);

    expect(calls[0]!.url).toBe("/api/v1/auth/session");
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.init.credentials).toBe("same-origin");
    const headers = calls[1]!.init.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBe("csrf-memory-only");
    expect(headers.Authorization).toBeUndefined();
  });

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
    } as const;
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

  it("keeps all-results and stage filters semantically distinct", async () => {
    const calls = stubFetch([
      { status: 200, body: { items: [], next_cursor: null } },
      { status: 200, body: { items: [], next_cursor: null } },
    ]);
    const { api } = client();
    await api.resultsPage("run", "all", 50);
    await api.resultsPage("run", "stage:FORMING", 50);
    expect(new URL(calls[0]!.url, "http://localhost").search).toContain("limit=50");
    expect(new URL(calls[0]!.url, "http://localhost").search).not.toContain("candidate=");
    expect(new URL(calls[1]!.url, "http://localhost").search).toContain("stage=FORMING");
    expect(new URL(calls[1]!.url, "http://localhost").search).toContain("candidate=true");
  });

  it("uses the automation control endpoints without inventing a request body", async () => {
    const status = {
      enabled: true,
      latest_completed_session: null,
      job: null,
      run: null,
      next_due_session: "2026-09-18",
      next_due_time: null,
      universe: null,
      report: null,
      notification: null,
    } satisfies AutomationStatus;
    const calls = stubFetch([
      { status: 200, body: status },
      { status: 200, body: status },
      { status: 202, body: status },
    ]);
    const { api } = client();

    await api.enableAutomation();
    await api.pauseAutomation();
    await api.runAutomationNow();

    expect(calls.map(({ url, init }) => [url, init.method, init.body])).toEqual([
      ["/api/v1/automation/enable", "POST", undefined],
      ["/api/v1/automation/pause", "POST", undefined],
      ["/api/v1/automation/run-now", "POST", undefined],
    ]);
  });

  it("aborts a never-resolving submit at the application deadline", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string | URL, init: RequestInit = {}) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener(
            "abort",
            () => reject(init.signal?.reason ?? new DOMException("aborted", "AbortError")),
            { once: true },
          );
        }),
      ),
    );
    const { api } = client();
    const body = { watchlist_id: "wl", as_of_session: null, data_mode: "auto" } as const;
    const pending = api.submitScan(body, "deadline-key-000001");
    const assertion = expect(pending).rejects.toMatchObject({ name: "TimeoutError" });

    await vi.advanceTimersByTimeAsync(15_000);
    await assertion;
  });

  it("cancels a 429 backoff when the caller aborts", async () => {
    stubFetch([
      { status: 429, body: { error: { code: "QUEUE_LIMIT_REACHED", message: "x", request_id: "r" } } },
    ]);
    const { api } = client();
    const controller = new AbortController();
    const body = { watchlist_id: "wl", as_of_session: null, data_mode: "auto" } as const;
    const pending = api.submitScan(body, "cancel-key-000001", controller.signal);
    const assertion = expect(pending).rejects.toMatchObject({ name: "AbortError" });

    controller.abort();
    await assertion;
  });
});
