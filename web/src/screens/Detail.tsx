import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiClient, ApiError, type Page, type ResultRow, type Review } from "../api/client";
import { CATEGORY_LABELS, REVIEW_LABELS, diagnosticLabel, reasonLabel, stageLabel } from "../state/labels";
import { Chart } from "./Chart";

const TERMINAL = new Set(["SUCCEEDED", "PARTIAL", "FAILED"]);
const STAGES = ["", "FORMING", "NEAR_CLOSE_RESISTANCE", "CLOSE_BREAK_ABOVE", "EXTENDED"];

type Props = { client: ApiClient; runId: string };

export function RunDetail({ client, runId }: Props) {
  const status = useQuery({
    queryKey: ["scan", runId],
    queryFn: (context) => client.scanStatus(runId, context.signal),
    refetchInterval: (query) => (TERMINAL.has(query.state.data?.state ?? "") ? false : 2_000),
  });
  const [filter, setFilter] = useState("candidates");
  const [stage, setStage] = useState("");
  const effectiveFilter = stage === "" ? filter : `stage:${stage}`;
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => setSelected(null), [runId]);

  const document = status.data;
  if (status.isLoading) return <p className="muted">載入 run 資料…</p>;
  if (document === undefined) return <p className="error-text">{String(status.error)}</p>;

  return (
    <>
      <section className="panel">
        <div className="row">
          <h2 style={{ margin: 0 }}>
            Run <code>{document.id}</code> —{" "}
            <span className={`state-${document.state}`}>{document.state}</span>
          </h2>
          <span className="muted">
            {document.as_of_session}（reference {document.reference_session}）・名單 revision v
            {document.watchlist_revision}・{document.data_mode}
          </span>
        </div>
        {document.counts != null && (
          <p className="muted">
            evaluated {document.counts.evaluated}／excluded {document.counts.excluded}／data-error{" "}
            {document.counts.data_error}／candidates {document.counts.candidate}
            {document.warnings.length > 0 && ` ⚠ ${document.warnings.join("；")}`}
          </p>
        )}
        {document.state === "PARTIAL" && (
          <p className="error-text">部分標的因資料錯誤未能評估（PARTIAL）——資料失敗唔係普通非候選。</p>
        )}
        {document.state === "FAILED" && (
          <p className="error-text">掃描失敗（{document.error ?? "未知原因"}）；無結果可睇。狀態診斷見上方。</p>
        )}
      </section>
      {TERMINAL.has(document.state) && document.state !== "FAILED" && (
        <ResultsTable
          client={client}
          runId={runId}
          filter={effectiveFilter}
          onFilter={setFilter}
          stage={stage}
          onStage={setStage}
          selected={selected}
          onSelect={setSelected}
          zeroCandidates={document.counts?.candidate === 0 && document.state === "SUCCEEDED"}
        />
      )}
    </>
  );
}

type TableProps = {
  client: ApiClient;
  runId: string;
  filter: string;
  onFilter: (value: string) => void;
  stage: string;
  onStage: (value: string) => void;
  selected: string | null;
  onSelect: (instrumentId: string) => void;
  zeroCandidates: boolean;
};

