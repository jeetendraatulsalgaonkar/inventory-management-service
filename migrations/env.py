import os

from alembic import context
from sqlalchemy import create_engine, pool


def migrate(connection):
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(dialect_name="postgresql", literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif (connection := context.config.attributes.get("connection")) is not None:
    migrate(connection)
else:
    engine = create_engine(os.environ["DATABASE_URL"], poolclass=pool.NullPool)
    with engine.connect() as connection:
        migrate(connection)
    engine.dispose()
