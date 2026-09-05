# Phase 2 本機操作

## 資料位置與 migration

使用 `bootstrap(provider, data_dir=Path(...))`，其次讀 `QSCAN_DATA_DIR`，否則使用
`platformdirs.user_data_path("qscan", appauthor=False)`。覆寫必須是絕對路徑，與 cwd 無關。
目錄內保存 `qscan.sqlite3`、SQLite WAL/SHM、`executor.lock` 及 `snapshots/`。
請指定可寫的本機磁碟；**不支援 NFS、SMB 或運行中的雲端同步 DB**。

bootstrap 在 data-directory FileLock 內執行 Alembic `upgrade head`。revision 0001
以及 env/config wiring 隨 wheel 安裝，不讀 repository 根目錄的 alembic.ini。
空 DB 建立六張業務表及 alembic_version，重跑保留資料。若需要直接執行 migration，
可呼叫 `qscan.adapters.persistence.repository.migrate(engine)`，呼叫者須先取得維護鎖。
不要用 metadata.create_all 取代 migration；schema_v1 是凍結的初始 migration 定義。

SQLite 每個 connection 開啟 foreign_keys、busy_timeout=10000、WAL。
SQLAlchemy Core repository 每次操作自建短 connection／transaction，不使用跨 thread ORM Session。
instruments metadata、run 內容及完整結果以版本化 Pydantic document 保存；用索引欄位
及 FK/unique constraints 維持身份、名單順序、owner/name、run/instrument 唯一與關聯。
未加入 Phase 4 的 HTTP idempotency keys／queue 欄位。

## 執行與交易

application services 是同步 Python 入口；沒有 server、worker、queue 或完整 scan CLI。
同資料目錄的 scan/replay/cache 更新、名單修改及 bootstrap migration 使用跨平台 FileLock，
等待上限 10 秒，第二個執行器超時會明確拋出 filelock.Timeout。queries 可同時讀取。
鎖保護整個掃描，但 provider 等待與核心計算期間不持有 DB write transaction。

先建立 RUNNING row，再取得資料、保存 snapshot、計算。最後 results、counts、input hash
及 terminal state 在同一短 DB transaction 公開。snapshot/result write 失敗以 FAILED 記錄，
不公開半份結果。若 DB 本身完全無法寫入，呼叫失敗，既有 RUNNING 不會冒稱成功。
本階段未實作 kill-process recovery；強制終止可能留下 RUNNING，需保留診斷並以新 run 重掃。
Phase 4 會在取得 executor lock 後做 WORKER_INTERRUPTED recovery，不自動重播半個工作。

Run 保存 `market_validation`（cache read/fetch/validation/cache write 合計）、`snapshot`、
`core`（含排名）及 `db_publication_precommit` 實際 perf_counter 秒數。
最後一項量至 publication transaction 內結果插入完成，不含 commit fsync；沒有把固定 clock
的零時間差冒充耗時，亦沒有宣稱 benchmark。requested/started/finished timestamps 使用注入 clock。

## snapshot 與 replay

schema_version=1。canonical bytes 為 UTF-8 JSON：sort_keys=true、separators=(',', ':')、
ensure_ascii=true、allow_nan=false；SHA-256 針對未壓縮 canonical bytes，不是 gzip bytes。
schema 保存完整 rules、context、日曆驗證 context、當次名單/revision、實際 Close suffix、
資料不可用原因、warnings 與 provenance。config hash 沿用 RuleConfig.canonical_json。

snapshot 先寫同目錄 temporary file，flush/fsync 後 atomic rename 為 `<sha256>.json.gz`，
之後 DB 才能參照。既有同 hash 檔案先驗證而不覆寫。private path 不出現在 Run DTO；
只保存內容 hash。cache、規則及名單修改均不改寫歷史快照或完成的 runs。

`app.scans.replay(scan_id)` 先做 owner-scoped query，只讀該 hash 的 snapshot；驗證 gzip、hash、
canonical 格式、schema、engine 版本及 context。缺檔／損壞／不相容直接失敗，不回退 cache。
成功建立新的 run，保留 source_run_id，rules 與輸入完全來自舊 snapshot。
replay 不連網、不重新解析交易日。分類、原因、分數及排名一致，特徵跨平台仍遵守核心 1e-12 容差。
這是已保存輸入的重現，不是 point-in-time 收益 backtest。

