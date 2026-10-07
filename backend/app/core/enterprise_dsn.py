"""enterprise_data 的 DSN 派生（Phase 10 spec §3.1）。

为什么只有这一个函数、且它是纯函数（不碰网络、不建 engine）：
    派生错了的表现是「应用连回自己的库」——跨源演示照样绿，这是最坏的一类静默错误
    （spec §7 针① 专门钉它）。纯函数 + 反向针能在零依赖面上证成它。

为什么不存整条 DSN 到配置里：
    存两条 DSN = host 与口令有两个来源，改 .env 只改一处就出现「应用连新口令、
    库存域连旧口令」的漂移。存库名，host/口令/端口只有一个真来源（database_url）。
"""

_PREFIX = "postgresql+asyncpg://"


def enterprise_database_url(default_dsn: str, db_name: str) -> str:
    """把 asyncpg DSN 的最后一段库名换成 db_name，其余（scheme/user/password/host/port）原样保留。

    只用 `rpartition("/")` 切最后一个斜杠、不用 `split("/")` 全量重拼：
    口令里含 '/' 时（`a/b@c`）全量重拼会把凭据段当库名换掉——那不是换库名，那是换了个用户。

    非 postgresql+asyncpg 前缀 / 末尾没有库名段 / 尾段带查询串 / db_name 非法 → **ValueError**。
    为什么不能静默返回原串：返回原串 = 应用连自己 = 「跨源」在开发机上根本不存在，
    而所有测试还是绿的（spec §2.2 立论的守护）。
    为什么尾段的查询串也要响：`…/ai_workspace?ssl=require` 的尾段是 `ai_workspace?ssl=require`，
    换掉整段会得到「库名对、连接参数没了」的串——静默改写出错比报错更难查。
    """
    if not default_dsn.startswith(_PREFIX):
        raise ValueError(
            f"只支持 {_PREFIX!r} 前缀，收到：{default_dsn[:24]!r}…"
            "（换驱动必须同时改库存域连接，不能静默沿用）"
        )
    if not db_name:
        raise ValueError("db_name 不能为空")
    if "/" in db_name or "?" in db_name or "@" in db_name:
        raise ValueError(f"db_name 只能是裸库名，收到：{db_name!r}")

    head, sep, tail = default_dsn.rpartition("/")
    # tail 里还有 '@' ⇒ 最后一个斜杠是 "//" 的那一个，说明这条 DSN 根本没写库名段
    if not sep or "@" in tail:
        raise ValueError(f"DSN 末尾没有库名段（应为 …/{db_name}）：{default_dsn[:40]!r}…")
    # tail 里还有 '?' ⇒ 库名段挂着查询串，整段替换会把连接参数静默丢掉（R133）
    if "?" in tail:
        raise ValueError(f"DSN 尾段含查询串，派生会静默丢参：{default_dsn[:40]!r}…")
    return f"{head}{sep}{db_name}"
