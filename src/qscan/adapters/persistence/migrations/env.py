"""Migration entry point using a bootstrap-owned connection."""

from alembic import context

from qscan.adapters.persistence.migrations.schema_v1 import metadata

context.configure(connection=context.config.attributes["connection"], target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
