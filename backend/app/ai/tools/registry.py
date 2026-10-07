"""工具注册表（Phase 3）：全局的能力目录 + 统一分发入口。

为什么需要一个全局注册表：
    ① 模型要先"知道有哪些工具"，才能选。这份清单必须能从一处枚举出来
       （→ list_tools），不能散落在各模块里靠 import 手动拼。
    ② 模型返回的是**工具名 + 一堆 JSON 参数**（模型生成的字符串，不可信）。
       从这个名字找到真正要执行的函数、并校验参数，是同一件事的两半，
       放两个地方迟早对不上（→ get_tool + execute）。
    ③ 将来 Trace / 权限 / Evaluation 都要遍历"全部工具"，全局表是唯一入口。

为什么是全局的、而不是每次请求建一个：
    注册表里存的是**能力定义**（函数、Schema），不含任何请求态数据 ——
    它可以在进程启动时建好、之后只读。请求态的东西（DB 会话、当前组织）
    走 ToolContext 显式传参，不污染注册表。
    混进去的后果：并发请求会互相看到对方的 session，这是最难查的一类 bug。

辨析：list_tools() 给的是**本进程注册了哪些工具**，
      与 Agent 绑定了哪些工具是两件事（docs/04-tool-design.md §4：
      绑定即权限边界，Supervisor / Reviewer 不绑工具）。
      本文件只管前者，"谁能用"由调用方按 tool_type 过滤。
"""
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from app.ai.tools.base import Tool, ToolContext, ToolResult
from app.ai.tools.permissions import tool_allowed
from app.core.audit import write_audit

# 进程级注册表：name → Tool
_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    """注册一个工具，返回它本身（方便在模块底部一行搞定注册）。

    重名直接报错而不是覆盖：两个同名工具互相顶掉，表现是"模型调 A 实际跑了 B"，
    或者某次重启后行为变了 —— 都极难排查。宁可启动就炸。
    """
    if tool.name in _REGISTRY:
        raise ValueError(f"工具名重复注册：{tool.name}")
    _REGISTRY[tool.name] = tool
    return tool


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def list_tools() -> list[Tool]:
    """全部已注册工具（注册顺序）。"""
    return list(_REGISTRY.values())


def build_tools_for_role(role: str) -> list[Tool]:
    """该角色有权的工具对象列表（8b T5，Step 5 钦定形状：**返回 Tool 本体**）。

    schema 转换（to_openai_schema）留在调用方 tool_loop：本函数只回答「谁能用哪些」，
    「长什么格式」是 OpenAI 协议的事，两层各一份职责。
    权限判据与 execute 的 in-band 闸**同一个函数**（tool_allowed × tool.permission）——
    构造面与执行面共用一张表，就不可能出现「schema 里看得见、执行时吃 403」的
    口径漂移（8a F1 降质的根因正是两口径分开长）。未知角色 → 空列表
    （tool_allowed 的默认拒绝语义原样透传）。
    """
    return [t for t in _REGISTRY.values() if tool_allowed(role, t.permission)]


def to_openai_schema(tool: Tool) -> dict:
    """把 Tool 翻译成 OpenAI function calling 的格式，喂给 bind_tools。

    args_schema 直接 model_json_schema() 即可：Pydantic 模型的字段类型 + Field 描述
    就是模型需要的全部信息。description 写得好不好，直接决定模型选不选对工具。
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.args_schema.model_json_schema(),
        },
    }


async def execute(name: str, raw_args: dict, ctx: ToolContext) -> ToolResult:
    """按名字找到工具、校验参数、执行，全程计时。

    这是模型输出与真实代码之间**唯一的关口**，所以四件事都在这里做：
        存在性 → 参数校验 → 权限闸（8a，06 §6.3）→ 执行（含兜底捕获）

    为什么每一层失败都返回 ToolResult 而不是抛异常：
        模型幻觉出一个不存在的工具名、参数填错、工具内部代码炸 ——
        这三种都会发生，而且都**应该让模型知道**（它可能换个名字或改参数就好了）。
        抛异常会直接中断整轮问答，模型连补救的机会都没有。

    为什么还要兜一层 except Exception：
        executor 的契约是"不抛异常"，但契约靠自觉。第三方库（搜索 API、
        数据库驱动）会在想不到的地方抛。这里兜住，保证"工具炸了"
        表现成"这一轮少了个工具的结果"，而不是整个请求 500。
    """
    tool = _REGISTRY.get(name)
    if tool is None:
        # 8b T10 R-T6b：不枚举可用表。模型幻觉出的名字下一句可能就是权限档
        # 之外的工具——把全量名录（含 member 无权的高危工具）白送给模型，
        # 既烧 token 又扩探测面。模型要的信息「这名字不存在」已经足够改错。
        return ToolResult(ok=False, error=f"没有名为 {name} 的工具")

    try:
        args = tool.args_schema.model_validate(raw_args)
    except ValidationError as exc:
        # 只取前 3 条错误：模型只要知道哪个字段不对就行，给全量校验堆栈纯浪费 token
        return ToolResult(ok=False, error=f"参数不合法：{exc.errors()[:3]}")

    if not tool_allowed(ctx.role, tool.permission):
        await write_audit(
            organization_id=ctx.organization_id, user_id=ctx.user_id, action="tool_denied",
            target_type="tool", target_id=tool.name,
            detail={"permission": tool.permission, "role": ctx.role},
        )
        # 仍是 ToolResult 不是异常（本函数"永远返回 ToolResult"契约）：
        # 模型要知道"这个没权限"才能换路；403 信封语义留给 HTTP 面。
        # 文案里的 AUTH_403002 是给测试与 Trace 的机读锚点。
        return ToolResult(ok=False, error=f"权限不足（AUTH_403002）：角色 {ctx.role} 未授权 {tool.permission}")

    started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    try:
        result = await tool.executor(args, ctx)
        if not isinstance(result, ToolResult):
            # 执行器忘了 return（None）或返回了半路数据：契约违约按失败处理。
            # 不拦的话下面第一句 result.xxx 就 AttributeError，还炸在 try 外面，
            # 整条 SSE / 任务链跟着 500 —— 正是本函数"永远返回 ToolResult"要防的事。
            result = ToolResult(ok=False, error=f"{name} 执行器返回了 {type(result).__name__}，应为 ToolResult")
    except Exception as exc:
        # 带类型名：模型的错误信息里 "KeyError: 'region'" 比 "'region'" 有用得多
        result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    result.finished_at = datetime.now(timezone.utc)
    result.started_at = started_at
    # duration_ms 用 perf_counter 而不是两个 wall-clock 时刻相减：
    # 单调时钟不受系统时间调整影响，也不会把亚毫秒的执行截断成 0。
    # （两个时刻仍要如实落库，但它们是"墙上时间"，用来排序和画时间轴）
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result