"""Resumable managed-run execution metadata and run-scoped input checkpoints."""

from sqlalchemy import JSON, Column, ForeignKey, Integer, MetaData, Table, UniqueConstraint

metadata = MetaData()
executions = Table(
    "scan_executions",
    metadata,
    Column("run_id", ForeignKey("scan_runs.id"), primary_key=True),
    Column("next_index", Integer, nullable=False),
    Column("document", JSON, nullable=False),
)
checkpoints = Table(
    "scan_input_checkpoints",
    metadata,
    Column("run_id", ForeignKey("scan_runs.id"), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("instrument_id", ForeignKey("instruments.id"), nullable=False),
    Column("document", JSON, nullable=False),
    UniqueConstraint("run_id", "instrument_id"),
)
