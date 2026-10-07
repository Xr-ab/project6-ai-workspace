"""报告业务逻辑（Phase 9b）：报告的归一读面 + 终态写入。

分三层，别混（docs/02 §4）：
    report_repo    只做数据读写（org/user 谓词在这里）
    report_service 本模块：形状判断、markdown 渲染、终态投影（业务判断在这里）
    api/reports.py 只取身份 → 调本模块 → 选响应模型

**本模块的核心裁定：报告是执行结论的投影，不是第二份真值。**
真值在 `task_runs.meta.report`（结构化）与 `task_runs.state.report`（白板）里；本模块
在终态落库时**从同一份结论拷一份**进 reports 表，只为了给出「列表 / 按 id 取 / 删除 /
回跳来源」这四种形状 —— `meta` 那种"挂在执行上的一个键"给不出这些形状，这正是
Phase 5 当时没建表的原因（`task_runner.py::_run_meta` 的注释）与 Phase 9b 现在建表的理由。
投影丢了可以重建（重跑/重投影），真值丢了统计就断了。

markdown 由**后端**渲染而不是让前端各自实现：消费者不止一个页面（报告详情页、
将来导出、评测材料），每个消费者各写一遍「字典 → Markdown」= 每个消费者各有一个
渲染口径，几个月后没人说得清哪份才是"报告的样子"。与 Trace 建树刻意放前端的裁定
（docs/09 §Phase 9a）方向相反，但理由不冲突：那边是**展示形状随 UI 演进**（折叠/高亮），
这边是**正文内容本身**必须先定下来（同一份报告导出成文件时不该与屏幕上的不一样）。
"""
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import pricing
from app.core.exceptions import ReportNotFoundError
from app.data.models import Report, Task, TaskRun
from app.data.repositories import report_repo, stats_repo

logger = logging.getLogger(__name__)

# report_type 取值（docs/05 §2 原 DDL 的注释：analysis / workflow / summary）。
# 与 task_type 不是一回事：task 说"这次执行走的哪个入口"，report 说"报告长什么样"。
REPORT_TYPES = frozenset({"analysis", "workflow", "summary"})

# 报告类型 ← task_type 的映射。dict 而不是 if/else：加一类任务时改这里一处，
# 且未知 task_type 有明确的降落点（见 report_type_for_task）。
_REPORT_TYPE_BY_TASK_TYPE = {
    "agent_analysis": "analysis",
    "workflow": "workflow",
    "document_summary": "summary",
}

# 字段 → 中文标签 + 展示顺序。**与产品模型 Report 的字段声明序一字不差**，
# 由 `tests/unit/test_report_layer.py:122` 同进程钉住（产品加字段 → 针红）。此处从前引的是 `test_report_markdown.py`——本仓无此文件，2026-10-03 勘正。
#
# 这份表在后端是权威（前端 `lib/taskReport.ts` 的渲染表是它的镜像，另有契约针
# `test_report_render_contract.py` 双向对账）。标签文案与 docs/03 §2.6 的七字段、
# `REPORT_PROMPT` 第 1 条那串中文逐字对齐，别各写各的。
FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("executive_summary", "执行摘要"),
    ("key_findings", "核心发现"),
    ("data_evidence", "数据依据"),
    ("root_causes", "原因分析"),
    ("risks", "风险提示"),
    ("recommendations", "可执行建议"),
    ("sources", "来源"),
)

# 降级支路（`nodes/report.py:59`）塞进来的第二个散文键：与 executive_summary 是同一段
# analysis 原文，渲染时不重复印一遍（只在两值真不同时才当独立一段）。
DEGRADED_CONTENT_KEY = "content"


def report_type_for_task(task: Task, task_run: TaskRun | None = None) -> str:
    """task → report_type。认不出的 task_type 一律落 `analysis`。

    为什么给未知值一个降落点而不是抛错：报告是**末端产物**，走到写报告这一步时
    执行已经成功结束，为一个分类标签把已经跑完的报告丢掉是本末倒置。
    未知类型落 `analysis`（最宽的那一类）并在日志留一行，比丢掉它诚实。
    """
    mapped = _REPORT_TYPE_BY_TASK_TYPE.get(task.task_type)
    if mapped is not None:
        return mapped
    logger.warning("未知 task_type=%r 的报告归入 analysis（task=%s）", task.task_type, task.id)
    return "analysis"


def title_for_task(task: Task, limit: int = 200) -> str:
    """报告标题：任务标题优先，退回问题原文（截断到 limit，与列宽一致）。

    为什么不用"报告 #<id 前 8 位>"这类生成名：列表页要的是"这份报告在讲什么"，
    生成名把唯一有用的信息（问题）换成了一个不可读的编号。
    """
    raw = (task.title or "").strip() or task.question.strip()
    return (raw[: limit - 1] + "…") if len(raw) > limit else raw


