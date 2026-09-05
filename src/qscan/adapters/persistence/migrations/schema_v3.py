"""Queue/idempotency columns for scan_runs; revision 0001/0002 definitions stay frozen."""

from sqlalchemy import JSON, Column, ForeignKey, MetaData, String, Table

metadata = MetaData()
runs = Table(
    "scan_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner_id", String, nullable=False),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("source_run_id", ForeignKey("scan_runs.id")),
    Column("input_hash", String),
    Column("idempotency_key", String),
    Column("request_hash", String),
    Column("document", JSON, nullable=False),
)
