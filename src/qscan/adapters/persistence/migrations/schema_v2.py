"""Provider-isolated cache; revision 0001 tables remain intact for safe upgrades."""

from sqlalchemy import JSON, Column, Float, MetaData, String, Table

metadata = MetaData()
cache = Table(
    "cache_by_provider",
    metadata,
    Column("instrument_id", String, primary_key=True),
    Column("provider", String, primary_key=True),
    Column("document", JSON, nullable=False),
)
prices = Table(
    "prices_by_provider",
    metadata,
    Column("instrument_id", String, primary_key=True),
    Column("provider", String, primary_key=True),
    Column("session", String, primary_key=True),
    Column("price_basis", String, nullable=False),
    Column("close", Float, nullable=False),
    Column("fetched_at", String, nullable=False),
)
