# Release Checklist — 0.1.0rc1（Local V1 Release Candidate）

日期：2026-09-06　基線 commit：`da4fefa`（Phase 4.1）→ 本輪提交
定位：**本地 CLI + loopback API／個人 EOD 試用的發布候選**——不是公開 App、
不是 production-ready 多使用者平台。每項列出證據；未完成項明確標示原因。

## 1. 執行結果（2026-09-06，macOS 26.6 arm64，Python 3.12.12）

| 命令 | 結果 |
| --- | --- |
| `uv sync --locked` | 通過（鎖定依賴未改動，`uv.lock` 本輪零變更） |
| `uv run pytest -m "not online"` | 227 passed（Phase 0–4.1 的 208 + Phase 5 新增 19） |
| `uv run ruff check .` / `ruff format --check .` | 全綠 |
| `uv run mypy` | Success: no issues found in 41 source files |
| `uv build` | wheel + sdist 產生；wheel 內容掃描無 DB/token/snapshot/報告 |
| installed-wheel smoke | CLI/HTTP/restart 段 + backup→restore→restored replay/API 段全過（`scripts/wheel_smoke.py`） |
| OpenAPI contract check | `scripts/openapi_snapshot.py --check`，屬本地 gate `scripts/check.py` 的一步 |
| 三平台自動驗證 | **GitHub Actions 已於 2026-09-06 移除（費用考量，見 [AGENTS.md](../AGENTS.md)）**。最後一次三平台全綠為 commit `614e7c3`（含 Windows/macOS/Linux wheel smoke 與本 checklist 全部測試）；之後提交由本地 gate 單機覆蓋，跨平台需手動於各平台執行 `scripts/check.py` |

## 2. Phase 4.1 遺留問題（已於 `da4fefa` 修復，本輪僅引用）

- Windows shutdown 測試 `ValueError: Unsupported signal: 2`：harness 改以
  CREATE_NEW_PROCESS_GROUP + console CTRL_C_EVENT（子程序 re-enable Ctrl+C delivery），
  production shutdown 路徑不變（uvicorn graceful → grace timeout → process exit）。
  強制 kill 的 crash recovery 由另一測試覆蓋，兩者未混淆。
- 失敗診斷：子程序 stdout/stderr 寫檔，斷言訊息附 token-redacted tail；finally 必清理。
- 三平台驗證：見上 CI run；UTF-8／cp1252 pipe、中文／空格路徑測試全數保留。

## 3. 備份／驗證／還原（ADR 0006）

- 一致性：executor lock 佔用拒絕（exit 4）；SQLite backup API 含未 checkpoint WAL
  ——`test_backup_captures_open_database_with_wal` 證明引擎開啟中備份仍完整。
- 失敗語義：missing/corrupt snapshot → 整次備份失敗、無 staging 殘留
  （`test_backup_fails_on_missing_or_corrupt_snapshot`）。
- 原子發布：拒絕覆蓋、拒絕輸出在 source 內（同上檔案 + wheel smoke `create-refusal`）。
- verify：離線、機器可讀、拒絕未知 format/schema version、抓 manifest 篡改
  （`test_verify_rejects_tampering_and_unknown_versions`）。
- 還原：只到全新目錄；`../`、絕對路徑、磁碟代號、symlink、重複條目全部拒絕；
  失敗不留半套（`test_restore_rejects_untrusted_archives_and_never_partial_restores`）。
- 升級演練：0002 schema 備份 → verify/restore → 明確 `init` 才 migration →
  歷史可讀；archive 原件再驗仍通過
  （`test_old_schema_backup_restore_then_explicit_upgrade`）。
- 內容保全：run IDs、counts、scores、stages、ranks、reasons、input/config hash、
  baseline binding、idempotency key→run id、owner scope、新/舊 token 行為
  （`test_backup_verify_restore_roundtrip_preserves_history` + wheel smoke backup 段）。

## 4. 效能（synthetic，實測檔：`docs/benchmarks/phase5-macos-arm64.json`）

環境：macOS 26.6 arm64（Apple Silicon）、Python 3.12.12、SQLite 3.50.4、
鎖定 pydantic/sqlalchemy/fastapi/uvicorn/numpy/pandas/matplotlib 版本見檔內
`environment.dependencies`；provider=fixture；seed=deterministic-formula-v1；
量測法與限制（RSS 方法、cold/warm 定義、tracemalloc 不可見 NumPy 等）記於檔內。

