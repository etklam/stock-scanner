# HTTP API 合約（Phase 4 已實作）

本文件描述 `qscan serve` 提供的已實作本地 API；生成的合約快照見
[openapi.json](openapi.json)（由 `uv run python scripts/openapi_snapshot.py` 產生，
`--check` 模式供 CI 驗證）。可執行 client 範例見
`scripts/http_client_example.py`，只透過 HTTP 完成整條流程，不 import 核心程式、
不讀 SQLite。

## 邊界

- 只綁 loopback（`qscan serve` 預設 `127.0.0.1:8000`），不支援多 worker、reload、
  遠端部署；這是單人本地 API，不是公開多使用者平台。
- 所有 `/api/v1/*` 請求需要 `Authorization: Bearer <local-api-token>`；token 由
  `qscan init` 建立並存於 `<data-dir>/api-token.json`（0600），`qscan token-rotate`
  明確輪換（principal 與 ownership scope 不變；serve 運行中輪換即時生效）。
  token 不放 query string／log／報告。
- 帶 `Origin` 的請求依嚴格 allowlist 驗證。allowlist 只由**受信任 server 配置**
  形成：本 server 自己的 loopback listen origins（`http://127.0.0.1:<port>`／
  `http://localhost:<port>`）加上運維明確傳入的 `serve --dev-origin`（例如
  Vite dev server），**不反射 request 的 Host/Origin**。同 origin 瀏覽器
  POST 因此通過（本地 UI 正常使用），外部或 `Origin: null` 一律 403；
  不輸出任何 CORS response header，不使用 `*`。原生 client 無 Origin
  但必須帶 token。Host 限 `127.0.0.1[:port]`／`localhost[:port]`，其他 403。
- `/health/live`、`/health/ready` 免認證，只回最少狀態（live 附 `provider`
  欄位，供排程 wrapper 核對 `--provider` 與 server 一致）；ready 以 DB 連線與
  executor thread 存活判定，不連 Yahoo。OpenAPI UI 預設關閉；
  `--dev-openapi` 才在 loopback 開啟 `/openapi.json` 與 `/docs`。
- `/ui/` 掛載**編譯後靜態 UI**（wheel 內建 `qscan/interfaces/web/dist`；
  source checkout 缺 build assets 時回 404 並附 `npm run build` 提示）。
  純 HTML/JS/CSS shell 可匿名載入；**所有 `/api/*` 業務請求仍需 bearer
  token**，且 `/api` 的 404 一律係 JSON error envelope，唔會被 SPA fallback
  吃成 200 HTML。UI 無 history-API fallback：未知 `/ui/*` asset 由
  StaticFiles 回 404。
- Request body 上限 1 MB（同時檢查 Content-Length 與實際讀取位元組數，413）。
  未來 App 的公開部署另有身份／TLS／授權要求，見 development-plan 第 11.2 節。

## Endpoints

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/health/live` | 200 `{"status":"alive"}` |
| GET | `/health/ready` | 200 / 503（database、executor 狀態） |
| GET | `/api/v1/watchlists` | 名單列表（含 symbols、links） |
| POST | `/api/v1/watchlists` | 建立（1–2000 symbols，201） |
| GET / PATCH / DELETE | `/api/v1/watchlists/{id}` | 讀取／改名（需 `expected_revision`）／刪除 |
| PUT | `/api/v1/watchlists/{id}/symbols` | 整份替換，revision 衝突回 409 `WATCHLIST_VERSION_CONFLICT` |
| GET | `/api/v1/rulesets` | 已註冊 ruleset（id/version/windows/minimum_history） |
| POST | `/api/v1/scans` | 提交掃描（需 Idempotency-Key），202 + Location |
| GET | `/api/v1/scans` | cursor 分頁，`created_at DESC, id DESC`；可 `state` 過濾 |
| GET | `/api/v1/scans/{id}` | 輕量狀態（見下），不含 results/rules/比較 payload |
| GET | `/api/v1/scans/{id}/results` | cursor 分頁；`score DESC, instrument_id ASC`，null score 最後；`stage`／`candidate` 過濾 |
| GET | `/api/v1/scans/{id}/results/{instrument_id}` | 單一結果完整內容（features、分數構成、reasons、windows） |
| GET | `/api/v1/scans/{id}/series/{instrument_id}` | 該 run snapshot 的 Close/SMA10/20/50；`limit` 預設 126、最大 504；均線先以足夠歷史計算再裁切顯示 |
| GET | `/api/v1/scans/{id}/changes` | 已固定的 comparison（不重新選 baseline） |
| GET | `/api/v1/scans/{id}/reviews` | 該 run 全部已保存人工標記（批量一個 query） |
| PUT | `/api/v1/scans/{id}/reviews/{instrument_id}` | 保存／更新一個標記（`label`＋`note≤500`＋`expected_revision`）；409 `REVIEW_REVISION_CONFLICT` |
| GET | `/api/v1/scans/{id}/export?format=csv` | 直接回 CSV 內容（UTF-8 BOM，與 CLI 同 renderer）；`all_results=true` 含全部分類；`X-Scan-State`、`X-Result-Count` headers 保留空結果語義，不放假 ticker |

GET 一律唯讀：不下載行情、不建立任務、不寫報告檔案；歷史 series/export 只讀該
run 的 snapshot，缺失／損壞明確回錯，不退回最新 cache。cursor 為 HMAC 簽名的
opaque token，綁定資源、principal、filter、排序與 **scan id + 排序 schema 版本**；
跨 owner／跨 run／篡改／不符的 cursor 回 400（Phase 4.1 起 results cursor 綁定
所屬 run——A run 的 cursor 用在 B run 會被拒，舊版 cursor 一律失效需重新取得第一頁）。
所有資源查詢均 owner-scoped，其他 principal 的資源一律 404，不洩漏
存在性。OpenAPI 契約（`docs/openapi.json`）與實際行為由
`scripts/openapi_snapshot.py --check` 驗證（本地 gate `scripts/check.py` 的一步）：
202=`ScanAccepted`、同 key 重試=200 `ScanStatusOut`、changes=`Comparison`、
export=`text/csv`、422=`ErrorEnvelope`。

## 提交掃描

```http
POST /api/v1/scans
Authorization: Bearer <token>
Idempotency-Key: 3f56af32-5f96-4ff1-9d44-0f02be7a1a94
Content-Type: application/json

