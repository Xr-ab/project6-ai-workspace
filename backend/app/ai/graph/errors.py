"""错误分类与节点级错误处理（Phase 4：Retry 与节点级错误处理）

设计来源：docs/03-agent-design.md §5「节点内捕获 → 写入 state.meta.error，不中断整个图」
        + docs/05-database-design.md §5.2 failure_category 分类表。

三层防线（各管一段，别越界）：
① 可重试的瞬时错误（超时/限流）→ RetryPolicy 指数退避自动重试，对节点透明
② 节点内业务失败 → try/except 捕获 → 写 errors + failed_nodes，返回降级 partial，图继续走
③ 不可恢复 → 抛 TaskNodeError(task 停止、落 failed + failure_category)，由执行层（下一项）接住落库

与 Phase 3 的分工：工具失败在 registry 层已转成 ToolResult(ok=False) 回喂模型（模型自纠），
本模块管的是「节点整体」的生死——LLM 调用本身失败、节点内非工具异常。

LangGraph 版本注意（1.2.12 实测）：
- RetryPolicy 从 langgraph.types 导入（教程里的 langgraph.retry 是旧路径，已不存在）
- 默认 retry_on（default_retry_on）对 ValueError / TimeoutError / OSError 一族【不重试】，
  只重试 ConnectionError 和 5xx——所以「超时自动重试」必须显式指定 retry_on
- node_guard 会把原始异常包成 TaskNodeError 再抛 → 重试层看到的是 TaskNodeError 而非
  TimeoutError。所以自定义 retry_on 必须「解包 __cause__」找回原始异常再判断是否瞬时，
  否则瞬时错误永远不重试（guard × retry 的层级冲突，实测踩过）
"""
import inspect
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.types import RetryPolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# failure_category（与 docs/05 §5.2 表格一一对应；TaskNodeError 只允许取这里面的值）
# ---------------------------------------------------------------------------
FAILURE_CATEGORIES: set[str] = {
    "model_timeout",      # LLM 无响应 / 网关超时
    "tool_failure",       # 工具执行失败（且模型多轮自纠后仍失败）
    "output_validation",  # 结构化输出解析失败且重试仍失败
    "context_limit",      # 超长 / 结果过大
    "rate_limit",         # 429 / 并发上限
    "review_timeout",     # Reviewer 超限（降级不置 failed，执行层特判）
    "internal_error",     # 未分类异常
}

# openai SDK 的异常族**不是**内建 TimeoutError/ConnectionError 的子类——
# 不显式加进映射表，真实 LLM 超时会被归成 internal_error 且永不重试（Phase 4 审查发现的真 bug）
try:
    from openai import (
        APIConnectionError as _OpenAIConnErr,
        APITimeoutError as _OpenAITimeoutErr,
        RateLimitError as _OpenAIRateLimitErr,
    )
except ImportError:  # 兜底：环境没装 openai 时不炸导入
    _OpenAITimeoutErr = _OpenAIConnErr = _OpenAIRateLimitErr = None  # type: ignore[assignment]

# 异常类型 → failure_category 的映射：分类靠「异常类型」判，不靠解析报错文本
_CATEGORY_BY_EXC: list[tuple[type[BaseException], str]] = [
    (TimeoutError, "model_timeout"),
    (ConnectionError, "model_timeout"),       # 网络不可达同归模型侧（多为网关/SDK 层）
    (PermissionError, "rate_limit"),          # 403/配额类多数 SDK 抛 PermissionError 家族
    (ValueError, "output_validation"),        # Pydantic 校验失败族
    (KeyError, "internal_error"),             # 代码缺陷（取错 key）不重试，归内部错误
]
if _OpenAITimeoutErr is not None:
    _CATEGORY_BY_EXC[0:0] = [
        (_OpenAITimeoutErr, "model_timeout"),  # openai 超时 ≠ 内建 TimeoutError，但都是网关超时
        (_OpenAIConnErr, "model_timeout"),     # 断连 / DNS / 握手失败
        (_OpenAIRateLimitErr, "rate_limit"),   # 429
    ]
_CATEGORY_BY_EXC = tuple(_CATEGORY_BY_EXC)

