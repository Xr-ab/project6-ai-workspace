"""数据分析工具（Phase 3）：python_calculator / csv_reader / excel_reader / data_statistics。

sql_query 也属于本文件（docs/09-roadmap.md 的目录约定），等业务表建好后补进来。

文件从哪来（本模块最重要的约定）：
    数据工具的入口不是磁盘路径，而是 **document_id** —— 所有文件都从知识库
    上传链路进来（Phase 2），磁盘上只存 uuid 文件名，模型不可能知道路径。
    典型链路：用户问"分析这个表" → 模型先 rag_search 找到表 → 拿到
    document_id → csv_reader / excel_reader 看结构 → data_statistics 出统计。
    这样顺带解决两件事：
    ① 权限：document 按 organization_id 过滤，模型编造别人的 id 读不到
    ② 路径安全：模型输入里永远不出现磁盘路径，没有路径穿越可言

为什么用标准库（csv / openpyxl / statistics）而不是 pandas：
    这批工具只做"读表 + 描述统计"，用不上向量化 / 合并 / 透视，
    pandas 却要连带拉进几十 MB 依赖。真要做复杂分析（Phase 6+）再引入。

为什么 python_calculator 不用 eval：
    eval 只要漏白名单一个名字，就等于把任意代码执行交给模型。
    这里走"AST 白名单求值"：先把表达式解析成语法树，只允许数字、
    四则运算、白名单函数这几类节点，逐节点求值 —— 语法上根本表达不出
    import / getattr / 下标，不需要去猜"这串字符危不危险"。
"""
import ast
import csv
import io
import math
import re
import statistics
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.concurrency import run_in_threadpool
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.ai.rag.parser import read_text
from app.ai.tools.base import Tool, ToolContext, ToolResult
from app.ai.tools.registry import register
from app.data.db import engine
from app.data.models import Document
from app.data.repositories import document_repo

# 单元格文本上限：防某个超长单元格（整段 JSON、长描述）把 prompt 吃掉
CELL_TEXT_LIMIT = 200

# Excel 导出的千分位数字（"1,234.5"）。只对严格符合千分位格式的串去逗号，
# 避免 "1,5"（欧洲小数写法）被错误地变成 15。
_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


# ---------------- 表格读取（csv_reader / excel_reader / data_statistics 共用） ----------------


def _norm_cell(value: Any) -> str:
    """单元格 → 干净的字符串。统计时再决定要不要转数字。"""
    if value is None:
        return ""
    # Excel 的整数存成 5.0，展示成 "5" 更符合直觉，float() 也照样能转
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value).strip()[:CELL_TEXT_LIMIT]


def _split_table(raw_rows: list[list[Any]]) -> tuple[list[str], list[list[str]]]:
    """首行当表头，其余是数据行。"""
    headers = [_norm_cell(c) for c in raw_rows[0]]
    rows = [[_norm_cell(c) for c in row] for row in raw_rows[1:]]
    return headers, rows


def _read_table(
    path: Path, file_type: str, sheet: str | None
) -> tuple[list[str], list[list[str]], list[str]]:
    """读表格文件，返回 (表头, 数据行, sheet 名列表)。csv 的 sheet 列表为空。

    同步函数：调用方必须放进 run_in_threadpool，别让大文件读卡住事件循环。
    """
    if file_type == "csv":
        # BOM：带 BOM 的 UTF-8 文件，第一个表头会带 \ufeff，
        # 模型按肉眼看到的列名对不上，所以开头剥掉
        text = read_text(path).lstrip("\ufeff")
        raw = [r for r in csv.reader(io.StringIO(text)) if any(str(c).strip() for c in r)]
        if not raw:
            raise ValueError("文件里没有数据行")
        headers, rows = _split_table(raw)
        return headers, rows, []

    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        if sheet is not None and sheet not in wb.sheetnames:
            raise ValueError(f"sheet '{sheet}' 不存在（可用：{', '.join(wb.sheetnames)}）")
        ws = wb[sheet] if sheet is not None else wb.worksheets[0]
        raw = [
            list(row)
            for row in ws.iter_rows(values_only=True)
            if any(c is not None and str(c).strip() for c in row)
        ]
        sheet_names = list(wb.sheetnames)
    finally:
        wb.close()
    if not raw:
        raise ValueError("该 sheet 没有数据行")
    headers, rows = _split_table(raw)
    return headers, rows, sheet_names


