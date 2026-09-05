# Close-only Setup Scanner — 開發計劃

> 獨立 Python 核心 + CLI + HTTP API；為日後 App 保留清晰介面，但第一版不做完整 App 平台。

| 項目 | 內容 |
| --- | --- |
| 文件版本 | 0.1 |
| 設計日期 | 2026-09-05 |
| 狀態 | 開發基線；本文件不是已完成的實作或實測報告 |
| 專案暫名 | `close-setup-scanner` |
| Python package / CLI 暫名 | `qscan`；未表示已註冊同名套件 |
| 第一版策略 | Qullamaggie-style breakout 候選初篩 |
| 執行環境 | macOS、Windows、Linux |
| 與其他專案關係 | 獨立專案，不依賴 invest-diary、Dify 或 n8n |

## 1. 產品目標與範圍

### 1.1 要解決的問題

使用者提供股票名單，程式在完整交易時段結束後，利用歷史每日收市價，找出「曾有升勢、正在整理、收市價收窄或接近整理區上沿」的候選，減少人手逐張圖覆核的工作。

輸出是**人工覆核優先次序**，不是買入指令、完美形態認證或成功率預測。原策略包括升浪、整理及突破等觀察；本專案只取其中可用 close-only 近似的部分，不聲稱完整重現原策略。[^qullamaggie]

### 1.2 已確認的硬性限制

| 限制 | 實作要求 |
| --- | --- |
| 不使用 AI | 不接 LLM、embedding、影像辨識或模型訓練；解釋文字由規則產生 |
| 只使用收市價 | 策略只能讀取交易日與標準化 Close；不使用 Open、High、Low、Volume、新聞或財報 |
| 免費數據起步 | 第一版採 Yahoo / yfinance；不要求付費 API key |
| 初篩而非交易系統 | 不做下單、倉位、止損、收益預測或原策略績效回測 |
| 使用自訂 list | 不自動抓全市場或歷史指數成分股，不硬湊 Top N 候選 |
| 跨平台 | 同一套 Python source 在三個作業系統運行；Docker 不是必要條件 |
| 日後可能做 App | CLI、API 共用 application service；App 不直接讀 SQLite 或呼叫 Yahoo |
| 保持簡單 | 一個 repository、一套核心、一個本地資料庫；不做微服務或 workflow 平台 |

日期、交易所、時區、幣別及拆股事件可作**資料校驗／標準化 metadata**，但不得進入策略評分。數據供應商一次回傳 OHLCV，不代表核心可以使用其他價格欄位。

### 1.3 V1 邊界

第一版支援使用者指定的**美股日線名單**。股票與明確識別的 ETF 可使用同一運算管線，但預設參數以成長股初篩為設計對象；不替 ETF 調整門檻，也不保證所有資產適用。其他市場、期權、期貨及加密資產先拒絕或列為不支援，不默默套用美股交易日曆。

V1 交付包含：可離線運算的核心、價格快取、CLI、JSON／CSV／HTML 報告、可查詢的歷史掃描結果，以及有持久化任務狀態的本地 HTTP API。

V1 不包含：GUI、手機 App、會員註冊、訂閱收費、完整多租戶身份系統、推送通知、EP／parabolic short 策略、策略 DSL、PostgreSQL 或分散式 queue。

---

## 2. 技術決策

以下為本專案的工程選擇，不代表相關技術的唯一正確用法。

| 層次 | 選擇 | 用途／限制 |
| --- | --- | --- |
| Runtime | Python 3.12 作首個驗收版本 | Phase 0 驗證相依套件可安裝；其他 minor version 未測試前不列為保證支援 |
| 專案管理 | `pyproject.toml` + `uv.lock` | 提交 lockfile；不使用未鎖定的隨機最新版部署 |
| 計算 | NumPy + pandas | 使用一般浮點向量運算；不加入 TA-Lib 或 GPU 依賴 |
| CLI | Typer | 只處理參數、呈現及 exit code |
| API | FastAPI + Pydantic v2 + Uvicorn | REST + JSON + OpenAPI；DTO 不直接暴露 ORM model |
| 儲存 | SQLite + SQLAlchemy 2.x + Alembic | 短交易、版本化 migration；不假設未來轉 PostgreSQL 零成本 |
| 價格來源 | yfinance adapter | 唯一線上 provider；另有 fixture provider 供離線測試 |
| 日期／時區 | `pandas_market_calendars` + `zoneinfo` + `tzdata` | 美股 session、假期、提早收市及夏令時間 |
| 路徑／執行鎖 | `pathlib` + `platformdirs` + `filelock` | 使用者可寫目錄、跨平台路徑及單一執行器鎖 |
| 報告 | Jinja2 + matplotlib | 靜態 HTML；圖表用 non-interactive backend，不依賴 display server |
| 品質工具 | pytest、HTTPX、Ruff、mypy | 單元／整合／API 合約測試及靜態檢查 |

`uv.lock` 用於保存跨平台解析結果；是否真的能在目標 OS／CPU 安裝，仍須由 CI 及 smoke test 驗證。[^uv] FastAPI 可產生 OpenAPI 文件，適合讓未來 App 依照明確合約接入。[^fastapi-features]

Windows 不一定具備 IANA 時區資料，故明確依賴 `tzdata`，不能只依賴開發者的 macOS 環境。[^zoneinfo]

---

## 3. 架構：一個核心，多個入口

```text
CLI / 本地排程 ─────────────┐
                           │
未來 App → HTTP API ────────┼→ Application Services
                           │          │
可選 n8n → HTTP API ────────┘          ├→ MarketDataProvider → Yahoo / fixture
                                      ├→ Repository → SQLite / input snapshots
                                      ├→ Pure Scanner Core
                                      └→ Report Renderer
```

### 3.1 依賴方向

`domain` 定義資料結構、錯誤及 enum；`core` 做純運算；`application` 組合用例；`adapters` 對接外部資料及儲存；`interfaces` 提供 CLI／API。

**禁止：** core import FastAPI、Typer、SQLAlchemy 或 yfinance；API route 自己計分；CLI 另寫一套策略；scanner 透過呼叫自身 HTTP API 才能運作。

不建立通用 plugin framework、泛型 repository 大框架或自訂 DI container。只在實際邊界使用小型 `Protocol` 與 constructor injection。

### 3.2 核心合約

```python
# Contract sketch only; all code comments and docstrings should be in English.


def analyze_symbol(
    series: CloseSeries,
    rules: RuleConfig,
    context: ScanContext,
) -> SymbolAnalysis:
    """Analyze validated, session-aligned closes without I/O."""


def rank_candidates(
    analyses: list[SymbolAnalysis],
    rules: RuleConfig,
) -> list[RankedCandidate]:
    """Apply stable ranking without changing eligibility."""
```

`CloseSeries` 只包含 `instrument_id`、有序 session dates、Close array 及 price basis。呼叫者先裁切至 `as_of_session`；核心亦檢查不得存在其後的價格。

核心禁止讀取系統時間、環境變數、網絡、DB 或檔案。相同輸入快照、規則、engine version 應產生相同分類、分數及穩定排序；跨平台浮點特徵按明確容差比較，不要求二進位逐 bit 相同。

### 3.3 Application Services

只需幾個具體服務：`WatchlistService`、`MarketDataService`、`ScanService`、`ScanQueryService`、`ReportService`。CLI 同 API 使用同一套服務，不複製業務邏輯。

第一版採同步 provider／運算介面。API 透過獨立執行 thread 運行 scan，不能在 async route 內直接阻塞 event loop。

### 3.4 目錄結構

