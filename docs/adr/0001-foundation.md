# ADR 0001 — Close-only contracts and local execution

Date: 2026-09-05. Status: accepted for the V1 implementation baseline.

## Decisions

- Domain inputs contain instrument ID, ordered session dates, positive finite Close values,
  and `split_adjusted_close` basis only. Provider metadata is validated outside strategy
  calculations. Core has no clock, environment, network, filesystem, CLI, HTTP, or DB access.
- The target basis adjusts splits without dividend reinvestment. Yahoo `Close` is a candidate
  source, not proof of this basis. Never substitute `Adj Close`, apply another split factor,
  forward-fill gaps, or silently repair prices. Online release remains blocked until the
  installed provider passes the documented split/dividend review.
- One process and one serial persisted executor use a local SQLite database. Short write
  transactions exclude network waits. Immutable input snapshots preserve historical runs.
  Persistence and execution are Phase 2/4 work, not implemented by this ADR.
- CLI and API call the same application services; transport models do not expose ORM models.
  Small protocols are introduced when an actual implementation boundary needs them.
- Python 3.12 is the first acceptance target. Commit the uv lock; verify installation on each
  OS rather than treating cross-platform resolution as evidence of runtime support.

## Consequences

Synthetic fixtures can unblock core development while live data remains unverified.
There is no GUI, broker integration, multiuser platform, generic plugin framework, or queue
service in the baseline. Pydantic provides immutable validated contracts; core computations
may depend on these contracts but must not import transport or persistence libraries.
