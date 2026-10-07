"""Alembic 运行环境（异步版）。

与默认模板的三处区别，都是必须改的：
1. **连库地址从 app.core.config 读**，不在 alembic.ini 里写死 —— 避免密钥进版本库，
   也保证"应用连哪个库，迁移就迁哪个库"。
2. **用 async_engine_from_config**：项目用的是 asyncpg 异步驱动，默认模板的同步
   engine_from_config 拿它连不上。
3. **导入 app.data.models**：Alembic 靠 Base.metadata 看有哪些表，模型文件不被
   import 的话 metadata 是空的，autogenerate 会生成"删掉所有表"的错误脚本。
"""
import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 把 backend/ 加进模块搜索路径，这样从任何目录执行 alembic 都能 import app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.data.db import Base  # noqa: E402
import app.data.models  # noqa: E402,F401  仅为注册模型，不能删

config = context.config

# 把 .env 里的 DATABASE_URL 注入 alembic 配置。
# replace("%", "%%")：configparser 把 % 当插值符号，密码里带 % 会解析报错。
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：不连库，只把 SQL 打印出来（alembic upgrade head --sql）。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