{"watchlist_id": "b5086dc3-...", "ruleset_id": "breakout-v1",
 "as_of_session": "2026-09-04", "data_mode": "auto"}
```

- 提交只做離線解析與短交易寫入 `QUEUED` row；不 fetch、不等掃描。
- 接受時固定：principal、watchlist 成員與 revision、resolved
  as_of/reference session、完整 rules config 與 config hash、engine version、
  provider、data mode、requested_at。之後名單修改不影響已接受任務；worker 只
  補齊已驗證 metadata，不重讀目前名單。
- **冪等**：scope 為 principal + endpoint + key。`request_hash` 對 client 原始
  欄位計算（`as_of_session` 未傳保持 null），先查既有 key 再解析可變預設值。
  同 key 同請求 → 回原 run（首次 202 + Location；重試 200 + 真實狀態，不把
  已完成任務假裝 QUEUED）；同 key 不同請求 → 409 `IDEMPOTENCY_CONFLICT`。
  key 隨 run 保留、無 TTL；主動重跑用新 key。並發由 owner+key unique index
  與單一 `BEGIN IMMEDIATE` 交易保證，並發同 key 只會建立一個 job。
- **Queue**：QUEUED 上限預設 20（`--queue-limit`），超限 429
  `QUEUE_LIMIT_REACHED`；已存在的合法冪等重試不佔名額。FIFO 依
  `(requested_at, id)`。

## 狀態與結果語義

狀態機 `QUEUED → RUNNING → SUCCEEDED/PARTIAL/FAILED`：

- QUEUED：`started_at` null、`counts` null——不假稱任何標的已 data_error。
- RUNNING：`started_at` 已固定；`progress` 提供 `phase`
  (market_data/analysis/publication)、`processed_symbols`、`total_symbols`、
  `updated_at`；`counts` 維持 null 直到發布（進度與最終 coverage 分離）。
- Terminal：`counts` 符合 `requested = evaluated + excluded + data_error`、
  `candidate <= evaluated`；有效資料零候選是 `SUCCEEDED`；不以 100% processed
  推論成功。

結果讀取：QUEUED/RUNNING → 409 `SCAN_NOT_READY`；FAILED → 409 `SCAN_FAILED`
（status endpoint 仍可讀 diagnostics）；只有 SUCCEEDED/PARTIAL 公開正常結果，
不用空 array 假扮「全部失敗但零候選」。daily baseline 在 job 開始執行時固定，
`changes` 回傳已發布的 comparison。

## 人工覆核標記（reviews；Phase 6A）

每個 run＋instrument 可保存一個最小人工覆核標記，scope 為
**authenticated principal + run ID + instrument ID**：

```json
{"label": "worth_reviewing", "note": "整理收窄，值得開圖", "expected_revision": 2}
```

- `label` ∈ `worth_reviewing / borderline / not_useful`；`note` ≤500 字；
  未標記 ≠ 唔值得睇。
- **可變資料**，獨立 table（migration 0004 `scan_reviews`），與 immutable
  scan results/snapshots 完全分離——保存／更新不改寫 score、rank、任何 hash；
  標記不會自動套到第二日，exact replay 唔會繼承原 run 標記（新 run 由零開始）。
- 只可標記**有有效 evaluation** 的標的（excluded／data-error 回 400）；
  其他 principal 的 run 一律 404。
- revision optimistic concurrency：首次保存可省略 `expected_revision`；
  之後必須帶目前 revision，不符（包括盲寫）回 409
  `REVIEW_REVISION_CONFLICT`，唔會靜默覆蓋。
- `GET .../reviews` 一次批量回全部標記（列表 UI 每行一個 request）。
- 備份／還原包含 reviews（單一 DB）；`scans review-export` CSV 會併入已保存
  標記並注明匯出時間——標記屬可變資料，唔冒充原 scan snapshot。

## 慣例

- 日期 `YYYY-MM-DD`；timestamp UTC ISO 8601；價格／比率為 JSON number 小數
  （0.2 = 20%）；缺失為 null；不輸出 NaN/Infinity。
- 錯誤統一 envelope：

```json
{"error": {"code": "INVALID_AS_OF_SESSION", "message": "...",
           "details": {}, "request_id": "dcf93ed8-..."}}
```

覆蓋 Pydantic validation（422）、domain errors、404、405（`METHOD_NOT_ALLOWED`）、
413、429 與未預期失敗（500，不含 stacktrace／原始 exception／私有路徑）；
`X-Request-Id` header 與 `request_id` 對應。machine-readable code 清單見
`qscan.domain.models.ErrorCode` 與 openapi.json；App 不需解析人類訊息。