async def _get_table_document(
    ctx: ToolContext, raw_id: str
) -> tuple[Document | None, ToolResult | None]:
    """把模型给的 document_id 变成"当前组织里的一份数据文件"。

    返回 (文档, None) 或 (None, 已就绪的错误结果)。只校验 UUID / 归属 / 是表格；
    具体是 csv 还是 xlsx 由各工具自己把关（报错里才能给出对的替代工具）。
    """
    try:
        document_id = uuid.UUID(raw_id)
    except ValueError:
        return None, ToolResult(ok=False, error=f"document_id 不是合法的 UUID：{raw_id}")

    doc = await document_repo.get_document(
        ctx.session, document_id=document_id, organization_id=ctx.organization_id
    )
    if doc is None:
        return None, ToolResult(
            ok=False,
            error=f"文档 {raw_id} 不存在或不属于当前组织（id 要来自 rag_search 的返回结果）",
        )
    if doc.file_type not in ("csv", "xlsx"):
        return None, ToolResult(
            ok=False,
            error=f"文档 {doc.filename} 是 {doc.file_type}，不是表格数据文件",
        )
    return doc, None


class _DocumentArgs(BaseModel):
    """三个表格工具共用的参数基类。"""

    document_id: str = Field(description="文档 id（UUID），来自 rag_search 返回结果里的 document_id")


class CsvReaderArgs(_DocumentArgs):
    max_rows: int = Field(default=20, ge=1, le=100, description="预览前多少行，默认 20")


async def csv_reader(args: CsvReaderArgs, ctx: ToolContext) -> ToolResult:
    """看一份 CSV 的列结构和前几行。模型先看结构才知道统计哪列、怎么算。"""
    doc, err = await _get_table_document(ctx, args.document_id)
    if err is not None:
        return err
    if doc.file_type != "csv":
        return ToolResult(
            ok=False,
            error=f"{doc.filename} 是 {doc.file_type}，本工具只读 CSV（Excel 请用 excel_reader）",
        )
    try:
        headers, rows, _ = await run_in_threadpool(_read_table, Path(doc.file_path), "csv", None)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))

    shown = rows[: args.max_rows]
    return ToolResult(
        ok=True,
        data={
            "filename": doc.filename,
            "columns": headers,
            "total_rows": len(rows),
            "rows": shown,
        },
        rows=len(rows),
        truncated=len(rows) > len(shown),
    )


class ExcelReaderArgs(_DocumentArgs):
    sheet: str | None = Field(default=None, description="sheet 名；不填默认第一个 sheet")
    max_rows: int = Field(default=20, ge=1, le=100, description="预览前多少行，默认 20")


async def excel_reader(args: ExcelReaderArgs, ctx: ToolContext) -> ToolResult:
    """看一份 Excel 的 sheet 列表、列结构和前几行。"""
    doc, err = await _get_table_document(ctx, args.document_id)
    if err is not None:
        return err
    if doc.file_type != "xlsx":
        return ToolResult(
            ok=False,
            error=f"{doc.filename} 是 {doc.file_type}，本工具只读 Excel（CSV 请用 csv_reader）",
        )
    try:
        headers, rows, sheet_names = await run_in_threadpool(
            _read_table, Path(doc.file_path), "xlsx", args.sheet
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))

    shown = rows[: args.max_rows]
    return ToolResult(
        ok=True,
        data={
            "filename": doc.filename,
            "sheets": sheet_names,
            "sheet": args.sheet if args.sheet is not None else sheet_names[0],
            "columns": headers,
            "total_rows": len(rows),
            "rows": shown,
        },
        rows=len(rows),
        truncated=len(rows) > len(shown),
    )


class DataStatisticsArgs(_DocumentArgs):
    column: str = Field(description="列名，必须与表头完全一致（区分大小写）")
    sheet: str | None = Field(default=None, description="Excel 的 sheet 名；CSV 不用传")