function ResultsTable(props: TableProps) {
  const { client, runId, filter, onFilter, stage, onStage, selected, onSelect, zeroCandidates } = props;
  // Query key includes run + filters: changing either resets the cursor (a
  // cursor from another run/filter is invalid by contract anyway).
  const page = useInfiniteQuery({
    queryKey: ["results", runId, filter],
    queryFn: ({ pageParam, signal }) =>
      client.resultsPage(runId, filter, 50, pageParam as string | undefined, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (value) => value.next_cursor,
  });
  const rows = page.data?.pages.flatMap((value) => value.items) ?? [];

  if (page.isLoading) return <p className="muted">載入結果…</p>;
  if (page.isError) {
    return (
      <p className="error-text">
        載入結果失敗：
        {page.error instanceof ApiError
          ? page.error.status === 409
            ? "掃描未有可公開嘅結果。"
            : `${page.error.code}: ${page.error.message}`
          : String(page.error)}
      </p>
    );
  }

  return (
    <section className="panel">
      <div className="row">
        <label className="field">
          顯示
          <select value={filter.startsWith("stage:") ? "all" : filter} onChange={(event) => onFilter(event.target.value)}>
            <option value="candidates">候選（預設）</option>
            <option value="all">全部結果（包括資料錯誤）</option>
          </select>
        </label>
        <label className="field">
          Stage
          <select value={stage} onChange={(event) => onStage(event.target.value)}>
            {STAGES.map((value) => (
              <option key={value || "all"} value={value}>{value === "" ? "全部 stage" : stageLabel(value)}</option>
            ))}
          </select>
        </label>
        {zeroCandidates && <span className="muted">呢個 run 完成咗，但係零候選 —— 唔係失敗。</span>}
      </div>
      {rows.length === 0 ? (
        <p className="empty">
          {filter === "candidates" ? "呢個過濾條件下無候選。" : "無符合嘅結果（包括資料錯誤）。"}
        </p>
      ) : (
        <div className="table-wrap">
          <table className="list">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>類別</th>
                <th>Rank</th>
                <th>Score</th>
                <th>Stage</th>
                <th>窗口</th>
                <th>主要原因</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const primary = primaryReason(row);
                return (
                  <tr
                    key={row.instrument.id}
                    aria-selected={row.instrument.id === selected}
                    onClick={() => onSelect(row.instrument.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") onSelect(row.instrument.id);
                    }}
                    tabIndex={0}
                  >
                    <td>{row.instrument.display_symbol}</td>
                    <td>
                      <span className={`chip ${row.category}`}>{CATEGORY_LABELS[row.category] ?? row.category}</span>
                    </td>
                    <td>{row.rank ?? "—"}</td>
                    <td>{row.analysis.score ?? "—"}</td>
                    <td>{stageLabel(row.analysis.stage)}</td>
                    <td>{row.analysis.selected_window?.window_sessions ?? "—"}</td>
                    <td>{primary ? reasonLabel(primary.code) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        {page.hasNextPage ? (
          <button disabled={page.isFetching} onClick={() => page.fetchNextPage()}>
            {page.isFetching ? "載入中…" : "載入更多（已載 " + rows.length + " 行）"}
          </button>
        ) : (
          <span className="muted">已載入全部 {rows.length} 行</span>
        )}
      </div>
      {selected !== null && <SymbolDetail client={client} runId={runId} instrumentId={selected} />}
    </section>
  );
}

function primaryReason(row: ResultRow) {
  return (
    row.analysis.selected_window?.reasons[0] ??
    row.analysis.alternative_windows.find((window) => window.reasons.length > 0)?.reasons[0] ??
    row.reasons[0]
  );
}

export function SymbolDetail({ client, runId, instrumentId }: { client: ApiClient; runId: string; instrumentId: string }) {
  // Only the selected symbol is fetched; React Query aborts the previous
  // request on key change, so a slow stale response can never overwrite the
  // currently selected chart.
  const detail = useQuery({
    queryKey: ["result", runId, instrumentId],
    queryFn: (context) => client.resultDetail(runId, instrumentId, context.signal),
  });
  const series = useQuery({
    queryKey: ["series", runId, instrumentId],
    queryFn: (context) => client.series(runId, instrumentId, 126, context.signal),
    retry: 1,
  });
  if (detail.isLoading) return <p className="muted">載入詳情…</p>;
  if (detail.isError || detail.data === undefined) {
    return <p className="error-text" role="alert">載入詳情失敗：{String(detail.error)}</p>;
  }
  const row = detail.data;
  const window = row.analysis.selected_window;
  const breakdown = window?.score_breakdown ?? null;
  const reasons = [...row.reasons, ...(window?.reasons ?? [])];
  const unavailableFeatures = Object.entries({ ...row.analysis.features, ...(window?.features ?? {}) })
    .filter(([, value]) => value === null)
    .map(([name]) => name);
  const alternativeWindows = row.alternative_windows.filter((candidate) => !candidate.eligible || candidate.reasons.length > 0);
  return (
    <div className="detail-grid" style={{ marginTop: 14 }}>
      <div>
        <h3 style={{ margin: "0 0 6px", fontSize: 14.5 }}>
          {row.instrument.display_symbol} · {stageLabel(row.analysis.stage)} · score {row.analysis.score ?? "—"}
        </h3>
        <div className="chart">
          {series.isError ? (
            <p className="error-text" role="alert">
              圖表資料缺失或損壞；唔會用最新行情補圖。{" "}
              {series.error instanceof ApiError && `（${series.error.code}）`}
            </p>
          ) : series.data === undefined ? (
            <p className="muted">圖表資料未提供。</p>
          ) : (
            <Chart series={series.data} />
          )}
        </div>
      </div>
      <div>
        <h3 style={{ margin: "0 0 6px", fontSize: 14.5 }}>入選原因與分項（候選／評估）</h3>
        <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
          {reasons.map((reason, index) => (
            <li key={`${reason.code}-${index}`}>
              {reasonLabel(reason.code)}
              {reason.parameters !== undefined && Object.keys(reason.parameters).length > 0 && (
                <span className="muted">（{JSON.stringify(reason.parameters)}）</span>
              )}
            </li>
          ))}
        </ul>
        {alternativeWindows.length > 0 && (
          <div>
            <h4 style={{ margin: "8px 0 4px" }}>其他窗口 gate</h4>
            <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
              {alternativeWindows.map((candidate) => (
                <li key={candidate.window_sessions}>
                  {candidate.window_sessions} 日：{candidate.available ? candidate.reasons.map((reason) => reasonLabel(reason.code)).join("、") : "資料不足，未能評估"}
                </li>
              ))}
            </ul>
          </div>
        )}
        {breakdown !== null && breakdown !== undefined && (
          <p className="muted">
            動量 {breakdown.momentum ?? "—"}｜趨勢 {breakdown.trend ?? "—"}｜整理{" "}
            {breakdown.structure ?? "—"}｜收窄 {breakdown.contraction ?? "—"}｜接近{" "}
            {breakdown.proximity ?? "—"}
          </p>
        )}
        {unavailableFeatures.length > 0 && (
          <p className="muted">不可用指標：{unavailableFeatures.join("、")}</p>
        )}
        <p className="muted">未評估（V1 邊界）：日內形態、成交量、流動性</p>
        {row.warnings.length > 0 && <p className="error-text">⚠ 資料警告：{row.warnings.map(diagnosticLabel).join("；")}</p>}
        <ReviewPanel key={`${runId}:${instrumentId}`} client={client} runId={runId} instrumentId={instrumentId} />
      </div>
    </div>
  );
}

function mergeReviewPages(current: Page<Review> | undefined, incoming: Page<Review>): Page<Review> {
  const merged = new Map((current?.items ?? []).map((review) => [review.instrument_id, review]));
  for (const review of incoming.items) {
    const previous = merged.get(review.instrument_id);
    if (previous === undefined || previous.revision <= review.revision) merged.set(review.instrument_id, review);
  }
  return { ...incoming, items: [...merged.values()] };
}

function upsertReview(current: Page<Review> | undefined, saved: Review): Page<Review> {
  return mergeReviewPages(current, { items: [saved], next_cursor: current?.next_cursor ?? null });
}

export function ReviewPanel({ client, runId, instrumentId }: { client: ApiClient; runId: string; instrumentId: string }) {
  const queryClient = useQueryClient();
  const reviewsKey = ["reviews", runId] as const;
  const reviews = useQuery({
    queryKey: reviewsKey,
    queryFn: async ({ signal }) => {
      const incoming = await client.reviews(runId, signal);
      // A GET can have started before a PUT completed. Never let its older
      // revision roll the cache (and therefore a clean form) backwards.
      return mergeReviewPages(queryClient.getQueryData<Page<Review>>(reviewsKey), incoming);
    },
  });
  const existing = reviews.data?.items.find((review) => review.instrument_id === instrumentId) ?? null;
  const [label, setLabel] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [knownRevision, setKnownRevision] = useState<number | null>(null);
  const [dirty, setDirty] = useState(false); // local edits are never clobbered by refetches
  const [savedRevision, setSavedRevision] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [reloadedAfterConflict, setReloadedAfterConflict] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [busy, setBusy] = useState(false);
  const identity = `${runId}:${instrumentId}`;
  const identityRef = useRef(identity);
  const draftVersion = useRef(0);
  const draftRef = useRef<{ label: string | null; note: string }>({ label: null, note: "" });
  const conflictDraft = useRef<{ label: string; note: string } | null>(null);

  useEffect(() => {
    if (identityRef.current === identity) return;
    identityRef.current = identity;
    draftVersion.current += 1;
    conflictDraft.current = null;
    draftRef.current = { label: null, note: "" };
    setLabel(null);
    setNote("");
    setKnownRevision(null);
    setSavedRevision(null);
    setError(null);
    setConflict(false);
    setReloadedAfterConflict(false);
    setDirty(false);
  }, [identity]);

  useEffect(() => {
    if (dirty || reviews.data === undefined) return;
    // Revision is part of the server baseline even if label/note happen to be
    // unchanged; it is the token needed by the next optimistic write.
    if (existing === null) {
      if (knownRevision !== null || label !== null || note !== "") {
        setLabel(null);
        setNote("");
        setKnownRevision(null);
        draftRef.current = { label: null, note: "" };
      }
      return;
    }
    if (knownRevision !== existing.revision || label !== existing.label || note !== existing.note) {
      setLabel(existing.label);
      setNote(existing.note);
      setKnownRevision(existing.revision);
      draftRef.current = { label: existing.label, note: existing.note };
    }
  }, [dirty, existing, knownRevision, label, note, reviews.data]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || label === null) return;
    const requestVersion = draftVersion.current;
    const requestIdentity = identity;
    const requestLabel = label;
    const requestNote = note;
    const requestRevision = knownRevision;
    setBusy(true);
    setError(null);
    setConflict(false);
    setReloadedAfterConflict(false);
    try {
      const body: { label: string; note: string; expected_revision?: number } = { label: requestLabel, note: requestNote };
      if (requestRevision !== null) body.expected_revision = requestRevision;
      const saved = await client.putReview(runId, instrumentId, body);
      queryClient.setQueryData<Page<Review>>(reviewsKey, (current) => upsertReview(current, saved));
      if (identityRef.current !== requestIdentity) return;
      setKnownRevision(saved.revision);
      if (draftVersion.current === requestVersion) {
        // 只有 2xx 回應先可以話「已保存」；cache、baseline、畫面三者
        // 同時採用同一份 server response，reload 後亦會取得它。
        setSavedRevision(saved.revision);
        setLabel(saved.label);
        setNote(saved.note);
        draftRef.current = { label: saved.label, note: saved.note };
        setDirty(false);
      } else {
        // The user edited while the request was in flight. The old response
        // is valid server state, but it did not save the newer local draft.
        setSavedRevision(null);
        setDirty(true);
        setError("舊草稿已保存；目前輸入仍未保存。");
      }
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setConflict(true);
        setReloadedAfterConflict(false);
        setError("標記已俾人更新過（revision 衝突）；請重新載入後再保存，輸入已保留。");
        const latest = draftRef.current;
        conflictDraft.current = draftVersion.current === requestVersion
          ? { label: requestLabel, note: requestNote }
          : { label: latest.label ?? requestLabel, note: latest.note };
      } else {
        setError(cause instanceof ApiError ? `${cause.code}: ${cause.message}` : String(cause));
      }
    } finally {
      setBusy(false);
    }
  };

  const reloadAfterConflict = async () => {
    setReloading(true);
    try {
      const latest = await reviews.refetch();
      const review = latest.data?.items.find((item) => item.instrument_id === instrumentId) ?? null;
      setLabel(review?.label ?? null);
      setNote(review?.note ?? "");
      setKnownRevision(review?.revision ?? null);
      draftRef.current = { label: review?.label ?? null, note: review?.note ?? "" };
      setSavedRevision(null);
      setDirty(false);
      setReloadedAfterConflict(true);
      setError(null);
    } finally {
      setReloading(false);
    }
  };

  const reapplyConflictDraft = () => {
    const draft = conflictDraft.current;
    if (draft === null) return;
    draftVersion.current += 1;
    setLabel(draft.label);
    setNote(draft.note);
    draftRef.current = draft;
    setDirty(true);
    setSavedRevision(null);
    setConflict(false);
    setReloadedAfterConflict(false);
    setError(null);
  };

  const markDraftDirty = (nextLabel: string, nextNote: string) => {
    draftVersion.current += 1;
    draftRef.current = { label: nextLabel, note: nextNote };
    if (conflict) conflictDraft.current = { label: nextLabel, note: nextNote };
    setDirty(true);
    setSavedRevision(null);
  };

  return (
    <form onSubmit={save} aria-label="人工覆核標記" style={{ marginTop: 12, borderTop: "1px solid var(--line)", paddingTop: 10 }}>
      <h3 style={{ margin: "0 0 6px", fontSize: 14.5 }}>人工覆核</h3>
      <div className="review-buttons" role="radiogroup" aria-label="標記">
        {Object.entries(REVIEW_LABELS).map(([value, text]) => (
          <button
            key={value}
            type="button"
            aria-pressed={label === value}
            onClick={() => {
              setLabel(value);
              markDraftDirty(value, note);
            }}
          >
            {text}
          </button>
        ))}
        <span className="muted">未標記 ≠ 唔值得睇</span>
      </div>
      <label className="field" style={{ marginTop: 8 }}>
        備註（最多 500 字）
        <textarea
          value={note}
          maxLength={500}
          onChange={(event) => {
            setNote(event.target.value);
            markDraftDirty(label ?? "", event.target.value);
          }}
        />
      </label>
      <div className="row">
        <button className="primary" type="submit" disabled={label === null || busy}>
          {busy ? "保存中…" : "保存標記"}
        </button>
        {savedRevision !== null && (
          <span className="ok-text" role="status">已保存（revision {savedRevision}）</span>
        )}
      </div>
      {conflict && (
        <button
          type="button"
          disabled={reloading}
          onClick={reloadAfterConflict}
        >
          {reloading ? "載入中…" : "重新載入最新標記"}
        </button>
      )}
      {conflict && reloadedAfterConflict && conflictDraft.current !== null && (
        <button type="button" onClick={reapplyConflictDraft}>套用未保存草稿</button>
      )}
      {error !== null && <p className="error-text" role="alert">{error}</p>}
      <p className="muted" style={{ marginTop: 6 }}>
        標記屬可變資料，綁定呢個 run 同標的；唔會自動帶去第二日，replay 亦唔會繼承。
        {knownRevision !== null && ` 目前 revision ${knownRevision}。`}
      </p>
    </form>
  );
}
