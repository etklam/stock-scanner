// Pending scan intent: the non-secret idempotency key + request body saved the
// moment a submission's outcome is unknown, so a reload restores the SAME
// intent instead of generating a new one. Never contains the bearer token.

const STORAGE_KEY = "qscan.pending-scan";

export type PendingIntent = {
  key: string;
  body: {
    watchlist_id: string;
    as_of_session?: string | null;
    data_mode: "auto" | "cache_only" | "force";
  };
  scanId: string | null;
};

function storage(): Storage | null {
  // jsdom/vitest may lack sessionStorage; the UI degrades to in-memory.
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

let memory: PendingIntent | null = null;

export function savePending(intent: PendingIntent): void {
  memory = intent;
  storage()?.setItem(STORAGE_KEY, JSON.stringify(intent));
}

export function loadPending(): PendingIntent | null {
  const raw = storage()?.getItem(STORAGE_KEY);
  if (!raw) return memory;
  try {
    const parsed = JSON.parse(raw) as PendingIntent;
    if (typeof parsed?.key !== "string" || typeof parsed?.body !== "object") return null;
    memory = parsed;
    return parsed;
  } catch {
    return null;
  }
}

export function clearPending(): void {
  memory = null;
  storage()?.removeItem(STORAGE_KEY);
}

// StrictMode mounts effects twice; an in-flight recovery must not double-POST.
const inFlight = new Set<string>();

export function claimRecovery(key: string): boolean {
  if (inFlight.has(key)) return false;
  inFlight.add(key);
  return true;
}

export function releaseRecovery(key: string): void {
  inFlight.delete(key);
}