```text
close-setup-scanner/
├── pyproject.toml
├── uv.lock
├── .python-version
├── README.md
├── config/
│   ├── settings.example.toml
│   └── rules/breakout-v1.toml
├── src/qscan/
│   ├── domain/          # Models, enums, typed errors, protocols
│   ├── core/            # Features, windows, eligibility, scoring, ranking
│   ├── application/     # Use cases, date resolution, scan execution, comparison
│   ├── adapters/
│   │   ├── market_data/ # Yahoo adapter and fixture provider
│   │   ├── persistence/# SQLAlchemy models and repositories
│   │   └── reports/     # HTML, CSV, JSON and chart rendering
│   ├── interfaces/
│   │   ├── cli/
│   │   └── api/         # Routes, schemas, auth dependency, error mapping
│   ├── worker.py        # Serial persisted-job executor
│   ├── settings.py
│   └── bootstrap.py     # Explicit dependency wiring
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── fixtures/
│   └── performance/
├── docs/
│   ├── development-plan.md
│   ├── rules.md
│   ├── api.md
│   ├── data-quality.md
│   ├── operations.md
│   └── adr/
└── AGENTS.md            # agent 規則（GitHub Actions 已移除，改本地 scripts/check.py）
```

運行資料不寫入 package 安裝目錄，也不提交 Git。預設用 `platformdirs` 的使用者資料目錄，並提供 `--data-dir`／`QSCAN_DATA_DIR` 覆寫。

---

## 4. 價格資料與交易日語義

### 4.1 價格口徑

固定 V1 `price_basis = split_adjusted_close`：目標是拆股調整、非股息總回報口徑的收市價。不可把 `Adj Close` 當作「只調整拆股」；Yahoo 對 adjusted close 的說明亦包括股息及／或資本收益分派。[^yahoo-history]

Yahoo adapter 明確設定：

```text
interval = "1d"
auto_adjust = false
back_adjust = false
repair = false
prepost = false
```

選用回傳的 `Close`，但欄位名稱不是口徑驗證的替代品。**Phase 0 必須核對拆股與除息例子，確認安裝版本的行為，並留下 fixture／檢查紀錄。** 不自行再次套用拆股因子，以免 double-adjust。

yfinance 有自動調整選項，而且 `end` 是 exclusive；adapter 必須明確處理，不能依賴預設值或把目標日漏掉。[^yfinance-download]

`repair=False` 是有意決定：yfinance 的 repair 部分情況會以較細時間級別重建價格。V1 不使用這種隱含盤中資料修復，也不默默修改異常值。[^yfinance-repair]

### 4.2 截止日

資料庫分開保存：

| 欄位 | 意義 |
| --- | --- |
| `as_of_session` | 掃描所屬美股交易日，`YYYY-MM-DD` |
| `requested_at` / `started_at` / `finished_at` | UTC timestamp，ISO 8601 |
| `source_fetched_at` | 供應商資料取得時間 |
| `reference_session` | 整理窗口及前段動量的截止交易日，V1 為 `as_of_session` 的上一個 session |

未傳入 `as_of_session` 時，依市場日曆取「預定收市時間 + 可配置資料緩衝」已過的最近 session；初始緩衝 30 分鐘是工程預設，不是供應商更新保證。

明確指定的日期若不是交易日或尚未完成，回傳 `INVALID_AS_OF_SESSION`／`SESSION_NOT_COMPLETE`，**不默默改成另一日**。例如命令範例使用 `2026-09-04`，不把星期六 `2026-09-05` 當成美股 session。

API 最後一行不一定已完成；必須按上述規則裁切。下載不到目標 session，不能整份報告偷偷退回前一天。

市場日曆套件包含假期及提早收市規則，但日曆是隨版本發佈，不是即時向交易所查詢。記錄版本，提供少量明確的人工 calendar override，並測試異常休市。[^calendar]

### 4.3 名單與 symbol

TXT 每行一個 ticker，可有空白行及以 `#` 起首的註解；CSV 要有 `symbol` 欄，可有 `exchange` 欄。輸入統一 UTF-8，接受 BOM；trim、合理大小寫正規化及去重。

使用穩定 `instrument_id` 作內部主鍵，另存 display symbol、provider symbol、exchange、currency、instrument type。例：類股 symbol 的 `.`／`-` 差異只在 adapter 中以已測試規則處理，不能全域盲目 replace。

限制單次名單最多 2,000 個去重後 symbol，輸入 body 不超過 1 MB。這是 V1 防護預設，可由 server config 調整，不是性能實測結論。

### 4.4 快取與更新

初次取得約兩年日線，最多保留本次分析／圖表需要的最近 504 個 sessions；新上市股票不足此長度不必下載不存在的資料。

| 模式 | 行為 |
| --- | --- |
| `auto` | 復用符合截止日與品質要求的快取；缺少資料則更新 |
| `cache_only` | 完全不連線；資料不足就清楚列出，不隱式 fetch |
| `force` | 重新取得所需歷史；用於覆核、修訂或供應商問題 |

日常增量更新重取最近 10 個 sessions 作交疊校驗。若偵測拆股事件、價格 basis 改變或交疊期既有 Close 的實質修訂，重新取得該 symbol 的完整保留範圍並原子替換，不拼接不同調整口徑。另以可配置週期，例如 30 日，執行完整歷史覆核。

每個 run 記錄是否使用快取、fetch 錯誤及歷史修訂；本次重取失敗但同一截止日的本地資料仍有效，可以繼續評估，但需保留 refresh warning。

下載採 bounded concurrency，初始上限 2；批次大小及 timeout 經 provider spike 確認。對暫時性錯誤有限重試，使用 backoff／jitter，尊重 rate limit；不得無限重試、輪換 IP 規避限制或每次重跑整份 list。網絡等待不持有 SQLite write transaction。

### 4.5 品質狀態與排除原因

以下狀態與「沒有 setup」必須分開：

| 情況 | 處理 |
| --- | --- |
| `NO_DATA` | 供應商無有效資料；不擅自推斷為已退市 |
| `STALE_DATA` | 沒有目標 session 的 Close；不能用昨日價格冒充今日 |
| `MISSING_REQUIRED_SESSION` | 某必需窗口有缺口；不 forward-fill、不壓縮為連續 sessions |
| `INVALID_CLOSE` | NaN、infinity、零或負值；不進入計算 |
| `CONFLICTING_DUPLICATE` | 同 symbol／session 有矛盾數值；不能隨機取一行 |
| `INSUFFICIENT_HISTORY` | 不足最低歷史或所有觀察窗口均不可計算 |
| `UNSUPPORTED_INSTRUMENT` | 非支援市場／資產，或無法安全確認市場歸屬 |
| `SUSPICIOUS_FLAT_SERIES` | 例如連續 20 sessions 同價；隔離覆核，不給「完美收窄」分數 |
| `ADJUSTMENT_REVIEW_REQUIRED` | 嚴重跳變且調整口徑未確認；重取／覆核，不自行猜測修正 |

缺資料不能當作真實橫行；close-only 亦不能可靠分辨停牌、極低流動性及部分資料錯誤，因此異常旗標只是覆核理由，不是事實判決。

---

## 5. V1 篩選規則：可解釋、可測試、可修改

> 本節所有數值均是**未驗證的初始設計參數**，不是 Qullamaggie 官方標準，也不是交易優勢的證據。先形成可運行基線，再用獨立樣本調整。

### 5.1 統一索引：先形成參考區，再觀察今日

所有 `n` 日均指交易 sessions，不是自然日。令最新完整 session 為 `t`，上一個 session 為 `u = t - 1`。

對 `n ∈ {10, 20, 40}`：

```text
s = u - n + 1
W_n = [C_s, ..., C_u]                 # n 個 Close；不包括 C_t
close_resistance_n = max(W_n)
close_support_n = min(W_n)
```

整理、收窄及前段動量使用截至 `u` 的資料；最新 `C_t` 用於均線位置、距離及收市升穿分類。這是 V1 的有意簡化：當日才形成的新整理特徵，最多延遲一個 session 才進入參考窗口。

**禁止把 `C_t` 放入阻力計算，再判斷 `C_t` 是否升穿該阻力。** 也不能先根據今天的急升重選一段事前不合格的 base，再聲稱昨天已存在 setup。

### 5.2 特徵定義

