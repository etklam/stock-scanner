import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "../api/client";

// First screen: paste the local API token (qscan init → api-token.json).
// The token is kept in memory only and is never auto-submitted on mount.

export function Connect({ onConnect }: { onConnect: (token: string) => Promise<void> }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault(); // never an automatic POST: user action only
    if (!token.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onConnect(token.trim());
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.status === 401
          ? "Token 無效或已輪換，請重新輸入。"
          : "連唔到本地 API；請確認 qscan serve 正在運行。",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="panel" onSubmit={submit} aria-label="連線本地 API">
      <h2>連線本地 API</h2>
      <p className="muted">
        先執行 <code>qscan init</code>（建立 token）同 <code>qscan serve</code>，
        再貼上 <code>api-token.json</code> 入面嘅 token。Token 只會存在於記憶體，
        唔會寫入瀏覽器儲存或 URL。
      </p>
      <div className="row">
        <label className="field">
          Local API token
          <input
            type="password"
            autoComplete="off"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="貼上 token"
            size={44}
          />
        </label>
        <button className="primary" type="submit" disabled={!token.trim() || busy}>
          {busy ? "連線中…" : "連線"}
        </button>
      </div>
      {error !== null && <p className="error-text" role="alert">{error}</p>}
    </form>
  );
}
