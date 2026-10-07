"""doc_summary（W3 企业文档 RAG 总结）：Phase 7 最短链路，先跑穿全图形态。

节点链（spec §7 五段 + reject_end 支线——宁拒不放，控制器裁定补正）：
    ingest → retrieve → summarize → approval(断点) → deliver / reject_end

复用的现成零件：
    ingest    = Phase 2 文档仓储 app.data.repositories.document_repo（org-scoped 取 Document 行）；
    retrieve  = Phase 2 混合检索入口 app.ai.rag.retriever.retrieve（hybrid：向量∪全文+RRF，
                knowledge_tools.rag_search 用的同一个），经 config 注入的 ToolContext session；
    summarize = llm_service.achat_stats（Phase 4 图节点的 token 口径来源）。

数据流走 WorkflowState 契约字段：ingest 做输入物化+就绪守卫（不存在/未就绪即判整条 run 失败，
不给坏输入去检索的机会）并铺 meta["document"] 标记；retrieve 把命中文本铺进 draft（草稿的第一作者），
summarize 覆写 draft 为摘要；命中清单/引用经 meta["retrieved"] 浅合并子键传递
（meta 的 reducer 就是为「多节点各写不同子键」设计的）。
"""
from __future__ import annotations

import uuid

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.ai.graph.errors import TaskNodeError, node_guard
from app.ai.graph.workflows.base import (
    APPROVAL_NODE,
    WorkflowState,
    approval_node,
    route_after_approval,
)
from app.ai.llm_service import llm_service
from app.ai.rag.retriever import retrieve as hybrid_retrieve
from app.data.repositories import document_repo

# summarize 的 prompt 起点（brief 钦定一句话口径；出口质量不靠 prompt 玄学，Task 11 实测再调）
SUMMARY_PROMPT = "请总结这份文档的要点。"

# 拼进 draft 的单块文本上限：与 knowledge_tools.CHUNK_TEXT_LIMIT 同值同理由——
# 防个别超长块（CSV 整表）吃掉 summarize 的 prompt 预算。
CHUNK_TEXT_LIMIT = 600

# Phase 2 文档就绪态：documents.status 流转 uploaded → parsing → chunking → embedding →
# ready | failed 的终态。ingest 的就绪守卫认这个值——非 ready（索引未完或已 failed）
# 一律挡在检索前，不让 retrieve 拿着空/坏向量做无效功（读自 models.Document.status 注释）。
DOC_READY_STATUS = "ready"


def _parse_document_id(raw, *, node: str = "retrieve") -> uuid.UUID:
    """workflow_input["document_id"] → UUID。非法值直接判定任务失败，不猜。

    node 参数化：ingest 复用同一份解析，但失败要挂在自己节点名下（TaskNodeError.node）。"""
    if isinstance(raw, uuid.UUID):
        return raw
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError) as exc:
        raise TaskNodeError("tool_failure", f"document_id 不合法：{raw!r}", node) from exc


@node_guard("ingest")
async def ingest(state: WorkflowState, config: RunnableConfig) -> dict:
    """输入物化 + 就绪守卫（spec §7 doc_summary 首节点）：把 document_id 落成可加工的文档。

    按 workflow_input["document_id"] 做 org-scoped 查询取 Document 行，校验存在且已就绪
    （status == DOC_READY_STATUS），再铺 meta["document"] 紧凑标记交给 retrieve。
    缺失/未就绪 → 显式 TaskNodeError("tool_failure", …, "ingest")，按图既有约定
    （Phase 4/5：node_guard 捕到即上抛 → ainvoke 抛出、执行层落 failed + failure_category）
    终止整条 run。session/org 仍从 config 注入的 ToolContext 取，绝不从 state 取身份。
    """
    ctx = config["configurable"]["tool_context"]
    params = state.get("workflow_input") or {}
    document_id = _parse_document_id(params.get("document_id"), node="ingest")

    doc = await document_repo.get_document(
        ctx.session, document_id=document_id, organization_id=ctx.organization_id
    )
    if doc is None:
        raise TaskNodeError("tool_failure", f"文档 {document_id} 不存在或不属于当前组织", "ingest")
    if doc.status != DOC_READY_STATUS:
        raise TaskNodeError(
            "tool_failure",
            f"文档 {document_id} 尚未就绪（status={doc.status!r}，需 {DOC_READY_STATUS!r}）",
            "ingest",
        )

    meta = dict(state.get("meta") or {})
    # Document 无独立 title 列，filename 即展示名（Phase 2 口径），title 键按 spec 语义填它
    meta["document"] = {"document_id": str(doc.id), "title": doc.filename}
    return {"meta": meta}