| 特徵 | 定義 |
| --- | --- |
| `return_k` | `C_u / C_(u-k) - 1`，`k = 21, 63, 126`；資料不足為 null |
| `prior_move_n` | `C_s / min(C_(s-63), ..., C_(s-1)) - 1`；低點必須早於窗口起點 |
| `sma_m` | 截至 `t` 最近 `m` 個 Close 平均，`m = 10, 20, 50` |
| `sma20_slope_5` | `SMA20_t / SMA20_(t-5) - 1` |
| `base_close_range_n` | `max(W_n) / min(W_n) - 1` |
| `base_net_return_n` | `C_u / C_s - 1` |
| `higher_close_lows_ratio_n` | `min(窗口後半) / min(窗口前半)`；只表示最低收市價比較 |
| `close_range_5` | `max(C_(u-4)..C_u) / min(C_(u-4)..C_u) - 1` |
| `daily_log_return_i` | `log(C_i / C_(i-1))` |
| `contraction_ratio` | `std(r_(u-4)..r_u) / std(r_(u-24)..r_(u-5))`，兩組 return observations 不重疊，固定 `ddof=0` |
| `distance_to_resistance_n` | `C_t / close_resistance_n - 1` |
| `distance_to_sma20` | `C_t / SMA20_t - 1` |

前後半窗口長度相等，因 V1 只用偶數窗口。`prior_move_n` 是簡單的前段升幅 proxy，不代表已識別真正 swing low 或完整 impulse leg。

`contraction_ratio` 的 20 日參考期可能早於短 base 的起點；報告應顯示它是較長背景的波動比較，不暗示全部資料都位於 base 之內。

波動分母小於數值容差 `1e-12` 時，輸出 null 及 `UNDEFINED_CONTRACTION`，不當作 0 或無限好。不得用「5 日區間小於包含它的 20 日區間」作單獨收窄證據。

### 5.3 歷史長度

最低要求 80 個連續完整 sessions；更長窗口另作自己的資料完整性檢查。按上述公式，包含最新 `t` 在內，10／20／40 日窗口的前段升幅分別至少需要 74／84／104 個 Close，因此 80 日歷史只評估可用的 10 日窗口。

`return_126` 在包含 `t` 的至少 128 個 Close 才可計算。不存在該值時不填 0，不直接踢走股票；動量 gate 可由其他有效指標通過，評分使用有效項目的最大值。

缺少額外歷史不必令整隻股票失效，但該指標／窗口必須列出 unavailable 原因。必需的近期 80 sessions 有缺口則不作正常評估。

### 5.4 入池條件

每個窗口獨立通過以下條件；不可用 10 日窗口的動量、40 日窗口的區間及 20 日窗口的阻力拼成一個不存在的形態。

```text
momentum_ok =
    return_63 >= 0.20
    OR return_126 >= 0.30
    OR prior_move_n >= 0.25

base_ok =
    base_close_range_n <= 0.25
    AND abs(base_net_return_n) <= 0.15

current_structure_not_broken =
    C_t >= close_support_n * 0.95

eligible_window =
    required_data_valid
    AND momentum_ok
    AND base_ok
    AND current_structure_not_broken
```

OR 條件只看有效數值；null 不算通過。整理區間及淨變幅限制是寬鬆的形態骨架，其餘特徵主要用於排序，不逐項硬性 AND。

最低股價、成交額及成交量不是 V1 入池條件。流動性始終標示 `NOT_EVALUATED`。

### 5.5 評分基線：五項合計 100 分

評分只在 eligible window 上執行。各級採互斥分段，取符合的最高級；不把同一指標各級累加。

| 維度 | 上限 | 初始分段 |
| --- | ---: | --- |
| 前段動量 | 25 | `return_63`：≥20% / 40% / 60% → 10 / 18 / 25；`return_126`：≥30% / 60% / 100% → 10 / 18 / 25；`prior_move_n`：≥25% / 50% / 75% → 10 / 18 / 25。三者取最大值 |
| 趨勢 | 15 | `C_t >= SMA20_t` +5；`C_t >= SMA50_t` +5；`sma20_slope_5 > 0` +5 |
| 整理結構 | 25 | 區間 ≤8% / 15% / 25% → 15 / 10 / 5；淨變幅絕對值 ≤5% / 10% / 15% → 5 / 3 / 1；後半最低 Close 比率 ≥1.00 / 0.95 → 5 / 2 |
| 收窄 | 20 | `contraction_ratio` ≤0.60 / 0.80 / 1.00 → 10 / 7 / 3；`close_range_5` ≤3% / 5% / 8% → 10 / 7 / 3；其他或 unavailable 部分為 0 分並註明 |
| 接近參考線 | 15 | 阻力距離絕對值 ≤2% / 5% / 10% → 15 / 10 / 5；其他 0 |

所有分段與權重放在經驗證的 `RuleConfig`，產生 canonical config hash。禁止執行使用者輸入的 expression、`eval` 或自訂 Python code。

門檻比較採固定微小容差 `1e-12`，不先將百分比四捨五入才比較。無效／缺失資料的數學值仍為 null；「不給某部分分數」不等同把缺失價格填成 0。

**分數 80 不代表成功率 80%。** V1 不用總分作另一道必需 gate；所有合格候選都保存，報告可依人工覆核容量只顯示前 N 個。

### 5.6 狀態分類與多窗口

對每個 eligible window，依下列先後順序分類：

| 順序 | 條件 | `stage` |
| ---: | --- | --- |
| 1 | 阻力上方距離 >10%，或高於 SMA20 >20% | `EXTENDED` |
| 2 | 收市價高於截至 `u` 的阻力 >0.2% | `CLOSE_BREAK_ABOVE` |
| 3 | 距離阻力介於 -5% 至 +0.2%，包括端點 | `NEAR_CLOSE_RESISTANCE` |
| 4 | 其餘 eligible window | `FORMING` |

另外保留 `is_close_break` boolean，即使分類為 `EXTENDED` 仍能知道它有否升穿參考線。0.2% 是可調雜訊緩衝，不是日內突破確認。

`CLOSE_BREAK_ABOVE` 只表示高於昨日可計算的參考區上沿，**不保證是完整長期 base 的第一次突破**。V1 不實作自動 swing segmentation 或長期 base identity。

同一 symbol 只輸出一筆主結果：先在非 `EXTENDED` 合格窗口中選最高分；全部 extended 才從該組選。分數同分時依固定窗口次序 `20 → 10 → 40`；其餘窗口存入 `alternative_windows`，不跨窗口累加分數。

沒有 eligible window 時，`evaluation_status = EVALUATED`、`is_candidate = false`、`stage = null`，附上 gate 失敗原因。資料無法評估則使用另一個狀態，不能混為 `NO_SETUP`。

### 5.7 排名與原因

全體候選按 `score DESC, instrument_id ASC` 穩定排名。HTML 按四種 stage 分組，同組按相同排序；不因報告顯示上限改變 persisted results。

原因使用穩定 `reason_code` + typed parameters，例如：

```json
{
  "code": "NEAR_CLOSE_RESISTANCE",
  "parameters": {
    "distance": -0.018,
    "reference_close": 100.0,
    "window_sessions": 20
  }
}
```

文字只是呈現層。API 保留 `momentum_as_of`、`reference_session` 等欄位，避免 App 把截至昨日的動量誤標為今日動量。V1 暫不加入 universe percentile／RS benchmark，避免名單改動影響主要分數。

---

## 6. 儲存、快照及每日變化

### 6.1 最小資料模型

| Table | 主要欄位／限制 |
| --- | --- |
| `instruments` | `id`, display/provider symbols, exchange, currency, type；provider symbol + market 唯一 |
| `prices` | `instrument_id`, session, close, provider, price_basis, fetched_at；instrument／session／basis 唯一；V1 不混用不同 provider |
| `watchlists` | UUID `id`, `owner_id`, name, revision, created_at, updated_at；owner + name 唯一 |
| `watchlist_members` | watchlist_id + instrument_id 唯一；保持可重現的名單排序 |
| `scan_runs` | UUID id, owner_id, state, watchlist/config snapshots, as_of_session, engine_version, config_hash, input_hash, idempotency_key, request_hash, private snapshot reference, counts, timings, error；有 key 時 owner + endpoint + key 唯一 |
| `scan_results` | run_id + instrument_id 唯一；evaluation status, candidate flag, stage, score, features, score breakdown, reasons, warnings, alternative windows |

