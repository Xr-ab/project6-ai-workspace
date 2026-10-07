"""MCP 数据服务包（Phase 10 spec §3.3）。

⚠️ 本文件**绝不 import server**：
    `import app.mcp` 会发生在 API / worker 进程的任何 `from app.mcp.x import y` 之前。
    若这里 import 了 server，那两个进程就顺手把 FastMCP 实例、uvicorn 依赖、
    enterprise 连接池配置全拖进来——「独立进程」当场变成假的，
    而且 `python -m app.mcp.server` 的独立性再也没人验证过（spec §7 针⑨ 的 grep 面同理）。

包内分工：
    inventory_data.py  enterprise_data 的唯一数据访问层（纯 SQL，不知道 MCP 协议存在）
    server.py          协议面 + 进程入口（FastMCP + /healthz + python -m app.mcp.server）
"""
