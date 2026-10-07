"""arq WorkerSettings（spec §3.1）：worker 进程的唯一装配点。

生命周期裁定：
- on_startup：checkpointer 池必须显式 setup（不 ensure 就建图会拿 None saver 假安全——
  API 侧 lifespan 同款纪律）；asyncpg 引擎与 redis 客户端是模块级单例（app.data.db /
  app.core.security.get_redis），import 即用，**不在 shutdown dispose**——
  engine.dispose() 会杀掉模块级单例本体，worker 重启场景（arq 不会）之外无人重建，
  与 API 进程既有口径一致（uvicorn 重载 = 新进程 = 新引擎）。
- max_jobs：在飞模型调用上限，零钱纪律的机制化。
"""
import logging

from arq import cron

from app.ai.graph.checkpointer import ensure_setup, shutdown_checkpointer
from app.core.config import settings
from app.workers.jobs import (
    index_document,
    run_agent_task,
    run_evaluation,
    workflow_execute,
    workflow_resume,
)
from app.workers.sweeper import sweep_all

logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    await ensure_setup()
    logger.info("worker startup: checkpointer pool ready")


async def shutdown(ctx: dict) -> None:
    await shutdown_checkpointer()
    logger.info("worker shutdown: checkpointer pool disposed")


class WorkerSettings:
    functions = [run_agent_task, workflow_execute, workflow_resume, run_evaluation, sweep_all, index_document]  # T4 agent 面 + T5 workflow 面 + T6 evaluation 面 + T7 sweep_all（作 job 也可直接被 cron 调）+ 排雷-F 文档索引面
    # T7 僵尸清扫 cron：second={0} = 每分钟第 0 秒触发 = 60s 一轮（arq 0.28 实读：
    # CronJob.second 字段；Worker 构造把 cron_jobs 自动注册成 cron:sweep_all job）。
    cron_jobs = [cron(sweep_all, second={0})]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = settings.worker_max_jobs
    redis_settings = None   # 在 __main__ 里装配（settings.redis_url 是 DSN，不硬编码第二份）
