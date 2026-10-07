"""Repository 层：只做数据读写，不含业务逻辑（见 docs/02-architecture.md §4）。
"""
# Phase 8a：audit 埋点遍布 auth/approval/工具闸各处，显式导出免得各调用方记不全路径
from app.data.repositories import audit_repo, user_repo  # noqa: F401
