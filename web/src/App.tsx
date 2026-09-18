import { useEffect, useState } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { useAuth } from "./auth";
import { Connect } from "./screens/Connect";
import { History } from "./screens/History";
import { ListsScan } from "./screens/ListsScan";
import { RunDetail } from "./screens/Detail";
import { Settings } from "./screens/Settings";
import { Today } from "./screens/Today";

type Tab = "today" | "history" | "settings" | "advanced" | "detail";

export function reportPath(search: string): string | null {
  const report = new URLSearchParams(search).get("report");
  return report
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(report)
    ? `/api/v1/reports/${report}`
    : null;
}

export function App({ queryClient }: { queryClient: QueryClient }) {
  const auth = useAuth();
  const [tab, setTab] = useState<Tab>("today");
  const [selectedRun, setSelectedRun] = useState<string | null>(null);

  useEffect(() => {
    if (!auth.connected || !auth.client) return;
    const path = reportPath(window.location.search);
    if (path) window.location.assign(path);
  }, [auth.connected, auth.client]);

  if (auth.checking) {
    return <p className="muted" role="status">正在連線本地 qscan…</p>;
  }
  if (!auth.connected || !auth.client) {
    return <Connect onConnect={auth.connect} onRetry={auth.retrySession} />;
  }
  const client = auth.client;

  const openRun = (runId: string) => {
    setSelectedRun(runId);
    setTab("detail");
  };

  return (
    <>
      <h1>qscan 覆核工作台</h1>
      <div className="banner" role="note">
        限制：本工具係 close-only 初篩；日內形態、成交量與流動性未評估。
        分數只係人工覆核優次，唔係勝率。Yahoo 資料屬 EOD_TRIAL（已完成交易試用），
        fixture 資料會標明 SYNTHETIC／DEMO。
      </div>
      <div className="row">
        <span className="muted">已連線本地 API</span>
        <button onClick={() => { auth.disconnect(); queryClient.clear(); }}>斷線</button>
      </div>
      <nav className="tabs" aria-label="主畫面">
        <button aria-current={tab === "today"} onClick={() => setTab("today")}>今日</button>
        <button aria-current={tab === "history"} onClick={() => setTab("history")}>歷史</button>
        <button aria-current={tab === "settings"} onClick={() => setTab("settings")}>設定</button>
        <button aria-current={tab === "advanced"} onClick={() => setTab("advanced")}>進階</button>
      </nav>
      {tab === "today" && <Today client={client} />}
      {tab === "history" && <History client={client} onOpenRun={openRun} selectedRun={selectedRun} />}
      {tab === "settings" && <Settings client={client} />}
      {tab === "advanced" && <ListsScan client={client} onOpenRun={openRun} />}
      {tab === "detail" && selectedRun !== null && (
        <RunDetail client={client} runId={selectedRun} />
      )}
    </>
  );
}
