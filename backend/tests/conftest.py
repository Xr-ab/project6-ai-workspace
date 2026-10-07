"""测试底座共用夹具（Phase 11b，spec §6）。

win32 loop 策略照 app/workers/__main__.py:16-21 的形状：psycopg 3 拒绝
ProactorEventLoop，checkpointer._require_supported_loop() 会 fail-fast 成人话报错。
只在策略仍是 Windows 默认（Proactor）时才切，不覆盖宿主/测试框架自设的策略。
"""
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

BACKEND_ROOT = Path(__file__).resolve().parents[1]

if sys.platform == "win32":
    import asyncio

    if isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsProactorEventLoopPolicy):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest


@pytest.fixture
def run_python():
    """在 backend/ 目录下用受控 env 跑一段 python 源码，返回 (rc, stdout, stderr)。

    为什么要子进程：`app/core/config.py:105` 的生产密钥门是 **import 期** 抛的
    RuntimeError，同进程内已经 import 过 settings 就再也测不到那条路径。
    env 覆盖走 os.environ 而不是 .env：pydantic-settings 的优先级里环境变量赢过
    env_file，所以本机 backend/.env 里真密钥不会污染断言（本函数也不读那个文件）。
    """

    def _run(source: str, **env_overrides: object) -> tuple[int, str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(BACKEND_ROOT)
        env.update({k: str(v) for k, v in env_overrides.items()})
        proc = subprocess.run(
            [sys.executable, "-c", source],
            cwd=str(BACKEND_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        return proc.returncode, proc.stdout, proc.stderr

    return _run


# ============ 集成层：隔离守卫（spec §6 硬闸 / §12 RK8） ============
#
# 住在 conftest 而不是各测试文件里，理由只有一条：守卫晚于任何一条 db 用例存在，
# 就等于那条用例已经连过一次真库。所以它跟着夹具走，谁要会话谁先过闸。


class UnsafeTarget(ValueError):
    """URL 指向本机默认项目（真库 / 真 redis）或 Phase 10 的外部库存域。

    继承 ValueError 而不是自造 BaseException：守卫失败的语义就是「配置错了」，
    而「停下来」这个动作由 require_isolated_targets() 翻译成 pytest.exit。
    """


DEFAULT_PG_PORT = 5432
DEFAULT_REDIS_PORT = 6379
FORBIDDEN_DB_NAMES = ("enterprise_data",)
GUARD_HINT = (
    "集成层拒绝启动：先跑 docker/run_integration_local.sh"
    "（一次性 compose 项目 p6test，宿主端口 5433 / 6380）"
)


def _ci_runner() -> bool:
    """GitHub 的 service container 就发布在 localhost:5432，与本机禁令正面冲突。

    裁定（R-CI，写进 docs/02 §11）：这条守卫守的是**本机攒了几个月的真卷**；
    runner 里的 postgres 是每次运行新建、随 runner 销毁的一次性容器，
    「误连真库 = 数据事故」的前提在 CI 上不成立 ⇒ CI 放行、本机严格。
    判据只认 runner 注入的字面 "true"，不接受 1/TRUE 之类的自造豁免面
    （test_only_the_literal_true_bypasses 钉住这条）。
    """
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _target(url: str) -> tuple[str, int, str]:
    """拆出 (host, port, 库名)；port=0 表示 URL 里没写端口（= 用驱动默认，正是要拦的形状）。

    只解析字符串，**一次都不连接**：守卫若在判定时连一次库，它自己就成了那次事故。
    """
    parsed = urlsplit(url)
    return (parsed.hostname or "", parsed.port or 0, parsed.path.lstrip("/"))


def assert_isolated_database(url: str) -> None:
    if _ci_runner():
        return
    host, port, db = _target(url)
    if port in (0, DEFAULT_PG_PORT):
        raise UnsafeTarget(
            f"DATABASE_URL 指向默认端口 {DEFAULT_PG_PORT}（host={host or '未写'}）："
            f"那是本机 p6-postgres 的真库。{GUARD_HINT}"
        )
    if db in FORBIDDEN_DB_NAMES:
        raise UnsafeTarget(
            f"DATABASE_URL 指向 {db}：集成层一个字都不该碰 Phase 10 的外部库存域。{GUARD_HINT}"
        )


def assert_isolated_redis(url: str) -> None:
    if _ci_runner():
        return
    host, port, _ = _target(url)
    if port in (0, DEFAULT_REDIS_PORT):
        raise UnsafeTarget(
            f"REDIS_URL 指向默认端口 {DEFAULT_REDIS_PORT}（host={host or '未写'}）："
            f"那是本机 p6-redis。{GUARD_HINT}"
        )


def require_isolated_targets(database_url: str, redis_url: str) -> None:
    """夹具入口：把 UnsafeTarget 换成 pytest.exit。

    为什么是 exit 而不是 fail：一条误连真库的用例失败之后，后面的用例会继续连、继续写。
    要的是整轮当场停，不是刷一屏红。returncode=7 是本仓自定的「隔离守卫拒绝」，
    与 pytest 自带的 1（有失败）/2（中断）/3（内部错）/4（用法错）/5（无用例）都不撞。
    """
    try:
        assert_isolated_database(database_url)
        assert_isolated_redis(redis_url)
    except UnsafeTarget as exc:
        pytest.exit(str(exc), returncode=7)


def require_isolated_for_markers(
    database_url: str, redis_url: str, *, db: bool, redis: bool
) -> None:
    """按 marker 落闸：只判被标的那一侧（db 用例不该因 redis_url 脏被误拒，反之亦然；
    两个都标如 test_sweeper 则两腿都判）。走的是与 require_isolated_targets 同一条
    UnsafeTarget → pytest.exit(7) 路径 —— 复用既有停机语义，不另造第二套机制。

    存在的理由（spec §6「守卫先于任何 integration 用例」）：require_isolated_targets 只挂在
    isolated_urls→db_session 这条夹具链上，而 test_checkpointer 的 ensure_setup()、
    test_migrations 的 alembic 子进程、test_rate_limit_redis 的 get_redis() 都不经过 db_session，
    于是一条都不过闸地开真 socket。_isolation_gate（autouse、marker 门控）补上这条链：
    用例体动手之前，按 marker 判一次，脏了就整轮退 7。"""
    try:
        if db:
            assert_isolated_database(database_url)
        if redis:
            assert_isolated_redis(redis_url)
    except UnsafeTarget as exc:
        pytest.exit(str(exc), returncode=7)


# ============ 集成层：夹具（spec §5.3 R-ENV） ============


@dataclass(frozen=True)
class Identity:
    """一次集成用例里现造的一对身份 + 可直接进 Authorization 头的 token。"""

    organization_id: uuid.UUID
    user_id: uuid.UUID
    token: str


@pytest.fixture(scope="session")
def isolated_urls() -> tuple[str, str]:
    """session 级闸：db 夹具链（本夹具 → migrated_schema → db_session）第一次被要时落一次。

    刻意不做成 autouse：本机 `pytest -m "not db and not redis"` 根本不跑集成层，
    若那时就因为 settings 默认指向 5432 而整轮 exit，是守卫的假红 ——
    而假红的代价是后人把守卫拆掉。

    注意这条链只覆盖「要 db_session 的用例」。不走 db_session 的真 socket 面
    （checkpointer.ensure_setup / alembic 子进程 / get_redis）由 marker 门控的 autouse
    夹具 `_isolation_gate` 逐条兜底（spec §6「先于任何 integration 用例」的正解）。
    """
    from app.core.config import settings

    require_isolated_targets(settings.database_url, settings.redis_url)
    return (settings.database_url, settings.redis_url)


@pytest.fixture(scope="session")
def migrated_schema(isolated_urls) -> None:
    """RK1 的现场：alembic upgrade head 在全新空库上跑穿。

    子进程跑而不是 in-process：migrations/env.py:70 自己 asyncio.run()，
    在 pytest-asyncio 已持有 loop 的用例里再开一层 loop 是要炸的。
    失败用 pytest.exit：库里连表都没有时，后面每条 db 用例都会以
    "relation does not exist" 的形状再失败一次，把根因埋在几十条噪音底下。

    PYTHONUTF8=1 打在**消费者**（alembic 子进程）的 env 上而不是启动脚本里：
    GBK 区域的主机无法用 configparser 的 locale 编码解析带中文注释的 alembic.ini，
    失败点在 ini-parse、连库之前 —— 与迁移链本身无关。放这里，无论是否经
    run_integration_local.sh 起（有人手工起 p6test 再裸跑 pytest 也一样）行为都一致；
    放进启动脚本反而让"手工起 + 默认命令"退成 pytest.exit(7) 的假 RK1 警报。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.exit(
            f"alembic upgrade head rc={proc.returncode}\n"
            f"--- stderr 尾 ---\n{proc.stderr[-3000:]}",
            returncode=7,
        )


@pytest.fixture
async def db_session(migrated_schema):
    """一条用例一个会话，**并且用例结束 dispose 引擎**。

    dispose 不是礼节而是必需：pytest-asyncio 每条用例一个新 event loop，
    而 app/data/db.py:30 的 engine 是模块级单例，池里的 asyncpg 连接绑在
    创建它的那个 loop 上。不 dispose，第二条 db 用例会在已关闭的 loop 上取到旧连接，
    报 "Task got Future attached to a different loop"——那是夹具的形状问题，
    不是被测代码的问题，误判成 bug 会一路查到仓储层去。
    """
    from app.core.config import settings
    from app.data.db import AsyncSessionLocal, engine

    assert_isolated_database(settings.database_url)  # 双保险：夹具与 session 各判一次
    async with AsyncSessionLocal() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def api_client(db_session):
    """ASGI 直连的 HTTP 客户端（不起端口、不占 8000/8002/8003）。

    依赖 db_session 只为三件事：过闸、保证 schema 已迁移、拿到 engine.dispose 的收尾。
    注意 httpx 的 ASGITransport **不发 lifespan 事件** ⇒ app.state.arq_pool 不存在 ⇒
    本层只允许 GET 读面与仓储直写，POST /tasks 这类要入队的端点一律不在这里测
    （那是 11c 的 load test / 容器验收面）。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def make_identity(db_session):
    """现造 (org, user) 并铸 access token。

    为什么绕开 /auth/register 与 scripts/seed_dev_users.py：
      - 注册面要口令，"测试口令字面量"会进 git diff 并被 secret_scan 的形状判据盯上；
        create_access_token 直铸，身份链上 org/user/role 三要素与真登录一字同形。
      - seed 脚本固定种三个 dev 账号，用例之间会互相看见对方的行。
    email 带 uuid 是为了 users.email 的 unique 约束不炸；@example.org 是保留域，
    永远不会真有人收信。前提是 settings.jwt_secret 非空（本机由 backend/.env 给，
    CI 由 §7 的 JWT_SECRET=test-only-not-a-real-secret 给）。
    """
    from app.core.security import create_access_token
    from app.data.models import Organization, User

    async def _make(
        role: str = "member", *, organization_id: uuid.UUID | None = None
    ) -> Identity:
        """role 仍是第一位置参（既有 `make_identity("admin")` / `role=` 调用不受影响）。

        organization_id 给定则**不新建 org**、只在该 org 下再造一个 user ——
        用来演「同 org 不同 user」这条 user 腿（test_auth_isolation 的
        test_same_org_other_user_is_also_404）。不给就是老行为：新 org + 新 user。
        """
        if organization_id is None:
            org = Organization(id=uuid.uuid4(), name=f"p6test-org-{uuid.uuid4().hex[:8]}")
            db_session.add(org)
            await db_session.flush()
            org_id = org.id
        else:
            org_id = organization_id
        user = User(
            id=uuid.uuid4(),
            organization_id=org_id,
            email=f"p6test-{uuid.uuid4().hex[:12]}@example.org",
            full_name="集成层探针",
            role=role,
        )
        db_session.add(user)
        await db_session.commit()
        return Identity(
            organization_id=org_id,
            user_id=user.id,
            token=create_access_token(user.id, org_id, role),
        )

    return _make


@pytest.fixture(autouse=True)
async def _isolation_gate(request):
    """marker 门控的隔离硬闸 + redis 单例复位：每个 integration 用例体动手之前先落闸。

    同一个 autouse 夹具做两件事：

    1) 过闸（spec §6「守卫必须先于任何 integration 用例」）：`db` marker → 判
       settings.database_url，`redis` marker → 判 settings.redis_url；脏了就
       require_isolated_for_markers → pytest.exit(7)，在**用例体**之前整轮停。
       这一层专治不走 db_session 的真 socket 面（checkpointer.ensure_setup 开池、
       test_migrations 的 alembic 子进程、rate_limit/sweeper 的 get_redis）：
       旧链路上它们一条都不过闸，只靠 test_auth_isolation 恰好字母序排第一、其
       db_session 顺手 require 一次的巧合才没出事。db 侧的 assert 在 db_session
       (双保险) 与这里各判一次，redis 侧此前夹具链根本没判 ⇒ 这条补齐两侧对称。

    2) redis 单例复位 + 关闭（与 db_session 的 engine.dispose、checkpointer 的
       clean_singletons 同一条 loop-per-test 纪律的第三个分身）：app.core.security.
       _redis_client（get_redis() 缓存，rate_limit 与 task_cache 共用）是进程级单例，
       其池里首个 Connection 绑在首次命令所在 loop；pytest-asyncio 每条用例一个新 loop
       ⇒ 不复位则第二条 redis 用例拿到死连接。收尾**必须 aclose()**：只置 None 会
       每条 redis 用例漏一个客户端 + 池 socket。aclose 是协程 ⇒ 夹具取 async；
       redis 用例与其 teardown 都在当前 loop 上跑，关得掉。

    离线层（两个 marker 都没有）直接 yield 放行：不判 URL、不建 loop 的活、不碰单例
    （marker 门控是「离线 97 不受扰动」的前提）。
    """
    node = request.node
    has_db = node.get_closest_marker("db") is not None
    has_redis = node.get_closest_marker("redis") is not None
    if not (has_db or has_redis):
        yield
        return

    from app.core.config import settings

    require_isolated_for_markers(
        settings.database_url, settings.redis_url, db=has_db, redis=has_redis
    )
    if not has_redis:
        yield
        return

    from app.core import security

    security._redis_client = None
    try:
        yield
    finally:
        client, security._redis_client = security._redis_client, None
        if client is not None:
            await client.aclose()