def _column_stats(headers: list[str], rows: list[list[str]], column: str) -> dict:
    """对一列做描述统计。纯函数（不碰 DB / 磁盘），方便单独验证。

    数值列 / 文本列自动判断：可转数字的占非空值一半以上按数值算，
    个别转不动的如实报 unparsable 数。文本列给 top 值 + 去重数。
    """
    if column not in headers:
        raise KeyError(f"列 '{column}' 不存在（可用列：{', '.join(headers)}）")
    idx = headers.index(column)
    values = [row[idx] if idx < len(row) else "" for row in rows]
    non_empty = [v for v in values if v != ""]

    numbers: list[float] = []
    unparsable = 0
    for v in non_empty:
        s = v.replace(",", "") if _THOUSANDS_RE.match(v) else v
        try:
            numbers.append(float(s))
        except ValueError:
            unparsable += 1

    stats: dict = {
        "column": column,
        "total_rows": len(rows),
        "missing": len(values) - len(non_empty),
    }
    if numbers and len(numbers) * 2 >= len(non_empty):
        stats["type"] = "numeric"
        stats["count"] = len(numbers)
        stats["unparsable"] = unparsable
        stats["min"] = min(numbers)
        stats["max"] = max(numbers)
        stats["mean"] = statistics.fmean(numbers)
        stats["median"] = statistics.median(numbers)
        if len(numbers) >= 2:
            stats["stdev"] = statistics.stdev(numbers)
            quartiles = statistics.quantiles(numbers, n=4)
            stats["p25"] = quartiles[0]
            stats["p75"] = quartiles[2]
    else:
        counts = Counter(non_empty)
        stats["type"] = "text"
        stats["count"] = len(non_empty)
        stats["distinct"] = len(counts)
        stats["top_values"] = [
            {"value": v, "count": n} for v, n in counts.most_common(10)
        ]
    return stats


async def data_statistics(args: DataStatisticsArgs, ctx: ToolContext) -> ToolResult:
    """对一列做描述统计：均值 / 中位数 / 分位数 / 缺失 / 分布。"""
    doc, err = await _get_table_document(ctx, args.document_id)
    if err is not None:
        return err
    try:
        headers, rows, _ = await run_in_threadpool(
            _read_table, Path(doc.file_path), doc.file_type, args.sheet
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    try:
        data = _column_stats(headers, rows, args.column)
    except KeyError as exc:
        return ToolResult(ok=False, error=str(exc.args[0]))
    return ToolResult(ok=True, data=data, rows=len(rows))


# ---------------- python_calculator（AST 白名单沙箱） ----------------


class CalculatorArgs(BaseModel):
    expression: str = Field(
        description=(
            "Python 算术表达式，如 (12500 * 1.08) / 12、round(3.14159, 2)、sqrt(144)。"
            "只支持纯计算：数字、+ - * / // % **、括号、min/max/abs/round/sqrt、常量 pi 和 e。"
            "不能访问变量、文件、网络。"
        )
    )


# 求值环境白名单：除此之外的任何名字都不存在
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max, "sqrt": math.sqrt}
_CONSTS = {"pi": math.pi, "e": math.e}

# 防资源耗尽：9**99999999 这类表达式不设防会算几分钟、吃几百 MB 内存
_MAX_EXPONENT = 10_000  # 幂运算指数上限
_MAX_INT_DIGITS = 100_000  # 幂运算「计算前」估算的中间结果位数上限（防内存炸）
# Python 的 int→str 转换默认上限 4300 位（sys.set_int_max_str_digits）：结果超过它，
# json.dumps（回填给模型）与写 JSONB（落 tool_calls）都会抛 ValueError 崩掉整轮，
# 连错误帧都推不出。所以「计算后」再按位长度兜一道，把结果限制在可序列化范围内。
# 用 bit_length 估算十进制位数（≈ bits×0.301），绝不去 str 一个超大整数（那本身就抛错）。
_MAX_RESULT_BITS = 13_000  # ≈ 3913 位十进制，稳低于 4300


