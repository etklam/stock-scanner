import { useQuery } from "@tanstack/react-query";
import type { ApiClient, AutomationStatus, LatestReport } from "../api/client";

type Props = { client: ApiClient };

function reportSession(report: LatestReport, status: AutomationStatus): string | null {
  return status.job?.run_id === report.run_id
    ? status.job.session
    : status.latest_completed_session;
}

export function Today({ client }: Props) {
  const status = useQuery({
    queryKey: ["automation", "status"],
    queryFn: ({ signal }) => client.automationStatus(signal),
    refetchInterval: 5_000,
  });
  const latest = useQuery({
    queryKey: ["reports", "latest"],
    queryFn: ({ signal }) => client.latestReport(signal),
  });

  if (status.isLoading) return <p className="muted" role="status">正在載入今日狀態…</p>;
  if (status.isError || status.data === undefined) {
    return <p className="error-text" role="alert">無法載入自動掃描狀態：{String(status.error)}</p>;
  }

  const data = status.data;
  const report = latest.data?.state !== "MISSING" ? latest.data : data.report;
  const progress = data.run?.progress;
  const counts = data.run?.counts;
  const done = progress?.processed_symbols
    ?? (counts ? counts.evaluated + counts.excluded + counts.data_error : null);
  const total = progress?.total_symbols ?? counts?.requested ?? data.universe?.member_count ?? null;
  const progressPercent = progress && progress.total_symbols > 0
    ? Math.round((100 * progress.processed_symbols) / progress.total_symbols)
    : null;
  const activeSession = data.job?.session ?? data.next_due_session;
  const reportReady = report?.run_id !== null && report?.run_id !== undefined;

  return (
    <section className="panel today">
      <h2>今日報告</h2>

      {data.run && !["SUCCEEDED", "PARTIAL", "FAILED"].includes(data.run.state) ? (
        <section className="today-status" aria-live="polite">
          <h3>正在準備 {activeSession ?? "未定時段"} 報告</h3>
          <p className={`state-${data.run.state}`}>狀態：{data.run.state}</p>
          {progressPercent !== null && (
            <div className="progressbar" role="progressbar" aria-label="每日掃描進度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progressPercent}>
              <div style={{ width: `${progressPercent}%` }} />
            </div>
          )}
          <p className="muted">
            {progress && `${progress.phase} · `}
            覆蓋 {done ?? "—"} / {total ?? "—"}
            {counts && ` · 候選 ${counts.candidate} · 資料錯誤 ${counts.data_error}`}
          </p>
        </section>
      ) : data.run?.state === "FAILED" ? (
        <div className="report-ready">
          <h3>{data.job?.session ?? "未定時段"} 掃描失敗</h3>
          <p className="state-FAILED">狀態：FAILED</p>
          <p className="muted">
            覆蓋 {done ?? "—"} / {total ?? "—"}
            {counts && ` · 候選 ${counts.candidate} · 資料錯誤 ${counts.data_error}`}
          </p>
        </div>
      ) : reportReady ? (
        <div className="report-ready">
          <h3>{reportSession(report, data) ?? "最新時段"} 報告{report.state === "PUBLISHED" ? "已備妥" : "處理中"}</h3>
          <p className={`state-${report.state}`}>報告狀態：{report.state}</p>
          {report.error && <p className="error-text" role="alert">報告建立失敗：{report.error}</p>}
          {report.state === "PUBLISHED" && report.url && (
            <a className="primary button-link" href={report.url}>開啟最新報告</a>
          )}
        </div>
      ) : (
        <div className="empty">
          <strong>尚未建立每日報告</strong>
          <p>{data.enabled ? "qscan 會在下一個可用時段準備報告。" : "自動掃描未啟用；可前往設定啟用。"}</p>
        </div>
      )}

      <div className="today-meta">
        <section>
          <h3>時段</h3>
          <dl>
            <div><dt>最近完成</dt><dd>{data.latest_completed_session ?? "尚未完成"}</dd></div>
            <div><dt>目前工作</dt><dd>{data.job?.session ?? "沒有進行中工作"}</dd></div>
            <div><dt>下個目標</dt><dd>{data.next_due_session ?? "尚未排定"}</dd></div>
            <div><dt>預計時間</dt><dd>{data.next_due_time ?? "尚未排定"}</dd></div>
          </dl>
        </section>
        <Universe status={data} />
        <section>
          <h3>傳送</h3>
          <p className="muted">
            {data.notification?.outcome ? `結果：${data.notification.outcome}` : "尚未完成通知"}
            {data.notification && ` · 嘗試 ${data.notification.attempts} 次`}
            {data.notification?.detail && ` · ${data.notification.detail}`}
          </p>
        </section>
      </div>
      {latest.isError && !data.report && <p className="error-text" role="alert">最新報告暫時無法載入。</p>}
    </section>
  );
}

function Universe({ status }: { status: AutomationStatus }) {
  const universe = status.universe;
  return (
    <section>
      <h3>成分名單</h3>
      {!universe ? (
        <p className="error-text" role="alert">
          {status.enabled ? "尚未有可用 managed universe；新掃描會等候名單取得。" : "啟用後才會取得 managed universe。"}
        </p>
      ) : (
        <>
          <p className={universe.freshness === "STALE" ? "error-text" : "muted"}>
            {universe.member_count} 個成員 · {universe.freshness === "STALE" ? "快照已過期，唔會開始新掃描" : "快照可用"}
          </p>
          <p className="muted">
            <a href={universe.source_url}>資料來源</a>
            {` · ${universe.source_license} · revision ${universe.source_revision} · 擷取 ${universe.retrieved_at}`}
          </p>
        </>
      )}
    </section>
  );
}
