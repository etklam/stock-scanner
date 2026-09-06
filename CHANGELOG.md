# Changelog

## 0.1.0rc1＋Phase 6A — Local Candidate Review Workbench (2026-09-06)

**定位不變：** 單人本地 CLI + loopback API + 本地覆核 UI；不是公開多使用者產品，
不聲稱手機遠端可用。

### Phase 6A — A 修正

- 排程 wrapper：pending 意圖（已提交、結果未知）優先以原 key／原 body／原
  scan ID 恢復，唔再用今日 session/revision/provider 重建；改名／改 revision
  唔擋恢復；pending 期間 `--force` 拒絕（exit 4）；API 意圖 pending 而
  server 不可達唔退回 CLI；provider 身份核對 server `/health/live`（新
  `provider` 欄位）；state 損壞隔離並聲明去重保證收窄；stdout 收細為
  永遠一份 JSON；CLI 子程序 exit code＋文件形狀雙檢查；timeout 保留 pending。
- 備份：DB entry 串流落碟＋增量 hash（verify／restore）；manifest 計入
  統一總讀取預算；共用 bounded snapshot 驗證（hash＋支援 schema＋canonical
  bytes）——backup verify 接受嘅 snapshot 正式 reader 一定可讀；唯一 staging
  檔；發布改 no-clobber hardlink＋O_EXCL copy fallback（唔用可覆蓋 rename）。
- 驗證可信度：connect_ex 防護＋stub curl_cffi/pycurl（如實聲明覆蓋範圍）；
  每個 spawned interpreter 經 `tests/_offline_guard/sitecustomize.py`；
  完整 gate preflight 記錄實際 Python/SQLite 並對受影響 runtime 明確失敗
  （`--fast` 係開發快檢）；wheel smoke 核對 venv 內實際 runtime 並釘住
  gate interpreter；doctor 記錄實際 Python runtime。

### Phase 6A — B 本地覆核 UI

- `qscan serve` 同 origin 提供 `/ui/`（React＋TS＋Vite，wheel 內置編譯
  assets，使用者唔需要 Node；source checkout 缺 assets 時列出 build 命令）。
- 三個畫面：名單與掃描（idempotency key、reload/StrictMode 唔自動重建、
  實時進度）、歷史（cursor 分頁、候選／全部有效評估、各 state 獨立呈現）、
  候選詳情（SVG Close/SMA 圖、窗口與收市阻力、中文原因、三級覆核標記＋備註）。
- 集中 typed API client（型別由 OpenAPI 生成＋contract check；ErrorEnvelope/
  401/409/429/timeout 集中處理；429 有界退避；terminal 停止輪詢）；token
  只喺記憶體，401/輪換清敏感 cache。
- Origin allowlist 由受信任 server 配置形成（自己嘅 loopback origins＋
  明確 `--dev-origin`），同 origin 瀏覽器 POST 通過、外部/null 403；
  `/api` 404 保持 JSON envelope，唔被 SPA fallback 吃掉。
- Reviews：migration 0004 `scan_reviews`（向後相容）；`GET/PUT
  /api/v1/scans/{id}/reviews[/{instrument_id}]`（principal+run+instrument
  scope、revision 衝突 409、只可標記 evaluated 標的）；不改寫 score/rank/
  hash；replay 唔繼承；備份還原 round-trip；`scans review-export` CSV 併入
  已保存標記＋匯出時間注明。
- 測試：frontend typecheck/lint/vitest/production build＋OpenAPI-TS
  contract check；真實本機 API＋fixture 瀏覽器 E2E 四條（連線→建名單→
  提交→terminal→候選→圖及原因→保存覆核→reload 重新認證→標記仍在→
  無重建掃描）；1280px/390px 真實瀏覽器視覺驗收。
- 已知限制：真人覆核標記仍 pending；Windows/Linux 未跑本輪 gate；
  E2E 依賴系統 Chrome（Playwright CDN 下載受網絡限制）。

## 0.1.0rc1 — Local V1 Release Candidate (2026-09-06)

**定位：** 本地 CLI + loopback API／個人美股 EOD 試用的發布候選。
不是公開多使用者平台，不是 production-ready App，不含下單或績效回測。

### Phase 5.1 — Daily Scheduling & Backup Verification Corrections

