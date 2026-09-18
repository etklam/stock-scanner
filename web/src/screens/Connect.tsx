import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "../api/client";

// Ordinary flow retries the automatic browser session. Bearer entry is an
// advanced fallback and remains memory-only.

export function Connect({
  onConnect,
  onRetry,
}: {
  onConnect: (token: string) => Promise<void>;
  onRetry: () => Promise<void>;
}) {
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
    <section className="panel" aria-label="連線本地 API">
      <h2>連線本地 API</h2>
      <p className="muted">
        無法建立本地瀏覽器 session。請確認 <code>qscan start</code> 正在運行後重試。
      </p>
      <button className="primary" type="button" onClick={() => void onRetry()}>
        重新連線
      </button>
      <details>
        <summary>進階：使用 Bearer token</summary>
        <form onSubmit={submit}>
          <p className="muted">Token 只存在記憶體，不會寫入瀏覽器儲存或 URL。</p>
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
            <button type="submit" disabled={!token.trim() || busy}>
              {busy ? "連線中…" : "Bearer 連線"}
            </button>
          </div>
          {error !== null && <p className="error-text" role="alert">{error}</p>}
        </form>
      </details>
    </section>
  );
}
