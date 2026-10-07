"""TaskState：查数→分析→报告 全图共享的白板（docs/03-agent-design.md §3）"""
import operator
from typing import Annotated, TypedDict

def merge_dict(left: dict, right: dict) -> dict:
    """dict 的 reducer：浅合并——新 dict 的键覆盖同键，其余键保留。

    operator.add 对 dict 直接 TypeError，所以 meta 要自定义一个。
    """
    return {**left, **right}

class TaskState(TypedDict):
    # ---- 单一写者、后写覆盖先写：默认覆盖，不加 reducer ----
    question: str                      # 用户原始问题
    plan: dict | None                  # supervisor 执行计划
    analysis: dict | None              # business_analyst 综合分析
    review: dict | None                # reviewer 结果 {verdict, reasons, retry_targets}
    retry_count: int                   # 重跑轮数
    report: dict | None                # 最终报告

    # ---- 单一写者、但写者是执行层不是任何节点：Phase 6 短期记忆 ----
    # 上一轮（及更早几轮）的压缩结论。白名单/分层/预算全在 memory.build_memory 里定，
    # 节点只读它、绝不写它 —— 一旦节点能写，"继承什么"就散落到六个节点里了（docs/11 §4.2）。
    # 它自己也进快照落库，但**不注入下一轮**（§4.1 末行：读上一轮的 memory_context 会套娃）。
    memory_context: dict | None

    # ---- 多节点往里添：Annotated[list, operator.add]，新值拼在旧值后面 ----
    data_results: Annotated[list, operator.add]      # data_analyst 每次查数一条
    research_results: Annotated[list, operator.add]  # research 每次检索一条
    messages: Annotated[list, operator.add]          # 图内消息记录

    # ---- 多节点各写不同子键：自定义浅合并 ----
    meta: Annotated[dict, merge_dict]  # trace_id / task_id / 耗时 / token / cost