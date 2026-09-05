"""Consistent local backup, verification, and safe restore of a data directory.

Backup is an offline operation. It refuses to run while the executor lock is
held (serve, scan, refresh, or migration owns the directory), copies the
database with the SQLite backup API — including committed-but-uncheckpointed
WAL content — and derives everything else (snapshot set, counts, schema
revision) from that copy, never from the live database again.

An archive is a zip with a versioned manifest. It carries the full business
database, every input snapshot the database references, and allowlisted
non-secret settings. It never carries the API bearer token, lock files, or
derived reports. Archives are checksummed for integrity, NOT encrypted: they
contain private watchlists and price history and must be stored securely by
the user.

Restore only ever targets a fresh destination directory. Archive entries are
treated as untrusted input: paths, counts, and sizes are validated and
extraction is manual — no ``extractall``.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from filelock import FileLock

from qscan import __version__
from qscan.application.contracts import ApplicationError
from qscan.domain.models import ErrorCode, RunState

BACKUP_FORMAT_VERSION = 1
_DB_ENTRY = "db/qscan.sqlite3"
_MANIFEST_ENTRY = "manifest.json"
_SNAPSHOT_PREFIX = "snapshots/"
_SETTINGS_PREFIX = "settings/"
_DIGEST = re.compile("[0-9a-f]{64}")
# Non-secret, non-derived data-directory files carried across restores. The
# current data directory holds nothing outside the database and snapshots;
# extend this allowlist (and the tests) if that ever changes.
_SETTINGS_ALLOWLIST: tuple[str, ...] = ()
# Restore hardening limits: archives may come from anywhere.
_MAX_ENTRIES = 10_000
_MAX_ENTRY_BYTES = 2**31
_MAX_TOTAL_BYTES = 2**33
_LOCK_TIMEOUT = 5.0

_VERSIONS_DIR = Path(__file__).parent / "persistence" / "migrations" / "versions"
KNOWN_SCHEMA_REVISIONS = frozenset(
    name.split("_")[0]
    for name in os.listdir(_VERSIONS_DIR)
    if name.endswith(".py") and not name.startswith("_")
)


def _fail(code: ErrorCode, message: str) -> ApplicationError:
    return ApplicationError(code, message)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_entry_name(name: str) -> None:
    """Reject absolute paths, traversal, drive/UNC hints, and separators that
    disagree between platforms."""
    if not name or name != name.strip() or "\\" in name:
        raise _fail(ErrorCode.VALIDATION_ERROR, f"Unsafe archive entry name: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
        raise _fail(ErrorCode.VALIDATION_ERROR, f"Unsafe archive entry name: {name!r}")
    if re.match(r"^[A-Za-z]:", name) or name.startswith("//"):
        raise _fail(ErrorCode.VALIDATION_ERROR, f"Unsafe archive entry name: {name!r}")


def _read_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw = archive.read(_MANIFEST_ENTRY)
    except KeyError as exc:
        raise _fail(ErrorCode.VALIDATION_ERROR, "Archive has no backup manifest") from exc
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest must be an object")
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            f"Unsupported backup format version: {manifest.get('format_version')!r}; "
            f"this engine reads format {BACKUP_FORMAT_VERSION}",
        )
    required = {
        "created_at",
        "engine_version",
        "schema_revision",
        "database",
        "counts",
        "snapshots",
    }
    missing = required - manifest.keys()
    if missing:
        raise _fail(
            ErrorCode.VALIDATION_ERROR, f"Backup manifest is missing fields: {sorted(missing)}"
        )
    return manifest


def _database_report(path: Path) -> dict[str, Any]:
    """Structure, counts, and referenced snapshot digests from a DB copy."""
    uri = f"file:{path.as_uri()[7:]}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        revision = row[0] if row else None
        if revision is None:
            raise _fail(ErrorCode.VALIDATION_ERROR, "Database has no schema revision")
        if revision not in KNOWN_SCHEMA_REVISIONS:
            raise _fail(
                ErrorCode.VALIDATION_ERROR,
                f"Unknown database schema revision {revision!r}; this engine knows "
                f"{sorted(KNOWN_SCHEMA_REVISIONS)}",
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise _fail(ErrorCode.INTERNAL_ERROR, f"Database integrity check failed: {integrity}")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise _fail(ErrorCode.INTERNAL_ERROR, "Database foreign key check failed")
        by_state: dict[str, int] = {}
        for state, count in connection.execute(
            "SELECT state, COUNT(*) FROM scan_runs GROUP BY state"
        ):
            by_state[state] = count
        digests = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT input_hash FROM scan_runs WHERE input_hash IS NOT NULL"
            )
        }
        return {
            "schema_revision": revision,
            "runs": sum(by_state.values()),
            "runs_by_state": by_state,
            "watchlists": connection.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0],
            "snapshot_digests": digests,
        }
    except sqlite3.Error as exc:
        raise _fail(ErrorCode.VALIDATION_ERROR, f"Not a readable qscan database: {exc}") from exc
    finally:
        connection.close()


def _database_entry_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    database = manifest["database"]
    if not isinstance(database, dict) or not _DIGEST.fullmatch(str(database.get("sha256", ""))):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest database hash is invalid")
    return database


def _snapshot_entries_from_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = manifest["snapshots"]
    if not isinstance(entries, list):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest snapshots must be a list")
    table: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict) or not _DIGEST.fullmatch(str(item.get("digest", ""))):
            raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest snapshot entry is invalid")
        if not _DIGEST.fullmatch(str(item.get("sha256", ""))):
            raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest snapshot hash is invalid")
        table[item["digest"]] = item
    return table


def _check_snapshot_bytes(digest: str, compressed: bytes) -> None:
    """Content-addressed check: decompressed snapshot bytes must hash to the
    digest in its name, independent of engine version or model evolution."""
    import gzip
    import zlib

    try:
        content = gzip.decompress(compressed)
    except (OSError, EOFError, zlib.error) as exc:
        raise _fail(ErrorCode.VALIDATION_ERROR, f"Snapshot {digest} is not readable gzip") from exc
    if hashlib.sha256(content).hexdigest() != digest:
        raise _fail(ErrorCode.VALIDATION_ERROR, f"Snapshot {digest} content hash mismatch")


def create_backup(source: Path, output: Path) -> dict[str, Any]:
    """Create a verified archive of an idle data directory; returns the manifest."""
    source = Path(os.path.abspath(source))
    output = Path(os.path.abspath(output))
    if not (source / "qscan.sqlite3").is_file():
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            "Source is not an initialized data directory; run qscan init",
        )
    if output.exists() or output.is_symlink():
        raise _fail(ErrorCode.VALIDATION_ERROR, f"Refusing to overwrite existing {output}")
    if output.parent.resolve() == source.resolve() or source in output.parents:
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            "Backup output must live outside the source data directory",
        )
    output.parent.mkdir(parents=True, exist_ok=True)

    # The executor lock serializes serve/scan/refresh/migration; a short
    # wait-and-refuse beats inventing a second locking scheme.
    lock = FileLock(source / "executor.lock", timeout=_LOCK_TIMEOUT)
    lock.acquire(timeout=_LOCK_TIMEOUT)
    try:
        work = Path(tempfile.mkdtemp(prefix="qscan-backup-"))
        staging = output.parent / f".{output.name}.staging"
    except Exception:
        lock.release()
        raise
    try:
        db_copy = work / "qscan.sqlite3"
        live = sqlite3.connect(f"file:{(source / 'qscan.sqlite3').as_uri()[7:]}?mode=ro", uri=True)
        snapshot_db = sqlite3.connect(db_copy)
        try:
            live.backup(snapshot_db)  # committed WAL content included
        finally:
            snapshot_db.close()
            live.close()

        report = _database_report(db_copy)  # everything below derives from the copy
        snapshots: list[dict[str, Any]] = []
        for digest in sorted(report["snapshot_digests"]):
            origin = source / "snapshots" / f"{digest}.json.gz"
            if not origin.is_file():
                raise _fail(
                    ErrorCode.INTERNAL_ERROR,
                    f"Database references missing snapshot {digest}; backup aborted",
                )
            compressed = origin.read_bytes()
            _check_snapshot_bytes(digest, compressed)
            sha, size = hashlib.sha256(compressed).hexdigest(), len(compressed)
            (work / "snapshots").mkdir(exist_ok=True)
            (work / "snapshots" / f"{digest}.json.gz").write_bytes(compressed)
            snapshots.append({"digest": digest, "sha256": sha, "size_bytes": size})

        settings: dict[str, dict[str, Any]] = {}
        if _SETTINGS_ALLOWLIST:
            (work / "settings").mkdir(exist_ok=True)
            for name in _SETTINGS_ALLOWLIST:
                if (source / name).is_file():
                    shutil.copy2(source / name, work / "settings" / name)
                    sha, size = _sha256_file(work / "settings" / name)
                    settings[name] = {"sha256": sha, "size_bytes": size}

        db_sha, db_size = _sha256_file(db_copy)
        manifest = {
            "application": "qscan",
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "engine_version": __version__,
            "schema_revision": report["schema_revision"],
            "database": {"sha256": db_sha, "size_bytes": db_size},
            "counts": {
                "runs": report["runs"],
                "runs_by_state": report["runs_by_state"],
                "watchlists": report["watchlists"],
                "snapshots": len(snapshots),
                "unfinished": {
                    "queued": report["runs_by_state"].get(RunState.QUEUED.value, 0),
                    "running": report["runs_by_state"].get(RunState.RUNNING.value, 0),
                },
            },
            "snapshots": snapshots,
            "settings": {"files": settings},
            "notes": [
                "Archive is unencrypted; it contains private watchlists and price "
                "history and must be stored securely. Checksums verify integrity "
                "only, not encryption or provenance.",
            ],
        }

        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(db_copy, _DB_ENTRY)
            for snapshot in snapshots:  # already digest-sorted
                name = _SNAPSHOT_PREFIX + snapshot["digest"] + ".json.gz"
                bundle.write(work / "snapshots" / f"{snapshot['digest']}.json.gz", name)
            for setting in sorted(settings):
                bundle.write(work / "settings" / setting, _SETTINGS_PREFIX + setting)
            bundle.writestr(_MANIFEST_ENTRY, json.dumps(manifest, indent=2, sort_keys=True))

        verify_backup(staging)  # never publish an archive we did not just re-verify
        try:
            os.link(staging, output)  # fails if output appeared meanwhile
        except FileExistsError as exc:
            raise _fail(
                ErrorCode.VALIDATION_ERROR, f"Refusing to overwrite existing {output}"
            ) from exc
        except OSError:
            if output.exists():
                raise _fail(
                    ErrorCode.VALIDATION_ERROR, f"Refusing to overwrite existing {output}"
                ) from None
            os.rename(staging, output)
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass
        lock.release()


def _extract_entry(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path, budget: dict[str, int]
) -> None:
    """Manual bounded extraction; never extractall, never trust stored sizes."""
    if info.file_size > _MAX_ENTRY_BYTES or budget["remaining"] < info.file_size:
        raise _fail(
            ErrorCode.VALIDATION_ERROR, f"Archive entry exceeds size limits: {info.filename}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    with archive.open(info) as origin, target.open("wb") as destination:
        while chunk := origin.read(1024 * 1024):
            copied += len(chunk)
            budget["remaining"] -= len(chunk)
            if copied > _MAX_ENTRY_BYTES or budget["remaining"] < 0:
                raise _fail(
                    ErrorCode.VALIDATION_ERROR,
                    f"Archive entry exceeds size limits: {info.filename}",
                )
            destination.write(chunk)


def verify_backup(archive: Path) -> dict[str, Any]:
    """Full offline verification; returns a machine-readable report."""
    archive = Path(os.path.abspath(archive))
    if not archive.is_file():
        raise _fail(ErrorCode.NOT_FOUND, f"Backup archive not found: {archive}")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) > _MAX_ENTRIES or len(set(names)) != len(names):
            raise _fail(ErrorCode.VALIDATION_ERROR, "Archive has duplicate or too many entries")
        for name in names:
            _safe_entry_name(name)
        for info in bundle.infolist():
            if (info.external_attr >> 16) & 0o170000 == 0o120000:  # S_IFLNK
                raise _fail(
                    ErrorCode.VALIDATION_ERROR, f"Archive entry is a symlink: {info.filename!r}"
                )
        manifest = _read_manifest(bundle)
        database = _database_entry_from_manifest(manifest)
        snapshot_table = _snapshot_entries_from_manifest(manifest)

        infos = {info.filename: info for info in bundle.infolist()}
        if _DB_ENTRY not in infos:
            raise _fail(ErrorCode.VALIDATION_ERROR, "Archive has no database entry")
        with tempfile.TemporaryDirectory(prefix="qscan-verify-") as temporary:
            db_copy = Path(temporary) / "qscan.sqlite3"
            budget = {"remaining": _MAX_TOTAL_BYTES}
            _extract_entry(bundle, infos[_DB_ENTRY], db_copy, budget)
            sha, size = _sha256_file(db_copy)
            if sha != database["sha256"] or size != database.get("size_bytes"):
                raise _fail(ErrorCode.VALIDATION_ERROR, "Database hash does not match the manifest")
            report = _database_report(db_copy)
            if manifest["schema_revision"] != report["schema_revision"]:
                raise _fail(
                    ErrorCode.VALIDATION_ERROR,
                    "Manifest schema revision does not match the archived database",
                )

            for digest in sorted(report["snapshot_digests"]):
                entry = _SNAPSHOT_PREFIX + digest + ".json.gz"
                recorded = snapshot_table.get(digest)
                if recorded is None or entry not in infos:
                    raise _fail(
                        ErrorCode.VALIDATION_ERROR,
                        f"Archive is missing database-referenced snapshot {digest}",
                    )
                compressed = bundle.read(infos[entry])
                if infos[entry].file_size > _MAX_ENTRY_BYTES:
                    raise _fail(
                        ErrorCode.VALIDATION_ERROR, f"Snapshot {digest} exceeds size limits"
                    )
                if len(compressed) != recorded["size_bytes"]:
                    raise _fail(ErrorCode.VALIDATION_ERROR, f"Snapshot {digest} size mismatch")
                if hashlib.sha256(compressed).hexdigest() != recorded["sha256"]:
                    raise _fail(ErrorCode.VALIDATION_ERROR, f"Snapshot {digest} hash mismatch")
                _check_snapshot_bytes(digest, compressed)
            budget.clear()

            recorded_state = manifest["counts"]
            expected = {
                "runs": report["runs"],
                "watchlists": report["watchlists"],
                "snapshots": len(snapshot_table),
            }
            if (
                recorded_state.get("runs") != expected["runs"]
                or recorded_state.get("watchlists") != expected["watchlists"]
                or recorded_state.get("snapshots") != expected["snapshots"]
            ):
                raise _fail(
                    ErrorCode.VALIDATION_ERROR,
                    "Manifest counts do not match the archived database",
                )

    warnings = []
    if manifest["engine_version"] != __version__:
        warnings.append(
            f"Archive was created by engine {manifest['engine_version']}; "
            f"this engine is {__version__}. Historical reports stay readable; "
            "exact replay requires the original engine version."
        )
    return {
        "valid": True,
        "archive": str(archive),
        "format_version": manifest["format_version"],
        "created_at": manifest["created_at"],
        "engine_version": manifest["engine_version"],
        "schema_revision": manifest["schema_revision"],
        "counts": manifest["counts"],
        "warnings": warnings,
    }


def restore_backup(archive: Path, destination: Path) -> dict[str, Any]:
    """Verify then restore an archive into a fresh destination directory.

    Never touches an existing directory, never migrates (run ``qscan init`` to
    upgrade an old schema), never resumes queued jobs, and never carries over
    credentials: the first ``init``/``serve`` in the destination creates a new
    local token.
    """
    report = verify_backup(archive)
    archive = Path(os.path.abspath(archive))
    destination = Path(os.path.abspath(destination))
    if destination.exists() or destination.is_symlink():
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            f"Destination {destination} already exists; restore only targets new directories",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.restore-staging"
    if staging.exists():
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            f"Stale restore staging directory {staging}; remove it and retry",
        )
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = [i for i in bundle.infolist() if i.filename != _MANIFEST_ENTRY]
            budget = {"remaining": _MAX_TOTAL_BYTES}
            for info in infos:
                name = info.filename
                if name == _DB_ENTRY:
                    target = staging / "qscan.sqlite3"
                elif name.startswith(_SNAPSHOT_PREFIX) or name.startswith(_SETTINGS_PREFIX):
                    target = staging / name
                else:
                    raise _fail(ErrorCode.VALIDATION_ERROR, f"Unexpected archive entry: {name!r}")
                _extract_entry(bundle, info, target, budget)
        os.rename(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "restored": True,
        "destination": str(destination),
        "engine_version": report["engine_version"],
        "schema_revision": report["schema_revision"],
        "counts": report["counts"],
        "warnings": report["warnings"]
        + [
            "New credentials are created on the first init/serve in the destination; "
            "old tokens are not carried over. QUEUED jobs resume and leftover "
            "RUNNING runs are finalized as WORKER_INTERRUPTED by the first serve "
            "startup; restore itself runs nothing."
        ],
    }
