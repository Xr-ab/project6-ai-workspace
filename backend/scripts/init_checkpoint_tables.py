"""把 checkpoint 表的首建收进 one-shot（spec §6 R-L）。

ensure_setup() 的幂等锁只在**进程内**（checkpointer.py:41/:109）⇒ app 与 worker 两个
进程在空卷首启时并发首建，会撞在 DDL 竞态窗口里。这里在两者起来之前单进程建好，
用的是既有 API（ensure_setup / shutdown_checkpointer），不新造机制。

按 `checkpoint%` 前缀全列而不写死条数：本机 information_schema 实测是 **4 张**
（checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations），
docs 旧称「三表」指前三张数据表——门只断那三张在，多出来的那张不参与判定。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32":
    # 与 checkpointer._require_supported_loop 同源：asyncpg 在 win32 要 Selector loop。
    # 容器（linux）不走这一支。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import asyncpg  # noqa: E402

from app.ai.graph.checkpointer import ensure_setup, shutdown_checkpointer  # noqa: E402
from app.core.config import settings  # noqa: E402


async def _names() -> list[str]:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename LIKE 'checkpoint%' "
            "ORDER BY tablename"
        )
        return [r["tablename"] for r in rows]
    finally:
        await conn.close()


async def _main() -> None:
    await ensure_setup()
    await shutdown_checkpointer()
    names = await _names()
    print("init_checkpoint_tables: " + ", ".join(names))
    missing = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"} - set(names)
    if missing:
        raise SystemExit(f"init_checkpoint_tables: 缺表 {sorted(missing)}")


if __name__ == "__main__":
    asyncio.run(_main())