def render_markdown(content: dict | None) -> str | None:
    """结构化报告 → Markdown 全文。认不出的形状返回 None（不猜、不硬凑）。

    两种产品形状都能渲：
      - 结构化七字段（`nodes/report.py:55` 的 `Report.model_dump()`）
      - 降级支路 `{executive_summary, content}`（`report.py:59`）

    认不出的形状（不是 dict / 空 dict / 全是空值）返回 None：调用方照实把 `markdown`
    写成 NULL，前端退回"按结构化字段渲染"那条路 —— 两个渲染面并存好过编一份假 markdown。
    """
    if not isinstance(content, dict) or not content:
        return None

    blocks: list[str] = []
    summary = _non_empty_str(content.get("executive_summary"))
    if summary:
        # 摘要不给自己加标题：它就是开篇第一段，加个「## 执行摘要」反而像正文被切了一刀。
        # 与前端 ReportView 的排版口径一致（摘要置顶、不重复标签）。
        blocks.append(summary)

    for key, label in FIELD_LABELS:
        if key == "executive_summary":
            continue
        value = content.get(key)
        rendered = _render_field(value)
        if rendered:
            blocks.append(f"## {label}\n\n{rendered}")

    # 降级支路的重复原文：只在与摘要不同的时候才另起一段（相同则已印过一遍）
    fallback = _non_empty_str(content.get(DEGRADED_CONTENT_KEY))
    if fallback and fallback != summary:
        blocks.append(f"## 报告原文\n\n{fallback}")

    # 认不出的字段**不丢**：排在已知字段之后，用键名当标题。
    # 理由与前端同一条：产品加字段时，旧代码是"少标一个标签"，不是"少显示一段正文"。
    known = {key for key, _ in FIELD_LABELS} | {DEGRADED_CONTENT_KEY, "executive_summary", "summary"}
    for key, value in content.items():
        if key in known:
            continue
        rendered = _render_field(value)
        if rendered:
            blocks.append(f"## {key}\n\n{rendered}")

    # 复核未通过却被放行（`report.py:68`）时挂的警告：与正文分开、显式标注用途
    warnings = _warnings(content)
    if warnings:
        listed = "\n".join(f"- {item}" for item in warnings)
        blocks.append(f"## 复核警告\n\n{listed}")

    if not blocks:
        return None
    return "\n\n".join(blocks) + "\n"


def _non_empty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _warnings(content: dict) -> list[str]:
    """`review_warning`（产品真发的键）与 `review_warnings`（复数，容忍）都收，字符串也收。"""
    out: list[str] = []
    for key in ("review_warning", "review_warnings"):
        value = content.get(key)
        if isinstance(value, str):
            if value.strip():
                out.append(value.strip())
        elif isinstance(value, list):
            out.extend(str(item).strip() for item in value if str(item).strip())
    return out


def _render_field(value: object) -> str | None:
    """一个字段值 → Markdown 片段。字符串原样、列表逐条 `- `、其余 JSON 串化。"""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(f"- {item}" for item in items) if items else None
    if value is None:
        return None
    # 数字 / 嵌套对象：**有内容就不许当空丢掉**（认不出 ≠ 没有）
    return f"```json\n{_json_dumps(value)}\n```"


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _token_totals(task_run: TaskRun) -> tuple[int, float]:
    """这次执行的 token 合计与成本（报告行上的两个计数列）。

    口径与 `_persist_agent_runs` 同一份数据源：`task_run.meta` 里
    `prompt_tokens` / `completion_tokens` 是 node_guard 逐节点累加出来的整轮合计。
    成本用 `pricing.compute_cost` 现算而不是从 agent_runs 求和：
    后者要再查一次库，且逐行 `cost` 是四舍五入到 6 位的近似值，求和的误差比整体算一次大。
    未配置单价时 `compute_cost` 返回 None ⇒ 写 0（这列 NOT NULL，口径同 agent_runs.cost）。
    """
    meta = task_run.meta if isinstance(task_run.meta, dict) else {}
    prompt = int(meta.get("prompt_tokens") or 0)
    completion = int(meta.get("completion_tokens") or 0)
    cost = pricing.compute_cost(prompt, completion) or 0
    return prompt + completion, float(cost)


