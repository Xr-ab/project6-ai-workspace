"""RK1：11 个迁移在**全新空库**上跑穿（本机库是十几个阶段增量演化来的，从没证明过这点）。

"全新"由 docker/run_integration_local.sh 负责：它在 up 之前先做一次守卫式 down -v，
所以每次运行面对的都是空卷。CI 上 service container 本身就是每次新建。
alembic upgrade head 本身由 conftest 的 migrated_schema 夹具跑（session 一次）；
本文件断言的是**结果**——把"跑穿了"这件事留在它该在的文件里，而不是埋在夹具里。

计数按 `ls migrations/versions/*.py` 实测（Phase 9b 的 `a7c41f0b9de2` 是第 11 个；
原写 10 是 9b 之前数的，2026-10-03 留白收口波对上）。
"""
import os
import subprocess
import sys

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db


async def test_alembic_version_row_is_exactly_one(db_session):
    """version_num 必须只有一行：多行 = 出现分叉头，下一次 upgrade 会 AmbiguousRevisionError。"""
    rows = (await db_session.execute(text("SELECT version_num FROM alembic_version"))).all()
    assert len(rows) == 1, [r[0] for r in rows]
    # 实读 migrations/versions/ 的 11 个文件名：revision 全是 12 位 hex
    assert len(rows[0][0]) == 12, rows[0][0]


async def test_extension_and_vector_index_landed(db_session):
    """pgvector 扩展由**第一个**迁移建（a14ff0f34d8f 里 CREATE EXTENSION IF NOT EXISTS vector），
    HNSW 索引由 documents 那期建 —— 两个都在 ⇒ 链不是只跑了第一步。"""
    ext = await db_session.scalar(
        text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    )
    assert ext == "vector"
    idx = await db_session.scalar(
        text("SELECT indexname FROM pg_indexes "
             "WHERE tablename = 'document_chunks' "
             "AND indexname = 'ix_document_chunks_embedding_hnsw'")
    )
    assert idx == "ix_document_chunks_embedding_hnsw"


@pytest.mark.parametrize(
    "table_name,column_name",
    [
        # 各期各自加的一列，按时间顺序抽五点：任何一环掉队这里就红
        ("organizations", "plan"),            # Phase 1 初表
        ("tasks", "heartbeat_at"),            # Phase 7 迁移 5ea259c3bdac
        ("task_runs", "run_type"),            # Phase 6 评测标记列
        ("audit_logs", "action"),             # Phase 8a
        ("reports", "reviewer_verdict"),      # Phase 9b 迁移 a7c41f0b9de2（本仓第一张"投影表"）
    ],
)
async def test_columns_from_every_era_are_present(db_session, table_name, column_name):
    got = await db_session.scalar(
        text("SELECT 1 FROM information_schema.columns "
             "WHERE table_name = :t AND column_name = :c"),
        {"t": table_name, "c": column_name},
    )
    assert got == 1, f"{table_name}.{column_name} 不存在 —— 有迁移没跑穿"


def test_upgrade_head_is_idempotent():
    """同一份 head 再 upgrade 一次必须安静退 0：CI 与本机都会对**同一个**库跑两次
    （脚本先跑一次、下一次运行的 session 夹具再跑一次），非幂等就是自毁。
    子进程跑：migrations/env.py 自己 asyncio.run()，不能在测试的 loop 里开第二层。
    PYTHONUTF8=1 打在 alembic 子进程 env 上（不放启动脚本）：GBK 主机用 configparser 的
    locale 编码解析带中文注释的 alembic.ini 会 UnicodeDecodeError，失败点在 ini-parse、
    连库之前 —— 与迁移链无关。放这里，手工起 p6test 再裸跑默认命令也一致，不会退成假 RK1。"""
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
