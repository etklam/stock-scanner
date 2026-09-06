import { describe, expect, it } from "vitest";
import { claimRecovery, clearPending, loadPending, releaseRecovery, savePending } from "./pending";

describe("pending intent store", () => {
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

  it("treats a corrupt payload as no pending intent", () => {
    globalThis.sessionStorage?.setItem("qscan.pending-scan", "{not json");
    expect(loadPending()).toBeNull();
    clearPending();
  });

  it("claims recovery exactly once per key (StrictMode double-mount safe)", () => {
    expect(claimRecovery("key-x")).toBe(true);
    expect(claimRecovery("key-x")).toBe(false);
    releaseRecovery("key-x");
    expect(claimRecovery("key-x")).toBe(true);
    releaseRecovery("key-x");
  });
});
