"""业务查询工具（Phase 3 / docs/04-tool-design.md §2.4）：query_customer / query_product / query_sales。

和 sql_query 的分工（docs 里"两者并存"那句话的实现版）：
    sql_query             面向分析师的**自由只读 SQL**：想怎么查怎么查，靠只读事务兜底
    本文件三个工具         面向业务语义的**受控查询**：参数是业务词（客户等级 / 区域名 / 时间区间），
                           SQL 由代码写死，只把参数绑进去

为什么两者并存、不干脆只留 sql_query：
    ① 口径只写一遍：organization_id 过滤、只要 completed 订单、
       "客户维度只能查 orders（sales 聚合层没有客户字段）" 这些**业务口径**
       在代码里固定下来。让模型每次自己写 SQL，同一句话问两次可能得到两个答案。
    ② 参数校验便宜：模型传 tier="vip" 会被 Pydantic 的 Literal 直接挡住并列出
       合法取值，比让它写出错 SQL 再解释报错省一轮。
    ③ 稳定性：业务问题（"华东这个月卖了多少"）用固定 SQL 每次都对；
       自由 SQL 有相当比例会踩类型 / 粒度 / 口径坑（见 问题收录.md 踩坑 1、2）。

两条贯穿本文件的规则：
    · group_by / sort_by 这类"列名由模型给"的参数，一律 **Literal 限定取值 +
      硬编码白名单映射**。绝不用 f-string 把模型输入拼进 SQL —— 那是注入。
    · 参数值全部走**绑定变量**（:org_id / :name / :limit），一样不做字符串拼接。
"""
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import text

from app.ai.tools.base import Tool, ToolContext, ToolResult
from app.ai.tools.data_tools import SQL_TIMEOUT_MS
from app.ai.tools.registry import register
from app.data.db import engine


async def _rows(ctx: ToolContext, sql: str, params: dict) -> list[dict]:
    """执行一段参数化只读 SQL，返回 dict 列表。

    org_id 在这里统一注入 —— 三个工具的每条 SQL 都会被强制带上组织过滤，
    这就是"受控查询"的含义（模型没有机会忘记写 organization_id = :org_id）。

    超时值在**调用时**取（不预先拼进常量串）：值是配置不是字面量，
    提前冻结的话改配置/测试里临时调小都不会生效。
    """
    async with engine.connect() as conn:
        # 和 sql_query 同一条原则：只读事务是**真防线**（不靠字符串检查），
        # 所以业务工具也走独立连接 + READ ONLY + 语句超时。
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(text(f"SET LOCAL statement_timeout = {SQL_TIMEOUT_MS}"))
        result = await conn.execute(
            text(sql), {"org_id": str(ctx.organization_id), **params}
        )
        return [dict(row) for row in result.mappings()]


async def _resolve_region(
    ctx: ToolContext, name: str
) -> tuple[uuid.UUID | None, str | None]:
    """区域名 → **省级**区域 id。返回 (id, 错误信息)。

    为什么要做这层解析、而不是直接按名字过滤：
        业务表（orders / sales / customers）只挂省级区域，市级区域（"上海"）
        下面没有直接挂业务数据。用户说"上海"时，直接按名字匹配会查到 0 行，
        然后回答"没有上海的数据" —— 数据没错、SQL 也没错，但结论是错的。
        这里把市名自动归到它的省份，正是"受控查询"相对自由 SQL 的价值。

    匹配顺序：先精确、再包含（取名字最短的，避免"华"匹配到华东/华北/华南）。
    """
    rows = await _rows(
        ctx,
        """
        SELECT id, name, level, parent_id
        FROM regions
        WHERE organization_id = :org_id AND name ILIKE :pattern
        ORDER BY (name = :exact) DESC, length(name)
        LIMIT 1
        """,
        {"pattern": f"%{name}%", "exact": name},
    )
    if not rows:
        return None, (
            f"没有找到区域'{name}'。可用区域（省）："
            "华东 / 华北 / 华南 / 华中 / 西南 / 西北，也可以直接给省下的市名。"
        )
    row = rows[0]
    if row["level"] == "city" and row["parent_id"] is not None:
        return row["parent_id"], None
    return row["id"], None


# ---------------- query_customer ----------------


