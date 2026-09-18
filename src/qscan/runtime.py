"""Small, stdlib-only boundary for safe local server discovery and launch."""

import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.removeprefix("[").removesuffix("]")).is_loopback
    except ValueError:
        return False


def data_dir_identity(data_dir: Path) -> str:
    return hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()


def instance_challenge(
    instance_id: str, instance_secret: str, data_dir: Path, nonce: str
) -> dict[str, str]:
    directory_id = data_dir_identity(data_dir)
    message = f"{instance_id}\n{directory_id}\n{nonce}".encode()
    return {
        "instance_id": instance_id,
        "data_dir_id": directory_id,
        "nonce": nonce,
        "proof": hmac.new(instance_secret.encode(), message, hashlib.sha256).hexdigest(),
    }


def verify_instance_challenge(
    challenge: Mapping[str, Any], credentials: Mapping[str, str], data_dir: Path, nonce: str
) -> bool:
    expected = instance_challenge(
        credentials["instance_id"], credentials["instance_secret"], data_dir, nonce
    )
    return all(
        isinstance(challenge.get(key), str) and secrets.compare_digest(challenge[key], value)
        for key, value in expected.items()
    )


def existing_instance(
    url: str,
    token_path: Path,
    data_dir: Path,
    timeout: float = 1.0,
    *,
    expected_provider: str | None = None,
) -> bool:
    """Prove that ``url`` is this data directory's already-running qscan."""
    try:
        credentials = json.loads(token_path.read_text(encoding="utf-8"))
        nonce = secrets.token_urlsafe(24)
        endpoint = f"{url}/api/v1/instance/challenge?nonce={nonce}"
        with urllib.request.urlopen(endpoint, timeout=timeout) as response:  # noqa: S310
            challenge = json.load(response)
        return (
            isinstance(credentials, dict)
            and isinstance(challenge, dict)
            and (verify_instance_challenge(challenge, credentials, data_dir, nonce))
            and (expected_provider is None or challenge.get("provider") == expected_provider)
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def open_browser(url: str) -> None:
    webbrowser.open(url)


def open_browser_when_ready(
    url: str,
    token_path: Path,
    data_dir: Path,
    timeout: float = 15.0,
    *,
    expected_provider: str | None = None,
) -> None:
    """Open the UI only after the expected local instance proves its identity."""

    def wait_and_open() -> None:
        deadline = time.monotonic() + timeout
        origin = url.removesuffix("/ui/")
        while time.monotonic() < deadline:
            if existing_instance(
                origin,
                token_path,
                data_dir,
                timeout=0.5,
                expected_provider=expected_provider,
            ):
                open_browser(url)
                return
            time.sleep(0.1)

    threading.Thread(target=wait_and_open, name="qscan-browser", daemon=True).start()
