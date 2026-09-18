import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient, AutomationStatus } from "../api/client";

const STATUS_KEY = ["automation", "status"] as const;

export function Settings({ client }: { client: ApiClient }) {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: STATUS_KEY,
    queryFn: ({ signal }) => client.automationStatus(signal),
  });
  const update = (value: AutomationStatus) => queryClient.setQueryData(STATUS_KEY, value);
  const enable = useMutation({ mutationFn: () => client.enableAutomation(), onSuccess: update });
  const pause = useMutation({ mutationFn: () => client.pauseAutomation(), onSuccess: update });
  const runNow = useMutation({ mutationFn: () => client.runAutomationNow(), onSuccess: update });

  if (status.isLoading) return <p className="muted" role="status">正在載入設定…</p>;
  if (status.isError || !status.data) return <p className="error-text" role="alert">無法載入自動掃描設定：{String(status.error)}</p>;

  const enabled = status.data.enabled;

  return (
    <section className="panel settings">
      <h2>自動掃描</h2>
      <p className={enabled === true ? "ok-text" : "muted"}>
        {enabled === null || enabled === undefined ? "自動掃描狀態未知" : `自動掃描已${enabled ? "啟用" : "暫停"}`}
      </p>
      {enabled === false && (
        <button className="primary" disabled={enable.isPending} onClick={() => enable.mutate()}>
          {enable.isPending ? "正在啟用…" : "啟用自動掃描"}
        </button>
      )}
      {enabled === true && (
        <div className="row">
          <button disabled={pause.isPending || runNow.isPending} onClick={() => pause.mutate()}>
            {pause.isPending ? "正在暫停…" : "暫停自動掃描"}
          </button>
          <button className="primary" disabled={pause.isPending || runNow.isPending} onClick={() => runNow.mutate()}>
            {runNow.isPending ? "正在排入…" : "立即執行"}
          </button>
        </div>
      )}
      {enable.isError && <p className="error-text" role="alert">啟用失敗：{String(enable.error)}</p>}
      {pause.isError && <p className="error-text" role="alert">暫停失敗：{String(pause.error)}</p>}
      {runNow.isError && <p className="error-text" role="alert">立即執行失敗：{String(runNow.error)}</p>}
    </section>
  );
}
