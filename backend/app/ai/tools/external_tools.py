"""外部 MCP 工具的应用侧适配器（Phase 10 spec §3.4，方案 A 的落地位置）。

为什么包成普通 Tool 而不是特判一条 MCP 分支（方案 A 的全部理由）：
    registry.execute 是唯一关口，一次做完存在性→参数校验→权限闸→执行→统一计时。
    只要这三把也是 Tool，那五件事（含 tool_denied 审计、started_at/finished_at 真时刻、
    tool_calls 落库→Trace 节点）**一行都不用重写**（spec §1.2）。

两条本文件级的硬规矩：
    ① **不 import、不引用、也不借用 ctx 上那个 ai_workspace 的 AsyncSession**：它与库存域
       毫无关系，用它是跨源泄漏（而且连不上）。钉在 spec §7 针③（grep 面）。
       措辞为什么绕开「那个字段名」的写法：针③ 的判据是对本文件**整份源码**扫那个字面串
       零命中，写下它（哪怕只写在注释里、哪怕写的是「不要用」）就当场红——
       判据宁可严，注释跟着判据走（先例：app/mcp/server.py 头注的同款回避）。
    ② 执行器**永不抛异常**：失败一律 ToolResult(ok=False, error=...)。
       registry 的兜底 except 能接住抛错，但接不住的话文案是 "ExceptionGroup: unhandled
       errors in a TaskGroup"——模型看了不知道怎么改（实测：连接被拒就是这根串）。
"""
import asyncio
import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, ConfigDict, Field

from app.ai.tools.base import Tool, ToolContext, ToolResult
from app.ai.tools.registry import register
from app.core.config import settings


class StockLevelsArgs(BaseModel):
    # extra="forbid"：模型多编一个字段就炸在参数校验（spec §6「非法参数」那类失败的来源），
    # 而不是静默丢掉——丢掉的话模型会拿着一份「它以为加了过滤」的结果继续推理。
    model_config = ConfigDict(extra="forbid")

    region: str | None = Field(default=None, description="区域名（省名，如华东）；不填=全部区域")
    below_safety_only: bool = Field(default=False, description="只看已低于安全库存的行")


class StockMovementsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str | None = Field(default=None, description="产品 id（uuid 字符串）；不填=本组织全部产品")
    days: int = Field(default=90, ge=1, le=365, description="统计窗口天数")


class ReplenishmentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days_of_cover: int = Field(default=30, ge=7, le=180, description="要覆盖多少天的消耗")


def _error_text(exc: BaseException) -> str:
    """把 anyio TaskGroup 的 ExceptionGroup 摊平成一行可读文案（递归取叶子）。

    为什么不 import exceptiongroup：Python 3.10 没有内置 BaseExceptionGroup，
    而 duck-type `.exceptions` 就够——SDK 换成普通异常时这段自动退化成单层文案，不会跟着坏。
    """
    subs = getattr(exc, "exceptions", None)
    if subs:
        return "；".join(_error_text(e) for e in subs)
    return f"{type(exc).__name__}: {exc}"


def _payload(result: Any) -> dict:
    """从 CallToolResult 里取 JSON 载荷。

    实测（mcp 1.30.0）：server 侧工具返回 str 时，JSON 就在 content[0].text；
    structuredContent 形如 {"result": "<同一串>"}，多包一层且**返回 dict 时是 None**
    ⇒ 只认 text，不依赖 structuredContent。
    """
    for item in result.content:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    raise ValueError("MCP 返回体里没有可解析的 JSON 文本")


async def _one_call(url: str, tool_name: str, payload: dict) -> dict:
    """一次 MCP 调用 = 一个 session（initialize→call_tool→关闭），spec §2.4 的裁定。

    为什么不共享 session 换成连接池：SDK 的 client session 不为并发复用设计（单 session 内
    请求要自己串行化），共享一建换来的是「并发下协议帧串台」——比多一次握手难查得多。
    """
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, payload)
            if getattr(result, "isError", False):
                # server 侧工具函数自己抛了（连不上库 / SQL 报错），文案在 content[0].text
                texts = " / ".join(getattr(i, "text", "") or "" for i in result.content)
                raise RuntimeError(f"server 侧执行失败：{texts}")
            return _payload(result)