@node_guard("retrieve")
async def retrieve(state: WorkflowState, config: RunnableConfig) -> dict:
    """按触发入参在指定文档内做混合检索，命中铺成 draft 原料 + meta["retrieved"] 引用清单。

    运行依赖全部来自 config 注入的 ToolContext（session / org），绝不从 state 取身份——
    state 里的东西理论上都可能被用户/模型影响，越权就从混用开始（tools/base.py 同一铁律）。
    """
    ctx = config["configurable"]["tool_context"]
    params = state.get("workflow_input") or {}
    document_id = _parse_document_id(params.get("document_id"))
    question = str(params.get("question") or "").strip()
    if not question:
        raise TaskNodeError("tool_failure", "workflow_input.question 不能为空", "retrieve")

    # 先验文档存在且属于本组织：删掉的文档检索只会静默返回 0 命中，
    # 归不成「文档不存在」的人话错误；Task 10 出口要用坏 document_id 真触发 failed。
    doc = await document_repo.get_document(
        ctx.session, document_id=document_id, organization_id=ctx.organization_id
    )
    if doc is None:
        raise TaskNodeError("tool_failure", f"文档 {document_id} 不存在或不属于当前组织", "retrieve")

    # SAVEPOINT 口径与 knowledge_tools.rag_search 一致：查询炸了不把共享事务拖进 aborted。
    # document_ids 过滤 = 把「全库问答」收窄成「总结这一份」。
    async with ctx.session.begin_nested():
        hits = await hybrid_retrieve(
            ctx.session,
            organization_id=ctx.organization_id,
            query=question,
            document_ids=[document_id],
            # 单文档总结显式豁免噪声门（2026-10-03 修复波）：0.45 阈值是给「全库问答」
            # 拦弱相关块的，判据是「片段像不像提问」；本图的提问常是元问题
            # （「用三句话总结这份文档」），与任何片段的余弦都低（company_report.md
            # 实测 top1=0.4038），中文问句还叠上全文腿不切中文（repo 的 'simple' 配置），
            # 两腿皆空 ⇒ retrieve 误判「没有可检索的内容」把整条 run 判 failed。
            # 这里范围已被 document_ids 收窄到用户钦点、ingest 验过 ready 的**单份文档**——
            # 文档内不存在「跨文档噪声」，块块都是总结材料，故传 0.0 关掉这道门。
            # 聊天面（知识库问答）语义不同：答不上可以不给引用，gate 保持默认 0.45 不动。
            min_score=0.0,
        )
    if not hits:
        raise TaskNodeError("tool_failure", f"文档 {document_id} 没有可检索的内容", "retrieve")

    material = "\n\n".join(
        f"[{h.filename}{f' p{h.page}' if h.page else ''}] {h.content[:CHUNK_TEXT_LIMIT]}"
        for h in hits
    )
    meta = dict(state.get("meta") or {})
    meta["retrieved"] = {
        "chunks": len(hits),
        "document_id": str(document_id),
        "hits": [
            {"document_id": str(h.document_id), "filename": h.filename,
             "page": h.page, "score": round(h.score, 3)}
            for h in hits
        ],
    }
    # draft 的第一作者：retrieve 铺原料，summarize 覆写成摘要（单一写者链，无并写）
    return {"draft": material, "meta": meta}


@node_guard("summarize")
async def summarize(state: WorkflowState, config: RunnableConfig) -> dict:
    """draft 原料 + 用户问句 → llm_service.achat_stats → 覆写 draft 为摘要。

    token 记账口径与 Phase 5 节点相同：meta 写「旧值+本轮」的覆盖型累计，
    node_guard 的 _token_delta 靠前后差算本节点 span 用量。
    """
    material = state.get("draft") or ""
    question = str((state.get("workflow_input") or {}).get("question") or "")
    messages = [
        SystemMessage(content=SUMMARY_PROMPT),
        HumanMessage(content=f"用户问题：{question}\n\n文档片段（检索命中）：\n{material}"),
    ]
    text, prompt_tokens, completion_tokens = await llm_service.achat_stats(messages)

    meta = dict(state.get("meta") or {})
    meta["prompt_tokens"] = meta.get("prompt_tokens", 0) + prompt_tokens
    meta["completion_tokens"] = meta.get("completion_tokens", 0) + completion_tokens
    return {"draft": text, "meta": meta}


@node_guard("deliver")
async def deliver(state: WorkflowState, config: RunnableConfig) -> dict:
    """审批放行后的成品装配：摘要 + 来源引用 → result（终态产物，Task 6 落 TaskRun.meta）。"""
    hits = ((state.get("meta") or {}).get("retrieved") or {}).get("hits") or []
    return {"result": {"summary": state.get("draft") or "", "sources": hits}}


@node_guard("reject_end")
async def reject_end(state: WorkflowState, config: RunnableConfig) -> dict:
    """拒绝支线的终点：显式 result=None。

    拒绝是人为决定不是链路失败（spec §6：不进失败分类表）；Task 6 按
    「reject 支路且 result 为空」把 Task.status 落 rejected。

    同步备忘：三图同款（doc_summary / sales_analysis / business_qa 各复制一份，
    spec §2-5「每图独立定义」裁定），改一处看三处。
    """
    return {"result": None}


def build(*, checkpointer) -> CompiledStateGraph:
    """编译 doc_summary 图。checkpointer 由注册表统一注入（Task 4 单例）。

    interrupt_before=["approval"]：静态断点，图停在审批节点**之前**，
    状态已由 saver 落 Postgres——进程重启后按 thread_id 从这里续跑（§2 裁定 1）。
    """
    g = StateGraph(WorkflowState)
    g.add_node("ingest", ingest)
    g.add_node("retrieve", retrieve)
    g.add_node("summarize", summarize)
    g.add_node(APPROVAL_NODE, approval_node)
    g.add_node("deliver", deliver)
    g.add_node("reject_end", reject_end)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "retrieve")
    g.add_edge("retrieve", "summarize")
    g.add_edge("summarize", APPROVAL_NODE)
    # path_map 键 = route_after_approval 的返回串（"deliver"/"rejected"），值 = 目标节点
    g.add_conditional_edges(APPROVAL_NODE, route_after_approval, {
        "deliver": "deliver",
        "rejected": "reject_end",
    })
    g.add_edge("deliver", END)
    g.add_edge("reject_end", END)

    return g.compile(checkpointer=checkpointer, interrupt_before=[APPROVAL_NODE])
