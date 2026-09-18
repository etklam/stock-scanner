"""Run-scoped metadata and input checkpoints for resumable managed scans."""

from alembic import op
from sqlalchemy import JSON, Column, ForeignKey, Integer, String, UniqueConstraint, inspect

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    names = {"scan_executions", "scan_input_checkpoints"}
    present = names.intersection(inspect(op.get_bind()).get_table_names())
    if present:
        if present == names:
            return
        raise RuntimeError("Refusing partial resumable-batch schema")
    op.create_table(
        "scan_executions",
        Column("run_id", String, ForeignKey("scan_runs.id"), primary_key=True),
        Column("next_index", Integer, nullable=False),
        Column("document", JSON, nullable=False),
    )
    op.create_table(
        "scan_input_checkpoints",
        Column("run_id", String, ForeignKey("scan_runs.id"), primary_key=True),
        Column("position", Integer, primary_key=True),
        Column("instrument_id", String, ForeignKey("instruments.id"), nullable=False),
        Column("document", JSON, nullable=False),
        UniqueConstraint("run_id", "instrument_id"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Resumable-batch downgrade is intentionally unsupported; restore a consistent backup"
    )
