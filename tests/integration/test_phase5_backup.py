"""Phase 5 backup/verify/restore: consistency, failure modes, safe restore.

Covers the release drills end to end at library and CLI level: an open
database with committed WAL content, missing/corrupt snapshots, busy
directories, tampered and untrusted archives, and the old-schema upgrade
path (explicit migration only, never implicit).
"""

import json
import os
import subprocess
import sys
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from filelock import FileLock, Timeout
from sqlalchemy import text

from qscan.adapters.backup import create_backup, restore_backup, verify_backup
from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.persistence.repository import open_database
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import ApplicationError, RawPrices
from qscan.bootstrap import bootstrap
from qscan.domain.models import RunState
from qscan.interfaces.api.localauth import TokenAuthenticator, ensure_token

SESSIONS = NYSECalendar().sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
CLOSES = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
CLOCK = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
SRC_ROOT = str(Path(__file__).parents[2] / "src")
# Every spawned interpreter gets the offline guard via sitecustomize:
OFFLINE_GUARD = str(Path(__file__).parents[2] / "tests" / "_offline_guard")
SRC_ROOT_AND_GUARD = OFFLINE_GUARD + os.pathsep + SRC_ROOT


def make_seed(directory: Path):
    """Two completed runs (distinct snapshots) plus one queued idempotency record."""
    app = bootstrap(
        FixtureProvider({"GOOD": _prices(RISING_CLOSES), "FLAT": _prices(FLAT_CLOSES)}),
        data_dir=directory,
        clock=CLOCK,
        calendar=NYSECalendar(),
    )
    try:
        rising = app.watchlists.create("rising", ["GOOD"])
        flat = app.watchlists.create("flat", ["FLAT"])
        run_a = app.scans.scan(rising.id, as_of=date(2026, 9, 4))
        run_b = app.scans.scan(flat.id, as_of=date(2026, 9, 4))
        queued = app.scans.prepare(flat.id, as_of=date(2026, 9, 3))
        created, _ = app.repository.enqueue(queued, "backup-key-000000001", "hash-0001", 20)
        assert created
    finally:
        app.close()
    ensure_token(directory / "api-token.json")
    return directory, run_a, run_b, queued


def _prices(rows) -> RawPrices:
    return RawPrices(rows)


RISING_CLOSES = tuple(zip(SESSIONS, CLOSES, strict=True))
FLAT_CLOSES = tuple(zip(SESSIONS, (100.0,) * len(SESSIONS), strict=True))


def _rebuild_zip(source: Path, target: Path, mutate) -> None:
    with zipfile.ZipFile(source) as bundle:
        entries = {name: bundle.read(name) for name in bundle.namelist()}
    mutate(entries)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in entries.items():
            bundle.writestr(name, payload)


def test_backup_verify_restore_roundtrip_preserves_history(tmp_path):
    directory, run_a, run_b, queued = make_seed(tmp_path / "資料 source")
    old_token = json.loads((directory / "api-token.json").read_text())["token"]
    archive = tmp_path / "backups" / "v1.zip"

    manifest = create_backup(directory, archive)
    assert manifest["counts"]["runs"] == 3
    assert manifest["counts"]["snapshots"] == 2
    assert manifest["counts"]["unfinished"] == {"queued": 1, "running": 0}
    with zipfile.ZipFile(archive) as bundle:
        assert "api-token.json" not in bundle.namelist()  # secrets never enter the archive

    report = verify_backup(archive)
    assert report["valid"] is True
    assert report["counts"]["runs"] == 3

    destination = tmp_path / "資料 restored"
    restore_backup(archive, destination)
    ensure_token(destination / "api-token.json")
    new_token = json.loads((destination / "api-token.json").read_text())["token"]
    assert new_token != old_token  # old credentials are never carried over

    restored = bootstrap(
        FixtureProvider({}),
        data_dir=destination,
        initialize=False,
        clock=CLOCK,
        calendar=NYSECalendar(),
    )
    try:
        for original in (run_a, run_b):
            copy = restored.queries.get(original.id)
            assert copy.state is original.state
            assert copy.counts == original.counts
            assert copy.input_hash == original.input_hash
            assert copy.comparison == original.comparison  # baseline binding survives
            assert [r.rank for r in copy.results] == [r.rank for r in original.results]
            assert [r.analysis.score for r in copy.results] == [
                r.analysis.score for r in original.results
            ]
            assert [r.analysis.stage for r in copy.results] == [
                r.analysis.stage for r in original.results
            ]
            assert [r.analysis.reasons for r in copy.results] == [
                r.analysis.reasons for r in original.results
            ]
        # The idempotency record still maps to the original run id.
        assert restored.repository.run_by_idempotency("backup-key-000000001") == (
            queued.id,
            "hash-0001",
        )
        authenticator = TokenAuthenticator(destination / "api-token.json")
        assert authenticator.check(f"Bearer {new_token}")
        assert not authenticator.check(f"Bearer {old_token}")
    finally:
        restored.close()


