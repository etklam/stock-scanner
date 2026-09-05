"""Frozen revision 0001 schema; future migrations must not modify this definition."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()
instruments = Table(
    "instruments",
    metadata,
    Column("id", String, primary_key=True),
    Column("provider_symbol", String, nullable=False),
    Column("market", String, nullable=False),
    Column("document", JSON, nullable=False),
    Column("cache_info", JSON),
    UniqueConstraint("provider_symbol", "market"),
)
prices = Table(
    "prices",
    metadata,
    Column("instrument_id", ForeignKey("instruments.id"), primary_key=True),
    Column("session", String, primary_key=True),
    Column("price_basis", String, primary_key=True),
    Column("close", Float, nullable=False),
    Column("provider", String, nullable=False),
    Column("fetched_at", String, nullable=False),
)
Index("ix_prices_instrument_session", prices.c.instrument_id, prices.c.session)
watchlists = Table(
    "watchlists",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner_id", String, nullable=False),
    Column("name", String, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("owner_id", "name"),
)
members = Table(
    "watchlist_members",
    metadata,
    Column("watchlist_id", ForeignKey("watchlists.id"), primary_key=True),
    Column("instrument_id", ForeignKey("instruments.id"), primary_key=True),
    Column("position", Integer, nullable=False),
    Column("document", JSON, nullable=False),
    UniqueConstraint("watchlist_id", "position"),
)
runs = Table(
    "scan_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner_id", String, nullable=False),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("source_run_id", ForeignKey("scan_runs.id")),
    Column("input_hash", String),
    Column("document", JSON, nullable=False),
)
Index("ix_runs_owner_created", runs.c.owner_id, runs.c.created_at)
Index("ix_runs_state_created", runs.c.state, runs.c.created_at)
results = Table(
    "scan_results",
    metadata,
    Column("run_id", ForeignKey("scan_runs.id"), primary_key=True),
    Column("instrument_id", ForeignKey("instruments.id"), primary_key=True),
    Column("is_candidate", Boolean, nullable=False),
    Column("score", Integer),
    Column("document", JSON, nullable=False),
)
Index(
    "ix_results_rank",
    results.c.run_id,
    results.c.is_candidate,
    results.c.score,
    results.c.instrument_id,
)
