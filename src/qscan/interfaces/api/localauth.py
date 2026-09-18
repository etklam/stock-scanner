"""Local API token file: creation, constant-time checks, explicit rotation."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from qscan.application.contracts import ApplicationError
from qscan.domain.models import ErrorCode

PRINCIPAL = "local"
_STABLE_SECRET_FIELDS = ("instance_secret", "browser_session_secret")
BROWSER_SESSION_SECONDS = 15 * 60


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
        document = _load(token_path)
        upgraded = False
        if not document.get("instance_id"):
            document["instance_id"] = str(uuid4())
            upgraded = True
        for field in _STABLE_SECRET_FIELDS:
            if not document.get(field):
                document[field] = secrets.token_urlsafe(32)
                upgraded = True
        if upgraded:
            _write(token_path, document)
        return document["token"], False
    document = {
        "token": secrets.token_urlsafe(32),
        "principal": PRINCIPAL,
        "cursor_secret": secrets.token_urlsafe(32),
        "instance_id": str(uuid4()),
        "instance_secret": secrets.token_urlsafe(32),
        "browser_session_secret": secrets.token_urlsafe(32),
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
        "instance_id": old.get("instance_id", str(uuid4())),
        "instance_secret": old.get("instance_secret", secrets.token_urlsafe(32)),
        "browser_session_secret": old.get("browser_session_secret", secrets.token_urlsafe(32)),
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

    @property
    def instance_id(self) -> str:
        return _load(self.token_path)["instance_id"]

    @property
    def instance_secret(self) -> str:
        return _load(self.token_path)["instance_secret"]

    @property
    def browser_session_secret(self) -> str:
        return _load(self.token_path)["browser_session_secret"]

    def check(self, authorization: str | None) -> bool:
        if not authorization:
            return False
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            return False
        return secrets.compare_digest(credential.strip(), self.token)

    def issue_browser_session(self, now: int | None = None) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(24)
        payload = json.dumps(
            {
                "csrf": csrf,
                "exp": (now if now is not None else int(time.time())) + BROWSER_SESSION_SECONDS,
                "instance_id": self.instance_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        signature = hmac.new(
            self.browser_session_secret.encode(), encoded.encode(), hashlib.sha256
        ).hexdigest()
        return f"{encoded}.{signature}", csrf

    def browser_csrf(self, session: str | None, now: int | None = None) -> str | None:
        if not session:
            return None
        try:
            encoded, signature = session.split(".", 1)
            expected = hmac.new(
                self.browser_session_secret.encode(), encoded.encode(), hashlib.sha256
            ).hexdigest()
            if not secrets.compare_digest(signature, expected):
                return None
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
            current = now if now is not None else int(time.time())
            if payload["exp"] < current or payload["instance_id"] != self.instance_id:
                return None
            csrf = payload["csrf"]
            return csrf if isinstance(csrf, str) and csrf else None
        except (KeyError, TypeError, ValueError):
            return None
