import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ApiClient } from "../api/client";
import { stageLabel } from "../state/labels";

// History: cursor-paginated run list. A loaded page is NEVER presented as the
// complete result set — more rows load on demand via the signed cursor.

type Props = {
  client: ApiClient;
  onOpenRun: (runId: string) => void;
  selectedRun: string | null;
};

const PAGE = 20;

export function History({ client, onOpenRun, selectedRun }: Props) {
  const scans = useInfiniteQuery({
    queryKey: ["scans"],
    queryFn: ({ pageParam, signal }) => client.scansPage(PAGE, pageParam as string | undefined, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor,
  });
  const latest = useQuery({ queryKey: ["session"], queryFn: (c) => client.currentSession(c.signal) });
  const rows = scans.data?.pages.flatMap((page) => page.items) ?? [];

  if (scans.isLoading) return <p className="muted">載入中…</p>;
  if (scans.isError) return <p className="error-text">載入歷史失敗：{String(scans.error)}</p>;
  if (rows.length === 0) {
    return <p className="empty">仲未有掃描紀錄；喺「名單與掃描」提交第一次掃描。</p>;
  }
  return (
    <section className="panel">
      <h2>歷史掃描</h2>
      <p className="muted">最新已完成市況時段：{latest.data?.as_of_session ?? "—"}</p>
      <div className="table-wrap">
        <table className="list">
          <thead>
            <tr>
              <th>日期</th>
              <th>名單 revision</th>
              <th>狀態</th>
              <th>Evaluated</th>
              <th>Excluded</th>
              <th>Data error</th>
              <th>Candidates</th>
              <th>Scan</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((run) => (
              <tr
                key={run.id}
                aria-selected={run.id === selectedRun}
                onClick={() => onOpenRun(run.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") onOpenRun(run.id);
                }}
                tabIndex={0}
              >
                <td>{run.as_of_session}</td>
                <td>v{run.watchlist_revision}</td>
                <td>
                  <span className={`state-${run.state}`}>{run.state}</span>{" "}
                  {run.state === "FAILED" && run.error !== null && (
                    <span className="muted">（{run.error}）</span>
                  )}
                </td>
                <td>{run.counts?.evaluated ?? "—"}</td>
                <td>{run.counts?.excluded ?? "—"}</td>
                <td>{run.counts?.data_error ?? "—"}</td>
                <td>{run.counts?.candidate ?? "—"}</td>
                <td>
                  <code>{run.id.slice(0, 8)}</code>
                  {run.source_run_id !== null && <span className="chip">replay</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        {scans.hasNextPage ? (
          <button disabled={scans.isFetching} onClick={() => scans.fetchNextPage()}>
            {scans.isFetching ? "載入中…" : "載入更多"}
          </button>
        ) : (
          <span className="muted">已載入全部 {rows.length} 個 run</span>
        )}
        <span className="muted">點擊一行查看候選與圖表</span>
      </div>
    </section>
  );
}

export function stageText(stage: string | null): string {
  return stageLabel(stage);
}