class QueryCustomerArgs(BaseModel):
    name: str | None = Field(
        default=None, description="客户名关键词（模糊匹配，如'恒远'）；不知道全名时用它"
    )
    tier: Literal["key", "standard", "small"] | None = Field(
        default=None, description="客户等级：key=重点客户 / standard=标准 / small=小微"
    )
    risk_level: Literal["low", "medium", "high"] | None = Field(
        default=None, description="风险等级：low / medium / high"
    )
    region: str | None = Field(
        default=None,
        description="区域名（省名如'华东'，或市名如'上海'，会自动归到所属省份）",
    )
    limit: int = Field(default=20, ge=1, le=100, description="最多返回多少条，默认 20")


async def query_customer(args: QueryCustomerArgs, ctx: ToolContext) -> ToolResult:
    """按业务维度查客户档案 + 贡献度（销售额来自 orders 的 completed 订单）。"""
    params: dict = {"limit": args.limit + 1}  # 多取一条只为判断是否被截断
    where = ["c.organization_id = :org_id"]
    if args.name is not None:
        where.append("c.name ILIKE :name")
        params["name"] = f"%{args.name}%"
    if args.tier is not None:
        where.append("c.tier = :tier")
        params["tier"] = args.tier
    if args.risk_level is not None:
        where.append("c.risk_level = :risk")
        params["risk"] = args.risk_level
    if args.region is not None:
        region_id, err = await _resolve_region(ctx, args.region)
        if err is not None:
            return ToolResult(ok=False, error=err)
        where.append("c.region_id = :region_id")
        params["region_id"] = str(region_id)

    sql = f"""
        SELECT c.code, c.name, c.industry,
               COALESCE(r.name, '未分配') AS region,
               c.tier, c.risk_level,
               COALESCE(o.order_count, 0) AS order_count,
               COALESCE(o.amount, 0) AS amount
        FROM customers c
        LEFT JOIN regions r ON r.id = c.region_id
        LEFT JOIN (
            SELECT customer_id,
                   COUNT(*) AS order_count,
                   ROUND(SUM(amount), 2)::float8 AS amount
            FROM orders
            WHERE organization_id = :org_id AND status = 'completed'
            GROUP BY customer_id
        ) o ON o.customer_id = c.id
        WHERE {" AND ".join(where)}
        ORDER BY COALESCE(o.amount, 0) DESC
        LIMIT :limit
    """
    rows = await _rows(ctx, sql, params)
    truncated = len(rows) > args.limit
    rows = rows[: args.limit]
    return ToolResult(
        ok=True,
        data={
            "customers": rows,
            "note": (
                "amount 是该客户**已完成订单**的累计成交额（sales 是区域+产品粒度，"
                "不带客户字段，所以客户贡献只能查 orders）"
            ),
        },
        rows=len(rows),
        truncated=truncated,
    )


# ---------------- query_product ----------------


class QueryProductArgs(BaseModel):
    name: str | None = Field(default=None, description="产品名关键词（模糊匹配）")
    category: str | None = Field(
        default=None,
        description="品类：软件授权 / 硬件设备 / 服务订阅 / 咨询服务 / 备件",
    )
    sort_by: Literal["amount", "profit", "quantity"] = Field(
        default="amount",
        description="排序依据：amount=销售额 / profit=毛利 / quantity=销量，都是降序",
    )
    limit: int = Field(default=20, ge=1, le=100, description="最多返回多少条，默认 20")


# 排序字段白名单：键来自 Literal，值是我们自己写死的 SQL 片段。
# 模型给什么字符串都只会命中字典里已有的几个键，不可能拼出任意 SQL。
_PRODUCT_SORTS = {
    "amount": "s.amount",
    "profit": "s.profit",
    "quantity": "s.quantity",
}