def _calc(node: ast.expr) -> int | float:
    """对白名单内的 AST 逐节点求值。任何不在白名单里的节点直接拒绝。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("只允许数字字面量")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError(
            f"不支持的名字 '{node.id}'（可用常量：pi、e；函数要带括号调用，如 sqrt(144)）"
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _calc(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left, right = _calc(node.left), _calc(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            return left / right
        if isinstance(op, ast.FloorDiv):
            return left // right
        if isinstance(op, ast.Mod):
            return left % right
        if isinstance(op, ast.Pow):
            # 指数必须是普通大小的整数。结果位数 ≈ 底数位数 × 指数，
            # 用这个估算在**计算之前**拦截，而不是算完再看（那时内存已经吃了）
            if not isinstance(right, int) or abs(right) > _MAX_EXPONENT:
                raise ValueError(f"幂运算的指数太大（|指数| ≤ {_MAX_EXPONENT}）")
            if isinstance(left, int) and len(str(left)) * max(right, 1) > _MAX_INT_DIGITS:
                raise ValueError("中间结果太大，请化简表达式")
            return left**right
        raise ValueError("不支持的运算符")
    if isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS):
            raise ValueError("只允许调用这些函数：" + ", ".join(sorted(_FUNCS)))
        if node.keywords:
            raise ValueError("函数参数不支持 key=value 写法，请按位置传")
        return _FUNCS[node.func.id](*[_calc(a) for a in node.args])
    raise ValueError(f"不支持的表达式成分：{type(node).__name__}")


async def python_calculator(args: CalculatorArgs, ctx: ToolContext) -> ToolResult:
    """受限计算沙箱：LLM 自己算多位数乘除会错，凡数字计算都该走这里。"""
    try:
        tree = ast.parse(args.expression.strip(), mode="eval")
    except SyntaxError as exc:
        return ToolResult(ok=False, error=f"表达式语法错误：{exc.msg}")
    try:
        value = _calc(tree.body)
    except ZeroDivisionError:
        return ToolResult(ok=False, error="除数为 0")
    except (ValueError, OverflowError) as exc:
        return ToolResult(ok=False, error=str(exc) or type(exc).__name__)
    if isinstance(value, float) and not math.isfinite(value):
        return ToolResult(ok=False, error="计算结果溢出（超出浮点数范围）")
    if isinstance(value, int) and value.bit_length() > _MAX_RESULT_BITS:
        return ToolResult(ok=False, error="结果过大（超出可返回的整数上限），请化简表达式")
    return ToolResult(ok=True, data={"expression": args.expression, "result": value})


# ---------------- 注册 ----------------

register(
    Tool(
        name="python_calculator",
        description=(
            "执行一条 Python 算术表达式并返回精确结果（受限沙箱：无变量、无文件、无网络）。\n"
            "何时用：任何需要精确计算的场景 —— 乘除、百分比、同比环比、汇总几个数。"
            "语言模型自己算多位数乘除会出错，涉及数字计算必须用本工具。\n"
            "何时不用：从数据表取数 —— 先用 csv_reader / excel_reader / sql_query，"
            "拿到数字后再用本工具算。"
        ),
        args_schema=CalculatorArgs,
        executor=python_calculator,
        tool_type="data",
    )
)

register(
    Tool(
        name="csv_reader",
        description=(
            "查看一份 CSV 数据文件的列结构和前若干行。\n"
            "何时用：用户提到分析某个表/文件，先用 rag_search 拿到 document_id，"
            "再用本工具看有哪些列、长什么样，然后决定统计哪列。\n"
            "何时不用：Excel 文件（用 excel_reader）；只需要找文档里的文字段落（用 rag_search）。"
        ),
        args_schema=CsvReaderArgs,
        executor=csv_reader,
        tool_type="data",
    )
)

register(
    Tool(
        name="excel_reader",
        description=(
            "查看一份 Excel 数据文件的 sheet 列表、列结构和前若干行。\n"
            "何时用：同 csv_reader，但文件是 xlsx。多 sheet 时先看默认 sheet，"
            "需要别的 sheet 用 sheet 参数指定名字。\n"
            "何时不用：CSV 文件（用 csv_reader）。"
        ),
        args_schema=ExcelReaderArgs,
        executor=excel_reader,
        tool_type="data",
    )
)

register(
    Tool(
        name="data_statistics",
        description=(
            "对数据文件的某一列做描述统计：数值列返回 均值/中位数/分位数/最值/缺失，"
            "文本列返回 去重数/top 值分布。\n"
            "何时用：看完表结构（csv_reader / excel_reader）后，用户问"
            "'平均/最大/分布/占比'这类问题。\n"
            "何时不用：要跨列组合计算（先取数再用 python_calculator）；"
            "列不存在时先看结构再改列名。"
        ),
        args_schema=DataStatisticsArgs,
        executor=data_statistics,
        tool_type="data",
    )
)


# ---------------- sql_query（对业务库执行只读 SELECT） ----------------
#
# Phase 3 里安全约束最重的工具：它把"任意 SQL"交给模型写。三层防线，缺一不可：
#
#   ① 字符串层（_validate_sql）—— 挡手滑和明显意图，且给模型**清晰可改的错误提示**。
#      这层永远不可能完备，所以绝不能是唯一防线。
#   ② 连接层（独立连接 + SET TRANSACTION READ ONLY + statement_timeout）—— 真防线。
#      数据库自己拒绝写操作，不管 SQL 长什么样。docs/04-tool-design.md §5
#      写的"连接层限制"就是这条。
#   ③ 结果层（行数 + 列数上限 + 类型转 JSON）—— 防爆上下文、爆内存、序列化报错。
#
# 为什么字符串层不可能完备，举个能骗过黑名单的真实写法：
#     WITH x AS (DELETE FROM customers RETURNING *) SELECT * FROM x
#   它以 WITH 开头（不是 SELECT），可以继续变换写法绕过任何词表。
#   只读事务不看 SQL 长什么样，直接拒绝 —— 这才是兜得住的。

_ALLOWED_TABLES = frozenset({"customers", "products", "orders", "sales", "regions"})

# 只读工具里这些词不该出现。用 \b 边界，所以 created_at / update_time /
# settle_date 这类列名不会被误伤（\b 认 word 边界，下划线和字母都算 word 字符）。
# set_config / current_setting 单独点名：它们是**函数**，
# "\bset\b" 在 set_config 里匹配不到（下划线算 word 字符，没有词边界）——
# 不点名就能用 SELECT set_config('statement_timeout','0') 关掉超时。
# RESET 不用拦：它是语句级命令，开头就过不了 ^select|with 那关。
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|"
    r"vacuum|analyze|refresh|set|call|do|comment|reindex|cluster|lock|checkpoint|"
    r"set_config|current_setting|pg_\w+|information_schema)\b",
    re.I,
)
_COMMENT = re.compile(r"(--|/\*)")
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.I)
_CTE_HEAD = re.compile(r"\bwith\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", re.I)
_CTE_MORE = re.compile(r",\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", re.I)

# 组织过滤占位符：模型写 :org_id，代码替换成真实 UUID。
ORG_PLACEHOLDER = ":org_id"
SQL_TIMEOUT_MS = 10_000  # docs §5：SQL 超时 10s
DEFAULT_ROW_LIMIT = 50
MAX_ROW_LIMIT = 200
MAX_COLUMNS = 30


class SqlQueryArgs(BaseModel):
    sql: str = Field(
        description=(
            "一条只读 SELECT 语句。硬性要求："
            "① 必须用 :org_id 占位符做组织过滤，如 WHERE organization_id = :org_id；"
            "② 只能查 customers / products / orders / sales / regions；"
            "③ 不能写注释、不能有多条语句。"
        )
    )
    limit: int = Field(
        default=DEFAULT_ROW_LIMIT,
        ge=1,
        le=MAX_ROW_LIMIT,
        description=f"最多返回多少行（默认 {DEFAULT_ROW_LIMIT}，上限 {MAX_ROW_LIMIT}）",
    )


def _jsonable(value: Any) -> Any:
    """把 SQL 结果里的非 JSON 原生类型转成 JSON 能表达的形式。

    asyncpg 查出来的是 Decimal / date / datetime / UUID —— 它们都不是 JSON 原生类型。
    原样塞进 JSONB 列或 json.dumps 都会报 "not JSON serializable"。

    Decimal 转 **str** 而不是 float：float 会把 12345.67 变成 12345.670000000001，
    金额数字在报告里出现这种尾数会让人怀疑整个系统的可信度。
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        # nan/inf 能穿过 isinstance(float) 白名单，但不是合法 JSON，JSONB 列会拒收——转字符串
        return value if math.isfinite(value) else str(value)
    return str(value)


