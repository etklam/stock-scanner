"""Signed opaque cursors bound to resource, principal, filters and sort."""

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any

from qscan.application.contracts import ApplicationError
from qscan.domain.models import ErrorCode

_MAX_CURSOR = 2048


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def encode_cursor(payload: dict[str, Any], secret: str) -> str:
    body = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{digest}"


def decode_cursor(cursor: str, secret: str, resource: str, principal: str) -> dict[str, Any]:
    if not cursor or len(cursor) > _MAX_CURSOR or "." not in cursor:
        raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Malformed cursor")
    body, _, digest = cursor.rpartition(".")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Cursor signature mismatch")
    try:
        payload = json.loads(_decode(body))
    except (ValueError, binascii.Error) as exc:
        raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Malformed cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("r") != resource
        or payload.get("p") != principal
    ):
        raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Cursor does not match this query")
    return payload
