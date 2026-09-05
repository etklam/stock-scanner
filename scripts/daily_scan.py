#!/usr/bin/env python3
"""Daily scan wrapper for scheduler use (launchd / Task Scheduler / cron / systemd).

Composes existing CLI and HTTP capabilities only; it contains no calendar,
holiday, DST or close-buffer logic — the completed session always comes from
the shared market-calendar service (`qscan sessions` / `GET
/api/v1/sessions/current`), never from the local wall-clock date.

Identity and dedup semantics
----------------------------
- One scan intent is identified by (watchlist UUID, resolved session,
  watchlist revision, provider, attempt). The Idempotency-Key is a SHA-256 of
  that identity, so retries, lost responses and restarts reuse the SAME key
  and byte-identical request body; `--force` is the only thing that bumps the
  attempt and creates a new intent (and its retries keep that new key).
- A watchlist revision change produces a new intent: edited lists are
  re-scanned by design. Re-running the identical intent does not: SUCCEEDED
  exits 0, PARTIAL keeps partial semantics (exit 3) and is treated as covered,
  FAILED stays exit 1 with a clear summary — nothing is silently converted to
  success. `--force` re-runs any of them.
- Before submitting, the record is written as SUBMITTED with the key and body.
  A crash after the server accepted the request replays the same key (200
  replay) instead of creating a second run; a previous incomplete attempt is
  resumed by scan id when known.

Path selection
--------------
- `serve` reachable -> HTTP only. After the first submission attempt the
  wrapper NEVER falls back to the CLI: 401/403/409 are configuration/intent
  errors (exit 2), 429 and poll timeouts are busy (exit 4, resumable), and
  network failures retry the same key.
- `serve` not reachable -> standalone CLI scan against the same directory.
  The CLI has no idempotency keys, so the wrapper deduplicates by looking up
  an existing terminal run for (watchlist id, session) first.

State
-----
`<data-dir>/daily-scan-state.json` (versioned) records the last intent per
watchlist UUID. Updates happen under `<data-dir>/daily-scan.lock` — a wrapper
private lock that never touches `executor.lock`, so it cannot deadlock with
the server — and are written to a unique temp file then atomically replaced.
A corrupt state file is quarantined and restarted; because keys are derived
from intent identity (not stored UUIDs), a lost state file can only replay an
earlier attempt's key, never duplicate a run.

Exit codes mirror the CLI: 0 ok (incl. zero candidates), 1 scan FAILED,
2 usage/config (also 401/403/409), 3 PARTIAL, 4 busy/unresolved (429,
timeouts). The bearer token travels only in the Authorization header to
127.0.0.1 — never in URLs, logs or scheduler definitions.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from filelock import FileLock, Timeout

TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED"}
EXIT = {"ok": 0, "failed": 1, "usage": 2, "partial": 3, "busy": 4}
STATE_VERSION = 1
RULESET = "breakout-v1"
_IDEMPOTENCY_MIN = 16  # server-side header constraint


class UsageError(Exception):
    """Bad parameters or configuration: exit 2."""


class BusyError(Exception):
    """Unresolved (429, timeouts, executor busy): exit 4, resumable."""


def log(message: str) -> None:
    print(f"daily-scan: {message}", file=sys.stderr, flush=True)


def fail(code: str, message: str, exit_code: int) -> int:
    log(f"{code}: {message}")
    print(json.dumps({"error": {"code": code, "message": message}}))
    return exit_code


def http_json(
    url: str,
    token: str | None = None,
    payload: dict | None = None,
    timeout: float = 10,
    idempotency_key: str | None = None,
):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urlrequest.Request(url, data=data, headers=headers)
    with urlrequest.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read())


def serve_is_up(port: int) -> bool:
    try:
        status, _ = http_json(f"http://127.0.0.1:{port}/health/live", timeout=2)
        return status == 200
    except (URLError, OSError, ValueError):
        return False


def intent_key(watchlist_id: str, session: str, revision: int, provider: str, attempt: int) -> str:
    """Deterministic identity of one scan intent; stable across processes."""
    material = "|".join((watchlist_id, session, str(revision), provider, RULESET, str(attempt)))
    digest = hashlib.sha256(material.encode()).hexdigest()[:24]
    key = f"daily-{session.replace('-', '')}-{digest}"
    assert len(key) >= _IDEMPOTENCY_MIN
    return key


class StateStore:
    """Per-watchlist last-intent record with atomic, crash-safe writes."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "daily-scan-state.json"
        self.lock = FileLock(data_dir / "daily-scan.lock", timeout=30)
        self.document: dict = {}

    def load(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("version") != STATE_VERSION:
                raise ValueError("unsupported state version")
            self.document = document
        except FileNotFoundError:
            self.document = {"version": STATE_VERSION, "watchlists": {}}
        except (OSError, ValueError) as exc:
            quarantine = self.path.with_suffix(f".corrupt-{int(datetime.now(UTC).timestamp())}")
            try:
                os.replace(self.path, quarantine)
                log(f"state file unreadable ({exc}); quarantined to {quarantine.name}")
            except OSError:
                log(f"state file unreadable ({exc}); starting fresh")
            self.document = {"version": STATE_VERSION, "watchlists": {}}

    def save(self) -> None:
        """Caller holds the lock: read-modify-write cycles are serialized."""
        handle, temporary = tempfile.mkstemp(dir=str(self.path.parent), prefix=".daily-state-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.document, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def resolve_watchlist(watchlists: list, watchlist: str) -> dict:
    """Accept name or UUID; ambiguity or absence is a usage error, never a guess."""
    if watchlist.count("-") == 4 and re.fullmatch(r"[0-9a-fA-F-]{36}", watchlist):
        matches = [w for w in watchlists if w["id"].lower() == watchlist.lower()]
    else:
        matches = [w for w in watchlists if w["name"] == watchlist]
    if len(matches) != 1:
        raise UsageError(f"watchlist {watchlist!r} matched {len(matches)} entries; use the UUID")
    return matches[0]


def _error_body(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:300]
    except OSError:
        return ""


def submit_with_retries(submit, *, deadline: float, retry_sleep: float):
    """Network/429 retries only; 401/403/409 are configuration or intent errors."""
    attempts = 0
    while True:
        attempts += 1
        try:
            return submit()
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise UsageError(f"authentication rejected by the API (HTTP {exc.code})") from exc
            if exc.code == 409:
                raise UsageError(
                    "idempotency conflict: this key was used with a different request"
                ) from exc
            if exc.code == 429 and time.monotonic() < deadline:
                time.sleep(retry_sleep)
                continue
            raise BusyError(f"submission rejected (HTTP {exc.code}): {_error_body(exc)}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            if attempts >= 3 or time.monotonic() > deadline:
                raise BusyError(f"submission did not complete: {exc}") from exc
            time.sleep(retry_sleep)


def run_api_path(arguments, data_dir: Path, store: StateStore) -> int:
    token = json.loads((data_dir / "api-token.json").read_text(encoding="utf-8"))["token"]
    base = f"http://127.0.0.1:{arguments.port}"
    deadline = time.monotonic() + arguments.timeout_seconds

    def get(path: str) -> dict:
        try:
            status, document = http_json(base + path, token=token)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise UsageError(f"authentication rejected by the API (HTTP {exc.code})") from exc
            raise BusyError(f"GET {path} returned HTTP {exc.code}") from exc
        if status != 200:
            raise BusyError(f"GET {path} returned HTTP {status}")
        return document

    try:
        watchlist = resolve_watchlist(get("/api/v1/watchlists"), arguments.watchlist)
        session = get("/api/v1/sessions/current")["as_of_session"]
    except (URLError, OSError, TimeoutError, KeyError, ValueError) as exc:
        raise BusyError(f"cannot reach the API: {exc}") from exc

    with store.lock:
        store.load()
        record = store.document["watchlists"].get(watchlist["id"], {})
        if (
            record.get("session") == session
            and record.get("revision") == watchlist["revision"]
            and record.get("provider") == arguments.provider
            and record.get("state") in TERMINAL
            and not arguments.force
        ):
            state = record["state"]
            log(
                f"session {session} already covered for {watchlist['name']} ({state}); "
                "nothing to do"
            )
            if state == "SUCCEEDED":
                return EXIT["ok"]
            return EXIT["partial"] if state == "PARTIAL" else EXIT["failed"]
        # --force is the only thing that increments the attempt (a new intent);
        # retries and restarts of the same intent reuse its deterministic key.
        attempt = (record.get("attempt", 0) + 1) if arguments.force else 0
        key = intent_key(
            watchlist["id"], session, watchlist["revision"], arguments.provider, attempt
        )
        body = {"watchlist_id": watchlist["id"], "as_of_session": session, "data_mode": "auto"}
        resumed_scan_id = record.get("scan_id") if record.get("key") == key else None
        store.document["watchlists"][watchlist["id"]] = {
            "session": session,
            "revision": watchlist["revision"],
            "provider": arguments.provider,
            "attempt": attempt,
            "key": key,
            "body": body,
            "scan_id": resumed_scan_id,
            "state": "SUBMITTED",
        }
        store.save()

    # Submission intent is durably recorded before the first request: a crash
    # here replays the same key (server-side idempotency) instead of making a
    # second run. The CLI fallback is never used once this record exists.
    scan_id: str | None = resumed_scan_id
    while True:
        if scan_id:
            status_document = get(f"/api/v1/scans/{scan_id}")
            if status_document["state"] in TERMINAL:
                return _finish(store, watchlist["id"], status_document)
            if time.monotonic() > deadline:
                raise BusyError(f"scan {scan_id} did not finish in time; it stays resumable")
            time.sleep(arguments.poll_seconds)
            continue
        try:
            status, document = submit_with_retries(
                lambda: http_json(
                    f"{base}/api/v1/scans",
                    token=token,
                    payload=body,
                    timeout=15,
                    idempotency_key=store.document["watchlists"][watchlist["id"]]["key"],
                ),
                deadline=deadline,
                retry_sleep=arguments.poll_seconds,
            )
        except HTTPError as exc:
            raise BusyError(f"submission rejected (HTTP {exc.code})") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise BusyError(f"submission did not complete: {exc}") from exc
        # 202 accepted-new and 200 idempotent replay both carry the run id.
        scan_id = document["id"]
        with store.lock:
            store.load()
            store.document["watchlists"][watchlist["id"]]["scan_id"] = scan_id
            store.save()


def _finish(store: StateStore, watchlist_id: str, document: dict) -> int:
    state = document["state"]
    log(f"scan {document['id']} finished: {state}")
    with store.lock:
        record = store.document["watchlists"].get(watchlist_id, {})
        record.update(
            {
                "state": state,
                "scan_id": str(document["id"]),
                "finished_at": document.get("finished_at"),
                "counts": document.get("counts"),
            }
        )
        store.document["watchlists"][watchlist_id] = record
        store.save()
    return (
        EXIT["ok"]
        if state == "SUCCEEDED"
        else EXIT["partial"]
        if state == "PARTIAL"
        else EXIT["failed"]
    )


def run_standalone_path(arguments, data_dir: Path, store: StateStore) -> int:
    """Serve is down: synchronous CLI scan, deduplicated against run history.

    The CLI has no idempotency keys, so the whole operation runs under the
    wrapper lock — a concurrent scheduler either waits and then dedups against
    finished history, or times out busy. Never silent: FAILED stays exit 1.
    """
    executable = Path(arguments.qscan).expanduser()
    if not executable.is_file():
        return fail("VALIDATION_ERROR", f"qscan executable not found: {executable}", EXIT["usage"])

    def cli_json(*args: str) -> dict:
        try:
            completed = subprocess.run(
                [
                    str(executable),
                    "--data-dir",
                    str(data_dir),
                    "--provider",
                    arguments.provider,
                    *args,
                ],
                capture_output=True,
                text=True,
                timeout=arguments.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise BusyError(f"qscan {args[0]} timed out; the run stays resumable") from exc
        try:
            return json.loads(completed.stdout)
        except ValueError:
            raise BusyError(
                f"qscan {args[0]} produced non-JSON output (exit {completed.returncode})"
            ) from None

    try:
        session = cli_json("sessions")["as_of_session"]
        watchlists = cli_json("watchlist", "list")
    except BusyError as exc:
        return fail("EXECUTOR_BUSY", str(exc), EXIT["busy"])
    except FileNotFoundError:
        return fail("VALIDATION_ERROR", f"qscan executable not found: {executable}", EXIT["usage"])
    matches = [
        w for w in watchlists if w["name"] == arguments.watchlist or w["id"] == arguments.watchlist
    ]
    if len(matches) != 1:
        return fail(
            "USAGE_ERROR",
            f"watchlist {arguments.watchlist!r} matched {len(matches)} entries",
            EXIT["usage"],
        )
    watchlist = matches[0]

    with store.lock:
        history = cli_json("scans", "list", "--limit", "200")
        candidates = [
            run
            for run in history
            if run.get("watchlist_id") == watchlist["id"]
            and run.get("context", {}).get("as_of_session") == session
        ]
        existing = [run for run in candidates if run["state"] in TERMINAL]
        store.load()
        record = store.document["watchlists"].get(watchlist["id"], {})
        # A run for this session only counts as coverage if the list has not
        # been edited since (revision mismatch -> new intent -> rescan).
        if (
            existing
            and not arguments.force
            and record.get("revision") == watchlist["revision"]
            and record.get("session") == session
        ):
            state = existing[0]["state"]
            log(
                f"session {session} already scanned for "
                f"{watchlist['name']} ({state}); nothing to do"
            )
            return (
                EXIT["ok"]
                if state == "SUCCEEDED"
                else EXIT["partial"]
                if state == "PARTIAL"
                else EXIT["failed"]
            )
        attempt = (record.get("attempt", 0) + 1) if arguments.force else 0
        store.document["watchlists"][watchlist["id"]] = {
            **record,
            "session": session,
            "revision": watchlist["revision"],
            "provider": arguments.provider,
            "attempt": attempt,
            "state": "SUBMITTED",
        }
        store.save()
        log(f"scanning {watchlist['name']} for session {session} (attempt {attempt})")
        document = cli_json(
            "scan",
            "--watchlist",
            watchlist["id"],
            "--as-of",
            session,
            "--data-mode",
            "auto",
            "--format",
            "json",
        )
        state = document.get("state", "FAILED")
        store.load()
        store.document["watchlists"][watchlist["id"]] = {
            **store.document["watchlists"].get(watchlist["id"], {}),
            "scan_id": document.get("id"),
            "state": state,
            "counts": document.get("counts"),
        }
        store.save()
    log(f"scan finished: {state}")
    return (
        EXIT["ok"]
        if state == "SUCCEEDED"
        else EXIT["partial"]
        if state == "PARTIAL"
        else EXIT["failed"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Absolute data directory")
    parser.add_argument("--watchlist", required=True, help="Watchlist name or UUID")
    parser.add_argument("--port", type=int, default=8000, help="Local serve port to probe")
    parser.add_argument("--provider", default="yahoo", choices=("yahoo", "fixture"))
    parser.add_argument("--qscan", default="qscan", help="Absolute path to the qscan executable")
    parser.add_argument("--poll-seconds", type=float, default=20.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument(
        "--force", action="store_true", help="New intent: rescans even a finished session"
    )
    arguments = parser.parse_args()

    data_dir = arguments.data_dir.expanduser()
    if not data_dir.is_absolute():
        return fail("VALIDATION_ERROR", "--data-dir must be an absolute path", EXIT["usage"])
    data_dir = data_dir.resolve()
    if not (data_dir / "qscan.sqlite3").is_file():
        return fail(
            "VALIDATION_ERROR", f"{data_dir} is not initialized; run qscan init", EXIT["usage"]
        )
    if not 1 <= arguments.port <= 65535:
        return fail("VALIDATION_ERROR", "--port must be 1-65535", EXIT["usage"])
    if arguments.poll_seconds <= 0 or arguments.timeout_seconds <= 0:
        return fail(
            "VALIDATION_ERROR", "--poll-seconds/--timeout-seconds must be positive", EXIT["usage"]
        )

    store = StateStore(data_dir)
    # One wrapper operation at a time per data directory. This lock is
    # wrapper-private and never held by serve, so it cannot deadlock with the
    # executor lifetime lock; concurrent schedulers wait here, then dedup.
    try:
        with store.lock:
            if serve_is_up(arguments.port):
                return run_api_path(arguments, data_dir, store)
            return run_standalone_path(arguments, data_dir, store)
    except Timeout:
        return fail(
            "EXECUTOR_BUSY",
            "another scheduler instance holds the state lock; retry later",
            EXIT["busy"],
        )


if __name__ == "__main__":
    raise SystemExit(main())
