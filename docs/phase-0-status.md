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

## Phase 3.5 — 2026-09-05 stabilization / live acceptance

起點 checkout `419710f02ec77ece9a3694f29858891b7ceb0586`，git status 為空，未回退／
覆蓋其他工作。本機 macOS 26.6 arm64、Python 3.12.12，uv.lock/依賴不變；沒有使用 sol-expert。

**已查核的歷史 remote CI：** [Phase 3 run 33964702780](https://github.com/etklam/stock-scanner/actions/runs/33964702780)
head 419710f，Ubuntu/macOS success，Windows pytest failure；Windows build/wheel 因此 skipped。
不能用 Phase 2 三平台 success 取代。本次修正 `test_partial_failed_running_exports_preserve_state`
的 UTF-8 read_text，source/tests/scripts 文字 IO／subprocess decoding 已 audit；沒有全域
UTF-8 mode、skip Windows 或刪測試。

本次先建立 regression 再修正：run/watchlist/cache 三個多 SELECT race 在舊碼均失敗，
DBAPI `in_transaction=False`，可讀出混合資源；顯式 BEGIN 後通過，擴至 readonly 共六例。
事件控制 writer 在 reader 第二 SELECT 前提交，沒有 sleep；WAL writer 可以 commit，
reader 完整保留舊 state/counts/results、revision/members 或 cache，owner/atomic tests 保留。
30 圖 call-count 舊碼為 31 run / 30 snapshot，修正後 1/1；CSV 為零 snapshot/chart 呼叫。

其餘修復：engine/rules replay 檢查移到 service，歷史 schema-1 snapshot 仍可讀／出圖；
reason_details 共用映射、CSV summary、Yahoo verified metadata / cache 相容路徑及集中 gate。
主要檔案：repository.py、reporting.py、snapshots.py、services.py、providers.py、
provider_release.py、report_renderer.py 與 tests/integration/test_phase35.py。

### 本次本機結果

uv sync --locked、Ruff check/format、strict mypy（33 source files）、build、repo 外
中文／空格目錄 installed-wheel CLI smoke 已執行通過。offline pytest **186 passed**（8144 個第三方 deprecation warnings，無 skip），
基線本輪實跑為 162 passed / 6094 warnings。
uv 沙箱 cache 使用 `/tmp/qscan-uv-cache` / `--no-sync`；沒有改 Python encoding mode。

### Live evidence 與範圍

- [Yahoo dated acceptance](phase35-provider-acceptance.json)：2026-09-05 12:26 UTC，價格事件與三標的 metadata checks PASS。來源對照和 gate 條件見 [ADR 0004](adr/0004-eod-trial-acceptance.md)。
- [真實 CLI](phase35-live-cli.json)：12:33 UTC，AAPL/MSFT/SPY，as-of 2026-09-04；init/import/refresh/scan/JSON+CSV+HTML/cache_only/replay 全部 exit 0。3 requested / 3 evaluated / 1 candidate / 0 errors，cache/replay 結果與 input hash 完全一致。第三方完整歷史只留臨時本機 DB/snapshots，不進 Git。
- 正常 Yahoo 為 EOD_TRIAL，並非 diagnostic 開關放行。metadata 為 USD、美股市場群組、EQUITY/ETF；未知資料拒絕。盤中真實觀察與真實 429／歷史修訂事件 **未執行／未觀察到**，其邊界僅有 offline tests；不聲稱 production SLA 或授權再分發。

### Report benchmark（實測秒）

[原始記錄](phase35-report-benchmark.json)，同機 100/1,000 synthetic symbols，每隻 504 sessions、
30 charts。script 分段量測，不把 fixture scan/fetch/core 或 report DTO 組裝計入表中。

| symbols | run load | snapshot decode/verify | chart-data | HTML/PNG rendering |
| --- | ---: | ---: | ---: | ---: |
| 100 | 0.023022 | 0.018172 | 0.007665 | 12.114395 |
| 1,000 | 0.065158 | 0.175570 | 0.008100 | 1.952539 |

單次執行，第一個 rendering 包含 cold matplotlib/font cache，第二個為同 process warm 狀態；
不能据此說 1,000 比 100 更快，亦非 end-to-end throughput／性能目標達標證明。
可重跑 `uv run python scripts/report_benchmark.py --output <path>`；沒有全域 mutable cache。

### 視覺驗收（本次實際瀏覽器）

依本次授權使用只 bind 127.0.0.1 的临時靜態 server，在 Codex browser 檢視上述真實 HTML，
桌面約 1265×712 和 390×844。Close/SMA/窗口/阻力圖及中文字體正常；mobile 圖縮放可見，
細字仍需放大。表格自身水平 scroll（343px container、793px candidate table），document
375px 沒有整頁橫向溢出。實際橫捲可讀右欄。
另以明示 synthetic 的 report DTO variations 檢查長中文／ASCII 名單、32字元 symbol、
null/None、PARTIAL、SUCCEEDED 零候選、chart error：文字換行、状态及告示可辨認。
這些 edge cases 是視覺 fixtures，不冒稱真實市場發生。viewport 已恢復，server 完成後停止。
沒有重新設計 UI 或前端框架；不是以 PNG signature 代替瀏覽器驗收。

### 本輪 remote CI

本輪首次 CI 和修正後通過的 CI 分別記於下方，保留失敗歷史。

### 尚未涵蓋

真實盤中來源觀察、公開產品行情授權／SLA、kill-process recovery、backup/restore 演練、
完整 API/auth/idempotency/queue、跨平台原生 GUI 視覺驗收。Phase 4 的前置共用服務已穩定，
下方三平台 CI 已通過，可以開始 Phase 4 API 開發；不代表 API 已存在。

### 本輪首個 CI 發現的第二處 Windows encoding 問題

[Run 33967148443](https://github.com/etklam/stock-scanner/actions/runs/33967148443)，
head `2f31bec11d7fe90efd43379c5fd7133e63a85b81`：三平台 pytest 與 build 通過，
Ubuntu/macOS wheel smoke 通過，Windows installed-wheel report JSON 命令 exit 2。
它是前輪 Windows pytest 失敗後從未執行到的步驟，不以本機 smoke 取代驗收。

在 subprocess 強制 cp1252 stdout 的最小 regression 成功重現 UnicodeEncodeError：
report 已產生 UTF-8 檔案，但 `emit` 向舊 codepage pipe 輸出含中文的 output path 失敗。
修正 CLI JSON 為標準 ASCII Unicode escapes（仍是合法 UTF-8 JSON，decode 後字串完全相同）；
報告檔案保持 UTF-8／CSV BOM，沒有設定全域 UTF-8 mode。測試刻意用非 UTF-8 codepage
證明適用，未 skip Windows。wheel smoke 現在亦在失敗時列出被捕捉的 stdout/stderr。


### Phase 3.5 最終程式驗收

[Run 33967493810](https://github.com/etklam/stock-scanner/actions/runs/33967493810)，
head `e46068fc672408b07aa3a7dd4e64b8318038b5ea`，completed/success。
已逐步查核 Windows／macOS／Ubuntu 三個 jobs：uv sync --locked、Ruff check/format、
strict mypy、完整 offline pytest、uv build、installed-wheel CLI smoke 全部 success，沒有 skip。

最終本機 **187 passed / 8144 第三方 warnings**；strict mypy 33 files、Ruff、wheel/sdist
build 與 repo 外 Unicode 路徑 installed-wheel smoke 再次通過。新增 cp1252 regression
使本輪總數由 186 增至 187；沒有更改 uv.lock、依賴版本或全域 UTF-8 mode。

程式、離線測試、實際來源、跨平台四項證據已分別完成：EOD 個人試用可用，Phase 4 的
前置条件滿足。尚未完成的盤中／production／授權及 Phase 4 功能仍依上方範圍列示。
本紀錄的後續提交只同步文件，已驗收程式版本為上述 SHA。

## Phase 4（2026-09-06 本輪實測）

基線：branch `codex/phase35-stabilization` 90dd5ad（工作樹乾淨，含 Phase 3.5 已驗收
e46068f 的全部修正）。本輪在該基線上直接實作 Phase 4，未 rebase／未 push／未改
uv.lock 依賴版本（dev group 新增無、runtime 依賴零變更——fastapi/uvicorn/httpx
皆已在 Phase 0 鎖定）。

### 本輪本機驗證（macOS 25.6.0，Python 3.12，全部離線）

- `uv sync --locked`：通過（73 packages audited）。
- `uv run pytest -m "not online"`：**208 passed**（Phase 3.5 基線 187 + 本輪 21：
  `test_phase4_api.py` 17、`test_phase4_executor.py` 4），0 skip、0 fail。
- `uv run ruff check .`、`uv run ruff format --check .`：全部通過。
- `uv run mypy`（strict，39 source files）：Success。
- `uv build` + `uv run python scripts/wheel_smoke.py`：repo 外 Unicode 路徑
  installed-wheel **CLI smoke**（原 Phase 3.5 全流程）與新增 **HTTP smoke**
  （`qscan serve` → 401 無 token → 建名單 → 202+Location 提交 → 輪詢至
  SUCCEEDED → 同 key 重試 200 回原 run → results/detail/series/changes/CSV
  （X-Scan-State、X-Result-Count、BOM、2 行）→ 停機 → 重啟後歷史 run 與
  CLI run 仍可讀、idempotency 對應不變）全部通過。
- `docs/openapi.json` 快照由 `scripts/openapi_snapshot.py` 生成，13 paths／
  24 schemas，`--check` 模式可比對。
- 真實 `qscan serve`（port 8901）+ `scripts/http_client_example.py` 全流程實跑：
  QUEUED → RUNNING（progress market_data 0/1）→ SUCCEEDED，results/detail/
  series（126 sessions）/changes/CSV 全部經 HTTP 取得。

### Crash recovery（真實 subprocess，非 mock）

`test_kill_running_server_recovers_on_restart`：以 fixture provider 在 server
子程序內將一個 job 卡在 fetch（harness-only 檔案訊號，production 無此介面），
第二個 job 維持 QUEUED，`kill -9` 整個 serve process 後以同一資料目錄重啟：
遺留 RUNNING 變 `FAILED/WORKER_INTERRUPTED`（原 started_at 保留、input_hash
null、無已發布結果）、QUEUED job 以同一 scan_id 完成並可查、crash 前已完成的
run document 逐欄不變、同 Idempotency-Key 重試仍回原（中斷）run 的 id。
連接埠由 bind(0) 動態取得，無 fork 依賴、無固定 sleep 等待。

### 冪等與併發

`test_concurrent_same_key_creates_one_run`（8 並發同 key：恰一個 202、其餘 200、
單一 run row）、`test_queue_limit_full_and_existing_key_still_readable`（queue 滿
後 429，既有 key 重試仍 200 可讀）、`test_idempotency_retry_after_date_roll_and_
watchlist_edit`（時鐘跨日＋名單修改／刪除後重試仍回原任務）、
`test_migration_0002_to_0003_preserves_history`（0002 舊 DB 經 init 升級，歷史
results／snapshot 可讀、新 queue 欄位可用）。

### 仍未完成／範圍外

- 三平台 CI 尚未對本輪 commit 執行（不可自動 push；push 後由既有
  `offline-quality` workflow 覆蓋，wheel_smoke 已含 HTTP 步驟）。
- 公開部署所需之真實身份/授權、TLS、多使用者資料授權、負載驗證（第 11.2 節）。
- Yahoo 盤中／production 行情、公開再分發授權（gate 維持 EOD_TRIAL）。
- 使用者取消任務 API、自動重跑、多 worker、PostgreSQL——依計劃延後。

---

## Phase 4.1 驗收（2026-09-06，commit da4fefa）

外部 review 對 5bcb4e1 提出 7 項；逐項獨立核實後 6 項屬實、1 項（streamed body
500）初判推翻、其後以 Windows CI 實測證實 review 為對（FastAPI route body 讀取會
把 receive 例外包成 HTTP 400）。修復與證據：

- **Shutdown ownership**：`run_serve` 停止超時改為 `os._exit(1)`——worker 與
  ownership lock 同生同死；真實 `run_serve` subprocess 測試
  （`test_shutdown_timeout_exits_process_and_releases_ownership`）在舊碼會 hang、
  新碼 exit 1 + 鎖立即可取得 + 下次啟動 recovery 收尾。
- **Worker 錯誤邊界**：pre-claim 失敗退避 + 每任務 5 次上限（`fail_queued` CAS），
  poison job 不再熱循環卡死隊列（`test_pre_claim_failure_backs_off_then_fails_poison_job`，
  舊碼無限循環）；post-claim（baseline bind）失敗必落 FAILED
  （`test_post_claim_bind_failure_marks_run_failed`，舊碼卡 RUNNING）。
- **終態不變量**：recovery 清 progress、封 counts（真 kill -9 crash 測試加入斷言）；
  recover 遇不可讀 document 改為 abort+rollback，不再寫入無法解析的偽 Run；
  零有效 evaluation 的 FAILED run 帶 `error=SCAN_FAILED` 與原因分佈 warning。
- **執行身份**：queued run 記錄 provider；engine/provider 不符以
  `EXECUTION_INCOMPATIBLE` 拒絕執行且 fixture provider 零呼叫。
- **API 合約**：results cursor 綁 scan id + sort version（cross-run cursor 400，
  舊碼 200）；OpenAPI 記錄 ScanAccepted 202／idempotent 200（ScanStatusOut）／
  Comparison／text/csv+headers／ErrorEnvelope 422（`test_openapi_contract_matches_wiring`）。
- 十個 Phase 4.1 regression tests 中六個以 `git stash` 實證在 5bcb4e1 全部失敗。

## Phase 5 驗收（2026-09-06，本地 V1 發布候選）

- 執行結果、備份演練、效能實測、live provider、安全檢查、產物與未完成項：
  全部彙總於 [release-checklist.md](release-checklist.md)，不在此重複。
- 新增自動化：`tests/integration/test_phase5_backup.py`（10）、
  `test_phase5_operations.py`（2）、`test_phase5_review.py`（1）；全套 227 passed。
- Windows shutdown 測試跨平台修復：console Ctrl+C 語義（CREATE_NEW_PROCESS_GROUP
  + `SetConsoleCtrlHandler(None, False)`，harness-only），三平台 CI run
  33986436893 起全綠。
- Live Yahoo smoke（2026-09-04 session）：`docs/acceptance/phase5-live-smoke-2026-09-06.json`
  ——3/3 evaluated、1 candidate、cache_only 與 force 一致、exact replay hash 相同、
  refresh 3/3；gate 維持 EOD_TRIAL。
- 人工覆核：`scans review-export` 工具交付；**真人標記 pending**，不宣稱效用數字。
- License 未決定：repository 無 LICENSE、pyproject 無 license 欄位，文件如實標明。

### CI 現況註記（2026-09-06）

GitHub Actions workflow 已因費用移除；上文各段提及的 CI run 編號屬歷史紀錄
（最後一次三平台全綠為 `614e7c3`）。現行品質把關 = 本地
`uv run python scripts/check.py`（規則見 [AGENTS.md](../AGENTS.md)）。

## Phase 5.1 驗收（2026-09-06，基線 9b65b07）

- **排程語義重寫**：session 去重改由市場日曆服務解析（新 `qscan sessions`
  CLI 與 `GET /api/v1/sessions/current`，OpenAPI 已更新）；意圖身份 =
  watchlist UUID + session + revision + provider + attempt；`--force` 新意圖、
  意圖內重試同 key/body；FAILED 維持 exit 1；state file 專屬 lock + 原子寫；
  401/403/409/429/timeout 分流。測試：`test_phase5_operations.py` 重寫為
  10 個情境（雙時鐘 API/CLI 全流程、lost-ack 重放同 run、401 不退回 CLI、
  revision 變更重掃、並發 scheduler 單一 run、參數驗證等）。
- **備份加固**：`backup.py` 重寫驗證路徑（bounds/集合一致性/格式解碼/單一
  來源還原/claim 發布）；`test_phase51_backup.py` 11 個 regression，其中
  bounds/一致性/format 類以 git stash 實證在 9b65b07 失敗、修正後通過；
  既有 9 個 backup 測試（含 WAL、舊 schema 升級、roundtrip）全部保留通過。
- **SQLite runtime 更正**：本機 uv cpython-3.12.12 帶 SQLite 3.50.4，屬官方
  WAL-reset bug（修復 3.51.3，2026-03-13；backport 3.50.7/3.44.6）受影響
  版本；Phase 5 checklist 的「無已知公告」宣稱已撤回。`doctor` 新增
  `sqlite_runtime` 檢查與 `sqlite_wal_reset_status` 判定；已實測修復路徑
  （brew python@3.12 = 3.12.14 + SQLite 3.53.4）。**狀態：BLOCKER**——
  切換鎖定 runtime 前不宣稱 SQLite 已驗收。
- **離線 gate**：`tests/conftest.py` socket guard 阻擋非 loopback 連線；
  全部排程/備份測試以固定時鐘執行，不依賴測試當日日期；本輪全套
  238→255 tests（最終數字以 gate 輸出為準）於 macOS 本機通過。
