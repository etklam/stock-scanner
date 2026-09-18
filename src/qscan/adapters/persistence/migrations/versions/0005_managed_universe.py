"""Durable immutable managed-universe snapshots and current LKG pointer."""

from alembic import op
from sqlalchemy import JSON, Column, ForeignKey, Integer, String, UniqueConstraint, inspect

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    names = {"universe_snapshots", "universe_snapshot_members", "managed_universes"}
    present = names.intersection(inspect(op.get_bind()).get_table_names())
    if present:
        if present == names:
            return
        raise RuntimeError("Refusing partial managed-universe schema")
    op.create_table(
        "universe_snapshots",
        Column("id", String, primary_key=True),
        Column("universe_key", String(32), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("source_revision", String, nullable=False),
        Column("retrieved_at", String(40), nullable=False),
        Column("document", JSON, nullable=False),
    )
    op.create_table(
        "universe_snapshot_members",
        Column("snapshot_id", String, ForeignKey("universe_snapshots.id"), primary_key=True),
        Column("instrument_id", String, ForeignKey("instruments.id"), primary_key=True),
        Column("position", Integer, nullable=False),
        Column("document", JSON, nullable=False),
        UniqueConstraint("snapshot_id", "position"),
    )
    op.create_table(
        "managed_universes",
        Column("universe_key", String(32), primary_key=True),
        Column("snapshot_id", String, ForeignKey("universe_snapshots.id"), nullable=False),
        Column("watchlist_id", String, ForeignKey("watchlists.id"), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Universe downgrade is intentionally unsupported; restore a consistent backup"
    )
