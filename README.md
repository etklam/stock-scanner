# Close-only Setup Scanner

依每日 Close 建立可解釋、可重現的人工覆核候選。設計與分階段驗收見
[開發計劃](docs/development-plan.md)。目前已加入 **Phase 1 離線核心**，尚未提供 CLI 掃描、資料庫或 HTTP server。

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

## Provider probe

```sh
uv run python scripts/provider_spike.py --output provider-spike.json
```

這是明確的線上診斷，使用小量 AAPL／MSFT 歷史區間；不屬正式 provider。
輸出只保存欄位形狀、比較結果與版本，不保存整批行情。失敗以非零 exit code 表示。
即使所有機械檢查通過，價格口徑及 incomplete-session 行為仍須人工覆核才可放行。

本工具不提供買賣指令；成交量、流動性及日內形態未評估。