def _validate_sql(sql: str) -> str | None:
    """字符串层检查。返回错误信息；None 表示通过。

    错误信息是**写给模型看的**，所以每条都要说"怎么改"而不是"你错了" ——
    模型据此重写 SQL 重试，这正是"失败返回 ToolResult 而非抛异常"的价值。
    """
    body = sql.strip().rstrip(";").strip()
    if not body:
        return "SQL 为空。"
    if ";" in body:
        return "只允许单条语句：检测到分号后还有内容，请改写成一条 SELECT。"
    if _COMMENT.search(body):
        return "SQL 里不要写注释（-- 或 /* */）：注释可用于绕过语法检查，本工具一律拒绝。"
    if not re.match(r"^(select|with)\b", body, re.I):
        return "只能执行 SELECT 查询（或以 WITH 开头的 CTE 查询）。"
    hit = _FORBIDDEN.search(body)
    if hit is not None:
        return f"只读工具不允许出现 {hit.group(0).upper()}，请改写为纯查询。"
    if ORG_PLACEHOLDER not in body:
        return (
            "SQL 必须包含 :org_id 占位符做组织过滤，"
            "例如 WHERE organization_id = :org_id。"
            "直接写这个占位符即可，不要自己编造或猜测 id。"
        )

    # 表白名单。CTE 里定义的名字（WITH t AS (...)）也是 FROM 的合法引用，
    # 所以要先从 refs 里减掉，否则 "WITH t AS (...) SELECT * FROM t" 会被误判。
    cte_names = {m.lower() for m in _CTE_HEAD.findall(body)}
    cte_names |= {m.lower() for m in _CTE_MORE.findall(body)}
    refs = [m.lower() for m in _TABLE_REF.findall(body)]
    real_refs = [r for r in refs if r in _ALLOWED_TABLES]
    unknown = {r for r in refs} - _ALLOWED_TABLES - cte_names
    if unknown:
        return (
            f"不认识的表：{', '.join(sorted(unknown))}。"
            f"只能查：{', '.join(sorted(_ALLOWED_TABLES))}。"
        )
    # 组织过滤要**逐表**做，不是整条 SQL 做一次就够：
    # "FROM sales JOIN products ON ... WHERE sales.organization_id = :org_id"
    # 只锁住了 sales，products 侧会把别的组织的商品名一起带出来——
    # 单查 :org_id 是否存在拦不住这种写法。占位符会被代码全量替换，
    # 所以要求的数量 = 真实表引用数（自关联算两次，正好也要过滤两次）。
    if body.count(ORG_PLACEHOLDER) < len(real_refs):
        return (
            f"SQL 里引用了 {len(real_refs)} 次业务表"
            f"（{', '.join(sorted(set(real_refs)))}），"
            f"但只出现 {body.count(ORG_PLACEHOLDER)} 次 :org_id。"
            "JOIN / 子查询里的**每张表都要各自**加 organization_id = :org_id 过滤，"
            "漏一张就会把其他组织的数据混进结果。"
        )
    return None


