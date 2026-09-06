// Typed API client: one place for the bearer token (memory), ErrorEnvelope,
// 401 rotation, 409 conflicts, 429 bounded backoff and request timeouts.
// Response shapes are checked against the generated OpenAPI types (schema.d.ts).

import type { components } from "./schema";

type ErrorBody = components["schemas"]["ErrorBody"];

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;

  constructor(status: number, body: ErrorBody | undefined) {
    super(body?.message ?? `HTTP ${status}`);
    this.status = status;
    this.code = body?.code ?? "UNKNOWN";
    this.requestId = body?.request_id ?? "";
  }

  get isConflict(): boolean {
    return this.status === 409;
  }
}

export type ScanBody = {
  watchlist_id: string;
  ruleset_id?: string;
  as_of_session?: string | null;
  data_mode: "auto" | "cache_only" | "force";
};

export type Watchlist = {
  id: string;
  name: string;
  revision: number;
  symbols: string[];
};

export type ScanStatus = components["schemas"]["ScanStatusOut"];

export type ResultRow = components["schemas"]["ResultOut"];

export type Series = components["schemas"]["SeriesOut"];

export type Review = components["schemas"]["SavedReviewOut"];

export type Page<T> = { items: T[]; next_cursor: string | null };

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export class ApiClient {
  constructor(
    private readonly base: string,
    private getToken: () => string | null,
    private onUnauthorized: () => void,
  ) {}

  private async request<T>(
    method: string,
    path: string,
    init?: { body?: unknown; idempotencyKey?: string; signal?: AbortSignal },
  ): Promise<T> {
    const token = this.getToken();
    if (token === null && path.startsWith("/api/")) {
      // Never fire authenticated business requests without a token.
      throw new ApiError(401, undefined);
    }
    const headers: Record<string, string> = { Accept: "application/json" };
    if (token !== null) headers.Authorization = `Bearer ${token}`;
    if (init?.body !== undefined) headers["Content-Type"] = "application/json";
    if (init?.idempotencyKey) headers["Idempotency-Key"] = init.idempotencyKey;
    const response = await fetch(this.base + path, {
      method,
      headers,
      body: init?.body === undefined ? undefined : JSON.stringify(init.body),
      signal: init?.signal,
    });
    if (response.status === 401) {
      // Token rotated or wrong: force reconnect; the caller clears caches.
      this.onUnauthorized();
      throw new ApiError(401, undefined);
    }
    if (!response.ok) {
      let body: ErrorBody | undefined;
      try {
        const parsed = (await response.json()) as { error?: ErrorBody };
        body = parsed?.error;
      } catch {
        // Non-JSON error body: fall through with the status code only.
      }
      throw new ApiError(response.status, body);
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  /** POST /scans with a caller-owned idempotency key. Retries on 429 reuse
   * the SAME key and body (bounded backoff, never infinite). */
  async submitScan(body: ScanBody, idempotencyKey: string): Promise<ScanStatus | ScanAccepted> {
    let delay = 500;
    for (let attempt = 1; ; attempt += 1) {
      try {
        return await this.request<ScanStatus | ScanAccepted>("POST", "/api/v1/scans", {
          body,
          idempotencyKey,
        });
      } catch (error) {
        if (error instanceof ApiError && error.status === 429 && attempt <= 3) {
          await sleep(delay);
          delay *= 2;
          continue;
        }
        throw error;
      }
    }
  }

  static isAccepted(value: ScanStatus | ScanAccepted): value is ScanAccepted {
    return (value as ScanAccepted).state === "QUEUED" && "watchlist_revision" in value;
  }

  scanStatus(id: string, signal?: AbortSignal) {
    return this.request<ScanStatus>("GET", `/api/v1/scans/${id}`, { signal });
  }

  watchlists(signal?: AbortSignal) {
    return this.request<Watchlist[]>("GET", "/api/v1/watchlists", { signal });
  }

  createWatchlist(name: string, symbols: string[]) {
    return this.request<Watchlist>("POST", "/api/v1/watchlists", { body: { name, symbols } });
  }

  renameWatchlist(id: string, name: string, expectedRevision: number) {
    return this.request<Watchlist>("PATCH", `/api/v1/watchlists/${id}`, {
      body: { name, expected_revision: expectedRevision },
    });
  }

  replaceSymbols(id: string, symbols: string[], expectedRevision: number) {
    return this.request<Watchlist>("PUT", `/api/v1/watchlists/${id}/symbols`, {
      body: { symbols, expected_revision: expectedRevision },
    });
  }

  currentSession(signal?: AbortSignal) {
    return this.request<{ as_of_session: string; reference_session: string }>(
      "GET",
      "/api/v1/sessions/current",
      { signal },
    );
  }

  scansPage(limit: number, cursor?: string | null, signal?: AbortSignal) {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) query.set("cursor", cursor);
    return this.request<Page<ScanStatus>>("GET", `/api/v1/scans?${query}`, { signal });
  }

  resultsPage(
    scanId: string,
    filter: string,
    limit: number,
    cursor?: string | null,
    signal?: AbortSignal,
  ) {
    const query = new URLSearchParams({ limit: String(limit) });
    if (filter === "candidates") {
      query.set("candidate", "true");
    } else if (filter.startsWith("stage:")) {
      query.set("stage", filter.slice("stage:".length));
      query.set("candidate", "true");
    }
    // filter === "all": no candidate param — evaluated + excluded + data errors.
    if (cursor) query.set("cursor", cursor);
    return this.request<Page<ResultRow>>(
      "GET",
      `/api/v1/scans/${scanId}/results?${query}`,
      { signal },
    );
  }

  resultDetail(scanId: string, instrumentId: string, signal?: AbortSignal) {
    return this.request<ResultRow>(
      "GET",
      `/api/v1/scans/${scanId}/results/${instrumentId}`,
      { signal },
    );
  }

  series(scanId: string, instrumentId: string, limit: number, signal?: AbortSignal) {
    return this.request<Series>(
      "GET",
      `/api/v1/scans/${scanId}/series/${instrumentId}?limit=${limit}`,
      { signal },
    );
  }

  reviews(scanId: string, signal?: AbortSignal) {
    return this.request<Page<Review>>("GET", `/api/v1/scans/${scanId}/reviews`, { signal });
  }

  putReview(
    scanId: string,
    instrumentId: string,
    body: { label: string; note: string; expected_revision?: number },
  ) {
    return this.request<Review>("PUT", `/api/v1/scans/${scanId}/reviews/${instrumentId}`, {
      body,
    });
  }
}

export type ScanAccepted = {
  id: string;
  state: "QUEUED";
  as_of_session: string;
  watchlist_revision: number;
  ruleset_version: string;
};
