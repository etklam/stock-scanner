# Close-only Setup Scanner

**Phase 3.5 CLI / EOD 個人試用**：匯入名單、同步掃描、歷史查詢、固定 baseline 每日比較、
JSON／CSV／離線 HTML 報告，以及 immutable snapshot replay。無需寫 Python 或啟動 server。
鎖定依賴下 Yahoo 為 **EOD_TRIAL**：可掃描已完成交易日、metadata 已驗證的美股／ETF。
真實三標的流程已通過；盤中與 production 行情未放行，見 [驗收決定](docs/adr/0004-eod-trial-acceptance.md)。
規則與階段邊界見 [開發計劃](docs/development-plan.md)。

## 安裝

需要 Python 3.12、uv；在本 repository 執行：

```sh
uv sync --locked
uv run qscan --help
uv run qscan --version
```

獨立 wheel 安裝可執行 `uv build`，再以
`uv tool install --python 3.12 ./dist/close_setup_scanner-0.1.0-py3-none-any.whl` 安裝。
此後在任意目錄用 `qscan` 代替 `uv run qscan`。安裝依賴需要網絡；以下 demo 執行不需要。
跨平台 lock／wheel 驗收見 [紀錄](docs/phase-0-status.md)。

## A. 完全離線操作

最短流程（macOS／Linux）：

```sh
uv run qscan --data-dir "$HOME/qscan-phase3-demo" init
uv run qscan --data-dir "$HOME/qscan-phase3-demo" demo --output ./output
uv run qscan --data-dir "$HOME/qscan-phase3-demo" watchlist list
uv run qscan --data-dir "$HOME/qscan-phase3-demo" scans list
uv run qscan --data-dir "$HOME/qscan-phase3-demo" doctor
```

直接開啟 `output/scan-<scan_id>.html`。demo 回傳本次 watchlist_id、scan_id、baseline_id、
partial_id、zero_id 與輸出路徑。每次新增獨立名單與 runs，保留所有既有資料。
固定 sessions 為 2026-09-03／04；包含 stage change、零候選及缺價 PARTIAL。
全部資料明示 **SYNTHETIC / DEMO**；fixture 與 Yahoo cache 分開保存。

完整一般 CLI 流程（macOS／Linux；此段使用 `jq` 讀取 JSON，掃描工具本身不依賴 jq）：

```sh
uv sync --locked
export QSCAN_DATA_DIR="$HOME/qscan-phase3-demo"
uv run qscan init
DEMO=$(uv run qscan demo)
WATCHLIST=$(printf '%s' "$DEMO" | jq -r .watchlist_id)
SCAN_ID=$(printf '%s' "$DEMO" | jq -r .scan_id)
uv run qscan watchlist list
uv run qscan --provider fixture scan --watchlist "$WATCHLIST" --as-of 2026-09-04 --data-mode cache_only --format json
uv run qscan scans show "$SCAN_ID" --format json
uv run qscan scans changes "$SCAN_ID" --format json
uv run qscan report "$SCAN_ID" --format json --output ./output
uv run qscan report "$SCAN_ID" --format csv --all-results --output ./output
uv run qscan report "$SCAN_ID" --format html --top 30 --output ./output
uv run qscan replay "$SCAN_ID" --format json
uv run qscan doctor
```

PowerShell 完整流程（不需要 jq）：

```powershell
uv sync --locked
$env:QSCAN_DATA_DIR = Join-Path $HOME 'qscan-phase3-demo'
uv run qscan init
$demo = uv run qscan demo | ConvertFrom-Json
uv run qscan watchlist list
uv run qscan --provider fixture scan --watchlist $demo.watchlist_id --as-of 2026-09-04 --data-mode cache_only --format json
uv run qscan scans show $demo.scan_id --format json
uv run qscan scans changes $demo.scan_id --format json
uv run qscan report $demo.scan_id --format json --output ./output
uv run qscan report $demo.scan_id --format csv --all-results --output ./output
uv run qscan report $demo.scan_id --format html --top 30 --output ./output
uv run qscan replay $demo.scan_id --format json
uv run qscan doctor
```

`--top` 只限制 HTML 顯示／report chart series（預設 30、最大 50），不改保存的 counts、
results 或 rank；JSON 保存完整 results。CSV 預設候選，`--all-results` 包含非候選、排除及錯誤。
JSON 預設保留 charts；CSV 不解碼 snapshot 或計算 charts，另產出 `scan-<id>.summary.json`，
即使零候選仍可取得 run state、counts、warnings。三種格式皆保留 symbol／窗口原因。
CSV 為 UTF-8 BOM，比率是小數（0.2 = 20%），空欄是 unavailable，不是零。
`replay` 建立有 source_run_id 的新 run，不取代每日 baseline。

