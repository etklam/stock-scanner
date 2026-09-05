"""Local API token file: creation, constant-time checks, explicit rotation."""

import json
import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path

from qscan.application.contracts import ApplicationError
from qscan.domain.models import ErrorCode

PRINCIPAL = "local"


def _write(token_path: Path, document: dict[str, str]) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = token_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)  # 0600 on POSIX; best effort on Windows
    os.replace(temporary, token_path)


def _load(token_path: Path) -> dict[str, str]:
    try:
        value = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApplicationError(
            ErrorCode.UNAUTHORIZED, "API token unavailable; run qscan init"
        ) from exc
    if not isinstance(value, dict) or not value.get("token"):
        raise ApplicationError(ErrorCode.UNAUTHORIZED, "API token file invalid; run qscan init")
    return value


def ensure_token(token_path: Path) -> tuple[str, bool]:
    """Create a high-entropy token if absent; rerun never rotates silently."""
    if token_path.is_file():
        return _load(token_path)["token"], False
    document = {
        "token": secrets.token_urlsafe(32),
        "principal": PRINCIPAL,
        "cursor_secret": secrets.token_urlsafe(32),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _write(token_path, document)
    return document["token"], True


def rotate_token(token_path: Path) -> str:
    """Explicit rotation only; principal and cursor secret stay stable."""
    old = _load(token_path) if token_path.is_file() else {}
    document = {
        "token": secrets.token_urlsafe(32),
        "principal": old.get("principal", PRINCIPAL),
        "cursor_secret": old.get("cursor_secret", secrets.token_urlsafe(32)),
        "created_at": datetime.now(UTC).isoformat(),
        "rotated_at": datetime.now(UTC).isoformat(),
    }
    _write(token_path, document)
    return document["token"]


class TokenAuthenticator:
    """Constant-time bearer check; the principal never contains the token.

    The token file is re-read per request so explicit rotation takes effect
    without a server restart; the file is local and tiny.
    """

    def __init__(self, token_path: Path) -> None:
        self.token_path = token_path
        document = _load(token_path)
        self.principal = document.get("principal", PRINCIPAL)

    @property
    def token(self) -> str:
        return _load(self.token_path)["token"]

    @property
    def cursor_secret(self) -> str:
        document = _load(self.token_path)
        return document.get("cursor_secret", document["token"])

    def check(self, authorization: str | None) -> bool:
        if not authorization:
            return False
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            return False
        return secrets.compare_digest(credential.strip(), self.token)
