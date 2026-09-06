"""Phase 5.1 backup regressions: bounds, consistency, format, single-source.

Each test crafts a self-consistent archive by hand, then applies ONE mutation
(oversized manifest, budget blowout, gzip bomb, hash-valid but unsupported
snapshot schema, undeclared/duplicate/missing entries, counts drift, source
swap, concurrent restore). Budgets are monkeypatched small so no test ever
allocates real gigabytes. The unmutated archive must verify cleanly — proving
enforcement, not blanket rejection.
"""

import gzip
import hashlib
import json
import shutil
import threading
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from qscan import __version__
from qscan.adapters import backup
from qscan.adapters.backup import restore_backup, verify_backup
from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.persistence.repository import open_database
from qscan.adapters.providers import FixtureProvider, resolve_us
from qscan.application.contracts import (
    CloseSeries,
    InputItem,
    InputSnapshot,
    RawPrices,
    RuleConfig,
    ScanContext,
    Watchlist,
)
from qscan.bootstrap import bootstrap

SESSIONS = NYSECalendar().sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
CLOSES = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
CLOCK = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))


def _valid_snapshot_plain() -> bytes:
    """A schema-1 snapshot that satisfies every contract invariant."""
    from qscan.adapters.snapshots import canonical

    context = ScanContext(
        as_of_session=SESSIONS[-1], reference_session=SESSIONS[-2], engine_version=__version__
    )
    instrument = resolve_us("GOOD")
    series = CloseSeries(instrument_id=instrument.id, sessions=SESSIONS[-50:], closes=(100.0,) * 50)
    snapshot = InputSnapshot(
        context=context,
        rules=RuleConfig(),
        watchlist=Watchlist(id=uuid4(), name="handcraft", revision=1, instruments=(instrument,)),
        calendar_version="test-calendar",
        expected_sessions=SESSIONS,
        items=(InputItem(instrument=instrument, series=series),),
    )
    return canonical(snapshot)


def _minimal_db(path: Path, digests: set[str]) -> None:
    engine = open_database(path)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        connection.execute(text("INSERT INTO alembic_version VALUES ('0003')"))
        connection.execute(
            text(
                "CREATE TABLE scan_runs (id VARCHAR(36) PRIMARY KEY, owner_id VARCHAR(50),"
                " state VARCHAR(20), created_at VARCHAR(40), input_hash VARCHAR(64),"
                " document TEXT)"
            )
        )
        for index, digest in enumerate(sorted(digests)):
            connection.execute(
                text(
                    "INSERT INTO scan_runs VALUES (:i, 'local', 'SUCCEEDED',"
                    " '2026-09-06T00:00:00+00:00', :h, '{}')"
                ),
                {"i": f"00000000-0000-0000-0000-{index:012d}", "h": digest},
            )
        connection.execute(text("CREATE TABLE watchlists (id VARCHAR(36))"))
    engine.dispose()


def _craft_archive(
    target: Path,
    *,
    snapshot_plain: bytes,
    manifest_mutator=None,
    extra_entries: dict[str, bytes] | None = None,
    drop_entries: set[str] | None = None,
    minimal_db: bool = True,
) -> Path:
    """A self-consistent archive the test then mutates in exactly one way."""
    digest = hashlib.sha256(snapshot_plain).hexdigest()
    compressed = gzip.compress(snapshot_plain, mtime=0)
    work = target.parent / f".craft-{target.name}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        db_path = work / "qscan.sqlite3"
        if minimal_db:
            _minimal_db(db_path, {digest})
        db_sha, db_size = backup._sha256_file(db_path)
        manifest = {
            "application": "qscan",
            "format_version": backup.BACKUP_FORMAT_VERSION,
            "created_at": "2026-09-06T00:00:00+00:00",
            "engine_version": __version__,
            "schema_revision": "0003",
            "database": {"sha256": db_sha, "size_bytes": db_size},
            "counts": {
                "runs": 1,
                "runs_by_state": {"SUCCEEDED": 1},
                "watchlists": 0,
                "snapshots": 1,
                "unfinished": {"queued": 0, "running": 0},
            },
            "snapshots": [
                {
                    "digest": digest,
                    "sha256": hashlib.sha256(compressed).hexdigest(),
                    "size_bytes": len(compressed),
                }
            ],
            "settings": {"files": {}},
        }
        if manifest_mutator is not None:
            manifest_mutator(manifest)
        entries: dict[str, bytes] = {
            backup._DB_ENTRY: db_path.read_bytes(),
            f"{backup._SNAPSHOT_PREFIX}{digest}.json.gz": compressed,
            backup._MANIFEST_ENTRY: json.dumps(manifest, indent=2, sort_keys=True).encode(),
        }
        entries.update(extra_entries or {})
        for name in drop_entries or set():
            entries.pop(name, None)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, payload in entries.items():
                bundle.writestr(name, payload)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_handcrafted_consistent_archive_verifies(tmp_path):
    """Control case: the hand-built archive passes, so rejections below are
    caused by the specific mutation, not by crafting."""
    archive = _craft_archive(tmp_path / "ok.zip", snapshot_plain=_valid_snapshot_plain())
    report = verify_backup(archive)
    assert report["valid"] is True
    assert report["counts"]["runs"] == 1


