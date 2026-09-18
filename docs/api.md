# Local HTTP API contract

This document describes the implemented API served by `qscan serve` and `qscan
start`. The generated public contract is [openapi.json](openapi.json). The browser
session and instance-challenge endpoints are intentionally excluded from that
generated contract because they are local runtime mechanisms, not application API.

## Runtime boundary

- The server binds only to loopback. `qscan serve` and `qscan start` reject a
  non-loopback host before creating or modifying the data directory. Multi-worker,
  reload, remote deployment, and public multi-user operation are unsupported.
- `qscan start` initializes or upgrades the selected data directory, starts the
  local server, and opens `/ui/` only after the server proves its qscan instance
  identity and association with that data directory. An already-running instance
  is reused only after the same nonce-based HMAC challenge succeeds.
- The browser automatically establishes a 15-minute signed session. The cookie is
  HttpOnly, SameSite=Strict, and scoped to `/api`; the returned CSRF token remains
  in memory. Cookie-authenticated mutations require both an exact allowlisted
  Origin and `X-CSRF-Token`.
- Bearer authentication remains compatible with existing clients. A native client
  may omit Origin and send `Authorization: Bearer <local-api-token>`. If it sends
  Origin, the same strict allowlist applies. The server emits no CORS allow header.
- Host is limited to configured loopback names/addresses. The trusted Origin list
  is derived from configured loopback listen origins plus explicit
  `serve --dev-origin` values; it is never reflected from request headers.
- These controls mitigate cross-site requests. They do not defend against a local
  process that can read the qscan data directory and its credential file.

`qscan init` creates `<data-dir>/api-token.json` with mode 0600. Existing credential
documents are upgraded in place with stable instance and browser-session secrets;
the Bearer token is not silently rotated. `qscan token-rotate` changes the Bearer
token immediately while preserving principal, instance identity, and the stable
session/challenge secrets.

