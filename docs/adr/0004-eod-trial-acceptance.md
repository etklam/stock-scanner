# ADR 0004 — Yahoo completed-session EOD trial

2026-09-05，accepted for personal EOD trial。此決定收窄原 Phase 0 的來源放行條件，
不宣稱 production、實時／盤中或交易所認證行情。沒有改策略公式或門檻。

## 決定及證據

集中狀態在 `adapters/provider_release.py`：本次接受 yfinance 0.2.66、pandas 2.3.3、
NumPy 2.5.2，與 uv.lock 相符時為 `EOD_TRIAL`；版本不符回到 `BLOCKED`。
adapter、doctor、CLI 讀取同一決定；probe 本身不能改 gate，也沒有 trust/skip 選項。
安裝未鎖定版本的 wheel 依賴可能受阻；用本 repository 的 `uv sync --locked` 可重現環境。

[實際觀察](../phase35-provider-acceptance.json) 在 2026-09-05 12:26 UTC 執行，
只保存數個事件觀察值及 metadata，沒有提交第三方價格歷史。

| 項目 | 證據與判定 |
| --- | --- |
| Close 口徑 | `auto_adjust=False, back_adjust=False, repair=False`；鎖定 yfinance `utils.parse_quotes` 直接映射 vendor quote.close，adjclose 另存，adapter 只取 Close |
| 拆股 | Apple 官方 2020-07-30 公告 4:1，2020-08-31 起調整交易；銀行研究記錄 8/28 未調整收市 499.23。期望 499.23/4=124.8075，實測 124.8075027466，誤差 <0.0001。未再乘除 split factor |
| 除息 | Apple 官方宣告每股 .25；Yahoo 2024-05-10 事件 .25。5/9 Close=184.5700073，對照當日公開收市 184.57。Adj Close/Close 的前後乘數比 .9986454918，期望 (184.57-.25)/184.57=.9986455004，誤差 <1e-6；沒有把總回報 Adj Close 當 Close |
| frame/end | 實測 AAPL 單 ticker、AAPL/MSFT 多 ticker 及 exclusive end；flat/兩種 MultiIndex/部分空欄另有 offline tests |
| metadata | 實測 AAPL/MSFT NMS、USD、EQUITY、New York；SPY PCX、USD、ETF、New York。symbol 必須吻合，exchange whitelist + currency + timezone + type 全部吻合才接受 |
| cutoff | application 在網絡前解析已完成 session（收市+30分鐘），validation 丟棄目標之後 rows。假期、DST、early close、未完成當日用 fixed-clock boundary tests；本次週末執行，**沒有真實盤中觀察** |
| 故障 | timeout→retry、空資料、部分 ticker、最大兩並行用 injected offline tests；沒有故意觸發真實 429。最多三次可配置 attempt，預設兩次；每次 metadata/price request 各有 timeout，並非整次操作 wall-clock SLA |
| cache/replay | 實際三標的 refresh→scan→三種報告→cache_only→replay 全部 exit 0，3 evaluated / 1 candidate，結果與 hash 一致，見 live CLI artifact。增量交疊、歷史修訂、split/full refresh/quarantine 是 deterministic offline tests；未宣稱本次觀察到真實歷史修訂 |

來源（本次查閱，不採用其投資建議）：

- [Apple 官方拆股公告](https://www.apple.com/newsroom/2020/07/apple-reports-third-quarter-results/)。
- [First Citizens 銀行研究：8/28 原價 499.23](https://www.firstcitizensgroup.com/tt/news-insights/a-closer-look-at-stock-splits/)。僅使用明確的 8/28 close，沒有採用文章對翌日 opening 的概括。
- [Apple 官方股息公告](https://www.apple.com/newsroom/2024/05/apple-reports-second-quarter-results/)：.25、5/13 record、5/16 payable；5/10 ex-date 為 vendor event，不能把公告的 record date 當 ex-date。
- [當日收市對照](https://stockinvest.us/stock-news/apple-inc-aapl-shows-bullish-signals-despite-overbought-conditions)：5/9 close 184.57；屬外部研究數值對照，非交易所原始 tape。
- [Yahoo 歷史欄位](https://finance.yahoo.com/quote/AAPL/history/)、[鎖定 yfinance 原始碼](https://github.com/ranaroussi/yfinance/blob/0.2.66/yfinance/utils.py)。

## 為何調整條件

原 gate 把「盤中觀察」與 EOD 所需條件綁在一起，會在週末永久阻塞已完成日線試用。
本次只放行 completed-session application 路徑：calendar cutoff 和 future-row trimming 可
獨立驗證；不對盤中 provider 行為作推論。真實盤中觀察留作擴大能力前的證據，不能以 mock 代替。
價格事件、原價數值對照、vendor 欄位映射三者共同支持有限 EOD 試用；兩個事件無法证明
所有標的／公司行動都正確。資料仍可修訂、延遲或限流，runtime quality gate 繼續有效。
公開產品的再分發授權、SLA、完整行情交叉核對不在本次放行範圍。

## metadata 與相容性

離線 import 只確立 UUID／alias／使用者 exchange hint，Yahoo instrument 為 UNVERIFIED，
無 hint 時 exchange/currency UNKNOWN。下載前透過 Yahoo chart metadata 核實；PCX/BTS
歸入使用同一 session 日曆的 NYSE 市場群組，不聲稱 listing venue 是 NYSE 本所。
舊預設 NYSE 的 EQUITY 記錄可經新 metadata 校正，UUID 保持不變；新明確 hint 若矛盾則拒絕。
拒絕新 metadata 時 invalidate cache，不能回退至曾驗證的市場身份掩蓋矛盾。

使用鎖定 yfinance 的私有 `YfData.get`，限定 Yahoo chart URL、5d/1d 和 request timeout，
避免公開 `get_history_metadata()` 隱藏觸發 1h history。私有介面升版須重新驗收；
yfinance 內部 cookie/crumb 行為不等於本專案有嚴格的總耗時截止。

verified metadata 與 cache document、當次 run/snapshot 一起保存；使用者原名單保留輸入 hint，
不在 fetch 時偷偷增加 revision。舊 Yahoo cache 沒有驗證 metadata 必須 refresh，不能當作
可信 cache_only 來源。已有 snapshots 不遷移、不改 hash；歷史讀取與 replay 不做 metadata IO。
