"""逐表精确计数（不是 pg_stat 的估算）。before/after 各跑一次，diff 必须为空。

形状沿用 11a 的 scratch 版（同一份输出格式），做了三处加固：
  1. 换库用 urlsplit 而不是 rsplit("/")——表名/库名里出现斜杠时旧写法会静默错位；
  2. --host-port 让同一个脚本能连本机默认项目（5432）与一次性 demo 栈（5543）；
  3. --out 直接写 UTF-8 文件，Windows 控制台（GBK）不参与证据生成。

DSN 从 settings.database_url 起步（除它之外没有第二个凭据面）。措辞要说准：
本脚本不**直接**读环境文件，但它 `from app.core.config import settings`，而 Settings
是带环境文件路径实例化的——import 那一下就间接读到了 backend 的那份环境配置；
口令面刻意原样透传：本脚本永不打印完整 DSN，只打印 host:port。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.core.config import settings  # noqa: E402


def build_dsn(base_url: str, db: str, *, host: str | None = None, port: int | None = None) -> str:
    """把 SQLAlchemy 风格 DSN 换成 asyncpg 风格，并替换库名/宿主端口。"""
    parts = urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"不是可用的 DSN: {base_url!r}")
    netloc = parts.netloc
    if host is not None or port is not None:
        userinfo, _, _host = netloc.rpartition("@")
        h = host if host is not None else _host.split(":")[0]
        n = port
        if n is None:
            raise ValueError("换 host 必须同时给 port")
        netloc = f"{userinfo}@{h}:{n}" if userinfo else f"{h}:{n}"
    return urlunsplit((parts.scheme.split("+")[0], netloc, f"/{db}", "", ""))


def dsn_host_port(dsn: str) -> tuple[str, int]:
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    return host, int(parts.port or 5432)


def render_counts(rows: list[tuple[str, int]]) -> list[str]:
    """rows 的表名已是 `db.table` 全名（_dump 拼），这里只排序与格式化。"""
    return [f"{name}={count}" for name, count in sorted(rows)]


def fingerprint(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


async def _dump(conn, db: str) -> list[tuple[str, int]]:
    tables = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    rows: list[tuple[str, int]] = []
    for t in tables:
        name = t["tablename"]
        n = await conn.fetchval(f'SELECT count(*) FROM "{name}"')
        rows.append((f"{db}.{name}", int(n)))
    return rows


async def _main(args: argparse.Namespace) -> None:
    base = settings.database_url
    lines: list[str] = []
    for db in args.db:
        dsn = build_dsn(base, db, host="localhost", port=args.host_port)
        conn = await asyncpg.connect(dsn)
        try:
            lines.extend(render_counts(await _dump(conn, db)))
        finally:
            await conn.close()
    lines.append(f"FINGERPRINT={fingerprint(lines)}")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    host, port = dsn_host_port(build_dsn(base, args.db[0], host="localhost", port=args.host_port))
    print(f"census target={host}:{port} tables={len(lines) - 1}", file=sys.stderr)
    print(text, end="")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="逐表计数 + 指纹（前后各跑一次，diff 必须为空）")
    p.add_argument("--db", action="append", required=True, help="可重复；如 ai_workspace enterprise_data")
    p.add_argument("--host-port", type=int, default=5432, help="本机默认 5432；demo 栈 5543")
    p.add_argument("--out", default="", help="把同一份文本另存为 UTF-8 文件")
    return p.parse_args(argv)


if __name__ == "__main__":
    asyncio.run(_main(parse_args(sys.argv[1:])))
