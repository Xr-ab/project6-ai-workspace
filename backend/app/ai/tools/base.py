"""Tool 抽象（Phase 3）：工具 = 经过封装的确定能力，有输入输出契约。

为什么不直接把逻辑写进 Prompt（比如让模型输出 SQL 文本，我们再执行）：
    Prompt 里的规则是"建议"，模型可以不听、可以编造参数、可以改主意。
    工具是**代码**：参数过不了 Pydantic 校验就是调不通，SQL 不是 SELECT 就执行不了。
    把"能不能做"交给代码判断，只把"要不要用哪个工具"交给模型判断 —— 这是
    Tool Calling 与"在 Prompt 里写指令"的本质分界。

三个数据的职责划分（本文件最重要的概念）：
    args_schema  给**模型**看的参数契约，会变成 function calling 的 JSON Schema
    ToolContext  给**工具**用的运行时依赖（DB 会话 / 组织 / 用户），模型看不到
    ToolResult   给**调用方**（tool loop / tool_calls 表）用的统一包装

为什么 ToolContext 要单独抽出来、而不是塞进 args_schema：
    args_schema 的内容模型看得见、也由模型填。如果把 session / organization_id
    放进去，等于让模型来决定"查哪个组织的数据" —— 越权就从这里开始。
    运行时依赖必须由代码注入，模型的输入里永远不出现它们。
"""
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class ToolContext:
    """单次工具调用的运行时依赖。由调用方（tool loop）构造，不来自模型输入。"""

    session: AsyncSession
    organization_id: uuid.UUID
    user_id: uuid.UUID
    # 8a 工具权限闸的输入。刻意不给默认值：给 "member" 会让漏传点静默拒功能，
    # 给 "admin" 是后门——炸在构造点比什么都好。
    role: str
    # Agent 任务侧专用：节点把每次工具调用记录塞进这个 sink，执行层（task_runner）在
    # 任务末尾统一落 tool_calls 表（与 spans→agent_runs 同一套「执行层负责落库」的分层，
    # 不让 AI 层的节点直接写库）。Chat 侧留 None（它有自己的 save_tool_calls 路径）。
    tool_call_sink: "list[ToolCallRecord] | None" = None
    # 当前正在执行的 span 身份（node_guard 在进入/退出节点时设置与恢复，见 errors.py）。
    # Chat 侧不经过 node_guard，恒为 None —— 那本来就没有 Trace 树。
    current_span_id: uuid.UUID | None = None


@dataclass
class ToolCallRequest:
    """模型要求调一次工具。**模型生成的内容，不可信**，执行前必须过校验。

    id 是模型给的调用编号，回填结果时原样带回（ToolMessage.tool_call_id）。
    为什么必须对上：模型一轮可能同时要调多个工具，回填时如果对不上号，
    它会把"查客户的结果"当成"查产品结果"来解释 —— 而且**不会报错**。
    """

    id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    """工具执行结果统一包装（docs/04-tool-design.md §3）。

    ok / error 是给模型看的：ok=False 时 error 文案会回填进对话，
    让模型有机会换参数重试，而不是整个请求失败。

    data 是**摘要化**过的结果：大结果直接塞回模型会爆上下文，
    完整结果另行落 tool_calls 表供 Trace / 报告取用（见 §3 最后一条）。
    data 必须是 JSON 可序列化的（dict / list / str / 数字），
    因为它要同时走两条路：回填给模型 + 写进 tool_calls.output_json。
    """

    ok: bool
    data: Any = None
    error: str | None = None
    rows: int | None = None  # 返回行数（SQL / 文件读取类工具）
    duration_ms: int = 0  # 由 registry.execute 统一计时，工具自己不用填
    truncated: bool = False  # 结果是否被截断过（前端要提示"只显示了前 N 条"）

    # 起止时刻，同样由 registry.execute 在调用执行器的前后各取一次真实时间。
    #
    # 为什么不留给调用方用 duration_ms 反推（曾经的写法）：
    #   反推出来的两个时刻共享同一个"现在"，只能算出一条时间轴 ——
    #   同一批里"先跑 A（5ms）再跑 B（800ms）"，反推会得到 B 更早开始，
    #   顺序刚好反了。而 tool_calls 表要靠这个顺序还原"模型是怎么一步步查的"，
    #   Phase 4 起还要拿它画 Trace 时间轴。真实时刻只能在实际计时的位置取。
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class ToolCallRecord:
    """一次工具调用的完整记录：请求 + 结果。

    请求与结果放一个对象里，是因为它们永远成对出现、且共用同一个 id。
    同时是三个下游的共同输入：
        ① 落 tool_calls 表（Trace 数据源）
        ② 推 SSE 帧（前端展示"调用了什么、耗时多久"）
        ③ 回填进消息列表（模型据此继续）
    """

    call: ToolCallRequest
    result: ToolResult
    # 产出这次调用的 Agent span。落库时写进 tool_calls.parent_span_id（Trace 树的父指针）。
    parent_span_id: uuid.UUID | None = None


def record_tool_call(ctx: ToolContext, call: ToolCallRequest, result: ToolResult) -> ToolCallRecord:
    """构造工具调用记录，并当场盖上「出自哪个 span」的归属。

    必须在产出点盖：任务末尾统一落库时，sink 里已经只剩一串记录，
    分不清谁生成的，更分不清出自重试的哪一次尝试。
    """
    return ToolCallRecord(call=call, result=result, parent_span_id=ctx.current_span_id)


# 执行函数签名。约定：**不抛异常**，失败也返回 ToolResult(ok=False, error=...)。
# 为什么统一成 async：工具里要么查库、要么发 HTTP、要么跑同步计算，
# 前两类天然是 async；同步计算自查 run_in_threadpool 即可。
# 统一成一种签名，tool loop 里就只有一条代码路径。
ToolExecutor = Callable[[BaseModel, ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    """一个可注册、可发现、可执行的工具。"""

    name: str  # 唯一名，模型看到的就是它，如 sql_query
    description: str  # 给模型看的说明：**何时用、何时不用**（不是给人看的注释）
    args_schema: type[BaseModel]  # 参数校验模型
    executor: ToolExecutor
    tool_type: str  # data / knowledge / research / business，落 tool_calls.tool_type

    @property
    def permission(self) -> str:
        """权限码（Phase 8 RBAC 用）。派生而非独立字段，避免两处名字写岔。"""
        return f"tools.{self.name}"