async def query_product(args: QueryProductArgs, ctx: ToolContext) -> ToolResult:
    """按业务维度查产品表现：销量 / 销售额 / 毛利 / 毛利率。"""
    params: dict = {"limit": args.limit + 1}
    where = ["p.organization_id = :org_id"]
    if args.name is not None:
        where.append("p.name ILIKE :name")
        params["name"] = f"%{args.name}%"
    if args.category is not None:
        where.append("p.category = :category")
        params["category"] = args.category

    order_col = _PRODUCT_SORTS[args.sort_by]
    # 内层子查询**保持 numeric 不转 float8**：Postgres 没有 ROUND(double precision, int)，
    # 一旦提前转成 float8，外层算毛利率时 ROUND(x, 2) 会直接报
    # "function round(double precision, integer) does not exist"。
    # 统一到最外层再 ::float8（给 JSON 用），中间过程全程 numeric。
    sql = f"""
        SELECT p.sku, p.name, p.category, p.status,
               p.unit_price::float8 AS unit_price,
               p.unit_cost::float8 AS unit_cost,
               COALESCE(s.quantity, 0) AS quantity,
               COALESCE(s.amount, 0)::float8 AS amount,
               COALESCE(s.profit, 0)::float8 AS profit,
               ROUND(COALESCE(s.profit, 0) / NULLIF(s.amount, 0) * 100, 2)::float8
                   AS margin_pct
        FROM products p
        LEFT JOIN (
            SELECT product_id,
                   SUM(quantity) AS quantity,
                   ROUND(SUM(amount), 2) AS amount,
                   ROUND(SUM(profit), 2) AS profit
            FROM sales
            WHERE organization_id = :org_id
            GROUP BY product_id
        ) s ON s.product_id = p.id
        WHERE {" AND ".join(where)}
        ORDER BY {order_col} DESC NULLS LAST
        LIMIT :limit
    """
    rows = await _rows(ctx, sql, params)
    truncated = len(rows) > args.limit
    rows = rows[: args.limit]
    return ToolResult(
        ok=True,
        data={
            "products": rows,
            "note": (
                "margin_pct 是毛利率（%）。unit_price / unit_cost 是**当前**挂牌价与成本，"
                "历史成交按订单时点固化在 orders.amount 里，两者不要混用"
            ),
        },
        rows=len(rows),
        truncated=truncated,
    )


# ---------------- query_sales ----------------


class QuerySalesArgs(BaseModel):
    group_by: Literal["region", "product", "category", "month"] = Field(
        default="region",
        description=(
            "汇总维度：region=按区域对比 / product=按产品 / "
            "category=按品类 / month=按月看趋势（月份按时间正序）"
        ),
    )
    region: str | None = Field(
        default=None, description="只看某个区域（省名或市名，市名自动归到省份）"
    )
    category: str | None = Field(default=None, description="只看某个产品品类")
    date_from: date | None = Field(default=None, description="起始日期（含），格式 YYYY-MM-DD")
    date_to: date | None = Field(default=None, description="结束日期（含），格式 YYYY-MM-DD")
    top_n: int = Field(default=10, ge=1, le=50, description="返回前多少组，默认 10")


# (分组表达式, 排序表达式)。两者都取自这个白名单，模型只能选键、给不了内容。
_SALES_GROUPS = {
    "region": ("COALESCE(r.name, '未分配区域')", "amount DESC"),
    "product": ("p.name", "amount DESC"),
    "category": ("COALESCE(p.category, '未分类')", "amount DESC"),
    # 月份按时间正序：趋势分析要看走向，按金额排序会把月份顺序打乱
    "month": ("to_char(s.sale_date, 'YYYY-MM')", "name ASC"),
}


