"""init-data 的门：业务表到底空不空（spec 计划层 R-T2b）。

为什么要有这道门：seed_business_data.py 的「幂等」是先清空再插，而行主键由 DB 默认值
重新生成 ⇒ 重跑一次，1481 行业务行的 product_id/order_id 全换（计数不变、身份证变）。
验收项目（空卷）需要它跑；本机形态（家里攒了几个月的演示数据）不需要、也不该被轮转。

退出码契约（init-data 的 shell 直接靠它分支）：0 = 空库，该播种；1 = 已有行，跳过。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.core.config import settings  # noqa: E402


async def _count() -> int:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval("SELECT count(*) FROM products")
    finally:
        await conn.close()


if __name__ == "__main__":
    n = asyncio.run(_count())
    print(f"needs_business_seed: products={n} → {'播种' if n == 0 else '跳过'}")
    raise SystemExit(0 if n == 0 else 1)
