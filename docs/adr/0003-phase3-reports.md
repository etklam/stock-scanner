# ADR 0003 — Bound comparisons and offline reports

Accepted 2026-09-05.

Use existing run JSON documents for comparison bindings and complete comparison results. Bind
before fetching under the existing executor lock, then publish comparison/results together.
An exact-session baseline uses owner/watchlist/config/basis/exact-engine compatibility and
finished_at <= started_at, with stable UUID tie-break. Replay is never a daily baseline. Old
run documents default to explicitly unavailable rather than retroactively selecting a baseline.

ReportService builds transport-neutral chart/report data from persisted results and immutable
snapshots; renderers only format these values. Self-contained Jinja HTML embeds Agg PNGs and
escapes text. Atomic single-file publication preserves old reports after render/write failure.
No report path changes scan state or fetches prices.

CLI history opens SQLite in read-only mode without migration. Explicit init upgrades storage.
Migration 0002 copies legacy cache into provider-keyed tables without deleting legacy data.
This is necessary because fixture and Yahoo can share stable instrument IDs; a provider check
alone prevents mixed reads but cannot prevent one provider overwriting the other's cache.
No strategy semantic, new provider release, HTTP service, queue or identity system is added.