def test_backup_captures_open_database_with_wal(tmp_path):
    """Backup runs while the engine is still open; committed writes survive."""
    directory = tmp_path / "資料 live"
    app = bootstrap(
        FixtureProvider({"GOOD": _prices(RISING_CLOSES)}),
        data_dir=directory,
        clock=CLOCK,
        calendar=NYSECalendar(),
    )
    try:
        watchlist = app.watchlists.create("wl", ["GOOD"])
        run = app.scans.scan(watchlist.id, as_of=date(2026, 9, 4))
        assert (directory / "qscan.sqlite3-wal").exists(), "expected live WAL content"
        archive = tmp_path / "wal.zip"
        manifest = create_backup(directory, archive)
        assert manifest["counts"]["runs"] == 1
    finally:
        app.close()

    destination = tmp_path / "restored"
    restore_backup(archive, destination)
    reopened = bootstrap(
        FixtureProvider({}),
        data_dir=destination,
        initialize=False,
        clock=CLOCK,
        calendar=NYSECalendar(),
    )
    try:
        assert reopened.queries.get(run.id).state is RunState.SUCCEEDED
    finally:
        reopened.close()


def test_backup_refuses_locked_directory(tmp_path):
    directory, *_ = make_seed(tmp_path / "資料 busy")
    owner = FileLock(directory / "executor.lock", timeout=1)
    owner.acquire(timeout=1)
    try:
        with pytest.raises(Timeout):
            create_backup(directory, tmp_path / "b.zip")
    finally:
        owner.release()
    assert create_backup(directory, tmp_path / "b.zip")["counts"]["runs"] == 3


def test_backup_fails_on_missing_or_corrupt_snapshot(tmp_path):
    directory, *_ = make_seed(tmp_path / "資料 snaps")
    missing = next((directory / "snapshots").glob("*.json.gz"))
    missing.unlink()
    output = tmp_path / "out" / "missing.zip"
    with pytest.raises(ApplicationError, match="missing snapshot"):
        create_backup(directory, output)
    assert not output.exists()
    assert not list((tmp_path / "out").glob(".*staging*"))  # no staging leftovers

    # Corrupt content fails the backup even when the file exists.
    directory2, *_ = make_seed(tmp_path / "資料 corrupt")
    corrupt = next((directory2 / "snapshots").glob("*.json.gz"))
    corrupt.write_bytes(b"not gzip")
    with pytest.raises(ApplicationError, match="not readable gzip"):
        create_backup(directory2, tmp_path / "out" / "corrupt.zip")
    assert not (tmp_path / "out" / "corrupt.zip").exists()


def test_backup_rejects_existing_output_and_output_inside_source(tmp_path):
    directory, *_ = make_seed(tmp_path / "資料 place")
    existing = tmp_path / "b.zip"
    existing.write_bytes(b"previous backup")
    with pytest.raises(ApplicationError, match="Refusing to overwrite"):
        create_backup(directory, existing)

    inside = directory / "self.zip"
    with pytest.raises(ApplicationError, match="outside the source data directory"):
        create_backup(directory, inside)
    nested = directory / "nested" / "self.zip"
    with pytest.raises(ApplicationError, match="outside the source data directory"):
        create_backup(directory, nested)


def test_verify_rejects_tampering_and_unknown_versions(tmp_path):
    directory, *_ = make_seed(tmp_path / "資料 tamper")
    archive = tmp_path / "b.zip"
    create_backup(directory, archive)

    def with_db_byte(entries):
        entries["db/qscan.sqlite3"] = entries["db/qscan.sqlite3"] + b"x"

    _rebuild_zip(archive, tmp_path / "tampered.zip", with_db_byte)
    with pytest.raises(ApplicationError, match="hash"):
        verify_backup(tmp_path / "tampered.zip")

    def with_format(entries):
        manifest = json.loads(entries["manifest.json"])
        manifest["format_version"] = 99
        entries["manifest.json"] = json.dumps(manifest).encode()

    _rebuild_zip(archive, tmp_path / "future.zip", with_format)
    with pytest.raises(ApplicationError, match="Unsupported backup format"):
        verify_backup(tmp_path / "future.zip")

    def with_unknown_schema(entries):
        manifest = json.loads(entries["manifest.json"])
        manifest["schema_revision"] = "9999"
        entries["manifest.json"] = json.dumps(manifest).encode()

    _rebuild_zip(archive, tmp_path / "unknown.zip", with_unknown_schema)
    with pytest.raises(ApplicationError, match="schema revision"):
        verify_backup(tmp_path / "unknown.zip")


def _evil_zip(target: Path, names: list[str], symlink: str | None = None) -> None:
    with zipfile.ZipFile(target, "w") as bundle:
        for name in names:
            bundle.writestr(name, b"x")
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16  # S_IFLNK
            bundle.writestr(info, b"link-target")


