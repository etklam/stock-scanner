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

## Phase 2 — 2026-09-05 本機驗收

本段接續歷史紀錄，不把 Phase 0/1 的測試數當作本次結果。
開始時 `git status --short` 為空，沒有既有未提交修改；baseline 實跑
`uv sync --locked`、70 個 offline tests、Ruff check/format、mypy 通過。
baseline pytest 有既有 `.pytest_cache` Windows 存取警告，未跳過測試。

本次新增同步 Watchlist/MarketData/Scan/ScanQuery services、typed boundaries、bootstrap、
六張 SQLite 業務表與 packaged Alembic migration、owner scope、TXT/CSV 匯入與 revision、
NYSE cutoff／overrides、Close quality、fixture 與 gated Yahoo adapter、三種 cache 模式、
overlap/full refresh/quarantine、immutable gzip snapshot、原子 publication 及 offline replay。
core、domain gate/score/stage/window 語義未改動。依賴與 uv.lock 未變更。

本機 Python 3.12.10、Windows 11，最終離線測試 **128 passed**。
測試涵蓋 migration 重跑、DB restart、雙 principal 隔離、Close/OHLCV/future invariance、
交易日 cutoff、cache_only 禁網、短歷史、缺口、修訂與拆股完整替換、refresh fallback warning、
全部不可評估、部分失敗、零候選、snapshot rename 失敗、第二筆 result insert 失敗、
cache replacement rollback、不可見的未提交結果、缺失/損壞/不相容 snapshot 及 executor collision。

實際執行結果：

| 命令 | 本機結果 |
| --- | --- |
| `uv sync --locked` | 通過 |
| `uv run pytest -m "not online"` | 128 passed；第三方 calendar/NumPy deprecation 及既有 pytest cache warning |
| `uv run ruff check .` | 通過 |
| `uv run ruff format --check .` | 通過 |
| `uv run mypy` | 通過，27 source files，strict 未降低 |
| `uv build` | wheel 與 source distribution 成功 |
| `uv run python scripts/phase2_demo.py --data-dir <absolute-local-path>` | SUCCEEDED；1 evaluation/candidate、score 86、replay_matches=true |
| `uv run python scripts/wheel_smoke.py` | wheel 安裝至新 venv，在 repo 外中文／空格目錄完整 scan/query/replay/cache_only 通過 |
| `uv run python scripts/provider_spike.py --output docs/provider-spike-phase2.json`（輸出更名保存） | 兩個機械案例 PASS；release 仍 BLOCKED |

三平台 CI 已增加 installed-wheel fixture E2E；**本次沒有執行 macOS/Linux runners**，
不能用 Windows 本機結果聲稱三平台均已驗收。未執行 benchmark、kill-process recovery、
backup/restore 演練或人工候選品質覆核。

Yahoo 實跑 yfinance 0.2.66、pandas 2.3.3、NumPy 2.5.2。AAPL 2020 拆股、
AAPL/MSFT 2024 除息與單/多 ticker、exclusive end 的機械檢查通過，完整輸出見
[phase2-provider-validation.json](phase2-provider-validation.json)。仍缺價格口徑人工確認、
真正盤中未完成日線觀察與安全市場 metadata 確認。adapter 預設阻擋；diagnostic raw
亦保留 adjustment-review 標記，不用 synthetic 證據靜默放行。

Phase 2 離線交付完成，線上來源驗收仍待完成。Phase 3 前應跑三平台 CI 並維持 Yahoo
release blocker；CLI/報告需沿用本次服務，不另寫掃描流程。HTTP server、queue、帳戶／token、
idempotency、crash recovery 屬 Phase 4；本次不稱 App-ready 或 V1 完成。

## Phase 2 — 三平台 CI 補充（2026-09-05）

