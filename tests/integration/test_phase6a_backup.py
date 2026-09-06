"""Phase 6A backup regressions: streaming DB reads, canonical snapshots, publish races.

Complements the Phase 5/5.1 suites: the database entry must stream to disk
(never fully resident), snapshot validation must be one shared gate identical
to the formal reader (hash + schema + canonical bytes), and publication must
refuse overwrites through an atomic no-clobber path — including on filesystems
without hardlinks. No test allocates gigabytes; caps are monkeypatched small.
"""

import hashlib
import json
import zipfile

import pytest
from test_phase5_backup import make_seed
from test_phase51_backup import _craft_archive, _valid_snapshot_plain

from qscan.adapters import backup
from qscan.adapters.backup import _validated_plain, create_backup, verify_backup
from qscan.adapters.snapshots import SnapshotStore
from qscan.application.contracts import ApplicationError


def test_database_entry_streams_with_tiny_cap(tmp_path, monkeypatch):
    """The DB entry is capped WHILE streaming to disk: a small cap fails at the
    cap during the read, never after allocating the whole bytes payload."""
    monkeypatch.setattr(backup, "_MAX_DB_ENTRY_BYTES", 1024)
    archive = _craft_archive(tmp_path / "ok.zip", snapshot_plain=_valid_snapshot_plain())
    with pytest.raises(backup.ApplicationError, match="exceeds size limits"):
        verify_backup(archive)


def test_manifest_counts_against_total_budget(tmp_path, monkeypatch):
    """The manifest shares the archive-wide budget: oversized manifest bytes
    cannot slip past the same total limit every other entry obeys."""
    monkeypatch.setattr(backup, "_MAX_TOTAL_BYTES", 256)
    archive = _craft_archive(tmp_path / "ok.zip", snapshot_plain=_valid_snapshot_plain())
    with pytest.raises(backup.ApplicationError, match="budget|exceeds size limits"):
        verify_backup(archive)


def test_verify_accepts_only_what_the_formal_reader_accepts(tmp_path):
    """Contract: what backup verify accepts, the formal SnapshotStore reader
    accepts too. A noncanonical-but-valid JSON document is rejected by BOTH."""
    plain = _valid_snapshot_plain()
    digest = hashlib.sha256(plain).hexdigest()
    compressed = zipfile_compress(plain)

    # The shared gate accepts exactly the reader's canonical form.
    assert _validated_plain(digest, compressed) == plain

    # Same schema, same content, noncanonical serialization.
    noncanonical = json.dumps(json.loads(plain), indent=2).encode()
    noncanonical_digest = hashlib.sha256(noncanonical).hexdigest()
    noncanonical_compressed = zipfile_compress(noncanonical)
    with pytest.raises(ApplicationError, match="content hash mismatch"):
        _validated_plain(digest, noncanonical_compressed)  # content address no longer matches
    with pytest.raises(ApplicationError, match="not canonical"):
        _validated_plain(noncanonical_digest, noncanonical_compressed)  # hash ok, format not
    # The formal reader rejects the same document too.
    store = SnapshotStore(tmp_path / "snapshots")  # constructor creates the directory
    (store.directory / (noncanonical_digest + ".json.gz")).write_bytes(noncanonical_compressed)
    with pytest.raises(ApplicationError):
        store.read(noncanonical_digest)


def zipfile_compress(raw: bytes) -> bytes:
    import gzip

    return gzip.compress(raw, mtime=0)


def test_verify_then_restore_snapshots_are_reader_clean(tmp_path):
    """End to end: snapshots inside a verified archive read back through the
    formal SnapshotStore after extraction."""
    plain = _valid_snapshot_plain()
    digest = hashlib.sha256(plain).hexdigest()
    compressed = zipfile_compress(plain)
    archive = _craft_archive(tmp_path / "real.zip", snapshot_plain=plain)
    assert verify_backup(archive)["valid"] is True
    store = SnapshotStore(tmp_path / "snapshots")
    (store.directory / (digest + ".json.gz")).write_bytes(compressed)
    assert store.read(digest) is not None


def test_run_snapshot_mismatch_still_rejected(tmp_path):
    """A manifest that declares a snapshot no run references is rejected: the
    DB-referenced set must equal the manifest set exactly."""
    plain = _valid_snapshot_plain()
    compressed = zipfile_compress(plain)
    orphan_digest = hashlib.sha256(plain + b"x").hexdigest()
    archive = _craft_archive(
        tmp_path / "ok.zip",
        snapshot_plain=plain,
        extra_entries={f"snapshots/{orphan_digest}.json.gz": compressed},
        manifest_mutator=lambda m: m["snapshots"].append(
            {
                "digest": orphan_digest,
                "sha256": hashlib.sha256(compressed).hexdigest(),
                "size_bytes": len(compressed),
            }
        ),
    )
    with pytest.raises(ApplicationError, match="never references"):
        verify_backup(archive)


def test_existing_output_never_clobbered(tmp_path):
    """A destination that already exists is refused and left byte-identical,
    with the unique staging file cleaned up."""
    directory = tmp_path / "資料 src"
    make_seed(directory)
    output = tmp_path / "backups" / "daily.zip"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"SENTINEL-NOT-AN-ARCHIVE")
    with pytest.raises(ApplicationError, match="Refusing to overwrite"):
        create_backup(directory, output)
    assert output.read_bytes() == b"SENTINEL-NOT-AN-ARCHIVE"
    assert not list(output.parent.glob(".daily.zip.staging-*"))


def test_publish_fallback_without_hardlinks_never_overwrites(tmp_path, monkeypatch):
    """On a filesystem without hardlink support the fallback must CLAIM the
    destination exclusively (O_EXCL): a concurrent writer's file is never
    replaced, and a lost race refuses cleanly."""
    directory = tmp_path / "資料 src"
    make_seed(directory)
    output = tmp_path / "backups" / "daily.zip"
    output.parent.mkdir(parents=True)

    def no_hardlinks(src, dst):
        raise OSError("simulated filesystem without link support")

    monkeypatch.setattr(backup.os, "link", no_hardlinks)
    manifest = create_backup(directory, output)
    assert manifest["counts"]["runs"] >= 1
    with zipfile.ZipFile(output) as bundle:
        assert backup._DB_ENTRY in bundle.namelist()

    # Race: destination appeared before publish -> refuse, content intact.
    raced = tmp_path / "backups" / "race.zip"
    raced.write_bytes(b"WINNER")

    def lost_race(src, dst):
        raise FileExistsError(f"{dst} appeared")

    monkeypatch.setattr(backup.os, "link", lost_race)
    with pytest.raises(ApplicationError, match="Refusing to overwrite"):
        create_backup(directory, raced)
    assert raced.read_bytes() == b"WINNER"
    assert not list(output.parent.glob(".race.zip.staging-*"))
