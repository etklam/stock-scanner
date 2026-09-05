# API contract draft — Phase 0

`src/qscan/interfaces/api/schemas.py` defines initial watchlist, scan submission, accepted-job,
and error-envelope schemas. These are **draft DTOs**, not running endpoints or completed
authorization. Unknown fields are rejected; client-controlled owner IDs and file paths are
not accepted. Rulesets are server registered. Dates use ISO dates and ratios use decimals.

The target endpoint map and idempotency semantics remain in development-plan.md section 8.
The API implementation, full result DTOs, OpenAPI snapshot, transport error mapping,
body-size limits, authentication, and persisted executor are Phase 4 work. Application
validation will resolve market sessions, symbol identity, watchlist revisions and ownership.
Schema validation alone does not establish these facts.

Phase 2 now implements synchronous application services in `qscan.application.services`, wired by
`qscan.bootstrap.bootstrap`. The shared `Run`, `ScanResult`, `Counts`, and `Watchlist` contracts live
in `qscan.application.contracts`; analyses reuse existing domain models. They retain context,
resolved rules/config hash, watchlist revision/members, input hash, provenance, timings, all symbol
results and candidate ranks. No private snapshot path is exposed. Replay creates a new run with
`source_run_id`; missing/corrupt/incompatible snapshots raise `SCAN_FAILED`, never cache fallback.

The repository derives ownership from trusted `ApplicationContext`, not a request owner field.
Watchlist/run/result queries are owner scoped, with other owners receiving `NOT_FOUND`.
`requested = evaluated + excluded + data_error` and `candidate <= evaluated` are validated.
Short history and unsupported instruments are excluded; valid zero-candidate runs can succeed.
An evaluated run with data errors is PARTIAL; no evaluation or global failure is FAILED.

These are application contracts, not implemented HTTP responses. HTTP server, authentication,
202 submission, idempotency, pagination, worker recovery and public result DTO mapping remain
Phase 4 work. Current services execute synchronously and do not depend on HTTP or Typer.

## Phase 3 shared contracts (HTTP still unimplemented)

`qscan.application.reporting` now provides ReportService and ComparisonService with typed
Report (schema_version 1), ChartSeries, and persisted Comparison/Change contracts. Chart series
include ISO sessions, Close, nullable SMA10/20/50, window_start/window_end and close_resistance.
Run adds backward-compatible comparison=None and price_basis="split_adjusted_close" defaults.
Report.run retains every persisted result and count; top only limits chart/presentation rows.
Report sources/warnings/chart_error/limitation explicitly distinguish synthetic, failed, missing
snapshot and zero-candidate cases. No ORM object or private snapshot path crosses this boundary.

Comparison is bound and saved by ScanService; future clients call ComparisonService.get rather
than computing changes. It includes semantic_version, previous_run_id, sessions, binding, reasons,
and instrument-keyed changes with previous/current analyses. Legacy and replay comparisons are
explicitly unavailable. WatchlistService.lookup/list and ScanQueryService.summaries provide
owner-scoped lookup and bounded history; ReportService.series is also owner-scoped.

CLI and renderer consume these services directly. Report failures use REPORT_ERROR and never
transition a completed scan. A successful query/export operation can succeed while the historical
run state remains PARTIAL or FAILED. HTTP routes, full public pagination, 202/Location, auth,
idempotency and executor recovery remain Phase 4; these are not live HTTP responses.

RefreshResult/RefreshItem expose resolved context, per-instrument available/updated/error/provenance/
warnings and counts without leaking price payloads. WatchlistService.import_named owns explicit
replacement and revision rules; transports only load the bounded input and present the result.

## Phase 3.5 shared contracts (no HTTP implementation)

- `Instrument.instrument_type` also supports `UNVERIFIED`; Yahoo offline import has UNKNOWN
  currency/exchange unless an exchange hint was supplied. Run/snapshot Instrument values describe
  the verified metadata used for that run. Cache metadata is local persistence, not an HTTP API.
- `Report` adds `charts_included` (default true) and `explanations`, keyed by instrument UUID.
  Each explanation separates symbol reasons/warnings, selected-window reasons and all windows'
  availability, eligibility and reasons. Existing `run.results` and default JSON charts remain.
- `ReportService.build(..., include_charts=False)` skips snapshot/chart computation; CLI CSV uses
  this path and emits a companion summary JSON. `series()` remains a single-instrument entry.
- Snapshot decode and engine compatibility are separate: supported schema-1 history can render
  with old engine metadata. Exact replay rejects a different engine or unsupported rules major
  before creating a run. No fallback to cache/current rules.
- Repository resource reads use an actual SQLite read transaction, including readonly queries.
  This provides resource consistency without waiting for the scanner's executor lock.

These are synchronous shared service/DTO changes. Routes, auth, queue, idempotency and startup
recovery remain Phase 4 work, not features of this milestone.
