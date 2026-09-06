import { useState } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { useAuth } from "./auth";
import { Connect } from "./screens/Connect";
import { History } from "./screens/History";
import { ListsScan } from "./screens/ListsScan";
import { RunDetail } from "./screens/Detail";

type Tab = "scan" | "history" | "detail";

export function App({ queryClient }: { queryClient: QueryClient }) {
  const auth = useAuth();
  const [tab, setTab] = useState<Tab>("scan");
  const [selectedRun, setSelectedRun] = useState<string | null>(null);

  if (!auth.connected || !auth.client) {
    return <Connect onConnect={auth.connect} />;
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
        <button aria-current={tab === "scan"} onClick={() => setTab("scan")}>名單與掃描</button>
        <button aria-current={tab === "history"} onClick={() => setTab("history")}>歷史</button>
        <button aria-current={tab === "detail"} disabled={selectedRun === null} onClick={() => setTab("detail")}>
          候選詳情
        </button>
      </nav>
      {tab === "scan" && <ListsScan client={client} onOpenRun={openRun} />}
      {tab === "history" && <History client={client} onOpenRun={openRun} selectedRun={selectedRun} />}
      {tab === "detail" && selectedRun !== null && (
        <RunDetail client={client} runId={selectedRun} />
      )}
    </>
  );
}
