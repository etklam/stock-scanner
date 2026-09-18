"""Managed daily jobs, report publication, and notification delivery state."""

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()

automation_settings = Table(
    "automation_settings",
    metadata,
    Column("owner_id", String, primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

daily_jobs = Table(
    "daily_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner_id", String, nullable=False),
    Column("session", String(10), nullable=False),
    Column("provider", String, nullable=False),
    Column("config_hash", String(64), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("universe_snapshot_id", ForeignKey("universe_snapshots.id"), nullable=False),
    Column("run_id", ForeignKey("scan_runs.id"), nullable=False, unique=True),
    Column("accepted_at", String(40), nullable=False),
    UniqueConstraint("owner_id", "session", "provider", "config_hash", "attempt"),
)

report_publications = Table(
    "report_publications",
    metadata,
    Column("run_id", ForeignKey("scan_runs.id"), primary_key=True),
    Column("owner_id", String, nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("relative_path", String),
    Column("error", String),
    Column("updated_at", String(40), nullable=False),
)

latest_reports = Table(
    "latest_reports",
    metadata,
    Column("owner_id", String, primary_key=True),
    Column("run_id", ForeignKey("scan_runs.id"), nullable=False),
    Column("relative_path", String, nullable=False),
    Column("published_at", String(40), nullable=False),
)

notification_deliveries = Table(
    "notification_deliveries",
    metadata,
    Column("dedup_key", String, primary_key=True),
    Column("owner_id", String, nullable=False),
    Column("run_id", ForeignKey("scan_runs.id"), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("outcome", String(16)),
    Column("detail", String),
    Column("updated_at", String(40), nullable=False),
)
