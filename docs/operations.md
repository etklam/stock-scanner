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
