"""Canonical, content-addressed immutable snapshots on local disk."""

import gzip
import hashlib
import json
import os
import re
import tempfile
import zlib
from pathlib import Path

from pydantic import ValidationError

from qscan import __version__
from qscan.application.contracts import ApplicationError, InputSnapshot
from qscan.domain.models import ErrorCode


def canonical(value: InputSnapshot) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


class SnapshotStore:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self.directory = directory
        if create:
            directory.mkdir(parents=True, exist_ok=True)

    def path(self, digest: str) -> Path:
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise ApplicationError(ErrorCode.SCAN_FAILED, "Invalid snapshot hash")
        return self.directory / (digest + ".json.gz")

    def write(self, value: InputSnapshot) -> str:
        content = canonical(value)
        digest = hashlib.sha256(content).hexdigest()
        target = self.path(digest)
        if target.exists():
            self.read(digest)
            return digest
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as stream:
                temporary = stream.name
                stream.write(gzip.compress(content, mtime=0))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return digest

    def read(self, digest: str) -> InputSnapshot:
        try:
            content = gzip.decompress(self.path(digest).read_bytes())
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("Snapshot hash mismatch")
            value = InputSnapshot.model_validate_json(content)
            if value.context.engine_version != __version__:
                raise ValueError("Incompatible engine version")
            if canonical(value) != content:
                raise ValueError("Noncanonical snapshot")
            return value
        except (OSError, EOFError, ValueError, ValidationError, zlib.error) as exc:
            raise ApplicationError(
                ErrorCode.SCAN_FAILED, "Snapshot missing, corrupt, or incompatible"
            ) from exc
