"""worker 进程入口：`python -m app.workers`（spec §3.1 两进程拓扑的开发机形态）。

照 app/main.py:13-18 的 win32 Selector 惯例：psycopg 3（checkpointer 池）拒绝
ProactorEventLoop，arq 的 run_worker 走 asyncio 默认策略建 loop，故必须在
run_worker 之前切策略；非 win32 无此问题不加戏。
"""
import sys

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


def _run_worker_guarded() -> None:
    """D-7 出口守护：run_worker 意外抛 → JSON 一行留痕 + 指数退避重启（进程内，不引外部 supervisor）。

    针⑤实测炸点：`docker stop p6-redis` → arq 0.28 Worker.run() 主循环抛连接异常
    （run() 只捕 CancelledError，其余原异常经 finally 向上传），finally 里 close() 的
    `delete(health_check_key)` 再抛 ConnectionError 顶替之、无人接 → 穿破裸 run_worker，
    进程带栈退出＝一次 redis 抖动永久下线。本守护把「下线」变「退避重试」：1s 起倍增、
    封顶 30s、跑满 5 分钟视为环境已稳、退避复位。KeyboardInterrupt/SystemExit（Ctrl+C、
    停机）直接干净退出**不重启**；run_worker 正常返回＝arq 优雅收尾，同样干净退场。
    日志走 setup_logging 的 JSON 制式（traceback 进 exc 字段，单行——针⑦逐行 json.loads 纪律不破）。

    #23（Phase 11b）在此之上加了两段：循环体开头 discard 掉死 loop 的 checkpointer 单例，
    以及 WORKER_EXIT_ON_FATAL 置真时不复活而 raise SystemExit(1) 让容器重启。进程内复活
    只是本机 dev 的形态；容器里复活 = 用一份中毒的单例继续服务。
    """
    import asyncio
    import logging
    import time

    from arq import run_worker

    from app.ai.graph import checkpointer
    from app.core.config import settings
    from app.workers.settings import WorkerSettings

    log = logging.getLogger("app.workers.__main__")
    min_backoff, max_backoff, survive_reset = 1.0, 30.0, 300.0
    backoff = min_backoff
    while True:
        # #23 修法 1：上一条 loop 已死 ⇒ 绑它的 checkpointer 单例（池 / saver / 锁）全部作废。
        # 必须在 new_event_loop() **之前** discard：晚一步的话新 loop 上第一次 ensure_setup()
        # 会走幂等分支复用死池（checkpointer.py:108-109 的 `if _saver is not None: return`，锁内双检 :111-112），
        # 症状就从「进程死」升级成「进程活、任务全死」——比原 bug 更难查。
        checkpointer.discard_singletons()
        # arq Worker.__init__ 经 get_event_loop 取环：每次重启给一条新 loop 并用完即关，
        # 不复用半拆解过的旧环（redis 崩停的那条 loop 上残着已死的连接与任务）。
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        started = time.monotonic()
        crashed = True
        try:
            run_worker(WorkerSettings)
            crashed = False
        except (KeyboardInterrupt, SystemExit):
            raise                       # 用户/进程停机：不留痕、不重启，栈照常向外走
        except Exception:               # redis 抖动等一切意外异常族
            log.error("worker exited unexpectedly; restart after %.0fs backoff",
                      backoff, exc_info=True)
        finally:
            loop.close()
        if not crashed:
            return
        if settings.worker_exit_on_fatal:
            # #23 修法 2：容器形态让进程真的退出，restart: unless-stopped 起全新进程。
            # 这一 raise 在 try 之外，所以不会被上面那句 `except (KeyboardInterrupt,
            # SystemExit): raise` 吞掉，也不会留下"崩了但没人知道"的中间态。
            # 本机 dev 不设 WORKER_EXIT_ON_FATAL ⇒ 走下面的进程内退避重启（双形态裁定）。
            log.error("worker fatal and WORKER_EXIT_ON_FATAL is set; "
                      "exiting for container restart")
            raise SystemExit(1)
        if time.monotonic() - started >= survive_reset:
            backoff = min_backoff       # 本次存活够久：抖动已过，退避钟复位
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    from arq.connections import RedisSettings

    from app.core.config import settings
    from app.core.logging_setup import setup_logging
    from app.workers.settings import WorkerSettings

    # R-8b-7 收敛兑现（T1 的 basicConfig 临时行退场）：worker 进程与 API 出同一
    # JSON 制式，request_id 由同一 Filter 注入（arq 0.28 不代管日志，装配点自己装）。
    setup_logging(settings.log_level)
    # 装配点读一次配置（不在 WorkerSettings 模块导入期读 settings，测试换
    # fakeredis 不受模块期副作用拖累）
    WorkerSettings.redis_settings = RedisSettings.from_dsn(settings.redis_url)
    _run_worker_guarded()
