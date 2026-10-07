"""add GIN index on document_chunks.content_tsv

Hybrid Search 全文召回腿（docs/05 §6.2）落地时补 §8.2 承诺的 GIN 索引：
没有它 content_tsv @@ tsquery 走全表顺序扫描，"列建了、索引也写了 DDL、
但没人索引"是假完成。

Revision ID: c6f2e8507b31
Revises: ddd76d59686e
Create Date: 2026-09-25 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c6f2e8507b31'
down_revision: Union[str, Sequence[str], None] = 'ddd76d59686e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        'ix_document_chunks_content_tsv_gin',
        'document_chunks',
        ['content_tsv'],
        unique=False,
        postgresql_using='gin',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_document_chunks_content_tsv_gin', table_name='document_chunks')
