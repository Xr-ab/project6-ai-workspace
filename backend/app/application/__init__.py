"""应用服务层（Application Layer）——本包是唯一的应用服务层。

Phase 1 的 chat_service / conversation_service / identity 已于 2026-09-23
从 app/services/ 并入本包。

本层职责：编排（读校验 → 调 repository → 调 ai 层 → 维护状态流转），
不写 SQL（属 app/data/repositories/），不碰 HTTP（属 app/api/）。
"""