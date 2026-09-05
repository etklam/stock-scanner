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