- 排程去重改以市場日曆解析的 completed session 為準（新唯讀介面
  `qscan sessions` / `GET /api/v1/sessions/current`）；意圖身份 =
  watchlist UUID + session + revision + provider + attempt；`--force` 遞增
  attempt 建立新意圖，意圖內所有重試沿用同一 key 與 body；FAILED 維持
  exit 1、PARTIAL 維持 exit 3，不再偽裝成功；state file 改用 wrapper 專屬
  lock（不觸 executor.lock）+ 唯一暫存檔原子寫；401/403/409/429/timeout
  分流，HTTP 已接受後永不退回 CLI。
- 備份驗證加固：manifest 獨立上限、zip/內層 gzip 邊讀邊計數的解壓上限、
  entry 集合必須等於 manifest 宣告、runs_by_state/unfinished counts 驗證、
  snapshot 以支援 schema 解碼驗證（hash 正確但格式不支援仍拒絕）、restore
  對同一開啟的 archive 重驗 + claim-then-rename 發布。
- SQLite WAL-reset advisory 更正：本機 runtime 3.50.4 屬 AFFECTED（修復於
  3.51.3；backport 3.50.7/3.44.6）；doctor 列出 runtime 與判定；release
  acceptance 對受影響 runtime 維持 BLOCKER，直至切換已修 build。
- 離線 gate：suite-wide socket guard 硬性阻擋非 loopback 連線；排程測試
  以兩個固定時鐘執行，不依賴真實今日。

### Phase 5 — Local V1 Release Hardening

- **一致備份／驗證／還原**：`qscan backup create/verify/restore`。SQLite backup API
  取得含未 checkpoint WAL 的一致 copy；內容由 copy 決定；缺失／損壞 snapshot 令備份
  失敗；staging + 自我驗證 + 原子發布；還原只到全新目錄，archive 按不可信輸入處理
  （路徑／symlink／重複／大小上限），還原後新 token 舊 token 作廢。格式與邊界見
  [ADR 0006](docs/adr/0006-backup-format.md)。
- **日常操作**：`scripts/daily_scan.py` 排程 wrapper（serve 在→HTTP 提交，否則→
  獨立 CLI；同日重觸發不重跑；retry 沿用同一 idempotency key）；macOS launchd／
  Windows Task Scheduler／cron／systemd timer 範例見 [operations](docs/operations.md)。
- **人工覆核樣本**：`qscan scans review-export`（候選 + 固定 seed 非候選對照組 CSV，
  資料錯誤另列；標籤留空待人類填寫）。
- **效能量測**：分段 benchmark（core／validation／報告／渲染／API／備份），100–2,000
  symbols；實測結果與限制見 `docs/benchmarks/phase5-macos-arm64.json` 與 release
  checklist。1,000×504 core+ranking median 0.42s（目標 ≤10s）。
- **跨平台**：Windows shutdown 測試改用 console Ctrl+C 語義；三平台 CI 綠
  （lint/format/mypy/tests/OpenAPI check/build/wheel smoke）。
- **移除 GitHub Actions**（2026-09-06，費用考量）：品質把關改為本地
  `scripts/check.py`（`--fast` 跳過 build/wheel smoke）；規則記錄於 `AGENTS.md`。
  移除前最後三平台全綠 commit 為 `614e7c3`。
- **安全收尾**：OSV advisory 檢查（2026-09-06，13 套件 0 已知漏洞）；token 寫入
  保持在發布前 0600；子程序診斷輸出以 token redaction 保留於測試失敗訊息。

### Phase 4.1 — Reliability & API Contract Fixes (2026-09-06)

- Shutdown 超時改為 process 結束（ownership 鎖與 worker 同生同死），不再出現
  「舊 worker 仍在寫、第二個 serve 已取得鎖」。
- Worker 錯誤邊界：pre-claim 失敗有退避與 5 次上限（`fail_queued`），不再熱循環；
  post-claim 失敗必定落 FAILED，不再卡 RUNNING。
- 終態不變量：recovery 清 progress、封 counts；零有效 evaluation 的 FAILED run
  帶非空 `error` 與原因分佈 warning。
- 執行身份：queued run 記錄 provider；engine/provider 不符以
  `EXECUTION_INCOMPATIBLE` 拒絕執行。
- API 合約：results cursor 綁 scan id + sort version；OpenAPI 如實記錄
  ScanAccepted／idempotent 200 replay／Comparison／text/csv／ErrorEnvelope 422；
  超限 streamed body 精確 413。

### 歷史

Phase 0–4 的逐段驗收與失敗紀錄見 [docs/phase-0-status.md](docs/phase-0-status.md)
及 [docs/adr/](docs/adr/)。