def test_oversized_manifest_rejected_before_read(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_MANIFEST_BYTES", 128)
    archive = _craft_archive(tmp_path / "ok.zip", snapshot_plain=_valid_snapshot_plain())
    # Phase 6A: the manifest counts against the shared bounded-read path, so
    # the rejection uses the unified entry-limit message (still before reading).
    with pytest.raises(backup.ApplicationError, match="exceeds size limits: manifest.json"):
        verify_backup(archive)


def test_cumulative_entry_budget_rejects_accumulation(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_MAX_TOTAL_BYTES", 1024)
    # Several individually tiny entries that together blow the total budget.
    plain = _valid_snapshot_plain()
    compressed = gzip.compress(plain, mtime=0)
    extra = {
        f"snapshots/{hashlib.sha256(plain + bytes([i])).hexdigest()}.json.gz": compressed
        for i in range(5)
    }
    archive = _craft_archive(
        tmp_path / "ok.zip",
        snapshot_plain=plain,
        extra_entries=extra,
        manifest_mutator=lambda m: m.__setitem__(
            "snapshots",
            m["snapshots"]
            + [
                {
                    "digest": hashlib.sha256(plain + bytes([i])).hexdigest(),
                    "sha256": hashlib.sha256(compressed).hexdigest(),
                    "size_bytes": len(compressed),
                }
                for i in range(5)
            ],
        ),
    )
    with pytest.raises(backup.ApplicationError, match="budget"):
        verify_backup(archive)


def test_gzip_bomb_rejected_at_decompressed_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_MAX_SNAPSHOT_PLAIN_BYTES", 4096)
    bomb_plain = b"0" * (1024 * 1024)  # 1 MB plain, tiny when compressed
    digest = hashlib.sha256(bomb_plain).hexdigest()
    archive = _craft_archive(tmp_path / "bomb.zip", snapshot_plain=bomb_plain)
    # The crafted name/hash is self-consistent; only the decompressed cap stops it.
    with pytest.raises(backup.ApplicationError, match="decompressed size"):
        verify_backup(archive)
    assert digest  # noqa: B018 - digest is part of the crafted entry name


def test_hash_valid_but_unsupported_schema_rejected(tmp_path):
    """A snapshot whose hash is self-consistent but whose schema the engine
    cannot decode is rejected on FORMAT, not on hash."""
    schema_two = json.dumps({"schema_version": 2, "anything": True}).encode()
    archive = _craft_archive(tmp_path / "schema2.zip", snapshot_plain=schema_two)
    with pytest.raises(backup.ApplicationError, match="supported snapshot schema"):
        verify_backup(archive)


def test_undeclared_entries_rejected(tmp_path):
    plain = _valid_snapshot_plain()
    archive = _craft_archive(
        tmp_path / "ok.zip",
        snapshot_plain=plain,
        extra_entries={
            f"snapshots/{'f' * 64}.json.gz": gzip.compress(b"{}", mtime=0),
            "settings/some-file.json": b"{}",
        },
    )
    with pytest.raises(backup.ApplicationError, match="does not declare"):
        verify_backup(archive)


def test_duplicate_and_missing_digest_rejected(tmp_path):
    plain = _valid_snapshot_plain()
    archive = _craft_archive(
        tmp_path / "dup.zip",
        snapshot_plain=plain,
        manifest_mutator=lambda m: m["snapshots"].append(dict(m["snapshots"][0])),
    )
    with pytest.raises(backup.ApplicationError, match="duplicate snapshot digests"):
        verify_backup(archive)

    missing_digest = hashlib.sha256(plain + b"x").hexdigest()
    archive2 = _craft_archive(
        tmp_path / "missing.zip",
        snapshot_plain=plain,
        manifest_mutator=lambda m: m["snapshots"].append(
            {
                "digest": missing_digest,
                "sha256": missing_digest,
                "size_bytes": 1,
            }
        ),
    )
    # The DB does not reference it AND the entry is absent: both reject.
    with pytest.raises(backup.ApplicationError):
        verify_backup(archive2)

    archive3 = _craft_archive(
        tmp_path / "missingentry.zip",
        snapshot_plain=plain,
        manifest_mutator=lambda m: m.__setitem__(
            "snapshots",
            [
                *m["snapshots"],
                {
                    "digest": "a" * 64,
                    "sha256": "b" * 64,
                    "size_bytes": 1,
                },
            ],
        ),
        minimal_db=True,
    )
    with pytest.raises(backup.ApplicationError, match="missing declared"):
        verify_backup(archive3)


def test_counts_drift_rejected(tmp_path):
    archive = _craft_archive(
        tmp_path / "counts.zip",
        snapshot_plain=_valid_snapshot_plain(),
        manifest_mutator=lambda m: m["counts"].__setitem__("runs", 5),
    )
    with pytest.raises(backup.ApplicationError, match="counts do not match"):
        verify_backup(archive)


def test_restore_reverifies_source_swapped_after_verify(tmp_path):
    """verify(path) then restore(path) on a file rewritten in between must
    fail on the NEW content — restore never trusts a prior check."""
    plain = _valid_snapshot_plain()
    good = _craft_archive(tmp_path / "good.zip", snapshot_plain=plain)
    assert verify_backup(good)["valid"] is True

    destination = tmp_path / "restored"
    tampered = _craft_archive(
        tmp_path / "tampered.zip",
        snapshot_plain=plain,
        manifest_mutator=lambda m: m["counts"].__setitem__("runs", 7),
    )
    shutil.copyfile(tampered, good)  # swap the source after the earlier verify
    with pytest.raises(backup.ApplicationError, match="counts do not match"):
        restore_backup(good, destination)
    assert not destination.exists()


def test_concurrent_restore_single_winner(tmp_path):
    plain = _valid_snapshot_plain()
    archive = _craft_archive(tmp_path / "ok.zip", snapshot_plain=plain)
    destination = tmp_path / "restored"
    results: list[str] = []

    def attempt() -> None:
        try:
            restore_backup(archive, destination)
            results.append("ok")
        except backup.ApplicationError as exc:
            results.append(str(exc))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1, results  # exactly one concurrent winner
    assert destination.joinpath("qscan.sqlite3").is_file()


def test_real_backup_roundtrip_survives_hardened_verify(tmp_path):
    """No regression: a REAL seeded directory still backs up, verifies and
    restores under the tightened rules (format decode included)."""
    from qscan.interfaces.api.localauth import ensure_token

    directory = tmp_path / "資料 real"
    app = bootstrap(
        FixtureProvider({"GOOD": RawPrices(tuple(zip(SESSIONS, CLOSES, strict=True)))}),
        data_dir=directory,
        clock=CLOCK,
        calendar=NYSECalendar(),
    )
    try:
        watchlist = app.watchlists.create("wl", ["GOOD"])
        run = app.scans.scan(watchlist.id, as_of=date(2026, 9, 4))
    finally:
        app.close()
    archive = tmp_path / "real.zip"
    create = backup.create_backup(directory, archive)
    assert create["counts"]["runs"] == 1
    assert verify_backup(archive)["valid"] is True
    destination = tmp_path / "資料 restored"
    restore_backup(archive, destination)
    ensure_token(destination / "api-token.json")
    reopened = bootstrap(
        FixtureProvider({}),
        data_dir=destination,
        initialize=False,
        clock=CLOCK,
        calendar=NYSECalendar(),
    )
    try:
        assert reopened.queries.get(run.id).state.value == "SUCCEEDED"
    finally:
        reopened.close()


def test_sqlite_wal_reset_status_classification():
    """Dated advisory knowledge: fixed releases pass, everything else below
    3.51.3 is AFFECTED even when its patch level looks newer."""
    from qscan.adapters.diagnostics import sqlite_wal_reset_status

    assert sqlite_wal_reset_status("3.53.4").startswith("OK")
    assert sqlite_wal_reset_status("3.51.3").startswith("OK")
    assert sqlite_wal_reset_status("3.50.7").startswith("OK")  # branch backport
    assert sqlite_wal_reset_status("3.44.6").startswith("OK")  # branch backport
    assert "AFFECTED" in sqlite_wal_reset_status("3.50.4")
    assert "AFFECTED" in sqlite_wal_reset_status("3.50.9")  # not the backport
    assert "AFFECTED" in sqlite_wal_reset_status("3.46.1")