保留上方本機驗收與當時「未執行 macOS/Linux runners」的歷史描述。
本次唯讀查核 GitHub Actions
[run 33961076219](https://github.com/etklam/stock-scanner/actions/runs/33961076219)：
head `0b3137aeef9cbd3df08908b7d1d0883ae1f2b6e4`，整體 conclusion=success；
windows-latest、macos-latest、ubuntu-latest 三個 jobs 均 completed/success，
包含各自 offline tests、quality、build 與 installed-wheel smoke。
這是 Phase 2 的歷史 baseline，不代表本次 Phase 3 修改的 CI 成功。

## Phase 3 — 2026-09-05 本機驗收

實際 checkout 為 `0b3137a`，開始時 git status 為空，未 checkout／回退舊版。
本機 macOS 26.6、Darwin arm64、Python 3.12.12。baseline：uv sync --locked 成功，
128 個離線測試、Ruff check/format、strict mypy 全部通過。
uv 預設 cache 遇沙箱寫入限制，測試與品質指令使用 /tmp/qscan-uv-cache 及既有 locked venv
（uv run --no-sync）；build／wheel install 取得執行授權後使用正常 uv cache。
沒有修改 pyproject.toml／uv.lock 或升級依賴。

交付正式 Typer CLI、同步 refresh、read-only history/report bootstrap、typed report/chart/
comparison/refresh DTO、run-bound charts、固定 daily baseline、JSON/CSV/self-contained HTML、
明確診斷及 synthetic demo。新增 schema 0002，以 provider 隔離 cache 並保留／搬入舊資料。
Run document 新欄位有 defaults，legacy comparison 明示 unavailable。core score/stage/gates/
window 語義未修改。未使用 subAgent 完成實作或審查。

| 本次命令／驗證 | 實際結果 |
| --- | --- |
| uv sync --locked | 成功，75 packages resolved / 73 audited |
| uv run pytest -m "not online"（使用上述 cache/no-sync 環境） | **162 passed**，無 skip；6094 個既有 calendar/NumPy deprecation warnings |
| uv run ruff check . | 通過 |
| uv run ruff format --check . | 通過 |
| uv run mypy | 通過，32 source files，strict 未降低 |
| uv build | wheel／source distribution 成功 |
| uv run python scripts/wheel_smoke.py | 本次 wheel 安裝至新 venv，repo 外中文／空格路徑，真實 qscan init/demo/list/cache_only scan/show/changes/JSON+CSV+HTML report/replay/doctor 通過 |
| README macOS/Linux 完整流程 | 使用 /tmp 絕對 data-dir 實跑通過；demo --output 三份報告、一般 CLI scan/query/report/replay 與 doctor local_healthy=true |
| GitHub Actions Phase 3 matrix | 已延伸使用同一 wheel smoke script；**本次尚未 push／執行新 remote CI** |

新增 34 個 cases 涵蓋 init 保留、help/version 無初始化／網絡、名稱／UUID／歧義／replace／
revision conflict、中文/BOM/非法輸入、JSON 分流、exit 0..4、PARTIAL/FAILED 查詢與匯出、
RUNNING 診斷、cache source 共存、v1 migration 保留、snapshot 修改／缺失／損壞、報告 atomic
write/render failure、escaping／CSV formula 與負數、top/rank/count/chart geometry、baseline
相鄰 session/config/basis/engine/start cutoff/tie/replay/legacy、universe、無有效 evaluation、
new/drop/stage/score/window/revision、跨 owner 與 DB/schema 只讀驗證。原有 128 cases 保留。

HTML 測試驗證內嵌 PNG signature、UTF-8、template escaping、無外部 URL，Agg 不需 display。
實際報告成功產生；**尚未完成桌面／手機瀏覽器視覺驗收**：本機 file:// 導覽被瀏覽器安全
政策阻擋，未繞過。impeccable detector 僅降級 regex 模式，不能視為完整 contrast/layout
驗收；responsive CSS／局部 table scroll 已實作。未執行本次 Windows/Linux runner、
PowerShell 實機流程、doctor --online、Yahoo 人工驗收、benchmark、kill-process recovery、
備份還原演練或候選品質人工覆核。

Yahoo release **BLOCKED**，仍缺價格 basis 人工覆核、盤中 incomplete-session 及可信市場
metadata。Phase 3 離線能力交付；真實市場日常使用尚未放行。進 Phase 4 前應補本次三平台
CI 與瀏覽器窄螢幕驗收並固定 shared DTO；Phase 4 再實作 HTTP/auth/idempotency/queue/
crash recovery 及其安全合約測試，不能把現有同步 CLI 宣稱 App-ready。
