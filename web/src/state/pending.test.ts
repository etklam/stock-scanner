import { describe, expect, it, vi } from "vitest";
import {
  claimRecovery,
  clearPending,
  clearPendingIfCurrent,
  loadPending,
  pendingStorageIsPersistent,
  releaseRecovery,
  savePending,
} from "./pending";

describe("pending intent store", () => {
  it("keeps an in-memory intent when sessionStorage cannot write", () => {
    clearPending();
    vi.stubGlobal("sessionStorage", {
      getItem: () => null,
      setItem: () => {
        throw new Error("quota");
      },
      removeItem: () => undefined,
    } as unknown as Storage);

    try {
      expect(() =>
        savePending({
          key: "storage-fallback-key",
          body: { watchlist_id: "wl", as_of_session: null, data_mode: "auto" },
          scanId: null,
        }),
      ).not.toThrow();
      expect(loadPending()?.key).toBe("storage-fallback-key");
      expect(pendingStorageIsPersistent()).toBe(false);
    } finally {
      vi.unstubAllGlobals();
      clearPending();
    }
  });

  it("round-trips the non-secret key/body/scanId", () => {
    clearPending();
    savePending({
      key: "k1",
      body: { watchlist_id: "wl", as_of_session: null, data_mode: "auto" },
      scanId: null,
    });
    expect(loadPending()?.key).toBe("k1");
    savePending({ key: "k1", body: { watchlist_id: "wl", data_mode: "auto" }, scanId: "scan-9" });
    expect(loadPending()?.scanId).toBe("scan-9");
    clearPending();
    expect(loadPending()).toBeNull();
  });

  it("does not clear a newer intent when an older key finishes", () => {
    clearPending();
    savePending({
      key: "new-key",
      body: { watchlist_id: "wl", as_of_session: null, data_mode: "auto" },
      scanId: null,
    });

    expect(clearPendingIfCurrent("old-key")).toBe(false);
    expect(loadPending()?.key).toBe("new-key");
    clearPending();
  });

  it("survives storage read and remove failures", () => {
    clearPending();
    savePending({
      key: "storage-error-key",
      body: { watchlist_id: "wl", as_of_session: null, data_mode: "auto" },
      scanId: null,
    });
    vi.stubGlobal("sessionStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => undefined,
      removeItem: () => {
        throw new Error("denied");
      },
    } as unknown as Storage);

    try {
      expect(loadPending()?.key).toBe("storage-error-key");
      expect(() => clearPending()).not.toThrow();
      expect(loadPending()).toBeNull();
    } finally {
      vi.unstubAllGlobals();
      clearPending();
    }
  });

  it("treats a corrupt payload as no pending intent", () => {
    globalThis.sessionStorage?.setItem("qscan.pending-scan", "{not json");
    expect(loadPending()).toBeNull();
    clearPending();
  });

  it("treats a structurally invalid payload as no pending intent", () => {
    clearPending();
    vi.stubGlobal("sessionStorage", {
      getItem: () => JSON.stringify({ key: "bad", body: { watchlist_id: "wl", data_mode: "invalid" }, scanId: null }),
      setItem: () => undefined,
      removeItem: () => undefined,
    } as unknown as Storage);
    try {
      expect(loadPending()).toBeNull();
    } finally {
      vi.unstubAllGlobals();
      clearPending();
    }
  });

  it("claims recovery exactly once per key (StrictMode double-mount safe)", () => {
    expect(claimRecovery("key-x")).toBe(true);
    expect(claimRecovery("key-x")).toBe(false);
    releaseRecovery("key-x");
    expect(claimRecovery("key-x")).toBe(true);
    releaseRecovery("key-x");
  });
});