| 規模（symbols×sessions） | core+ranking median | e2e scan(force) | render warm(top30) | backup / verify / restore | peak RSS |
| --- | --- | --- | --- | --- | --- |
| 100×504 | 0.028s | 0.62s | 2.33s | 0.20/0.05/0.06s | 205MB |
| 500×504 | 0.201s | 8.55s | 2.71s | 1.40/0.33/0.46s | 240MB |
| **1000×504** | **0.423s（目標 ≤10s）** | 17.29s | 4.16s | 3.29/0.62/0.77s | 316MB |
| 2000×504 | 0.663s | 72.98s | 11.19s | 13.01/5.47/4.98s | 517MB |

解讀（僅就已分段量測的證據）：核心計算遠低於計劃目標；此 synthetic 設定下
已量到的主要耗時為**渲染（matplotlib 圖表）**與 fixture 路徑的取得＋驗證＋
發布（e2e force scan 列）。**fixture/in-memory 的 force scan 不代表 Yahoo
下載耗時**——網絡取得未在本輪 profile，不得把未歸因時間全數說成網絡或
渲染；逐段數字以檔內 raw samples 為準。API slow-provider 段：worker 被可控慢 provider 卡住時
status/results/冪等重放照常服務（submit 0.08s、status median 0.066s、無重複入隊）。
自動化只跑小規模 correctness（`tests/`），絕對秒數不作任何硬門檻。

## 5. 真實來源與人工覆核

- **Live provider 驗收（已執行）**：`docs/acceptance/phase5-live-smoke-2026-09-06.json`
  ——Yahoo EOD_TRIAL、as-of 2026-09-04、3/3 evaluated、1 candidate、cache_only 與
  force 結果一致、exact replay hash 相同、refresh 3/3；provider gate 狀態
  EOD_TRIAL 記錄在案。冇宣稱 SLA；盤中／production 行情維持不放行。
- **人工覆核（工具已交付，標記 pending）**：`qscan scans review-export` 產生候選 +
  固定 seed 對照樣本 CSV（`worth_reviewing/borderline/not_useful` 欄位留空）。
  **沒有實際人工標記，因此不宣稱任何 precision/recall**；歷史 runs 基於修訂後
  cache，非真正 point-in-time 資料，文件已標明。

## 6. 安全

- token 寫入：temp file 先 0600 再 atomic replace（`localauth._write`），含 review。
- backup/restore 對不可信 archive 的防護見上第 3 節與 ADR 0006。
- dependency/security advisory 檢查（**2026-09-06**）：OSV.dev querybatch 對
  uv.lock 全部關鍵套件——0 已知漏洞。`uvx pip-audit` 在本機環境無法建 venv
  （ensurepip SIGABRT），以 OSV API 直接查詢代替。注意：**pip 套件 audit 不等同
  Python 內建 SQLite runtime audit**（見下節 Phase 5.1 更正）。
  本輪（Phase 5.1）零依賴變更。
- 第三方 warnings（pandas_market_calendars/exchange_calendars NumPy DeprecationWarning、
  starlette TestClient deprecation）屬上游套件，記錄於此；不加 blanket ignore、
  不為消警告而升級。

## 7. 產物與授權

- `dist/close_setup_scanner-0.1.0-py3-none-any.whl`、`dist/close_setup_scanner-0.1.0.tar.gz`
  及 `dist/SHA256SUMS.txt`；OpenAPI snapshot `docs/openapi.json`；release notes =
  [CHANGELOG.md](../CHANGELOG.md)。只在本地產生——**無** PyPI 上傳、無 GitHub
  Release、無 tag、無自動部署。
- wheel 內容已掃描：無使用者 DB、價格歷史、token、私人 snapshots、暫存報告。
- **授權未決定**：repository 無 LICENSE 檔、pyproject 無 license 欄位；按原作者
  版權預設處理，不得再分發。行情（Yahoo）使用權與程式碼授權分開；本地免費下載
  不代表多使用者再分發授權。指定法律條款前不明示任何 open-source licence。

## 8. 未完成 / 已知限制（如實列出）

- **Windows/Linux 本機未逐項手跑 Phase 5 新增演練**：截至 `614e7c3` 由 GitHub
  三平台 CI（含 Windows wheel smoke）覆蓋；該 workflow 已於 2026-09-06 因費用移除，
  之後的提交只由本地 gate（macOS）覆蓋——跨平台需於各平台手動
  `uv run python scripts/check.py`，未手跑的組合不宣稱。
