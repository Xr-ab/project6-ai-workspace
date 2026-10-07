"""Supervisor 节点（Phase 5 工程版）：真调模型产出执行计划 plan。

对比 scratch/supervisor_min.py 的写死版，这里只把「产出 plan」那一步做实：
    写死的 dict  ──▶  astructured(SupervisorPlan) 让模型按 schema 吐 JSON

职责边界（docs/03-agent-design.md §2.1）：Supervisor 只「判断问题类型 + 出计划」，
绝不亲自分析数据。它的产出是控制流数据（plan），供 router 翻译成图的去向。

为什么 plan 用 Pydantic 约束（§4 + structured_output.py 的理由）：
    plan 要被 router 用代码消费（读 task_breakdown[i].agent），不能是散文。
    agent 字段用 Literal 锁死取值，router 才能信任它返回的字符串一定是已注册节点名，
    否则模型吐个 "sql_bot" 进来，路由直接 unknown node 崩图。

失败怎么办（§5「节点内捕获 → 不中断整个图」）：
    astructured 内部已重试 3 次仍拿不到合法 plan → 不抛异常崩任务，降级成
    「只派 data_analyst」的最小计划，让任务还能往下跑出个结果，
    同时把降级原因写进 meta，供 Trace / Evaluation 事后看到「这次是兜底跑的」。
"""
from copy import deepcopy
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from app.ai.graph.errors import node_guard
from app.ai.graph.memory import SUPERVISOR_INCLUDE, render_block
from app.ai.graph.state import TaskState
from app.ai.prompts import SUPERVISOR_PROMPT
from app.ai.structured_output import astructured


class SubTask(BaseModel):
    # Literal 锁死可选 agent：这就是「router 能信任返回值」的根因
    agent: Literal["data_analyst", "research"]
    description: str = Field(description="这个子任务要做什么，一句话说清")


class SupervisorPlan(BaseModel):
    task_breakdown: list[SubTask] = Field(description="按执行顺序排列的子任务列表")
    rationale: str = Field(description="为什么这么拆，一句话")


# 兜底计划：模型连续失败时至少让数据分析师跑一遍，任务不至于直接崩
_FALLBACK_PLAN = {
    "task_breakdown": [{"agent": "data_analyst", "description": "查询与问题相关的数据"}],
    "rationale": "Supervisor 未能产出有效计划，降级为只走数据分析",
}


@node_guard("supervisor")
async def supervisor(state: TaskState, config: RunnableConfig) -> dict:
    # 记忆只追加进 System（§5）：本轮问题仍是 HumanMessage —— 两者混了会出
    # "模型拿上一轮问题当本轮问题"的哑 bug，而 review 只判本轮答没答对，抓不到它。
    sys_prompt = SUPERVISOR_PROMPT + render_block(
        state.get("memory_context"), include=SUPERVISOR_INCLUDE
    )
    messages = [
        SystemMessage(content=sys_prompt),
        HumanMessage(content=state["question"]),
    ]
    result = await astructured(messages, SupervisorPlan)

    meta = dict(state.get("meta") or {})
    meta["prompt_tokens"] = meta.get("prompt_tokens", 0) + result.prompt_tokens
    meta["completion_tokens"] = meta.get("completion_tokens", 0) + result.completion_tokens

    if result.ok:
        plan = result.value.model_dump()
        agents = [t["agent"] for t in plan["task_breakdown"]]
        note = f"supervisor 规划：{agents}"
    else:
        # deepcopy：dict() 只抄顶层，内层 task_breakdown 列表会和本模块常量
        # 共用一份——plan 是进 state 的请求态数据，将来谁在原位改一笔
        # （补派、去重），就污染了进程级常量、串到所有并发请求。
        plan = deepcopy(_FALLBACK_PLAN)
        # 累积进 errors 列表（不是单键 meta["error"]）：多个节点各自降级时原因不被后写覆盖
        meta["errors"] = [*(meta.get("errors") or []), f"supervisor 结构化失败，已降级：{result.error}"]
        note = "supervisor 降级为最小计划（data_analyst）"

    return {
        "plan": plan,
        "messages": [note],
        "meta": meta,
    }
