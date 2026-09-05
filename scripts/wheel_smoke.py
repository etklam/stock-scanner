"""Install the built wheel with locked dependencies and run outside the repository."""

import base64
import csv
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TERMINAL_STATES = {"SUCCEEDED", "PARTIAL", "FAILED"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_json(method: str, url: str, token: str, body: dict | None = None, key: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8")), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), exc.headers


def wait_ready(base: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, _ = http_json("GET", base + "/health/ready", "ignored")
            if status == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise AssertionError("serve never became ready")


def start_server(executable: Path, data_dir: Path, workdir: Path) -> tuple[subprocess.Popen, str]:
    port = free_port()
    process = subprocess.Popen(
        [
            str(executable),
            "--data-dir",
            str(data_dir),
            "--provider",
            "fixture",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=workdir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    wait_ready(base)
    return process, base


def http_flow(executable: Path, data_dir: Path, workdir: Path) -> str:
    """Serve, drive the full API flow over loopback, and return the scan id."""
    process, base = start_server(executable, data_dir, workdir)
    try:
        token = json.loads((data_dir / "api-token.json").read_text(encoding="utf-8"))["token"]
        # Unauthenticated access is rejected; loopback does not mean anonymous.
        status, _, _ = http_json("GET", base + "/api/v1/watchlists", "wrong-token")
        assert status == 401
        status, created, _ = http_json(
            "POST",
            base + "/api/v1/watchlists",
            token,
            {"name": "api-smoke", "symbols": ["DEMO"]},
        )
        assert status == 201, created
        watchlist = created["id"]
        key = "wheel-smoke-idempotency-key-0001"
        status, accepted, headers = http_json(
            "POST",
            base + "/api/v1/scans",
            token,
            {"watchlist_id": watchlist, "as_of_session": "2026-09-04", "data_mode": "force"},
            key=key,
        )
        assert status == 202 and headers["Location"] == f"/api/v1/scans/{accepted['id']}", accepted
        scan_id = accepted["id"]
        deadline = time.monotonic() + 60
        document = {}
        while time.monotonic() < deadline:
            _, document, _ = http_json("GET", f"{base}/api/v1/scans/{scan_id}", token)
            if document["state"] in TERMINAL_STATES:
                break
            time.sleep(0.2)
        assert document["state"] == "SUCCEEDED", document
        assert document["counts"]["candidate"] == 1
        # Same key + same body returns the same run (200 with real state).
        status, replayed, _ = http_json(
            "POST",
            base + "/api/v1/scans",
            token,
            {"watchlist_id": watchlist, "as_of_session": "2026-09-04", "data_mode": "force"},
            key=key,
        )
        assert status == 200 and replayed["id"] == scan_id and replayed["state"] == "SUCCEEDED"
        status, page, _ = http_json("GET", f"{base}/api/v1/scans/{scan_id}/results?limit=50", token)
        assert status == 200 and len(page["items"]) == 1
        instrument = page["items"][0]["instrument"]["id"]
        status, detail, _ = http_json(
            "GET", f"{base}/api/v1/scans/{scan_id}/results/{instrument}", token
        )
        assert status == 200 and detail["analysis"]["stage"] == "CLOSE_BREAK_ABOVE"
        status, series, _ = http_json(
            "GET", f"{base}/api/v1/scans/{scan_id}/series/{instrument}?limit=50", token
        )
        assert status == 200 and series["displayed_sessions"] == 50 and series["closes"]
        status, changes, _ = http_json("GET", f"{base}/api/v1/scans/{scan_id}/changes", token)
        assert status == 200 and changes["binding"] == "recorded"
        request = urllib.request.Request(
            f"{base}/api/v1/scans/{scan_id}/export?format=csv",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            assert response.headers["X-Scan-State"] == "SUCCEEDED"
            csv_body = response.read().decode("utf-8-sig")
        assert len(csv_body.strip().splitlines()) == 2  # header + one candidate
        return scan_id
    finally:
        process.terminate()
        process.wait(30)
        if process.poll() is None:
            process.kill()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wheel = next((root / "dist").glob("*.whl"))
    with tempfile.TemporaryDirectory(prefix="qscan wheel 測試 ") as temporary:
        directory = Path(temporary)
        environment = directory / "wheel env"
        subprocess.run(["uv", "venv", "--python", "3.12", str(environment)], check=True)
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        requirements = directory / "dependencies.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "-r", str(requirements), str(wheel)],
            check=True,
        )
        executable = binary / ("qscan.exe" if os.name == "nt" else "qscan")
        data_dir = directory / "資料 data"

        def invoke(*args: str) -> object:
            result = subprocess.run(
                [str(executable), "--data-dir", str(data_dir), "--provider", "fixture", *args],
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert result.returncode == 0, (args, result.stdout, result.stderr)
            return json.loads(result.stdout)

        invoke("init")
        assert invoke("init")["api_token"] == "kept"  # rerun never rotates silently
        demo = invoke("demo")
        assert isinstance(demo, dict)
        assert invoke("watchlist", "list")
        run = invoke(
            "scan",
            "--watchlist",
            demo["watchlist_id"],
            "--as-of",
            demo["as_of"],
            "--data-mode",
            "cache_only",
            "--format",
            "json",
        )
        assert isinstance(run, dict)
        identity = run["id"]
        assert invoke("scans", "show", identity, "--format", "json") == run
        invoke("scans", "list")
        invoke("scans", "changes", identity)
        output = directory / "報告 output"
        for format in ("json", "csv", "html"):
            invoke("report", identity, "--format", format, "--output", str(output))
        report = json.loads((output / f"scan-{identity}.json").read_text(encoding="utf-8"))
        assert report["run"]["counts"]["candidate"] == 1 and report["charts"]
        with (output / f"scan-{identity}.csv").open(encoding="utf-8-sig", newline="") as stream:
            assert len(list(csv.DictReader(stream))) == 1
        summary = json.loads((output / f"scan-{identity}.summary.json").read_text(encoding="utf-8"))
        assert summary["run"]["counts"] == run["counts"]
        assert "results" not in summary["run"]
        html = (output / f"scan-{identity}.html").read_text(encoding="utf-8")
        image = re.search(r'data:image/png;base64,([^" ]+)', html)
        assert image and base64.b64decode(image[1]).startswith(b"\x89PNG\r\n\x1a\n")
        assert "SYNTHETIC" in html and "https://" not in html
        replay = invoke("replay", identity, "--format", "json")
        assert isinstance(replay, dict) and replay["source_run_id"] == identity
        assert replay["results"] == run["results"]
        diagnosis = invoke("doctor")
        assert isinstance(diagnosis, dict) and diagnosis["local_healthy"]
        assert diagnosis["yahoo_release"] == "EOD_TRIAL"
        print(
            "Installed-wheel CLI smoke passed: "
            "init/demo/list/scan/show/changes/reports/replay/doctor"
        )

        # Full loopback HTTP flow against the installed wheel, then restart and
        # confirm the history is still readable (no rerun, same id).
        scan_id = http_flow(executable, data_dir, directory)
        assert scan_id
        process, base = start_server(executable, data_dir, directory)
        try:
            token = json.loads((data_dir / "api-token.json").read_text(encoding="utf-8"))["token"]
            status, document, _ = http_json("GET", f"{base}/api/v1/scans/{scan_id}", token)
            assert status == 200 and document["state"] == "SUCCEEDED"
            status, history, _ = http_json("GET", f"{base}/api/v1/scans?limit=200", token)
            assert status == 200 and any(item["id"] == identity for item in history["items"])
        finally:
            process.terminate()
            process.wait(30)
            if process.poll() is None:
                process.kill()
        print(
            "Installed-wheel HTTP smoke passed: "
            "serve/auth/watchlist/202+idempotency/poll/results/detail/series/changes/csv/restart"
        )


if __name__ == "__main__":
    main()