async def write_for_terminal_run(
    session: AsyncSession, *, task: Task, task_run: TaskRun
) -> Report | None:
    """终态落库时把这位 run 的报告投影成 reports 表的一行（没报告就返回 None）。

    调用点在 `task_runner.run_task` 的成功支路（写完 `task_run.meta` 之后、
    `_settle_terminal` 那一拍的 commit 之前）—— **与执行结论同事务**：
    报告行和它引用的执行行要么一起在、要么一起不在，不留"执行完成了但报告没影子"。

    幂等：先按 `task_run_id` 查一次，已有行就**原地更新**而不是再插一行。
    为什么需要幂等（worker 崩溃后 sweeper 会对账）：
    `run_task` 的收尾段可能被重放（sweeper 重入队 / 手工补投），没有这道闸就会
    给同一次执行长出两份报告。**注意这不改变"最多一次执行"的口径**（那是幂等门的事），
    只是不让投影层自己长重复行。

    只写 `completed` 且真有报告的执行：失败/驳回的执行没有可交付的正文，
    给它建一行空报告会让 `/reports` 列表里混进"点开什么都没有"的条目 ——
    failed 执行的事实已经完整落在 task_runs（状态/失败分类/Trace），不需要在报告表里再记一遍。
    """
    meta = task_run.meta if isinstance(task_run.meta, dict) else {}
    content = meta.get("report")
    if not isinstance(content, dict) or not content:
        return None

    existing = await report_repo.get_by_task_run(
        session, task_run_id=task_run.id, organization_id=task.organization_id
    )
    total_tokens, cost = _token_totals(task_run)
    verdict = meta.get("reviewer_verdict")
    markdown = render_markdown(content)

    if existing is not None:
        existing.title = title_for_task(task)
        existing.report_type = report_type_for_task(task, task_run)
        existing.content = content
        existing.markdown = markdown
        existing.reviewer_verdict = verdict if isinstance(verdict, str) else None
        existing.total_tokens = total_tokens
        existing.cost = cost
        report = existing
    else:
        report = Report(
            organization_id=task.organization_id,
            user_id=task.user_id,
            task_id=task.id,
            task_run_id=task_run.id,
            title=title_for_task(task),
            report_type=report_type_for_task(task, task_run),
            content=content,
            markdown=markdown,
            status="final",
            reviewer_verdict=verdict if isinstance(verdict, str) else None,
            total_tokens=total_tokens,
            cost=cost,
        )
        session.add(report)

    # tasks.report_id 指向**最新一份**（Phase 4 建了这一列，Phase 9b 才第一次有写入方）。
    # flush 而不是 commit：id 要现在拿到（客户端默认 uuid4，其实 flush 前就有值，
    # 但 flush 保证这一行真的排进了同一事务的 insert 序列）。
    await session.flush()
    task.report_id = report.id
    return report


async def list_reports(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
    report_type: str | None,
    limit: int,
    offset: int,
    range_key: str = "all",
) -> tuple[list[Report], int]:
    """列表 + 总数（同一次调用保证两条谓词同源，见 report_repo 的说明）。

    时间窗在这里算（`range_key` → `since`）而不是在路由里：路由只认 HTTP 形状，
    "近 7 天从哪一刻起"是业务口径。口径本身不自建，直接取 `stats_repo.range_start`
    —— 与 stats 两个端点同一个函数、同一条滚动窗裁定（week/month = 滚动 7/30 天、
    today = 本地日 00:00）。**全仓只许有这一份时间窗语义**：两处各写一遍，
    同一个"近 7 天"就会在两个页面上是两个意思，而对不上数的人怀疑的是系统。

    `now` 传带 tz 的当前时间（`range_start` 的 docstring 写明：列是 timestamptz，
    naive datetime 会被 PG 按会话时区猜，`today` 的边界能差一天）。
    """
    since = stats_repo.range_start(range_key, now=datetime.now().astimezone())
    kwargs = {
        "organization_id": organization_id,
        "user_id": user_id,
        "report_type": report_type,
        "since": since,
    }
    rows = await report_repo.list_reports(session, limit=limit, offset=offset, **kwargs)
    total = await report_repo.count_reports(session, **kwargs)
    return rows, total


async def get_visible_report(
    session: AsyncSession,
    *,
    report_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> Report:
    """取一条可见的报告，取不到抛同形 404（`REPORT_404001`）。

    "跨 org / 跨 user / 真不存在"三种情况在这里**合并成同一个异常**：能分辨就等于
    允许靠状态码探测别人有什么（全仓 404 同形闸口径，docs/06 §5 那条）。
    """
    report = await report_repo.get_report(
        session, report_id=report_id, organization_id=organization_id, user_id=user_id
    )
    if report is None:
        raise ReportNotFoundError()
    return report


async def delete_report(session: AsyncSession, report: Report) -> None:
    """删报告（调用方已取到可见行）。commit 归调用方（路由层不半提交）。"""
    await report_repo.delete_report(session, report)
