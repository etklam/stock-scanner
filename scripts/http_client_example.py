"""Standalone loopback HTTP client example: the whole scan flow over the API only.

The client imports no qscan code and never touches SQLite: it reads the local API
token from the data directory (or QSCAN_API_TOKEN), creates a watchlist, submits a
scan with an idempotency key, polls with a bounded loop, and reads results, chart
series, changes and CSV export. It handles PARTIAL/FAILED/429/409 responses and
reuses the same Idempotency-Key when retrying a submission.

Usage (server running via `qscan serve`):

    uv run python scripts/http_client_example.py --base-url http://127.0.0.1:8000 \
        --data-dir "$HOME/qscan-personal"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

MAX_POLLS = 60
POLL_INTERVAL = 1.0
TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED"}


class ClientError(RuntimeError): ...


def read_token(data_dir: Path | None) -> str:
    configured = Path(data_dir) / "api-token.json" if data_dir else None
    if configured and configured.is_file():
        document = json.loads(configured.read_text(encoding="utf-8"))
        return document["token"]
    import os

    token = os.environ.get("QSCAN_API_TOKEN")
    if not token:
        raise ClientError(
            "No API token: pass --data-dir with api-token.json or set QSCAN_API_TOKEN"
        )
    return token


class ScanClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = httpx.request(
            method, self.base + path, headers=self.headers, timeout=15, **kwargs
        )
        if response.status_code >= 400:
            detail = response.json().get("error", {}) if response.content else {}
            raise ClientError(
                f"{method} {path} -> {response.status_code} "
                f"{detail.get('code', 'UNKNOWN')}: {detail.get('message', '')}"
            )
        return response

    def create_watchlist(self, name: str, symbols: list[str]) -> dict:
        response = self.request(
            "POST", "/api/v1/watchlists", json={"name": name, "symbols": symbols}
        )
        return response.json()

    def rulesets(self) -> list[dict]:
        return self.request("GET", "/api/v1/rulesets").json()

    def submit_scan(self, watchlist_id: str, idempotency_key: str, as_of: str | None) -> dict:
        body: dict = {"watchlist_id": watchlist_id}
        if as_of:
            body["as_of_session"] = as_of
        response = httpx.request(
            "POST",
            self.base + "/api/v1/scans",
            headers=self.headers | {"Idempotency-Key": idempotency_key},
            json=body,
            timeout=15,
        )
        if response.status_code == 429:
            raise ClientError("Queue is full (429 QUEUE_LIMIT_REACHED); retry later")
        if response.status_code == 409:
            detail = response.json()["error"]
            raise ClientError(f"Rejected: {detail['code']}: {detail['message']}")
        if response.status_code not in (200, 202):
            detail = response.json().get("error", {})
            raise ClientError(f"Submit failed: {response.status_code} {detail.get('code')}")
        return response.json()

    def poll_until_terminal(self, scan_id: str) -> dict:
        for _ in range(MAX_POLLS):
            status = self.request("GET", f"/api/v1/scans/{scan_id}").json()
            if status["state"] in TERMINAL:
                return status
            progress = status.get("progress")
            note = (
                f"{progress['phase']} {progress['processed_symbols']}/{progress['total_symbols']}"
                if progress
                else "queued"
            )
            print(f"  state={status['state']} ({note})")
            time.sleep(POLL_INTERVAL)
        raise ClientError(f"Scan {scan_id} did not finish within {MAX_POLLS} polls")

    def results(self, scan_id: str) -> list[dict]:
        return self.request("GET", f"/api/v1/scans/{scan_id}/results", params={"limit": 50}).json()[
            "items"
        ]

    def result_detail(self, scan_id: str, instrument_id: str) -> dict:
        return self.request("GET", f"/api/v1/scans/{scan_id}/results/{instrument_id}").json()

    def series(self, scan_id: str, instrument_id: str, limit: int = 126) -> dict:
        return self.request(
            "GET",
            f"/api/v1/scans/{scan_id}/series/{instrument_id}",
            params={"limit": limit},
        ).json()

    def changes(self, scan_id: str) -> dict:
        return self.request("GET", f"/api/v1/scans/{scan_id}/changes").json()

    def export_csv(self, scan_id: str) -> bytes:
        return self.request("GET", f"/api/v1/scans/{scan_id}/export?format=csv").content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="Directory holding api-token.json"
    )
    parser.add_argument("--as-of", default=None, help="Optional completed session (YYYY-MM-DD)")
    parser.add_argument("--symbols", nargs="+", default=["AAPL", "MSFT"])
    args = parser.parse_args()

    token = read_token(args.data_dir)
    client = ScanClient(args.base_url, token)
    idempotency_key = str(uuid.uuid4())  # kept for the whole flow; retries reuse it

    print("rulesets:", client.rulesets())
    watchlist = client.create_watchlist(f"api-example-{uuid.uuid4().hex[:8]}", args.symbols)
    print("watchlist:", watchlist["id"], watchlist["symbols"])

    try:
        accepted = client.submit_scan(watchlist["id"], idempotency_key, args.as_of)
        print(f"submitted: {accepted['id']} ({accepted.get('state', 'QUEUED')})")
    except ClientError as exc:
        print(f"submission rejected: {exc}", file=sys.stderr)
        return 1

    status = client.poll_until_terminal(accepted["id"])
    print("final:", status["state"], status.get("counts"))
    if status["state"] == "FAILED":
        print("error:", status.get("error"), file=sys.stderr)
        return 1
    if status["state"] == "PARTIAL":
        print("note: partial coverage; see status warnings", file=sys.stderr)

    results = client.results(accepted["id"])
    print(f"results: {len(results)} row(s)")
    for row in results[:5]:
        print(
            f"  #{row['rank']} {row['instrument']['display_symbol']} "
            f"score={row['analysis']['score']} stage={row['analysis']['stage']}"
        )
    if results:
        detail = client.result_detail(accepted["id"], results[0]["instrument"]["id"])
        print("detail reasons:", detail["reasons"] or "(none)")
        series = client.series(accepted["id"], results[0]["instrument"]["id"])
        print(f"series: {series['displayed_sessions']} sessions for {series['symbol']}")
    changes = client.changes(accepted["id"])
    print("changes:", changes["binding"], changes["reasons"])
    csv_bytes = client.export_csv(accepted["id"])
    print(f"csv: {len(csv_bytes)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
