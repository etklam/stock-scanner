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
