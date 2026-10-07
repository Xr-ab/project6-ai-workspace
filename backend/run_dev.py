"""开发机启动器（win32 必读）：把 uvicorn 真实钉在 Selector event loop 上。

为什么不能裸跑 `python -m uvicorn app.main:app`：
    psycopg 3 async 在 Windows 拒绝 ProactorEventLoop（建连即 InterfaceError），
    app/main.py 顶部设置的 WindowsSelectorEventLoopPolicy 只对「走 policy 建 loop」
    的入口（asyncio.run / TestClient）有效。uvicorn 不建 policy 的 loop：
    Server.run → config.get_loop_factory() 在 win32（无 --reload/--workers）
    显式返回 ProactorEventLoop，完全绕开 policy。又因池 open(wait=False)，
    症状是「服务看着起得来，第一条 checkpoint 写入挂几十秒抛 PoolTimeout」。

做法：给 uvicorn Config 注入自定义 loop 工厂（uvicorn≥0.36 支持 loop=<callable>，
import_from_string 对非字符串原样返回），Server 就跑在 Selector loop 上。
非 win32 无此问题，回退 "auto"（Linux 生产不用本文件）。

跑法（backend/ 下，需 PYTHONPATH=.）：
    .venv/Scripts/python.exe run_dev.py   →  http://127.0.0.1:8002
"""
import asyncio
import sys

import uvicorn


def _selector_loop_factory() -> asyncio.AbstractEventLoop:
    """新建 Selector loop 并登记为当前 loop（uvicorn 的自定义工厂分支不代做）。"""
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    return loop


def main() -> None:
    loop = _selector_loop_factory if sys.platform == "win32" else "auto"
    config = uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=8002,
        loop=loop,  # win32：绕开 get_loop_factory 的 Proactor 分支
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