## 備份與信任邊界

停用所有 application 呼叫並取得 executor.lock 後，再一致備份整個 data directory。
不要在運行中只複製主 sqlite3 檔而漏掉 WAL；也不要只備份 DB 而遺失 snapshots。
還原到新的本機目錄，先查詢舊 run 再 replay 驗證。完整 backup/restore 工具及 kill-process
演練屬後續 hardening，本次未宣稱已完成。

snapshot 寫成但 DB publication 失敗可能留下孤立快照，正常查詢不自動清除。
一般示範亦不刪既有資料；maintenance 清理工具尚未提供。

ApplicationContext 是可信本機 bootstrap context，預設 local principal。不能將未來 HTTP 的
owner_id 任意值直接放進此 context；身份驗證與 transport 授權仍屬 Phase 4。
各 principal 只能查自己的 watchlist/run/results。這是分層邊界，不是已完成帳戶系統。

## Phase 3 CLI 與 schema 0002

本節更新上方 Phase 2 操作描述。正式 CLI 現已提供 init、watchlist import/list、data refresh、
scan、scans list/show/changes、report、replay、demo、doctor；沒有 serve 或 HTTP server。
完整可執行命令見 README。所有 global options 放在 subcommand 前；data-dir 優先於環境變數，
必須是絕對本機路徑。demo 的 `--output` 可直接產出 JSON/CSV/HTML。

CLI 只有 init／demo 初始化；其他命令以 `initialize=False` 開啟相容 DB，查詢／report 另用
SQLite URI `mode=ro`。schema 不符會提示 init，不在一般讀取中升級。程式呼叫 bootstrap
預設仍維持 Phase 2 初始化行為，呼叫者需要唯讀時明確指定 initialize=False, readonly=True。

migration 0002 新增 cache_by_provider／prices_by_provider，複製既有 cache，保留原 tables。
provider 是 cache key 的一部分，fixture 和 Yahoo 同 instrument 不互相覆蓋。run 新欄位
comparison／price_basis 存在既有 JSON document，提供向後相容 defaults，不刪庫重建。
請在維護鎖下備份整個資料目錄再 init；不支援 downgrade，以一致備份還原。

歷史 list 預設 30、上限 200，按 requested_at DESC、UUID DESC 排序，不載入 results rows。
show 保留所有分類結果。refresh 使用相同 calendar／market service／lock，只更新 cache，
不建立 run。以可信舊 cache fallback 的 refresh 會包含 warning，exit 3。

| 本次操作 exit | 意義 |
| --- | --- |
| 0 | 成功；掃描零候選也成功；成功讀取／匯出歷史 PARTIAL／FAILED 仍可為 0 |
| 1 | 執行／報告失敗，或 scan/replay/refresh 沒有有效結果 |
| 2 | 參數、交易日、設定、schema、檔案權限或查找驗證失敗；doctor local 不健康 |
| 3 | scan/replay 部分資料錯誤，或 refresh 部分失敗／fallback warning |
| 4 | executor lock busy；稍後重試，不刪 lock、不改寫 run state |

JSON 模式 stdout 是一個 document（含錯誤 envelope），warnings／progress 用 stderr。
stdout 錯誤包含 error.code/message，參數錯誤另有 details.hint；不輸出 exception stack 或
snapshot 私有路徑。NOT_FOUND 也是 owner scope 拒絕跨 owner 資源的行為。

report 先在記憶體完成 render，再以同目的目錄暫存檔、fsync、atomic replace 發布。
同 ID 重建可安全取代舊成功檔；render/write 失敗為 REPORT_ERROR、保留舊報告與 run，
可直接重試 report。圖表缺失會在 JSON/HTML 顯示 chart_error，不換用最新行情。
CSV 預設候選、--all-results 含所有分類；文字前綴 =/+/-/@ 防 formula injection，真正負數
數值不加引號前綴。JSON／HTML UTF-8，CSV UTF-8 BOM，比例欄為 decimal ratios。

預設 doctor 零網絡，檢查路徑／權限、schema、lock、snapshot directory、timezone/calendar。
未初始化會如實回報，不建立 DB；不修復 RUNNING 或解除 gate。local_healthy 與
Yahoo release BLOCKED 分開呈現；lock BUSY 不是 Yahoo 問題。
--online 額外做一次 AAPL 2024-06-03..07 來源診斷（一次 attempt、request timeout 5 秒），
不保存市場 cache，也不放行來源；provider cookie／HTTP library 的內部快取由該 library 管理。

