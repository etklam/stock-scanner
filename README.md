# Close-only Setup Scanner

依每日 Close 建立可解釋、可重現的人工覆核候選。設計與分階段驗收見
[開發計劃](docs/development-plan.md)。目前可使用 **Phase 2 同步 application services**：
匯入名單、交易日解析、fixture／cache 掃描、SQLite 查詢及 immutable snapshot 離線重播。
正式 CLI scan、報告及 HTTP server 尚未實作；Yahoo 線上 provider release 仍為 BLOCKED。

## 開發

需要 Python 3.12 和 uv。在 repository 根目錄執行：

```sh
uv sync --locked
uv run qscan --version
uv run pytest -m "not online"
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

目前 CLI 只有 `--help`／`--version`。計劃中的 `init`、`scan`、`serve` 等命令尚未實作。
離線測試只用 synthetic data，不連線 Yahoo。Python 其他 minor version 尚未支援。
CI 已配置 Windows、macOS、Linux；實際驗收狀態見 [Phase 0 紀錄](docs/phase-0-status.md)。

離線核心入口：`from qscan.core import analyze_symbol, rank_candidates`。
輸入為 `CloseSeries`、`RuleConfig`、`ScanContext`；公式、固定樣本結果及呼叫端責任見
[規則說明](docs/rules.md)。

## Phase 2 離線示範

PowerShell（資料寫到本機使用者資料目錄，可重複執行）：

```powershell
uv sync --locked
uv run python scripts/phase2_demo.py --data-dir "$env:LOCALAPPDATA\qscan-phase2-demo\示範 data"
```

macOS／Linux（請使用本機磁碟上的絕對路徑）：

```sh
uv sync --locked
uv run python scripts/phase2_demo.py --data-dir "$HOME/qscan-phase2-demo"
```

示範使用固定 2026-09-04 session、synthetic Close，建立獨立 demo 名單並保留既有資料。
它呼叫正式服務完成 scan → 關閉 DB → 重開 query → snapshot replay → cache_only scan，
輸出 `SUCCEEDED`、requested/evaluated/candidate 各 1、score 86、`replay_matches: true`。
每次會新增名單與 runs，不清理舊資料。安裝 wheel 後可在任意目錄執行
`python -m qscan.demo --data-dir <absolute-local-path>`。

```sh
uv build
uv run python scripts/wheel_smoke.py
```

wheel smoke 會建立暫存環境、按 lock 安裝依賴，並在 repo 外的中文／空格路徑跑上述流程。
安裝依賴可能需要網絡；安裝完成後的掃描示範不連網。

程式入口為 `qscan.bootstrap.bootstrap(provider, data_dir=..., clock=..., calendar=...)`，
回傳 `watchlists`、`market`、`scans`、`queries`；使用後呼叫 `close()`。
未覆寫資料位置時使用 platformdirs；亦可設定 `QSCAN_DATA_DIR`，相對路徑會被拒絕。
cache 最多 504 sessions，支援 `auto / cache_only / force`，snapshot replay 不讀 cache。
操作與備份限制見 [operations](docs/operations.md)，資料語義見 [data quality](docs/data-quality.md)。

## Provider probe

```sh
uv run python scripts/provider_spike.py --output provider-spike.json
```

這是明確的線上診斷，使用小量 AAPL／MSFT 歷史區間。
輸出只保存欄位形狀、比較結果與版本，不保存整批行情。失敗以非零 exit code 表示。
即使所有機械檢查通過，價格口徑及 incomplete-session 行為仍須人工覆核才可放行。
2026-09-05 Windows 實跑兩個機械案例通過，版本與結果見
[線上診斷紀錄](docs/phase2-provider-validation.json)。`YahooProvider` 已有獨立 adapter，
但預設拒絕 fetch；`diagnostic=True` 的資料仍標為 adjustment review，不能直接評估。

本工具不提供買賣指令；成交量、流動性及日內形態未評估。
