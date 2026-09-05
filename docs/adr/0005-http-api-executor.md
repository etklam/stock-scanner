# ADR 0005: HTTP API 的提交／執行分離、鎖架構與冪等佇列

日期：2026-09-06。狀態：已實作（Phase 4）。

## 背景

Phase 2–3 的 `ScanService.scan()` 是同步入口：鎖內建立 RUNNING row、抓資料、計算、
原子發布。Phase 4 需要一個本地 HTTP API，其提交路徑不得等待或執行掃描，且必須
在重啟後仍可證明「同一個 scan_id、最多一次執行」。

## 決策

### 1. prepare / execute_existing 取代單體 scan()

`ScanService.prepare()` 只組裝 QUEUED run（名單成員、resolved sessions、rules、
config hash、data mode、requested_at 全部固定，不碰 DB、不碰網絡）；持久化由
transport 選擇：API 用 `repository.enqueue()`（見下），CLI 用 `create_run()`。
`execute_existing(scan_id)` 以 `QUEUED → RUNNING` 的 compare-and-set 認領同一 row，
在同一個 id 上抓資料、寫 snapshot、原子發布結果。worker 不會再呼叫舊 `scan()`，
因此不存在第二個 run。CLI `scan` 仍是同步的 `prepare → create_run → execute_existing`，
不需要 HTTP。

### 2. 三層鎖，不把大鎖搬進 lifespan

- **執行器所有權**：`serve` 在啟動時以另一個 process 級 FileLock 獨占
  `executor.lock`，生命週期與 process 相同。第二個 `serve`、直接 CLI
  `scan/replay/refresh`、`init`/migration 都會在該鎖上衝突（CLI 沿用 exit 4）。
  read-only CLI（report/scans show/watchlist list）不取鎖，不被掃描阻塞。
- **服務鎖注入**：`bootstrap(service_lock=NullLock())` 讓 server 內的服務完全不
  重新取得該檔案；filelock 的实例跨 thread／跨 instance 不可重入，若 worker 內
  的 `market.obtain` 再取同一檔案，會與 lifespan 持有的鎖互相等待造成死結。
  CLI 路徑維持原本真 FileLock 行為（含 `test_executor_collision`）。
- **短 DB 交易**：API 的 watchlist 寫入是 SQLite CAS 短交易（revision 條件在
  UPDATE WHERE 內），不再包執行器鎖；提交與查詢只做短交易，不在網絡請求期間
  持有交易。`enqueue` 用 `BEGIN IMMEDIATE` 把「key 保留、容量檢查、job 寫入」
  放進單一交易，靠 owner+key unique index 保證並發唯一。

### 3. 冪等語義

Idempotency-Key 的 scope 是 principal + endpoint + key。`request_hash` 對
**client 原始請求欄位**（watchlist_id、ruleset_id、as_of_session 原值（含 null）、
data_mode）做 canonical SHA-256；解析預設日期、名單 revision 等可變輸入都在
key 查詢之後。因此「首次未傳 as_of、重試時已跨市場日」或「名單已修改／刪除」
的重試仍返回原任務。同 key 不同請求 → 409 IDEMPOTENCY_CONFLICT；首次成功 →
202 + Location；重試 → 200 + 真實目前狀態。key 隨 run 保留，無 TTL；FAILED
不自动重跑，重掃用新 key。

### 4. 狀態機與 recovery

`QUEUED → RUNNING → SUCCEEDED/PARTIAL/FAILED`。QUEUED 的 counts 全零（明確
「未開始」語義），RUNNING 提供 phase/processed/total/updated_at 的 progress，
terminal 才有 coverage counts。啟動 recovery 只在取得執行器所有權後執行：
遺留 RUNNING → FAILED/WORKER_INTERRUPTED（保留原 started_at、不刪結果、
不自動重跑）；QUEUED 保留並由 worker 繼續。shutdown 先合作式停止（每個
symbol 之間檢查 stop flag），有界 join；worker 未停時不釋放鎖、不 dispose
engine，交給下一次 startup recovery。daily baseline 在 job 真正開始（claim）
時固定，不在 GET 時動態選取。

## 後果

- API 與 CLI 共用 `prepare/execute_existing`，沒有第二套 scanner 邏輯。
- `executor.lock` 的語義從「每次掃描的互斥」變成「server 存續期的目錄所有權」
  ＋「CLI 每次操作的互斥」；upgrade 時舊版 CLI 與新版 serve 自然互斥。
- filelock 不可重入是硬約束：任何新服務代碼不得在持有所有權鎖的同一 process
  內再取該檔案，必須注入 NullLock 或等待明確設計的新鎖。
