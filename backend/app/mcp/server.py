"""企业库存域 MCP server（Phase 10 spec §3.3）。streamable-http 常驻进程，独立启动：

    cd backend && PYTHONPATH=. .venv\\Scripts\\python.exe -m app.mcp.server

零 AI 层 import（`app/ai` 那个目录在本进程里一字不提）：本进程不认识 Tool / ToolContext /
registry。它的输出由应用侧的 external_tools.py 翻译成 ToolResult（spec §3.4）——
反向依赖一旦成立，「独立进程」就是假的（spec §7 针⑨ 的 grep 面钉住这条）。
措辞为什么绕开那个目录的全名：套件的判据是对本文件**整串源码**扫全名零命中，
写下它（哪怕只写在注释里）就当场红——判据宁可严，注释跟着判据走。

工具一律返回 **json.dumps 后的字符串**：实测 mcp 1.30.0 下返回 dict 时
CallToolResult.structuredContent 为 None，返回 str 时 content[0].text 就是那串 JSON。
形状跟着实测走，不跟着「协议应该有结构化结果」的想当然走。
"""
import json
import sys
from urllib.parse import urlparse

# 与 app/main.py:13-18、app/workers/__main__.py:12-27 同一段制式守卫：win32 下
# 仅当策略还是 Proactor 时才切 Selector（asyncpg 两种 loop 都能跑，但三个进程制式一致
# 才不会出现「同一段代码在开发机与 CI 表现不同」）。必须在任何 loop 建立前执行。
if sys.platform == "win32":
    import asyncio

    if isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsProactorEventLoopPolicy):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.logging_setup import setup_logging
from app.mcp import inventory_data

_TARGET = urlparse(settings.mcp_enterprise_url)   # host:port:path 一把来源，代码里不写死（spec §3.3）

mcp = FastMCP(
    "enterprise-data",
    instructions=(
        "企业库存与补货数据域（只读）。这里只有水位与出入库流水，"
        "没有产品名、品类、客户与成交数据——那些在另一个系统里。"
        "所有查询都按 organization_id 过滤，调用方必须带上它。"
    ),
    host=_TARGET.hostname or "127.0.0.1",
    port=_TARGET.port or 8100,
    streamable_http_path=_TARGET.path or "/mcp",
)


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool(description=(
    "查库存水位：现货量 vs 安全库存，返回 product_id / region_name / on_hand_qty / "
    "safety_stock_qty / gap。**不返回产品名**（本服务没有 products 表），"
    "要名字请回调用方查产品目录。可选 region=省名精确匹配，"
    "可选 below_safety_only=true 只看已低于安全库存的行。"))
async def query_stock_levels(organization_id: str, region: str | None = None,
                             below_safety_only: bool = False) -> str:
    return _json(await inventory_data.stock_levels(organization_id, region, below_safety_only))


@mcp.tool(description=(
    "查出入库流水：返回明细行与窗口内 in_total / out_total / daily_out_avg（日均出库）。"
    "只给 product_id，不给产品名。可选 product_id 只看某个产品，days=1..365 窗口天数（默认 90）。"))
async def query_stock_movements(organization_id: str, product_id: str | None = None,
                                days: int = 90) -> str:
    return _json(await inventory_data.stock_movements(organization_id, product_id, days))


@mcp.tool(description=(
    "要补货清单：按「安全库存 + 日均出库 × 覆盖天数 - 现货」算出建议量，"
    "只返回 suggested_qty > 0 的行（按建议量从大到小排）。days_of_cover=7..180，默认 30。"
    "返回 product_id / region_name / on_hand_qty / safety_stock_qty / daily_out_avg / suggested_qty，"
    "**没有产品名与品类**——落地成采购单需要产品名时，调用方得自己回查目录。"))
async def suggest_replenishment(organization_id: str, days_of_cover: int = 30) -> str:
    return _json(await inventory_data.replenishment_suggestions(organization_id, days_of_cover))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request) -> JSONResponse:
    """与 app/main.py:99 同形状的探针。库存域连不上时 status 降级为 degraded
    （200 不变：探针的用途是「这个进程活着吗 + 它依赖的东西通不通」两件事分开答）。"""
    try:
        await inventory_data.ping()
        pg = "ok"
    except Exception as exc:
        pg = f"error: {type(exc).__name__}"
    return JSONResponse({
        "status": "ok" if pg == "ok" else "degraded",
        "env": settings.app_env,
        "postgres": pg,
        "service": "mcp-enterprise-data",
        "database": settings.enterprise_db_name,
    })


def main() -> None:
    setup_logging(settings.log_level)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
