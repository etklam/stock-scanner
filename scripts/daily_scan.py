#!/usr/bin/env python3
"""Daily scan wrapper for scheduler use (launchd / Task Scheduler / cron / systemd).

Composes existing CLI and HTTP capabilities only; it contains no scanning or
trading-day logic. One invocation follows exactly one of two paths:

- `qscan serve` is reachable  -> submit over loopback HTTP with a bearer token
  from the data directory, then poll to a terminal state.
- `serve` is not reachable    -> run the standalone CLI scan against the same
  data directory (never both at once: the executor lock already prevents two
  writers, and this wrapper never races it).

Stability rules:
- The Idempotency-Key is derived from (watchlist, local date) and REUSED across
  retries within the day, so a retried submission replays the original scan
  instead of creating a duplicate.
- A small state file (<data-dir>/daily-scan-state.json) remembers the last
  finished session; a re-trigger on the same finished session exits 0 without
  resubmitting. Delete the state file to force a rerun.
- US market holidays resolve server-side via the bundled market calendar (the
  CLI/API never assume localtoday == US session); on holidays the resolved
  session repeats, the state file prevents a duplicate run unless --force.
- Exit codes mirror the CLI contract: 0 ok, 1 scan failed, 2 usage/validation,
  3 partial (some symbols lacked data), 4 executor busy (retry later).

Token handling: the bearer token is read from <data-dir>/api-token.json and
sent only in the Authorization header to 127.0.0.1. It is never logged,
never placed in a URL, and never written into scheduler definitions.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED"}
EXIT_CODES = {"ok": 0, "failed": 1, "usage": 2, "partial": 3, "busy": 4}


def log(message: str) -> None:
    print(f"daily-scan: {message}", file=sys.stderr, flush=True)


def idempotency_key(watchlist: str, today: str) -> str:
    digest = hashlib.sha256(f"{watchlist}|{today}".encode()).hexdigest()[:24]
    return f"daily-{today}-{digest}"


def http_json(url: str, token: str | None = None, payload: dict | None = None, timeout: float = 10):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urlrequest.Request(url, data=data, headers=headers)
    with urlrequest.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read())


def serve_is_up(port: int) -> bool:
    try:
        status, _ = http_json(f"http://127.0.0.1:{port}/health/live", timeout=2)
        return status == 200
    except (URLError, OSError, ValueError):
        return False


def _submit_and_poll(port, token, watchlist_id, key, body, poll_seconds, timeout_s):
    """Submit with retries (same key every time), then poll to a terminal state."""
    deadline = time.monotonic() + timeout_s
    attempts = 0
    while True:
        attempts += 1
        try:
            request = urlrequest.Request(
                f"http://127.0.0.1:{port}/api/v1/scans",
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": key,
                },
            )
            with urlrequest.urlopen(request, timeout=10) as response:
                document = json.loads(response.read())
            break
        except HTTPError as exc:
            if exc.code in (409, 429) and time.monotonic() < deadline and attempts < 5:
                time.sleep(poll_seconds)
                continue
            raise
        except (URLError, OSError):
            if time.monotonic() > deadline or attempts >= 3:
                raise
            time.sleep(5)
    scan_id = document["id"]
    while time.monotonic() < deadline:
        _, status = http_json(f"http://127.0.0.1:{port}/api/v1/scans/{scan_id}", token=token)
        if status["state"] in TERMINAL:
            return status
        time.sleep(poll_seconds)
    raise TimeoutError(f"scan {scan_id} did not finish within {timeout_s}s")


def load_state(data_dir: Path) -> dict:
    path = data_dir / "daily-scan-state.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(data_dir: Path, state: dict) -> None:
    path = data_dir / "daily-scan-state.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Absolute data directory")
    parser.add_argument("--watchlist", required=True, help="Watchlist name or id")
    parser.add_argument("--port", type=int, default=8000, help="Local serve port to probe")
    parser.add_argument("--qscan", default="qscan", help="Absolute path to the qscan executable")
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--force", action="store_true", help="Rerun even if the session is recorded"
    )
    arguments = parser.parse_args()
    data_dir = arguments.data_dir.expanduser().resolve()
    if not (data_dir / "qscan.sqlite3").is_file():
        log(f"{data_dir} is not initialized; run qscan init")
        return EXIT_CODES["usage"]

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    state = load_state(data_dir)
    previous = state.get(arguments.watchlist, {})
    if previous.get("date") == today and previous.get("state") in TERMINAL and not arguments.force:
        log(f"{arguments.watchlist} already finished today ({previous['state']}); nothing to do")
        return EXIT_CODES["ok"]

    if serve_is_up(arguments.port):
        token = json.loads((data_dir / "api-token.json").read_text())["token"]
        try:
            _, watchlists = http_json(
                f"http://127.0.0.1:{arguments.port}/api/v1/watchlists", token=token
            )
            matches = [w for w in watchlists if w["name"] == arguments.watchlist]
            if len(matches) != 1:
                log(f"watchlist {arguments.watchlist!r} not found on the API")
                return EXIT_CODES["usage"]
            key = idempotency_key(arguments.watchlist, today)
            status = _submit_and_poll(
                arguments.port,
                token,
                matches[0]["id"],
                key,
                {"watchlist_id": matches[0]["id"], "data_mode": "auto"},
                arguments.poll_seconds,
                arguments.timeout_seconds,
            )
        except (URLError, HTTPError, OSError, TimeoutError, KeyError) as exc:
            log(f"API path failed: {exc}")
            return (
                EXIT_CODES["busy"] if isinstance(exc, (URLError, OSError)) else EXIT_CODES["failed"]
            )
        path_used = "api"
    else:
        command = [
            str(arguments.qscan),
            "--data-dir",
            str(data_dir),
            "scan",
            "--watchlist",
            arguments.watchlist,
            "--format",
            "json",
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=arguments.timeout_seconds
        )
        if completed.returncode not in (0, 1, 3):
            log(f"CLI scan failed ({completed.returncode}): {completed.stderr[-500:]}")
            return EXIT_CODES["busy"] if completed.returncode == 4 else EXIT_CODES["usage"]
        document = json.loads(completed.stdout)
        status = {"state": document["state"], "as_of_session": document["context"]["as_of_session"]}
        path_used = "cli"

    state.setdefault(arguments.watchlist, {}).update(
        {"date": today, "state": status["state"], "as_of_session": status.get("as_of_session")}
    )
    save_state(data_dir, state)
    log(f"scan finished via {path_used}: {status['state']} (as_of {status.get('as_of_session')})")
    return (
        EXIT_CODES["failed"]
        if status["state"] == "FAILED"
        else EXIT_CODES["partial"]
        if status["state"] == "PARTIAL"
        else EXIT_CODES["ok"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
