# Breakout v1 規則實作

規則版本 `1.0.0`。參數定義見 `src/qscan/domain/rules.py`；完整公式及分段見
[開發計劃 §5](development-plan.md#5-v1-篩選規則可解釋可測試可修改)。這些是未經績效驗證的初篩參數。

入口為 `qscan.core.analyze_symbol(series, rules, context)`，只接收日期與正數有限 Close。
呼叫端須先按交易所日曆驗證連續 sessions，再裁切到 `as_of_session`；核心拒絕未來資料，
缺少目標或 reference session 會輸出資料錯誤。Phase 2 application 已加入日曆連續性驗證，
缺口裁切及 cache 行為見 [data quality](data-quality.md)。

- `t` 是目標 session，`u=t-1`；10／20／40 日窗口全部截止於 `u`。
- 回報為 `C_u / C_(u-k) - 1`；前段升幅為窗口首日 Close 除以前 63 sessions 最低 Close 減一。
- 最少 80 sessions；窗口分別需 74／84／104 筆，126 日回報需 128 筆。
- SMA 包含 `t`；SMA20 斜率比較相隔五個 sessions 的兩組平均。
- 收窄比較截至 `u` 最近五個 log returns 與之前二十個、不重疊的 returns，標準差採 `ddof=0`。
  分母小於 config epsilon 時為 null 並附 `UNDEFINED_CONTRACTION`。
- 每個窗口獨立檢查動量、區間、淨變幅及支撐 gate；只為合格窗口評五項分數，上限合共 100。
- 分段取最高適用分數；比較容差固定為 `1e-12`，不先四捨五入。
- Stage 依次判定 EXTENDED、CLOSE_BREAK_ABOVE、NEAR_CLOSE_RESISTANCE、FORMING。
- 先選非 extended 窗口，再按最高分及 `20 → 10 → 40` 決勝；其他窗口完整保留。
- 候選按分數降序、instrument UUID 升序排名；混合 config 或重複 instrument 拒絕排名。
- 任意連續 20 筆同價隔離為 `SUSPICIOUS_FLAT_SERIES`；流動性固定 `NOT_EVALUATED`。

`tests/unit/test_scanner.py` 的固定樣本為 88 筆 `50 + 0.5*i`，接 40 筆 99／100 交替，
最後 Close 為 100。預期主窗口 20、阻力 100、stage NEAR_CLOSE_RESISTANCE、總分 86：
動量 18、趨勢 15、結構 25、收窄 13、接近參考線 15。分數不表示成功率。

結果保存 context、config hash、共同及各窗口特徵、分項分數、原因與 unavailable 狀態。
Phase 2 已加入 fixture／受阻擋的 Yahoo adapter、資料快取及持久化；核心公式未改。
CLI scan 與 HTTP handlers 仍未實作。
