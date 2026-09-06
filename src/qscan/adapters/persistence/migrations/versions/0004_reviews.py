"""Per-run human review labels (mutable, owner-scoped, replay-independent)."""

from alembic import op
from sqlalchemy import Column, ForeignKey, Integer, String

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_reviews",
        Column("owner_id", String, nullable=False, primary_key=True),
        Column("run_id", String, ForeignKey("scan_runs.id"), nullable=False, primary_key=True),
        Column(
            "instrument_id",
            String,
            ForeignKey("instruments.id"),
            nullable=False,
            primary_key=True,
        ),
        Column("label", String(32), nullable=False),
        Column("note", String(500), nullable=False),
        Column("revision", Integer, nullable=False),
        Column("updated_at", String(40), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Review downgrade is intentionally unsupported; restore a consistent backup")
