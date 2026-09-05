# Phase 2 資料品質與 cache

本文件描述已實作的同步資料管線；規則與門檻仍以 [rules](rules.md) 為準。
核心程式未改動，不讀 OHLCV、網絡、時鐘或 DB。

## 名單與市場

`WatchlistService.import_content()` 接收 UTF-8 bytes、TXT／CSV 格式與名單名稱。
接受 BOM、空行、trim、TXT 行首 `#` 註解；CSV 要有 `symbol`，可有 `exchange`。
上限為 1,000,000 bytes、去重後 2,000 標的；非法 UTF-8、ticker、CSV 或空名單以
`VALIDATION_ERROR` 拒絕。修改須提供既有 ID 與 expected revision，衝突以
`WATCHLIST_VERSION_CONFLICT` 拒絕，名稱在 owner 內唯一。

離線 US symbol resolver 不查網絡，使用 UUIDv5（namespace URL，`qscan:US:<provider_symbol>`）
保存穩定 identity。只有 BRK.A／BRK.B／BF.A／BF.B 的已測試 aliases 映射成連字號；
其他 `.` 不替換。display symbol 與 provider symbol 分開保存，名單順序按首次出現。
NYSE／NASDAQ／AMEX 使用同一 US session 日曆；明確其他市場或不支援的 symbol 型態排除。
fixture resolver 預設 USD、EQUITY、NYSE，是合成資料的明確 context，不能當成 Yahoo
已核實的市場／資產 metadata。ETF 可由具體 provider 回傳 `Instrument(instrument_type="ETF")`，
策略不作特殊調參。正式 Yahoo 市場身份確認亦須在解除 release blocker 前完成。

## 交易日與時間

`NYSECalendar` 使用鎖定 pandas-market-calendars，clock 可注入。
未指定日期取收市加 buffer（預設 30 分鐘）已過的最近 session；明確非法日期為
`INVALID_AS_OF_SESSION`，未完成日期為 `SESSION_NOT_COMPLETE`，不回退日期。
reference 必須是 as-of 的上一個有效 session。測試涵蓋假期、DST、提早收市及 buffer 邊界。

`overrides={date: aware_datetime_or_None}` 可明確改收市或移除 session；datetime 必須帶時區。
snapshot 保存實際 expected sessions、日曆套件版本、buffer 與 override 描述，replay 不重新
讀取今日的日曆。資料日期保留市場 session date；所有 application timestamp 轉為 UTC。

## Close 驗證

先裁切至目標日期及最多 504 sessions 的保留範圍，再檢查排序、重複、有限正數及交易日。
相同日期相同值可合併，矛盾值為 `CONFLICTING_DUPLICATE`；未排序為
`VALIDATION_ERROR`，非 session 行為 `MISSING_REQUIRED_SESSION`。
空回傳為 `NO_DATA`，沒有目標日為 `STALE_DATA`，無效價格為 `INVALID_CLOSE`。

不補值、不插值、不壓縮缺口。只保留終止於目標日的最後連續區段；若舊資料證明中間有缺口，
且 suffix 少於 80，為 `MISSING_REQUIRED_SESSION`。suffix 至少 80 時可安全評估，
保存缺口 warning；126 return、40 日窗口等會按核心既有語義 unavailable。
沒有較舊資料可證明缺口的短上市歷史交由核心以 `INSUFFICIENT_HISTORY` 排除。
任意連續 20 筆同價由核心隔離為 `SUSPICIOUS_FLAT_SERIES`。

basis 固定 `split_adjusted_close`。非此 basis、provider 標記待覆核，或相鄰 Close
最大／最小比值至少 3，均為 `ADJUSTMENT_REVIEW_REQUIRED`。最後一項是保守的資料
隔離閾值，不是拆股判定或交易訊號；不修改價格。不使用 Adj Close，不重複套用 split factor。

## cache 更新與修訂

