"""Regenerate docs/openapi.json from the implemented FastAPI app (offline, no server)."""

import json
import sys
import tempfile
from pathlib import Path

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.bootstrap import bootstrap
from qscan.executor import ScanExecutor
from qscan.interfaces.api.app import create_app
from qscan.interfaces.api.localauth import ensure_token

ROOT = Path(__file__).resolve().parents[1]


def build_openapi() -> dict:
    with tempfile.TemporaryDirectory(prefix="qscan-openapi-") as directory:
        root = Path(directory)
        scanner = bootstrap(
            FixtureProvider({}),
            data_dir=root / "data",
            clock=FixedClock(datetime_utc()),
            calendar=NYSECalendar(),
        )
        try:
            ensure_token(root / "api-token.json")
            app = create_app(
                scanner,
                token_path=root / "api-token.json",
                dev_openapi=True,
                executor=ScanExecutor(scanner),
            )
            schema = app.openapi()
        finally:
            scanner.engine.dispose()
    # Paths/schemas only; server URLs depend on the request and are not contractual.
    schema.pop("servers", None)
    return schema


def datetime_utc():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def main() -> int:
    target = ROOT / "docs" / "openapi.json"
    schema = build_openapi()
    rendered = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") == rendered:
        print(f"OpenAPI snapshot up to date: {target}")
        return 0
    if "--check" in sys.argv:
        print("OpenAPI snapshot is stale; run: uv run python scripts/openapi_snapshot.py")
        return 1
    target.write_text(rendered, encoding="utf-8")
    print(f"OpenAPI snapshot written: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