V1 ruleset 來自 server 管理的版本化 TOML 檔，不另外建立遠端任意規則編輯器。每次 run 保存完整解析後 config；同一 ruleset id 的設定改變必須增加 version，不覆寫舊 run。

`owner_id` 由 application context 提供，V1 固定 local principal；所有 watchlist／scan query 仍需帶 owner scope。這只是未來授權邊界，**不是已完成多租戶安全性**，也不是邀請 App 傳入任意 owner_id。

必要索引：`prices(instrument_id, session)`、`scan_runs(owner_id, created_at)`、`scan_runs(state, created_at)`、`scan_results(run_id, is_candidate, score, instrument_id)`。

### 6.2 可重現快照

提交 scan 時固定 watchlist revision、symbol 清單、ruleset version 及 config。開始計算前，將實際採用的日期／Close／metadata 保存為 immutable、壓縮 JSON input snapshot，檔案名稱用內容 hash，不用使用者輸入的路徑。

以暫存檔 + atomic rename 完成 snapshot 後才在 DB 參照；失敗不得把 run 標成成功。孤立暫存檔可由明確 maintenance command 清理，不能在一般讀取 API 隨機刪除。

API 不暴露本機 snapshot path。歷史報告、圖表及 replay 讀取該 run 的 snapshot，不使用已被更新的 prices table 偷偷改寫舊結果。

同一日期因供應商修訂而重跑，建立新 `scan_id`／input hash，舊結果保持不變。保存 run 的同時保存 snapshot；V1 不預設自動刪除，清理功能須明確告知會喪失哪些 replay 能力。

### 6.3 歷史 replay 限制

把今天下載的修訂後歷史裁切到過去，只能叫 `revised_history_replay`，不能叫真正 point-in-time 回測。當時的供應商版本、已退市標的及歷史 universe 未必存在；現在名單回看過去亦有存活者／選樣偏差。

已自行保存的當日 input snapshot 可做可重現 replay。沒有保存過的舊快照，不能只靠一個 hash 聲稱已經可以完整重建。

### 6.4 每日變化

比較同一 watchlist、相同規則／價格 basis 的相鄰有效交易日，僅對兩日都有有效 evaluation 的相同 instrument 判斷：

`NEW_CANDIDATE`、`STAGE_CHANGED`、`SCORE_CHANGED`、`DROPPED_CANDIDATE`。

首次執行顯示 `NO_BASELINE`，不把所有項目當作新信號。名單新增／移除另標示 `UNIVERSE_CHANGED`；今天缺價、資料隔離或歷史不足用 `COMPARISON_UNAVAILABLE`，不當作形態惡化。

同一日有多個 run，只取本次 scan 開始前已完成且可比較的 run，按 finished_at DESC、UUID DESC 選取，保存 previous_run_id 與 comparison semantic version。replay 不参与 baseline；比較結果隨 run publication 保存，後來補跑不改寫。legacy 缺 binding 明示 unavailable；細節见 data-quality.md。不同 config hash 或非相鄰 sessions 不顯示「一日變化」。主要觀察窗口改變需附 `WINDOW_CHANGED`，不把分數變動單純解讀為價格變動。若歷史修訂涉及上個 run 的分析區間，附 `DATA_REVISION_DIFF`；不把差異全歸因於新一天的價格，也不重寫舊 baseline。

---

## 7. CLI 合約

Phase 3 已實作以下離線 CLI（serve 留待 Phase 4）；可直接執行的 demo 與來源限制見 README。

```bash
uv sync --locked
uv run qscan init

uv run qscan watchlist import --name us-growth --file watchlist.txt
uv run qscan watchlist list

uv run qscan data refresh --watchlist us-growth
uv run qscan scan --watchlist us-growth --ruleset breakout-v1
uv run qscan scan --watchlist us-growth --as-of 2026-09-04 --data-mode cache_only

uv run qscan scans list
uv run qscan scans show <scan-id> --format json
uv run qscan report <scan-id> --format html --top 30 --output ./output
uv run qscan report <scan-id> --format csv --output ./output
uv run qscan replay <scan-id>

uv run qscan doctor
```

Global `--data-dir` 放在 subcommand 前，優先於 QSCAN_DATA_DIR，且必須是絕對路徑。`watchlist import` 遇到同名 list 不默默覆蓋；須使用 `--replace`，並提升 revision。也可使用 UUID，CLI name 只作方便使用的別名。

`scan` 預設同步等候，仍使用同一套持久化 run／executor 邏輯；執行摘要輸出 scan ID、實際 session、成功評估／排除／錯誤數、候選數及報告位置。JSON 結果只寫 stdout；log／progress 寫 stderr，方便 pipe。

| Exit code | 意義 |
| ---: | --- |
| 0 | 成功；零候選也是成功 |
| 1 | 不可恢復的執行失敗，或沒有任何標的可以有效評估 |
| 2 | 輸入、設定、交易日或權限驗證失敗 |
| 3 | 部分成功；有結果，但部分標的因資料／下載錯誤未能評估 |
| 4 | 執行器已由其他本地程序持有；稍後重跑（API 未實作） |

`doctor` 檢查目錄權限、DB schema、設定、lock 狀態與時區資料。網絡／provider 測試需明確 `--online`，避免診斷命令預設產生外部請求。

---

## 8. HTTP API：App 可接入的穩定邊界

### 8.1 原則

API prefix 固定 `/api/v1`；使用 UUID resource ID，不以 ticker、檔案名稱或 workflow execution ID 代替產品 ID。

日期為 `YYYY-MM-DD`，timestamp 為 UTC ISO 8601。價格及比率傳 JSON number，百分比一律用小數，例如 `0.20 = 20%`、`-0.018 = -1.8%`。缺失數值為 null，不得輸出 NaN／Infinity。

回傳機器可讀 `code`／enum，不要求 App 解析中文訊息。前端不重新計算 gate、stage 或 score；OpenAPI 納入版本控制／合約測試。V1 採 polling，不做 WebSocket。

