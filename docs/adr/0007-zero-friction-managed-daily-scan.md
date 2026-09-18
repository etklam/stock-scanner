# ADR 0007: Zero-friction managed daily scan

- Status: Accepted; implemented with stated notification limitations
- Date: 2026-09-18
- Supersedes: the user-supplied-watchlist-only product constraint in the Phase 6A plan

## Context

The ordinary product flow must become `start -> managed S&P 500 universe -> daily
scan -> report and notification -> optional review`. Manual watchlists and the
Bearer-token API remain supported advanced interfaces, but they are no longer
onboarding requirements.

The existing boundaries remain: deterministic close-only rules, one local SQLite
database, one serial executor, immutable finalized input snapshots, mutable review
annotations, loopback-only HTTP, and shared application services for CLI and HTTP.

## Decision

This ADR records the implemented architecture and the remaining release limits.

### Implementation status (2026-09-18)

**Implemented:**

- `qscan start` initializes/upgrades the selected data directory, starts the
  existing loopback API/UI runtime, and opens the UI only after a nonce-bound HMAC
  challenge proves the expected qscan instance and data-directory identity. It may
  reuse an already-running endpoint only after the same proof.
- The browser obtains a 15-minute signed HttpOnly, SameSite=Strict session and an
  in-memory CSRF token. Cookie-authenticated mutations require an exact trusted
  Origin and matching CSRF header. Existing Bearer clients remain supported.
- Migration 0005 and the internal universe service persist immutable English
  Wikipedia S&P 500 snapshots, provenance, a stable managed watchlist, and an
  atomic last-known-good pointer with the seven-day stale policy described below.
- A bounded native desktop notifier adapter reports acknowledged attempt, denied,
  unavailable, or failed outcomes.
- Migrations 0006–0007 add resumable checkpoints, daily identities, automatic
  report publication/latest pointer, and durable notification outcomes.
- `qscan start` runs the coordinator, and the UI defaults to Today with History,
  Settings and Advanced navigation.
- User-level autostart definitions are available through
  `qscan autostart install|status`; installation is explicit.

### Local trust and launch

`qscan start` is the ordinary local launcher. It initializes or upgrades the
platform data directory, starts one local runtime/coordinator, and opens the report-first UI.
A second launch may reuse an instance only after a challenge proves both qscan
identity and association with the same data directory. Provisioning the managed
universe is refreshed when the coordinator accepts the newest completed session.

The browser uses a short-lived signed HttpOnly, SameSite=Strict local session. The
existing Bearer credential is not exposed to the browser. Cookie-authenticated
mutations require an exact configured Origin and a session-bound CSRF header.
Bearer clients retain their existing no-Origin behavior. Frictionless mode may bind
only to loopback. These controls mitigate cross-site web attacks; they do not defend
against a local process that can read the user's qscan data directory.

### Managed universe

The S&P 500 universe is stored as immutable, provenance-bearing snapshots with a
stable managed-list identity. The initial no-key adapter uses English Wikipedia's
[List of S&P 500 companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies)
through the MediaWiki API and records attribution and revision metadata. It is a
public secondary list under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), not an official S&P or
exchange-certified feed and not point-in-time constituent history. Redistribution
must retain required source attribution/share-alike treatment and comply with any
other applicable use limitations.

An update becomes current only after complete validation. Invalid or unavailable
updates preserve the last-known-good snapshot. The default stale limit is seven
calendar days and is displayed honestly; a missing or expired snapshot blocks a
new managed scan instead of creating a successful empty run. Member count is
validated within broad safety bounds, never against an exact 500 or 503. Distinct
share lines remain distinct instruments.

The coordinator calls this service before a new daily acceptance. Consequently the
stale-data block prevents a missing/expired universe from becoming an empty success.

### Daily identity and execution

One completed market session maps to one normal daily job per principal, provider,
rules/config and attempt. Refreshing membership after acceptance does not change
that identity. A forced rerun increments an explicit attempt.

At acceptance the job freezes session, universe snapshot, provider, engine/rules,
calendar version, expected sessions and comparison baseline. Inputs are processed
serially in configurable chunks, initially 50. Each completed chunk is committed as
run-scoped validated input checkpoints. Restart resumes the same run from those
checkpoints. Provider cache remains mutable shared market data and is not evidence
that a run input was checkpointed.

All checkpointed inputs form one immutable final snapshot. Analysis and candidate
ranking run once across the complete universe, preserving deterministic global
ordering. A chunk is never represented as an independent scan or report.

### Report and delivery

Scan execution, report publication and notification delivery have separate durable
states and retry policies. A report is generated from a terminal run and its
immutable snapshot without refetching data, then atomically becomes the latest
report. Notification attempts use a deduplication key and never cause a rescan.
Local delivery is at-least-once attempt semantics; denied permission, unavailable
targets and adapter failure are not reported as sent.

## Consequences

- Existing manual watchlists, CLI scans, Bearer API clients, replay, reviews and
  backups remain supported.
- Managed-universe, execution, report-publication and notification rows live in the
  same SQLite database and are included by normal backup/restore.
- Packaged coordination lives under `src/qscan`; `scripts/daily_scan.py` remains a
  compatibility wrapper for user-configured manual watchlists.
- The runtime cannot scan while the host is off or asleep. On resume/restart it
  catches up only the newest completed unprocessed session.
- Native notification acceptance is not complete: the macOS display command was
  exercised successfully but has no clickable report action, and Windows/Linux
  actions have not been exercised on real hosts.
- Remote notification and public writable deployment remain out of scope.