async def sql_query(args: SqlQueryArgs, ctx: ToolContext) -> ToolResult:
    error = _validate_sql(args.sql)
    if error is not None:
        return ToolResult(ok=False, error=error)

    body = args.sql.strip().rstrip(";").strip()
    # :org_id 由代码替换成真实 UUID。值是代码生成的、不是模型输入，所以字面量替换
    # 没有注入风险；好处是模型既不需要知道 org id 的值，也**不可能忘记过滤**
    # （缺了占位符在上面就被拒了）。
    sql = body.replace(ORG_PLACEHOLDER, f"'{ctx.organization_id}'")
    # 外面包一层、取 limit+1 行：多取的那一行只用来判断"是否被截断"，
    # 不返回给模型 —— 否则模型会看到一条实际不存在的结果。
    wrapped = f"SELECT * FROM (\n{sql}\n) AS _q LIMIT {args.limit + 1}"

    try:
        # 用**独立连接**而不是 ctx.session，两个原因：
        #   ① SET TRANSACTION READ ONLY 必须是事务的第一条语句，而请求 session
        #      可能已经开过事务了；
        #   ② 更要命的是只读会污染整个事务 —— 同一请求稍后要写 assistant 消息
        #      和 tool_calls，那些 INSERT 会全部失败。
        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text(f"SET LOCAL statement_timeout = {SQL_TIMEOUT_MS}"))
            result = await conn.execute(text(wrapped))
            rows = result.fetchall()
            columns = list(result.keys())
    except Exception as exc:  # noqa: BLE001 —— 见下方注释：错误转成可读提示给模型
        # 这里显式捕获、不走 registry 的兜底：registry 兜底只会给出
        # "ProgrammingError: <几百字原始报错>"，模型读不懂也没法改。
        # 换成简短提示后，模型能立刻改 SQL 重试。
        name = type(exc).__name__
        detail = str(exc)[:300]
        if "statement timeout" in detail or "QueryCanceled" in name:
            return ToolResult(
                ok=False,
                error=(
                    f"查询超时（>{SQL_TIMEOUT_MS / 1000:.1f}s）。请缩小范围："
                    "加时间条件、减少 JOIN、或先做聚合。"
                ),
            )
        # 类型不匹配是实测里最高频的一类失败：模型拿 regions.code（'R-EC' 这种字符串）
        # 去和 region_id（uuid）比较。原始报错只有一句 "operator does not exist:
        # uuid = character varying"，模型看不出该改哪里，实测连撞 4 轮才绕过去。
        # 直接告诉它正确写法，一轮就能改对。
        if "uuid = character varying" in detail or "character varying = uuid" in detail:
            return ToolResult(
                ok=False,
                error=(
                    "类型不匹配：xxx_id 这类外键列是 uuid，不能拿 code / sku / name 这类"
                    "文本列去比。要按名称关联，请写成 "
                    "(SELECT id FROM regions WHERE organization_id = :org_id AND name = '华东')，"
                    "取的是 id 而不是 code。"
                ),
            )
        return ToolResult(ok=False, error=f"SQL 执行失败：{name}: {detail}")

    truncated = len(rows) > args.limit
    rows = rows[: args.limit]
    # 列也要截：宽表（几十列）光列名就能吃掉大量 prompt 预算
    columns = columns[:MAX_COLUMNS]
    data = [dict(zip(columns, (_jsonable(v) for v in row))) for row in rows]

    return ToolResult(
        ok=True,
        data={"columns": columns, "rows": data},
        rows=len(data),
        truncated=truncated,
    )