### 8.2 Endpoints

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/health/live` | 程序存活；不回傳內部路徑／機密 |
| GET | `/health/ready` | DB／executor 可用；不依賴 Yahoo 當下是否可連線 |
| GET / POST | `/api/v1/watchlists` | 名單列表／建立 |
| GET / PATCH / DELETE | `/api/v1/watchlists/{id}` | 讀取／修改名稱／移除名單；歷史 run 保留 snapshot |
| PUT | `/api/v1/watchlists/{id}/symbols` | 明確取代整份名單，revision optimistic concurrency |
| GET | `/api/v1/rulesets` | 已發布 ruleset ID、version、說明及限制 |
| POST | `/api/v1/scans` | 建立持久化任務，回傳 `202 Accepted` |
| GET | `/api/v1/scans` | 分頁查詢已提交任務 |
| GET | `/api/v1/scans/{id}` | 狀態、進度、coverage、warnings、timings |
| GET | `/api/v1/scans/{id}/results` | 分頁候選／全部 evaluation；可按 stage／candidate 過濾 |
| GET | `/api/v1/scans/{id}/results/{instrument_id}` | 特徵、分數構成、原因及候選窗口 |
| GET | `/api/v1/scans/{id}/series/{instrument_id}` | 該 run snapshot 的 Close／均線序列，支援未來 App 畫圖 |
| GET | `/api/v1/scans/{id}/changes` | 與有效 baseline 的變化與不可比較原因 |
| GET | `/api/v1/scans/{id}/export?format=csv` | 串流匯出既有結果；不觸發重新掃描 |

GET 不產生下載、重新分析或寫入任務的副作用。series 預設 126、最多 504 sessions，僅限該 run 的標的；不是任意公開行情下載 endpoint。

名單更新須傳 `expected_revision`，衝突回 `409 WATCHLIST_VERSION_CONFLICT`；scan 接受後不受其後名單修改影響。若名單有 active run，刪除可先回 `409 WATCHLIST_IN_USE`，避免不明確的行為。

結果只在 `SUCCEEDED`／`PARTIAL` 的原子完成後公開。進行中的結果讀取回 `409 SCAN_NOT_READY`；失敗且無結果回 `409 SCAN_FAILED`，不要用空 array 假扮零候選。

### 8.3 建立 scan 範例

以下 ID 及資料只用於展示合約。

```http
POST /api/v1/scans
Content-Type: application/json
Authorization: Bearer <local-api-token>
Idempotency-Key: 3f56af32-5f96-4ff1-9d44-0f02be7a1a94
```

```json
{
  "watchlist_id": "b5086dc3-4ee6-4e99-94a8-ead7e6064b68",
  "ruleset_id": "breakout-v1",
  "as_of_session": "2026-09-04",
  "data_mode": "auto"
}
```

```json
{
  "id": "818f06d7-83bf-496b-a22b-7812c2ac1c43",
  "state": "QUEUED",
  "as_of_session": "2026-09-04",
  "watchlist_revision": 3,
  "ruleset_version": "1.0.0",
  "links": {
    "self": "/api/v1/scans/818f06d7-83bf-496b-a22b-7812c2ac1c43",
    "results": "/api/v1/scans/818f06d7-83bf-496b-a22b-7812c2ac1c43/results"
  }
}
```

同時回傳 `Location` header。HTTP response 不等下載完成才回應，也不聲稱接受任務等於掃描已成功。

### 8.4 冪等性與分頁

`POST /scans` 要求 Idempotency-Key，scope 為 principal + endpoint + key。以 unique constraint 和 transaction 保證 concurrent requests 不重複建立。

完成身份及 request shape 驗證後，先查既有 key，再解析會改變的名單／預設日期。相同 key + 相同 canonical request 回傳原 scan；不同 request 則 `409 IDEMPOTENCY_CONFLICT`。重試原請求不重新解析新一天的 default as-of，也不因 watchlist 剛被修改而建立另一個 run；回傳首次接受時的 snapshot 語義。主動重掃使用新 key。

V1 key 隨 run 保留，不建立隱含短 TTL。Key 不是秘密，不放 token 或個人資料。提交太多 queued jobs 回 `429 QUEUE_LIMIT_REACHED`，初始上限 20；限制只由 server 設定。

列表採 cursor pagination，default 50、max 200。結果固定 `score DESC, instrument_id ASC`，null score 排最後；cursor 綁定 run、filter、sort，不讓使用者輸入任意 SQL order。掃描列表固定 `created_at DESC, id DESC`。

### 8.5 結果欄位示例

```json
{
  "instrument_id": "36a13c94-ef33-4556-b911-4fcb96d865db",
  "symbol": "EXAMPLE",
  "evaluation_status": "EVALUATED",
  "is_candidate": true,
  "stage": "NEAR_CLOSE_RESISTANCE",
  "score": 79,
  "score_breakdown": {
    "momentum": 18,
    "trend": 15,
    "consolidation": 17,
    "contraction": 14,
    "proximity": 15
  },
  "selected_window_sessions": 20,
  "as_of_session": "2026-09-04",
  "reference_session": "2026-09-03",
  "price_basis": "split_adjusted_close",
  "latest_close": 98.2,
  "close_resistance": 100.0,
  "distance_to_resistance": -0.018,
  "close_range_5": 0.04,
  "contraction_ratio": 0.7,
  "not_evaluated": ["INTRADAY_RANGE", "VOLUME", "LIQUIDITY", "CATALYST"],
  "reasons": [
    {
      "code": "NEAR_CLOSE_RESISTANCE",
      "parameters": {"distance": -0.018, "window_sessions": 20}
    }
  ],
  "warnings": []
}
```

這是自製合約示例，不是即時行情或真實掃描結果。完整 Pydantic schema 另含 features、alternative windows 及不可用指標原因，並由測試驗證欄位一致性。

錯誤使用一致 envelope：

```json
{
  "error": {
    "code": "INVALID_AS_OF_SESSION",
    "message": "The requested date is not a supported trading session.",
    "details": {"as_of_session": "2026-09-05"},
    "request_id": "dcf93ed8-a86b-4f58-9c20-0b96db41da25"
  }
}
```

驗證錯誤、domain error 與框架錯誤均經同一映射；404 不透露其他 principal 的資源是否存在。範例英文訊息方便未來 App localization，顯示層可翻譯 reason code。

---

## 9. 任務執行與可靠性

### 9.1 第一版部署模型

一個 Uvicorn process、一個 serial scan executor、一個本地 SQLite。`qscan serve` 用 lifespan 啟停 executor；下載／運算在其專屬 thread 執行，從 DB 輪詢 `QUEUED` jobs。API handler 只驗證、建立紀錄及查詢狀態。

不把 `BackgroundTasks` 當持久化 queue。Starlette 對此功能的定位是 in-process background task；本專案需要的重啟狀態及任務紀錄，由自己的 DB state machine 保證。[^starlette-background] FastAPI lifespan 用於資源啟停。[^fastapi-lifespan]

V1 不用 Celery、Redis、RabbitMQ 或多 worker。啟動時用 data directory 級跨平台 exclusive executor lock 防止兩個掃描執行器操作同一 cache；第二個 `serve` 或直接 `scan` 清楚報錯。API 已運行時，新的掃描透過 API 提交；V1 不要求另外實作完整 remote CLI。

讀取命令可並行；watchlist 的短交易可透過相同 DB 操作。migrate、restore、full cache maintenance 須在停機／取得維護鎖後執行。SQLAlchemy Session／SQLite connection 按 thread／operation 建立，不跨 thread 共用 ORM Session。

### 9.2 狀態機

```text
QUEUED → RUNNING → SUCCEEDED
                 → PARTIAL
                 → FAILED