## B. 自訂名單與來源限制

先建立 UTF-8 `watchlist.txt`，每行一個 symbol；或 CSV 使用 `symbol,exchange` 欄位。
接受 BOM、中文／空格檔案路徑、TXT 註解、大小寫與已知 class-share aliases。
以下使用獨立本機資料目錄（macOS／Linux）：

```sh
uv run qscan --data-dir "$HOME/qscan-personal" init
uv run qscan --data-dir "$HOME/qscan-personal" watchlist import --name us-growth --file ./watchlist.txt
uv run qscan --data-dir "$HOME/qscan-personal" watchlist list
uv run qscan --data-dir "$HOME/qscan-personal" data refresh --watchlist us-growth
uv run qscan --data-dir "$HOME/qscan-personal" scan --watchlist us-growth --ruleset breakout-v1
```

最後兩步使用正常 Yahoo 路徑；無 `--as-of` 時取已完成收市及 30 分鐘 buffer 的最近交易日。
未知市場／非 USD／非 EQUITY 或 ETF 拒絕；metadata 不參與策略評分。
`doctor` 離線顯示集中 release 狀態，`doctor --online` 只做診斷，不改 gate。
版本未經驗收時仍 BLOCKED；請保留 uv.lock。cache_only 的空 cache 仍會回報無資料。

小名單完整實測可重跑（每次使用新的絕對資料目錄）：

```sh
uv run python scripts/live_cli_smoke.py --data-dir /tmp/qscan-my-live-test --as-of 2026-09-04 --output /tmp/qscan-live-result.json
```

或在上述 import / refresh 後繼續（macOS／Linux，使用 jq）：

```sh
SCAN_ID=$(uv run qscan --data-dir "$HOME/qscan-personal" scan --watchlist us-growth --format json | jq -r .id)
uv run qscan --data-dir "$HOME/qscan-personal" report "$SCAN_ID" --format json --output ./output
uv run qscan --data-dir "$HOME/qscan-personal" report "$SCAN_ID" --format csv --output ./output
uv run qscan --data-dir "$HOME/qscan-personal" report "$SCAN_ID" --format html --output ./output
uv run qscan --data-dir "$HOME/qscan-personal" scan --watchlist us-growth --data-mode cache_only --format json
uv run qscan --data-dir "$HOME/qscan-personal" replay "$SCAN_ID" --format json
```

已有同名名單須明確 `--replace`；可加 `--expected-revision 1` 作外部 revision 保護。
未指定時以讀到的 revision 作 optimistic concurrency check。`--name` 也可填 UUID 取代既有
名單；scan／refresh 的 `--watchlist` 接受名稱或 UUID，歧義名稱要求 UUID。

## 資料位置、錯誤與限制

Global options **放在命令之前**：`qscan --data-dir <absolute-path> --provider fixture scan ...`。
優先序：`--data-dir` → `QSCAN_DATA_DIR` → platformdirs 本機使用者資料目錄；相對 data-dir 拒絕。
報告 `--output` 可用相對路徑。預設 provider 是 Yahoo；demo 固定使用 fixture，
之後一般 cache-only fixture scan 必須加 global `--provider fixture`。

`init` 可重跑；只有 init／demo 的 bootstrap 初始化會升級 schema。舊 schema 先備份後執行 init。
歷史查詢／報告使用唯讀 DB，不下載、不 migration；help/version 不初始化。
exit codes：0 成功（含零候選）、1 失敗或零有效 evaluation、2 輸入／設定／權限問題、
3 部分成功、4 executor busy。成功讀取／匯出 PARTIAL run 為 0，內容保留 PARTIAL。
JSON stdout 只有一份合法 JSON；warnings／progress 在 stderr，錯誤有穩定 code。

報告失敗可用原 scan ID 重試，不改 scan 狀態。snapshot 缺失／損壞時明示圖表不可用，
replay 拒絕；不以最新 cache 補圖。比較條件與 legacy run 限制見
[data quality](docs/data-quality.md)，診斷與備份見 [operations](docs/operations.md)。

## 驗證與開發入口

```sh
uv sync --locked
uv run pytest -m "not online"
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
uv run python scripts/wheel_smoke.py
```

共用服務由 `qscan.bootstrap.bootstrap` 組裝；`watchlists`、`market`、`scans`、`queries`、
`reports`、`comparisons` 可供未來 API 重用。HTTP server、serve、背景 queue、登入／token、
crash recovery 均未實作。Phase 2 Python demo 保留作歷史驗收入口。

本工具是 close-only 初篩，流動性及日內形態未評估，不提供下單或績效回測。