def test_restore_rejects_untrusted_archives_and_never_partial_restores(tmp_path):
    evil_cases = [
        (["../evil.txt"], "Unsafe archive entry"),
        (["/abs.txt"], "Unsafe archive entry"),
        (["C:/evil.txt"], "Unsafe archive entry"),
        (["a/../../b.txt"], "Unsafe archive entry"),
        (["manifest.json", "manifest.json"], "duplicate"),
        (["snapshots/evil"], None),  # replaced below with a symlink entry
    ]
    for index, (names, message) in enumerate(evil_cases[:-1]):
        evil = tmp_path / f"evil{index}.zip"
        _evil_zip(evil, names)
        destination = tmp_path / f"dest{index}"
        with pytest.raises(ApplicationError, match=message):
            restore_backup(evil, destination)
        assert not destination.exists()
        assert not list(tmp_path.glob(".dest*.restore-staging"))

    link = tmp_path / "link.zip"
    _evil_zip(link, ["snapshots/ok.json.gz"], symlink="snapshots/link.json.gz")
    with pytest.raises(ApplicationError, match="symlink"):
        restore_backup(link, tmp_path / "dest-link")

    directory, *_ = make_seed(tmp_path / "資料 exist")
    archive = tmp_path / "ok.zip"
    create_backup(directory, archive)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    with pytest.raises(ApplicationError, match="already exists"):
        restore_backup(archive, occupied)
    # Stale fixed-name staging directories are impossible by construction
    # (staging names are unique per restore); a concurrent publish is covered
    # by the phase 5.1 tests.
    fresh = tmp_path / "fresh"
    restore_backup(archive, fresh)
    assert (fresh / "qscan.sqlite3").is_file()


def test_old_schema_backup_restore_then_explicit_upgrade(tmp_path):
    """Old-schema archives verify and restore; migration happens only when the
    user explicitly runs init, and the original archive stays untouched."""
    directory, run_a, *_ = make_seed(tmp_path / "資料 modern")
    document = json.loads(run_a.model_dump_json(exclude={"results"}))
    for key in ("progress", "data_mode", "warnings"):
        document.pop(key, None)

    legacy = tmp_path / "資料 legacy"
    legacy.mkdir()
    engine = open_database(legacy / "qscan.sqlite3")
    config = Config()
    config.set_main_option("path_separator", "os")
    config.set_main_option(
        "script_location",
        str(Path(SRC_ROOT) / "qscan" / "adapters" / "persistence" / "migrations"),
    )
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0002")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO scan_runs (id, owner_id, state, created_at, source_run_id,"
                " input_hash, document) VALUES (:i, 'local', :s, :c, NULL, :h, :d)"
            ),
            {
                "i": str(run_a.id),
                "s": RunState.SUCCEEDED.value,
                "c": run_a.requested_at.isoformat(),
                "h": run_a.input_hash,
                "d": json.dumps(document),
            },
        )
    engine.dispose()
    # The legacy directory also carries the snapshot the run references.
    import shutil

    (legacy / "snapshots").mkdir()
    shutil.copy2(
        directory / "snapshots" / f"{run_a.input_hash}.json.gz",
        legacy / "snapshots" / f"{run_a.input_hash}.json.gz",
    )

    archive = tmp_path / "legacy.zip"
    manifest = create_backup(legacy, archive)
    assert manifest["schema_revision"] == "0002"
    assert verify_backup(archive)["schema_revision"] == "0002"

    destination = tmp_path / "資料 upgraded"
    restore_backup(archive, destination)
    with pytest.raises(ApplicationError, match="Incompatible schema"):
        bootstrap(
            FixtureProvider({}),
            data_dir=destination,
            initialize=False,
            clock=CLOCK,
            calendar=NYSECalendar(),
        )
    upgraded = bootstrap(
        FixtureProvider({}),
        data_dir=destination,
        initialize=True,
        clock=CLOCK,
        calendar=NYSECalendar(),
    )
    try:
        page, _ = upgraded.queries.page(limit=10)
        assert [item.id for item in page] == [run_a.id]
        assert page[0].state is RunState.SUCCEEDED
        assert page[0].input_hash == run_a.input_hash
    finally:
        upgraded.close()
    verify_backup(archive)  # the original archive is still complete and valid


def test_backup_cli_commands_end_to_end(tmp_path):
    """The documented CLI syntax works as advertised, stdout stays JSON-only."""
    directory, *_ = make_seed(tmp_path / "資料 cli")
    archive = tmp_path / "cli.zip"
    destination = tmp_path / "資料 cli-restored"

    def run_cli(*arguments: str) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from qscan.interfaces.cli import app; app()",
                "--data-dir",
                str(directory),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": SRC_ROOT_AND_GUARD},
        )
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        return json.loads(result.stdout)

    created = run_cli("backup", "create", "--output", str(archive))
    assert created["created"] is True and created["manifest"]["counts"]["runs"] == 3
    verified = run_cli("backup", "verify", str(archive))
    assert verified["valid"] is True
    restored = run_cli("backup", "restore", str(archive), "--destination", str(destination))
    assert restored["token"] == "created"
    assert (destination / "api-token.json").is_file()