```

| 狀態 | 精確定義 |
| --- | --- |
| `SUCCEEDED` | 至少一個有效 evaluation，沒有未處理的標的資料錯誤；零候選可成功；可包含已明確排除的短歷史／不支援標的 |
| `PARTIAL` | 至少一個有效 evaluation，但有部分標的因下載、缺價或其他資料錯誤未能評估 |
| `FAILED` | 全域失敗，或沒有任何標的可以有效評估 |

對外提供 `phase`、`processed_symbols`、`total_symbols`、最近 progress timestamp，不製造不準確完成時間預測。`candidate_count` 不等於資料完整率。

固定計數不變量：

```text
requested_symbols = evaluated_symbols + excluded_symbols + data_error_symbols
candidate_symbols <= evaluated_symbols
```

預期的資料不足／不支援屬 excluded；意外缺價／無法取數屬 data_error。規則不符合但資料有效仍屬 evaluated。

### 9.3 重啟及交易界線

接收請求先 commit `QUEUED` row，才回 `202`。executor 在短 transaction 內以 compare-and-set claim 一個 job；即使暫時只有一個 executor，也不使用無條件 state update。

價格更新、input snapshot 形成與計算分開；網絡呼叫期間不持有 DB write lock。最終 results 及 terminal state 在同一短 transaction 公開，避免「顯示成功但結果尚未寫完」。

重新啟動時，未開始的 `QUEUED` 保留並繼續；在取得 exclusive executor lock 後，把遺留 `RUNNING` 標為 `FAILED / WORKER_INTERRUPTED`。V1 不自動重放一半完成的 run；使用者以新 key 建立新 run。必須測試 kill-process／重啟情境，不能只測正常 shutdown。

啟動診斷記錄實際 SQLite runtime version，release 前核對相關安全／修正公告。SQLite 設定 foreign keys、busy timeout、WAL；資料庫放同一主機本地磁碟，不放 NFS／SMB 或運行中的雲端同步目錄。SQLite 官方明確指出 WAL 不適用於 network filesystem。[^sqlite-wal]

---

## 10. 報告與人工覆核

### 10.1 輸出內容

HTML 以一頁候選表、stage 分組及少量圖表為主，不做 dashboard 平台。表格包含 symbol、score、stage、窗口、前段動量、收窄、阻力距離、主要原因及警告。

每份報告頂部顯示：scan ID、as-of／reference session、ruleset／engine version、price basis、完整度計數，以及「只屬 close-only 初篩，流動性及日內形態未評估」。

先顯示候選，再顯示資料錯誤／排除摘要。沒有候選與全部數據失敗必須明顯不同。

只為顯示的候選生成圖表，default 30、max 50。用收市線、SMA10／20／50、所選窗口及收市阻力線；不畫不存在的 OHLC candle，也不把 Close range 稱為 ATR／ADR。圖表由該 run snapshot 產生。

### 10.2 匯出安全與格式

JSON／HTML 統一 UTF-8；CSV 預設 UTF-8 BOM 方便常見試算表讀取，並在文件註明。日期、比率欄位名稱有固定語義，不把同一欄一時輸出 0.2、一時輸出 20%。

HTML 對名單名稱及文字 escape。CSV 對可能被試算表當成公式的**文字欄位**作 injection 防護；有效的數值欄位仍以數字輸出，不把所有負數轉成字串。輸出檔案名稱由程式產生，避免 path traversal。

報告生成失敗不推翻已完成的 scan；記錄 report error，允許以相同 scan ID 重建，不重新下載或掃描。

### 10.3 效用驗證

匯出一份簡單人工標記 CSV：日期、symbol、selected window、`worth_reviewing / borderline / not_useful`、備註。不加入自動學習，也不要求完整 review UI。

評估「候選中值得開圖的比例」和覆核工作量；再抽查未入選股票，以估計漏網情況。只看候選不能知道 recall。先用一組日期調參，再用另一組未參與調參的日期檢查，不用事後大升股票反向砌門檻。

這不是收益 backtest；close-only 無法重建原策略所有日內交易路徑。

---

## 11. 安全與未來 App 的分界

### 11.1 V1 必須完成

預設只 bind `127.0.0.1`，不聲稱「本機所以不用防護」。所有業務 API 要求本地 bearer token，由 init 產生並存於使用者設定區，CLI 直接核心模式不需經 token；health endpoint 只暴露極少資訊。

所有帶 Origin 的請求依 allowlist 驗證，CORS 預設不開放；不使用 `*`，限制 Host，不提供無認證的寫入表單。OpenAPI UI 僅在本地／已授權開發模式開放。token 不放 query string、Git、HTML 報告或 log。

client 不能指定 server file path、provider URL、Python expression 或任意輸出目的地。API 只接受已註冊 ruleset ID；使用 Pydantic 限制長度／範圍及未知欄位，並對 queue／body／pagination 作上限控制。

`owner_id` 由 server principal 決定，所有 repository access 做 scope 檢查；用兩個測試 principal 驗證跨 owner 資源不可讀寫，即使 V1 正式使用只有 local principal。

### 11.2 公開 App 前必須另外完成

| 能力 | 為何不是 V1 local token 的替代稱呼 |
| --- | --- |
| 真正身份與授權 | 帳戶、可撤銷 session／token、object-level authorization；不可把同一固定秘密放進所有 App |
| HTTPS 與部署防護 | TLS、反向代理、rate limit、監控、機密管理及備份還原演練 |
| 資料授權確認 | 個人免費原型不代表可向多使用者公開提供行情或衍生服務 |
| 負載及資料庫驗證 | 按實際並發、DB contention 及 worker needs 決定是否轉 PostgreSQL／外部 queue |
| App 合約測試 | 由 OpenAPI 產生或校驗 client types，測試登入失效、輪詢、重試及離線呈現 |

yfinance 說明其與 Yahoo 無官方關係，並提示資料使用權及個人用途限制；公開 App 前要核對實際數據授權，不能把「可免費下載」當作再分發授權。[^yfinance-terms]

未來 native App 走遠端 API，不要求手機內嵌 Python 或直接同步 SQLite。日後加入 n8n，只作 API client，不改 core 或產品資料模型。

---

## 12. 測試與效能驗收

### 12.1 必測清單

| 類別 | 測試案例 |
| --- | --- |
| 數學 | return、SMA、各窗口長度、log-return std、ddof、區間及分段邊界的手算樣本 |
| 無偷看未來 | 改動 `t` 後所有資料不影響結果；改動 `C_t` 不改每個固定窗口截至 `u` 的阻力／前段特徵 |
| close-only | provider 回傳相同 Close 但不同 OHLCV，結果完全一致；核心資料型別沒有其他價格欄位 |
| 形態 | 先升後整理、一路下跌、一路直升、低位橫行、曾升但現已跌破、收市升穿、過度延伸 |
| 缺值 | 缺 126 日回報、短歷史、某窗口缺資料、std 分母為零、常數序列、NaN／inf／非正值 |
| 調整 | 拆股前後一致口徑、不 double-adjust、除息不切換到總回報價、交疊修訂後完整重取 |
| 日期 | 週末、假日、提早收市、夏令時間切換、香港／東京／UTC 本地時鐘不影響 market session |
| 名單 | 大小寫、去重、BOM、中文名稱、類股符號、不支援市場、過大輸入、名單 revision 衝突 |
| 快取 | cache_only 零網絡請求、auto 命中快取、force、timeout、限流、單一 symbol 失敗及舊資料不可冒充今日 |
| 一致性 | 同 snapshot 經 core、CLI、API 取得相同 score／stage／排序；報告與 persisted result 一致 |
| API | 202／Location、冪等衝突、並發同 key、pagination、unknown fields、null／float serialization |
| 任務 | queue 上限、雙 executor、process kill、RUNNING recovery、queued resume、atomic result publication |
| 安全 | 無 token、跨 owner ID、Origin／Host、path traversal、CSV formula injection、log secret redaction |
| 變化 | 缺價不算掉出候選、名單改動不算價格 signal、不同 config 不做日比較、同日重跑 baseline |
| migration | 全新 DB、舊 schema 升級、資料保留、備份／還原後 results + snapshots 可 replay |

單元及 contract tests 全部使用 synthetic／fixture data，不依賴 Yahoo。線上 smoke test 單獨標記，手動或低頻執行；CI 不因外部限流而隨機失敗。

### 12.2 跨平台驗收

CI 至少有 Windows、macOS、Linux 三個 runner，全部安裝已鎖定依賴、跑離線測試，並從 wheel 安裝後執行 CLI smoke test。以含空格／中文且非 repository 內的 data path 測試路徑與權限。

另外確認 Linux headless 圖表、Windows CSV、UTF-8 路徑及安裝環境的 timezone 資料。macOS Apple Silicon／Intel、Windows／Linux 不同 CPU 架構的支援要按實際測試列明；不能因測過一個 macOS runner 就聲稱所有架構已驗收。

產出 wheel／source distribution 作基本交付。單檔 `.exe`、`.app` installer 與 code signing 延後；跨平台 source 不等於同一個 native executable 能在三個 OS 運行。

### 12.3 效能

建立可重跑 benchmark，分別測 100／500／1,000／2,000 symbols，每隻最多 504 sessions。分開記錄 fetch、validation、snapshot、core compute、DB write、report rendering 與 peak memory，不能只報一個總秒數。

第一個性能目標：在記錄硬件／OS／Python／套件版本的參考機上，1,000 × 504 的**離線核心計算及排序**中位數不超過 10 秒，作為可調整的工程目標；至少 warm-up 一次、量測五次。這不是已達成數字，也不包含下載或圖表。

初次／增量下載只報實測分佈、成功率及重試，不承諾免費 provider 固定幾秒完成。若超出目標，先 profile；先修重複計算、過量資料搬運及不必要圖表，再考慮 multiprocessing。

未發現性能問題前，保持單 process 計算，不為每個指標建立 worker。

---

## 13. 開發階段與交付順序

每一階段以可驗收產物完成，不以「寫了幾個檔案」完成。所有 task 在實際完成前保持 unchecked；不得把 mock、設計範例或未執行的測試列為已通過。

### Phase 0 — 基線、provider spike 與合約

**目的：** 先固定最容易造成重寫的資料與介面決策。

- [x] 建立 package、uv lock、Ruff／mypy／pytest、三平台 CI 骨架。
- [x] 寫 ADR：close-only 邊界、價格 basis、單 process + SQLite、core／CLI／API 分層。
- [ ] 驗證 yfinance 的單／多 ticker 回傳形狀、Close／Adj Close、拆股、除息、end exclusive 及 incomplete session。
- [ ] 用小型合法個人測試名單驗證來源；離線 fixtures 使用 synthetic 或可合法保留的最小樣本，勿把整批第三方歷史提交 repository。
- [x] 固定 typed domain models、RuleConfig、error codes 與 API schema 初稿。
- [x] 把仍未核實的 provider 行為記錄為明確失敗／限制，不用猜測關閉問題。

**驗收：** `uv sync --locked` 及最小離線測試在三平台可跑；provider spike 有紀錄；核心資料型別只容許 Close。若來源口徑未通過，先阻擋線上 provider release，不阻擋 fixture core 開發。

### Phase 1 — 純核心與規則基線

**目的：** 完全不依賴網絡、DB 或 HTTP 也能篩選。

- [x] 實作 session-indexed features、10／20／40 日窗口及 unavailable feature 語義。
- [x] 實作 gate、五維 scoring、stage、窗口選擇、reason codes 及穩定排名。
- [x] 為每個門檻、null、zero denominator、下跌／突破／延伸例子寫測試。
- [x] 加 no-lookahead 與 close-only invariance tests。
- [x] 將公式、參數及限制寫入 `docs/rules.md`，與 fixture expected results 對照。

本機離線核心交付及未完成驗收見 [開發驗收記錄](phase-0-status.md)。no-lookahead 已測；
close-only 合約拒絕額外欄位已測，Phase 2 已以單／多 ticker adapter fixtures 完成 OHLCV invariance。

**驗收：** 人手可核對的 synthetic datasets 有固定 expected output；所有公式、stage 邊界及未來資料擾動測試通過。不接受只以真實股票圖片「睇落似」作驗收。

### Phase 2 — 資料層、快取與持久化 run

**目的：** 令一次真實掃描可以查詢、重跑及診斷。

- [x] 建立 SQLite schema、Alembic migration、repository 及 local principal scope。
- [x] 實作 list import、symbol adapter、calendar／as-of resolution、資料品質分流。
- [ ] 實作 Yahoo provider、bounded fetch、有限重試、增量交疊及歷史修訂處理。
- [x] 加 `auto / cache_only / force`；cache_only 測試保證零網絡。
- [x] 保存完整 watchlist／rules／input snapshots、results、hash、counts 及 timings。
- [x] 驗證資料 update 與 snapshot／result publication 的交易界線。

Phase 2 離線持久化流程已實作並有 installed-wheel demo。Yahoo adapter、bounded retry／
concurrency 及 fixture overlap/full refresh 測試已完成；上列 Yahoo 綜合項仍不勾選，
因正式來源價格口徑、盤中 incomplete-session 與市場 metadata 尚未驗收。
2026-09-05 live 機械 probe 兩例通過仍不解除 BLOCKED，詳見
[Phase 2 驗收紀錄](phase-0-status.md) 與 [data quality](data-quality.md)。
同步服務不包含第 9 節的 HTTP queue／startup recovery，不代表 Phase 3/4 或 V1 完成。

**驗收：** 同一 snapshot 可離線重現；單一下載失敗不終止其他 symbol；拆股／修訂 fixture 不產生拼接口徑；沒有資料與沒有 setup 分得清。

### Phase 3 — 可使用的 CLI 與報告

**目的：** 使用者不需啟動 web server，已可完成日常流程。

- [x] 實作第 7 節 CLI、exit codes、stdout／stderr 分流與 doctor。
- [x] 輸出 JSON、CSV、HTML 與 top-candidate Close charts。
- [x] 實作歷史 run 查詢、snapshot replay、daily changes 與比較限制。
- [x] 補 HTML escaping、CSV 防護、跨平台檔案與 headless 測試。
- [x] README 提供由空目錄開始、匯入 list、掃描、開啟報告的最短流程。

本次本機 CLI／offline wheel 驗收見 [紀錄](phase-0-status.md)。三平台 CI 已延伸，
本次 Windows/Linux runner 與手機瀏覽器視覺驗收仍待執行；Yahoo 維持 BLOCKED。

**驗收：** fresh install 可跑 fixture demo；有來源時可跑個人名單；第二次 cache-only 結果可重現；零候選、partial failure、report failure 都有明確輸出。完成本階段已有可用 CLI MVP。

### Phase 3.5 — Stabilization & Live-data Acceptance

2026-09-05 新增，以上 Phase 2/3 結果保留為當時歷史，以下為本輪交付。

- [x] Windows UTF-8 regression 修復，保留 BOM／中文路徑測試。
- [x] SQLite 真正 BEGIN read transaction；event 控制 run/watchlist/cache 競態。
- [x] Report run/snapshot 各載入一次；CSV 不算 charts；100/1,000 symbols benchmark。
- [x] 歷史 snapshot decode 與 exact replay engine 檢查分離。
- [x] 結構化 symbol／窗口原因、空 CSV companion summary。
- [x] 集中 Yahoo EOD_TRIAL gate、可靠 metadata、小名單真實 CLI 全流程。
- [x] 桌面／窄螢幕 HTML 實際視覺驗收，沒有新增前端框架。
- [x] 新三平台 offline tests/build/installed-wheel CI：run 33967493810 全部通過，見驗收紀錄。

實際證據、限制及本輪 CI 見 [驗收紀錄](phase-0-status.md)；gate 收窄理由見
[ADR 0004](adr/0004-eod-trial-acceptance.md)。盤中觀察、公開行情授權、production SLA
不冒稱完成。Phase 4 可在三平台 gate 通過後開始共用 application service 的 API 工作。

### Phase 4 — API 與持久化 executor

**目的：** App-ready 的介面，而不是只把 CLI 包一層 shell。

2026-09-06 本輪交付（branch `codex/phase4-local-api`，基於 Phase 3.5 已驗收
e46068f／90dd5ad 工作樹）：

- [x] 實作 FastAPI schema、error envelope、watchlist CRUD／revision、rulesets 及 scan queries。
- [x] 實作 POST scan 202、DB job state、serial executor、進度及 startup recovery。
- [x] 實作 Idempotency-Key transaction／unique constraint、queue limit 及 cursor pagination。
- [x] 實作 run-bound detail／series／changes／export endpoints。
- [x] 加 local token、Origin／Host 檢查、owner scope、request limits 及 secret redaction。
- [x] 測 concurrent idempotency、雙 executor、process crash、資料寫入原子性。
- [x] 輸出 OpenAPI snapshot 及一個 HTTP client smoke example，確認不解析 HTML／CLI 文字也能完成流程。

`ScanService` 拆為 `prepare`（純組裝 QUEUED run）與 `execute_existing`（CAS 認領
同一 id 執行並原子發布）；CLI `scan` 同步走同一對入口，worker 不產生第二個 run。
migration 0003 為 `scan_runs` 加 `idempotency_key`／`request_hash` 欄位與
owner+key unique index，不刪庫重建。serve 以 data-directory 所有權鎖獨占目錄
（第二個 serve／CLI scan／init 衝突回 exit 4），服務以注入 NullLock 避免跨
thread 重入死結（[ADR 0005](adr/0005-http-api-executor.md)）。已實測證據、
限制與本輪 CI 見 [驗收紀錄](phase-0-status.md)；Windows/Linux runner 結果以 CI
實際 run 為準。

**驗收：** HTTP client 可建立名單 → 提交任務 → 輪詢 → 取得結果／圖表資料；重送同 key 只得一個 run；重啟無永久 RUNNING；API 與 CLI 用同 snapshot 得到相同結果。完成本階段才算 CLI + API 功能齊備。本輪已以 repo 外 installed-wheel HTTP smoke（init → serve → HTTP 全流程 → 重啟查歷史）及 kill-process recovery 測試驗證；公開多使用者部署仍屬第 11.2 節範圍，不因本輪完成。

### Phase 5 — 跨平台、效能與 release hardening

**目的：** 令安裝、維護及限制都有可重現證據。

- [x] 三平台 wheel installation／offline E2E：GitHub Actions 曾覆蓋
  （ubuntu/windows/macos，最後全綠 `614e7c3`），**2026-09-06 起因費用移除**；
  現行把關係本地 `scripts/check.py`（全套含 OpenAPI check/build/wheel smoke），
  跨平台需於各平台手動執行——未手跑組合見 [release checklist](release-checklist.md)
  第 8 節。
- [x] Benchmark：`scripts/benchmark.py` 分段量測 100–2,000 symbols，輸出硬件、
  套件版本、各段耗時、raw samples、peak RSS 方法與限制；實測
  `docs/benchmarks/phase5-macos-arm64.json`；1,000×504 core+ranking median 0.42s
  （目標 ≤10s，目標而非保證，CI 不設絕對秒數門檻）。
- [x] 人工候選抽查：`scans review-export` 產生候選 + 固定 seed 非候選對照 CSV；
  固定 breakout-v1 baseline；**真人標記仍 pending**，不宣稱 precision/recall。
- [x] DB + snapshots 一致備份、還原及 migration 演練：`qscan backup
  create/verify/restore`（[ADR 0006](adr/0006-backup-format.md)），含 WAL、舊
  schema 升級、不可信 archive、忙碌拒絕等演練測試與 wheel smoke 段。
- [x] 排程指引：macOS launchd／Windows Task Scheduler／cron／systemd timer 範例
  （operations.md），絕對路徑、不依賴互動 shell、休市日由日曆判斷；只提供範例，
  不自動安裝。
- [x] README／rules／API／operations／data-quality 現況化；備份格式 ADR；CHANGELOG
  draft；source **license 未決定——已如實標明，不擅自指定條款**。
- [x] Release checklist：[docs/release-checklist.md](release-checklist.md)，
  每項附證據；未完成項（真人覆核標記、未手跑平台組合）明確列出；定位維持
  「本地 CLI + API／個人 EOD 試用的發布候選」，不稱公開多使用者平台。

**驗收：** 核心功能與可維護性證據齊備；真人覆核標記與本地多平台手跑屬已知未完成，
見 checklist 第 5/8 節。V1 Definition of Done 對照亦記錄於該 checklist。

---

## 14. V1 Definition of Done

| 範圍 | 必須成立 |
| --- | --- |
| 分層 | 無 API／CLI 業務邏輯副本；core 無外部 I/O 依賴 |
| 數據 | close-only、固定價格 basis、正確 session、無 forward-fill 偽整理、缺價可見 |
| 篩選 | 可用窗口、gate／score／stage／原因明確，無當日阻力 lookahead |
| 可重現性 | 保存 config、名單、engine version 及真正 input snapshot；歷史報告不隨 cache 改寫 |
| CLI | 不啟 API 亦可完成掃描與匯出，錯誤碼及離線模式可測 |
| API | 202 任務、持久化 state、冪等、分頁、details／series、OpenAPI 及一致錯誤格式 |
| 可靠性 | partial failure、process interruption、executor collision 及 atomic completion 均有測試 |
| 安全 | localhost default、本地 token、owner-scoped access、Origin／Host／輸入／匯出防護 |
| 跨平台 | Windows／macOS／Linux 已測環境可以安裝並完成離線 E2E |
| 效能 | 有可重跑 benchmark；未聲稱未測量的速度、容量或免費 provider SLA |
| 使用價值 | 能辨認候選、排除、資料失敗及每日變化，人工覆核流程可完成 |
| 文件 | 新開發者依 README 可安裝、跑 demo、測試、啟動 API，並理解已知限制 |

---

## 15. 延後項目與啟動條件

| 延後項目 | 何時才加入 |
| --- | --- |
| Web／mobile GUI | CLI／API 與篩選效用已驗收，使用流程清楚後 |
| 公開多使用者部署 | 完成身份、授權、數據權利及營運檢查後 |
| PostgreSQL | 真實並發／部署需求超出單機 SQLite，並完成 migration／integration test 後 |
| 外部 queue／多 worker | 測到單 worker 吞吐、隔離或重試能力不足，而非預先假設 |
| n8n 通知／串接 | 需要自動同步或通知時，以既有 API client 方式加入 |
| 額外 provider | Yahoo 不再符合需求，或正式服務需要授權來源時 |
| 短歷史 IPO、RS／市場 regime | 有明確漏網案例與人工覆核證據，另立規則版本 |
| GUI 打包／installer | 確認桌面分發需求後，各平台分別 build／sign／test |
| 策略績效驗證 | 另定交易模型、可用資料、成本及 point-in-time 限制，不混入初篩驗收 |

**不是延後項目：** 價格口徑、session／as-of、核心與介面分離、資料錯誤分流、API 任務冪等、規則快照及歷史重現。這些直接影響結果正確性或未來 App 合約，必須在 V1 處理。

---

## 16. 交給 coding agent 的執行規則

依 Phase 0 → 5 實作，每階段保持可安裝、可測試。不要一次建立大量空 abstraction，也不要先做 GUI、登入平台或微服務。

先用 synthetic fixtures 完成核心，再接 Yahoo；所有因第三方資料造成的失敗要可見。核心、CLI、API 與 report 不得各自修改公式。規則變更同步更新 config version、tests 及 `docs/rules.md`。

程式識別字、comments、docstrings 用英文；面向使用者的文件及報告可用繁體中文。保留結構化 reason codes，避免日後 localization 需要改核心。

每次交付說明實作內容、執行過的測試、實際結果及未完成項目。未執行的 benchmark 不可提供看似實測的秒數；provider 限流或數據缺口不可用 mock result 偽裝成功。

MVP 的完成標準是：**一份 list 進來，產生可解釋、可追溯的候選；同一功能既能由 CLI 使用，也能由未來 App 經 API 使用。**

---

## 17. 參考來源與核實範圍

以下來源於 2026-09-05 查閱。外部來源用於原策略背景、函式庫行為及限制；本文件的門檻、權重、任務模型、API、性能目標和開發順序均是本專案的設計決策，不是來源背書或已驗證成效。

[^qullamaggie]: Qullamaggie, “3 TIMELESS setups that have made me TENS OF MILLIONS!” — https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/ 。只參考 breakout 的升浪／整理／突破背景，未複製其交易執行規則。

[^uv]: uv 官方文件，Project structure and files — https://docs.astral.sh/uv/concepts/projects/layout/ 。核實 `pyproject.toml`／`uv.lock` 與跨平台 lockfile 行為。

[^fastapi-features]: FastAPI 官方文件，Features — https://fastapi.tiangolo.com/features/ 。核實 OpenAPI／JSON Schema 介面能力。

[^zoneinfo]: Python 官方文件，zoneinfo — https://docs.python.org/3/library/zoneinfo.html 。核實 IANA timezone data 來源及跨平台 `tzdata` 建議。

[^yahoo-history]: Yahoo Finance 歷史資料頁 — https://finance.yahoo.com/quote/%5EGSPC/history/ 。adjusted close 定義包含拆股、股息及／或資本收益分派；實際 adapter 的 Close 口徑仍須由 Phase 0 驗證。

[^yfinance-download]: yfinance 官方文件，download — https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html 。核實調整參數、日線、threads、timeout 與 exclusive end date。

[^yfinance-repair]: yfinance 官方文件，Price Repair — https://ranaroussi.github.io/yfinance/advanced/price_repair.html 。核實價格修復可能用更細時間級別重建資料及調整錯誤情境。

[^calendar]: pandas_market_calendars 官方文件 — https://pandas-market-calendars.readthedocs.io/en/latest/ 。核實假期／提早收市及日曆規則隨套件版本提供，而非 runtime live feed。

[^starlette-background]: Starlette 官方文件，Background Tasks — https://starlette.dev/background/ 。核實內建 task 為 in-process 執行模型。

[^fastapi-lifespan]: FastAPI 官方文件，Lifespan Events — https://fastapi.tiangolo.com/advanced/events/ 。核實應用資源啟停方式。

[^sqlite-wal]: SQLite 官方文件，Write-Ahead Logging — https://www.sqlite.org/wal.html 。核實 WAL 的同主機與 network filesystem 限制。

[^yfinance-terms]: yfinance 官方首頁及使用提醒 — https://ranaroussi.github.io/yfinance/ 。核實非 Yahoo 官方工具、個人使用及資料權利提醒；不取代正式授權審查。