- 人工覆核無真人標記（見第 5 節）；review export 是工具，不是結論。
- 排程範例（operations.md）只提供檔案，未在任何使用者機器安裝或啟用。
- 渲染效能受 matplotlib 支配；未做跨平台渲染效能宣稱。
- live smoke 日期為 2026-09-04 session；日後重跑結果可能不同，屬正常。
- 無 installer／GUI／自動更新（計劃內延後項）。

---

# Phase 5.1 更正與追加（2026-09-06，commit 基線 9b65b07）

## SQLite runtime 驗收 — BLOCKER（未解除前不得視為已驗收 release）

- **實際 runtime（本機 macOS arm64）**：uv cpython-3.12.12，`sqlite3.sqlite_version`
  = **3.50.4**。
- **官方來源（當日查核）**：sqlite.org/news.html——「Patch release 3.51.3 fixes
  the WAL-reset bug」；sqlite.org/wal.html#walresetbug——WAL-Reset bug
  （2026-03-03 發現，罕見情況可致資料庫損壞）；修復版本 3.51.3（2026-03-13），
  釋出分支 backport 為 **3.50.7** 與 **3.44.6**。相關分析：Antithesis
  「Breaking the WAL」、Tailscale blog（2026）。
- **判定**：3.50.4 **不在**已修清單——本機 runtime **AFFECTED**。此前 Phase 5
  checklist 寫「SQLite 3.50.4 無已知公告」屬**錯誤宣稱**，已撤回；亦不宣稱
  使用者既有 DB 已損壞（該 bug 是罕見觸發條件，非必然損壞）。
- **修復路徑（已實測）**：`brew install python@3.12` → Python 3.12.14 +
  SQLite **3.53.4**（≥3.51.3，已含修復）。本輪最終驗收 gate 全部以該 runtime
  執行（見下）。各平台部署前須確認其 Python build 的
  `sqlite3.sqlite_version` 屬 3.51.3+ 或 3.50.7/3.44.6 backport；
  `qscan doctor` 現會列出 runtime 與 advisory 判定（3.50.9 等同分支非
  backport 版本亦報 AFFECTED，不用「所有 ≥3.50.7 都安全」的錯誤判斷）。
- **狀態：BLOCKER**——uv 預設下載的 cpython-3.12.12 仍帶 3.50.4；在项目鎖定
  runtime 切換到已修 build（例如 UV_PYTHON 指向帶新 SQLite 的 Python 3.12）
  之前，本發布候選不得標為 SQLite-runtime 已驗收。

## Phase 5.1 修正範圍（詳見 CHANGELOG）

- 排程語義：session 由市場日曆服務解析（新 `qscan sessions` /
  `GET /api/v1/sessions/current`）；去重身份 = watchlist UUID + resolved
  session + revision + provider + attempt；--force 遞增 attempt（同意圖重試
  沿用同一 key）；FAILED 維持 exit 1；state file 有專屬 lock + 原子寫入；
  401/403/409 與 429/timeout 分流；HTTP 已接受後永不退回 CLI。
- 備份加固：所有 entry（含 manifest）讀取前有獨立上限；zip 與內層 gzip 解壓
  邊讀邊計數；entry 集合必須等於 manifest 宣告（settings allowlist、digest
  不重複、未引用內容拒絕）；runs_by_state/unfinished counts 驗證；snapshot
  以支援 schema 解碼驗證（hash 正確但格式不支援仍拒絕）；restore 對同一開啟
  的 archive 先重驗再解壓，claim-then-rename 發布，唯一 staging 目錄。
- 離線 gate：tests/conftest.py 以 socket guard 硬性阻擋非 loopback 連線；
  排程測試以兩個固定時鐘完整流程執行，不依賴真實今日；standalone CLI 測試
  明確 --provider fixture（不意外使用 Yahoo）。

## 本輪實測（2026-09-06，macOS arm64）

- 安全 runtime（Python 3.12.14 + SQLite 3.53.4）+ 原鎖定依賴：
  `scripts/check.py` 全套通過（lint/format/mypy/offline tests/OpenAPI
  check/build/wheel smoke）。
