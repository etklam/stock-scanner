# Phase 6A.1 current status matrix

Status: Phase 6A.1 implementation is present. Local macOS release acceptance
passed on 2026-09-17; Windows and Linux remain unverified.

| Area | Current support / acceptance | Latest verification | Unverified or blocked |
| --- | --- | --- | --- |
| Result explanations | Candidate stage reason, selected-window reasons, alternative-window gate failures, data warnings, and unavailable features are shown separately. | `web/src/screens/Detail.test.tsx`; full local gate passed. | Browser rendering on other platforms remains unverified. |
| Filters | `candidates` remains candidate-only; `all` is labelled and documented as all result categories, including data errors. Stage filters remain candidate-stage filters. | API query regression plus the full local Playwright flow. | Other browser/platform combinations remain unverified. |
| Series errors | A series error is shown beside the result detail and never replaces saved reasons or diagnostics. | Source regression test plus Playwright E2E. | Browser rendering on other platforms remains unverified. |
| Generated TS contract | Release frontend gate runs `npm run check:contract` using a cross-platform Node script. | `npm run check:contract` passed in the full gate. | Other Node/platform combinations remain unverified. |
| Browser E2E | Release frontend gate runs `npm run test:e2e`; Playwright/Chrome absence is a failure, not a skip. The fixture server clock is pinned to its synthetic data date. | 4/4 Playwright tests passed locally. | Other browser/platform combinations remain unverified. |
| Cross-platform gate | `scripts/check.py --fast` is dev-only; full `scripts/check.py` includes contract, build, and E2E, plus runtime preflight. | Existing gate structure retained. | Windows/Linux remain unverified until manually run. |

## Verification record

- Baseline: `814c0f2153b5f8f73b3b816799b6f4eb2d227e55`.
- The working tree is based on that baseline and was dirty during verification;
  no commit was created by this implementation pass.
- Local verification command: `uv run python scripts/check.py` (exit 0), which
  ran 280 offline Python tests, 39 frontend unit tests, contract/build/E2E,
  wheel build, and installed-wheel CLI/HTTP/backup smoke.
- Runtime: macOS 27.0 arm64, Python 3.12.14, SQLite 3.53.4 (WAL-reset status
  OK), Node v26.4.0, npm 11.17.0, Google Chrome 152.0.7977.84.
- This does not reuse historical Phase 6A green results as evidence. A package
  audit is not an embedded SQLite runtime audit, and this local result does not
  establish Windows/Linux support.
