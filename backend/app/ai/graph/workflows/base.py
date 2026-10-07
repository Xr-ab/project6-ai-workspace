"""Workflow 图基座（Phase 7）：三条 Workflow 共用的 state / 审批常量 / 审批路由。

与 TaskState（app/ai/graph/state.py）的关系：白板字段不同（这里是「产物+审批」
而非「查数+回环」），但 meta 的浅合并 reducer 直接复用，不重定义第二份。

审批机制口径（spec §6 勘误 + 计划 Task 6 裁定，绑定实现）：
    静态断点 interrupt_before=["approval"] + aupdate_state({"approval_decision":…})
    （不填 as_node）+ astream(None, config) 续跑；决策经 route_after_approval 纯函数分岔。
"""
from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig

from app.ai.graph.errors import node_guard
from app.ai.graph.state import merge_dict

# 审批节点名——三张图共用同一个名字：interrupt_before=["approval"]、
# WorkflowApproval.graph_node、前端审批区识别都锚这一个常量，别各处写裸字符串。
APPROVAL_NODE = "approval"


class WorkflowState(TypedDict, total=False):
    """Workflow 图共享白板。total=False：各节点只写自己那一格，入口只给 workflow_input。"""

    workflow_input: dict          # 触发入参（input_spec 校验后的原样 dict）
    draft: str                    # 产物演进位：doc_summary 里 retrieve 铺原料、summarize 覆写为摘要
    approval_decision: str        # "approved" / "rejected"，由审批引擎在断点处写入
    result: dict | None           # 终态产物；reject_end 显式置 None（Task 6 靠「reject 支路 result 空」判 rejected）
    meta: Annotated[dict, merge_dict]  # trace / token / 检索命中数等记账，多节点各写不同子键（浅合并）


def route_after_approval(state) -> str:
    """审批后的道岔。**anything != "approved" → "rejected"**——包括键缺失。

    未决策兜到拒绝侧（宁拒不放）：断点被绕过、引擎忘了写决策，都出不了成品；
    放过去是假通过，拦下来最多多一次人工复核。纯函数，返回串必须是 path_map 键。
    """
    return "rejected" if state.get("approval_decision") != "approved" else "deliver"


@node_guard(APPROVAL_NODE)
async def approval_node(state: WorkflowState, config: RunnableConfig) -> dict:
    """审批占位节点：本体不做事（无 LLM、无落库），价值全在「站在断点上」。

    interrupt_before=["approval"] 让图在它前面停下并把状态落进 saver；
    决策由引擎（Task 6 workflow_service）写进 state 后 resume，本节点被执行时
    只是「已过闸」的通过标记，随后 route_after_approval 分岔。
    循环节点不能当断点的旧坑（docs/10:145）对它不成立——它是非循环单次节点。
    """
    return {}