## Local runtime endpoints

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/auth/session` | Same-origin bootstrap | Issue the browser cookie and return an in-memory CSRF token |
| GET | `/api/v1/instance/challenge?nonce=...` | None; verified by caller | Prove instance ID and data-directory identity with an HMAC bound to the nonce |
| GET | `/health/live` | None | Minimal liveness plus provider identity |
| GET | `/health/ready` | None | Database and executor readiness; does not contact Yahoo |
| GET | `/ui/` | None | Compiled static UI shell |

The session bootstrap rejects absent, null, external, or cross-site Origin/fetch
metadata. The instance response does not expose a secret; callers must already hold
the credential file to verify its proof. `/ui/` is anonymous static content, but
private application requests require either the browser session or Bearer token.
Unknown `/api/*` paths remain JSON errors and are not swallowed by a UI fallback.

## Application endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/watchlists` | List watchlists with symbols and links |
| POST | `/api/v1/watchlists` | Create a watchlist with 1–2,000 symbols |
| GET / PATCH / DELETE | `/api/v1/watchlists/{id}` | Read, rename with `expected_revision`, or delete |
| PUT | `/api/v1/watchlists/{id}/symbols` | Replace all symbols with optimistic revision control |
| GET | `/api/v1/rulesets` | List registered rule sets |
| GET | `/api/v1/sessions/current` | Resolve the latest completed market session |
| POST | `/api/v1/scans` | Submit a scan with `Idempotency-Key`; first acceptance is 202 |
| GET | `/api/v1/scans` | Cursor-paginated runs, optionally filtered by state |
| GET | `/api/v1/scans/{id}` | Lightweight run status and progress |
| GET | `/api/v1/scans/{id}/results` | Cursor-paginated results with stage/candidate filters |
| GET | `/api/v1/scans/{id}/results/{instrument_id}` | Full result details |
| GET | `/api/v1/scans/{id}/series/{instrument_id}` | Snapshot-backed Close/SMA series |
| GET | `/api/v1/scans/{id}/changes` | The comparison fixed for this run |
| GET | `/api/v1/scans/{id}/reviews` | All saved mutable reviews for the run |
| PUT | `/api/v1/scans/{id}/reviews/{instrument_id}` | Create or update one review with revision control |
| GET | `/api/v1/scans/{id}/export?format=csv` | Render snapshot-backed CSV |

GET operations do not fetch market data, create work, or write report files.
Historical series and exports use the run's immutable snapshot; missing or corrupt
snapshots fail explicitly rather than falling back to current cache. Resource access
is principal-scoped, and another principal's resource is reported as 404.

The request-body limit is 1 MB and applies to both declared and streamed size.
OpenAPI and Swagger UI are disabled by default; `--dev-openapi` enables them on the
loopback server. Source checkouts without compiled web assets return a precise build
instruction at `/ui/`; installed wheels include those assets.

## Scan submission

```http
POST /api/v1/scans
Authorization: Bearer <token>
Idempotency-Key: 3f56af32-5f96-4ff1-9d44-0f02be7a1a94
Content-Type: application/json

{"watchlist_id":"b5086dc3-...","ruleset_id":"breakout-v1",
 "as_of_session":"2026-09-04","data_mode":"auto"}
```

Submission performs offline resolution and a short insert of a `QUEUED` row. It
does not fetch prices or wait for execution. Acceptance freezes principal,
watchlist membership and revision, sessions, rules/config hash, engine version,
provider, data mode, and request time. A later watchlist edit does not alter the
accepted job.

Idempotency is scoped by principal, endpoint, and key. The request hash uses the
client's original fields before resolving mutable defaults. Reusing a key with the
same request returns the original run and its real current state; reusing it with a
different request returns 409 `IDEMPOTENCY_CONFLICT`. Keys have no TTL. Queue depth
defaults to 20; a new request above the limit returns 429 `QUEUE_LIMIT_REACHED`,
while a valid retry of an existing key consumes no new slot. FIFO order is
`(requested_at, id)`.

## Run and result semantics

The state machine is `QUEUED -> RUNNING -> SUCCEEDED/PARTIAL/FAILED`.

- `QUEUED`: `started_at` and final counts are null.
- `RUNNING`: progress reports phase, processed symbols, total symbols, and update
  time. Final coverage counts remain null.
- Terminal: `requested = evaluated + excluded + data_error` and
  `candidate <= evaluated`. A valid run with zero candidates is `SUCCEEDED`.

Results are unavailable while queued/running (409 `SCAN_NOT_READY`) and for failed
runs (409 `SCAN_FAILED`). The status endpoint remains readable for diagnostics.
Only successful or partial runs expose normal result collections. Daily comparison
is bound when execution starts and returned as stored, not recalculated on read.

Opaque cursors are HMAC-signed and bind principal, resource, filters, ordering,
scan ID, and sorting-schema version. A cursor from another run, owner, filter, or
schema is rejected.

## Review semantics

A review is mutable annotation data keyed by authenticated principal, run, and
instrument:

```json
{"label":"worth_reviewing","note":"Tight consolidation","expected_revision":2}
```

`label` is `worth_reviewing`, `borderline`, or `not_useful`; notes are limited to
500 characters. Only instruments with a valid evaluation can be reviewed. The
first write may omit `expected_revision`; later writes require the current revision
or return 409 `REVIEW_REVISION_CONFLICT`. Reviews do not modify immutable results,
scores, ranks, or hashes, and replay does not inherit them. Backup/restore includes
reviews, and review-export records that they are mutable at export time.

## Managed daily workflow

Migration 0005 and the universe service publish immutable snapshots
from English Wikipedia's [List of S&P 500
companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies), with
provenance and a seven-day last-known-good policy. This public secondary list is
licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), not an official S&P or
exchange feed and not point-in-time constituent history. `qscan start` refreshes it
before accepting a new daily job; an expired or missing LKG blocks that job.

Authenticated workflow endpoints are `GET /api/v1/automation/status`,
`POST /api/v1/automation/enable`, `POST /api/v1/automation/pause`,
`POST /api/v1/automation/run-now`, and `GET /api/v1/reports/latest`. Daily job,
report publication, latest pointer, and notification attempts are durable and
owner-scoped. Notification delivery is best-effort; native cross-platform live
acceptance and a clickable macOS report action remain unverified/incomplete.

## Errors and response conventions

Dates use `YYYY-MM-DD`; timestamps use UTC ISO 8601. Ratios are decimal JSON numbers
(`0.2` means 20%), missing values are null, and NaN/Infinity are never emitted.
Errors use one envelope:

```json
{"error":{"code":"INVALID_AS_OF_SESSION","message":"...",
          "details":{},"request_id":"dcf93ed8-..."}}
```

The envelope covers validation, domain failures, 404, 405, 413, 429, and sanitized
500 errors. `X-Request-Id` matches `request_id`. Machine-readable codes are defined
by `qscan.domain.models.ErrorCode`; clients must not parse human-readable messages.

Development checks are documented separately in the README. The generated OpenAPI
file must not be edited by hand.