## Phase 3.5 更新

本節為目前行為，前文 Phase 2/3 中的來源 BLOCKED 屬歷史狀態。Yahoo 集中 release
狀態見 `provider_release.py` 及 [ADR 0004](adr/0004-eod-trial-acceptance.md)。鎖定依賴為
EOD_TRIAL；offline doctor 不依賴網絡，版本不符時如實列出 blocker。

多 SELECT 組合一份 run/watchlist/cache 時，repository 顯式發出 SQLite `BEGIN`。
Python 3.12 sqlite3 legacy 模式不會因 SELECT 或 SQLAlchemy engine.begin 自動開始
DBAPI transaction。read connection 結束即 rollback/release；WAL writer 可同時 commit。
包括 URI mode=ro 的 queries 均由 event 交錯測試驗證 actual in_transaction=True。
每一資源一致，list 不承諾跨所有資源的同一時刻快照；GET 不取得整次 scan executor lock。

SnapshotStore.read 僅驗證 gzip/hash/canonical/schema/資料形狀。schema 1 的歷史 engine
metadata 可讀、可出圖；exact replay 另檢查目前 engine 與 breakout-v1 rules major 1，
不相容時在建立新 run 之前拒絕。可配置的 major-1 rules 保存於快照中，minor/patch
版本及參數重播使用原值，不拿今日預設覆蓋。缺檔／corruption／unknown schema 仍拒絕。

一次 ReportService.build 載入一次完整 run；需圖表時只驗證解碼一次 snapshot，建立索引
後批量產生最多 top 張 ChartSeries。單標的入口為 reports.series(run_id, instrument_id)。
JSON 預設 charts 與 Phase 3 相同；新增 charts_included 表明是否要求 charts。
CSV CLI 使用 include_charts=False，完全不讀 snapshot、不算 SMA。

CSV 同時原子發布 `scan-<id>.summary.json`，包含 run identity/state/counts/context、
comparison、sources/warnings/limitation，省略 results/charts。即使 CSV 只有 header，
摘要也不遺失；不插入假 ticker。每個檔案各自以 temp/fsync/replace 發布，摘要先於 CSV，
不是跨檔案 filesystem transaction；完成 run 不可變，重試同 ID 可恢復匯出。
HTML/JSON/CSV 的 reason_details 共用 symbol reasons/warnings、selected window reasons、
各 window available/eligible/reasons；原本 JSON run.results 語義保持不變。

重跑效能測量（synthetic，不含 fetch/core）：

```sh
uv run python scripts/report_benchmark.py --output /tmp/qscan-report-benchmark.json
```

重跑小量線上驗收（新目錄；日期必須為完成 session）：

```sh
uv run python scripts/yahoo_acceptance.py --output /tmp/qscan-provider-acceptance.json
uv run python scripts/live_cli_smoke.py --data-dir /tmp/qscan-live-new --as-of 2026-09-04 --output /tmp/qscan-live-cli.json
```

第一個 script 是證據收集，不能自行解除 gate。第二個逐步呼叫真正 CLI，只有摘要／hash
写入指定 output，行情只在 data-dir。實際安裝、UI 與 CI 證據見 [驗收紀錄](phase-0-status.md)。

CLI JSON stdout 使用標準 Unicode escapes，讓 Windows 舊 codepage 的重導向 pipe 也可
由 UTF-8 JSON reader 無損解析。這只改 JSON 序列化，未改系統／Python encoding mode；
JSON/HTML 檔案仍 UTF-8，CSV 仍 UTF-8 BOM。

## Phase 4：serve、queue、鎖與 recovery

`qscan serve`（預設 `127.0.0.1:8000`，`--host` 限 loopback、`--port`、
`--queue-limit`、`--dev-openapi`）在啟動時：bootstrap（initialize=False，schema
不符提示 init，exit 2）→ 取得 data-directory 級 `executor.lock` 所有權（被佔用
時 exit 4）→ startup recovery → 啟動單一 worker thread → 開始 HTTP。

鎖分三層（[ADR 0005](adr/0005-http-api-executor.md)）：

