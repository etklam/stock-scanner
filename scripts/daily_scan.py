#!/usr/bin/env python3
"""Daily scan wrapper for scheduler use (launchd / Task Scheduler / cron / systemd).

Composes existing CLI and HTTP capabilities only; it contains no calendar,
holiday, DST or close-buffer logic — the completed session always comes from
the shared market-calendar service (`qscan sessions` / `GET
/api/v1/sessions/current`), never from the local wall-clock date.

Pending intents come first
--------------------------
- One scan intent is identified by (watchlist UUID, resolved session,
  watchlist revision, provider, attempt); the Idempotency-Key is a SHA-256 of
  that identity. A saved submission whose outcome is unknown (state SUBMITTED)
  is an independent, immutable pending intent: it is resumed with its ORIGINAL
  key, ORIGINAL request body and ORIGINAL scan id — never rebuilt from today's
  session/revision, and a renamed or re-revised watchlist cannot block the
  recovery because resumption reads only the state file plus the saved scan id.
- `--force` while an intent is pending is refused (exit 4): forcing would
  orphan an intent that the server may already have accepted. Resolve the
  pending intent first; forcing a finished session afterwards works as before.
- After the pending intent reaches a terminal state, the wrapper re-evaluates
  coverage for the current session and creates a new intent only if needed.
- If the API is unreachable while an API intent is pending, the wrapper does
  NOT fall back to the CLI: the server may already have accepted the saved
  key. It exits 4 (busy, resumable) instead.
- Plain retries of an unfinished force intent keep its original attempt (and
  therefore its original key); only an explicit `--force` on a RESOLVED intent
  increments the attempt and creates a new one.
- Provider identity is taken from the running server (`/health/live`), never
  from the flag alone: a `--provider` flag that disagrees with the serve
  process is a configuration error (exit 2), not a silently deduplicated run.
- A missing or corrupt state file is quarantined and reported loudly. Because
  keys are derived from intent identity, a lost record can only replay an
  earlier attempt's key, never fabricate a duplicate; the wrapper does not
  claim exact deduplication from an unknown state, it fails safe and says so.

Path selection
--------------
- `serve` reachable -> HTTP only. After the first submission attempt the
  wrapper NEVER falls back to the CLI: 401/403/409 are configuration/intent
  errors (exit 2), 429 and poll timeouts are busy (exit 4, resumable — the
  intent stays pending, never written as a terminal FAILED), and network
  failures retry the same key.
- `serve` not reachable and nothing pending -> standalone CLI scan against the
  same directory. The CLI has no idempotency keys, so the wrapper deduplicates
  by looking up an existing terminal run for (watchlist id, session) first.
  CLI subprocess results are validated by exit code AND document shape; an
  error envelope is never treated as a run document.

Output contract
---------------
stdout carries exactly one JSON document (a result summary, or an error
object); every diagnostic goes to stderr; bearer tokens never appear in any
output. Exit codes mirror the CLI: 0 ok (incl. zero candidates), 1 scan
FAILED, 2 usage/config (also 401/403/409, token/state problems), 3 PARTIAL,
4 busy/unresolved (429, timeouts, pending + server unreachable). The token
travels only in the Authorization header to 127.0.0.1 — never in URLs, logs
or scheduler definitions.
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
_UUID = re.compile(r"[0-9a-fA-F-]{36}")
# Exit codes a finished `qscan scan` may legitimately return with a run document.
_CLI_SCAN_EXITS = frozenset({0, 1, 3})


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


def state_exit(state: str) -> int:
    if state == "SUCCEEDED":
        return EXIT["ok"]
    return EXIT["partial"] if state == "PARTIAL" else EXIT["failed"]


def summary(**fields: object) -> dict:
    keep = ("action", "watchlist", "session", "state", "scan_id", "attempt", "key")
    return {name: fields[name] for name in keep if fields.get(name) is not None}


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


def load_token(data_dir: Path) -> str:
    """Read the local API token; unreadable/invalid files are a usage error."""
    try:
        token = json.loads((data_dir / "api-token.json").read_text(encoding="utf-8"))["token"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UsageError(f"API token unavailable or invalid ({exc}); run qscan init") from exc
    if not isinstance(token, str) or not token:
        raise UsageError("API token file has no token; run qscan init")
    return token


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
                log(
                    f"state file unreadable ({exc}); quarantined to {quarantine.name}. "
                    "Dedup guarantees are reduced until the next intent completes: keys are "
                    "deterministic, so at worst an earlier attempt's key is replayed — never a "
                    "silently duplicated run. Inspect the quarantined file before deleting it."
                )
            except OSError:
                log(f"state file unreadable ({exc}) and could not be quarantined; starting fresh")
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


def find_pending(store: StateStore, watchlist: str) -> tuple[str, dict] | None:
    """Locate a recorded API intent whose outcome is unknown (SUBMITTED + key).

    Matched by watchlist UUID or by the name recorded when the intent was
    created, so a rename since then cannot orphan it. The lookup reads only
    the state file: it never contacts the API and never rebuilds the intent.
    """
    for identity, record in store.document["watchlists"].items():
        if record.get("state") != "SUBMITTED" or not record.get("key"):
            continue
        if record.get("watchlist_name") == watchlist or identity.lower() == watchlist.lower():
            return identity, record
    return None


def resolve_watchlist(watchlists: list, watchlist: str) -> dict:
    """Accept name or UUID; ambiguity or absence is a usage error, never a guess."""
    if _UUID.fullmatch(watchlist):
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


def _finish(store: StateStore, identity: str, document: dict) -> None:
    """Record a terminal outcome, but never stamp it onto a different intent."""
    with store.lock:
        store.load()
        record = store.document["watchlists"].get(identity)
        if record is None:
            log(f"state record for {identity} disappeared; outcome kept in run history")
            return
        if record.get("scan_id") not in (None, str(document["id"])):
            log(
                f"record moved to another intent (scan {record.get('scan_id')}); "
                f"not overwriting it with the outcome of {document['id']}"
            )
            return
        record.update(
            {
                "state": document["state"],
                "scan_id": str(document["id"]),
                "finished_at": document.get("finished_at"),
                "counts": document.get("counts"),
            }
        )
        store.save()


def await_intent(
    arguments, base: str, token: str, deadline: float, store: StateStore, identity: str
) -> str:
    """Drive one recorded intent (saved key+body+scan id) to a terminal state.

    Submits the SAVED body with the SAVED key when the outcome is unknown:
    server-side idempotency makes that a replay of the original intent, never
    a second run. A timeout leaves the record SUBMITTED (pending, exit 4) —
    it is never rewritten as a terminal FAILED.
    """
    while True:
        with store.lock:
            store.load()
            record = store.document["watchlists"].get(identity, {})
        key, body = record.get("key"), record.get("body")
        if not key or not isinstance(body, dict):
            raise UsageError(
                f"pending record for {identity} has no usable key/body; resolve it "
                "manually via the API history before scheduling again"
            )
        scan_id = record.get("scan_id")
        if scan_id:
            status_document = get_status(base, token, scan_id)
            if status_document["state"] in TERMINAL:
                _finish(store, identity, status_document)
                return str(status_document["state"])
            if time.monotonic() > deadline:
                raise BusyError(
                    f"scan {scan_id} did not finish in time; the pending intent stays resumable"
                )
            time.sleep(arguments.poll_seconds)
            continue

        def submit(key: str = key, body: dict = body) -> tuple[int, dict]:
            return http_json(
                f"{base}/api/v1/scans",
                token=token,
                payload=body,
                timeout=15,
                idempotency_key=key,
            )

        try:
            _, document = submit_with_retries(
                submit,
                deadline=deadline,
                retry_sleep=arguments.poll_seconds,
            )
        except HTTPError as exc:
            raise BusyError(f"submission rejected (HTTP {exc.code}): {_error_body(exc)}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise BusyError(f"submission did not complete: {exc}") from exc
        # 202 accepted-new and 200 idempotent replay both carry the run id.
        with store.lock:
            store.load()
            store.document["watchlists"][identity]["scan_id"] = str(document["id"])
            store.save()


def get_status(base: str, token: str, scan_id: str) -> dict:
    try:
        status, document = http_json(f"{base}/api/v1/scans/{scan_id}", token=token)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise UsageError(f"authentication rejected by the API (HTTP {exc.code})") from exc
        if exc.code == 404:
            raise BusyError(
                f"recorded scan {scan_id} is unknown to the server (deleted or different "
                "data directory); resolve the state file manually"
            ) from exc
        raise BusyError(f"status poll returned HTTP {exc.code}") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise BusyError(f"cannot reach the API: {exc}") from exc
    if status != 200 or "state" not in document:
        raise BusyError(f"status poll returned an unusable document for {scan_id}")
    return document


def run_api_path(
    arguments, data_dir: Path, store: StateStore, pending: tuple[str, dict] | None
) -> tuple[int, dict]:
    token = load_token(data_dir)
    base = f"http://127.0.0.1:{arguments.port}"
    deadline = time.monotonic() + arguments.timeout_seconds

    def get(path: str) -> object:
        try:
            status, document = http_json(base + path, token=token)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise UsageError(f"authentication rejected by the API (HTTP {exc.code})") from exc
            raise BusyError(f"GET {path} returned HTTP {exc.code}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise BusyError(f"cannot reach the API: {exc}") from exc
        if status != 200:
            raise BusyError(f"GET {path} returned HTTP {status}")
        return document

    def resume(identity: str, record: dict) -> None:
        log(
            f"resuming pending intent for {record.get('watchlist_name', identity)} "
            f"(key {record['key']}, scan {record.get('scan_id') or 'unknown'})"
        )
        state = await_intent(arguments, base, token, deadline, store, identity)
        log(f"pending intent finished: {state}")

    # 1. Provider identity comes from the actual server, never the flag alone:
    #    a mismatched --provider is a configuration error, not a silent dedup.
    server = get("/health/live")
    server_provider = server.get("provider") if isinstance(server, dict) else None
    if server_provider and server_provider != arguments.provider:
        raise UsageError(
            f"serve runs provider {server_provider!r} but the wrapper was given "
            f"--provider {arguments.provider!r}: refusing to submit or deduplicate "
            "against a mismatched provider"
        )

    action = "covered"
    # 2. A pending intent found offline (by saved name or UUID) resumes before
    #    anything is re-resolved: its own key/body/scan id are the only inputs.
    if pending is not None:
        resume(pending[0], pending[1])
        action = "resumed"

    # 3. Resolve the requested list; a pending record keyed by its UUID (the
    #    list may have been renamed since) still resumes first and is never
    #    rebuilt from today's session/revision.
    watchlist = resolve_watchlist(get("/api/v1/watchlists"), arguments.watchlist)  # type: ignore[arg-type]
    with store.lock:
        store.load()
        record = store.document["watchlists"].get(watchlist["id"], {})
    if record.get("state") == "SUBMITTED" and record.get("key"):
        # Already drained above when this is the same record; a terminal state
        # falls through, so this only fires for a pending record the offline
        # pre-check could not see (e.g. the list was renamed in between).
        if arguments.force:
            raise BusyError(
                f"watchlist {watchlist['name']!r} still has a pending intent "
                f"(key {record.get('key')}); --force now would orphan an intent the "
                "server may already have accepted. Re-run without --force first."
            )
        resume(watchlist["id"], record)
        action = "resumed"

    # 4. Only now resolve today's session to judge whether a new intent is
    #    needed (the finished pending intent feeds the coverage check as-is).
    session = get("/api/v1/sessions/current")["as_of_session"]  # type: ignore[index]

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
            return state_exit(state), summary(
                action="covered",
                watchlist=watchlist["name"],
                session=session,
                state=state,
                scan_id=record.get("scan_id"),
                attempt=record.get("attempt"),
                key=record.get("key"),
            )
        # --force on a resolved intent is the only thing that increments the
        # attempt (a new intent); retries and restarts reuse the deterministic key.
        attempt = (record.get("attempt", 0) + 1) if arguments.force else 0
        key = intent_key(
            watchlist["id"], session, watchlist["revision"], arguments.provider, attempt
        )
        body = {"watchlist_id": watchlist["id"], "as_of_session": session, "data_mode": "auto"}
        resumed_scan_id = (
            record.get("scan_id")
            if record.get("key") == key and record.get("state") == "SUBMITTED"
            else None
        )
        store.document["watchlists"][watchlist["id"]] = {
            "watchlist_name": watchlist["name"],
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
    if action == "covered":
        action = "submitted"
    state = await_intent(arguments, base, token, deadline, store, watchlist["id"])
    return state_exit(state), summary(
        action=action,
        watchlist=watchlist["name"],
        session=session,
        state=state,
        attempt=attempt,
        key=key,
    )


def run_standalone_path(arguments, data_dir: Path, store: StateStore) -> tuple[int, dict]:
    """Serve is down and nothing is pending: synchronous CLI scan, deduplicated.

    The CLI has no idempotency keys, so the whole operation runs under the
    wrapper lock — a concurrent scheduler either waits and then dedups against
    finished history, or times out busy. Never silent: FAILED stays exit 1.
    """
    executable = Path(arguments.qscan).expanduser()
    if not executable.is_file():
        raise UsageError(f"qscan executable not found: {executable}")

    def cli_json(*args: str, allowed: frozenset[int] = frozenset({0})) -> object:
        """Run a CLI subcommand and validate BOTH the exit code and the shape.

        An error envelope is never treated as a run document; a non-zero exit
        without an envelope leaves the outcome unknown (busy, resumable).
        """
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
        except OSError as exc:
            raise UsageError(f"qscan executable could not be run: {exc}") from exc
        try:
            document = json.loads(completed.stdout)
        except ValueError:
            raise BusyError(
                f"qscan {args[0]} produced non-JSON output (exit {completed.returncode})"
            ) from None
        if isinstance(document, dict) and "error" in document and "state" not in document:
            detail = document["error"].get("message") or document["error"].get("code") or ""
            if completed.returncode in (2, 4):
                raise UsageError(f"qscan {args[0]} failed (exit {completed.returncode}): {detail}")
            raise BusyError(f"qscan {args[0]} failed (exit {completed.returncode}): {detail}")
        if completed.returncode not in allowed:
            raise BusyError(
                f"qscan {args[0]} exited {completed.returncode} without an error "
                "document; outcome unknown, treat as resumable"
            )
        return document

    try:
        session = cli_json("sessions")["as_of_session"]  # type: ignore[index]
        watchlists = cli_json("watchlist", "list")  # type: ignore[assignment]
        assert isinstance(watchlists, list)
    except (BusyError, UsageError) as exc:
        raise UsageError(f"local CLI preflight failed: {exc}") from exc
    matches = [
        w for w in watchlists if w["name"] == arguments.watchlist or w["id"] == arguments.watchlist
    ]
    if len(matches) != 1:
        raise UsageError(f"watchlist {arguments.watchlist!r} matched {len(matches)} entries")
    watchlist = matches[0]

    with store.lock:
        history = cli_json("scans", "list", "--limit", "200")  # type: ignore[assignment]
        assert isinstance(history, list)
        candidates = [
            run
            for run in history
            if run.get("watchlist_id") == watchlist["id"]
            and run.get("context", {}).get("as_of_session") == session
        ]
        existing = [run for run in candidates if run["state"] in TERMINAL]
        record = store.document["watchlists"].get(watchlist["id"], {})
        # A run for this session only counts as coverage if the list has not
        # been edited since (revision mismatch -> new intent -> rescan).
        if (
            existing
            and not arguments.force
            and record.get("revision") == watchlist["revision"]
            and record.get("session") == session
            and record.get("provider") == arguments.provider
        ):
            state = existing[0]["state"]
            log(
                f"session {session} already scanned for "
                f"{watchlist['name']} ({state}); nothing to do"
            )
            return state_exit(state), summary(
                action="covered",
                watchlist=watchlist["name"],
                session=session,
                state=state,
                scan_id=existing[0].get("id"),
                attempt=record.get("attempt"),
            )
        attempt = (record.get("attempt", 0) + 1) if arguments.force else 0
        store.document["watchlists"][watchlist["id"]] = {
            **record,
            "watchlist_name": watchlist["name"],
            "session": session,
            "revision": watchlist["revision"],
            "provider": arguments.provider,
            "attempt": attempt,
            "state": "SUBMITTED",
        }
        store.save()
        log(f"scanning {watchlist['name']} for session {session} (attempt {attempt})")
        document = cli_json(  # type: ignore[assignment]
            "scan",
            "--watchlist",
            watchlist["id"],
            "--as-of",
            session,
            "--data-mode",
            "auto",
            "--format",
            "json",
            allowed=_CLI_SCAN_EXITS,
        )
        assert isinstance(document, dict)
        state = document.get("state")
        if state not in TERMINAL:
            raise BusyError(
                f"qscan scan returned no terminal state (exit document state {state!r}); "
                "the run stays resumable"
            )
        store.load()
        store.document["watchlists"][watchlist["id"]] = {
            **store.document["watchlists"].get(watchlist["id"], {}),
            "scan_id": document.get("id"),
            "state": state,
            "counts": document.get("counts"),
        }
        store.save()
    log(f"scan finished: {state}")
    return state_exit(state), summary(
        action="scanned",
        watchlist=watchlist["name"],
        session=session,
        state=state,
        scan_id=document.get("id"),
        attempt=attempt,
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
            store.load()
            pending = find_pending(store, arguments.watchlist)
            if pending is not None and arguments.force:
                identity, record = pending
                raise BusyError(
                    f"watchlist {record.get('watchlist_name', identity)!r} still has a pending "
                    f"intent (key {record.get('key')}); --force now would orphan an intent the "
                    "server may already have accepted. Re-run without --force to resolve it "
                    "first."
                )
            if pending is not None and not serve_is_up(arguments.port):
                raise BusyError(
                    "a submitted scan intent may already have been accepted but the API is "
                    f"unreachable on port {arguments.port}; the CLI fallback is refused to "
                    "avoid a duplicate. Start serve (or wait) and re-run; the intent resumes "
                    "with its original key."
                )
            if serve_is_up(arguments.port):
                code, document = run_api_path(arguments, data_dir, store, pending)
            else:
                code, document = run_standalone_path(arguments, data_dir, store)
        print(json.dumps(document, ensure_ascii=True))
        return code
    except Timeout:
        return fail(
            "EXECUTOR_BUSY",
            "another scheduler instance holds the state lock; retry later",
            EXIT["busy"],
        )
    except BusyError as exc:
        return fail("EXECUTOR_BUSY", str(exc), EXIT["busy"])
    except UsageError as exc:
        return fail("USAGE_ERROR", str(exc), EXIT["usage"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Token/state durability problems must never surface as a traceback,
        # and a half-written intent is never silently treated as submitted.
        return fail("STATE_OR_CONFIG_ERROR", f"{type(exc).__name__}: {exc}", EXIT["usage"])
    except Exception as exc:  # noqa: BLE001 - the process boundary owns the contract
        return fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", EXIT["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
