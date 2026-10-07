"""add task_runs state snapshot

Revision ID: a29c57d8e0fd
Revises: c6f2e8507b31
Create Date: 2026-09-25 13:49:02.518468

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a29c57d8e0fd'
down_revision: Union[str, Sequence[str], None] = 'c6f2e8507b31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Phase 6 短期记忆（docs/11 §3.1）：只加一列，不加索引——
    # 读快照的查询是「按 task_id 取最近 N 条 completed run」，走已有的
    # UNIQUE (task_id, run_no) 索引；state 本身从不进 WHERE。
    op.add_column(
        'task_runs',
        sa.Column('state', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('task_runs', 'state')