- 回歸證據：5.1 新增 scheduler/backup regression tests 中，備份 bounds/
  consistency/format 類在 9b65b07 舊實作上以 git stash 實證失敗、修正後通過；
  scheduler 舊語義（本機日期去重、FAILED→exit 0、force 同 key）由重寫後的
  測試直接釘住。
- 備份→verify→restore→report/replay 與 scheduler CLI/API 路徑均重跑通過。
- Windows/Linux：**本輪未跑**（無 hosted CI）；須於各平台手動
  `uv run python scripts/check.py` 後方可宣稱跨平台。

---

# Phase 6A 交付與實測（2026-09-06，macOS arm64，Python 3.12.14，SQLite 3.53.4）

基線：`b461fd0`（Phase 5.1）。範圍＝A 修正（排程 pending 意圖／wrapper 錯誤契約／
備份收尾／驗證可信度）＋B 薄型本地覆核 UI。詳細範圍見
[development-plan](development-plan.md) Phase 6A 節；操作細節見
[operations](operations.md) Phase 6A 節。

## A 修正回歸證據

| 根因 | Regression（新測試） |
| --- | --- |
| wrapper 先解析最新 session/revision 先重建 key，舊 pending 意圖被覆寫 | `test_old_session_pending_resumes_then_new_session_intent`（SUBMITTED＋新 session 重啟 → 原 key/scan ID 恢復＋新 session 正常新意圖，共 2 runs） |
| 未完成 force intent 退回 attempt 0（第二條 key） | `test_force_intent_pending_keeps_attempt_on_plain_retry`（attempt 1 SUBMITTED＋普通重試 → 仍 attempt 1 原 key，1 run） |
| lost acknowledgement 後跨日期／改名單擋住恢復 | `test_lost_ack_survives_rename_and_revision_bump_across_dates`（改名＋revision bump＋跨 session → 以已保存 ID 恢復） |
| pending＋server unreachable 誤啟動 CLI | `test_pending_api_intent_with_server_down_never_starts_cli`（exit 4；CLI sentinel 未執行；state 不變） |
| 兩個 wrapper 並發覆寫／重複建立 | `test_concurrent_api_wrappers_single_run`（並發 → 1 run、state 一致） |
| `--force` 時 pending 行為未定義 | `test_force_while_pending_is_refused_not_overwritten`（exit 4、舊意圖原封不動、之後正常完成） |
| polling timeout 寫成終局 FAILED | `test_poll_timeout_keeps_pending_then_resumes`（timeout → SUBMITTED 保留 → 恢復完成，1 run） |
| wrapper flag 與 server provider 不一致靜默去重 | `test_mismatched_provider_flag_is_a_configuration_error`（exit 2，無意圖寫入；server 由 `/health/live` 提供身份） |
| stdout 無 JSON／有 traceback | `test_stdout_contract_single_json_no_traceback`（成功單一 JSON；token 檔損壞 → exit 2 envelope、無 traceback/token） |
| CLI 子程序只查 JSON 唔查 exit code／形狀 | `test_cli_subprocess_checked_for_exit_code_and_shape`（envelope≠run document；exit 0 envelope→busy；FAILED 文件→exit 1） |
| 備份 DB entry 整份讀入記憶體 | `test_database_entry_streams_with_tiny_cap`（串流落碟＋增量 hash；cap 邊讀邊擋） |
| manifest 唔計入總讀取預算 | `test_manifest_counts_against_total_budget` |
| verify 接受正式 reader 唔收嘅 snapshot | `test_verify_accepts_only_what_the_formal_reader_accepts`＋`..._reader_clean`（noncanonical-but-valid JSON 兩邊都拒；canonical 全通路） |
| manifest 多宣告／run-snapshot 不符 | `test_run_snapshot_mismatch_still_rejected` |
| `exists()`＋可覆蓋 rename 冒稱原子拒絕覆蓋 | `test_existing_output_never_clobbered`＋`test_publish_fallback_without_hardlinks_never_overwrites`（O_EXCL fallback；模擬無 hardlink FS 同競態） |
| conftest 只 patch socket.connect；宣稱能攔 native HTTP | conftest＋`tests/_offline_guard/sitecustomize.py`：connect/connect_ex＋stub curl_cffi/pycurl；docstring 如實聲明覆蓋範圍（唔宣稱攔截所有 native library） |
| 完整 gate 唔會因受影響 SQLite runtime 失敗 | `scripts/check.py` preflight：本機 host runtime 3.53.4 OK；**wheel-smoke venv 實測抓出 uv managed CPython 3.12.12＋SQLite 3.50.4 AFFECTED 並 FAIL**（即 5.1 記錄的 BLOCKER 其實一直存在於 wheel smoke 的 venv），已改為釘住 gate 自身已驗證 interpreter（3.53.4 OK）。`qscan doctor` 新增實際 `python_runtime` |

