"""Immutable managed-universe snapshots and one mutable last-known-good pointer."""

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, UniqueConstraint

metadata = MetaData()
universe_snapshots = Table(
    "universe_snapshots",
    metadata,
    Column("id", String, primary_key=True),
    Column("universe_key", String(32), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("source_revision", String, nullable=False),
    Column("retrieved_at", String(40), nullable=False),
    Column("document", JSON, nullable=False),
)
universe_snapshot_members = Table(
    "universe_snapshot_members",
    metadata,
    Column("snapshot_id", String, primary_key=True),
    Column("instrument_id", String, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("document", JSON, nullable=False),
    UniqueConstraint("snapshot_id", "position"),
)
managed_universes = Table(
    "managed_universes",
    metadata,
    Column("universe_key", String(32), primary_key=True),
    Column("snapshot_id", String, nullable=False),
    Column("watchlist_id", String, nullable=False),
)
