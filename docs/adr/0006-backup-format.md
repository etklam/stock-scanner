# ADR 0006 — 本機備份格式、一致性與還原邊界

日期：2026-09-06　狀態：已接受　對應：Phase 5（`qscan backup create/verify/restore`）

## 背景

V1 需要可日常使用的備份／還原。資料目錄只有一個 SQLite（WAL 模式）與內容尋址的
`snapshots/`；`api-token.json` 是機密，`executor.lock` 與 WAL/SHM 是暫存狀態。
要求：停止 scanner 後的一致備份、離線驗證、只還原到全新目錄、不引入通用備份框架
或雲端同步。

## 決定

1. **一致性來源是 SQLite backup API，不是檔案複製。** create 先取得 `executor.lock`
   （serve/scan/refresh/migration 佔用時以 `EXECUTOR_LOCKED` 拒絕，不殺程序、不刪鎖），
   再用 `sqlite3.Connection.backup` 複製完整業務 DB——已 commit 但未 checkpoint 的
   WAL 內容一併帶走。之後**全部**內容決策（snapshot 清單、run counts、schema
   revision）都讀自該份 copy，不再回頭查正在變動的原 DB；短 watchlist 寫入即使未被
   executor lock 涵蓋，也不影響備份自洽。
2. **快照以內容尋址驗證。** 每個 `<sha256>.json.gz` 解壓後重新計算 SHA-256 必須等於
   檔名 digest；這是 engine/model 無關的檢查，舊 engine 寫的 snapshot 不會被新版本
   誤判。缺失或損壞的 referenced snapshot 令整次備份失敗——不略過、不假成功。
3. **Archive 是 zip + 版本化 manifest（format_version=1）。** 條目：
   `db/qscan.sqlite3`、`snapshots/<sha256>.json.gz`、`settings/*`（allowlist，現為空）、
   `manifest.json`。manifest 記 format version、建立時間、engine version、DB schema
   revision、DB/snapshot 檔案大小與 SHA-256、run/watchlist/snapshot counts、
   runs-by-state 與未完成（QUEUED/RUNNING）摘要。**不含** bearer token、cursor secret、
   lock 檔、WAL/SHM、可重建報告。settings allowlist 目前為空是如實描述：資料目錄
   現時沒有 DB 與 snapshots 以外的非機密設定；日後有新檔案時擴充 allowlist 並加測試。
4. **發布是原子的。** 全部寫入 staging，先跑完整 verify，再用不可覆蓋的 link/rename
   發布最終檔案；目的檔已存在即拒絕；輸出落在 source data directory 內即拒絕；
   任何失敗清理 staging，不留假成品。
5. **verify 完全離線、機器可讀。** 不啟動 scanner、不連網、不改使用者資料：驗 manifest
   結構與 format version（未知即拒絕）、每個條目 hash、SQLite `integrity_check` 與
   `foreign_key_check`、schema revision 屬已知集合、DB 引用的 snapshot 全部存在且
   hash 相符、manifest counts 與 DB 實際一致。engine 版本不同只產生 warning，不否定
   仍可讀的歷史。
6. **還原只到全新目錄，archive 視為不可信輸入。** destination 已存在或 staging 殘留
   即拒絕；驗證先行；手動逐條目解壓（絕不用 `extractall`），條目名拒絕絕對路徑、
   `..`、磁碟代號／UNC、反斜線、symlink 屬性與重複；條目數量、單檔與總解壓大小有上限。
   還原不 migration（舊 schema 由使用者明確 `qscan init` 升級，archive 原件不動）、
   不連網、不執行 queued jobs；run IDs、snapshot hashes、revisions、comparison
   bindings、owner identities 與 idempotency records 原樣保留。**不攜回舊 token**：
   還原後第一次 `init`/`serve` 在新目錄建立新本機憑證，owner 不變。
7. **未加密要講清楚。** checksum 只證完整性，不是加密或來源認證；備份內含私人
   名單與價格歷史，須由使用者自行安全保存。

## 後果

- 備份/還原/驗證可在任何安裝了本工具的機器離線執行，不需 provider 或網絡。
- format_version 與已知 schema revision 集合讓未來格式變化可被明確拒絕而非誤讀。
- 演練證據（含未 checkpoint WAL、舊 schema 升級、不可信 archive、忙碌拒絕）見
  `tests/integration/test_phase5_backup.py`、`scripts/wheel_smoke.py` 與
  [release checklist](../release-checklist.md)。

## 5.1 修訂（2026-09-06）

- **上限邊讀邊算**：manifest 讀入前有獨立大小上限；每個 entry（DB、snapshot、
  settings）有單檔上限並計入一份共享累積 budget；snapshot 內層 gzip 解壓以
  streaming 計數（多 member 亦受上限約束），達上限即以 typed validation error
  拒絕並清理 staging。測試以縮小 budget 證明，不分配真實 GB 記憶體。
- **集合一致性**：archive entry 集合必須等於 manifest 宣告集合——settings 必須
  在 allowlist 且帶 hash/size；snapshot digest 不可重複宣告；DB 引用、manifest
  集合與實際檔案三方一致，未引用內容明確拒絕；runs_by_state／unfinished
  counts 一併驗證；必要欄位有 typed validation，不出裸 KeyError。
- **格式驗證**：hash 正確不足以放行——snapshot 以支援版本的 decoder
  （schema_version）解碼驗證；unknown schema 拒絕；支援的歷史 schema 不因
  engine 版本不同而禁止歷史讀取；exact replay 的 engine 相容性仍在 replay 層。
- **單一來源**：restore 對**同一個已開啟**的 archive 完成重驗與解壓——先前
  verify(path) 的結果不再被假設對稍後 reopen 的同一有效；extraction 期間維持
  path/type/size 限制；staging 以 tempfile 唯一命名；發布以 exclusive claim
  （O_EXCL）加 rename，並發還原只有一個成功；verify 與 extraction 之間來源被
  換走的情況因單一開啟而消失，且 restore 本身永遠重新完整驗證。
