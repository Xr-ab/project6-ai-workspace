"""add evaluation tables

Revision ID: ef987cd499bb
Revises: a29c57d8e0fd
Create Date: 2026-09-25 18:46:46.739914

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision: str = 'ef987cd499bb'
down_revision: Union[str, Sequence[str], None] = 'a29c57d8e0fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "evaluation_datasets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("category_scope", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    # 与模型 __table_args__ 保持纯 ASC 组合（仓内先例 ix_tasks_org_created 即 ASC；
    # docs/ 从未规定 DESC，计划文本里的 DESC 系笔误）
    op.create_index("ix_eval_datasets_org_created", "evaluation_datasets",
                    ["organization_id", "created_at"])

    op.create_table(
        "evaluation_cases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("dataset_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("expected", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("references", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("judgement", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("tags", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("dataset_id", "code"),
    )
    op.create_index("ix_eval_cases_dataset", "evaluation_cases", ["dataset_id"])

    op.create_table(
        "evaluation_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        # SET NULL：删数据集不该带走历史基线（回归对比的左半边）
        sa.Column("dataset_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_datasets.id", ondelete="SET NULL")),
        sa.Column("target", sa.String(50), server_default=sa.text("'agent'"), nullable=False),
        sa.Column("target_version", sa.String(100), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("progress", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("case_total", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("case_done", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("metrics", JSONB),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_eval_runs_org_created", "evaluation_runs",
                    ["organization_id", "created_at"])
    op.create_index("ix_eval_runs_status", "evaluation_runs", ["status"])

    op.create_table(
        "evaluation_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_cases.id", ondelete="SET NULL")),
        sa.Column("case_no", sa.Integer, nullable=False),
        sa.Column("case_snapshot", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("task_run_id", UUID(as_uuid=True), sa.ForeignKey("task_runs.id", ondelete="SET NULL")),
        sa.Column("trace_id", UUID(as_uuid=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("passed", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("score", sa.Integer),
        sa.Column("judge_source", sa.String(20), server_default=sa.text("'auto'"), nullable=False),
        sa.Column("reasons", JSONB),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("prompt_tokens", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("cost", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("failure_category", sa.String(50)),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("run_id", "case_no"),
    )
    op.create_index("ix_eval_results_run", "evaluation_results", ["run_id"])

    # task_runs 的评测标记列（docs/08 §5.2）。先建表再加列，避免自引用外键顺序问题。
    op.add_column("task_runs", sa.Column("run_type", sa.String(20),
                                         server_default=sa.text("'product'"), nullable=False))
    op.add_column("task_runs", sa.Column("evaluation_run_id", UUID(as_uuid=True)))
    op.create_index("ix_task_runs_run_type", "task_runs", ["run_type"])
    op.create_index("ix_task_runs_eval_run", "task_runs", ["evaluation_run_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_task_runs_eval_run", table_name="task_runs")
    op.drop_index("ix_task_runs_run_type", table_name="task_runs")
    op.drop_column("task_runs", "evaluation_run_id")
    op.drop_column("task_runs", "run_type")
    op.drop_table("evaluation_results")
    op.drop_table("evaluation_runs")
    op.drop_table("evaluation_cases")
    op.drop_table("evaluation_datasets")