async def query_sales(args: QuerySalesArgs, ctx: ToolContext) -> ToolResult:
    """按区域 / 产品 / 品类 / 月份汇总销售额、成本、毛利（含合计与占比）。"""
    params: dict = {"top_n": args.top_n}
    where = ["s.organization_id = :org_id"]
    if args.region is not None:
        region_id, err = await _resolve_region(ctx, args.region)
        if err is not None:
            return ToolResult(ok=False, error=err)
        where.append("s.region_id = :region_id")
        params["region_id"] = str(region_id)
    if args.category is not None:
        where.append("p.category = :category")
        params["category"] = args.category
    if args.date_from is not None:
        where.append("s.sale_date >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to is not None:
        where.append("s.sale_date <= :date_to")
        params["date_to"] = args.date_to
    cond = " AND ".join(where)

    # 两个查询共用同一套过滤条件：分组明细 + 合计。
    # 合计必须单独查、不能用返回的 top_n 组相加 —— 那样只是"前 N 名的合计"，
    # 会被当成"全量合计"用（数字看着正常，但结论偏了）。
    joins = """
        FROM sales s
        LEFT JOIN products p ON p.id = s.product_id
        LEFT JOIN regions  r ON r.id = s.region_id
    """
    totals = (
        await _rows(
            ctx,
            f"""
            SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(s.quantity), 0) AS quantity,
                   COALESCE(ROUND(SUM(s.amount), 2), 0)::float8 AS amount,
                   COALESCE(ROUND(SUM(s.cost), 2), 0)::float8 AS cost,
                   COALESCE(ROUND(SUM(s.profit), 2), 0)::float8 AS profit,
                   to_char(MIN(s.sale_date), 'YYYY-MM-DD') AS first_date,
                   to_char(MAX(s.sale_date), 'YYYY-MM-DD') AS last_date
            {joins} WHERE {cond}
            """,
            params,
        )
    )[0]

    label, order_by = _SALES_GROUPS[args.group_by]
    groups = await _rows(
        ctx,
        f"""
        SELECT {label} AS name,
               SUM(s.quantity) AS quantity,
               ROUND(SUM(s.amount), 2)::float8 AS amount,
               ROUND(SUM(s.cost), 2)::float8 AS cost,
               ROUND(SUM(s.profit), 2)::float8 AS profit,
               CASE WHEN SUM(s.amount) > 0
                    THEN ROUND(SUM(s.profit) / SUM(s.amount) * 100, 2)::float8
               END AS margin_pct
        {joins} WHERE {cond}
        GROUP BY {label}
        ORDER BY {order_by}
        LIMIT :top_n
        """,
        params,
    )

    # 占比在 Python 里算（SQL 里算要再来一次窗口函数，不值得）。
    # 分母用 totals["amount"]，所以占比之和可能小于 100% —— 差额就是没进 top_n 的组。
    total_amount = totals["amount"] or 0
    for row in groups:
        row["share_pct"] = (
            round(row["amount"] / total_amount * 100, 2) if total_amount else None
        )

    return ToolResult(
        ok=True,
        data={
            "group_by": args.group_by,
            "period": {
                "from": totals["first_date"],
                "to": totals["last_date"],
                "note": "未指定时间时覆盖库存里的全部日期；这是数据实际覆盖区间",
            },
            "total": {
                "amount": totals["amount"],
                "cost": totals["cost"],
                "profit": totals["profit"],
                "quantity": totals["quantity"],
                "margin_pct": (
                    round(totals["profit"] / total_amount * 100, 2)
                    if total_amount
                    else None
                ),
            },
            "groups": groups,
            "note": (
                "数据来自 sales（由**已完成**订单聚合，已排除 cancelled）；"
                "sales 是 区域+产品+日期 粒度，没有客户字段，客户维度请用 query_customer"
            ),
        },
        rows=len(groups),
    )


# ---------------- 注册 ----------------

register(
    Tool(
        name="query_customer",
        description=(
            "按业务维度查询客户档案与贡献度：等级 / 风险 / 行业 / 所属区域 + 累计成交额。\n"
            "何时用：客户相关的问题 —— 有哪些重点客户、哪些客户风险高、"
            "哪个客户贡献最大、某区域有哪些客户。\n"
            "何时不用：需要自定义条件或多表复杂关联时用 sql_query；"
            "成交额的时间范围本工具不细分（要按月拆请用 sql_query 查 orders）。"
        ),
        args_schema=QueryCustomerArgs,
        executor=query_customer,
        tool_type="business",
    )
)

register(
    Tool(
        name="query_product",
        description=(
            "按业务维度查询产品表现：销量 / 销售额 / 毛利 / 毛利率，可按销售额、"
            "毛利、销量排序。\n"
            "何时用：产品相关的问题 —— 哪些产品卖得好、哪个产品毛利异常低、"
            "某品类的产品表现。\n"
            "何时不用：想看某个产品在不同区域/月份的表现用 sql_query；"
            "看整体销售趋势用 query_sales。"
        ),
        args_schema=QueryProductArgs,
        executor=query_product,
        tool_type="business",
    )
)

register(
    Tool(
        name="query_sales",
        description=(
            "销售汇总：按区域 / 产品 / 品类 / 月份分组，给出销售额、成本、毛利、"
            "毛利率和占比，并附全量合计与实际数据区间。\n"
            "何时用：销售类问题的首选 —— 区域对比、月度趋势、各品类贡献。\n"
            "何时不用：客户维度（sales 没有客户字段，用 query_customer）；"
            "需要自定义分组或更复杂口径时用 sql_query。"
        ),
        args_schema=QuerySalesArgs,
        executor=query_sales,
        tool_type="business",
    )
)