# 開發驗收記錄

更新：2026-09-05。

## Phase 0

已建立 package、鎖定依賴、Close-only domain／RuleConfig、API schema 初稿、ADR、
三平台 CI 骨架及 bounded provider probe。Windows 本機 `uv sync --locked` 成功，
原有 29 個離線測試通過；本次修正兩處 `zip(strict=False)` Ruff 問題。

README 原先指向本記錄但檔案缺失，本次補回。

**未驗收：** macOS／Linux CI 實跑；Yahoo probe 線上執行、拆股／除息價格口徑人工覆核、
incomplete-session 實測。線上 provider release 維持 BLOCKED；不得視 synthetic 測試為行情驗證。

## Phase 1

已實作離線特徵、gate、分項評分、stage、窗口選擇、結構化結果與穩定排名，
並加入合成資料測試及 `rules.md`。核心不讀取網絡、檔案、系統時間或資料庫。

測試涵蓋固定預期分數、公式、各 scoring band／stage 門檻、窗口歷史長度、null、
zero denominator、下跌／跌破／延伸、今日不改寫參考特徵、未來輸入拒絕、排序及序列化。
已補齊三個動量 OR 分支、整理 gate、支撐門檻、非 extended 優先、全 extended 選擇、
短歷史入池及 core import 邊界測試。本機結果：**70 passed**，Ruff check／format、mypy
均通過，wheel／source distribution build 成功。pytest 使用 `-p no:cacheprovider`，
避免既有 `.pytest_cache` 目錄的 Windows 存取拒絕；未跳過測試。

Phase 1 離線核心交付已完成。adapter 的「相同 Close、不同 OHLCV」invariance 留待 Phase 2，
目前由輸入合約拒絕非 Close 欄位、核心依賴測試及固定結果測試確保核心邊界。

下一步：依 Phase 2 加日曆品質驗證、watchlist、SQLite migration、
fixture provider 與 immutable snapshots。完整 CLI／報告及 API 分別屬 Phase 3／4。
