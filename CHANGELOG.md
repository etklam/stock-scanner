# Changelog

## 0.1.0rc1 — Local V1 Release Candidate (2026-09-06)

**定位：** 本地 CLI + loopback API／個人美股 EOD 試用的發布候選。
不是公開多使用者平台，不是 production-ready App，不含下單或績效回測。

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
