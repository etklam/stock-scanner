"""Human-review labels; mutable data fully separated from immutable scan results.

One row per (owner, run, instrument). Reviews never touch scan_runs or
scan_results: scores, ranks, hashes and snapshots stay byte-identical, and an
exact replay of a reviewed run starts with zero labels because the new run has
a different id. Revision enables optimistic concurrency so two editors cannot
silently overwrite each other.
"""

from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table

metadata = MetaData()
reviews = Table(
    "scan_reviews",
    metadata,
    Column("owner_id", String, nullable=False, primary_key=True),
    Column("run_id", ForeignKey("scan_runs.id"), nullable=False, primary_key=True),
    Column("instrument_id", ForeignKey("instruments.id"), nullable=False, primary_key=True),
    Column("label", String(32), nullable=False),
    Column("note", String(500), nullable=False, default=""),
    Column("revision", Integer, nullable=False),
    Column("updated_at", String(40), nullable=False),
)
