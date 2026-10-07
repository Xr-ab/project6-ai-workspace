"""enterprise_data 的只读数据访问（Phase 10 spec §3.3）。

分层：本文件只有 SQL 与算术，不认识 MCP、不认识 Tool/ToolContext、不认识应用注册表。
⇒ 三把工具的行为可以在不起 server 的进程里直接测（Task 3 的模块面针），
  协议面只剩「把返回值 json.dumps 一遍」这一件事。

三条防线（照抄 business_tools._rows 的同一条原则：不靠字符串检查）：
    ① 每条查询 SET TRANSACTION READ ONLY
    ② SET LOCAL statement_timeout（server 侧的界；应用侧另有一层 wait_for，spec §6「两侧都有界」）
    ③ organization_id 无条件进 WHERE，来自参数而不是模型输入

补货算术口径（与 scripts/create_enterprise_db.suggested()、scratch/e2e_p10_exit.crosscheck() 三处同一行式子）：
    daily = out_qty / CONSUMPTION_WINDOW        # 窗口固定 90 天，与 days_of_cover 无关
    suggested = max(0, ceil(safety + daily * days_of_cover - on_hand))
    只返回 suggested > 0 的行（spec R129②）。
"""
import math
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import settings
from app.core.enterprise_dsn import enterprise_database_url

STATEMENT_TIMEOUT_MS = 10_000
CONSUMPTION_WINDOW = 90        # 日均出库的统计窗口（**与建库种子脚本的同名常量同源**）
MOVEMENT_DETAIL_LIMIT = 50     # 流水明细的返回上限（聚合值不受它限制）

_engine: AsyncEngine | None = None


def _ent_engine() -> AsyncEngine:
    """惰性建 engine：import 本模块不连库。

    为什么要惰性：进程启动顺序不该被库存域绑死——库存域连不上时要炸在**第一次调用**
    （报「连的是哪个库」），而不是炸在 import 语句上（那会让人以为是代码坏了）。
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            enterprise_database_url(settings.database_url, settings.enterprise_db_name),
            pool_pre_ping=True,
        )
    return _engine


async def _rows(sql: str, params: dict) -> list[dict]:
    async with _ent_engine().connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        result = await conn.execute(text(sql), params)
        return [dict(r) for r in result.mappings()]


async def ping() -> bool:
    await _rows("SELECT 1 AS ok", {})
    return True


async def stock_levels(org_id: str, region: str | None = None, below_safety_only: bool = False) -> dict:
    """当前水位。gap = 安全库存 - 现货（正数=缺口，负数=超出）。

    region 是**省名精确匹配**（副本列 region_name，见 R129①）：应用侧的 _resolve_region
    才有层级解析，两侧各写一遍是重复（spec §3.3「故意不给」第二条），行为如实登记在 spec §6。
    """
    sql = (
        "SELECT product_id::text AS product_id, region_name, on_hand_qty, safety_stock_qty, "
        "       (safety_stock_qty - on_hand_qty) AS gap "
        "FROM inventory_levels WHERE organization_id = :org"
    )
    params: dict = {"org": org_id}
    if region:
        sql += " AND region_name = :region"
        params["region"] = region
    if below_safety_only:
        sql += " AND on_hand_qty < safety_stock_qty"
    sql += " ORDER BY gap DESC, region_name, product_id"
    rows = await _rows(sql, params)
    return {"rows": rows, "count": len(rows)}


async def stock_movements(org_id: str, product_id: str | None = None, days: int = 90) -> dict:
    """出入库流水 + 窗口合计 + 日均出库（补货公式要的那个数）。"""
    days = max(1, min(int(days), 365))          # server 是独立进程，不信任调用方（应用侧另有一层 Pydantic）
    since = date.today() - timedelta(days=days)
    where = "WHERE organization_id = :org AND movement_date >= :since"
    params: dict = {"org": org_id, "since": since}
    if product_id:
        where += " AND product_id = :pid"
        params["pid"] = product_id

    agg = await _rows(
        f"SELECT direction, COALESCE(SUM(qty), 0) AS total FROM stock_movements {where} GROUP BY direction",
        params,
    )
    totals = {r["direction"]: int(r["total"]) for r in agg}
    in_total, out_total = totals.get("in", 0), totals.get("out", 0)

    detail_params = dict(params, limit=MOVEMENT_DETAIL_LIMIT)
    detail = await _rows(
        "SELECT product_id::text AS product_id, direction, qty, movement_date::text AS date "
        f"FROM stock_movements {where} ORDER BY movement_date DESC, product_id LIMIT :limit",
        detail_params,
    )
    return {
        "rows": detail,
        "in_total": in_total,
        "out_total": out_total,
        "daily_out_avg": round(out_total / days, 4),
        "window_days": days,
        "truncated": len(detail) >= MOVEMENT_DETAIL_LIMIT,
    }


async def replenishment_suggestions(org_id: str, days_of_cover: int = 30) -> dict:
    """按「安全库存 + 日均出库 × 覆盖天数 - 现货」给建议补货量，只回 suggested > 0 的行。

    日均按 (product_id, region_id) 粒度算——水位本来就是按这个粒度存的，
    用产品全局日均会让「一个省缺货、另一个省积压」糊成一团。
    """
    cover = max(7, min(int(days_of_cover), 180))
    since = date.today() - timedelta(days=CONSUMPTION_WINDOW)
    levels = await _rows(
        "SELECT product_id::text AS product_id, region_id::text AS region_id, region_name, "
        "       on_hand_qty, safety_stock_qty "
        "FROM inventory_levels WHERE organization_id = :org",
        {"org": org_id},
    )
    consumption = await _rows(
        "SELECT product_id::text AS product_id, region_id::text AS region_id, "
        "       COALESCE(SUM(qty), 0) AS out_qty "
        "FROM stock_movements "
        "WHERE organization_id = :org AND direction = 'out' AND movement_date >= :since "
        "GROUP BY product_id, region_id",
        {"org": org_id, "since": since},
    )
    out_map = {(r["product_id"], r["region_id"]): int(r["out_qty"]) for r in consumption}

    rows = []
    for lv in levels:
        out_qty = out_map.get((lv["product_id"], lv["region_id"]), 0)
        daily = out_qty / CONSUMPTION_WINDOW
        suggested = max(0, math.ceil(
            lv["safety_stock_qty"] + daily * cover - lv["on_hand_qty"]))
        if suggested <= 0:
            continue
        rows.append({
            "product_id": lv["product_id"],
            "region_name": lv["region_name"],
            "on_hand_qty": lv["on_hand_qty"],
            "safety_stock_qty": lv["safety_stock_qty"],
            "daily_out_avg": round(daily, 4),
            "suggested_qty": suggested,
        })
    rows.sort(key=lambda r: (-r["suggested_qty"], r["region_name"], r["product_id"]))
    return {"rows": rows, "count": len(rows), "days_of_cover": cover,
            "consumption_window_days": CONSUMPTION_WINDOW}