## B 交付與實測

- **Reviews**（migration 0004，向後相容，`test_old_schema_upgrade_adds_reviews_table`）：
  `test_phase6a_reviews.py` 11 條——保存／清單／revision 衝突 409（盲寫都拒）、
  只有 evaluated 可標記、QUEUED/RUNNING 不可標記、replay 由零開始＋results 不變、
  跨 principal 讀寫 404、**備份→還原 round-trip 保留標記**、review-export CSV
  併入標記＋匯出時間注明。OpenAPI snapshot 已重新生成並通過 `--check`。
- **UI**（`web/`，React 19＋TS＋Vite 7，單一 lockfile）：`npm run typecheck /
  lint / test`（17 tests：client envelope/401/429 同 key 有界退避、pending
  round-trip＋StrictMode claim、chart null-gap／常數序列／空資料、review panel
  未 2xx 唔顯示已保存／409 保留輸入）＋ `npm run build` →
  `src/qscan/interfaces/web/dist` 進 wheel（`unzip -l` 含 3 個 asset 條目）。
- **Origin/UI mount**：同 origin POST 通過、`https://evil.example`/`null` 403、
  allowlist 由受信任 server 配置形成；`/ui/` 匿名 shell、未知 asset 404、
  `/api` 404 係 JSON envelope（無 token 時 401 不洩漏路由）、業務 API 認證不變
  （`test_same_origin_...`、`test_ui_mount_...`、`test_ui_missing_...`）。
- **Installed-wheel UI smoke**（repo 外 venv 安裝 wheel）：`/ui/` 匿名 200＋
  `/api` 404 JSON＋業務 API 401——已併入 `scripts/wheel_smoke.py` 並通過。
- **瀏覽器 E2E**（真實本機 `qscan serve`＋fixture demo＋headless Chrome，
  瀏覽器 DNS 全部 poison 至 NOTFOUND 只容 loopback，合成 token）：
  `web/e2e/app.spec.ts` 4 條全過——連線→建立名單→提交→terminal→查看候選→
  開圖及原因→保存覆核（revision 1）→reload＋重新認證→標記仍在→**該名單只有
  1 個 run**（reload 無重建掃描）。
- **視覺驗收**：`web/e2e/visual.mjs`（真實瀏覽器渲染，非 regex/PNG 存在性）——
  桌面 1280px 與窄屏 390px 截圖（`web/e2e/shots/`）經人工檢查：長名稱／中文／
  空值／錯誤訊息正常、表格局部捲動、圖表隨寬度縮放、兩個寬度 document 橫向
  overflow = 0px、鍵盤 Enter 選列生效。
- **完整 gate**：`uv run python scripts/check.py` 全套通過（159.3s；步驟見上
  第 1 節格式：runtime preflight → ruff/format/mypy/pytest 277+11 條 →
  OpenAPI → frontend dev gate → frontend release build → uv build →
  installed-wheel smoke，wheel 內 runtime identity 3.53.4 OK）。

## Phase 6A 未完成 / 限制（如實列出）

- **真人覆核標記仍 pending**（5 節同）；UI 標記功能已交付並有測試，但無真人
  標記資料，唔宣稱 precision/recall。
- **Windows/Linux 未跑本輪 gate**（無 hosted CI）；frontend build 與 E2E 只在
  macOS（Chrome）驗證。其他平台需手動 `uv run python scripts/check.py`＋
  `cd web && npm run test:e2e`。
- E2E 用系統 Chrome（`channel: chrome`）；Playwright CDN 下載 Chromium 失敗
  （網絡限制），唔影響「真實瀏覽器」宣稱，但 CI 化需先解決 browser 供應。
- wheel-smoke venv 改為釘住 gate interpreter：各平台跑 gate 前須確認其
  Python build 的 SQLite 屬已修版本（gate 會自行失敗如實反映）。
- UI 無手機遠端連線宣稱：僅 loopback 單人本地使用。
