"""报告 API（Phase 9b，docs/06 §2.5）：列表 / 详情 / 删除。

三条只读一删，**没有创建口**：报告的写入方是执行链路（`report_service
.write_for_terminal_run` 在终态落库时投影一行），不由任何人手工 POST。
为什么不开 `POST /reports`：一份没有来源执行的报告是**凭空造出来的交付物** ——
它没有 Trace、没有数据依据、没有复核结论，而本仓对"报告"的定义是"某次执行的可交付结论"。
真要做手工撰写的报告（例如周报），那是另一个资源，不该借这个口混进来。

分层薄同 chat/documents：路由只取身份 → 调 service → 选响应模型。
org / user 谓词全在仓储与 service（`report_service.get_visible_report`），
本层不自己拼查询 —— 隔离逻辑只许有一处（docs/02 §4）。
"""
import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.application import report_service
from app.data.db import get_db
from app.schemas.report import ReportDetailOut, ReportListOut, ReportOut
from app.schemas.stats import RangeKey

router = APIRouter(prefix="/reports", tags=["reports"])


def _report_scope(user) -> uuid.UUID | None:
    """报告可见性分档（与 documents 的 `_doc_scope` 同一条裁定，8b T10 R-T5b）。

    member → 本人 user_id（只看自己产出的报告；碰他人的 → 与不存在同形 404）；
    admin  → None = 本组织全量（审计页 admin 先例同款分档）。
    为什么不统一成"全组织可见"：报告正文里可能有业务数字，跨人可见是产品决定
    而不是实现细节；今天按最保守的那档走，放开是明确的一行改动（改这里一处）。
    """
    return None if user.role == "admin" else user.id


@router.get("", response_model=ReportListOut)
async def list_reports(
    user: CurrentUser,
    report_type: str | None = Query(None, description="analysis / workflow / summary"),
    range: RangeKey = Query(
        "all",
        description="时间窗（滚动窗，口径与 /stats 同一条；列表是交付物台账，默认全部）",
    ),
    # 上下界与全仓其他列表口一致（`ge=1` 是 8b 那条 bug 的教训：
    # 少了它 `?limit=-1` 会一路进 SQL LIMIT 由 PG 抛错 → 500，而契约承诺 4xx）
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> ReportListOut:
    """本组织报告列表，最近创建在前。

    `report_type` 是白名单外的值也不报错 —— 它只是过滤条件，认不出的类型
    自然筛出空列表（与 documents 无服务端过滤参数形成对比，见 docs/06 §7.2 的勘正）。
    真要卡白名单，也应该在 docs/06 §7.2「非法值 4xx」（`:456`）那条统一裁定之后一起加，不在这里单方面收紧。

    `range` 与上面那条不矛盾：它的合法值由 `RangeKey` 锁死（非法值 422），因为时间窗
    是**口径**而不是文本匹配 —— 猜错一个词就静默少一段正文的过滤，不该宽容。
    默认 `all`（stats 默认 `today`）：报告列表回答的是"我出过哪些报告"，
    默认当日会让它看上去是空的；那条差异由 OpenAPI 针钉住，改一边另一边红。
    """
    rows, total = await report_service.list_reports(
        session,
        organization_id=user.organization_id,
        user_id=_report_scope(user),
        report_type=report_type,
        limit=limit,
        offset=offset,
        range_key=range,
    )
    return ReportListOut(items=[ReportOut.model_validate(row) for row in rows], total=total)


@router.get("/{report_id}", response_model=ReportDetailOut)
async def get_report(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> ReportDetailOut:
    """报告详情：结构化 `content` + 渲染好的 `markdown` 都给（见 schema docstring）。

    跨 org / 跨 user / 真不存在 → `REPORT_404001` 逐字段同形（全仓 404 闸口径）。
    """
    report = await report_service.get_visible_report(
        session,
        report_id=report_id,
        organization_id=user.organization_id,
        user_id=_report_scope(user),
    )
    return ReportDetailOut.model_validate(report)


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> Response:
    """删除报告：**204 无 body**（与 conversation 删除口同形，docs/06 §2.2 补记 ③）。

    只删报告行：来源任务、执行痕迹、Trace 一行不动（报告是可重建的投影）。
    重复删除 → 第二次是 404（不是 204）：这一行真的不在了，回 204 等于谎报"删掉了"。
    """
    report = await report_service.get_visible_report(
        session,
        report_id=report_id,
        organization_id=user.organization_id,
        user_id=_report_scope(user),
    )
    await report_service.delete_report(session, report)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