- `cache_only` 在任何 fetch 前返回，不 lookup metadata、不 resolve 已保存的 symbol，
  bootstrap／名單匯入的 resolver 亦不連網。測試直接阻擋 socket 與 provider calls。
- `auto` 復用可信且包含目標 session 的 cache。初次／需要較早歷史／週期覆核採完整下載；
  正常向前更新重取最近 10 sessions，比較既有 Close，再保留最多 504 sessions。
- `force` 重取目標的完整保留範圍。新上市資料可少於 504。
- overlap 差異超過 `math.isclose(rel_tol=1e-8, abs_tol=1e-10)`、拆股事件或 basis 問題，
  先將舊 cache 標記不可信，再 full refresh；只在新完整範圍驗證通過後原子替換。
- full refresh 也比較既有歷史，保存 UTC 修訂紀錄。覆核預設 30 天，可由 bootstrap
  `review_interval` 注入；auto 同一目標日達週期仍會覆核。
- fetch 失敗但相同目標的舊 cache 仍可信時可以回用，run provenance 保存
  `refresh_failed:<ErrorCode>`。已觀察到歷史修訂／basis 不可信則禁止 fallback。
  失敗保留舊 price rows 供診斷；被 quarantine 的 rows 在 cache_only 也不能評估。

市場資料是共用本機 cache，名單／run／results 查詢有 principal scope；cache 並非多租戶安全
儲存。provider 回傳只在 adapter 邊界轉成 Close rows，單一 fetch 的 typed 或非預期錯誤
會成為該標的結果，不阻止其他標的。DB／snapshot 全域寫入失敗則整個 run 失敗。

## Yahoo 驗收界線

adapter 明確設定 interval 1d、auto_adjust/back_adjust/repair/prepost 均 false，
目標日轉為 exclusive end = 目標日 + 1 calendar day；每隻獨立下載，yfinance threads=false。
最大並行 2、預設 timeout 15 秒、最多 2 次 attempt（設定上限 3）、短 exponential backoff。
同步 service 目前逐隻執行，因此實際並行是 1；adapter semaphore 在多呼叫者下仍限制 2。
處理 flat／field-first／ticker-first 形狀，不因部分失敗重跑整份名單。

[實際 probe](phase2-provider-validation.json) 的 AAPL 拆股、AAPL/MSFT 除息／多 ticker、
exclusive end 機械檢查通過，但不代表完整口徑驗收。YahooProvider 預設 BLOCKED；
diagnostic 模式的 raw data 仍帶 adjustment-review 標記。人工價格口徑、真正盤中
未完成日線觀察與安全的市場 metadata 確認尚待完成。fixture 通過不能解除此限制。

## 結果狀態

`INSUFFICIENT_HISTORY`／`UNSUPPORTED_INSTRUMENT` 計 excluded；其餘資料不可評估計
data_error。規則不符仍計 evaluated。至少一個 evaluation 且沒有 data_error 為 SUCCEEDED；
有部分 data_error 為 PARTIAL；全域失敗或零 evaluation 為 FAILED。零候選可以成功。
永遠驗證 `requested = evaluated + excluded + data_error`、`candidate <= evaluated`。
未新增 domain ErrorCode；snapshot 損壞／不相容使用 `SCAN_FAILED` 附明確診斷訊息。

## Phase 3 每日比較（semantic_version 1）

baseline 在 scan 取得 executor lock、建立 RUNNING 之前選定，隨 run document 保存。
只取同 owner（repository scope）、watchlist UUID、config hash、price_basis、完全相同
engine version、exact reference_session 的 SUCCEEDED/PARTIAL 且 evaluated > 0 run。
finished_at 必須不晚於本次 started_at；同日依 finished_at DESC、UUID DESC 決勝。
source_run_id 非空的 replay 永遠不參與日常 baseline 選擇。

