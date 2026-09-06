import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiClient, ApiError, ScanAccepted, ScanStatus } from "../api/client";
import { claimRecovery, clearPending, loadPending, savePending } from "../state/pending";

const TERMINAL = new Set(["SUCCEEDED", "PARTIAL", "FAILED"]);

type Props = {
  client: ApiClient;
  onOpenRun: (runId: string) => void;
};

function stateLabel(state: string): string {
  return { QUEUED: "排隊中", RUNNING: "掃描中", SUCCEEDED: "完成", PARTIAL: "部分完成", FAILED: "失敗" }[state] ?? state;
}

export function ListsScan({ client, onOpenRun }: Props) {
  const queryClient = useQueryClient();
  const session = useQuery({ queryKey: ["session"], queryFn: (c) => client.currentSession(c.signal) });
  const lists = useQuery({ queryKey: ["watchlists"], queryFn: (c) => client.watchlists(c.signal) });

  const [watchlistId, setWatchlistId] = useState("");
  const resolvedWatchlistId = watchlistId || lists.data?.[0]?.id || "";
  const [mode, setMode] = useState<"auto" | "cache_only" | "force">("auto");
  const [activeId, setActiveId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recoveredOnce = useRef(false);

  // Reload recovery: a saved pending intent resumes FIRST, with its original
  // key/body/scan id — a new intent is never generated on mount.
  useEffect(() => {
    if (recoveredOnce.current) return;
    recoveredOnce.current = true;
    const pending = loadPending();
    if (!pending) return;
    if (pending.scanId !== null) {
      setActiveId(pending.scanId); // known outcome: pure GET polling resumes
      return;
    }
    // Unknown outcome: resubmit the SAME key+body exactly once (server-side
    // idempotency makes this a replay, never a second scan). StrictMode's
    // double-invoke is guarded by claimRecovery.
    if (!claimRecovery(pending.key)) return;
    client
      .submitScan(pending.body, pending.key)
      .then((document) => {
        savePending({ ...pending, scanId: document.id });
        setActiveId(document.id);
      })
      .catch((cause: unknown) => {
        setError(cause instanceof ApiError ? `${cause.code}: ${cause.message}` : String(cause));
      })
      .finally(() => {
        // Released only after the outcome is known; the intent stays saved.
        clearPending();
        setActiveId((current) => current);
      });
  }, [client]);

  const status = useQuery({
    queryKey: ["scan", activeId],
    enabled: activeId !== null,
    queryFn: (context) => client.scanStatus(context.queryKey[1] as string, context.signal),
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state !== undefined && TERMINAL.has(state) ? false : 2_000;
    },
  });

  useEffect(() => {
    const document = status.data;
    if (document === undefined || !TERMINAL.has(document.state)) return;
    const pending = loadPending();
    if (pending?.scanId === document.id) clearPending();
  }, [status.data]);

  const submit = async (event: FormEvent) => {
    event.preventDefault(); // never an automatic POST: user action only
    if (!resolvedWatchlistId || submitting) return;
    setSubmitting(true);
    setError(null);
    // A NEW intentional scan uses a NEW idempotency key, saved (non-secret)
    // before the request so a lost response replays instead of duplicating.
    const key = crypto.randomUUID();
    const body = { watchlist_id: resolvedWatchlistId, as_of_session: null, data_mode: mode } as const;
    savePending({ key, body, scanId: null });
    try {
      const document: ScanStatus | ScanAccepted = await client.submitScan(body, key);
      savePending({ key, body, scanId: document.id });
      setActiveId(document.id);
      queryClient.invalidateQueries({ queryKey: ["scans"] });
    } catch (cause) {
      if (!(cause instanceof ApiError && cause.status === 401)) clearPending();
      setError(
        cause instanceof ApiError
          ? cause.status === 429
            ? "排隊額已滿（429），稍後再試；意圖已保存，reload 後會恢復。"
            : `${cause.code}: ${cause.message}`
          : String(cause),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const document = status.data;

  return (
    <>
      <section className="panel" aria-label="名單">
        <h2>名單</h2>
        <div className="muted">最新已完成市況時段：{session.data?.as_of_session ?? "載入中…"}</div>
        {lists.isLoading && <p className="muted">載入中…</p>}
        {lists.data !== undefined && lists.data.length === 0 && (
          <p className="muted">仲未有名單；喺下面建立第一個。</p>
        )}
        {lists.data !== undefined && lists.data.length > 0 && (
          <div className="table-wrap">
            <table className="list">
              <thead>
                <tr>
                  <th>名稱</th>
                  <th>Symbols</th>
                  <th>Revision</th>
                </tr>
              </thead>
              <tbody>
                {lists.data.map((list) => (
                  <tr key={list.id}>
                    <td>{list.name}</td>
                    <td title={list.symbols.join(", ")}>{list.symbols.length} 隻</td>
                    <td>v{list.revision}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <WatchlistForms client={client} lists={lists.data ?? []} />
      </section>

      <section className="panel" aria-label="提交掃描">
        <h2>提交掃描</h2>
        <form onSubmit={submit} className="row">
          <label className="field">
            <span>名單</span>
            <select
              aria-label="掃描名單"
              value={resolvedWatchlistId}
              onChange={(event) => setWatchlistId(event.target.value)}
            >
              {(lists.data ?? []).map((list) => (
                <option key={list.id} value={list.id}>
                  {list.name}（{list.symbols.length} 隻）
                </option>
              ))}
              {resolvedWatchlistId === "" && <option value="">（無名單）</option>}
            </select>
          </label>
          <label className="field">
            Data mode
            <select value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}>
              <option value="auto">auto（預設：有需要先更新）</option>
              <option value="cache_only">cache_only（完全離線）</option>
              <option value="force">force（強制重新下載）</option>
            </select>
          </label>
          <button className="primary" type="submit" disabled={!watchlistId || submitting}>
            {submitting ? "提交中…" : "開始掃描"}
          </button>
        </form>
        {error !== null && <p className="error-text" role="alert">{error}</p>}
        {document !== undefined && (
          <div aria-live="polite">
            <h3 style={{ margin: "10px 0 4px", fontSize: 13.5 }}>
              Scan <code>{document.id}</code> —{" "}
              <span className={`state-${document.state}`}>{stateLabel(document.state)}</span>
            </h3>
            {document.state === "RUNNING" && document.progress != null && (
              <>
                <div className="progressbar" role="progressbar" aria-valuemin={0} aria-valuemax={document.progress.total_symbols} aria-valuenow={document.progress.processed_symbols}>
                  <div style={{ width: `${Math.round((100 * document.progress.processed_symbols) / document.progress.total_symbols)}%` }} />
                </div>
                <div className="muted">
                  {document.progress.phase}：{document.progress.processed_symbols}/{document.progress.total_symbols}
                </div>
              </>
            )}
            {TERMINAL.has(document.state) && (
              <p className="row">
                <span className="muted">
                  Counts：evaluated {document.counts?.evaluated ?? 0}／excluded{" "}
                  {document.counts?.excluded ?? 0}／data-error {document.counts?.data_error ?? 0}／
                  candidates {document.counts?.candidate ?? 0}
                </span>
                {(document.state === "SUCCEEDED" || document.state === "PARTIAL") && (
                  <button onClick={() => onOpenRun(document.id)}>查看候選</button>
                )}
              </p>
            )}
          </div>
        )}
      </section>
    </>
  );
}

function WatchlistForms({ client, lists }: { client: ApiClient; lists: { id: string; name: string; revision: number }[] }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [symbols, setSymbols] = useState("");
  const [replaceId, setReplaceId] = useState("");
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const parse = (raw: string) =>
    raw
      .split(/[\s,;]+/)
      .map((symbol) => symbol.trim().toUpperCase())
      .filter((symbol) => symbol.length > 0 && !symbol.startsWith("#"));

  const create = useMutation({
    mutationFn: () => client.createWatchlist(name.trim(), parse(symbols)),
    onSuccess: (list) => {
      setMessage({ kind: "ok", text: `已建立 ${list.name}（${list.symbols.length} 隻）` });
      setName("");
      setSymbols("");
      queryClient.invalidateQueries({ queryKey: ["watchlists"] });
    },
    onError: (cause) => setMessage({ kind: "error", text: describe(cause) }),
  });

  const replace = useMutation({
    mutationFn: () => {
      const list = lists.find((candidate) => candidate.id === replaceId);
      if (!list) throw new Error("揀一個名單");
      return client.replaceSymbols(list.id, parse(symbols), list.revision);
    },
    onSuccess: (list) => {
      setMessage({ kind: "ok", text: `已更新 ${list.name} → revision ${list.revision}` });
      setSymbols("");
      queryClient.invalidateQueries({ queryKey: ["watchlists"] });
    },
    onError: (cause) => {
      setMessage({
        kind: "error",
        text:
          cause instanceof ApiError && cause.status === 409
            ? "Revision 衝突：名單已俾人改過，請重載後再試。"
            : describe(cause),
      });
      queryClient.invalidateQueries({ queryKey: ["watchlists"] });
    },
  });

  return (
    <div style={{ marginTop: 10 }}>
      <div className="row">
        <label className="field">
          {replaceId === "" ? "新名單名稱" : "貼上 symbols（每行一個）"}
          {replaceId === "" ? (
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="us-growth" />
          ) : null}
        </label>
        <label className="field" style={{ flex: 1, minWidth: 220 }}>
          Symbols（每行一個）
          <textarea value={symbols} onChange={(event) => setSymbols(event.target.value)} placeholder={"AAPL\nMSFT"} />
        </label>
      </div>
      <div className="row">
        <button className="primary" disabled={name.trim() === "" || parse(symbols).length === 0 || create.isPending} onClick={() => create.mutate()}>
          建立名單
        </button>
        <select value={replaceId} onChange={(event) => setReplaceId(event.target.value)} aria-label="選擇要取代的名單">
          <option value="">取代現有名單…</option>
          {lists.map((list) => (
            <option key={list.id} value={list.id}>{list.name}（v{list.revision}）</option>
          ))}
        </select>
        <button disabled={replaceId === "" || parse(symbols).length === 0 || replace.isPending} onClick={() => replace.mutate()}>
          取代 symbols
        </button>
      </div>
      {message !== null && (
        <p className={message.kind === "ok" ? "ok-text" : "error-text"} role={message.kind === "error" ? "alert" : undefined}>
          {message.text}
        </p>
      )}
    </div>
  );
}

export function describe(cause: unknown): string {
  return cause instanceof ApiError ? `${cause.code}: ${cause.message}` : String(cause);
}