# 重试判据的异常集合（内建族 + openai 族同一份事实，别写两遍）；
# PermissionError 虽归 rate_limit 但重试无意义，不进集合
_RETRYABLE_EXC: tuple[type[BaseException], ...] = tuple(
    t
    for t in (TimeoutError, ConnectionError, _OpenAITimeoutErr, _OpenAIConnErr, _OpenAIRateLimitErr)
    if t is not None
)


class TaskNodeError(Exception):
    """节点内不可恢复错误：携带 failure_category，让执行层直接落库不再猜。

    用法：节点内捕到异常后，能降级的降级；不能降级的 raise TaskNodeError(...)——
    它必须穿透 RetryPolicy（retry_on 里排除它），终止整图，由任务执行层写 task_runs.failed。
    """

    def __init__(self, category: str, message: str, node: str):
        if category not in FAILURE_CATEGORIES:
            category = "internal_error"
        super().__init__(f"[{node}] {category}: {message}")
        self.category = category
        self.node = node
        self.message = message


def classify_exception(exc: BaseException) -> str:
    """未知异常 → 查映射表归类；对不上号的按 internal_error 兜底。"""
    for exc_type, category in _CATEGORY_BY_EXC:
        if isinstance(exc, exc_type):
            return category
    return "internal_error"


# RetryPolicy 的重试判据：只救「瞬时错误」，TaskNodeError 本身是终态信号不该重试——
# 但 node_guard 会把 TimeoutError 先包成 TaskNodeError 再抛，重试层拿到的是 TaskNodeError。
# 区分两种 TaskNodeError：
#   - guard 包装的（raise ... from exc → __cause__ 是原始异常）→ 解包看原始异常是不是瞬时
#   - 节点自己 raise 的（__cause__ 是 None）→ 业务上已判定不可恢复，绝不重试
def _retry_on(exc: Exception) -> bool:
    # guard 包装的 TaskNodeError → 解包 __cause__ 看原始异常是不是瞬时；
    # 节点直接 raise 的 TaskNodeError（cause=None）→ 业务已判定不可恢复，不重试
    target = exc.__cause__ if isinstance(exc, TaskNodeError) else exc
    return target is not None and isinstance(target, _RETRYABLE_EXC)


# 指数退避：1s → 2s（首次失败后等 1s 重试，再失败等 2s，最多 3 次尝试）
RETRY_POLICY = RetryPolicy(max_attempts=3, initial_interval=1.0, backoff_factor=2.0, retry_on=_retry_on)

RETRYABLE_NODES: dict[str, RetryPolicy] = {
    # 只给「调外部服务」的节点配：LLM 调用 / 检索 / 工具循环
    "supervisor": RETRY_POLICY,
    "data_analyst": RETRY_POLICY,
    "research": RETRY_POLICY,
    "business_analyst": RETRY_POLICY,
    "reviewer": RETRY_POLICY,
    # report / 落库类节点不配：重跑副作用大于收益，失败直接走 TaskNodeError
}


def _token_delta(state: dict, result: dict | None) -> tuple[int, int]:
    """(本次尝试的 prompt, completion) token 增量 = 返回 meta 累计 − 进入前 meta 累计。

    节点遵守「meta 覆盖型：返回旧值+本轮」的约定，所以差值即本轮真实用量；
    result 为 None（本尝试抛异常）或节点没写 meta 时，负值/缺失一律夹到 0。
    """
    before = (state or {}).get("meta") or {}
    after = (result or {}).get("meta") or {}
    delta = []
    for key in ("prompt_tokens", "completion_tokens"):
        try:
            diff = int(after.get(key, 0)) - int(before.get(key, 0))
        except (TypeError, ValueError):
            diff = 0
        delta.append(max(diff, 0))
    return delta[0], delta[1]


