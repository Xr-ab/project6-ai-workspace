"""Checkpointer：图执行状态持久化的唯一入口。

Phase 4 起用 InMemorySaver（进程内 dict）。Phase 6 上短期记忆时**故意没有换它**：
    路线 C（docs/11 §3）把记忆放在 task_runs.state 快照这条显式路径上，
    checkpointer 退回原职 —— 只服务「单次 run 内的中断恢复」。

为什么 thread_id 必须保持 run 级（= task_runs.trace_id，见 task_runner.run_task）：
    LangGraph 对同一个 thread_id 的行为是**从上一个 checkpoint 续跑**。把 thread 挂到
    task 上，等于让上一轮的 retry_count / data_results / review 自动灌进新一轮，
    docs/11 §4.1 白名单禁掉的那些东西会绕过设计回来，而且只在跑第二三轮时才暴露。

口子已于 Phase 7 兑现（记账）：
    当年写的唯一触发条件——「Phase 7 的 Workflow 需要停在人工审批节点、进程重启后
    从那一步接着跑」——如期到达。checkpointer 换轴 AsyncPostgresSaver：checkpoint
    落 Postgres，跨进程按 thread_id 续跑可用。InMemorySaver 就此退役，且**不许静默
    回退**（回退=重启丢状态的假安全，见 get_checkpointer 的 RuntimeError）。
    双驱动（应用主链路 asyncpg、saver 侧 psycopg）是审批语义的买价，docs/11 §3。

原句存档（换轴前的「触发条件」段注释历史，逐字保留）：
    切 AsyncPostgresSaver 的触发条件（唯一，别提前装 psycopg）：
        Phase 7 的 Workflow 需要「停在人工审批节点、进程重启后从那一步接着跑」。
        在那之前跨进程恢复用不上，而记忆已经在 Postgres 里（task_runs.state），重启不丢。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url

from app.core.config import settings

# 模块级私有单例：_saver 只在 ensure_setup 成功后存在——
# 「先异步 setup、后同步 get」的分工让编译图这类同步上下文拿对象即可
_pool: AsyncConnectionPool | None = None
_saver: AsyncPostgresSaver | None = None
# ensure_setup 的并发保护：无锁的「查-等-写」会让并发调用各建一池，
# 后写覆盖前者 → 前一个池永不关闭（连接泄漏）。Python 3.10 起 Lock
# 构造不绑 loop，模块级创建安全。
_setup_lock = asyncio.Lock()
# 关池后的缓存失效回调（图编译单例等持有旧 saver 的模块级缓存在此复位，
# 见 agent_task_service.register_shutdown_hook 的接入）
_shutdown_hooks: list[Callable[[], None]] = []


def register_shutdown_hook(hook: Callable[[], None]) -> None:
    """注册 shutdown_checkpointer() 后同步回调（幂等：同一 hook 只登记一次）。"""
    if hook not in _shutdown_hooks:
        _shutdown_hooks.append(hook)


def _to_psycopg_dsn(database_url: str) -> str:
    """app 链路的 asyncpg DSN → psycopg DSN：先 make_url 校验，再只换 scheme。

    只动 scheme、不碰 userinfo：百分号编码原样透传给 psycopg，
    密码含特殊字符时 urllib.parse 往返不变形（见 Task 4 测试①）。
    """
    make_url(database_url)  # 非法 URL 在这里就炸，不放行到连接层
    dsn = str(database_url).replace("postgresql+asyncpg://", "postgresql://", 1)
    # 结果必须仍是裸 postgresql://：sqlite 或已带其它 driver 的串在这里挡住，
    # 不让 replace 静默 no-op 把怪 DSN 放行到连接层才炸
    if not dsn.startswith("postgresql://"):
        raise ValueError("database_url 必须是 postgresql(+asyncpg) DSN，派生结果应以 postgresql:// 开头")
    return dsn


def _require_supported_loop() -> None:
    """win32 fail-fast：psycopg 3 async 直接拒绝 ProactorEventLoop（建连时抛
    InterfaceError），而池 open(wait=False) 会把失败推迟到首次 checkpoint 写入
    （PoolTimeout 假象，几十秒才炸）。在入口把「用错了 loop」换成人话。

    背景：`python -m uvicorn app.main:app` 在 win32 不走 event loop policy——
    uvicorn Config.get_loop_factory() 显式返回 ProactorEventLoop，main.py 顶部
    设的 Selector 策略被绕过。开发机请用 `python run_dev.py`（注入 Selector loop
    工厂），别裸起 uvicorn。
    """
    if sys.platform != "win32":
        return
    if isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop):
        raise RuntimeError(
            "psycopg 需要 Selector event loop，当前是 ProactorEventLoop："
            "请用 `python run_dev.py` 启动开发服务（或脚本入口先设 "
            "WindowsSelectorEventLoopPolicy）；裸 `python -m uvicorn` 会被 "
            "uvicorn 的 loop 工厂强制切回 Proactor，模块级策略会被绕过。"
        )


def get_checkpointer() -> AsyncPostgresSaver:
    """进程内单例。未 setup 直接抛——不许静默回退 InMemorySaver（假安全）。"""
    if _saver is None:
        raise RuntimeError("先调 ensure_setup()")
    return _saver


async def ensure_setup() -> None:
    """开池 + 建 saver 自有表。重复/并发调用安全（锁内双检幂等）。

    lifespan 在首次建图前调用；win32 上若跑在 Proactor loop 直接人话报错，
    不留到首次 checkpoint 写入才炸 PoolTimeout 假象。
    """
    global _pool, _saver
    _require_supported_loop()  # 必须 get_running_loop()，放锁前——错 loop 连锁都不配拿
    if _saver is not None:
        return
    async with _setup_lock:
        if _saver is not None:  # 双检：等锁期间别的协程已开好池
            return
        pool = AsyncConnectionPool(
            _to_psycopg_dsn(settings.database_url),
            min_size=1,
            max_size=5,
            open=False,  # 构造保持同步友好；开启动作由下面的 await pool.open() 负责
            # 这两个 kwargs 是 AsyncPostgresSaver 官方对连接池的硬性要求，别删：
            #   autocommit=True     —— checkpoint 读写不搭外层事务的车
            #   prepare_threshold=0 —— 关服务端预编译语句，池化连接轮换不会撞上 prepare 状态
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        try:
            await pool.open()  # open 也在罩内：自身抛错同样不留悬挂 worker
            saver = AsyncPostgresSaver(pool)
            await saver.setup()  # CREATE TABLE IF NOT EXISTS…，天然幂等
        except BaseException:
            await pool.close()  # 失败不留悬挂池
            raise
        _pool, _saver = pool, saver


async def shutdown_checkpointer() -> None:
    """关池并复位单例：重启/重测后 ensure_setup 一定拿到全新连接对象。

    注意在飞请求口径：复位只挡住之后的 get_checkpointer()（→ RuntimeError）；
    已持有旧 saver 的在飞执行拿到的是 PoolClosed/PoolTimeout 类错误，不是 RuntimeError。
    """
    global _pool, _saver
    pool, _pool, _saver = _pool, None, None  # 先复位：不把正在关闭的池发给别人
    if pool is not None:
        await pool.close()
    # 关池后失效依赖旧 saver 的模块级缓存（如 agent_task_service._graph）：
    # 不复位的话，同进程内 shutdown→ensure_setup 之后旧编译图仍绑死池，
    # 后续请求 PoolClosed 而不是重新拿新单例。hook 是同步函数，异常照常上抛。
    for hook in list(_shutdown_hooks):
        hook()


def discard_singletons() -> None:
    """**丢弃**（而不是关闭）绑在已死 loop 上的单例：#23 中毒环的第一段修法。

    为什么需要它而不是复用 shutdown_checkpointer()：arq 0.28.0 的 Worker.close() 里
    `await self.pool.delete(health_check_key)`（worker.py:874）**先于** on_shutdown
    （:875-876）。redis 抖一次，:874 就抛，on_shutdown 按构造不可达 ⇒ 唯一会复位单例的
    shutdown_checkpointer() 根本没跑，旧池还挂在 _pool 上而它的 loop 已经关了。

    这里刻意 **不 await 旧池的 close()**：旧 loop 已经死了，await 它等于把复位路径重新挂在
    一个会抛的调用上——那正是原 bug 的形状。诚实代价：旧 AsyncConnectionPool 的连接交给
    GC 与服务器侧超时回收；在「loop 已死」这个前提下不存在可能的真清理，因为那些连接
    绑在已关闭的 loop 上（spec §8 诚实代价段，不许粉饰成"已优雅关闭"）。

    契约两条，由 test_23_discard_fires_hooks_and_never_raises 钉住：
      1. 一定跑 _shutdown_hooks（图编译缓存失效的唯一通道，见 register_shutdown_hook）；
      2. **不因 hook 的常规失败外抛**——复位路径一抛就回到「守护带着中毒单例复活」的原
         bug 形状，所以 hook 内部抛出的 Exception 只进日志（契约针里那条会抛的 hook 钉的
         就是这条）。口径限定，别读成"什么都吞"：下面的捕获是 `except Exception` 而不是
         BaseException，KeyboardInterrupt（Ctrl+C）、SystemExit（停机）、
         asyncio.CancelledError 都**按设计穿出** discard——守护侧靠
         `except (KeyboardInterrupt, SystemExit): raise` 干净退场，把 Ctrl+C 吞成一行日志
         继续重启比它外抛更糟。
    """
    global _pool, _saver, _setup_lock
    _pool, _saver = None, None
    # 锁也必须换：asyncio.Lock 第一次被 await 时把 waiter 记在当前 loop 上，
    # 复用旧锁会让新 loop 上的 ensure_setup() 抛或静默死锁。
    # 构造本身不绑 loop（Python 3.10+，见文件顶部 :41-44 的既有注释），所以这里安全。
    _setup_lock = asyncio.Lock()
    log = logging.getLogger(__name__)
    for hook in list(_shutdown_hooks):
        try:
            hook()
        except Exception:                 # noqa: BLE001 —— 契约 2 的窄面：只吞 hook 的常规异常，BaseException 按设计穿出
            log.exception("checkpointer shutdown hook failed during discard")