比較結果在本次 publication 內保存 comparison.previous_run_id、兩個 session、binding、
semantic_version、reasons、changes 及變更前後 analyses。後來補跑舊日期／replay 都不會
改寫舊 comparison。沒有前一日 baseline 用 NO_BASELINE；有規則／basis／engine 不相容
前一日 run 用 COMPARISON_UNAVAILABLE 與明確原因。只用 exact previous session，
不回退幾日前。較晚完成、失敗或 replay 的 runs 不構成有效 baseline。

相同 instrument UUID 兩日都 category=evaluated 才比較 candidate、stage、score、window。
新增／移除名單成員只用 UNIVERSE_CHANGED；短歷史、隔離、缺價及昨日無有效 evaluation
用 COMPARISON_UNAVAILABLE，不能叫掉出候選。provider 不同也不可比較。
NEW_CANDIDATE／DROPPED_CANDIDATE 可伴隨 STAGE_CHANGED、SCORE_CHANGED、WINDOW_CHANGED。
兩 snapshot 交疊 Close 超過 rel_tol=1e-8、abs_tol=1e-10 的差異附 DATA_REVISION_DIFF；
這只是修訂警示，不推算分數變動原因，也不重算舊 baseline。

legacy run 缺 comparison 時回傳 binding=legacy_unavailable、LEGACY_BASELINE_NOT_RECORDED，
不事後挑 baseline 冒充當時紀錄。replay 為 replay_unavailable、REPLAY_NOT_DAILY_SCAN。
baseline snapshot 在本次比較時缺失／損壞會保存 BASELINE_SNAPSHOT_UNAVAILABLE；既有
已保存 comparison 不依賴之後的 cache，也不會因後來 snapshot 消失被重寫。

圖表只取 run.input_hash 對應快照，檢查 context/watchlist/config；均線為包含當日 Close
的 SMA10/20/50，前期不足保持 null；selected window 陰影截止 reference_session。
--top 不改 persisted counts/results/ranks。render 不重新計算策略。

schema 0002 將 cache 改為 provider + instrument scope，保留並搬入原有 cache；fixture
不覆蓋 Yahoo，也不能被 Yahoo cache_only 接受。這是來源隔離，並非多租戶 market cache。

## Phase 3.5 — 已完成日線試用

前述 Phase 2 BLOCKED 是歷史結果。目前集中判定為 EOD_TRIAL，版本不符仍 BLOCKED；
完整證據矩陣、官方事件／價格對照與殘餘風險見 [ADR 0004](adr/0004-eod-trial-acceptance.md)。

Yahoo import 是離線 identity／hint，使用 UNVERIFIED、UNKNOWN currency，無 hint 不猜 NYSE。
正常 fetch 前核實 Yahoo chart symbol/exchange/currency/timezone/type；不符合則排除，
不在未知市場上評分。metadata 拒絕亦使舊 cache 不可信；新 hint 與 cached metadata
矛盾時 cache_only 同樣排除。診斷仍標記 adjustment_review，不能把診斷資料用作評分。

快照與 run 保存當次核實的 Instrument；stable UUID、alias 不變。cache document 附
verified Instrument，cache_only 只使用歷史已核實身份，沒有即時 lookup 的承諾。
舊 Yahoo cache 缺 metadata 須完整刷新；舊 snapshots 不變、不改 hash。fixture context
沿用原 synthetic resolver，不能替 Yahoo 放行。

本次完成 session 實測是 2026-09-04、AAPL/MSFT/SPY，3 evaluated、1 candidate；
真實 cache_only/replay 一致。假期／DST／early-close／未完成當日、timeout/429、
增量歷史修訂與 partial failure 是離線 boundary tests；沒有聲稱真實盤中或限流觀察。

報告的結構化 explanations 與 CSV 欄位分開 symbol warnings/reasons、selected-window
reasons 和全部窗口 gate failures，避免只看 symbol-level reasons 遺失主要原因。
空 candidate CSV 的 companion summary 保留 state/counts/warnings；SUCCEEDED 零候選、
PARTIAL、FAILED 零 evaluation 仍是三種不同結果。
