"""Preserve legacy cache while introducing source-isolated storage."""

from alembic import op
from sqlalchemy import insert, select

from qscan.adapters.persistence.migrations.schema_v1 import instruments
from qscan.adapters.persistence.migrations.schema_v1 import prices as old_prices
from qscan.adapters.persistence.migrations.schema_v2 import cache, metadata, prices

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    metadata.create_all(connection)
    for row in connection.execute(select(instruments)).mappings():
        info = row["cache_info"]
        if info is not None:
            connection.execute(
                insert(cache).values(
                    instrument_id=row["id"], provider=info["provenance"]["provider"], document=info
                )
            )
    for row in connection.execute(select(old_prices)).mappings():
        connection.execute(insert(prices).values(**dict(row)))


def downgrade() -> None:
    raise RuntimeError("Cache downgrade is intentionally unsupported; restore a consistent backup")
