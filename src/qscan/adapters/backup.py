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

Verification treats the archive as untrusted input end to end:
- every entry (manifest included) is size-capped before it is fully read;
- decompression (zip and inner gzip) streams with running counters, so a
  bomb fails at the cap instead of after allocating;
- the entry set must equal exactly what the manifest declares (no undeclared
  settings or snapshots, no duplicate digests, nothing missing);
- snapshot content that hashes correctly but does not decode as a supported
  snapshot schema is still rejected;
- counts in the manifest must match the archived database.

Restore only ever targets a fresh destination directory, verifies and
extracts from the SAME opened archive (a file swapped between a prior verify
and the restore is re-checked, never trusted), and publishes through an
exclusive claim so concurrent restores cannot both succeed.
"""

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from filelock import FileLock
from pydantic import ValidationError

from qscan import __version__
from qscan.application.contracts import ApplicationError, InputSnapshot
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
_SETTINGS_ALLOWLIST: frozenset[str] = frozenset()
# Untrusted-input limits. Tests shrink these to prove enforcement without
# allocating gigabytes; every limit is checked WHILE reading, not after.
_MANIFEST_BYTES = 256 * 1024
_MAX_ENTRIES = 10_000
_MAX_DB_ENTRY_BYTES = 2**31
_MAX_SNAPSHOT_ENTRY_BYTES = 64 * 1024 * 1024
_MAX_SNAPSHOT_PLAIN_BYTES = 256 * 1024 * 1024
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


def _entry_names(bundle: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    raw_entries = bundle.infolist()
    infos = {info.filename: info for info in raw_entries}
    if len(raw_entries) != len(infos):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Archive has duplicate entries")
    if len(infos) > _MAX_ENTRIES:
        raise _fail(ErrorCode.VALIDATION_ERROR, "Archive has too many entries")
    lowered: set[str] = set()
    for name in infos:
        _safe_entry_name(name)
        if (infos[name].external_attr >> 16) & 0o170000 == 0o120000:  # S_IFLNK
            raise _fail(ErrorCode.VALIDATION_ERROR, f"Archive entry is a symlink: {name!r}")
        if name.lower() in lowered:
            raise _fail(
                ErrorCode.VALIDATION_ERROR,
                f"Archive entries collide case-insensitively: {name!r}",
            )
        lowered.add(name.lower())
    return infos


def _read_bounded(
    bundle: zipfile.ZipFile, info: zipfile.ZipInfo, budget: dict[str, int], cap: int
) -> bytes:
    """Read one entry with a per-entry cap and a shared cumulative budget,
    counting while streaming — never allocating first and checking after."""
    if info.file_size > cap:
        raise _fail(
            ErrorCode.VALIDATION_ERROR, f"Archive entry exceeds size limits: {info.filename}"
        )
    if budget["remaining"] < info.file_size:
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            f"Archive exceeds the total size budget at {info.filename}",
        )
    chunks: list[bytes] = []
    taken = 0
    with bundle.open(info) as stream:
        while chunk := stream.read(1024 * 1024):
            taken += len(chunk)
            if taken > cap or budget["remaining"] - taken < 0:
                raise _fail(
                    ErrorCode.VALIDATION_ERROR,
                    f"Archive entry exceeds size limits: {info.filename}",
                )
            chunks.append(chunk)
    budget["remaining"] -= taken
    return b"".join(chunks)


def _typed_manifest(manifest: Any) -> dict[str, Any]:
    """Structural validation with typed errors; no bare KeyError may escape."""
    if not isinstance(manifest, dict):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest must be an object")
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            f"Unsupported backup format version: {manifest.get('format_version')!r}; "
            f"this engine reads format {BACKUP_FORMAT_VERSION}",
        )
    required = (
        "created_at",
        "engine_version",
        "schema_revision",
        "database",
        "counts",
        "snapshots",
    )
    for key in required:
        if key not in manifest:
            raise _fail(ErrorCode.VALIDATION_ERROR, f"Backup manifest is missing field: {key}")
    database = manifest["database"]
    if not isinstance(database, dict) or not isinstance(database.get("size_bytes"), int):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest database entry is invalid")
    if not _DIGEST.fullmatch(str(database.get("sha256", ""))):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest database hash is invalid")
    snapshots = manifest["snapshots"]
    if not isinstance(snapshots, list):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest snapshots must be a list")
    for item in snapshots:
        if not isinstance(item, dict) or not isinstance(item.get("size_bytes"), int):
            raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest snapshot entry is invalid")
        if not _DIGEST.fullmatch(str(item.get("digest", ""))) or not _DIGEST.fullmatch(
            str(item.get("sha256", ""))
        ):
            raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest snapshot hash is invalid")
    if len({item["digest"] for item in snapshots}) != len(snapshots):
        raise _fail(
            ErrorCode.VALIDATION_ERROR, "Backup manifest declares duplicate snapshot digests"
        )
    settings = manifest.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest settings must be an object")
    counts = manifest["counts"]
    if not isinstance(counts, dict) or not isinstance(counts.get("unfinished"), dict):
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest counts are invalid")
    return manifest


def _read_manifest(bundle: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo]) -> dict[str, Any]:
    if _MANIFEST_ENTRY not in infos:
        raise _fail(ErrorCode.VALIDATION_ERROR, "Archive has no backup manifest")
    if infos[_MANIFEST_ENTRY].file_size > _MANIFEST_BYTES:
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            f"Backup manifest exceeds {_MANIFEST_BYTES} bytes and is rejected before reading",
        )
    try:
        manifest = json.loads(bundle.read(infos[_MANIFEST_ENTRY]))
    except (ValueError, zipfile.BadZipFile) as exc:
        raise _fail(ErrorCode.VALIDATION_ERROR, "Backup manifest is not valid JSON") from exc
    return _typed_manifest(manifest)


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


def _decode_snapshot(digest: str, compressed: bytes) -> bytes:
    """Bounds-checked gunzip (multi-member safe) plus content-address check.

    Returns the plain canonical bytes so the caller can also validate the
    snapshot schema; a correct hash alone never proves a decodable format.
    """
    digest_stream = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as reader:
            while chunk := reader.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_SNAPSHOT_PLAIN_BYTES:
                    raise _fail(
                        ErrorCode.VALIDATION_ERROR,
                        f"Snapshot {digest} decompressed size exceeds the limit",
                    )
                digest_stream.update(chunk)
                chunks.append(chunk)
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise _fail(ErrorCode.VALIDATION_ERROR, f"Snapshot {digest} is not readable gzip") from exc
    if digest_stream.hexdigest() != digest:
        raise _fail(ErrorCode.VALIDATION_ERROR, f"Snapshot {digest} content hash mismatch")
    return b"".join(chunks)


def _snapshot_table(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    table = {item["digest"]: item for item in manifest["snapshots"]}
    if len(table) != len(manifest["snapshots"]):
        raise _fail(
            ErrorCode.VALIDATION_ERROR, "Backup manifest declares duplicate snapshot digests"
        )
    return table


def _verify_bundle(bundle: zipfile.ZipFile) -> tuple[dict[str, Any], dict[str, Any]]:
    """Full verification of an already-opened archive. Both verify and restore
    use this on the SAME opened source, so nothing can trust a stale check."""
    infos = _entry_names(bundle)
    manifest = _read_manifest(bundle, infos)
    snapshot_table = _snapshot_table(manifest)
    settings = manifest.get("settings", {}).get("files", {})

    declared = {_DB_ENTRY, _MANIFEST_ENTRY}
    for digest in snapshot_table:
        declared.add(f"{_SNAPSHOT_PREFIX}{digest}.json.gz")
    for name in settings:
        if name not in _SETTINGS_ALLOWLIST:
            raise _fail(
                ErrorCode.VALIDATION_ERROR, f"Setting {name!r} is not in the backup allowlist"
            )
        declared.add(f"{_SETTINGS_PREFIX}{name}")
    extras = set(infos) - declared
    if extras:
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            f"Archive contains entries the manifest does not declare: {sorted(extras)}",
        )
    missing = declared - set(infos)
    if missing:
        raise _fail(
            ErrorCode.VALIDATION_ERROR, f"Archive is missing declared entries: {sorted(missing)}"
        )

    budget = {"remaining": _MAX_TOTAL_BYTES}
    with tempfile.TemporaryDirectory(prefix="qscan-verify-") as temporary:
        db_copy = Path(temporary) / "qscan.sqlite3"
        db_bytes = _read_bounded(bundle, infos[_DB_ENTRY], budget, _MAX_DB_ENTRY_BYTES)
        db_copy.write_bytes(db_bytes)
        sha, size = _sha256_file(db_copy)
        database = manifest["database"]
        if sha != database["sha256"] or size != database["size_bytes"]:
            raise _fail(ErrorCode.VALIDATION_ERROR, "Database hash does not match the manifest")
        report = _database_report(db_copy)
        if manifest["schema_revision"] != report["schema_revision"]:
            raise _fail(
                ErrorCode.VALIDATION_ERROR,
                "Manifest schema revision does not match the archived database",
            )

        for digest in sorted(report["snapshot_digests"]):
            if digest not in snapshot_table:
                raise _fail(
                    ErrorCode.VALIDATION_ERROR,
                    f"Archive is missing database-referenced snapshot {digest}",
                )
            entry = f"{_SNAPSHOT_PREFIX}{digest}.json.gz"
            recorded = snapshot_table[digest]
            compressed = _read_bounded(bundle, infos[entry], budget, _MAX_SNAPSHOT_ENTRY_BYTES)
            if len(compressed) != recorded["size_bytes"]:
                raise _fail(ErrorCode.VALIDATION_ERROR, f"Snapshot {digest} size mismatch")
            if hashlib.sha256(compressed).hexdigest() != recorded["sha256"]:
                raise _fail(ErrorCode.VALIDATION_ERROR, f"Snapshot {digest} hash mismatch")
            plain = _decode_snapshot(digest, compressed)
            try:
                InputSnapshot.model_validate_json(plain)
            except ValidationError as exc:
                raise _fail(
                    ErrorCode.VALIDATION_ERROR,
                    f"Snapshot {digest} is not a supported snapshot schema "
                    f"({exc.error_count()} validation error(s))",
                ) from exc

        for digest in sorted(set(snapshot_table) - report["snapshot_digests"]):
            raise _fail(
                ErrorCode.VALIDATION_ERROR,
                f"Manifest declares snapshot {digest} that the database never references",
            )

        counts = manifest["counts"]
        expected = {
            "runs": report["runs"],
            "watchlists": report["watchlists"],
            "snapshots": len(snapshot_table),
        }
        if (
            counts.get("runs") != expected["runs"]
            or counts.get("watchlists") != expected["watchlists"]
            or counts.get("snapshots") != expected["snapshots"]
            or counts.get("runs_by_state") != report["runs_by_state"]
            or counts.get("unfinished")
            != {
                "queued": report["runs_by_state"].get(RunState.QUEUED.value, 0),
                "running": report["runs_by_state"].get(RunState.RUNNING.value, 0),
            }
        ):
            raise _fail(
                ErrorCode.VALIDATION_ERROR,
                "Manifest counts do not match the archived database",
            )

    warnings: list[str] = []
    if manifest["engine_version"] != __version__:
        warnings.append(
            f"Archive was created by engine {manifest['engine_version']}; "
            f"this engine is {__version__}. Historical reports stay readable; "
            "exact replay requires the original engine version."
        )
    return manifest, {
        "counts": manifest["counts"],
        "schema_revision": manifest["schema_revision"],
        "engine_version": manifest["engine_version"],
        "warnings": warnings,
    }


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
        snapshot_dir = work / "snapshots"
        for digest in sorted(report["snapshot_digests"]):
            origin = source / "snapshots" / f"{digest}.json.gz"
            if not origin.is_file():
                raise _fail(
                    ErrorCode.INTERNAL_ERROR,
                    f"Database references missing snapshot {digest}; backup aborted",
                )
            compressed = origin.read_bytes()
            if len(compressed) > _MAX_SNAPSHOT_ENTRY_BYTES:
                raise _fail(
                    ErrorCode.VALIDATION_ERROR,
                    f"Snapshot {digest} exceeds the archive entry limit",
                )
            plain = _decode_snapshot(digest, compressed)  # corrupt source fails the backup
            try:
                InputSnapshot.model_validate_json(plain)
            except ValidationError as exc:
                raise _fail(
                    ErrorCode.VALIDATION_ERROR,
                    f"Snapshot {digest} is not a supported snapshot schema; backup aborted",
                ) from exc
            snapshot_dir.mkdir(exist_ok=True)
            (snapshot_dir / f"{digest}.json.gz").write_bytes(compressed)
            snapshots.append(
                {
                    "digest": digest,
                    "sha256": hashlib.sha256(compressed).hexdigest(),
                    "size_bytes": len(compressed),
                }
            )

        settings: dict[str, dict[str, Any]] = {}
        if _SETTINGS_ALLOWLIST:
            (work / "settings").mkdir(exist_ok=True)
            for name in sorted(_SETTINGS_ALLOWLIST):
                if (source / name).is_file():
                    shutil.copy2(source / name, work / "settings" / name)
                    sha, size = _sha256_file(work / "settings" / name)
                    settings[name] = {"sha256": sha, "size_bytes": size}

        db_sha, db_size = _sha256_file(db_copy)
        manifest: dict[str, Any] = {
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
                name = f"{_SNAPSHOT_PREFIX}{snapshot['digest']}.json.gz"
                bundle.write(snapshot_dir / f"{snapshot['digest']}.json.gz", name)
            for setting in sorted(settings):
                bundle.write(work / "settings" / setting, f"{_SETTINGS_PREFIX}{setting}")
            bundle.writestr(_MANIFEST_ENTRY, json.dumps(manifest, indent=2, sort_keys=True))

        with zipfile.ZipFile(staging) as bundle:
            _verify_bundle(bundle)  # never publish an archive we did not verify
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


def verify_backup(archive: Path) -> dict[str, Any]:
    """Full offline verification; returns a machine-readable report."""
    archive = Path(os.path.abspath(archive))
    if not archive.is_file():
        raise _fail(ErrorCode.NOT_FOUND, f"Backup archive not found: {archive}")
    with zipfile.ZipFile(archive) as bundle:
        manifest, summary = _verify_bundle(bundle)
    return {
        "valid": True,
        "archive": str(archive),
        "format_version": manifest["format_version"],
        "created_at": manifest["created_at"],
        "engine_version": manifest["engine_version"],
        "schema_revision": manifest["schema_revision"],
        "counts": manifest["counts"],
        "warnings": summary["warnings"],
    }


def restore_backup(archive: Path, destination: Path) -> dict[str, Any]:
    """Verify and restore ONE opened archive into a fresh destination.

    Never touches an existing directory, never migrates (run ``qscan init`` to
    upgrade an old schema), never resumes queued jobs, and never carries over
    credentials: the first ``init``/``serve`` in the destination creates a new
    local token. Publication claims the destination exclusively so two
    concurrent restores cannot both succeed.
    """
    archive = Path(os.path.abspath(archive))
    destination = Path(os.path.abspath(destination))
    if destination.exists() or destination.is_symlink():
        raise _fail(
            ErrorCode.VALIDATION_ERROR,
            f"Destination {destination} already exists; restore only targets new directories",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent))
    claim = destination.parent / f".{destination.name}.restore-claim"
    try:
        with zipfile.ZipFile(archive) as bundle:  # the SAME source, re-verified
            manifest, summary = _verify_bundle(bundle)
            infos = _entry_names(bundle)
            budget = {"remaining": _MAX_TOTAL_BYTES}
            for name, info in sorted(infos.items()):
                if name == _MANIFEST_ENTRY:
                    continue
                if name == _DB_ENTRY:
                    target = staging / "qscan.sqlite3"
                elif name.startswith(_SNAPSHOT_PREFIX) or name.startswith(_SETTINGS_PREFIX):
                    target = staging / name
                else:
                    raise _fail(ErrorCode.VALIDATION_ERROR, f"Unexpected archive entry: {name!r}")
                cap = _MAX_DB_ENTRY_BYTES if name == _DB_ENTRY else _MAX_SNAPSHOT_ENTRY_BYTES
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_read_bounded(bundle, info, budget, cap))
        try:
            handle = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(handle)
        except FileExistsError as exc:
            raise _fail(
                ErrorCode.VALIDATION_ERROR,
                "Another restore is already publishing this destination",
            ) from exc
        try:
            if destination.exists():
                raise _fail(
                    ErrorCode.VALIDATION_ERROR,
                    f"Destination {destination} appeared during restore",
                )
            os.rename(staging, destination)
        finally:
            try:
                os.unlink(claim)
            except OSError:
                pass
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "restored": True,
        "destination": str(destination),
        "engine_version": summary["engine_version"],
        "schema_revision": summary["schema_revision"],
        "counts": summary["counts"],
        "warnings": summary["warnings"]
        + [
            "New credentials are created on the first init/serve in the destination; "
            "old tokens are not carried over. QUEUED jobs resume and leftover "
            "RUNNING runs are finalized as WORKER_INTERRUPTED by the first serve "
            "startup; restore itself runs nothing."
        ],
    }
