"""add workflows

Revision ID: 5ea259c3bdac
Revises: ef987cd499bb
Create Date: 2026-09-26 13:14:53.232215

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision: str = '5ea259c3bdac'
down_revision: Union[str, Sequence[str], None] = 'ef987cd499bb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ---- workflows：Phase 7 预置/自定义 workflow 的目录表 ----
    op.create_table(
        "workflows",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(100)),
        sa.Column("description", sa.Text()),
        # graph_key 是图的稳定标识（注册表键），全局唯一 —— 预置三条靠它做幂等种子
        sa.Column("graph_key", sa.String(50), nullable=False),
        sa.Column("input_spec", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("graph_key"),
    )
    op.create_index("ix_workflows_org", "workflows", ["organization_id"])

    # ---- workflow_approvals：审批门（interrupt 挂起 / 恢复的落库凭据）----
    op.create_table(
        "workflow_approvals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # CASCADE：审批记录依附于任务，删任务即删审批痕迹（区别于 workflows FK 不级联的取舍）
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("graph_node", sa.String(50), nullable=False),
        # pending / approved / rejected
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decided_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("comment", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_workflow_approvals_task", "workflow_approvals", ["task_id", "status"])

    # ---- tasks：心跳列 + workflow_id 补外键 ----
    op.add_column("tasks", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    # 可空性不变、不收紧（Phase 4 起该列恒 NULL，加 FK 不影响存量）；
    # FK 不带 cascade：删编目不牵连历史任务
    op.create_foreign_key("fk_tasks_workflow_id", "tasks", "workflows", ["workflow_id"], ["id"])

    # ---- 种 3 条预置 workflow（dev org，固定 UUID 见 a14ff0f34d8f 种子先例）----
    # ON CONFLICT (graph_key) DO NOTHING：重放/重复执行安全
    op.execute(
        """
        INSERT INTO workflows (organization_id, name, graph_key, input_spec) VALUES
            ('00000000-0000-0000-0000-000000000001', '销售数据分析报告', 'sales_analysis', '{"question":"string"}'::jsonb),
            ('00000000-0000-0000-0000-000000000001', '业务问题问数', 'business_qa', '{"question":"string"}'::jsonb),
            ('00000000-0000-0000-0000-000000000001', '企业文档 RAG 总结', 'doc_summary', '{"document_id":"uuid","question":"string"}'::jsonb)
        ON CONFLICT (graph_key) DO NOTHING
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("fk_tasks_workflow_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "heartbeat_at")
    op.drop_index("ix_workflow_approvals_task", table_name="workflow_approvals")
    op.drop_table("workflow_approvals")
    op.drop_index("ix_workflows_org", table_name="workflows")
    op.drop_table("workflows")
