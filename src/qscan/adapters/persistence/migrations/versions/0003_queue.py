"""Durable queue columns and owner-scoped idempotency uniqueness."""

from alembic import op
from sqlalchemy import Column, String

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scan_runs", Column("idempotency_key", String))
    op.add_column("scan_runs", Column("request_hash", String))
    # SQLite UNIQUE indexes allow repeated NULLs, so legacy/CLI rows without keys coexist.
    op.create_index(
        "uq_runs_owner_idempotency", "scan_runs", ["owner_id", "idempotency_key"], unique=True
    )


def downgrade() -> None:
    raise RuntimeError("Queue downgrade is intentionally unsupported; restore a consistent backup")
