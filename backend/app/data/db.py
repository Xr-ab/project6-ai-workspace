"""数据库连接与会话（Data Access 层基础设施）。

分层约定（见 docs/02-architecture.md §4）：
    本文件只负责"连上库"和"给每个请求一个会话"，不放任何业务逻辑；
    具体读写放 app/data/repositories/，业务判断放 Service 层。
"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    Alembic 靠 Base.metadata 拿到全部表定义，再和数据库现状比对生成迁移脚本；
    所以新增的模型必须 import 进 migrations/env.py，否则迁移会"看不见"它。
    """


# 引擎：进程内全局唯一，内部自带连接池。不要每个请求 create_async_engine，
# 那等于每次都新建一个连接池，连接会越开越多直到数据库拒绝。
# pool_pre_ping：连接闲置被数据库单方面断开后，取用前先探活，避免拿到死连接报错。
engine = create_async_engine(settings.database_url, pool_pre_ping=True)

# 会话工厂：调用一次 = 一个数据库会话（一次工作单元）。
# expire_on_commit=False：commit 之后对象属性仍可读。默认 True 会在 commit 后把对象
# 标记为过期，FastAPI 序列化返回值时又得回库查一遍（异步下还会直接报错）。
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个独立会话，请求结束自动归还连接。

    机制：
    1. **会话从哪来**：调用工厂 AsyncSessionLocal()，得到一个 AsyncSession 对象。
    2. **为什么用 `async with`**：它保证无论请求成功还是抛异常，退出时都会
       await session.close() 把连接还给池子。漏了这层，异常路径下连接不归还，
       并发一上来连接池就被占满（表现为请求卡死而不是报错，很难查）。
    3. **`yield` 在这里的作用**：把 session "借"给路由函数用（FastAPI 的依赖注入
       机制），路由函数跑完之后，代码会回到 yield 之后继续执行 —— 也就是去关会话。

    commit / rollback 不在这里做。这里只负责"给会话、收会话"，
    事务边界交给 repository（只有它知道这次操作到底改了什么）。
    """
    async with AsyncSessionLocal() as session:
        yield session
