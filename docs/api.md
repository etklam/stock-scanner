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
