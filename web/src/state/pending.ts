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
let storageDirty = false;

export function savePending(intent: PendingIntent): void {
  memory = intent;
  try {
    const target = storage();
    if (target === null) {
      storageDirty = true;
      return;
    }
    target.setItem(STORAGE_KEY, JSON.stringify(intent));
    storageDirty = false;
  } catch {
    // A denied or full sessionStorage must not strand the in-memory request.
    storageDirty = true;
  }
}

export function loadPending(): PendingIntent | null {
  if (storageDirty) return memory;
  const target = storage();
  if (target === null) {
    storageDirty = true;
    return memory;
  }
  let raw: string | null;
  try {
    raw = target.getItem(STORAGE_KEY);
  } catch {
    storageDirty = true;
    return memory;
  }
  if (!raw) return memory;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isPendingIntent(parsed)) return memory;
    memory = parsed;
    return parsed;
  } catch {
    return memory;
  }
}

export function pendingStorageIsPersistent(): boolean {
  return !storageDirty && storage() !== null;
}

export function clearPending(): void {
  memory = null;
  try {
    const target = storage();
    if (target === null) {
      storageDirty = true;
      return;
    }
    target.removeItem(STORAGE_KEY);
    storageDirty = false;
  } catch {
    // Clearing memory is still safe when browser storage is unavailable.
    storageDirty = true;
  }
}

export function savePendingIfCurrent(key: string, intent: PendingIntent): boolean {
  if (loadPending()?.key !== key) return false;
  savePending(intent);
  return true;
}

export function clearPendingIfCurrent(key: string, scanId?: string): boolean {
  const current = loadPending();
  if (current?.key !== key || (scanId !== undefined && current.scanId !== scanId)) return false;
  clearPending();
  return true;
}

function isPendingIntent(value: unknown): value is PendingIntent {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  const body = candidate.body;
  if (typeof candidate.key !== "string" || candidate.key.length === 0) return false;
  if (candidate.scanId !== null && (typeof candidate.scanId !== "string" || candidate.scanId.length === 0)) {
    return false;
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) return false;
  const request = body as Record<string, unknown>;
  if (typeof request.watchlist_id !== "string" || request.watchlist_id.length === 0) return false;
  if (!new Set(["auto", "cache_only", "force"]).has(request.data_mode as string)) return false;
  return request.as_of_session === undefined || request.as_of_session === null || typeof request.as_of_session === "string";
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
