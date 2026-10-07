"""FastAPI 应用入口：注册中间件、异常处理、路由。"""
import sys
import uuid
from contextlib import asynccontextmanager

# Windows 开发机：psycopg 3（checkpointer 池）拒绝 ProactorEventLoop，入口统一切
# Selector 策略——asyncpg 主链路在 Selector 下同样工作，双驱动共存一个 loop。
# 策略必须在任何 loop 创建前设置，故放在模块导入最前；非 win32 无此问题不加戏。
# 注意：本策略只对被 policy 建 loop 的入口生效（asyncio.run / TestClient）。
# 裸 `python -m uvicorn app.main:app` 会被 uvicorn 的 loop 工厂强建 Proactor、
# 绕过策略——win32 起开发服务请用 `python run_dev.py`（显式注入 Selector loop
# 工厂）；万一用错入口，checkpointer.ensure_setup() 会 fail-fast 成人话报错。
if sys.platform == "win32":
    import asyncio

    # 仅当策略还是 Windows 默认（Proactor）时才切，不覆盖宿主/测试框架自设的策略
    if isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsProactorEventLoopPolicy):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from arq import create_pool
from arq.connections import RedisSettings

from app.api import (
    agents,
    auth,
    chat,
    conversation,
    documents,
    evaluations,
    reports,
    stats,
    workflows,
)
from app.ai.graph.checkpointer import ensure_setup, shutdown_checkpointer
from app.core.audit import REQUEST_ID
from app.core.config import settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging_setup import setup_logging
from app.data.db import AsyncSessionLocal


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 8b T2：结构化 Logging 底座先于一切装配（spec §7）——API 进程从这行起
    # 出 JSON 制式，request_id 由 Filter 从中间件 contextvar 注入每行，与 audit 行对账。
    # 留痕：uvicorn 0.53 的 dictConfig 在 Config.__init__ 就执行、且「Started server
    # process」行早于 lifespan——生产 run_dev.py 起进程时这两行仍是文本制式（其余全 JSON）。
    setup_logging(settings.log_level)
    # Phase 7：checkpointer 池必须在首次建图前就绪——agent_task_service 的模块级
    # _graph 惰性编译，首个请求到来时 lifespan 已跑完 ensure_setup；关闭时 dispose 池
    await ensure_setup()
    # Phase 8b：API 侧入队池（spec §3.1——API 进程只入队不执行，执行面在 worker）。
    # app.state 是 FastAPI 正统挂点；app.arq_pool 直持引用是同对象的第二名字（接口契约，
    # 便于不经 request 的装配处取用），关闭时一并置 None 防拿到已 aclose 的池。
    # R-8b-7：create_pool 会 ping 并在 broker 够不着时 raise——不兜底则 Redis 宕机
    # API 直接起不来，砸掉 8a「Redis 宕机 API 照常起、/healthz 如实报 degraded」的口径
    # （P8-T8-4 redis-down 冒烟的前提就是能起）。兜底置 None 保住如实报；healthz 的
    # queue 探针走 ping_queue 直连不吃此池，不受影响；入队面见 None 抛 503 的分支归 Task 4
    try:
        app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    except Exception:  # noqa: BLE001
        app.state.arq_pool = None
    app.arq_pool = app.state.arq_pool
    yield
    if app.state.arq_pool is not None:
        await app.state.arq_pool.aclose()
    app.state.arq_pool = None
    app.arq_pool = None
    await shutdown_checkpointer()


app = FastAPI(title="Enterprise AI Workspace", version="0.1.0", lifespan=lifespan)

# CORS：允许前端开发服务器跨域访问
# Phase 8b T11 修复轮 1（I-1）：expose_headers 放行 Retry-After——它不在 CORS 安全
# 响应头集合里，不加则跨源（前端 5173 → API 8002、vite 无 proxy）fetch 读不到，
# client.ts 的 429「约 N 秒后可再试」秒数分支恒死。ExceptionMiddleware（异常处理器
# 所在层）在 CORS 之内，429 响应冒泡时同样被裹上 expose 头。只加这一参数，白名单不动。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

register_exception_handlers(app)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    """06 §1.3 补账：每请求一个 request_id，进 X-Request-Id 响应头 + 审计行。

    不进响应体（Global 裁定 1）：33 端点的裸 JSON 形状已是前端消费中的契约。
    """
    rid = "req_" + uuid.uuid4().hex[:12]
    token = REQUEST_ID.set(rid)
    try:
        response = await call_next(request)
    finally:
        REQUEST_ID.reset(token)
    response.headers["X-Request-Id"] = rid
    return response


@app.get("/healthz")
async def health() -> dict:
    """探活（spec §4：8a 把 simple 版升级为 pg+redis 双面；8b 补第三面 queue）。

    降级如实报：degraded 就是 degraded——healthz 是给部署和排查看的，
    把半死不活报成 ok 等于拆自己的监控。queue 探针对齐 rd 同源口径：
    只答「Redis 当 broker 通不通」，队列空也算通（spec §3.3）。
    """
    from sqlalchemy import text

    from app.core.redis_ping import ping_queue, ping_redis

    pg = True
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        pg = False
    rd = await ping_redis()
    q = await ping_queue()
    return {"status": "ok" if (pg and rd and q) else "degraded", "env": settings.app_env,
            "postgres": pg, "redis": rd, "queue": q}


# 业务路由
app.include_router(auth.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(conversation.router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(evaluations.router, prefix="/api/v1")
app.include_router(workflows.router, prefix="/api/v1")
# Phase 9b：报告独立资源（列表 / 详情 / 删除）。此前报告正文只挂在
# task_runs.meta.report 上，`/reports` 页因此只能挂占位符。
app.include_router(reports.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")
