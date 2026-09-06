import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { ApiClient, ApiError } from "../api/client";
import { CATEGORY_LABELS, REVIEW_LABELS, reasonLabel, stageLabel } from "../state/labels";
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
            <option value="all">全部有效評估（對照）</option>
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
          {filter === "candidates" ? "呢個過濾條件下無候選。" : "無符合嘅評估。"}
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
              {rows.map((row) => (
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
                  <td>{row.reasons[0] ? reasonLabel(row.reasons[0].code) : "—"}</td>
                </tr>
              ))}
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

function SymbolDetail({ client, runId, instrumentId }: { client: ApiClient; runId: string; instrumentId: string }) {
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
  if (detail.isLoading || series.isLoading) return <p className="muted">載入詳情…</p>;
  if (series.isError) {
    return (
      <p className="error-text" role="alert">
        圖表資料缺失或損壞；唔會用最新行情補圖。{" "}
        {series.error instanceof ApiError && `（${series.error.code}）`}
      </p>
    );
  }
  const row = detail.data;
  const data = series.data;
  if (row === undefined || data === undefined) return null;
  const window = row.analysis.selected_window;
  const breakdown = window?.score_breakdown ?? null;
  // Candidates carry their reasons on the selected window; gate failures on
  // the analysis itself. Either way the human reads a reason, never a blank.
  const reasons = row.reasons.length > 0 ? row.reasons : (window?.reasons ?? []);
  const unavailableFeatures = Object.entries(window?.features ?? {})
    .filter(([, value]) => value === null)
    .map(([name]) => name);
  return (
    <div className="detail-grid" style={{ marginTop: 14 }}>
      <div>
        <h3 style={{ margin: "0 0 6px", fontSize: 14.5 }}>
          {row.instrument.display_symbol} · {stageLabel(row.analysis.stage)} · score {row.analysis.score ?? "—"}
        </h3>
        <div className="chart">
          <Chart series={data} />
        </div>
      </div>
      <div>
        <h3 style={{ margin: "0 0 6px", fontSize: 14.5 }}>入選原因與分項</h3>
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
        {row.warnings.length > 0 && <p className="error-text">⚠ {row.warnings.join("；")}</p>}
        <ReviewPanel client={client} runId={runId} instrumentId={instrumentId} />
      </div>
    </div>
  );
}

export function ReviewPanel({ client, runId, instrumentId }: { client: ApiClient; runId: string; instrumentId: string }) {
  const reviews = useQuery({ queryKey: ["reviews", runId], queryFn: (c) => client.reviews(runId, c.signal) });
  const existing = reviews.data?.items.find((review) => review.instrument_id === instrumentId);
  const [label, setLabel] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [knownRevision, setKnownRevision] = useState<number | null>(null);
  const [dirty, setDirty] = useState(false); // local edits are never clobbered by refetches
  const [savedRevision, setSavedRevision] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);

  if (!dirty && existing !== undefined && (label !== existing.label || note !== existing.note)) {
    // First arrival (or refetch) of the saved review while the form is clean:
    // adopt server state so optimistic writes carry the current revision.
    setLabel(existing.label);
    setNote(existing.note);
    setKnownRevision(existing.revision);
  }

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || label === null) return;
    setBusy(true);
    setError(null);
    setConflict(false);
    try {
      const body: { label: string; note: string; expected_revision?: number } = { label, note };
      if (knownRevision !== null) body.expected_revision = knownRevision;
      const saved = await client.putReview(runId, instrumentId, body);
      // 只有 2xx 回應先可以話「已保存」。
      setSavedRevision(saved.revision);
      setKnownRevision(saved.revision);
      setDirty(false);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setConflict(true);
        setError("標記已俾人更新過（revision 衝突）；請重新載入後再保存，輸入已保留。");
      } else {
        setError(cause instanceof ApiError ? `${cause.code}: ${cause.message}` : String(cause));
      }
    } finally {
      setBusy(false);
    }
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
              setDirty(true);
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
            setDirty(true);
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
          onClick={() => {
            reviews.refetch();
            setConflict(false);
          }}
        >
          重新載入最新標記
        </button>
      )}
      {error !== null && <p className="error-text" role="alert">{error}</p>}
      <p className="muted" style={{ marginTop: 6 }}>
        標記屬可變資料，綁定呢個 run 同標的；唔會自動帶去第二日，replay 亦唔會繼承。
        {knownRevision !== null && ` 目前 revision ${knownRevision}。`}
      </p>
    </form>
  );
}