def node_guard(node_name: str) -> Callable:
    """节点包装器：所有节点统一走这里，异常分类逻辑只写一遍。

    成功 → 原样返回节点的 partial
    TaskNodeError → 原样上抛（这是「决定任务失败」的信号，不能吞）
    其它异常 → 分类后包成 TaskNodeError 上抛（任务失败，但 failure_category 已带好）
    注意：node_guard 不做降级——降级是节点自己的业务决策（捕获后返回 partial 即可，
    不抛异常就轮不到 guard）。guard 管的是「节点没能自己处理」的漏网异常。

    签名按被包节点动态适配：LangGraph 靠「函数有几个参数」决定要不要传 config，
    wrapper 写死单参会把 config 弄丢（实测 TypeError: missing 1 required
    positional argument: 'config'，且被本模块正确分类成 internal_error）。
    """
    def decorator(fn: Callable) -> Callable:
        takes_config = len(inspect.signature(fn).parameters) >= 2

        # 注解必须是 RunnableConfig：LangGraph 靠注解识别「第二个参数是 config」，
        # 写成 dict | None 它就不注入（实测 config 一直是 None）
        async def wrapper(state: dict, config: RunnableConfig | None = None) -> dict:
            # span 记录（docs/03 §5「节点包装器自动生成 Agent span」）：
            # 执行层把 spans 列表放进 config["configurable"]，guard 就地补一条；
            # 没人放（scratch 演示）就零开销跳过。重试的每次尝试各记一条，
            # 落库后 Trace 页能看到「失败了两次、第三次成功」的完整过程。
            spans = None
            ctx = None
            if config is not None:
                configurable = config.get("configurable") or {}
                spans = configurable.get("spans")
                ctx = configurable.get("tool_context")
            # 每次尝试在进入节点前就发一个 span 身份：agent_runs.span_id 是 unique 列，
            # 重试的两条行各有各的 id；而本次尝试里产出的工具记录要指向「这一次」，
            # 末尾落库时才不至于把所有记录都糊到同一个父上（tool_calls.parent_span_id 的源头）。
            span_id = uuid.uuid4()
            # 进前值要留着恢复：Phase 7 把 Phase 5 的 Supervisor 图当子图挂过，guard 会嵌套，
            # 不恢复就把外层节点的工具算到内层头上。
            prev_span_id = getattr(ctx, "current_span_id", None)
            if ctx is not None:
                ctx.current_span_id = span_id
            started_at = datetime.now(timezone.utc)
            t0 = time.monotonic()

            status = "ok"
            error_message: str | None = None
            result: dict | None = None
            try:
                if takes_config:
                    result = await fn(state, config)
                else:
                    result = await fn(state)
                return result
            except TaskNodeError as exc:
                status, error_message = "error", str(exc)
                raise
            except Exception as exc:  # noqa: BLE001 —— 兜底层，分类后转译
                category = classify_exception(exc)
                status, error_message = "error", f"{category}: {exc}"
                logger.error("节点 %s 失败（%s）: %s", node_name, category, exc)
                raise TaskNodeError(category, str(exc), node_name) from exc
            finally:
                if ctx is not None:
                    ctx.current_span_id = prev_span_id
                # 用 finally 而不是在各 except 里记：成功路径也要记 span
                if spans is not None:
                    # token 记「本次尝试增量」而非 meta 累计值：节点写的是 meta=旧值+本轮，
                    # 故 (返回后 meta − 进入前 meta) = 本节点本轮用量。重试各尝试各算各的，
                    # 落库逐 span 成行后相加才不会三角式虚高（Phase 5 审查发现的真 bug）。
                    prompt_tokens, completion_tokens = _token_delta(state, result)
                    spans.append(
                        {
                            # 落库侧（_persist_agent_runs）按这个 id 写 agent_runs.span_id，
                            # 工具行的 parent_span_id 也认它 —— span 与 tool 的父子关系就此对齐。
                            "span_id": span_id,
                            "node": node_name,
                            "status": status,
                            "started_at": started_at,
                            "finished_at": datetime.now(timezone.utc),
                            "duration_ms": int((time.monotonic() - t0) * 1000),
                            "error_message": error_message,
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                        }
                    )

        # 让 LangGraph 看到的函数名不变（tracing / 日志可读性）
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


def apply_retry(add_node: Callable, name: str, fn: Callable, **kwargs: Any) -> None:
    """graph.add_node 的薄包装：按 RETRYABLE_NODES 自动挂 RetryPolicy，注册点不再散落配置。

    参数名用 retry_policy（1.2 正名）；旧的 retry= 已废弃且只会触发弃用警告。
    """
    if name in RETRYABLE_NODES:
        kwargs.setdefault("retry_policy", RETRYABLE_NODES[name])
    add_node(name, fn, **kwargs)