| 鎖 | 持有者 | 範圍 |
| --- | --- | --- |
| `executor.lock`（serve 所有權） | serve process 全程 | 排除第二個 serve、CLI scan/replay/refresh、init/migration（exit 4）；read-only CLI 不受影響 |
| 服務鎖（CLI=FileLock／serve=NullLock） | 每次掃描／cache 操作 | CLI 維持 Phase 2/3 行為；serve 內不重入所有權檔案，避免跨 thread 死結 |
| SQLite 短交易 | 單一操作 | watchlist CAS、提交（key 保留＋容量＋insert 單一 `BEGIN IMMEDIATE`）、claim CAS、原子發布、progress |

Queue 以 DB 為唯一事實來源：QUEUED 上限預設 20，超限 429；FIFO 按
`(requested_at, id)`；冪等 scope/principal、request hash、重試語義見
[api.md](api.md)。background task／記憶體 list 均未使用。

**Startup recovery**（只在取得所有權後執行）：遺留 RUNNING →
`FAILED/WORKER_INTERRUPTED`（保留原 started_at，無結果、不自動重跑）；QUEUED
保留並繼續；已完成結果不重算；idempotency key 對應不變。process 被強制終止時
由下一次啟動收尾，有真實 subprocess kill-process 測試覆蓋。

**Shutdown**：uvicorn 收到信號後停止接受新請求，executor 進入合作式停止
（每個 symbol 之間檢查 stop flag；已有界 grace，預設 30 秒）。thread 停止前
不釋放所有權鎖、不 dispose engine；worker 卡在 provider 時不再等待，process
結束交由下次 recovery，serve 以 exit 1 如實回報。asyncio 取消不等於 thread
已停止，不假裝完成。

**Migration 0003**：`scan_runs` 新增 `idempotency_key`／`request_hash` 欄位與
`uq_runs_owner_idempotency` unique index（NULL 可重複，legacy/CLI rows 共存）。
`qscan init` 在維護鎖內升級，舊 run 文件以預設值相容（`data_mode=auto`、
`progress=null`、`warnings=[]`），歷史 results、comparison 與 Phase 3.5 snapshot
可讀性不變；0002→0003 已有測試覆蓋。不支援 downgrade；請以一致備份還原。

運行中觀察：`/health/ready` 回 DB 與 executor 狀態；`doctor` 的 lock 檢查在
serve 運行時會如實顯示 BUSY。log/redaction：server 不記錄 request body 與
header；token 只存在 api-token.json（0600），錯誤回應不含 stack 與本機路徑。

## Phase 5：備份、還原、排程與診斷

### 一致備份 / 驗證 / 還原（[ADR 0006](adr/0006-backup-format.md)）

備份前**先停止** `serve` 與所有 scan/refresh/migration（backup 會取得 executor.lock，
佔用中如實回 `EXECUTOR_LOCKED`／exit 4）。可直接複製的操作：

```sh
# 備份（輸出不可在 data directory 內、不可覆蓋既有檔）
uv run qscan --data-dir "$HOME/qscan-personal" backup create --output "$HOME/backups/qscan-2026-09-06.zip"

# 離線驗證（不連網、不動資料；JSON 報告含 counts 與 warnings）
uv run qscan backup verify "$HOME/backups/qscan-2026-09-06.zip"

# 還原到全新目錄（必須不存在；完成後第一次 init/serve 建立新 token）
uv run qscan backup restore "$HOME/backups/qscan-2026-09-06.zip" --destination "$HOME/qscan-restored"
uv run qscan --data-dir "$HOME/qscan-restored" init        # 如 schema 較舊，明確升級
```

備份**未加密**：內含私人名單與價格歷史，請自行安全保存；checksum 只是完整性檢查。
還原不會執行 queued jobs——QUEUED 由還原後第一次 `serve` 啟動時繼續，遺留 RUNNING
由同一次 startup recovery 收尾為 `FAILED/WORKER_INTERRUPTED`；舊 token 不隨備份
遷移，新目錄一律建立新本機憑證。

### 日常排程（兩條路徑，不可混淆）

**規則：** `serve` 在跑 → 只用 HTTP client 提交；serve 沒跑 → 用獨立 CLI scan。
**永遠不要**在 serve 運行時對同一資料目錄直接跑 CLI `scan`（會 exit 4）。

