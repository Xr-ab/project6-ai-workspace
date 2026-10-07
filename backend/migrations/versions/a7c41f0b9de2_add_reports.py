"""add reports table + tasks.report_id FK (Phase 9b)

Revision ID: a7c41f0b9de2
Revises: 34334832cc2a
Create Date: 2026-10-03 16:20:00.000000

为什么这一版才有 reports 表：Phase 5 出口只要求「出报告 + Trace 可查」，报告正文当时
落 `task_runs.meta.report` 就够（`task_runner.py::_run_meta` 的注释即那次裁定）。
Phase 9 的 `/reports` 页要的是**独立报告资源**（列表 / 按 id 取 / 删除 / 回跳来源），
`meta` 那种"挂在执行上的一个键"给不出这些形状 ⇒ 本迁移建表，docs/05 §2 的那段 DDL 落地。

两处与 docs/05 原 DDL 的差异（都记在 docs/05 的落地补记里，以本文件为准）：
  1. `content` 的 `server_default '{}'::jsonb`（原 DDL 是 NOT NULL 无默认）——
     同表其余 JSONB 列的既有写法（evaluation_cases.expected 等），也让手工插行不必写它。
  2. 索引名按本仓惯例 `ix_reports_*`（原 DDL 只给了属性没给名字）。

顺带把 `tasks.report_id` 从裸 uuid 接成真外键（ON DELETE SET NULL）：那一列 Phase 4 就建了，
一直注释着「目标表还不存在，那期再加约束」（存量值全 NULL，不影响既有行）。
删报告不牵连任务、删任务不牵连报告 —— 报告是交付物，不该跟着执行痕迹一起没。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a7c41f0b9de2'
down_revision: Union[str, Sequence[str], None] = '34334832cc2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        # SET NULL（不是 CASCADE）：删任务/删执行痕迹不该把已经交付的报告带走 ——
        # 与 evaluation_results.task_run_id 同一条裁定。
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("report_type", sa.String(length=30), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'final'"), nullable=False),
        sa.Column("reviewer_verdict", sa.String(length=20), nullable=True),
        sa.Column("total_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=6), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    # 列表页主查询形态：本 org 按时间倒序分页（docs/05 §4）
    op.create_index("ix_reports_org_created", "reports", ["organization_id", sa.text("created_at DESC")])
    op.create_index("ix_reports_organization_id", "reports", ["organization_id"])
    # 来源钻取：从一条执行找它的报告
    op.create_index("ix_reports_task_run", "reports", ["task_run_id"])
    op.create_index("ix_reports_task", "reports", ["task_id"])

    # tasks.report_id：裸 uuid → 真外键（存量值全 NULL，加约束不会失败）
    op.create_foreign_key(
        "fk_tasks_report_id_reports", "tasks", "reports", ["report_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_report_id_reports", "tasks", type_="foreignkey")
    op.drop_index("ix_reports_task", table_name="reports")
    op.drop_index("ix_reports_task_run", table_name="reports")
    op.drop_index("ix_reports_organization_id", table_name="reports")
    op.drop_index("ix_reports_org_created", table_name="reports")
    op.drop_table("reports")
