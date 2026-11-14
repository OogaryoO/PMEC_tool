"""Alembic 執行環境：只支援 online 模式，連線字串由 src.db.upgrade_schema 設定。"""
from alembic import context
from sqlalchemy import create_engine, pool

url = context.config.get_main_option("sqlalchemy.url")
connectable = create_engine(url, poolclass=pool.NullPool)

with connectable.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