**去重與意圖（5.1 修正後語義）：**
- 「是否已掃過」由**市場日曆服務解析的最新完成 session** 決定（`qscan sessions`
  或 `GET /api/v1/sessions/current`），wrapper 不自行判斷假期／DST／收市時間，
  也不以本地日期作身份。
- 一個掃描意圖 =（watchlist **UUID**、resolved session、名單 revision、provider、
  attempt）。Idempotency-Key 是該身份的 SHA-256——重試、斷線、程序重啟沿用
  **同一 key 與同一 body**；`--force` 遞增 attempt（新意圖、新 key），該次意圖內
  的重試仍用新 key。
- **名單修改（revision 變更）＝新意圖**：會重新掃描；相同身份的重跑不會：
  SUCCEEDED → exit 0、PARTIAL → exit 3（視為已涵蓋，補掃請 `--force`）、
  FAILED → **exit 1**（不自動重試失敗任務；要重掃請 `--force`）。
- watchlist 接受 name 或 UUID；name 歧義時拒絕（exit 2），不猜第一個。
- HTTP 已可能接受請求後**永不退回 CLI**：401/403/409 是設定／意圖錯誤（exit 2）、
  429 與 timeout 是 busy（exit 4，可恢復——下次觸發先查詢或重放原 key）。
- state：`<data-dir>/daily-scan-state.json`（版本化），以 wrapper 專屬
  `daily-scan.lock` 串行化 + 唯一暫存檔原子寫；該 lock 不觸 executor.lock，
  不會與 serve 死鎖。同一資料目錄同時只有一個 wrapper 操作（並發觸發會等待後
  去重，或 exit 4）。
- exit 0/1/2/3/4 與 CLI 一致；token 只進 Authorization header（不進 URL／log／
  排程定義）。

macOS launchd（`~/Library/LaunchAgents/com.qscan.daily.plist`）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.qscan.daily</string>
  <key>ProgramArguments</key><array>
    <string>/Users/USER/.local/bin/uv</string><string>run</string><string>python</string>
    <string>/Users/USER/src/stock-scanner/scripts/daily_scan.py</string>
    <string>--data-dir</string><string>/Users/USER/qscan-personal</string>
    <string>--watchlist</string><string>us-growth</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>9</integer><key>Minute</key><integer>15</integer>
  </dict>
  <key>StandardErrorPath</key><string>/Users/USER/qscan-personal/daily.log</string>
</dict></plist>
```

Windows Task Scheduler（`schtasks /create` 一行，可放進佈署筆記；用絕對路徑）：

```bat
schtasks /create /tn "qscan daily" /tr "C:\Python312\python.exe C:\src\stock-scanner\scripts\daily_scan.py --data-dir C:\qscan-personal --watchlist us-growth" /sc daily /st 09:15
```

Linux cron（`crontab -e`；systemd timer 亦可，同樣用絕對路徑與 `Environment=PYTHONUNBUFFERED=1`）：

```cron
15 9 * * 1-5  /usr/bin/python3 /opt/stock-scanner/scripts/daily_scan.py --data-dir /home/user/qscan-personal --watchlist us-growth >> /home/user/qscan-personal/daily.log 2>&1
```

注意：美股收市（17:00 ET）對應香港時間清晨；排程時間請自行對齊並預留數小時。
範例**只提供檔案內容**，本工具不會安裝、啟用或修改任何使用者系統排程。
missed schedule：launchd/cron 不補跑（可改用 launchd `StartCalendarInterval` 以外的
輪詢寫法自行處理）；同一天重複觸發由 state file 擋下；部分失敗（exit 3）仍記錄
session，可用 `--force` 重跑。排程不執行 `uv sync`／依賴升級。

### 診斷

- serve 啟動（stderr）：engine 版本、provider、recovered run 數。
- worker（stderr）：每個 job 的失敗原因與第幾次嘗試、退避、放棄（fail_queued）、
  開機 recovery 動作——不吞例外。
- backup/restore（stderr）：開始、完成；驗證失敗時說明資料未受影響。
- JSON stdout 永遠只有一份 JSON；診斷全在 stderr。token／Authorization／完整價格
  payload 不出現在任何輸出。
- `doctor`：預設離線、只做輕量檢查；`--online` 才做一次外部診斷（仍不改 gate）。
  深度全庫掃描未提供——如需要，直接 `backup verify`（含 integrity/FK/引用檢查）。
