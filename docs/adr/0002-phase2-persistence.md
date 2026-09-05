# ADR 0002 — 同步掃描與 immutable inputs

日期：2026-09-05。狀態：Phase 2 已實作。

沿用 ADR 0001 的 close-only core。新增四個具體 application services 與 bootstrap，
provider/clock/calendar/repository/snapshot 邊界使用小型 typed Protocol。SQLite 使用
SQLAlchemy Core；不需要 ORM identity map，因此無 ORM Session 跨 thread 的生命週期問題。
不新增依賴，不預建 ReportService、HTTP route 或 worker。

六張業務表使用關聯鍵及查詢索引，完整 domain payload 用 JSON 保存，避免重複定義每一個
feature/window 欄位。初始 schema 凍結在 package 的 migration resources，wheel 外部目錄
smoke test 同時驗證 migration 可找到。後續 schema 變更需新 Alembic revision。

採單資料目錄 FileLock，所有 cache mutation／scan/replay／名單寫入串行，provider 和 core
期間不持有 SQLite write transaction。接受名單修改等待 scan 完成這個簡單限制；API 的
短請求接受／排隊與 recovery 留在 Phase 4。

snapshot 使用 canonical JSON + SHA-256 + gzip，先原子寫檔後 DB publication。
檔案系統與 SQLite 無跨資源原子交易，因此可能有孤立檔案；接受此狀況，禁止反過來讓
成功 run 指向未完成檔案。重播只接受相容 engine/schema，不能偷用最新 cache。

Yahoo adapter 與診斷已有實作，但來源價格口徑不因 synthetic 測試自動放行。
2026-09-05 live 機械 probe 通過，人工與 incomplete-session 驗收仍缺，正式 release 保持 BLOCKED。
Phase 2 的可靠性證據由 deterministic fixture、failure injection 與 installed-wheel demo 提供。