register(
    Tool(
        name="sql_query",
        description=(
            "对业务数据库执行一条只读 SELECT，用于经营分析类问题。\n"
            "可查表结构（均已按组织隔离，查询必须过滤 organization_id；"
            "带 → 的是外键，指向目标表的 id 列，不是 code / sku）：\n"
            "  regions(id, code, name, level, parent_id → regions.id)\n"
            "  customers(id, code, name, industry, region_id → regions.id,"
            " tier, risk_level)\n"
            "  products(id, sku, name, category, unit_price, unit_cost, status)\n"
            "  orders(id, order_no, customer_id → customers.id,"
            " product_id → products.id, region_id → regions.id,"
            " order_date, quantity, unit_price, amount, status)\n"
            "  sales(id, region_id → regions.id, product_id → products.id,"
            " customer_id → customers.id, sale_date, quantity, amount, cost, profit)\n"
            "写 SQL 的硬性要求：**每一张**业务表（含 JOIN、子查询里的）都要各自写 "
            "organization_id = :org_id（照抄这个占位符，不要编造 id），"
            "漏一张会查到别的组织的数据并被直接拒绝；只能一条 SELECT；不能写注释。\n"
            # 这条数据粒度说明是实测踩出来的：orders/sales 只挂省级区域，
            # 模型按城市名（如"上海"）去 join 会查到 0 行，然后回一句
            # "没有查到上海的销售数据" —— 看起来像工具坏了，实际是它找错了粒度。
            # 没有这行提示，模型会用一个错误的结论回答用户（比查不到更糟）。
            "数据粒度提醒：orders / sales 的 region_id 只指向**省级**区域"
            "（level='province'），市级区域（如'上海'）没有直接挂业务数据；"
            "要按城市看，得先用 parent_id 找到它的省份。\n"
            "何时用：需要聚合/排序/跨表关联的定量分析，例如销售额趋势、毛利排行、"
            "区域对比、客户贡献度。\n"
            "何时不用：单表查一条详情用 query_customer / query_product / query_sales "
            "更稳；上传的 CSV/Excel 文件用 csv_reader / data_statistics；"
            "知识库文档内容用 rag_search。"
        ),
        args_schema=SqlQueryArgs,
        executor=sql_query,
        tool_type="data",
    )
)
