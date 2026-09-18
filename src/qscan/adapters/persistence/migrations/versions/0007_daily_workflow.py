"""Persist managed daily coordination, reports, and notification outcomes."""

from alembic import op
from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, UniqueConstraint, inspect

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    names = {
        "automation_settings",
        "daily_jobs",
        "report_publications",
        "latest_reports",
        "notification_deliveries",
    }
    present = names.intersection(inspect(op.get_bind()).get_table_names())
    if present:
        if present == names:
            return
        raise RuntimeError("Refusing partial daily-workflow schema")
    op.create_table(
        "automation_settings",
        Column("owner_id", String, primary_key=True),
        Column("enabled", Boolean, nullable=False),
        Column("updated_at", String(40), nullable=False),
    )
    op.create_table(
        "daily_jobs",
        Column("id", String, primary_key=True),
        Column("owner_id", String, nullable=False),
        Column("session", String(10), nullable=False),
        Column("provider", String, nullable=False),
        Column("config_hash", String(64), nullable=False),
        Column("attempt", Integer, nullable=False),
        Column("universe_snapshot_id", String, ForeignKey("universe_snapshots.id"), nullable=False),
        Column("run_id", String, ForeignKey("scan_runs.id"), nullable=False, unique=True),
        Column("accepted_at", String(40), nullable=False),
        UniqueConstraint("owner_id", "session", "provider", "config_hash", "attempt"),
    )
    op.create_table(
        "report_publications",
        Column("run_id", String, ForeignKey("scan_runs.id"), primary_key=True),
        Column("owner_id", String, nullable=False),
        Column("state", String(16), nullable=False),
        Column("attempts", Integer, nullable=False),
        Column("relative_path", String),
        Column("error", String),
        Column("updated_at", String(40), nullable=False),
    )
    op.create_table(
        "latest_reports",
        Column("owner_id", String, primary_key=True),
        Column("run_id", String, ForeignKey("scan_runs.id"), nullable=False),
        Column("relative_path", String, nullable=False),
        Column("published_at", String(40), nullable=False),
    )
    op.create_table(
        "notification_deliveries",
        Column("dedup_key", String, primary_key=True),
        Column("owner_id", String, nullable=False),
        Column("run_id", String, ForeignKey("scan_runs.id"), nullable=False),
        Column("attempts", Integer, nullable=False),
        Column("outcome", String(16)),
        Column("detail", String),
        Column("updated_at", String(40), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Daily-workflow downgrade is unsupported; restore a consistent backup")
