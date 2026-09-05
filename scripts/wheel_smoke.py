"""Install the built wheel with locked dependencies and run outside the repository."""

import base64
import csv
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


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
        demo = invoke("demo")
        assert isinstance(demo, dict)
        invoke("init")
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


if __name__ == "__main__":
    main()
