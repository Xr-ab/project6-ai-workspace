"""arq worker 进程侧（Phase 8b spec §3）：装配点在 settings.py，入口在 __main__.py。

API 进程只经由 arq create_pool 入队，本包之外的代码不感知 arq。
"""