async def _call_external(tool_name: str, payload: dict) -> ToolResult:
    """三次远程调用共用的那一条路。**地址与超时在调用时读 settings**：
    写进默认参数的话，spec §7 针⑦ 那条「临时把 mcp_tool_timeout_seconds 改成 0」的超时针就永远打不着。

    签名里没有 `ctx` 位（Task 4 曾为「对账面」预留，Task 5 的对账按 schema 名集按不到这里，
    于是按「不留死参数」删掉）：payload 已经在各执行器里用 `ctx` 拼好，
    这里再收一个用不上的 `ctx` 只会让人以为它参与了鉴权或注入——它没有。
    """
    url = settings.mcp_enterprise_url
    timeout = settings.mcp_tool_timeout_seconds
    try:
        # Python 3.10 没有 asyncio.timeout，只有 wait_for（spec §6）。
        # timeout<=0 时 CPython 走「已经超时」分支：不 await、立即 cancel ⇒ 确定性，不靠赛跑。
        # （已读本机 3.10.11 `asyncio/tasks.py` 源码确认：`if timeout <= 0: fut = ensure_future(fut)`
        #   → `if fut.done(): return fut.result()` → `await _cancel_and_wait(fut)` → `raise exceptions.TimeoutError()`。
        #   ensure_future 只把协程包成 Task、还没执行一步，所以 fut.done() 必为 False，分支恒走 TimeoutError。）
        data = await asyncio.wait_for(_one_call(url, tool_name, payload), timeout)
    except asyncio.TimeoutError:
        return ToolResult(ok=False, rows=0,
                          error=f"MCP 调用超时（超过 {timeout}s 未返回）：{tool_name}")
    except Exception as exc:
        return ToolResult(ok=False, rows=0,
                          error=f"MCP 调用失败（{tool_name} @ {url}）：{_error_text(exc)}")
    rows = data.get("rows") or []
    result = ToolResult(ok=True, data=data, rows=len(rows))
    result.truncated = bool(data.get("truncated"))
    return result


async def query_stock_levels(args: StockLevelsArgs, ctx: ToolContext) -> ToolResult:
    payload = {
        "organization_id": str(ctx.organization_id),   # 运行时依赖由适配器注入，模型改不了（spec §6）
        "region": args.region,
        "below_safety_only": args.below_safety_only,
    }
    return await _call_external("query_stock_levels", payload)


async def query_stock_movements(args: StockMovementsArgs, ctx: ToolContext) -> ToolResult:
    payload = {
        "organization_id": str(ctx.organization_id),
        "product_id": args.product_id,
        "days": args.days,
    }
    return await _call_external("query_stock_movements", payload)


async def suggest_replenishment(args: ReplenishmentArgs, ctx: ToolContext) -> ToolResult:
    payload = {
        "organization_id": str(ctx.organization_id),
        "days_of_cover": args.days_of_cover,
    }
    return await _call_external("suggest_replenishment", payload)


register(Tool(
    name="query_stock_levels",
    description=(
        "查**库存水位**（现货量 vs 安全库存，来自外部库存系统）。何时用：断货、缺货、还能卖多少、"
        "哪些产品低于安全库存。何时不用：历史成交量/毛利是 query_sales 与 sql_query 的事——"
        "本工具只读外部库存系统，没有成交数据，也**不返回产品名**（要名字用 query_product）。"
    ),
    args_schema=StockLevelsArgs,
    executor=query_stock_levels,
    tool_type="external",
))

register(Tool(
    name="query_stock_movements",
    description=(
        "查**出入库流水**：明细行 + 窗口内 in_total/out_total/daily_out_avg（日均出库量）。"
        "何时用：出库速度、补库节奏、为什么这个产品在掉库存。按 product_id 查，不返回产品名。"
    ),
    args_schema=StockMovementsArgs,
    executor=query_stock_movements,
    tool_type="external",
))

register(Tool(
    name="suggest_replenishment",
    description=(
        "外部库存系统算好的**补货建议量**（安全库存 + 日均出库×覆盖天数 − 现货，只给建议量>0 的行）。"
        "何时用：补货清单、采购量建议。拿到的是 product_id，"
        "**要落到产品名与品类必须再调 query_product**——本工具所在的库存系统没有产品目录。"
    ),
    args_schema=ReplenishmentArgs,
    executor=suggest_replenishment,
    tool_type="external",
))
